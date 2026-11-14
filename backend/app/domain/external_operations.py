"""Separate transactions around external writes. Never call inside a DB savepoint.

Callers supply an isolated Session factory, an authenticated user, a validated
request and provider-specific verification. No secrets or raw provider text are
stored. UNKNOWN and EXECUTING claims block duplicate dispatch permanently until
reconciliation; a new HTTP request must reuse the same business request key.
"""
from dataclasses import dataclass
import hashlib
import json
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from app.agent.execution_contract import TaskState, VerifiedOutcome
from app.models import ExternalOperation, new_id


@dataclass(frozen=True)
class DispatchReceipt:
    remote_id: str
    data: dict


class OperationConflict(ValueError):
    pass


class DefiniteRejection(Exception):
    """Only an adapter with proof of no remote write may raise this."""


def _find(db, user_id, provider, key):
    return db.scalar(select(ExternalOperation).where(ExternalOperation.user_id == user_id,
                     ExternalOperation.provider == provider, ExternalOperation.request_key == key))


def _outcome(row):
    return VerifiedOutcome(state=TaskState(row.status), operation_id=row.id, remote_id=row.remote_id,
        verified=row.status == TaskState.SUCCEEDED, data=dict(row.result or {}),
        recovery="query_remote_status_or_manual_reconciliation; do_not_resubmit" if row.status in {TaskState.UNKNOWN, TaskState.EXECUTING, TaskState.VERIFYING} else "")


def execute(factory, *, user_id, provider, capability_id, request_key, payload, dispatch, verify):
    if not request_key or len(request_key) > 128:
        raise ValueError("business_request_key_required")
    fingerprint = hashlib.sha256(json.dumps({"capability_id": capability_id, "payload": payload},
                               sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    # The claim commits BEFORE a side effect. Concurrent inserts use the unique
    # constraint; no network request occurs while a database lock is held.
    with factory() as db:
        prior = _find(db, user_id, provider, request_key)
        if prior:
            if prior.payload_hash != fingerprint:
                raise OperationConflict("request_key_payload_conflict")
            return _outcome(prior)
        row = ExternalOperation(id=new_id(), user_id=user_id, provider=provider, capability_id=capability_id,
                                request_key=request_key, payload_hash=fingerprint, status=TaskState.EXECUTING)
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            prior = _find(db, user_id, provider, request_key)
            if prior is None:
                raise
            if prior.payload_hash != fingerprint:
                raise OperationConflict("request_key_payload_conflict")
            return _outcome(prior)
        operation_id = row.id
    remote_id, data, status = None, {}, TaskState.UNKNOWN
    receipt = {}
    try:
        # Dispatch returns only a remote identifier. A response `success=true`
        # alone is insufficient; verification must read authoritative state.
        candidate = dispatch()
        if isinstance(candidate, DispatchReceipt):
            receipt = dict(candidate.data)
            candidate = candidate.remote_id
        if not isinstance(candidate, str) or not candidate.strip() or len(candidate) > 128:
            raise ValueError("invalid_remote_identifier")
        remote_id = candidate
        if remote_id:
            # Persist the remote identifier before a potentially failing verification.
            with factory() as db:
                saved = db.get(ExternalOperation, operation_id)
                saved.remote_id, saved.status, saved.result = remote_id, TaskState.VERIFYING, receipt
                db.commit()
            verified, data = verify(remote_id)
            if type(verified) is not bool or not isinstance(data, dict):
                raise ValueError("invalid_verification_evidence")
            data = {**receipt, **data}
            status = TaskState.SUCCEEDED if verified else TaskState.UNKNOWN
    except DefiniteRejection:
        status, data = TaskState.FAILED, {"reason": "provider_definite_rejection"}
    except Exception as exc:
        # Timeout, transport errors, parsing failures and verification failures
        # can all occur AFTER the provider has created an order.
        status, data = TaskState.UNKNOWN, {**receipt, "error_type": type(exc).__name__}
    with factory() as db:
        row = db.get(ExternalOperation, operation_id)
        row.status, row.remote_id, row.result = status, remote_id, data
        db.commit()
        return _outcome(row)


def inspect_operation(factory, *, user_id, operation_id):
    with factory() as db:
        row = db.get(ExternalOperation, operation_id)
        if row is None or row.user_id != user_id:
            raise LookupError("operation_not_found")
        return _outcome(row)


def reconcile(factory, *, user_id, operation_id, verify):
    # Query only: this function never dispatches a write, even if no match is found.
    previous = inspect_operation(factory, user_id=user_id, operation_id=operation_id)
    if previous.state in {TaskState.SUCCEEDED, TaskState.FAILED}:
        return previous
    if not previous.remote_id:
        return previous
    try:
        verified, data = verify(previous.remote_id)
    except Exception:
        return previous
    if not verified:
        return previous
    with factory() as db:
        row = db.get(ExternalOperation, operation_id)
        row.status, row.result = TaskState.SUCCEEDED, data
        db.commit()
        return _outcome(row)
