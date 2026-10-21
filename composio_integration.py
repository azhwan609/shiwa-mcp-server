from __future__ import annotations

import json
import os
from typing import Any

import requests
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

router = APIRouter(prefix="/connect", tags=["Composio connections"])

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://cnwowumvrbawuicueskb.supabase.co").rstrip("/")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "sb_publishable_3AKixcRmPJDhexKEd4Qnqg_JY9wQ-hi").strip()
COMPOSIO_API_KEY = os.getenv("COMPOSIO_API_KEY", "").strip()
APP_ORIGIN = os.getenv("APP_ORIGIN", "https://shiwa-creator-hub-49bd8d.netlify.app").rstrip("/")
MCP_PUBLIC_URL = os.getenv("MCP_PUBLIC_URL", "https://deep-shiwa-ai-mcp.onrender.com").rstrip("/")
HTTP_TIMEOUT = (8, 25)
SUPPORTED_TOOLKITS = {"instagram", "telegram"}


class ExecuteRequest(BaseModel):
    toolkit: str
    tool_slug: str
    arguments: dict[str, Any] = Field(default_factory=dict)


def _composio():
    if not COMPOSIO_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="Composio is not configured on the server. Set COMPOSIO_API_KEY in the server environment.",
        )
    try:
        from composio import Composio
        return Composio(api_key=COMPOSIO_API_KEY)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Could not initialize the Composio SDK.") from exc


def _current_user(request: Request) -> dict[str, str]:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="A valid SHIWA user session is required.")
    token = auth.split(" ", 1)[1].strip()
    try:
        response = requests.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={"apikey": SUPABASE_ANON_KEY, "Authorization": f"Bearer {token}"},
            timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="Could not verify the SHIWA user session.") from exc
    if response.status_code != 200:
        raise HTTPException(status_code=401, detail="SHIWA user session is invalid or expired.")
    user = response.json()
    if not user.get("id"):
        raise HTTPException(status_code=401, detail="Supabase did not return a user identity.")
    return {"id": str(user["id"])}


def _toolkit(value: str) -> str:
    slug = value.strip().lower()
    if slug not in SUPPORTED_TOOLKITS:
        raise HTTPException(status_code=404, detail="Unsupported connection provider.")
    return slug


def _session(user_id: str, toolkits: list[str] | None = None):
    # The Supabase auth.users UUID is stable and isolates every user's Composio accounts.
    try:
        return _composio().create(user_id=user_id, toolkits=toolkits or sorted(SUPPORTED_TOOLKITS))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Composio could not create a user-scoped session.") from exc


def search_tools_for_user(user_id: str, provider: str, query: str) -> dict[str, Any]:
    provider = _toolkit(provider)
    session = _session(user_id, [provider])
    try:
        result = session.search(query)
        if hasattr(result, "model_dump"):
            return result.model_dump(mode="json")
        if isinstance(result, dict):
            return result
        return {"result": str(result)}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Composio could not search {provider} tools.") from exc


def execute_tool_for_user(user_id: str, provider: str, tool_slug: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    provider = _toolkit(provider)
    slug = tool_slug.strip().upper()
    expected_prefix = "INSTAGRAM_" if provider == "instagram" else "TELEGRAM_"
    if not slug.startswith(expected_prefix) or len(slug) > 120:
        raise HTTPException(status_code=400, detail="Tool slug does not match the selected provider.")
    session = _session(user_id, [provider])
    try:
        result = session.execute(slug, arguments=arguments or {})
        if hasattr(result, "model_dump"):
            result = result.model_dump(mode="json")
        elif not isinstance(result, (dict, list, str, int, float, bool, type(None))):
            result = str(result)
        return {"ok": True, "provider": provider, "result": result}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Composio could not execute {provider} tool {slug}.") from exc


def _connection_rows(session, toolkit: str) -> list[dict[str, Any]]:
    try:
        details = session.toolkits(toolkits=[toolkit], limit=20)
        items = getattr(details, "items", []) or []
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Could not read this user's Composio connection status.") from exc

    rows: list[dict[str, Any]] = []
    for item in items:
        toolkit_obj = getattr(item, "toolkit", None)
        slug = str(getattr(toolkit_obj, "slug", None) or getattr(item, "slug", None) or toolkit).lower()
        if slug != toolkit:
            continue
        connection = getattr(item, "connection", None)
        connected_account = getattr(connection, "connected_account", None) if connection else None
        state = getattr(connected_account, "state", None) if connected_account else None
        state_data = state.model_dump(mode="json") if hasattr(state, "model_dump") else state
        if not isinstance(state_data, dict):
            state_data = {}
        state_value = state_data.get("val", {})
        if not isinstance(state_value, dict):
            state_value = {}
        account_id = str(getattr(connected_account, "id", "") or "")
        active = bool(getattr(connection, "is_active", False))
        if not account_id or not active:
            continue
        rows.append({
            "id": account_id,
            "toolkit": toolkit,
            "status": str(getattr(connected_account, "status", "") or "ACTIVE"),
            "connected": True,
            "display_name": str(state_value.get("displayName") or state_value.get("display_name") or ""),
        })
    return rows


@router.get("/callback", response_class=HTMLResponse)
async def composio_callback(request: Request):
    provider = request.query_params.get("provider", "").strip().lower()
    status = request.query_params.get("status", "").strip().lower()
    if provider not in SUPPORTED_TOOLKITS:
        provider = ""
    payload = json.dumps({
        "type": "shiwa:composio",
        "provider": provider,
        "ok": status == "success" and bool(provider),
    }).replace("<", "\\u003c")
    target_origin = json.dumps(APP_ORIGIN).replace("<", "\\u003c")
    message = "اتصال با موفقیت انجام شد. می‌توانید این پنجره را ببندید." if status == "success" else "اتصال کامل نشد. پنجره را ببندید و دوباره تلاش کنید."
    safe_message = message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    html = (
        '<!doctype html><html lang="fa" dir="rtl"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>SHIWA · Composio</title><body style="background:#050b12;color:#cdeef5;'
        'font:16px sans-serif;text-align:center;padding:12vh 24px"><h2>SHIWA · اتصال حساب</h2><p>'
        + safe_message
        + '</p><a href="' + (APP_ORIGIN or "/") + '" style="color:#d8b4fe">بازگشت به SHIWA</a>'
        + '<script>(function(){var target=' + target_origin + ';try{if(window.opener){window.opener.postMessage(' + payload + ',target);setTimeout(function(){window.close()},700);return;}}catch(e){}setTimeout(function(){window.location.replace(target)},1600);})();</script></body></html>'
    )
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@router.get("/{toolkit}/start")
async def start_connection(toolkit: str, request: Request):
    provider = _toolkit(toolkit)
    user = _current_user(request)
    session = _session(user["id"], [provider])
    callback_url = f"{MCP_PUBLIC_URL}/connect/callback?provider={provider}"
    try:
        connection_request = session.authorize(provider, callback_url=callback_url)
        redirect_url = getattr(connection_request, "redirect_url", None)
        if not isinstance(redirect_url, str) or not redirect_url.startswith("https://"):
            raise ValueError("Composio did not return a valid HTTPS connect link.")
        return {"authorization_url": redirect_url, "provider": provider}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not start the {provider} connection in Composio.") from exc


@router.get("/{toolkit}/accounts")
async def list_connections(toolkit: str, request: Request):
    provider = _toolkit(toolkit)
    user = _current_user(request)
    session = _session(user["id"], [provider])
    return {"accounts": _connection_rows(session, provider)}


@router.delete("/{toolkit}/accounts/{account_id}")
async def disconnect_account(toolkit: str, account_id: str, request: Request):
    provider = _toolkit(toolkit)
    user = _current_user(request)
    composio = _composio()
    session = _session(user["id"], [provider])
    owned = [row for row in _connection_rows(session, provider) if row["id"] == account_id and row["connected"]]
    if not owned:
        raise HTTPException(status_code=404, detail="That active connection does not belong to this SHIWA user.")
    try:
        result = composio.connected_accounts.revoke(account_id)
        account = getattr(result, "connected_account", None)
        return {"ok": True, "provider": provider, "status": str(getattr(account, "status", "REVOKED"))}
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Composio could not revoke this connection.") from exc


@router.post("/execute")
async def execute_user_tool(payload: ExecuteRequest, request: Request):
    user = _current_user(request)
    provider = _toolkit(payload.toolkit)
    slug = payload.tool_slug.strip().upper()
    expected_prefix = "INSTAGRAM_" if provider == "instagram" else "TELEGRAM_"
    if not slug.startswith(expected_prefix):
        raise HTTPException(status_code=400, detail="Tool slug does not match the selected provider.")
    if len(slug) > 120:
        raise HTTPException(status_code=400, detail="Tool slug is invalid.")
    session = _session(user["id"], [provider])
    try:
        result = session.execute(slug, arguments=payload.arguments)
        if hasattr(result, "model_dump"):
            result = result.model_dump(mode="json")
        elif not isinstance(result, (dict, list, str, int, float, bool, type(None))):
            result = str(result)
        return {"ok": True, "provider": provider, "result": result}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Composio could not execute {provider} tool {slug}.") from exc
