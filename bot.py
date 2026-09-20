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


async def api_get_state(request: web.Request) -> web.Response:
    user_id = _resolve_user_id(request)
    if user_id is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    state = db.get_state(user_id)
    return web.json_response(state)


async def api_save_state(request: web.Request) -> web.Response:
    user_id = _resolve_user_id(request)
    if user_id is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"error": "bad json"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"error": "bad payload"}, status=400)
    db.save_state(user_id, payload)
    return web.json_response({"ok": True})


async def web_index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse("web/index.html")


async def web_static(request: web.Request) -> web.StreamResponse:
    path = request.match_info["path"]
    return web.FileResponse(f"web/{path}")


def setup_web() -> web.Application:
    web_app.router.add_get("/api/state", api_get_state)
    web_app.router.add_post("/api/state", api_save_state)
    web_app.router.add_get("/", web_index)
    web_app.router.add_get("/{path}", web_static)
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
