"""健康知识接口：搜索 + 问答（答案必带引用）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.routes import get_current_user, get_db, ok
from app.knowledge.retriever import search
from app.knowledge.service import ask
from app.models import User

router = APIRouter(prefix="/api/v1")


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=300)
    body_part: str | None = None


@router.get("/knowledge/search")
def knowledge_search(
    request: Request,
    q: str,
    body_part: str | None = None,
    limit: int = 3,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    hits = search(db, q, body_part=body_part, limit=max(1, min(limit, 10)))
    return ok(request, [
        {
            "id": hit.chunk.id,
            "title": hit.chunk.title,
            "topic": hit.chunk.topic,
            "body_part": hit.chunk.body_part,
            "content": hit.chunk.content,
            "score": round(hit.score, 2),
            "source_title": hit.chunk.source_title,
            "source_url": hit.chunk.source_url,
        }
        for hit in hits
    ])


@router.post("/knowledge/ask")
def knowledge_ask(
    payload: AskIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return ok(request, ask(db, payload.question, body_part=payload.body_part))

@router.get("/knowledge/rag")
def knowledge_rag(
    request: Request,
    q: str,
    engine: str = "hybrid",
    limit: int = 5,
    user: User = Depends(get_current_user),
):
    """外部语料 RAG 演示：keyword / vector / hybrid 三种检索对比。"""
    from app.knowledge import rag

    hits = rag.search(q, engine=engine, limit=max(1, min(limit, 20)))
    return ok(request, {"query": q, "engine": engine, "hits": hits, "stats": rag.stats()})
