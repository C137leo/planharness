"""FastAPI 应用入口（应用工厂模式）。

create_app 负责：建 Engine/Session 工厂 → 建表 → 灌 demo 种子数据 → 装中间件与路由。
工厂模式让测试可以传入独立 Settings（临时 SQLite 库），避免污染开发数据。
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

import app.models  # noqa: F401 - register SQLAlchemy models
from app.api.routes import router
from app.config import Settings, get_settings
from app.db import Base, make_engine, make_session_factory
from app.openapi_docs import mount_api_docs
from app.seed import seed_demo


def create_app(settings: Settings | None = None) -> FastAPI:
    """按给定配置组装并返回 FastAPI 实例。"""
    settings = settings or get_settings()
    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    # 无迁移工具时的兜底建表；已存在的表不会被重复创建
    Base.metadata.create_all(engine)

    # 种子逻辑内部幂等（demo 用户已存在就直接返回），所以每次启动都调用
    with session_factory() as session:
        seed_demo(session)

    # docs_url/redoc_url 置空：默认的 /docs 依赖 cdn.jsdelivr.net，国内常被重置导致白屏，
    # 改由 mount_api_docs() 用本地静态文件提供（见 app/openapi_docs.py）
    app = FastAPI(title=settings.app_name, version="0.1.0", docs_url=None, redoc_url=None)
    # 运行期共享的单例挂在 app.state，供依赖注入（get_db/get_current_user）取用
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def add_trace_id(request: Request, call_next):
        """为每个请求分配 trace_id，并原样回写响应头，便于串联日志与前端排查。"""
        trace_id = request.headers.get("X-Trace-Id") or uuid4().hex
        request.state.trace_id = trace_id
        response = await call_next(request)
        response.headers["X-Trace-Id"] = trace_id
        return response

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
