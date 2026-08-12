import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from credits import tts_cost, video_cost


def test_video_base_cost_is_one_charge_per_video():
    plan = {"video_credit_cost": 10, "video_credit_cost_per_minute": 0}
    assert video_cost(plan, 1) == 10
    assert video_cost(plan, 3600) == 10


def test_video_per_minute_cost_rounds_up():
    plan = {"video_credit_cost": 10, "video_credit_cost_per_minute": 3}
    assert video_cost(plan, 1) == 13
    assert video_cost(plan, 61) == 16


def test_tts_cost_rounds_characters_to_hundreds():
    plan = {"tts_credit_per_100_chars": 2}
    assert tts_cost(plan, 0) == 0
    assert tts_cost(plan, 100) == 2
    assert tts_cost(plan, 101) == 4


import asyncio
import sqlite3

from credits import CreditError, add_credits, balance, ensure_account


class _Meta:
    def __init__(self, changes):
        self.changes = changes


class _Result:
    def __init__(self, rows=None, changes=0):
        self.results = rows or []
        self.meta = _Meta(changes)


class _Statement:
    def __init__(self, conn, sql):
        self.conn = conn
        self.sql = sql
        self.params = ()

    def bind(self, *params):
        self.params = params
        return self

    async def run(self):
        cur = self.conn.execute(self.sql, self.params)
        self.conn.commit()
        return _Result(changes=cur.rowcount if cur.rowcount >= 0 else 0)

    async def all(self):
        cur = self.conn.execute(self.sql, self.params)
        cols = [item[0] for item in cur.description or []]
        return _Result([dict(zip(cols, row)) for row in cur.fetchall()])


class _Db:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript("""
        CREATE TABLE credit_accounts(user_id TEXT PRIMARY KEY,balance INTEGER NOT NULL DEFAULT 0,lifetime_earned INTEGER NOT NULL DEFAULT 0,lifetime_spent INTEGER NOT NULL DEFAULT 0,version INTEGER NOT NULL DEFAULT 0,updated_at TEXT);
        CREATE TABLE credit_ledger(id TEXT PRIMARY KEY,user_id TEXT,delta INTEGER,balance_after INTEGER,kind TEXT,reference_id TEXT,description TEXT,actor_user_id TEXT,idempotency_key TEXT,created_at TEXT,UNIQUE(user_id,idempotency_key));
        """)

    def prepare(self, sql):
        return _Statement(self.conn, sql)


def test_credit_wallet_is_idempotent_and_locked():
    async def run_test():
        db = _Db()
        await ensure_account(db, "u1")
        await add_credits(db, "u1", 100, "grant", idempotency_key="grant-1")
        await add_credits(db, "u1", 100, "grant", idempotency_key="grant-1")
        await add_credits(db, "u1", -30, "spend", idempotency_key="spend-1")
        wallet = await balance(db, "u1")
        assert wallet["balance"] == 70
        assert wallet["version"] == 2
        try:
            await add_credits(db, "u1", -100, "spend", idempotency_key="bad")
        except CreditError:
            pass
        else:
            raise AssertionError("expected insufficient balance")
    asyncio.run(run_test())
