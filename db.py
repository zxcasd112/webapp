"""Слой хранения данных (SQLite).

Ключевые решения:

* WAL + ``synchronous=NORMAL`` — быстрая запись и чтение без блокировок;
* одно соединение на поток (event loop aiohttp однопоточный) с ``busy_timeout``,
  чтобы параллельные запросы не падали с «database is locked»;
* индексы по ``user_id`` для всех выборок — время ответа не зависит от объёма;
* дневник — ровно одна запись на день (``UNIQUE(user_id, day)`` + UPSERT);
* батч-операции (:func:`apply_ops`) выполняются в одной транзакции: клиент
  отправляет пачку изменений одним запросом вместо запроса на каждое действие.

Модуль сознательно не использует внешних зависимостей и блокирующего I/O,
кроме самого SQLite: объём данных одного пользователя — сотни записей, поэтому
синхронные вызовы дешевле запуска задач в пуле потоков.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path

import config

DEFAULT_PROFILE = {
    "title": "slaughter_lord",
    "sub": "Записывай. Кастомизируй. Побеждай.",
    "label": "задача",
    "accent": "#ff0d0d",
    "mode": "blood",
}

PROFILE_FIELDS = ("title", "sub", "label", "accent", "mode")
TASK_FIELDS = (
    "parent_id", "project_id", "title", "note", "done", "archived",
    "priority", "important", "due", "scheduled", "repeat", "position",
)
BOOL_FIELDS = ("done", "archived", "important")

MAX_TEXT = 500
MAX_TITLE = 200
MAX_NOTE = 4000
MAX_DAY = 16
MAX_OPS_PER_REQUEST = 200

_local = threading.local()

SCHEMA = """
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

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id);
CREATE INDEX IF NOT EXISTS idx_tasks_user_due ON tasks(user_id, due);
CREATE INDEX IF NOT EXISTS idx_projects_user ON projects(user_id);
CREATE INDEX IF NOT EXISTS idx_tags_user ON tags(user_id);
CREATE INDEX IF NOT EXISTS idx_tasks_tags_task ON task_tags(task_id);
CREATE INDEX IF NOT EXISTS idx_habits_user ON habits(user_id);
CREATE INDEX IF NOT EXISTS idx_habit_days_habit ON habit_days(habit_id);
CREATE INDEX IF NOT EXISTS idx_pomo_user ON pomo(user_id, started);
CREATE INDEX IF NOT EXISTS idx_diary_user_day ON diary(user_id, day);
"""


# --------------------------------------------------------------------------
# соединение
# --------------------------------------------------------------------------
def db_path() -> Path:
    """Путь к файлу базы. Читается из конфига на каждом вызове (удобно в тестах)."""
    return Path(config.DATA_DIR) / "journal.db"


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0, isolation_level="")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_conn() -> sqlite3.Connection:
    """Соединение текущего потока. Переоткрывается, если сменился путь к базе."""
    path = db_path()
    conn = getattr(_local, "conn", None)
    if conn is None or getattr(_local, "path", None) != path:
        if conn is not None:
            conn.close()
        conn = _connect(path)
        _local.conn = conn
        _local.path = path
    return conn


def dispose() -> None:
    """Закрыть соединение текущего потока (используется в тестах и при выходе)."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None
        _local.path = None


def init_db() -> None:
    """Создаёт схему и приводит её к актуальному виду (идемпотентно)."""
    conn = get_conn()
    conn.executescript(SCHEMA)
    # Легаси-базы могли содержать несколько записей дневника за один день:
    # оставляем самую свежую, иначе UNIQUE-индекс не создать.
    conn.execute(
        "DELETE FROM diary WHERE id NOT IN "
        "(SELECT MAX(id) FROM diary GROUP BY user_id, day)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uniq_diary_user_day ON diary(user_id, day)"
    )
    conn.executescript(INDEXES)
    conn.commit()


# --------------------------------------------------------------------------
# утилиты
# --------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today() -> str:
    return date.today().isoformat()


def _clip(value, limit: int = MAX_TEXT) -> str:
    if value is None:
        return ""
    text = str(value)
    return text[:limit]


def _opt_day(value) -> str | None:
    if not value:
        return None
    text = _clip(value, MAX_DAY)
    return text or None


def _opt_int(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bit(value) -> int:
    return 1 if value else 0


# --------------------------------------------------------------------------
# профиль
# --------------------------------------------------------------------------
def get_profile(user_id: int) -> dict:
    row = get_conn().execute(
        "SELECT * FROM profile WHERE user_id = ?", (user_id,)
    ).fetchone()
    if row is None:
        return dict(DEFAULT_PROFILE)
    return dict(row)


def _upsert_profile(conn: sqlite3.Connection, user_id: int, data: dict) -> None:
    row = conn.execute("SELECT * FROM profile WHERE user_id = ?", (user_id,)).fetchone()
    merged = dict(row) if row is not None else dict(DEFAULT_PROFILE)
    limits = {"title": 60, "sub": 120, "label": 40, "accent": 32, "mode": 16}
    for key in PROFILE_FIELDS:
        value = data.get(key)
        if value is None:
            continue
        merged[key] = _clip(value, limits.get(key, 60))
    conn.execute(
        "INSERT INTO profile (user_id, title, sub, label, accent, mode) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET title=excluded.title, sub=excluded.sub, "
        "label=excluded.label, accent=excluded.accent, mode=excluded.mode",
        (
            user_id,
            merged["title"],
            merged["sub"],
            merged["label"],
            merged["accent"],
            merged["mode"],
        ),
    )


def save_profile(user_id: int, data: dict) -> dict:
    conn = get_conn()
    _upsert_profile(conn, user_id, data)
    conn.commit()
    return get_profile(user_id)


# --------------------------------------------------------------------------
# проекты
# --------------------------------------------------------------------------
def list_projects(user_id: int) -> list[dict]:
    rows = get_conn().execute(
        "SELECT * FROM projects WHERE user_id = ? ORDER BY sort, id", (user_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def _project_row(conn: sqlite3.Connection, user_id: int, project_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM projects WHERE id = ? AND user_id = ?", (project_id, user_id)
    ).fetchone()
    return dict(row) if row is not None else None


def add_project(user_id: int, data: dict) -> dict:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO projects (user_id, name, icon, color, sort) VALUES (?,?,?,?,?)",
        (
            user_id,
            _clip(data.get("name") or "Проект", 60),
            _clip(data.get("icon"), 8),
            _clip(data.get("color") or "#ff0d0d", 32),
            _opt_int(data.get("sort")) or 0,
        ),
    )
    conn.commit()
    return _project_row(conn, user_id, cur.lastrowid) or {}


def update_project(user_id: int, project_id: int, data: dict) -> dict | None:
    conn = get_conn()
    row = _project_row(conn, user_id, project_id)
    if row is None:
        return None
    for key, limit in (("name", 60), ("icon", 8), ("color", 32)):
        if data.get(key) is not None:
            row[key] = _clip(data[key], limit)
    if data.get("sort") is not None:
        row["sort"] = _opt_int(data["sort"]) or 0
    conn.execute(
        "UPDATE projects SET name=?, icon=?, color=?, sort=? WHERE id=? AND user_id=?",
        (row["name"], row["icon"], row["color"], row["sort"], project_id, user_id),
    )
    conn.commit()
    return row


def delete_project(user_id: int, project_id: int) -> bool:
    conn = get_conn()
    cur = conn.execute(
        "DELETE FROM projects WHERE id = ? AND user_id = ?", (project_id, user_id)
    )
    # задачи удалённого проекта остаются, но теряют привязку
    conn.execute(
        "UPDATE tasks SET project_id = NULL WHERE project_id = ? AND user_id = ?",
        (project_id, user_id),
    )
    conn.commit()
    return cur.rowcount > 0


# --------------------------------------------------------------------------
# теги
# --------------------------------------------------------------------------
def list_tags(user_id: int) -> list[dict]:
    rows = get_conn().execute(
        "SELECT * FROM tags WHERE user_id = ? ORDER BY name", (user_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def add_tag(user_id: int, name: str, color: str = "#b44dff") -> dict:
    conn = get_conn()
    name = _clip(name, 40).strip() or "тег"
    row = conn.execute(
        "SELECT * FROM tags WHERE user_id = ? AND name = ?", (user_id, name)
    ).fetchone()
    if row is not None:
        return dict(row)
    cur = conn.execute(
        "INSERT INTO tags (user_id, name, color) VALUES (?,?,?)",
        (user_id, name, _clip(color, 32)),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM tags WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


# --------------------------------------------------------------------------
# задачи
# --------------------------------------------------------------------------
def _set_task_tags(conn: sqlite3.Connection, task_id: int, tags: list, tag_map: dict | None = None) -> None:
    for tag in _normalize_tags(tags, tag_map or {}):
        conn.execute(
            "INSERT OR IGNORE INTO task_tags (task_id, tag_id) VALUES (?,?)",
            (task_id, tag),
        )


def _normalize_tags(tags, tag_map: dict) -> list[int]:
    """Приводит ссылки на теги к серверным id.

    В пачке /api/sync задача может ссылаться на тег, который создан этой же
    пачкой: тогда в качестве id приходит ``cid`` операции tag.add, а не число.
    ``tag_map`` хранит соответствие cid -> настоящий id.
    """
    result: list[int] = []
    for tag in tags or []:
        if isinstance(tag, dict):
            tag = tag.get("id")
        mapped = tag_map.get(str(tag))
        if mapped is not None:
            result.append(int(mapped))
            continue
        tag_id = _opt_int(tag)
        if tag_id:
            result.append(tag_id)
    return result


def list_tasks(user_id: int, include_archived: bool = True) -> list[dict]:
    conn = get_conn()
    sql = "SELECT * FROM tasks WHERE user_id = ?"
    if not include_archived:
        sql += " AND archived = 0"
    tasks = [dict(r) for r in conn.execute(sql + " ORDER BY id", (user_id,)).fetchall()]
    if not tasks:
        return tasks
    rows = conn.execute(
        "SELECT tt.task_id, t.id AS tag_id, t.name, t.color "
        "FROM task_tags tt JOIN tags t ON t.id = tt.tag_id "
        "WHERE t.user_id = ?",
        (user_id,),
    ).fetchall()
    tag_map: dict[int, list] = {}
    for row in rows:
        tag_map.setdefault(row["task_id"], []).append(
            {"id": row["tag_id"], "name": row["name"], "color": row["color"]}
        )
    for task in tasks:
        task["tags"] = tag_map.get(task["id"], [])
    return tasks


def get_task(user_id: int, task_id: int) -> dict | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id)
    ).fetchone()
    if row is None:
        return None
    task = dict(row)
    task["tags"] = [
        dict(r)
        for r in conn.execute(
            "SELECT t.id, t.name, t.color FROM task_tags tt "
            "JOIN tags t ON t.id = tt.tag_id WHERE tt.task_id = ? ORDER BY t.name",
            (task_id,),
        ).fetchall()
    ]
    return task


def _insert_task(conn: sqlite3.Connection, user_id: int, data: dict, tag_map: dict | None = None) -> int:
    now = _now_iso()
    cur = conn.execute(
        "INSERT INTO tasks (user_id, parent_id, project_id, title, note, done, "
        "archived, priority, important, due, scheduled, repeat, position, created, updated) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            user_id,
            _opt_int(data.get("parent_id")),
            _opt_int(data.get("project_id")),
            _clip(data.get("title") or "Новая задача", MAX_TITLE),
            _clip(data.get("note"), MAX_NOTE),
            _bit(data.get("done")),
            _bit(data.get("archived")),
            _opt_int(data.get("priority")) or 0,
            _bit(data.get("important")),
            _opt_day(data.get("due")),
            _clip(data.get("scheduled"), 8) or None,
            _clip(data.get("repeat"), 16),
            float(data.get("position") or 0),
            now,
            now,
        ),
    )
    task_id = int(cur.lastrowid)
    _set_task_tags(conn, task_id, data.get("tags") or [], tag_map)
    return task_id


def add_task(user_id: int, data: dict) -> dict:
    conn = get_conn()
    task_id = _insert_task(conn, user_id, data)
    conn.commit()
    return get_task(user_id, task_id) or {}


def _update_task(
    conn: sqlite3.Connection,
    user_id: int,
    task_id: int,
    data: dict,
    tag_map: dict | None = None,
) -> bool:
    row = conn.execute(
        "SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id)
    ).fetchone()
    if row is None:
        return False
    merged = dict(row)
    for key in TASK_FIELDS:
        if key not in data:
            continue
        value = data[key]
        if key in BOOL_FIELDS:
            merged[key] = _bit(value)
        elif key == "due":
            merged[key] = _opt_day(value)
        elif key == "scheduled":
            merged[key] = _clip(value, 8) or None
        elif key in ("parent_id", "project_id"):
            merged[key] = _opt_int(value)
        elif key == "priority":
            merged[key] = _opt_int(value) or 0
        elif key == "position":
            merged[key] = float(value or 0)
        elif key == "title":
            merged[key] = _clip(value or merged["title"], MAX_TITLE)
        elif key == "note":
            merged[key] = _clip(value, MAX_NOTE)
        else:
            merged[key] = _clip(value, 16)
    merged["updated"] = _now_iso()
    conn.execute(
        "UPDATE tasks SET parent_id=?, project_id=?, title=?, note=?, done=?, "
        "archived=?, priority=?, important=?, due=?, scheduled=?, repeat=?, "
        "position=?, updated=? WHERE id=? AND user_id=?",
        (
            merged["parent_id"],
            merged["project_id"],
            merged["title"],
            merged["note"],
            merged["done"],
            merged["archived"],
            merged["priority"],
            merged["important"],
            merged["due"],
            merged["scheduled"],
            merged["repeat"],
            merged["position"],
            merged["updated"],
            task_id,
            user_id,
        ),
    )
    if "tags" in data:
        conn.execute("DELETE FROM task_tags WHERE task_id = ?", (task_id,))
        _set_task_tags(conn, task_id, data["tags"] or [], tag_map)
    return True


def update_task(user_id: int, task_id: int, data: dict) -> dict | None:
    conn = get_conn()
    if not _update_task(conn, user_id, task_id, data):
        return None
    conn.commit()
    return get_task(user_id, task_id)


def _delete_task(conn: sqlite3.Connection, user_id: int, task_id: int) -> bool:
    conn.execute("DELETE FROM task_tags WHERE task_id = ?", (task_id,))
    cur = conn.execute(
        "DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id)
    )
    return cur.rowcount > 0


def delete_task(user_id: int, task_id: int) -> bool:
    conn = get_conn()
    ok = _delete_task(conn, user_id, task_id)
    conn.commit()
    return ok


# --------------------------------------------------------------------------
# привычки
# --------------------------------------------------------------------------
def _attach_days(conn: sqlite3.Connection, user_id: int, habits: list[dict]) -> list[dict]:
    if not habits:
        return habits
    rows = conn.execute(
        "SELECT hd.habit_id, hd.day FROM habit_days hd "
        "JOIN habits h ON h.id = hd.habit_id WHERE h.user_id = ? ORDER BY hd.day",
        (user_id,),
    ).fetchall()
    day_map: dict[int, list[str]] = {}
    for row in rows:
        day_map.setdefault(row["habit_id"], []).append(row["day"])
    for habit in habits:
        habit["days"] = day_map.get(habit["id"], [])
    return habits


def list_habits(user_id: int) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM habits WHERE user_id = ? ORDER BY id", (user_id,)
    ).fetchall()
    return _attach_days(conn, user_id, [dict(r) for r in rows])


def get_habit(user_id: int, habit_id: int) -> dict | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM habits WHERE id = ? AND user_id = ?", (habit_id, user_id)
    ).fetchone()
    if row is None:
        return None
    return _attach_days(conn, user_id, [dict(row)])[0]


def _insert_habit(conn: sqlite3.Connection, user_id: int, data: dict) -> int:
    cur = conn.execute(
        "INSERT INTO habits (user_id, name, good, color, days, created) VALUES (?,?,?,?,?,?)",
        (
            user_id,
            _clip(data.get("name") or "Привычка", 60),
            _bit(data.get("good", True)),
            _clip(data.get("color") or "#b44dff", 32),
            "[]",
            _now_iso(),
        ),
    )
    habit_id = int(cur.lastrowid)
    for day in data.get("days") or []:
        value = _opt_day(day)
        if value:
            conn.execute(
                "INSERT OR IGNORE INTO habit_days (habit_id, day) VALUES (?,?)",
                (habit_id, value),
            )
    return habit_id


def add_habit(user_id: int, data: dict) -> dict:
    conn = get_conn()
    habit_id = _insert_habit(conn, user_id, data)
    conn.commit()
    return get_habit(user_id, habit_id) or {}


def _toggle_habit_day(
    conn: sqlite3.Connection, user_id: int, habit_id: int, day: str
) -> bool | None:
    owner = conn.execute(
        "SELECT 1 FROM habits WHERE id = ? AND user_id = ?", (habit_id, user_id)
    ).fetchone()
    if owner is None:
        return None
    exists = conn.execute(
        "SELECT 1 FROM habit_days WHERE habit_id = ? AND day = ?", (habit_id, day)
    ).fetchone()
    if exists is not None:
        conn.execute(
            "DELETE FROM habit_days WHERE habit_id = ? AND day = ?", (habit_id, day)
        )
    else:
        conn.execute(
            "INSERT OR IGNORE INTO habit_days (habit_id, day) VALUES (?,?)",
            (habit_id, day),
        )
    return exists is None


def toggle_habit_day(user_id: int, habit_id: int, day: str) -> dict | None:
    conn = get_conn()
    day_value = _opt_day(day)
    if day_value is None:
        return None
    if _toggle_habit_day(conn, user_id, habit_id, day_value) is None:
        return None
    conn.commit()
    return get_habit(user_id, habit_id)


def _delete_habit(conn: sqlite3.Connection, user_id: int, habit_id: int) -> bool:
    conn.execute("DELETE FROM habit_days WHERE habit_id = ?", (habit_id,))
    cur = conn.execute(
        "DELETE FROM habits WHERE id = ? AND user_id = ?", (habit_id, user_id)
    )
    return cur.rowcount > 0


def delete_habit(user_id: int, habit_id: int) -> bool:
    conn = get_conn()
    ok = _delete_habit(conn, user_id, habit_id)
    conn.commit()
    return ok


# --------------------------------------------------------------------------
# дневник
# --------------------------------------------------------------------------
def list_diary(user_id: int, limit: int = 120) -> list[dict]:
    rows = get_conn().execute(
        "SELECT * FROM diary WHERE user_id = ? ORDER BY day DESC, id DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_diary_day(user_id: int, day: str) -> dict | None:
    row = get_conn().execute(
        "SELECT * FROM diary WHERE user_id = ? AND day = ?", (user_id, day)
    ).fetchone()
    return dict(row) if row is not None else None


def _upsert_diary(
    conn: sqlite3.Connection, user_id: int, day: str, text: str, mood
) -> None:
    conn.execute(
        "INSERT INTO diary (user_id, day, text, mood, updated) VALUES (?,?,?,?,?) "
        "ON CONFLICT(user_id, day) DO UPDATE SET text=excluded.text, "
        "mood=excluded.mood, updated=excluded.updated",
        (
            user_id,
            day,
            _clip(text, MAX_NOTE),
            _opt_int(mood),
            _now_iso(),
        ),
    )


def save_diary_day(user_id: int, day: str, text: str, mood) -> dict | None:
    day_value = _opt_day(day)
    if day_value is None:
        return None
    conn = get_conn()
    _upsert_diary(conn, user_id, day_value, text, mood)
    conn.commit()
    return get_diary_day(user_id, day_value)


# --------------------------------------------------------------------------
# помодоро
# --------------------------------------------------------------------------
def add_pomo(
    user_id: int, task_id, minutes: int = 25, completed: bool = True
) -> dict:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO pomo (user_id, task_id, started, minutes, completed) VALUES (?,?,?,?,?)",
        (
            user_id,
            _opt_int(task_id),
            _now_iso(),
            max(1, min(int(minutes or 25), 600)),
            _bit(completed),
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM pomo WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


# --------------------------------------------------------------------------
# статистика и полное состояние
# --------------------------------------------------------------------------
def stats(user_id: int) -> dict:
    conn = get_conn()
    today = _today()
    row = conn.execute(
        "SELECT "
        "COALESCE(SUM(CASE WHEN done=1 AND archived=0 THEN 1 ELSE 0 END),0) AS done_total, "
        "COALESCE(SUM(CASE WHEN done=0 AND archived=0 THEN 1 ELSE 0 END),0) AS open_total, "
        "COALESCE(SUM(CASE WHEN done=1 AND archived=0 AND date(updated)=? THEN 1 ELSE 0 END),0) AS done_today, "
        "COALESCE(SUM(CASE WHEN done=0 AND archived=0 AND date(due)=? THEN 1 ELSE 0 END),0) AS open_today, "
        "COALESCE(SUM(CASE WHEN done=0 AND archived=0 AND due IS NOT NULL AND date(due)<? "
        "THEN 1 ELSE 0 END),0) AS overdue "
        "FROM tasks WHERE user_id = ?",
        (today, today, today, user_id),
    ).fetchone()
    pomo = conn.execute(
        "SELECT COALESCE(SUM(minutes),0) AS m, COUNT(*) AS c FROM pomo "
        "WHERE user_id = ? AND completed = 1 AND date(started) = ?",
        (user_id, today),
    ).fetchone()
    habits = conn.execute(
        "SELECT COUNT(*) AS c FROM habits WHERE user_id = ?", (user_id,)
    ).fetchone()
    return {
        "done_total": row["done_total"],
        "open_total": row["open_total"],
        "done_today": row["done_today"],
        "open_today": row["open_today"],
        "overdue": row["overdue"],
        "pomo_minutes_today": pomo["m"],
        "pomo_count_today": pomo["c"],
        "habit_count": habits["c"],
        "today": today,
    }


def state_version(user_id: int) -> str:
    """Дешёвая версия состояния для ETag (только агрегаты по индексам).

    Позволяет отвечать ``304 Not Modified`` без сборки и сериализации полного
    состояния — а это самый частый сценарий повторного открытия WebApp.
    """
    row = get_conn().execute(
        "SELECT "
        "(SELECT COUNT(*) FROM tasks WHERE user_id=:u) AS t_cnt, "
        "(SELECT COALESCE(MAX(updated),'') FROM tasks WHERE user_id=:u) AS t_upd, "
        "(SELECT COALESCE(SUM(LENGTH(title)),0) FROM tasks WHERE user_id=:u) AS t_len, "
        "(SELECT COUNT(*) FROM task_tags WHERE task_id IN "
        " (SELECT id FROM tasks WHERE user_id=:u)) AS t_tags, "
        "(SELECT COUNT(*) FROM habits WHERE user_id=:u) AS h_cnt, "
        "(SELECT COALESCE(SUM(LENGTH(name)),0) FROM habits WHERE user_id=:u) AS h_len, "
        "(SELECT COUNT(*) FROM habit_days WHERE habit_id IN "
        " (SELECT id FROM habits WHERE user_id=:u)) AS h_days, "
        "(SELECT COUNT(*) FROM diary WHERE user_id=:u) AS d_cnt, "
        "(SELECT COALESCE(MAX(updated),'') FROM diary WHERE user_id=:u) AS d_upd, "
        "(SELECT COALESCE(SUM(LENGTH(text)),0) FROM diary WHERE user_id=:u) AS d_len, "
        "(SELECT COUNT(*) FROM pomo WHERE user_id=:u) AS p_cnt, "
        "(SELECT COUNT(*) FROM projects WHERE user_id=:u) AS prj_cnt, "
        "(SELECT COALESCE(SUM(LENGTH(title))+SUM(LENGTH(sub))+SUM(LENGTH(label))"
        "+SUM(LENGTH(accent))+SUM(LENGTH(mode)),0) FROM profile WHERE user_id=:u) AS pr_len",
        {"u": user_id},
    ).fetchone()
    return ":".join([_today()] + [str(value) for value in tuple(row)])


def full_state(user_id: int) -> dict:
    """Всё, что нужно клиенту при открытии приложения — за один запрос."""
    return {
        "profile": get_profile(user_id),
        "projects": list_projects(user_id),
        "tags": list_tags(user_id),
        "tasks": list_tasks(user_id),
        "habits": list_habits(user_id),
        "diary": list_diary(user_id),
        "stats": stats(user_id),
    }


# --------------------------------------------------------------------------
# батч-синхронизация
# --------------------------------------------------------------------------
def _apply_op(conn: sqlite3.Connection, user_id: int, op: dict, tag_map: dict) -> dict:
    """Одна операция пачки. Выполняется в транзакции вызывающего."""
    kind = str(op.get("t") or op.get("type") or "").strip().lower()
    data = op.get("data") if isinstance(op.get("data"), dict) else {}
    cid = op.get("cid")

    if kind in ("task.add", "task_add"):
        task_id = _insert_task(conn, user_id, data, tag_map)
        return {"t": "task.add", "cid": cid, "id": task_id, "item": get_task(user_id, task_id)}

    if kind in ("task.update", "task_update", "task.patch"):
        task_id = _opt_int(op.get("id"))
        if task_id is None:
            return {"t": "task.update", "cid": cid, "error": "id required"}
        ok = _update_task(conn, user_id, task_id, data, tag_map)
        return {
            "t": "task.update", "cid": cid, "id": task_id, "ok": ok,
            "item": get_task(user_id, task_id) if ok else None,
        }

    if kind in ("task.delete", "task_delete", "task.del"):
        task_id = _opt_int(op.get("id"))
        if task_id is None:
            return {"t": "task.delete", "cid": cid, "error": "id required"}
        return {
            "t": "task.delete", "cid": cid, "id": task_id,
            "ok": _delete_task(conn, user_id, task_id),
        }

    if kind in ("habit.add", "habit_add"):
        habit_id = _insert_habit(conn, user_id, data)
        return {"t": "habit.add", "cid": cid, "id": habit_id, "item": get_habit(user_id, habit_id)}

    if kind in ("habit.toggle", "habit_toggle"):
        habit_id = _opt_int(op.get("id"))
        day = _opt_day(op.get("day"))
        if habit_id is None or day is None:
            return {"t": "habit.toggle", "cid": cid, "error": "id and day required"}
        toggled = _toggle_habit_day(conn, user_id, habit_id, day)
        return {
            "t": "habit.toggle", "cid": cid, "id": habit_id, "day": day,
            "ok": toggled is not None, "on": bool(toggled),
            "item": get_habit(user_id, habit_id),
        }

    if kind in ("habit.delete", "habit_delete", "habit.del"):
        habit_id = _opt_int(op.get("id"))
        if habit_id is None:
            return {"t": "habit.delete", "cid": cid, "error": "id required"}
        return {
            "t": "habit.delete", "cid": cid, "id": habit_id,
            "ok": _delete_habit(conn, user_id, habit_id),
        }

    if kind in ("diary.save", "diary_save"):
        day = _opt_day(data.get("day") or op.get("day"))
        if day is None:
            return {"t": "diary.save", "cid": cid, "error": "day required"}
        _upsert_diary(conn, user_id, day, data.get("text", ""), data.get("mood"))
        return {"t": "diary.save", "cid": cid, "id": day, "item": get_diary_day(user_id, day)}

    if kind in ("project.add", "project_add"):
        cur = conn.execute(
            "INSERT INTO projects (user_id, name, icon, color, sort) VALUES (?,?,?,?,?)",
            (
                user_id,
                _clip(data.get("name") or "Проект", 60),
                _clip(data.get("icon"), 8),
                _clip(data.get("color") or "#ff0d0d", 32),
                _opt_int(data.get("sort")) or 0,
            ),
        )
        project_id = int(cur.lastrowid)
        return {
            "t": "project.add", "cid": cid, "id": project_id,
            "item": _project_row(conn, user_id, project_id),
        }

    if kind in ("project.delete", "project_delete", "project.del"):
        project_id = _opt_int(op.get("id"))
        ok = False
        if project_id is not None:
            cur = conn.execute(
                "DELETE FROM projects WHERE id = ? AND user_id = ?", (project_id, user_id)
            )
            conn.execute(
                "UPDATE tasks SET project_id = NULL WHERE project_id = ? AND user_id = ?",
                (project_id, user_id),
            )
            ok = cur.rowcount > 0
        return {"t": "project.delete", "cid": cid, "id": project_id, "ok": ok}

    if kind in ("tag.add", "tag_add"):
        name = _clip(data.get("name"), 40).strip() or "тег"
        row = conn.execute(
            "SELECT * FROM tags WHERE user_id = ? AND name = ?", (user_id, name)
        ).fetchone()
        if row is None:
            cur = conn.execute(
                "INSERT INTO tags (user_id, name, color) VALUES (?,?,?)",
                (user_id, name, _clip(data.get("color") or "#b44dff", 32)),
            )
            row = conn.execute(
                "SELECT * FROM tags WHERE id = ?", (int(cur.lastrowid),)
            ).fetchone()
        if cid:
            tag_map[str(cid)] = int(row["id"])
        return {"t": "tag.add", "cid": cid, "id": int(row["id"]), "item": dict(row)}

    if kind in ("pomo.add", "pomo_add"):
        minutes = max(1, min(_opt_int(data.get("minutes")) or 25, 600))
        cur = conn.execute(
            "INSERT INTO pomo (user_id, task_id, started, minutes, completed) VALUES (?,?,?,?,?)",
            (
                user_id,
                _opt_int(data.get("task_id")),
                _now_iso(),
                minutes,
                _bit(data.get("completed", True)),
            ),
        )
        row = conn.execute("SELECT * FROM pomo WHERE id = ?", (cur.lastrowid,)).fetchone()
        return {"t": "pomo.add", "cid": cid, "id": int(cur.lastrowid), "item": dict(row)}

    return {"t": kind or "unknown", "cid": cid, "error": "unknown op"}


def apply_ops(user_id: int, ops: list, profile: dict | None = None) -> dict:
    """Применяет пачку операций в одной транзакции и возвращает результат.

    Один сетевой запрос вместо N — основной способ синхронизации клиента.
    При любой ошибке транзакция откатывается целиком (атомарность пачки).
    """
    conn = get_conn()
    items = [op for op in (ops or []) if isinstance(op, dict)][:MAX_OPS_PER_REQUEST]
    results: list[dict] = []
    tag_map: dict = {}
    try:
        if isinstance(profile, dict) and profile:
            _upsert_profile(conn, user_id, profile)
        # Подготовка: создаём теги заранее, чтобы задачи могли ссылаться
        # на них по временному cid внутри той же пачки.
        for op in items:
            kind = str(op.get("t") or op.get("type") or "").strip().lower()
            if kind not in ("tag.add", "tag_add"):
                continue
            data = op.get("data") if isinstance(op.get("data"), dict) else {}
            name = _clip(data.get("name"), 40).strip() or "тег"
            row = conn.execute(
                "SELECT * FROM tags WHERE user_id = ? AND name = ?", (user_id, name)
            ).fetchone()
            if row is None:
                cur = conn.execute(
                    "INSERT INTO tags (user_id, name, color) VALUES (?,?,?)",
                    (user_id, name, _clip(data.get("color") or "#b44dff", 32)),
                )
                row = conn.execute(
                    "SELECT * FROM tags WHERE id = ?", (int(cur.lastrowid),)
                ).fetchone()
            if op.get("cid"):
                tag_map[str(op["cid"])] = int(row["id"])
        for op in items:
            results.append(_apply_op(conn, user_id, op, tag_map))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "results": results,
        "profile": get_profile(user_id),
        "stats": stats(user_id),
    }






