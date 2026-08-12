from __future__ import annotations

import json
from typing import Any


async def all_rows(db: Any, sql: str, *params: Any) -> list[dict[str, Any]]:
    result = await db.prepare(sql).bind(*params).all()
    rows = getattr(result, "results", result)
    if hasattr(rows, "to_py"):
        rows = rows.to_py()
    return list(rows or [])


async def first_row(db: Any, sql: str, *params: Any) -> dict[str, Any] | None:
    rows = await all_rows(db, sql, *params)
    return rows[0] if rows else None


async def run(db: Any, sql: str, *params: Any) -> Any:
    return await db.prepare(sql).bind(*params).run()


async def json_row(db: Any, sql: str, *params: Any) -> dict[str, Any] | None:
    row = await first_row(db, sql, *params)
    return row


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(value: Any, default: Any = None) -> Any:
    if value is None:
        return default
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return default
