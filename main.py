from __future__ import annotations

import ipaddress
import json
import os
import socket
import tempfile
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request
from instagrapi import Client as InstagramClient
from telegram import Bot
from telegram.error import TelegramError
from composio_integration import (
    _current_user,
    execute_tool_for_user,
    router as composio_router,
    search_tools_for_user,
)

load_dotenv()

SERVICE_NAME = "DEEP-SHIWA-AI"
MAX_MEDIA_BYTES = int(os.getenv("MAX_MEDIA_BYTES", str(25 * 1024 * 1024)))
MCP_API_KEY = os.getenv("MCP_API_KEY", "").strip()
APP_ORIGIN = os.getenv("APP_ORIGIN", "https://shiwa-creator-hub-49bd8d.netlify.app").rstrip("/")
INSTAGRAM_SESSIONID = os.getenv("INSTAGRAM_SESSIONID", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

mcp = FastMCP(SERVICE_NAME)


@mcp.tool
def composio_search_social_tools(toolkit: str, query: str) -> dict[str, Any]:
    """Search Composio's available tools for the signed-in SHIWA user's Instagram or Telegram connection."""
    request = get_http_request()
    user = _current_user(request)
    return search_tools_for_user(user["id"], toolkit, query)


@mcp.tool
def composio_execute_social_tool(
    toolkit: str,
    tool_slug: str,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute one Instagram or Telegram Composio tool using only the caller's own connected account."""
    request = get_http_request()
    user = _current_user(request)
    return execute_tool_for_user(user["id"], toolkit, tool_slug, arguments)


_instagram_client: InstagramClient | None = None
_instagram_lock = threading.RLock()


def _require_mcp_key() -> None:
    if not MCP_API_KEY:
        raise RuntimeError("Server is not configured: MCP_API_KEY is missing.")


def _instagram() -> InstagramClient:
    """Create one Instagram client and authenticate with a sessionid cookie."""
    global _instagram_client
    if not INSTAGRAM_SESSIONID:
        raise RuntimeError("Instagram is not configured: set INSTAGRAM_SESSIONID in Render.")
    with _instagram_lock:
        if _instagram_client is None:
            client = InstagramClient()
            client.delay_range = [1, 3]
            client.login_by_sessionid(INSTAGRAM_SESSIONID)
            _instagram_client = client
        return _instagram_client


def _telegram_token() -> str:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Telegram is not configured: set TELEGRAM_BOT_TOKEN in Render.")
    return TELEGRAM_BOT_TOKEN


def _download_public_media(url: str, allowed_suffixes: tuple[str, ...]) -> Path:
    """Download HTTPS media from a public host; reject private/reserved IPs to reduce SSRF risk."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("media_url must be an HTTPS URL with no embedded credentials.")
    hostname = parsed.hostname
    try:
        addresses = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("media_url hostname could not be resolved.") from exc
    if not addresses:
        raise ValueError("media_url hostname did not resolve.")
    for entry in addresses:
        ip = ipaddress.ip_address(entry[4][0])
        if not ip.is_global:
            raise ValueError("media_url must resolve only to public IP addresses.")
    response = requests.get(
        url,
        stream=True,
        timeout=(8, 30),
        allow_redirects=False,
        headers={"User-Agent": "DEEP-SHIWA-AI/1.0"},
    )
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
    if not (content_type.startswith("image/") or content_type.startswith("video/") or content_type == "application/octet-stream"):
        response.close()
        raise ValueError("The URL must serve an image or video file.")
    suffix = Path(parsed.path).suffix.lower()
    if suffix not in allowed_suffixes:
        suffix = ".mp4" if content_type.startswith("video/") else ".jpg"
    tmp = tempfile.NamedTemporaryFile(prefix="shiwa-media-", suffix=suffix, delete=False)
    size = 0
    try:
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            size += len(chunk)
            if size > MAX_MEDIA_BYTES:
                raise ValueError(f"Media exceeds MAX_MEDIA_BYTES ({MAX_MEDIA_BYTES} bytes).")
            tmp.write(chunk)
        tmp.close()
        if size == 0:
            raise ValueError("The media URL returned an empty file.")
        return Path(tmp.name)
    except Exception:
        tmp.close()
        Path(tmp.name).unlink(missing_ok=True)
        raise
    finally:
        response.close()


# Disabled: this legacy tool used a single global INSTAGRAM_SESSIONID.
def instagram_post_photo(media_url: str, caption: str = "") -> dict[str, Any]:
    """Publish a photo to the authenticated Instagram account from a public HTTPS media URL."""
    path = _download_public_media(media_url, (".jpg", ".jpeg", ".png", ".webp"))
    try:
        with _instagram_lock:
            client = _instagram()
            media = client.photo_upload(str(path), caption)
        return {"ok": True, "media_id": str(media.pk), "code": getattr(media, "code", None)}
    finally:
        path.unlink(missing_ok=True)


# Disabled: this legacy tool used a single global INSTAGRAM_SESSIONID.
def instagram_post_video(media_url: str, caption: str = "") -> dict[str, Any]:
    """Publish a video/reel to Instagram from a public HTTPS media URL."""
    path = _download_public_media(media_url, (".mp4", ".mov", ".m4v"))
    try:
        with _instagram_lock:
            client = _instagram()
            media = client.clip_upload(str(path), caption)
        return {"ok": True, "media_id": str(media.pk), "code": getattr(media, "code", None)}
    finally:
        path.unlink(missing_ok=True)


# Disabled: this legacy tool used a single global INSTAGRAM_SESSIONID.
def instagram_upload_story(media_url: str, media_type: str = "photo") -> dict[str, Any]:
    """Upload an Instagram story. media_type must be 'photo' or 'video'."""
    kind = media_type.strip().lower()
    if kind not in {"photo", "video"}:
        raise ValueError("media_type must be 'photo' or 'video'.")
    suffixes = (".jpg", ".jpeg", ".png", ".webp") if kind == "photo" else (".mp4", ".mov", ".m4v")
    path = _download_public_media(media_url, suffixes)
    try:
        with _instagram_lock:
            client = _instagram()
            media = client.photo_upload_to_story(str(path)) if kind == "photo" else client.video_upload_to_story(str(path))
        return {"ok": True, "media_id": str(media.pk)}
    finally:
        path.unlink(missing_ok=True)


# Disabled: this legacy tool used a single global INSTAGRAM_SESSIONID.
def instagram_send_dm(username: str, message: str) -> dict[str, Any]:
    """Send a direct message to an Instagram username."""
    username = username.strip().lstrip("@")
    if not username or not message.strip():
        raise ValueError("username and message are required.")
    with _instagram_lock:
        client = _instagram()
        user_id = client.user_id_from_username(username)
        result = client.direct_send(message, [user_id])
    return {"ok": True, "thread_or_message_id": str(getattr(result, "id", "")), "recipient": username}


# Disabled: this legacy tool used a single global INSTAGRAM_SESSIONID.
def instagram_recent_comments(limit: int = 5) -> dict[str, Any]:
    """Fetch comments on recent media owned by the authenticated Instagram account."""
    limit = max(1, min(int(limit), 20))
    with _instagram_lock:
        client = _instagram()
        user_id = client.user_id
        medias = client.user_medias(user_id, amount=limit)
        output: list[dict[str, Any]] = []
        for media in medias:
            try:
                comments = client.media_comments(media.pk, amount=20)
            except Exception as exc:
                output.append({"media_id": str(media.pk), "error": str(exc)})
                continue
            for comment in comments:
                output.append({
                    "media_id": str(media.pk),
                    "comment_id": str(getattr(comment, "pk", "")),
                    "username": getattr(getattr(comment, "user", None), "username", None),
                    "text": getattr(comment, "text", ""),
                    "created_at": str(getattr(comment, "created_at_utc", "")),
                })
    return {"ok": True, "comments": output[:100]}


# Disabled: this legacy tool used a single global TELEGRAM_BOT_TOKEN.
async def telegram_send_message(chat_id: str, text: str) -> dict[str, Any]:
    """Send a Telegram message to a chat ID the bot is allowed to message."""
    if not text.strip():
        raise ValueError("text is required.")
    try:
        async with Bot(token=_telegram_token()) as bot:
            msg = await bot.send_message(chat_id=chat_id, text=text)
        return {"ok": True, "chat_id": str(msg.chat_id), "message_id": msg.message_id}
    except TelegramError as exc:
        raise RuntimeError(f"Telegram API error: {exc}") from exc


# Disabled: this legacy tool used a single global TELEGRAM_BOT_TOKEN.
async def telegram_recent_messages(chat_id: str, limit: int = 10) -> dict[str, Any]:
    """Read recent pending Telegram updates for a chat. Bot API cannot fetch arbitrary old chat history."""
    limit = max(1, min(int(limit), 100))
    try:
        async with Bot(token=_telegram_token()) as bot:
            updates = await bot.get_updates(limit=100, timeout=0, allowed_updates=["message", "edited_message", "channel_post", "edited_channel_post"])
        messages: list[dict[str, Any]] = []
        wanted = str(chat_id)
        for update in updates:
            msg = update.effective_message
            if msg is None or str(msg.chat_id) != wanted:
                continue
            messages.append({
                "update_id": update.update_id,
                "message_id": msg.message_id,
                "chat_id": str(msg.chat_id),
                "from_user": getattr(msg.from_user, "username", None) if msg.from_user else None,
                "text": msg.text or msg.caption or "",
                "date": msg.date.isoformat() if msg.date else None,
            })
        return {
            "ok": True,
            "messages": messages[-limit:],
            "note": "This returns pending Bot API updates only, not full chat history. A configured webhook prevents getUpdates from working.",
        }
    except TelegramError as exc:
        raise RuntimeError(f"Telegram API error: {exc}") from exc


@asynccontextmanager
async def lifespan(app: FastAPI):
    # FastMCP's session manager must be started by the host application's lifespan.
    async with mcp_app.lifespan(app):
        yield


mcp_app = mcp.http_app(path="/mcp")


app = FastAPI(
    title=SERVICE_NAME,
    version="1.0.0",
    description="Authenticated MCP bridge for Instagram and Telegram.",
    lifespan=lifespan,
)

# Browser calls from the SHIWA frontend need CORS for the exact configured origin.
# Keep this origin explicit; do not use wildcard origins for an authenticated API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[APP_ORIGIN] if APP_ORIGIN else [],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "apikey"],
)

app.include_router(composio_router)


@app.middleware("http")
async def protect_mcp(request: Request, call_next):
    if request.url.path == "/mcp" or request.url.path.startswith("/mcp/"):
        supplied = request.headers.get("authorization", "")
        expected = f"Bearer {MCP_API_KEY}" if MCP_API_KEY else ""
        # Existing server key remains valid for legacy MCP clients, but Composio
        # tools still require a real Supabase user bearer token in the tool context.
        import hmac
        if MCP_API_KEY and hmac.compare_digest(supplied, expected):
            return await call_next(request)
        try:
            _current_user(request)
        except Exception:
            return JSONResponse(status_code=401, content={"detail": "Use a valid Supabase user access token for user-scoped Composio tools."})
    return await call_next(request)


@app.get("/")
async def root():
    return {
        "service": SERVICE_NAME,
        "status": "running",
        "mcp_endpoint": "/mcp",
        "auth": "MCP_API_KEY for legacy clients; Supabase user bearer token for user-scoped Composio tools",
    }


@app.get("/health")
async def health():
    return {"ok": True, "service": SERVICE_NAME}


# Keep this mount last so the FastMCP route does not shadow the FastAPI health routes.
app.mount("/", mcp_app)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "10000")))