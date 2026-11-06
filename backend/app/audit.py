"""审计哈希链：让「谁在什么时候对计划做了什么」无法被悄悄篡改。

结构（每个 stream 一条链）：

    event_i.hash = sha256(stream_id | seq | action | actor | payload_hash | event_{i-1}.hash)

于是：

- 改 payload 不改 hash → `payload_hash` 对不上，`broken_at = i`；
- 改 payload 且重算 hash → 下一条的 `prev_hash` 对不上，`broken_at = i+1`；
- 删中间一条 → `seq` 出现断档，且下一条的 `prev_hash` 对不上。

这不是"防黑客"级别的方案（能改库的人理论上也能重算整条链），
但它能提供**可验证的证据**：`GET /audit/{stream_id}/verify` 会明确告诉你链在哪里断了。
真正的强度来自「把最新 hash 定期锚定到外部」——那是 P1 的事。
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditEvent

# 链头：没有任何前置事件时用 64 个 0
GENESIS_HASH = "0" * 64

ACTIONS = (
    "patch_created",
    "approval_granted",
    "approval_rejected",
    "patch_committed",
    "verification_failed",
    "rollback_committed",
    "plan_retargeted",
    # 教练计划的生成链路：草案 → 修订 → 确认/拒绝。审计要能看出
    # "这份计划是谁确认的、改过几轮、有没有走模型降级"。
    "plan_draft_created",
    "plan_draft_revised",
    "plan_draft_rejected",
    "plan_confirmed",
    "profile_updated",
    # 麦当劳点餐：建单/取消都要能在审计链里查到（谁在什么时候点了多少钱）
    "mcd_order_created",
    "mcd_order_cancelled",
)


def canonical_payload(payload: dict | None) -> str:
    """稳定序列化：键排序 + 紧凑分隔符，保证同内容得到同一串。

    不排序的话，`{"a":1,"b":2}` 和 `{"b":2,"a":1}` 会算出不同 hash，
    链就没法验证了 —— 这是哈希链最常见的坑。
    """
    return json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def payload_hash(payload: dict | None) -> str:
    return sha256_hex(canonical_payload(payload))


def event_hash(
    *,
    stream_id: str,
    seq: int,
    action: str,
    actor_type: str,
    actor_id: str,
    payload_digest: str,
    prev_hash: str,
) -> str:
    material = "|".join(
        [
            stream_id,
            str(seq),
            action,
            actor_type,
            actor_id,
            payload_digest,
            prev_hash,
        ]
    )
    return sha256_hex(material)


def last_event(db: Session, stream_id: str) -> AuditEvent | None:
    return db.scalar(
        select(AuditEvent)
        .where(AuditEvent.stream_id == stream_id)
        .order_by(AuditEvent.seq.desc())
        .limit(1)
    )


def append_event(
    db: Session,
    stream_id: str,
    action: str,
    payload: dict | None = None,
    *,
    actor_type: str = "agent",
    actor_id: str = "",
) -> AuditEvent:
    """往链尾追加一条事件（不 commit，跟调用方在同一事务里）。"""
    if action not in ACTIONS:
        raise ValueError(f"unknown audit action: {action} (allowed: {ACTIONS})")

    previous = last_event(db, stream_id)
    seq = (previous.seq + 1) if previous is not None else 1
    prev_hash = previous.hash if previous is not None else GENESIS_HASH
    digest = payload_hash(payload)

    event = AuditEvent(
        stream_id=stream_id,
        seq=seq,
        actor_type=actor_type,
        actor_id=actor_id or "",
        action=action,
        payload=payload or {},
        payload_hash=digest,
        prev_hash=prev_hash,
        hash=event_hash(
            stream_id=stream_id,
            seq=seq,
            action=action,
            actor_type=actor_type,
            actor_id=actor_id or "",
            payload_digest=digest,
            prev_hash=prev_hash,
        ),
    )
    db.add(event)
    db.flush()
    return event


@dataclass(frozen=True)
class VerifyResult:
    stream_id: str
    valid: bool
    count: int
    broken_at: int | None = None          # 出问题的 seq
    reason: str = ""
    checks: list[str] = field(default_factory=list)
    # 链头（最后一条的 seq/hash）：配合外部签名，才能发现"整链重写"或"尾部截断"
    head_seq: int | None = None
    head_hash: str = ""

    def to_dict(self) -> dict:
        return {
            "stream_id": self.stream_id,
            "valid": self.valid,
            "count": self.count,
            "broken_at": self.broken_at,
            "reason": self.reason,
            "checks": self.checks,
            "head_seq": self.head_seq,
            "head_hash": self.head_hash,
        }


def sign_head(secret: str, stream_id: str, seq: int | None, head_hash: str) -> str:
    """给链头做 HMAC 签名：把"这条链当时长这样"变成可带出去的凭据。

    为什么需要它：哈希链本身只能防"改中间几条"。
    有人如果能改库，就可以**重算整条链**，或者**删掉尾部几条**，
    这两种情况在纯链内校验里看起来都是"合法的"。
    签名用的是服务端 SECRET_KEY（不在审计表里），把 (stream_id, seq, head_hash)
    抄到数据库外（工单/邮件/另一台机器）之后，再被重写就会对不上签名。
    """
    material = f"{stream_id}|{seq}|{head_hash}"
    return hmac.new(secret.encode("utf-8"), material.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def verify_stream(db: Session, stream_id: str) -> VerifyResult:
    """从头重算整条链，返回第一处断裂的位置。"""
    events = list(
        db.scalars(
            select(AuditEvent)
            .where(AuditEvent.stream_id == stream_id)
            .order_by(AuditEvent.seq.asc())
        ).all()
    )
    if not events:
        return VerifyResult(stream_id, True, 0, reason="empty stream")

    expected_prev = GENESIS_HASH
    rewritten = 0
    for index, event in enumerate(events, start=1):
        # 1) seq 必须连续：删掉中间一条会在这里暴露
        if event.seq != index:
            return VerifyResult(
                stream_id,
                False,
                len(events),
                broken_at=event.seq,
                reason=f"seq gap: expected {index}, got {event.seq}",
                checks=[f"event{i} seq ok" for i in range(1, index)],
            )

        # 2) payload 必须没被改过
        if payload_hash(event.payload) != event.payload_hash:
            return VerifyResult(
                stream_id,
                False,
                len(events),
                broken_at=event.seq,
                reason="payload_hash mismatch（payload 被改过）",
                checks=[f"event{i} ok" for i in range(1, index)],
            )

        # 3) 链接必须对得上
        if event.prev_hash != expected_prev:
            return VerifyResult(
                stream_id,
                False,
                len(events),
                broken_at=event.seq,
                reason="prev_hash mismatch（上一条被替换或删除）",
                checks=[f"event{i} ok" for i in range(1, index)],
            )

        # 4) 自身 hash 必须自洽
        recomputed = event_hash(
            stream_id=event.stream_id,
            seq=event.seq,
            action=event.action,
            actor_type=event.actor_type,
            actor_id=event.actor_id,
            payload_digest=event.payload_hash,
            prev_hash=event.prev_hash,
        )
        if recomputed != event.hash:
            return VerifyResult(
                stream_id,
                False,
                len(events),
                broken_at=event.seq,
                reason="hash mismatch（事件内容被改过并试图保留原 hash）",
                checks=[f"event{i} ok" for i in range(1, index)],
            )

        expected_prev = event.hash
        rewritten = index
        head_seq, head_hash = event.seq, event.hash

    return VerifyResult(
        stream_id,
        True,
        len(events),
        reason="ok",
        checks=[f"{rewritten}/{len(events)} 条事件逐条校验通过"],
        head_seq=head_seq,
        head_hash=head_hash,
    )
