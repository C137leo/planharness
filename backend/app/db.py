"""SQLAlchemy 基础设施：声明式基类、Engine、Session 工厂。

P0 阶段不引入 Alembic，启动时用 create_all 建表，减少初始化步骤。
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    """所有 ORM 模型的声明式基类。"""

    pass


def make_engine(database_url: str):
    """创建 Engine。

    - SQLite 默认禁止跨线程复用连接，而 FastAPI 会把同步接口丢进线程池，所以关闭该检查；
    - pool_pre_ping：取连接前先探活，长驻进程遇到网络抖动/服务端断连时可自动重连。
    """
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    return create_engine(database_url, pool_pre_ping=True, connect_args=connect_args)


def make_session_factory(engine):
    """创建 Session 工厂。

    autoflush=False：写库时机交给显式 flush/commit，避免查询时隐式写出半成品数据；
    expire_on_commit=False：commit 后对象仍可直接读属性，接口层可直接序列化返回值。
    """
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
