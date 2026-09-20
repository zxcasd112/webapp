import json
import sqlite3
import threading
from pathlib import Path

DB_PATH = Path("data") / "journal.db"

_local = threading.local()

DEFAULT_STATE = {
    "title": "БОЕВОЙ ДНЕВНИК",
    "sub": "Записывай. Кастомизируй. Побеждай.",
    "label": "задача",
    "accent": "#ff2e2e",
    "mode": "blood",
    "tasks": [],
}


def _get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        _local.conn = conn
    return conn


def init_db() -> None:
    conn = _get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            state TEXT NOT NULL
        )
        """
    )
    conn.commit()


def get_state(user_id: int) -> dict:
    conn = _get_conn()
    row = conn.execute("SELECT state FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if row is None:
        return json.loads(json.dumps(DEFAULT_STATE))
    try:
        state = json.loads(row["state"])
    except (ValueError, TypeError):
        return json.loads(json.dumps(DEFAULT_STATE))
    merged = json.loads(json.dumps(DEFAULT_STATE))
    merged.update(state)
    return merged


def save_state(user_id: int, state: dict) -> None:
    conn = _get_conn()
    conn.execute(
        "INSERT INTO users (user_id, state) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET state = excluded.state",
        (user_id, json.dumps(state, ensure_ascii=False)),
    )
    conn.commit()
