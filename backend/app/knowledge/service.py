"""知识问答：检索 → 组织答案 → 强制带引用。

三条硬规则：
1. 检索不到就明说"暂无足够依据"，不编；
2. 答案必须带 citations（id / title / source）；
3. 不诊断、不给用药建议；红旗症状直接转安全分流。
"""

from __future__ import annotations

from app.knowledge.retriever import search
from app.knowledge.seeds import SEEDS

RED_FLAGS = ("胸痛", "胸闷", "晕厥", "晕倒", "心悸", "麻木", "无力", "剧烈头痛", "呼吸困难")
MEDICAL = ("吃什么药", "用药", "布洛芬", "止痛药", "消炎药", "剂量", "诊断")
FOOTER = "以上内容仅供运动参考，不构成医疗建议；持续或加重的不适请就医。"


def _knowledge_items() -> list[dict]:
    """内置 27 条 + 可选的"清单型"扩展（data/knowledge/expanded.json）。

    扩展由 scripts/expand_curated_from_rag.py 从 600 条外部语料生成，
    删除该文件即可回滚到只有内置条目。
    """
    import json
    from pathlib import Path

    items = list(SEEDS)
    path = Path(__file__).resolve().parents[2] / "data" / "knowledge" / "expanded.json"
    if path.exists():
        try:
            expanded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(expanded, list):
                items.extend(item for item in expanded if isinstance(item, dict))
        except Exception:  # noqa: BLE001 - 扩展文件坏了不影响内置知识
            pass
    return items


def seed_knowledge(db) -> int:
    """把内置知识写进库（幂等：已存在就更新内容，不重复插入）。"""
    from app.models import KnowledgeChunk

    count = 0
    for item in _knowledge_items():
        row = db.get(KnowledgeChunk, item["id"])
        if row is None:
            row = KnowledgeChunk(id=item["id"])
            db.add(row)
        row.title = item["title"]
        row.body_part = item["body_part"]
        row.topic = item["topic"]
        row.content = item["content"]
        row.tags = item.get("tags") or []
        row.source_title = item["source_title"]
        row.source_url = item["source_url"]
        count += 1
    db.commit()
    return count


def _citations(hits) -> list[dict]:
    return [
        {
            "id": hit.chunk.id,
            "title": hit.chunk.title,
            "topic": hit.chunk.topic,
            "source_title": hit.chunk.source_title,
            "source_url": hit.chunk.source_url,
        }
        for hit in hits
    ]


def ask(db, question: str, *, body_part: str | None = None) -> dict:
    text = (question or "").strip()
    if not text:
        return {"answer": "请先说说你想了解什么。", "citations": [], "refused": True}

    # 红旗症状：先给安全分流，不进入具体训练建议
    if any(word in text for word in RED_FLAGS):
        hits = search(db, "红旗症状 就医", body_part=None, limit=1, min_score=1.0)
        return {
            "answer": "出现胸痛、晕厥、明显气短或进行性麻木无力这类症状，应先停止运动并做医疗评估，"
                      "不建议继续按计划训练。具体原因需要专业人员判断。" + FOOTER,
            "citations": _citations(hits),
            "refused": False,
            "safety": "red_flag",
        }

    hits = search(db, text, body_part=body_part, limit=3)
    if not hits:
        return {
            "answer": "暂无足够依据回答这个问题。可以换个说法，或者先看动作要领/安全相关的条目。" + FOOTER,
            "citations": [],
            "refused": True,
        }

    # 用药/诊断类问题：给依据，但明确不提供这些内容
    medic = any(word in text for word in MEDICAL)
    top = hits[0].chunk
    answer = f"{top.title}：{top.content}"
    if len(hits) > 1 and hits[1].score >= hits[0].score * 0.8:
        answer += f"\n\n{hits[1].chunk.title}：{hits[1].chunk.content}"
    if medic:
        answer += "\n\n这里不提供诊断或用药建议，涉及药物请咨询医生或药师。"
    answer += FOOTER

    return {
        "answer": answer,
        "citations": _citations(hits),
        "refused": False,
        "safety": "normal",
    }
