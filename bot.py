"""Telegram-бот и HTTP-сервер WebApp.

Один процесс, один event loop:

* aiogram (long polling) принимает команды и отдаёт кнопку WebApp;
* aiohttp отдаёт статику и JSON API.

Такой процесс не блокирует друг друга: пока бот ждёт апдейты, сервер
обслуживает запросы приложения, поэтому интерфейс не «подвисает».

Схема авторизации — подписанный Telegram ``initData`` (Mini App):
``secret = HMAC_SHA256(key="WebAppData", msg=bot_token)``, из
data-check-string исключается только поле ``hash``.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import hmac
import html
import json
import logging
import mimetypes
import os
import time
from pathlib import Path
from urllib.parse import parse_qs

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

import config
import db

log = logging.getLogger("taskjournal")

BASE_DIR = config.BASE_DIR
WEB_DIR = config.WEB_DIR

# Статику держим в памяти: файлы маленькие, а отдача превращается в memcpy.
# Ключ — путь, значение — (mtime, raw, gzip, etag).
_ASSET_CACHE: dict[str, tuple[float, bytes, bytes, str]] = {}

# MIME для нескольких типов, которые mimetypes может не знать на slim-образах.
_EXTRA_TYPES = {
    ".webp": "image/webp",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}

MAX_BODY = 512 * 1024
GZIP_MIN = 700


# --------------------------------------------------------------------------
# initData
# --------------------------------------------------------------------------
def _secret_key() -> bytes:
    """Ключ подписи Mini App: HMAC(bot_token, key="WebAppData")."""
    return hmac.new(
        b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256
    ).digest()


def parse_init_data(raw: str, max_age: int | None = None) -> dict | None:
    """Проверяет подпись initData и возвращает разобранные поля.

    Возвращает ``None``, если подпись неверна, данных нет или они просрочены.
    ``user`` разворачивается из JSON в словарь.
    """
    if not raw:
        return None
    try:
        parsed = parse_qs(raw, keep_blank_values=True, strict_parsing=False)
    except ValueError:
        return None
    pairs = {key: values[0] for key, values in parsed.items() if values}

    received_hash = pairs.pop("hash", "")
    if not received_hash:
        return None

    # data-check-string: все поля кроме hash, отсортированные по ключу
    data_check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    digest = hmac.new(_secret_key(), data_check_string.encode(), hashlib.sha256)
    if not hmac.compare_digest(digest.hexdigest(), received_hash):
        return None

    horizon = config.INIT_DATA_MAX_AGE if max_age is None else max_age
    auth_date = pairs.get("auth_date", "")
    if horizon and str(auth_date).isdigit():
        if time.time() - int(auth_date) > horizon:
            return None

    user_raw = pairs.get("user")
    if user_raw:
        try:
            pairs["user"] = json.loads(user_raw)
        except (TypeError, ValueError):
            pairs["user"] = None
    return pairs


def resolve_user_id(request: web.Request) -> int | None:
    """Идентификатор пользователя из initData (заголовок или query).

    Закрытый по умолчанию: ``?user_id=`` работает только при ``DEV_MODE=1``,
    чтобы никто не читал чужие записи.
    """
    header = request.headers.get("Authorization", "")
    raw = ""
    if header[:4].lower() == "tma ":
        raw = header[4:].strip()
    if not raw:
        raw = request.query.get("initData", "")
    if raw:
        data = parse_init_data(raw)
        if data:
            user = data.get("user")
            if isinstance(user, dict):
                user_id = user.get("id")
                if isinstance(user_id, int) or str(user_id or "").isdigit():
                    return int(user_id)
            return None
        return None
    if config.DEV_MODE:
        candidate = request.query.get("user_id", "")
        if candidate.lstrip("-").isdigit():
            return int(candidate)
    return None


def accepts_gzip(request: web.Request) -> bool:
    return "gzip" in request.headers.get("Accept-Encoding", "").lower()


async def read_json(request: web.Request) -> dict:
    """Тело запроса как объект. Пустое/битое тело — пустой словарь."""
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


# --------------------------------------------------------------------------
# ответы
# --------------------------------------------------------------------------
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

# Мини-приложение живёт в iframe web.telegram.org, поэтому frame-ancestors
# ограничен Telegram'ом, а остальные источники — только собственный домен.
CSP = (
    "default-src 'self'; "
    "script-src 'self' https://telegram.org; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com data:; "
    "img-src 'self' data: blob:; "
    "connect-src 'self'; "
    "frame-ancestors 'self' https://telegram.org https://*.telegram.org; "
    "base-uri 'none'; form-action 'none'"
)


def json_response(
    request: web.Request,
    payload: dict | list,
    status: int = 200,
    etag: str | None = None,
    cache_control: str = "no-store",
) -> web.Response:
    """Компактный JSON с gzip и корректными заголовками.

    ``ensure_ascii=False`` + отсутствие пробелов дают до 40 % экономии
    трафика на кириллице, а gzip сжимает ответы ещё сильнее.
    """
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    headers = {
        **SECURITY_HEADERS,
        "Cache-Control": cache_control,
        "Content-Type": "application/json; charset=utf-8",
    }
    if etag:
        headers["ETag"] = etag
    if len(body) >= GZIP_MIN and accepts_gzip(request):
        body = gzip.compress(body, 6)
        headers["Content-Encoding"] = "gzip"
        headers["Vary"] = "Accept-Encoding"
    return web.Response(body=body, status=status, headers=headers)


def error_response(request: web.Request, status: int, message: str) -> web.Response:
    return json_response(request, {"error": message}, status=status)


def _etag_matches(request: web.Request, etag: str) -> bool:
    header = request.headers.get("If-None-Match", "")
    if not header:
        return False
    return any(candidate.strip() in {etag, "*"} for candidate in header.split(","))


def _content_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in _EXTRA_TYPES:
        return _EXTRA_TYPES[ext]
    guess, _ = mimetypes.guess_type(path.name)
    return guess or "application/octet-stream"


def load_asset(path: Path) -> tuple[bytes, bytes, str] | None:
    """Файл из памяти: (raw, gzip, etag). Кеш живёт до изменения mtime."""
    key = str(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    cached = _ASSET_CACHE.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1], cached[2], cached[3]
    data = path.read_bytes()
    gz = gzip.compress(data, 6) if len(data) >= GZIP_MIN else b""
    etag = '"%s"' % hashlib.sha1(data).hexdigest()[:16]
    _ASSET_CACHE[key] = (mtime, data, gz, etag)
    return data, gz, etag


async def _serve_file(
    request: web.Request,
    path: Path,
    cache_control: str,
) -> web.Response:
    loaded = load_asset(path)
    if loaded is None:
        raise web.HTTPNotFound()
    data, gz, etag = loaded
    headers = {
        **SECURITY_HEADERS,
        "Content-Type": _content_type(path),
        "Cache-Control": cache_control,
        "ETag": etag,
    }
    if path.name == "index.html":
        headers["Content-Security-Policy"] = CSP
    if _etag_matches(request, etag):
        return web.Response(status=304, headers=headers)
    body = data
    if gz and accepts_gzip(request):
        body = gz
        headers["Content-Encoding"] = "gzip"
        headers["Vary"] = "Accept-Encoding"
    return web.Response(body=body, headers=headers)


async def handle_index(request: web.Request) -> web.Response:
    # index.html всегда ревалидируется по ETag (мгновенный 304 после первого раза).
    return await _serve_file(request, WEB_DIR / "index.html", "no-cache")


async def handle_asset(request: web.Request) -> web.Response:
    relative = request.match_info.get("path", "")
    target = (WEB_DIR / relative).resolve()
    root = WEB_DIR.resolve()
    if not target.is_file() or root not in target.parents:
        raise web.HTTPNotFound()
    if target.name == "index.html":
        raise web.HTTPNotFound()
    # Файлы с версией в query (?v=9) можно кешировать навсегда.
    versioned = bool(request.query.get("v"))
    return await _serve_file(
        request, target, "public, max-age=31536000, immutable" if versioned else "public, max-age=3600"
    )


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------
def _int_or_none(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def authed(handler):
    """Проверяет initData и передаёт user_id вторым аргументом обработчика."""

    async def wrapper(request: web.Request) -> web.Response:
        user_id = resolve_user_id(request)
        if user_id is None:
            return error_response(request, 401, "unauthorized")
        return await handler(request, user_id)

    wrapper.__name__ = getattr(handler, "__name__", "handler")
    return wrapper


async def api_not_found(request: web.Request) -> web.Response:
    """Любой неизвестный /api/* отвечает JSON, а не HTML-страницей."""
    return error_response(request, 404, "not found")


async def api_health(request: web.Request) -> web.Response:
    """Дешёвый эндпоинт для keep-alive: не читает базу и почти ничего не делает."""
    return json_response(request, {"ok": True, "now": int(time.time())})


@authed
async def api_full(request: web.Request, user_id: int) -> web.Response:
    version = db.state_version(user_id)
    etag = 'W/"%s"' % hashlib.sha1(version.encode()).hexdigest()[:20]
    if _etag_matches(request, etag):
        return web.Response(
            status=304,
            headers={**SECURITY_HEADERS, "ETag": etag, "Cache-Control": "no-cache"},
        )
    return json_response(
        request, db.full_state(user_id), etag=etag, cache_control="no-cache"
    )


@authed
async def api_sync(request: web.Request, user_id: int) -> web.Response:
    """Пачка изменений одним запросом — основной путь синхронизации клиента."""
    payload = await read_json(request)
    ops = payload.get("ops")
    if ops is None:
        ops = []
    if not isinstance(ops, list):
        return error_response(request, 400, "ops must be a list")
    profile = payload.get("profile")
    result = db.apply_ops(user_id, ops, profile if isinstance(profile, dict) else None)
    return json_response(request, result)


@authed
async def api_profile(request: web.Request, user_id: int) -> web.Response:
    if request.method == "POST":
        return json_response(request, db.save_profile(user_id, await read_json(request)))
    return json_response(request, db.get_profile(user_id), cache_control="no-cache")


@authed
async def api_stats(request: web.Request, user_id: int) -> web.Response:
    return json_response(request, db.stats(user_id), cache_control="no-cache")


@authed
async def api_projects(request: web.Request, user_id: int) -> web.Response:
    if request.method == "POST":
        return json_response(
            request, db.add_project(user_id, await read_json(request)), status=201
        )
    return json_response(request, db.list_projects(user_id), cache_control="no-cache")


@authed
async def api_project_item(request: web.Request, user_id: int) -> web.Response:
    project_id = _int_or_none(request.match_info.get("pid"))
    if project_id is None:
        return error_response(request, 400, "bad project id")
    if request.method == "DELETE":
        return json_response(request, {"ok": db.delete_project(user_id, project_id)})
    item = db.update_project(user_id, project_id, await read_json(request))
    if item is None:
        return error_response(request, 404, "not found")
    return json_response(request, item)


@authed
async def api_tags(request: web.Request, user_id: int) -> web.Response:
    if request.method == "POST":
        payload = await read_json(request)
        return json_response(
            request,
            db.add_tag(user_id, payload.get("name", ""), payload.get("color", "#b44dff")),
            status=201,
        )
    return json_response(request, db.list_tags(user_id), cache_control="no-cache")


@authed
async def api_tasks(request: web.Request, user_id: int) -> web.Response:
    if request.method == "POST":
        return json_response(
            request, db.add_task(user_id, await read_json(request)), status=201
        )
    return json_response(request, db.list_tasks(user_id), cache_control="no-cache")


@authed
async def api_task_item(request: web.Request, user_id: int) -> web.Response:
    task_id = _int_or_none(request.match_info.get("tid"))
    if task_id is None:
        return error_response(request, 400, "bad task id")
    if request.method == "DELETE":
        return json_response(request, {"ok": db.delete_task(user_id, task_id)})
    item = db.update_task(user_id, task_id, await read_json(request))
    if item is None:
        return error_response(request, 404, "not found")
    return json_response(request, item)


@authed
async def api_habits(request: web.Request, user_id: int) -> web.Response:
    if request.method == "POST":
        return json_response(
            request, db.add_habit(user_id, await read_json(request)), status=201
        )
    return json_response(request, db.list_habits(user_id), cache_control="no-cache")


@authed
async def api_habit_item(request: web.Request, user_id: int) -> web.Response:
    habit_id = _int_or_none(request.match_info.get("hid"))
    if habit_id is None:
        return error_response(request, 400, "bad habit id")
    if request.method == "DELETE":
        return json_response(request, {"ok": db.delete_habit(user_id, habit_id)})
    payload = await read_json(request)
    day = payload.get("day")
    if not day:
        return error_response(request, 400, "day required")
    item = db.toggle_habit_day(user_id, habit_id, day)
    if item is None:
        return error_response(request, 404, "not found")
    return json_response(request, item)


@authed
async def api_diary(request: web.Request, user_id: int) -> web.Response:
    if request.method == "POST":
        payload = await read_json(request)
        day = payload.get("day")
        if not day:
            return error_response(request, 400, "day required")
        item = db.save_diary_day(
            user_id, day, payload.get("text", ""), payload.get("mood")
        )
        if item is None:
            return error_response(request, 400, "bad day")
        return json_response(request, item)
    return json_response(request, db.list_diary(user_id), cache_control="no-cache")


@authed
async def api_pomo(request: web.Request, user_id: int) -> web.Response:
    payload = await read_json(request)
    minutes = _int_or_none(payload.get("minutes")) or 25
    item = db.add_pomo(
        user_id, payload.get("task_id"), minutes, payload.get("completed", True)
    )
    return json_response(request, item, status=201)


# --------------------------------------------------------------------------
# приложение
# --------------------------------------------------------------------------
@web.middleware
async def errors_mw(request: web.Request, handler):
    """Ни один сбой не должен ломать приложение: отвечаем JSON-ошибкой."""
    try:
        return await handler(request)
    except web.HTTPException as exc:
        if request.path.startswith("/api/"):
            return error_response(request, exc.status, exc.reason or "error")
        raise
    except Exception:
        log.exception("Необработанная ошибка: %s %s", request.method, request.path)
        return error_response(request, 500, "internal error")


async def on_startup(_: web.Application) -> None:
    db.init_db()
    log.info("База готова: %s", db.db_path())


async def on_cleanup(_: web.Application) -> None:
    db.dispose()


def create_app() -> web.Application:
    """Собирает aiohttp-приложение (используется и в тестах)."""
    app = web.Application(client_max_size=MAX_BODY, middlewares=[errors_mw])
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    router = app.router
    router.add_get("/health", api_health)
    router.add_get("/api/health", api_health)
    router.add_get("/api/full", api_full)
    router.add_post("/api/sync", api_sync)
    router.add_get("/api/profile", api_profile)
    router.add_post("/api/profile", api_profile)
    router.add_get("/api/stats", api_stats)
    router.add_get("/api/projects", api_projects)
    router.add_post("/api/projects", api_projects)
    router.add_post("/api/projects/{pid}", api_project_item)
    router.add_delete("/api/projects/{pid}", api_project_item)
    router.add_get("/api/tags", api_tags)
    router.add_post("/api/tags", api_tags)
    router.add_get("/api/tasks", api_tasks)
    router.add_post("/api/tasks", api_tasks)
    router.add_post("/api/tasks/{tid}", api_task_item)
    router.add_delete("/api/tasks/{tid}", api_task_item)
    router.add_get("/api/habits", api_habits)
    router.add_post("/api/habits", api_habits)
    router.add_post("/api/habits/{hid}", api_habit_item)
    router.add_delete("/api/habits/{hid}", api_habit_item)
    router.add_get("/api/diary", api_diary)
    router.add_post("/api/diary", api_diary)
    router.add_post("/api/pomo", api_pomo)
    router.add_route("*", "/api/{tail:.*}", api_not_found)
    router.add_get("/", handle_index)
    router.add_get("/{path}", handle_asset)
    return app


# --------------------------------------------------------------------------
# бот
# --------------------------------------------------------------------------
bot_router = Router()

WELCOME = (
    "<b>{title}</b>\n\n"
    "Дневник, привычки, статистика и фокус-таймер — всё внутри Telegram.\n"
    "Открывай кнопкой ниже, данные сохраняются на сервере."
)

HINT = "Открой дневник кнопкой ниже 👇"


def start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="\U0001F4DD Открыть дневник",
                    web_app=WebAppInfo(url=config.WEBAPP_URL),
                )
            ]
        ]
    )


@bot_router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    first_name = (message.from_user.first_name or "") if message.from_user else ""
    greeting = f"Привет, {html.escape(first_name)}.\n\n" if first_name else ""
    await message.answer(
        greeting + WELCOME.format(title=html.escape(config.BOT_NAME)),
        reply_markup=start_keyboard(),
    )


@bot_router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HINT, reply_markup=start_keyboard())


@bot_router.message(F.text & ~F.text.startswith("/"))
async def any_text(message: Message) -> None:
    """Любое сообщение = быстрый доступ к приложению."""
    await message.answer(HINT, reply_markup=start_keyboard())


# --------------------------------------------------------------------------
# запуск
# --------------------------------------------------------------------------
def setup_logging() -> None:
    logging.basicConfig(
        level=(os.getenv("LOG_LEVEL") or "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Иначе каждая картинка и каждый апдейт засоряют лог хостинга.
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


async def keepalive_loop(seconds: int) -> None:
    """Периодически дёргает свой /health, чтобы хостинг не усыплял контейнер."""
    url = config.WEBAPP_URL.rstrip("/") + "/health"
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    log.debug("keepalive %s -> %s", url, resp.status)
        except Exception as exc:  # сеть может быть недоступна — это не ошибка
            log.debug("keepalive недоступен: %s", exc)
        await asyncio.sleep(seconds)


async def main() -> None:
    setup_logging()

    app = create_app()
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, config.WEB_HOST, config.WEB_PORT)
    await site.start()
    log.info(
        "WebApp слушает http://%s:%s (публичный адрес: %s)",
        config.WEB_HOST,
        config.WEB_PORT,
        config.WEBAPP_URL,
    )

    tasks: list[asyncio.Task] = []
    if config.SELF_PING_SECONDS > 0:
        tasks.append(asyncio.create_task(keepalive_loop(config.SELF_PING_SECONDS)))

    bot: Bot | None = None
    try:
        if config.BOT_TOKEN == config.TOKEN_PLACEHOLDER:
            log.warning(
                "BOT_TOKEN не задан — работает только веб-часть (DEV_MODE=%s)",
                config.DEV_MODE,
            )
            await asyncio.Event().wait()
            return

        bot = Bot(
            token=config.BOT_TOKEN,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        dispatcher = Dispatcher()
        dispatcher.include_router(bot_router)
        log.info("Запускаю long polling для @%s", config.BOT_NAME)
        await dispatcher.start_polling(
            bot, allowed_updates=["message"], drop_pending_updates=True
        )
    except TelegramUnauthorizedError:
        log.error(
            "Telegram отклонил токен: приложение продолжит работать, "
            "но кнопка в боте не появится. Проверьте BOT_TOKEN."
        )
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        raise
    finally:
        for task in tasks:
            task.cancel()
        if bot is not None:
            await bot.session.close()
        await runner.cleanup()
        db.dispose()
        log.info("Остановлено")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass





