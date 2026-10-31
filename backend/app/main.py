from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

import app.models  # noqa: F401 - register SQLAlchemy models
from app.api.routes import router
from app.config import Settings, get_settings
from app.db import Base, make_engine, make_session_factory
from app.seed import seed_demo


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    Base.metadata.create_all(engine)

    with session_factory() as session:
        seed_demo(session)

    app = FastAPI(title=settings.app_name, version="0.1.0")
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
        trace_id = request.headers.get("X-Trace-Id") or uuid4().hex
        request.state.trace_id = trace_id
        response = await call_next(request)
        response.headers["X-Trace-Id"] = trace_id
        return response

    app.include_router(router)

    @app.get("/")
    def root():
        return {"name": settings.app_name, "status": "ok", "docs": "/docs"}

    return app


app = create_app()
