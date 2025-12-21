import os
import uuid
import datetime as dt
import sqlite3
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

DB_PATH = os.getenv("APP_DB_PATH", "app.db")

app = FastAPI(title="Tarot MiniApp Backend", version="0.1.0")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        telegram_user_id INTEGER UNIQUE NOT NULL,
        username TEXT,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS spreads (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        description TEXT,
        price_stars INTEGER NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 1
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        spread_id TEXT NOT NULL,
        status TEXT NOT NULL,
        payload_json TEXT,
        result_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id),
        FOREIGN KEY(spread_id) REFERENCES spreads(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS payments (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        provider TEXT NOT NULL,
        status TEXT NOT NULL,
        telegram_charge_id TEXT,
        amount_stars INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(session_id) REFERENCES sessions(id)
    )
    """)

    conn.commit()
    conn.close()


@app.on_event("startup")
def _startup():
    init_db()
    seed_spreads_if_empty()


def now_iso() -> str:
    return dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc).isoformat()


def seed_spreads_if_empty():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(1) AS c FROM spreads")
    c = int(cur.fetchone()["c"])
    if c == 0:
        spreads = [
            ("spread_3cards", "🔮 Расклад 3 карты", "Прошлое / Настоящее / Будущее", 50, 1),
            ("spread_love", "❤️ Любовный расклад", "Ситуация / Его мысли / Твой шаг", 75, 1),
        ]
        for sid, title, desc, price, active in spreads:
            cur.execute(
                "INSERT INTO spreads (id, title, description, price_stars, is_active) VALUES (?, ?, ?, ?, ?)",
                (sid, title, desc, price, active),
            )
        conn.commit()
    conn.close()


# -------------------------
# Pydantic models
# -------------------------
class SpreadOut(BaseModel):
    id: str
    title: str
    description: Optional[str] = None
    price_stars: int


class SessionCreateIn(BaseModel):
    telegram_user_id: int
    username: Optional[str] = None
    spread_id: str
    payload: Dict[str, Any] = Field(default_factory=dict)  # например сфера/выбор карт


class SessionOut(BaseModel):
    id: str
    spread_id: str
    status: str
    payload: Dict[str, Any]
    result: Optional[Dict[str, Any]] = None
    created_at: str
    updated_at: str


class StartPaymentOut(BaseModel):
    session_id: str
    amount_stars: int
    status: str


class PaymentEventIn(BaseModel):
    session_id: str
    provider: str = "telegram_stars"
    status: str  # "paid" | "failed"
    telegram_charge_id: Optional[str] = None
    amount_stars: int


# -------------------------
# Helpers
# -------------------------
def get_or_create_user(telegram_user_id: int, username: Optional[str]) -> str:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE telegram_user_id = ?", (telegram_user_id,))
    row = cur.fetchone()
    if row:
        user_id = row["id"]
        if username:
            cur.execute("UPDATE users SET username = ? WHERE id = ?", (username, user_id))
            conn.commit()
        conn.close()
        return user_id

    user_id = str(uuid.uuid4())
    cur.execute(
        "INSERT INTO users (id, telegram_user_id, username, created_at) VALUES (?, ?, ?, ?)",
        (user_id, telegram_user_id, username, now_iso()),
    )
    conn.commit()
    conn.close()
    return user_id


def spread_exists(spread_id: str) -> Optional[sqlite3.Row]:
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, title, description, price_stars, is_active FROM spreads WHERE id = ?",
        (spread_id,),
    )
    row = cur.fetchone()
    conn.close()
    return row


# -------------------------
# Routes
# -------------------------
@app.get("/api/v1/spreads", response_model=List[SpreadOut])
def list_spreads():
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, title, description, price_stars FROM spreads WHERE is_active = 1 ORDER BY price_stars ASC"
    )
    rows = cur.fetchall()
    conn.close()
    return [SpreadOut(**dict(r)) for r in rows]


@app.post("/api/v1/sessions", response_model=SessionOut)
def create_session(payload: SessionCreateIn):
    srow = spread_exists(payload.spread_id)
    if not srow or int(srow["is_active"]) != 1:
        raise HTTPException(status_code=404, detail="Spread not found or inactive")

    user_id = get_or_create_user(payload.telegram_user_id, payload.username)
    session_id = str(uuid.uuid4())
    created = now_iso()

    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO sessions (id, user_id, spread_id, status, payload_json, result_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (session_id, user_id, payload.spread_id, "created", json_dumps(payload.payload), None, created, created),
    )
    conn.commit()
    conn.close()

    return SessionOut(
        id=session_id,
        spread_id=payload.spread_id,
        status="created",
        payload=payload.payload,
        result=None,
        created_at=created,
        updated_at=created,
    )


@app.post("/api/v1/sessions/{session_id}/start_payment", response_model=StartPaymentOut)
def start_payment(session_id: str):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT s.id, s.status, sp.price_stars
        FROM sessions s
        JOIN spreads sp ON sp.id = s.spread_id
        WHERE s.id = ?
        """,
        (session_id,),
    )
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Session not found")

    if row["status"] in ("paid", "delivered"):
        conn.close()
        return StartPaymentOut(session_id=session_id, amount_stars=int(row["price_stars"]), status=row["status"])

    amount = int(row["price_stars"])
    cur.execute(
        "UPDATE sessions SET status = ?, updated_at = ? WHERE id = ?",
        ("pending_payment", now_iso(), session_id),
    )
    conn.commit()
    conn.close()

    # На этом шаге Mini App/бот понимает сумму Stars.
    # Реальный invoice создаст бот; он же позже пришлёт PaymentEventIn.
    return StartPaymentOut(session_id=session_id, amount_stars=amount, status="pending_payment")


@app.get("/api/v1/sessions/{session_id}", response_model=SessionOut)
def get_session(session_id: str):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, spread_id, status, payload_json, result_json, created_at, updated_at FROM sessions WHERE id = ?",
        (session_id,),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")

    payload = json_loads(row["payload_json"]) if row["payload_json"] else {}
    result = json_loads(row["result_json"]) if row["result_json"] else None

    return SessionOut(
        id=row["id"],
        spread_id=row["spread_id"],
        status=row["status"],
        payload=payload,
        result=result,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@app.post("/api/v1/telegram/payment_event")
def payment_event(evt: PaymentEventIn):
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT id, status FROM sessions WHERE id = ?", (evt.session_id,))
    s = cur.fetchone()
    if not s:
        conn.close()
        raise HTTPException(status_code=404, detail="Session not found")

    payment_id = str(uuid.uuid4())
    cur.execute(
        """
        INSERT INTO payments (id, session_id, provider, status, telegram_charge_id, amount_stars, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (payment_id, evt.session_id, evt.provider, evt.status, evt.telegram_charge_id, evt.amount_stars, now_iso()),
    )

    if evt.status == "paid":
        cur.execute("UPDATE sessions SET status = ?, updated_at = ? WHERE id = ?", ("paid", now_iso(), evt.session_id))
    else:
        cur.execute("UPDATE sessions SET status = ?, updated_at = ? WHERE id = ?", ("created", now_iso(), evt.session_id))

    conn.commit()
    conn.close()
    return {"ok": True}


# -------------------------
# Small JSON helpers
# -------------------------
import json as _json

def json_dumps(obj: Any) -> str:
    return _json.dumps(obj, ensure_ascii=False)

def json_loads(s: str) -> Any:
    return _json.loads(s)
