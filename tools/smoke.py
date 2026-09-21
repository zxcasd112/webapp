"""Живой смоук-тест: поднимает настоящий сервер и проверяет его запросами."""

from __future__ import annotations

import asyncio
import gzip
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
PORT = 8099
BASE = f"http://127.0.0.1:{PORT}"


def start_server() -> subprocess.Popen:
    tmp = tempfile.mkdtemp(prefix="tj-smoke-")
    env = os.environ.copy()
    env.update({
        "BOT_TOKEN": "",           # бот не запустится — проверяем веб-часть
        "DEV_MODE": "1",
        "WEB_PORT": str(PORT),
        "DATA_DIR": tmp,
        "PYTHONUNBUFFERED": "1",
    })
    return subprocess.Popen(
        [sys.executable, str(ROOT / "bot.py")],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def wait_ready(session: aiohttp.ClientSession) -> None:
    for _ in range(60):
        try:
            async with session.get(BASE + "/health") as resp:
                if resp.status == 200:
                    return
        except Exception:
            await asyncio.sleep(0.3)
    raise SystemExit("сервер не поднялся")


async def main() -> None:
    proc = start_server()
    try:
        async with aiohttp.ClientSession() as s:
            await wait_ready(s)

            async with s.get(BASE + "/") as r:
                body = await r.text()
                assert r.status == 200, r.status
                for marker in ["view-temple", "tpl-task", "app.js?v=9", "netBadge",
                               "data-action", "rb3.webp", "projectForm"]:
                    assert marker in body, marker
                assert r.headers["Cache-Control"] == "no-cache"
                assert "frame-ancestors" in r.headers["Content-Security-Policy"]
                print("index OK,", len(body), "байт")

            async with s.get(BASE + "/style.css?v=9",
                             headers={"Accept-Encoding": "gzip"}) as r:
                assert r.status == 200
                assert r.headers.get("Content-Encoding") == "gzip"
                assert "immutable" in r.headers["Cache-Control"]
                raw = await r.read()
                # aiohttp сам прозрачно распаковывает gzip-ответы
                assert b"content-visibility" in raw
                print("style.css gzip OK,", len(raw), "байт после распаковки")

            async with s.get(BASE + "/app.js?v=9") as r:
                assert r.status == 200
                print("app.js OK")

            async with s.get(BASE + "/rb3.webp?v=9") as r:
                assert r.status == 200
                assert r.headers["Content-Type"] == "image/webp"
                print("rb3.webp OK")

            q = "?user_id=999999"
            async with s.get(BASE + "/api/full" + q) as r:
                assert r.status == 200
                full = await r.json()
                etag = r.headers["ETag"]
                print("full OK:", {k: len(v) for k, v in full.items() if isinstance(v, list)})

            async with s.post(BASE + "/api/sync" + q, json={
                "ops": [
                    {"t": "task.add", "cid": "s1", "data": {"title": "Смоук-задача", "due": "2026-09-22"}},
                    {"t": "habit.add", "cid": "h1", "data": {"name": "Смоук", "good": 1}},
                    {"t": "diary.save", "data": {"day": "2026-09-21", "text": "проверка", "mood": 4}},
                    {"t": "pomo.add", "data": {"minutes": 25}},
                    {"t": "project.add", "cid": "p1", "data": {"name": "СмоукПроект", "icon": "☠"}},
                ],
                "profile": {"title": "smoke_lord", "mode": "void"},
            }) as r:
                assert r.status == 200, r.status
                data = await r.json()
                assert len(data["results"]) == 5
                assert data["results"][0]["cid"] == "s1"
                assert data["profile"]["title"] == "smoke_lord"
                tid = data["results"][0]["id"]
                print("sync OK:", [r2["t"] for r2 in data["results"]])

            async with s.get(BASE + "/api/full" + q,
                             headers={"If-None-Match": etag}) as r:
                assert r.status == 200  # etag устарел после sync
                etag2 = r.headers["ETag"]
            async with s.get(BASE + "/api/full" + q,
                             headers={"If-None-Match": etag2}) as r:
                assert r.status == 304, r.status
                print("ETag/304 OK")

            async with s.post(BASE + "/api/sync" + q, json={
                "ops": [{"t": "task.update", "id": tid, "data": {"done": True}},
                        {"t": "task.delete", "id": tid}],
            }) as r:
                data = await r.json()
                assert data["results"][0]["ok"] and data["results"][1]["ok"]
                print("update+delete OK")

            async with s.get(BASE + "/api/nope" + q) as r:
                assert r.status == 404
                assert "error" in await r.json()
                print("404 JSON OK")

            async with s.get(BASE + "/api/full", headers={"Authorization": "tma bad"}) as r:
                assert r.status == 401
                print("401 OK")

        print("SMOKE PASS")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


asyncio.run(main())
