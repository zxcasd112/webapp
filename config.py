import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "PASTE_YOUR_BOT_TOKEN_HERE")

WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = os.getenv("WEB_PORT", "8080")

# On Render the public HTTPS URL is injected as RENDER_EXTERNAL_URL.
_render_url = os.getenv("RENDER_EXTERNAL_URL", "").strip("/")
WEBAPP_URL = os.getenv("WEBAPP_URL") or (_render_url or f"http://localhost:{WEB_PORT}")
