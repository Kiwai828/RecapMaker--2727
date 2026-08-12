from __future__ import annotations

import time
import uuid
from typing import Any

from db import first_row, run


class CreditError(Exception):
    pass


async def ensure_account(db: Any, user_id: str) -> None:
    await run(db, "INSERT OR IGNORE INTO credit_accounts(user_id,balance) VALUES(?,0)", user_id)


async def balance(db: Any, user_id: str) -> dict[str, Any]:
    await ensure_account(db, user_id)
    row = await first_row(db, "SELECT user_id,balance,lifetime_earned,lifetime_spent,version,updated_at FROM credit_accounts WHERE user_id=?", user_id)
    return row or {"user_id": user_id, "balance": 0, "lifetime_earned": 0, "lifetime_spent": 0, "version": 0}


async def add_credits(
    db: Any,
    user_id: str,
    delta: int,
    kind: str,
    *,
    reference_id: str | None = None,
    description: str | None = None,
    actor_user_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    if delta == 0:
        return await balance(db, user_id)
    await ensure_account(db, user_id)
    if idempotency_key:
        previous = await first_row(db, "SELECT balance_after FROM credit_ledger WHERE user_id=? AND idempotency_key=?", user_id, idempotency_key)
        if previous:
            return await balance(db, user_id)
    for _ in range(4):
        account = await balance(db, user_id)
        current = int(account["balance"])
        new_balance = current + delta
        if new_balance < 0:
            raise CreditError("Insufficient credits")
        version = int(account.get("version", 0))
        update = await run(
            db,
            "UPDATE credit_accounts SET balance=?, lifetime_earned=lifetime_earned+?, lifetime_spent=lifetime_spent+?, version=version+1, updated_at=datetime('now') WHERE user_id=? AND version=?",
            new_balance,
            max(delta, 0),
            max(-delta, 0),
            user_id,
            version,
        )
        meta = getattr(update, "meta", None)
        changes = int(getattr(meta, "changes", 1)) if meta is not None else 1
        if changes == 0:
            continue
        await run(
            db,
            "INSERT INTO credit_ledger(id,user_id,delta,balance_after,kind,reference_id,description,actor_user_id,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?)",
            str(uuid.uuid4()),
            user_id,
            delta,
            new_balance,
            kind,
            reference_id,
            description,
            actor_user_id,
            idempotency_key,
        )
        return await balance(db, user_id)
    raise CreditError("Credit wallet is busy; retry the operation")


async def active_plan(db: Any, user_id: str) -> dict[str, Any]:
    row = await first_row(
        db,
        "SELECT p.*,up.expires_at AS subscription_expires_at FROM user_plans up JOIN plans p ON p.id=up.plan_id WHERE up.user_id=? AND up.status='active' AND p.active=1 AND (up.expires_at IS NULL OR up.expires_at>datetime('now')) ORDER BY up.created_at DESC LIMIT 1",
        user_id,
    )
    if row:
        return row
    row = await first_row(db, "SELECT * FROM plans WHERE name='Free' AND active=1 LIMIT 1")
    if not row:
        row = await first_row(db, "SELECT * FROM plans WHERE active=1 ORDER BY sort_order,id LIMIT 1")
    return row or {"id": "free", "name": "Free", "video_credit_cost": 10, "video_credit_cost_per_minute": 0, "tts_credit_per_100_chars": 1, "voice_clone_credit_cost": 0, "max_video_duration_seconds": 300}


def video_cost(plan: dict[str, Any], duration_seconds: float | int | None) -> int:
    seconds = max(float(duration_seconds or 0), 1.0)
    base = int(plan.get("video_credit_cost", 0))
    per_minute = int(plan.get("video_credit_cost_per_minute", 0))
    minutes = int((seconds + 59) // 60)
    return max(0, base + minutes * per_minute)


def tts_cost(plan: dict[str, Any], characters: int) -> int:
    unit = max(int(plan.get("tts_credit_per_100_chars", 1)), 0)
    return max(0, ((max(characters, 0) + 99) // 100) * unit)
