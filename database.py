"""
database.py — SQLite schema and all CRUD helpers for MemoraeBot
Tables: users, memories, tasks, reminders
"""

import sqlite3
import os
import json
import logging
from datetime import datetime
from typing import Optional

DB_PATH = os.getenv("DB_PATH", "memorae.db")
log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Connection
# ──────────────────────────────────────────────────────────────────────────────

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ──────────────────────────────────────────────────────────────────────────────
# Schema bootstrap
# ──────────────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id      INTEGER UNIQUE NOT NULL,
    name             TEXT,
    apple_id         TEXT,
    apple_password   TEXT,
    briefing_time    TEXT    DEFAULT '07:00',
    timezone         TEXT    DEFAULT 'Asia/Kolkata',
    serendipity_on   INTEGER DEFAULT 1,
    onboarded        INTEGER DEFAULT 0,
    created_at       TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    content     TEXT    NOT NULL,
    tags        TEXT    DEFAULT '[]',
    collection  TEXT    DEFAULT 'General',
    source      TEXT    DEFAULT 'text',
    created_at  TEXT    DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    title       TEXT    NOT NULL,
    description TEXT,
    status      TEXT    DEFAULT 'queue',
    priority    TEXT    DEFAULT 'normal',
    deadline    TEXT,
    context     TEXT,
    created_at  TEXT    DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS reminders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    content     TEXT    NOT NULL,
    remind_at   TEXT    NOT NULL,
    is_sent     INTEGER DEFAULT 0,
    job_id      TEXT,
    created_at  TEXT    DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id);
CREATE INDEX IF NOT EXISTS idx_tasks_user    ON tasks(user_id);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(remind_at, is_sent);
"""


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
    log.info("Database initialised at %s", DB_PATH)


# ──────────────────────────────────────────────────────────────────────────────
# USERS
# ──────────────────────────────────────────────────────────────────────────────

def upsert_user(telegram_id: int, name: str = "") -> dict:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (telegram_id, name) VALUES (?, ?)",
            (telegram_id, name),
        )
        row = conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
    return dict(row)


def get_user(telegram_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
    return dict(row) if row else None


def get_user_by_id(user_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    return dict(row) if row else None


def update_user(telegram_id: int, **kwargs) -> None:
    allowed = {"name", "apple_id", "apple_password", "briefing_time", "timezone",
               "serendipity_on", "onboarded"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as conn:
        conn.execute(
            f"UPDATE users SET {set_clause} WHERE telegram_id = ?",
            (*fields.values(), telegram_id),
        )


def get_all_users() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM users WHERE onboarded = 1").fetchall()
    return [dict(r) for r in rows]


# ──────────────────────────────────────────────────────────────────────────────
# MEMORIES
# ──────────────────────────────────────────────────────────────────────────────

def add_memory(user_id: int, content: str, tags: list = None,
               collection: str = "General", source: str = "text") -> dict:
    tags_json = json.dumps(tags or [])
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO memories (user_id, content, tags, collection, source)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, content, tags_json, collection, source),
        )
        row = conn.execute(
            "SELECT * FROM memories WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return dict(row)


def get_memories(user_id: int, collection: str = None, limit: int = 20) -> list[dict]:
    with get_conn() as conn:
        if collection:
            rows = conn.execute(
                "SELECT * FROM memories WHERE user_id = ? AND collection = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (user_id, collection, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM memories WHERE user_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
    return [dict(r) for r in rows]


def search_memories(user_id: int, query: str, limit: int = 10) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM memories WHERE user_id = ? AND content LIKE ? "
            "ORDER BY created_at DESC LIMIT ?",
            (user_id, f"%{query}%", limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_random_memory(user_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM memories WHERE user_id = ? ORDER BY RANDOM() LIMIT 1",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def get_memory_count(user_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM memories WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row["cnt"] if row else 0


def get_collections(user_id: int) -> list[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT collection FROM memories WHERE user_id = ? ORDER BY collection",
            (user_id,),
        ).fetchall()
    return [r["collection"] for r in rows]


def get_collections_with_counts(user_id: int) -> list[tuple]:
    """Returns list of (collection_name, count) sorted by count desc."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT collection, COUNT(*) as cnt FROM memories "
            "WHERE user_id = ? AND source != 'system' "
            "GROUP BY collection ORDER BY cnt DESC",
            (user_id,),
        ).fetchall()
    return [(r["collection"], r["cnt"]) for r in rows]


def rename_collection(user_id: int, old_name: str, new_name: str) -> int:
    """Rename all memories from old_name collection to new_name. Returns count updated."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE memories SET collection = ? WHERE user_id = ? AND collection = ?",
            (new_name, user_id, old_name),
        )
    return cur.rowcount


def delete_memory(memory_id: int, user_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM memories WHERE id = ? AND user_id = ?",
            (memory_id, user_id),
        )
    return cur.rowcount > 0


# ──────────────────────────────────────────────────────────────────────────────
# TASKS
# ──────────────────────────────────────────────────────────────────────────────

TASK_STATUSES = ("queue", "this_week", "today", "done")


def add_task(user_id: int, title: str, description: str = "",
             status: str = "queue", priority: str = "normal",
             deadline: str = None, context: str = None) -> dict:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO tasks (user_id, title, description, status, priority, deadline, context)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, title, description or "", status, priority, deadline, context),
        )
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return dict(row)


def get_tasks(user_id: int, status: str = None) -> list[dict]:
    with get_conn() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE user_id = ? AND status = ? "
                "ORDER BY CASE priority WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END, created_at",
                (user_id, status),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE user_id = ? AND status != 'done' "
                "ORDER BY CASE status WHEN 'today' THEN 1 WHEN 'this_week' THEN 2 ELSE 3 END, "
                "CASE priority WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END",
                (user_id,),
            ).fetchall()
    return [dict(r) for r in rows]


def get_task_by_id(task_id: int, user_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id)
        ).fetchone()
    return dict(row) if row else None


def update_task(task_id: int, user_id: int, **kwargs) -> bool:
    allowed = {"title", "description", "status", "priority", "deadline", "context"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return False
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE tasks SET {set_clause} WHERE id = ? AND user_id = ?",
            (*fields.values(), task_id, user_id),
        )
    return cur.rowcount > 0


def delete_task(task_id: int, user_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id)
        )
    return cur.rowcount > 0


def get_task_counts(user_id: int) -> dict:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM tasks WHERE user_id = ? GROUP BY status",
            (user_id,),
        ).fetchall()
    counts = {"queue": 0, "this_week": 0, "today": 0, "done": 0}
    for r in rows:
        counts[r["status"]] = r["cnt"]
    return counts


# ──────────────────────────────────────────────────────────────────────────────
# REMINDERS
# ──────────────────────────────────────────────────────────────────────────────

def add_reminder(user_id: int, content: str, remind_at: str, job_id: str = None) -> dict:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO reminders (user_id, content, remind_at, job_id) VALUES (?, ?, ?, ?)",
            (user_id, content, remind_at, job_id),
        )
        row = conn.execute(
            "SELECT * FROM reminders WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return dict(row)


def get_pending_reminders(user_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM reminders WHERE user_id = ? AND is_sent = 0 "
            "ORDER BY remind_at ASC",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_reminder_sent(reminder_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE reminders SET is_sent = 1 WHERE id = ?", (reminder_id,)
        )


def get_all_due_reminders() -> list[dict]:
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT r.*, u.telegram_id FROM reminders r "
            "JOIN users u ON r.user_id = u.id "
            "WHERE r.is_sent = 0 AND r.remind_at <= ?",
            (now,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_reminder(reminder_id: int, user_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM reminders WHERE id = ? AND user_id = ?",
            (reminder_id, user_id),
        )
    return cur.rowcount > 0


def delete_reminders_by_date(user_id: int, date_str: str) -> int:
    """Delete all unsent reminders on a given date (YYYY-MM-DD). Returns count deleted."""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM reminders WHERE user_id = ? AND is_sent = 0 AND remind_at LIKE ?",
            (user_id, f"{date_str}%"),
        )
    return cur.rowcount


def delete_all_reminders(user_id: int) -> int:
    """Delete all pending reminders for a user. Returns count deleted."""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM reminders WHERE user_id = ? AND is_sent = 0",
            (user_id,),
        )
    return cur.rowcount


def delete_reminders_except(user_id: int, except_keyword: str) -> tuple[int, int]:
    """Delete all pending reminders except those whose content contains except_keyword.
    Returns (deleted_count, kept_count)."""
    with get_conn() as conn:
        kept = conn.execute(
            "SELECT COUNT(*) as cnt FROM reminders WHERE user_id = ? AND is_sent = 0 AND content LIKE ?",
            (user_id, f"%{except_keyword}%"),
        ).fetchone()["cnt"]
        cur = conn.execute(
            "DELETE FROM reminders WHERE user_id = ? AND is_sent = 0 AND content NOT LIKE ?",
            (user_id, f"%{except_keyword}%"),
        )
    return cur.rowcount, kept
