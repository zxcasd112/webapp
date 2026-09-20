import json
import sqlite3
import threading
from pathlib import Path
from datetime import date, datetime

DB_PATH = Path("data") / "journal.db"

_local = threading.local()

DEFAULT_PROFILE = {
    "title": "slaughter_lord",
    "sub": "Записывай. Кастомизируй. Побеждай.",
    "label": "задача",
    "accent": "#ff0d0d",
    "mode": "blood",
}


def _get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        _local.conn = conn
    return conn


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today() -> str:
    return date.today().isoformat()


def init_db() -> None:
    conn = _get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS profile (
            user_id INTEGER PRIMARY KEY,
            title TEXT NOT NULL DEFAULT 'slaughter_lord',
            sub TEXT NOT NULL DEFAULT 'Записывай. Кастомизируй. Побеждай.',
            label TEXT NOT NULL DEFAULT 'задача',
            accent TEXT NOT NULL DEFAULT '#ff0d0d',
            mode TEXT NOT NULL DEFAULT 'blood'
        );

        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            icon TEXT NOT NULL DEFAULT '',
            color TEXT NOT NULL DEFAULT '#ff0d0d',
            sort INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            color TEXT NOT NULL DEFAULT '#b44dff'
        );

        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            parent_id INTEGER,
            project_id INTEGER,
            title TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            done INTEGER NOT NULL DEFAULT 0,
            archived INTEGER NOT NULL DEFAULT 0,
            priority INTEGER NOT NULL DEFAULT 0,
            important INTEGER NOT NULL DEFAULT 0,
            due TEXT,
            scheduled TEXT,
            repeat TEXT NOT NULL DEFAULT '',
            position REAL NOT NULL DEFAULT 0,
            created TEXT NOT NULL,
            updated TEXT NOT NULL,
            FOREIGN KEY (parent_id) REFERENCES tasks(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS task_tags (
            task_id INTEGER NOT NULL,
            tag_id INTEGER NOT NULL,
            PRIMARY KEY (task_id, tag_id)
        );

        CREATE TABLE IF NOT EXISTS habits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            good INTEGER NOT NULL DEFAULT 1,
            color TEXT NOT NULL DEFAULT '#b44dff',
            days TEXT NOT NULL DEFAULT '[]',
            created TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS habit_days (
            habit_id INTEGER NOT NULL,
            day TEXT NOT NULL,
            PRIMARY KEY (habit_id, day)
        );

        CREATE TABLE IF NOT EXISTS diary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            day TEXT NOT NULL,
            text TEXT NOT NULL DEFAULT '',
            mood INTEGER,
            updated TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pomo (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            task_id INTEGER,
            started TEXT NOT NULL,
            minutes INTEGER NOT NULL DEFAULT 25,
            completed INTEGER NOT NULL DEFAULT 1
        );
        """
    )
    conn.commit()


# ---------- profile ----------
def get_profile(user_id: int) -> dict:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM profile WHERE user_id = ?", (user_id,)
    ).fetchone()
    if row is None:
        return dict(DEFAULT_PROFILE)
    return dict(row)


def save_profile(user_id: int, data: dict) -> None:
    conn = _get_conn()
    merged = {**DEFAULT_PROFILE, **data}
    conn.execute(
        "INSERT INTO profile (user_id, title, sub, label, accent, mode) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "title=excluded.title, sub=excluded.sub, label=excluded.label, "
        "accent=excluded.accent, mode=excluded.mode",
        (user_id, merged["title"], merged["sub"], merged["label"],
         merged["accent"], merged["mode"]),
    )
    conn.commit()


# ---------- projects ----------
def list_projects(user_id: int) -> list:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM projects WHERE user_id = ? ORDER BY sort, id", (user_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def add_project(user_id: int, data: dict) -> dict:
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO projects (user_id, name, icon, color, sort) VALUES (?,?,?,?,?)",
        (user_id, data.get("name", "Проект"), data.get("icon", ""),
         data.get("color", "#ff0d0d"), data.get("sort", 0)),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM projects WHERE id = ?", (cur.lastrowid,)
    ).fetchone()
    return dict(row)


def update_project(user_id: int, project_id: int, data: dict) -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM projects WHERE id=? AND user_id=?", (project_id, user_id)
    ).fetchone()
    if row is None:
        return None
    merged = dict(row)
    for k in ("name", "icon", "color", "sort"):
        if k in data:
            merged[k] = data[k]
    conn.execute(
        "UPDATE projects SET name=?, icon=?, color=?, sort=? WHERE id=?",
        (merged["name"], merged["icon"], merged["color"], merged["sort"], project_id),
    )
    conn.commit()
    return merged


def delete_project(user_id: int, project_id: int) -> bool:
    conn = _get_conn()
    cur = conn.execute(
        "DELETE FROM projects WHERE id=? AND user_id=?", (project_id, user_id)
    )
    conn.commit()
    return cur.rowcount > 0


# ---------- tags ----------
def list_tags(user_id: int) -> list:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM tags WHERE user_id = ? ORDER BY name", (user_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def add_tag(user_id: int, name: str, color: str = "#b44dff") -> dict:
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO tags (user_id, name, color) VALUES (?,?,?)",
        (user_id, name, color),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM tags WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


# ---------- tasks ----------
def list_tasks(user_id: int, include_archived: bool = False) -> list:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM tasks WHERE user_id = ?", (user_id,)
    ).fetchall()
    tasks = [dict(r) for r in rows]
    tag_rows = conn.execute(
        "SELECT tt.task_id, t.id AS tag_id, t.name, t.color "
        "FROM task_tags tt JOIN tags t ON t.id = tt.tag_id "
        "WHERE t.user_id = ?",
        (user_id,),
    ).fetchall()
    tag_map: dict[int, list] = {}
    for tr in tag_rows:
        tag_map.setdefault(tr["task_id"], []).append(
            {"id": tr["tag_id"], "name": tr["name"], "color": tr["color"]}
        )
    for t in tasks:
        t["tags"] = tag_map.get(t["id"], [])
    return tasks


def get_task(user_id: int, task_id: int) -> dict | None:
    for t in list_tasks(user_id):
        if t["id"] == task_id:
            return t
    return None


def add_task(user_id: int, data: dict) -> dict:
    conn = _get_conn()
    now = _now_iso()
    cur = conn.execute(
        "INSERT INTO tasks (user_id, parent_id, project_id, title, note, done, "
        "archived, priority, important, due, scheduled, repeat, position, created, updated) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (user_id, data.get("parent_id"), data.get("project_id"), data.get("title", ""),
         data.get("note", ""), 1 if data.get("done") else 0,
         1 if data.get("archived") else 0, data.get("priority", 0),
         1 if data.get("important") else 0, data.get("due"), data.get("scheduled"),
         data.get("repeat", ""), data.get("position", 0), now, now),
    )
    task_id = cur.lastrowid
    _set_task_tags(conn, task_id, data.get("tags", []))
    conn.commit()
    return get_task(user_id, task_id)


def update_task(user_id: int, task_id: int, data: dict) -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM tasks WHERE id=? AND user_id=?", (task_id, user_id)
    ).fetchone()
    if row is None:
        return None
    merged = dict(row)
    field_map = {
        "parent_id": "parent_id", "project_id": "project_id", "title": "title",
        "note": "note", "done": "done", "archived": "archived",
        "priority": "priority", "important": "important", "due": "due",
        "scheduled": "scheduled", "repeat": "repeat", "position": "position",
    }
    for key, col in field_map.items():
        if key in data:
            val = data[key]
            if col in ("done", "archived", "important", "priority"):
                merged[col] = 1 if val else 0
            else:
                merged[col] = val
    merged["updated"] = _now_iso()
    conn.execute(
        "UPDATE tasks SET parent_id=?, project_id=?, title=?, note=?, done=?, "
        "archived=?, priority=?, important=?, due=?, scheduled=?, repeat=?, "
        "position=?, updated=? WHERE id=?",
        (merged["parent_id"], merged["project_id"], merged["title"], merged["note"],
         merged["done"], merged["archived"], merged["priority"], merged["important"],
         merged["due"], merged["scheduled"], merged["repeat"], merged["position"],
         merged["updated"], task_id),
    )
    if "tags" in data:
        conn.execute("DELETE FROM task_tags WHERE task_id=?", (task_id,))
        _set_task_tags(conn, task_id, data["tags"])
    conn.commit()
    return get_task(user_id, task_id)


def _set_task_tags(conn, task_id: int, tags: list) -> None:
    for tag in tags or []:
        if isinstance(tag, dict):
            tid = tag.get("id")
        else:
            tid = tag
        if tid:
            conn.execute(
                "INSERT OR IGNORE INTO task_tags (task_id, tag_id) VALUES (?,?)",
                (task_id, tid),
            )


def delete_task(user_id: int, task_id: int) -> bool:
    conn = _get_conn()
    cur = conn.execute(
        "DELETE FROM tasks WHERE id=? AND user_id=?", (task_id, user_id)
    )
    conn.commit()
    return cur.rowcount > 0


# ---------- habits ----------
def list_habits(user_id: int) -> list:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM habits WHERE user_id = ? ORDER BY id", (user_id,)
    ).fetchall()
    habits = [dict(r) for r in rows]
    day_rows = conn.execute(
        "SELECT habit_id, day FROM habit_days WHERE habit_id IN ("
        "SELECT id FROM habits WHERE user_id = ?)",
        (user_id,),
    ).fetchall()
    day_map: dict[int, list] = {}
    for dr in day_rows:
        day_map.setdefault(dr["habit_id"], []).append(dr["day"])
    for h in habits:
        h["days"] = day_map.get(h["id"], [])
    return habits


def add_habit(user_id: int, data: dict) -> dict:
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO habits (user_id, name, good, color, days, created) VALUES (?,?,?,?,?,?)",
        (user_id, data.get("name", "Привычка"),
         1 if data.get("good", True) else 0, data.get("color", "#b44dff"),
         json.dumps(data.get("days", [])), _now_iso()),
    )
    conn.commit()
    return get_habit(user_id, cur.lastrowid)


def get_habit(user_id: int, habit_id: int) -> dict | None:
    for h in list_habits(user_id):
        if h["id"] == habit_id:
            return h
    return None


def toggle_habit_day(user_id: int, habit_id: int, day: str) -> dict:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM habits WHERE id=? AND user_id=?", (habit_id, user_id)
    ).fetchone()
    if row is None:
        return None
    exists = conn.execute(
        "SELECT 1 FROM habit_days WHERE habit_id=? AND day=?", (habit_id, day)
    ).fetchone()
    if exists:
        conn.execute("DELETE FROM habit_days WHERE habit_id=? AND day=?", (habit_id, day))
    else:
        conn.execute("INSERT INTO habit_days (habit_id, day) VALUES (?,?)", (habit_id, day))
    conn.commit()
    return get_habit(user_id, habit_id)


def delete_habit(user_id: int, habit_id: int) -> bool:
    conn = _get_conn()
    cur = conn.execute(
        "DELETE FROM habits WHERE id=? AND user_id=?", (habit_id, user_id)
    )
    conn.commit()
    return cur.rowcount > 0


# ---------- diary ----------
def list_diary(user_id: int) -> list:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM diary WHERE user_id = ? ORDER BY day DESC", (user_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_diary_day(user_id: int, day: str) -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM diary WHERE user_id=? AND day=?", (user_id, day)
    ).fetchone()
    return dict(row) if row else None


def save_diary_day(user_id: int, day: str, text: str, mood: int | None) -> dict:
    conn = _get_conn()
    conn.execute(
        "INSERT INTO diary (user_id, day, text, mood, updated) VALUES (?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET text=excluded.text, mood=excluded.mood, updated=excluded.updated",
        (user_id, day, text, mood, _now_iso()),
    )
    conn.commit()
    return get_diary_day(user_id, day)


# ---------- pomodoro ----------
def add_pomo(user_id: int, task_id: int | None, minutes: int, completed: bool = True) -> dict:
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO pomo (user_id, task_id, started, minutes, completed) VALUES (?,?,?,?,?)",
        (user_id, task_id, _now_iso(), minutes, 1 if completed else 0),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM pomo WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


# ---------- stats ----------
def stats(user_id: int) -> dict:
    conn = _get_conn()
    today = _today()
    done_total = conn.execute(
        "SELECT COUNT(*) c FROM tasks WHERE user_id=? AND done=1 AND archived=0", (user_id,)
    ).fetchone()["c"]
    open_total = conn.execute(
        "SELECT COUNT(*) c FROM tasks WHERE user_id=? AND done=0 AND archived=0", (user_id,)
    ).fetchone()["c"]
    done_today = conn.execute(
        "SELECT COUNT(*) c FROM tasks WHERE user_id=? AND done=1 AND archived=0 "
        "AND date(updated) = ?", (user_id, today)
    ).fetchone()["c"]
    open_today = conn.execute(
        "SELECT COUNT(*) c FROM tasks WHERE user_id=? AND done=0 AND archived=0 "
        "AND date(due) = ?", (user_id, today)
    ).fetchone()["c"]
    pomo_today = conn.execute(
        "SELECT COALESCE(SUM(minutes),0) m FROM pomo WHERE user_id=? "
        "AND completed=1 AND date(started) = ?", (user_id, today)
    ).fetchone()["m"]
    habit_count = conn.execute(
        "SELECT COUNT(*) c FROM habits WHERE user_id=?", (user_id,)
    ).fetchone()["c"]
    return {
        "done_total": done_total,
        "open_total": open_total,
        "done_today": done_today,
        "open_today": open_today,
        "pomo_minutes_today": pomo_today,
        "habit_count": habit_count,
        "today": today,
    }


def full_state(user_id: int) -> dict:
    """Aggregate everything the client needs on load."""
    return {
        "profile": get_profile(user_id),
        "projects": list_projects(user_id),
        "tags": list_tags(user_id),
        "tasks": list_tasks(user_id),
        "habits": list_habits(user_id),
        "diary": list_diary(user_id),
        "stats": stats(user_id),
    }
