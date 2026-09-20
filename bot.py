import hashlib
import hmac
import json
import logging
from urllib.parse import parse_qs

from aiohttp import web
from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

import db
from config import BOT_TOKEN, WEB_HOST, WEB_PORT, WEBAPP_URL

logging.basicConfig(level=logging.INFO)

router = Router()
web_app = web.Application()

# Telegram WebApp requires an HTTPS URL (from an env var on Render, etc.)
# and a static WebApp URL is fine as long as it's public https.


def _tg_secret_key() -> bytes:
    return hashlib.sha256(BOT_TOKEN.encode()).digest()


def validate_init_data(init_data: str) -> dict | None:
    """Validate Telegram WebApp initData and return its parsed fields.

    initData is a URL-encoded query string where each value is decoded
    (HTTP-style) before building the data-check-string, exactly as Telegram
    documents. The `user` value is then parsed as JSON.
    """
    if not init_data:
        return None

    parsed = parse_qs(init_data, keep_blank_values=True)
    pairs = {k: v[0] for k, v in parsed.items()}

    hash_received = pairs.pop("hash", "")
    if not hash_received:
        return None

    data_check_string = "\n".join(
        f"{k}={v}" for k, v in sorted(pairs.items())
    )
    secret_key = _tg_secret_key()
    digest = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(digest, hash_received):
        return None

    user_raw = pairs.get("user")
    if user_raw:
        try:
            pairs["user"] = json.loads(user_raw)
        except (ValueError, TypeError):
            pass
    return pairs


def _resolve_user_id(request: web.Request) -> int | None:
    """Try to extract a validated Telegram user id from initData or auth_date."""
    init_data = request.query.get("initData") or ""
    if not init_data:
        # allow ?user_id= for local/dev testing only
        raw = request.query.get("user_id")
        if raw and raw.isdigit():
            return int(raw)
        return None
    data = validate_init_data(init_data)
    if not data:
        return None
    user = data.get("user")
    if isinstance(user, dict) and user.get("id"):
        return int(user["id"])
    return None


def _require_user(request: web.Request) -> int | web.Response:
    uid = _resolve_user_id(request)
    if uid is None:
        return 401
    return uid


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ---------- generic REST helpers ----------


async def api_full(request: web.Request) -> web.Response:
    uid = _resolve_user_id(request)
    if uid is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    return web.json_response(db.full_state(uid))


async def api_profile(request: web.Request) -> web.Response:
    uid = _resolve_user_id(request)
    if uid is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    if request.method == "POST":
        payload = await request.json()
        db.save_profile(uid, payload)
    return web.json_response(db.get_profile(uid))


async def api_projects(request: web.Request) -> web.Response:
    uid = _resolve_user_id(request)
    if uid is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    if request.method == "POST":
        payload = await request.json()
        return web.json_response(db.add_project(uid, payload), status=201)
    return web.json_response(db.list_projects(uid))


async def api_project_item(request: web.Request) -> web.Response:
    uid = _resolve_user_id(request)
    if uid is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    pid = _int_or_none(request.match_info["pid"])
    if request.method == "DELETE":
        ok = db.delete_project(uid, pid)
        return web.json_response({"ok": ok})
    payload = await request.json()
    res = db.update_project(uid, pid, payload)
    if res is None:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response(res)


async def api_tags(request: web.Request) -> web.Response:
    uid = _resolve_user_id(request)
    if uid is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    if request.method == "POST":
        payload = await request.json()
        return web.json_response(db.add_tag(uid, payload.get("name", ""), payload.get("color", "#b44dff")), status=201)
    return web.json_response(db.list_tags(uid))


async def api_tasks(request: web.Request) -> web.Response:
    uid = _resolve_user_id(request)
    if uid is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    if request.method == "POST":
        payload = await request.json()
        return web.json_response(db.add_task(uid, payload), status=201)
    return web.json_response(db.list_tasks(uid))


async def api_task_item(request: web.Request) -> web.Response:
    uid = _resolve_user_id(request)
    if uid is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    tid = _int_or_none(request.match_info["tid"])
    if request.method == "DELETE":
        ok = db.delete_task(uid, tid)
        return web.json_response({"ok": ok})
    payload = await request.json()
    res = db.update_task(uid, tid, payload)
    if res is None:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response(res)


async def api_habits(request: web.Request) -> web.Response:
    uid = _resolve_user_id(request)
    if uid is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    if request.method == "POST":
        payload = await request.json()
        return web.json_response(db.add_habit(uid, payload), status=201)
    return web.json_response(db.list_habits(uid))


async def api_habit_item(request: web.Request) -> web.Response:
    uid = _resolve_user_id(request)
    if uid is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    hid = _int_or_none(request.match_info["hid"])
    if request.method == "DELETE":
        ok = db.delete_habit(uid, hid)
        return web.json_response({"ok": ok})
    payload = await request.json()
    day = payload.get("day")
    if day:
        res = db.toggle_habit_day(uid, hid, day)
        if res is None:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(res)
    return web.json_response({"error": "bad payload"}, status=400)


async def api_diary(request: web.Request) -> web.Response:
    uid = _resolve_user_id(request)
    if uid is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    if request.method == "POST":
        payload = await request.json()
        day = payload.get("day")
        if not day:
            return web.json_response({"error": "day required"}, status=400)
        res = db.save_diary_day(uid, day, payload.get("text", ""), payload.get("mood"))
        return web.json_response(res)
    return web.json_response(db.list_diary(uid))


async def api_pomo(request: web.Request) -> web.Response:
    uid = _resolve_user_id(request)
    if uid is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    payload = await request.json()
    res = db.add_pomo(uid, payload.get("task_id"), int(payload.get("minutes", 25)), payload.get("completed", True))
    return web.json_response(res, status=201)


async def api_stats(request: web.Request) -> web.Response:
    uid = _resolve_user_id(request)
    if uid is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    return web.json_response(db.stats(uid))


async def web_index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse("web/index.html")


async def web_static(request: web.Request) -> web.StreamResponse:
    path = request.match_info["path"]
    return web.FileResponse(f"web/{path}")


def setup_web() -> web.Application:
    r = web_app.router
    r.add_get("/api/full", api_full)
    r.add_get("/api/stats", api_stats)
    r.add_get("/api/profile", api_profile)
    r.add_post("/api/profile", api_profile)
    r.add_get("/api/projects", api_projects)
    r.add_post("/api/projects", api_projects)
    r.add_post("/api/projects/{pid}", api_project_item)
    r.add_delete("/api/projects/{pid}", api_project_item)
    r.add_get("/api/tags", api_tags)
    r.add_post("/api/tags", api_tags)
    r.add_get("/api/tasks", api_tasks)
    r.add_post("/api/tasks", api_tasks)
    r.add_post("/api/tasks/{tid}", api_task_item)
    r.add_delete("/api/tasks/{tid}", api_task_item)
    r.add_get("/api/habits", api_habits)
    r.add_post("/api/habits", api_habits)
    r.add_post("/api/habits/{hid}", api_habit_item)
    r.add_delete("/api/habits/{hid}", api_habit_item)
    r.add_get("/api/diary", api_diary)
    r.add_post("/api/diary", api_diary)
    r.add_post("/api/pomo", api_pomo)
    r.add_get("/", web_index)
    r.add_get("/{path}", web_static)
    return web_app


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="\U0001F4DD Открыть дневник задач",
                    web_app=WebAppInfo(url=WEBAPP_URL),
                )
            ]
        ]
    )
    await message.answer(
        "ДОБРО ПОЖАЛОВАТЬ В ДНЕВНИК.\n\n"
        "Каждая задача — поле боя. Записывай, кастомизируй, побеждай.\n"
        "Жми кнопку, чтобы открыть свой военный журнал.",
        reply_markup=keyboard,
    )


async def main() -> None:
    if BOT_TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE":
        logging.error(
            "Нет токена бота. Вставьте BOT_TOKEN в .env (получите его у @BotFather)."
        )
        return

    db.init_db()

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    app = setup_web()
    runner = web.AppRunner(app)
    await runner.setup()

    port = int(WEB_PORT)
    site = web.TCPSite(runner, WEB_HOST, port)
    await site.start()
    logging.info("WebApp запущен на http://%s:%s", WEB_HOST, port)

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        await runner.cleanup()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
