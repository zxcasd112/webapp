"""Интеграционные тесты HTTP-слоя (aiohttp-приложение из bot.py).

Запуск из корня проекта::

    python -m unittest discover -s tests -v

Тесты используют временный каталог для SQLite и подписывают initData тем же
алгоритмом, что и Telegram (HMAC с ключом WebAppData), поэтому проверяют
реальную схему авторизации, а не заглушку.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TOKEN = "123456:TEST_TOKEN_FOR_UNIT_TESTS"
TMP_DIR = tempfile.mkdtemp(prefix="taskjournal-test-")

# Переменные окружения должны быть заданы до импорта config: он читает их
# в момент импорта (и .env не перекрывает уже установленные значения).
os.environ["BOT_TOKEN"] = TOKEN
os.environ["DEV_MODE"] = "0"
os.environ["DATA_DIR"] = TMP_DIR

import config  # noqa: E402

config.BOT_TOKEN = TOKEN
config.DATA_DIR = Path(TMP_DIR)

import bot  # noqa: E402
import db  # noqa: E402

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

_user_seq = iter(range(900000, 900500))


def next_user() -> int:
    return next(_user_seq)


def make_init_data(
    user_id: int,
    token: str = TOKEN,
    auth_date: int | None = None,
    include_signature: bool = False,
) -> str:
    """Собирает подписанный initData так, как это делает Telegram."""
    fields = {
        "auth_date": str(auth_date if auth_date is not None else int(time.time())),
        "query_id": "AAABBBCCC",
        "user": json.dumps(
            {"id": user_id, "first_name": "Тест", "language_code": "ru"},
            ensure_ascii=False,
        ),
    }
    if include_signature:
        fields["signature"] = "ZmFrZS1zaWduYXR1cmU"
    check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": digest})


class ApiTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.client = TestClient(TestServer(bot.create_app()))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()

    # ---- помощники -------------------------------------------------------
    def auth(self, user_id: int) -> dict:
        """Заголовок авторизации — ровно так же делает и клиент WebApp."""
        return {"Authorization": f"tma {make_init_data(user_id)}"}

    async def get_json(self, user_id: int, path: str, headers: dict | None = None):
        response = await self.client.get(
            path, params={"initData": make_init_data(user_id)}, headers=headers
        )
        return response, await response.json()

    async def post_json(self, user_id: int, path: str, payload: dict):
        response = await self.client.post(
            path, params={"initData": make_init_data(user_id)}, json=payload
        )
        return response, await response.json()

    # ---- здоровье и статика ---------------------------------------------
    async def test_health_is_cheap_and_ok(self) -> None:
        response = await self.client.get("/health")
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_index_html_is_served_with_security_headers(self) -> None:
        response = await self.client.get("/")
        body = await response.text()
        self.assertEqual(response.status, 200)
        self.assertIn("view-temple", body)
        self.assertIn("app.js?v=", body)
        self.assertEqual(response.headers["Cache-Control"], "no-cache")
        self.assertIn("frame-ancestors", response.headers["Content-Security-Policy"])
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertTrue(response.headers.get("ETag"))

    async def test_versioned_assets_are_immutable_and_gzipped(self) -> None:
        response = await self.client.get("/style.css?v=9", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(response.status, 200)
        self.assertIn("immutable", response.headers["Cache-Control"])
        self.assertEqual(response.headers.get("Content-Encoding"), "gzip")
        self.assertIn("text/css", response.headers["Content-Type"])

        app_js = await self.client.get("/app.js?v=9")
        self.assertEqual(app_js.status, 200)
        self.assertIn("javascript", app_js.headers["Content-Type"])

    async def test_index_etag_gives_304(self) -> None:
        first = await self.client.get("/")
        etag = first.headers["ETag"]
        second = await self.client.get("/", headers={"If-None-Match": etag})
        self.assertEqual(second.status, 304)
        self.assertEqual(await second.text(), "")

    async def test_unknown_asset_and_traversal_are_not_found(self) -> None:
        self.assertEqual((await self.client.get("/nope.js")).status, 404)
        self.assertEqual((await self.client.get("/../config.py")).status, 404)
        self.assertEqual((await self.client.get("/index.html")).status, 404)


class AuthTestCase(ApiTestCase):
    async def test_missing_init_data_is_unauthorized(self) -> None:
        response = await self.client.get("/api/full")
        self.assertEqual(response.status, 401)
        self.assertEqual((await response.json())["error"], "unauthorized")

    async def test_tampered_hash_is_rejected(self) -> None:
        data = make_init_data(next_user())
        head, _, tail = data.partition("hash=")
        broken = head + "hash=" + ("0" if tail[0] != "0" else "1") + tail[1:]
        response = await self.client.get("/api/full", params={"initData": broken})
        self.assertEqual(response.status, 401)

    async def test_expired_init_data_is_rejected(self) -> None:
        old = int(time.time()) - config.INIT_DATA_MAX_AGE - 60
        response = await self.client.get(
            "/api/full", params={"initData": make_init_data(next_user(), auth_date=old)}
        )
        self.assertEqual(response.status, 401)

    async def test_fresh_init_data_is_accepted(self) -> None:
        response = await self.client.get(
            "/api/full", params={"initData": make_init_data(next_user())}
        )
        self.assertEqual(response.status, 200)

    async def test_signature_field_participates_in_hash(self) -> None:
        """Bot API 7.10+ присылает signature — он входит в data-check-string."""
        data = make_init_data(next_user(), include_signature=True)
        response = await self.client.get("/api/full", params={"initData": data})
        self.assertEqual(response.status, 200)

    async def test_authorization_header_is_supported(self) -> None:
        data = make_init_data(next_user())
        response = await self.client.get(
            "/api/full", headers={"Authorization": f"tma {data}"}
        )
        self.assertEqual(response.status, 200)

    async def test_dev_mode_switch_controls_user_id_access(self) -> None:
        user = next_user()
        self.assertFalse(config.DEV_MODE, "в тестах DEV_MODE должен быть выключен")
        self.assertEqual((await self.client.get(f"/api/full?user_id={user}")).status, 401)
        config.DEV_MODE = True
        try:
            self.assertEqual((await self.client.get(f"/api/full?user_id={user}")).status, 200)
        finally:
            config.DEV_MODE = False

    async def test_garbage_init_data_is_rejected(self) -> None:
        self.assertIsNone(bot.parse_init_data("auth_date=1&hash=deadbeef"))
        self.assertIsNone(bot.parse_init_data(""))
        response = await self.client.get("/api/full?initData=broken")
        self.assertEqual(response.status, 401)


class SyncTestCase(ApiTestCase):
    async def test_task_lifecycle(self) -> None:
        user = next_user()
        response, payload = await self.post_json(
            user,
            "/api/sync",
            {
                "ops": [
                    {
                        "t": "task.add",
                        "cid": "c1",
                        "data": {
                            "title": "Обряд",
                            "due": "2026-09-21",
                            "scheduled": "09:00",
                            "important": 1,
                        },
                    }
                ]
            },
        )
        self.assertEqual(response.status, 200)
        result = payload["results"][0]
        self.assertEqual(result["cid"], "c1")
        self.assertEqual(result["item"]["title"], "Обряд")
        self.assertEqual(result["item"]["important"], 1)
        self.assertEqual(payload["stats"]["open_total"], 1)
        task_id = result["id"]

        _, payload = await self.post_json(
            user,
            "/api/sync",
            {"ops": [{"t": "task.update", "id": task_id, "data": {"done": True}}]},
        )
        self.assertTrue(payload["results"][0]["ok"])
        self.assertEqual(payload["results"][0]["item"]["done"], 1)
        self.assertEqual(payload["stats"]["done_total"], 1)

        _, payload = await self.post_json(
            user, "/api/sync", {"ops": [{"t": "task.delete", "id": task_id}]}
        )
        self.assertTrue(payload["results"][0]["ok"])
        _, full = await self.get_json(user, "/api/full")
        self.assertEqual(full["tasks"], [])

    async def test_unknown_op_does_not_break_batch(self) -> None:
        user = next_user()
        _, payload = await self.post_json(
            user,
            "/api/sync",
            {
                "ops": [
                    {"t": "nonsense"},
                    {"t": "task.add", "data": {"title": "Живая задача"}},
                ]
            },
        )
        self.assertIn("error", payload["results"][0])
        self.assertEqual(payload["results"][1]["item"]["title"], "Живая задача")

    async def test_profile_is_saved_with_batch(self) -> None:
        user = next_user()
        _, payload = await self.post_json(
            user,
            "/api/sync",
            {"ops": [], "profile": {"title": "новый титул", "mode": "acid"}},
        )
        self.assertEqual(payload["profile"]["title"], "новый титул")
        self.assertEqual(payload["profile"]["mode"], "acid")
        _, full = await self.get_json(user, "/api/full")
        self.assertEqual(full["profile"]["title"], "новый титул")


class SyncHabitDiaryTestCase(ApiTestCase):
    async def test_habit_toggle_adds_and_removes_day(self) -> None:
        user = next_user()
        _, payload = await self.post_json(
            user,
            "/api/sync",
            {"ops": [{"t": "habit.add", "data": {"name": "Бег", "good": 1}}]},
        )
        habit_id = payload["results"][0]["id"]
        self.assertEqual(payload["stats"]["habit_count"], 1)

        _, payload = await self.post_json(
            user,
            "/api/sync",
            {"ops": [{"t": "habit.toggle", "id": habit_id, "day": "2026-09-21"}]},
        )
        result = payload["results"][0]
        self.assertTrue(result["on"])
        self.assertEqual(result["item"]["days"], ["2026-09-21"])

        _, payload = await self.post_json(
            user,
            "/api/sync",
            {"ops": [{"t": "habit.toggle", "id": habit_id, "day": "2026-09-21"}]},
        )
        self.assertFalse(payload["results"][0]["on"])
        self.assertEqual(payload["results"][0]["item"]["days"], [])

    async def test_diary_keeps_single_entry_per_day(self) -> None:
        user = next_user()
        await self.post_json(
            user, "/api/diary", {"day": "2026-09-21", "text": "первая", "mood": 2}
        )
        await self.post_json(
            user, "/api/diary", {"day": "2026-09-21", "text": "вторая", "mood": 4}
        )
        _, entries = await self.get_json(user, "/api/diary")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["text"], "вторая")
        self.assertEqual(entries[0]["mood"], 4)

    async def test_pomodoro_minutes_accumulate(self) -> None:
        user = next_user()
        for _ in range(2):
            response, _ = await self.post_json(
                user, "/api/pomo", {"minutes": 25, "completed": True}
            )
            self.assertEqual(response.status, 201)
        _, stats = await self.get_json(user, "/api/stats")
        self.assertEqual(stats["pomo_minutes_today"], 50)
        self.assertEqual(stats["pomo_count_today"], 2)


class IsolationAndCacheTestCase(ApiTestCase):
    async def test_users_are_isolated(self) -> None:
        owner, stranger = next_user(), next_user()
        _, payload = await self.post_json(
            owner, "/api/sync", {"ops": [{"t": "task.add", "data": {"title": "Секрет"}}]}
        )
        task_id = payload["results"][0]["id"]

        _, full = await self.get_json(stranger, "/api/full")
        self.assertEqual(full["tasks"], [])
        _, result = await self.post_json(
            stranger,
            "/api/sync",
            {"ops": [{"t": "task.update", "id": task_id, "data": {"title": "Взлом"}}]},
        )
        self.assertFalse(result["results"][0]["ok"])
        response, _ = await self.post_json(
            stranger, f"/api/tasks/{task_id}", {"title": "Взлом"}
        )
        self.assertEqual(response.status, 404)

        _, full = await self.get_json(owner, "/api/full")
        self.assertEqual(full["tasks"][0]["title"], "Секрет")

    async def test_full_returns_304_when_nothing_changed(self) -> None:
        user = next_user()
        first = await self.client.get(
            "/api/full", params={"initData": make_init_data(user)}
        )
        etag = first.headers["ETag"]
        self.assertTrue(etag.startswith('W/"'))
        second = await self.client.get(
            "/api/full",
            params={"initData": make_init_data(user)},
            headers={"If-None-Match": etag},
        )
        self.assertEqual(second.status, 304)

        await self.post_json(user, "/api/tasks", {"title": "Изменение"})
        third = await self.client.get(
            "/api/full",
            params={"initData": make_init_data(user)},
            headers={"If-None-Match": etag},
        )
        self.assertEqual(third.status, 200)

    async def test_full_is_gzipped_for_large_state(self) -> None:
        user = next_user()
        for index in range(60):
            await self.post_json(
                user,
                "/api/tasks",
                {"title": f"Задача номер {index} с достаточно длинным описанием"},
            )
        response = await self.client.get(
            "/api/full",
            params={"initData": make_init_data(user)},
            headers={"Accept-Encoding": "gzip"},
        )
        self.assertEqual(response.headers.get("Content-Encoding"), "gzip")
        payload = await response.json()
        self.assertEqual(len(payload["tasks"]), 60)


class RestTestCase(ApiTestCase):
    async def test_rest_task_flow(self) -> None:
        user = next_user()
        response, task = await self.post_json(
            user, "/api/tasks", {"title": "REST", "due": "2026-09-21"}
        )
        self.assertEqual(response.status, 201)
        _, tasks = await self.get_json(user, "/api/tasks")
        self.assertEqual(len(tasks), 1)

        response, updated = await self.post_json(
            user, f"/api/tasks/{task['id']}", {"done": True}
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(updated["done"], 1)

        response = await self.client.delete(
            f"/api/tasks/{task['id']}", headers=self.auth(user)
        )
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())["ok"])

    async def test_rest_happy_path_for_habits_and_diary(self) -> None:
        user = next_user()
        response, habit = await self.post_json(user, "/api/habits", {"name": "Вода", "good": 1})
        self.assertEqual(response.status, 201)
        response, updated = await self.post_json(
            user, f"/api/habits/{habit['id']}", {"day": "2026-09-20"}
        )
        self.assertEqual(updated["days"], ["2026-09-20"])
        response = await self.client.delete(
            f"/api/habits/{habit['id']}", headers=self.auth(user)
        )
        self.assertTrue((await response.json())["ok"])

        response, _ = await self.post_json(
            user, "/api/diary", {"day": "2026-09-20", "text": "ок"}
        )
        self.assertEqual(response.status, 200)

    async def test_bad_requests_return_json_errors(self) -> None:
        user = next_user()
        response, payload = await self.post_json(user, "/api/diary", {"text": "без дня"})
        self.assertEqual(response.status, 400)
        self.assertEqual(payload["error"], "day required")

        response, _ = await self.post_json(user, "/api/sync", {"ops": "not-a-list"})
        self.assertEqual(response.status, 400)

        response = await self.client.post(
            "/api/tasks/abc", headers=self.auth(user), json={"title": "x"}
        )
        self.assertEqual(response.status, 400)

        response, _ = await self.post_json(user, "/api/projects/999999", {"name": "нет такого"})
        self.assertEqual(response.status, 404)

        response = await self.client.get("/api/does-not-exist")
        self.assertEqual(response.status, 404)
        self.assertIn("error", await response.json())

    async def test_projects_and_tags_roundtrip(self) -> None:
        user = next_user()
        response, project = await self.post_json(
            user, "/api/projects", {"name": "Культ", "icon": "†"}
        )
        self.assertEqual(response.status, 201)
        _, projects = await self.get_json(user, "/api/projects")
        self.assertEqual(projects[0]["name"], "Культ")

        response, tag = await self.post_json(user, "/api/tags", {"name": "ритуал"})
        self.assertEqual(response.status, 201)
        response, task = await self.post_json(
            user,
            "/api/tasks",
            {"title": "С тегом", "tags": [tag["id"]], "project_id": project["id"]},
        )
        self.assertEqual([t["name"] for t in task["tags"]], ["ритуал"])

        response = await self.client.delete(
            f"/api/projects/{project['id']}", headers=self.auth(user)
        )
        self.assertTrue((await response.json())["ok"])
        _, tasks = await self.get_json(user, "/api/tasks")
        self.assertIsNone(tasks[0]["project_id"])

    async def test_batch_resolves_tag_cids_inside_one_request(self) -> None:
        """Задача в пачке может ссылаться на тег, созданный той же пачкой."""
        user = next_user()
        _, payload = await self.post_json(
            user,
            "/api/sync",
            {
                "ops": [
                    {"t": "tag.add", "cid": "tagTmp1", "data": {"name": "ритм", "color": "#27c8ff"}},
                    {"t": "task.add", "cid": "taskTmp1", "data": {"title": "С тегом из пачки", "tags": ["tagTmp1"]}},
                ]
            },
        )
        self.assertEqual(payload["results"][1]["t"], "task.add")
        item = payload["results"][1]["item"]
        self.assertEqual([t["name"] for t in item["tags"]], ["ритм"])
        _, tasks = await self.get_json(user, "/api/tasks")
        self.assertEqual(tasks[0]["tags"][0]["name"], "ритм")

    async def test_profile_validation_clips_long_values(self) -> None:
        user = next_user()
        response, profile = await self.post_json(
            user, "/api/profile", {"title": "x" * 500, "accent": "#ff0000"}
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(len(profile["title"]), 60)
        self.assertEqual(profile["accent"], "#ff0000")


if __name__ == "__main__":
    unittest.main()


def tearDownModule() -> None:
    db.dispose()
    shutil.rmtree(TMP_DIR, ignore_errors=True)
