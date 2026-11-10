"""FastAPI 应用入口（应用工厂模式）。

create_app 负责：建 Engine/Session 工厂 → 建表 → 灌 demo 种子数据 → 装中间件与路由。
工厂模式让测试可以传入独立 Settings（临时 SQLite 库），避免污染开发数据。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

import app.models  # noqa: F401 - register SQLAlchemy models
from app.agent.checkpointer import make_checkpointer
from app.agent.patch_graph import build_patch_graph
from app.api.auth_routes import router as auth_router
from app.api.agent_routes import router as agent_router
from app.api.coach_routes import router as coach_router
from app.api.food_routes import router as food_router
from app.api.mcd_routes import router as mcd_router
from app.api.knowledge_routes import router as knowledge_router
from app.api.routes import router
from app.api.system_routes import router as system_router
from app.config import Settings, get_settings
from app.db import Base, make_engine, make_session_factory
from app.db_migrations import backfill_lifecycle_data, ensure_additive_schema
from app.domain.memory_lifecycle import run_lifecycle_with_factory
from app.llm.deepseek import explainer_from_settings
from app.llm.guard import configure_llm_guard
from app.llm.types import LlmBudget
from app.openapi_docs import mount_api_docs
from app.seed import seed_demo
from app.knowledge.service import seed_knowledge

http_logger = logging.getLogger("planharness.http")
if not http_logger.handlers:
    _http_handler = logging.StreamHandler()
    _http_handler.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))
    http_logger.addHandler(_http_handler)
    http_logger.setLevel(logging.INFO)
    http_logger.propagate = False

lifecycle_logger = logging.getLogger("planharness.memory")
if not lifecycle_logger.handlers:
    _life_handler = logging.StreamHandler()
    _life_handler.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))
    lifecycle_logger.addHandler(_life_handler)
    lifecycle_logger.setLevel(logging.INFO)
    lifecycle_logger.propagate = False


def _memory_maintenance_loop(session_factory, settings: Settings) -> asyncio.Task:
    """后台每日维护；第一次启动后先跑一次，之后按配置间隔。"""

    async def _loop() -> None:
        interval = max(60, int(settings.memory_cleanup_interval_seconds or 0))
        # 先等一个周期再跑第一次：避免测试/开发启动瞬间和请求抢 SQLite 写锁。
        # 启动期的 schema 补齐与旧数据回填已经同步完成；每日清理从下一个周期开始。
        await asyncio.sleep(interval)
        while True:
            try:
                # 放到线程里跑，避免同步数据库操作阻塞事件循环
                result = await asyncio.to_thread(
                    run_lifecycle_with_factory,
                    session_factory,
                    short_term_retention_days=settings.short_term_memory_retention_days,
                    temporary_retention_days=settings.temporary_context_retention_days,
                )
                lifecycle_logger.info("memory maintenance: %s", result)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 维护失败不能拖垮主服务
                lifecycle_logger.exception("memory maintenance loop failed")
            await asyncio.sleep(interval)

    return asyncio.create_task(_loop(), name="planharness-memory-maintenance")


def create_app(settings: Settings | None = None) -> FastAPI:
    """按给定配置组装并返回 FastAPI 实例。"""
    settings = settings or get_settings()
    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    # 无迁移工具时的兜底建表；已存在的表不会被重复创建
    Base.metadata.create_all(engine)
    # 已有数据库补齐本次新增列（纯加法），并回填旧 TemporaryContext override
    try:
        ensure_additive_schema(engine)
    except Exception:  # noqa: BLE001 - 迁移失败要显式记录，后续接口会暴露
        logging.getLogger("planharness.migrations").exception(
            "additive schema migration failed"
        )
    # 全局大模型开关：底层客户端和接口依赖都读它
    configure_llm_guard(session_factory)

    # 种子逻辑内部幂等（demo 用户已存在就直接返回），所以每次启动都调用
    with session_factory() as session:
        seed_demo(session)
        # 知识库是全局数据（不按用户），启动时幂等灌入
        seed_knowledge(session)
        try:
            counts = backfill_lifecycle_data(
                session,
                short_term_retention_days=settings.short_term_memory_retention_days,
            )
            session.commit()
            logging.getLogger("planharness.migrations").info(
                "lifecycle backfill: %s", counts
            )
        except Exception:  # noqa: BLE001
            session.rollback()
            logging.getLogger("planharness.migrations").exception(
                "lifecycle backfill failed"
            )

    # LangGraph 的挂起状态靠 checkpointer 活着：dev 用内存，prod 用 Postgres。
    # 生产选 postgres 时连不上会直接启动失败 —— 不静默降级（否则审批会悄悄丢）。
    checkpointer, close_checkpointer = make_checkpointer(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        """应用生命周期：启动记忆维护任务，退出时取消并关闭 checkpointer。"""
        maintenance = None
        if settings.memory_cleanup_enabled and settings.memory_cleanup_interval_seconds >= 0:
            # 生产环境启动时先补跑一次（进程可能每天重启，不能只依赖 24h 后的第一次）
            if settings.env not in ("dev", "test"):
                try:
                    startup_result = await asyncio.to_thread(
                        run_lifecycle_with_factory,
                        session_factory,
                        short_term_retention_days=settings.short_term_memory_retention_days,
                        temporary_retention_days=settings.temporary_context_retention_days,
                    )
                    lifecycle_logger.info("startup memory maintenance: %s", startup_result)
                except Exception:  # noqa: BLE001
                    lifecycle_logger.exception("startup memory maintenance failed")
            maintenance = _memory_maintenance_loop(session_factory, settings)
            app.state.memory_maintenance_task = maintenance
        try:
            yield
        finally:
            if maintenance is not None:
                maintenance.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await maintenance
            close_checkpointer()

    # docs_url/redoc_url 置空：默认的 /docs 依赖 cdn.jsdelivr.net，国内常被重置导致白屏，
    # 改由 mount_api_docs() 用本地静态文件提供（见 app/openapi_docs.py）
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    # 运行期共享的单例挂在 app.state，供依赖注入（get_db/get_current_user）取用
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory

    app.state.checkpointer = checkpointer
    app.state.patch_graph = build_patch_graph(
        session_factory,
        explainer=explainer_from_settings(settings).explain,
        checkpointer=checkpointer,
        token_budget=settings.context_token_budget,
        llm_budget=LlmBudget(
            max_tokens_per_run=settings.max_tokens_per_run,
            max_cost_cny_per_run=settings.max_cost_cny_per_run,
            price_in_cny_per_mtok=settings.llm_price_in_cny_per_mtok,
            price_out_cny_per_mtok=settings.llm_price_out_cny_per_mtok,
        ),
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def add_trace_id(request: Request, call_next):
        """为每个请求分配 trace_id、写日志并回写响应头。

        日志只记 method/path/status/耗时，不记 header 和 body，避免把 token 打进 journal。
        线上排障时用 trace_id 把前端报错、Nginx 和 journal 串起来。
        """
        trace_id = request.headers.get("X-Trace-Id") or uuid4().hex
        request.state.trace_id = trace_id
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = (time.perf_counter() - started) * 1000
            http_logger.exception(
                "trace_id=%s %s %s failed after %.0fms",
                trace_id, request.method, request.url.path, elapsed_ms,
            )
            raise
        elapsed_ms = (time.perf_counter() - started) * 1000
        if request.url.path.startswith("/api/"):
            http_logger.info(
                "trace_id=%s %s %s -> %s %.0fms",
                trace_id, request.method, request.url.path,
                response.status_code, elapsed_ms,
            )
        response.headers["X-Trace-Id"] = trace_id
        return response

    app.include_router(auth_router)
    app.include_router(food_router)
    app.include_router(mcd_router)
    app.include_router(agent_router)
    app.include_router(coach_router)
    app.include_router(system_router)
    app.include_router(knowledge_router)
    app.include_router(router)

    # 离线 API 文档：/docs + /static/swagger/*，不依赖外网 CDN
    mount_api_docs(app, title=settings.app_name)

    @app.get("/")
    def root():
        """根路径只做存活提示，真实接口统一挂在 /api/v1 下。"""
        return {"name": settings.app_name, "status": "ok", "docs": "/docs"}

    return app


# uvicorn app.main:app 使用的模块级实例
app = create_app()
