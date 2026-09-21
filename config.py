"""Конфигурация приложения.

Все значения читаются из переменных окружения (файл .env подхватывается
автоматически). Модуль не имеет побочных эффектов, кроме чтения окружения,
поэтому его безопасно импортировать в тестах.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"

load_dotenv(BASE_DIR / ".env")

TOKEN_PLACEHOLDER = "PASTE_YOUR_BOT_TOKEN_HERE"


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "да"}


def _int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    return default


# --- Telegram -------------------------------------------------------------
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip() or TOKEN_PLACEHOLDER
BOT_NAME = (os.getenv("BOT_NAME") or "slaughter_lord").strip()

# Публичный HTTPS-адрес WebApp. На Render приходит RENDER_EXTERNAL_URL.
_render_url = (os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
WEBAPP_URL = (
    (os.getenv("WEBAPP_URL") or "").strip().rstrip("/")
    or _render_url
    or "http://localhost:8080"
)

# initData живёт ограниченное время: Telegram рекомендует проверять auth_date.
# 7 суток — компромисс, чтобы WebApp не «выкидывал» пользователя из сессии.
INIT_DATA_MAX_AGE = _int("INIT_DATA_MAX_AGE", 60 * 60 * 24 * 7)

# --- HTTP -----------------------------------------------------------------
WEB_HOST = (os.getenv("WEB_HOST") or "0.0.0.0").strip()
WEB_PORT = _int("WEB_PORT", 8080)

# --- Данные ---------------------------------------------------------------
# На бесплатном Render диск эфемерный: чтобы не потерять базу, подключите
# постоянный диск и укажите DATA_DIR=/var/data
DATA_DIR = Path(os.getenv("DATA_DIR") or (BASE_DIR / "data")).expanduser()

# Демо-режим открывает доступ по ?user_id= и нужен только для локальной
# разработки и тестов. В продакшене должен быть выключен.
DEV_MODE = _flag("DEV_MODE", False)

# --- Кеширование статики --------------------------------------------------
# Меняйте при релизе: браузеры получат новые файлы, не теряя кеш старых.
ASSET_VERSION = (os.getenv("ASSET_VERSION") or "9").strip()

# Необязательный self-ping: контейнер сам дёргает /health, чтобы хостинг
# не усыплял сервис (0 = выключено).
SELF_PING_SECONDS = _int("SELF_PING_SECONDS", 0)
