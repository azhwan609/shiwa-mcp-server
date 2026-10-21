from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import requests
from cryptography.fernet import Fernet
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

router = APIRouter(prefix="/connect/instagram", tags=["Instagram OAuth"])
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
INSTAGRAM_APP_ID = os.getenv("INSTAGRAM_APP_ID", "").strip()
INSTAGRAM_APP_SECRET = os.getenv("INSTAGRAM_APP_SECRET", "").strip()
INSTAGRAM_REDIRECT_URI = os.getenv("INSTAGRAM_REDIRECT_URI", "").strip()
STATE_SECRET = os.getenv("STATE_SECRET", "").strip()
TOKEN_ENCRYPTION_KEY = os.getenv("TOKEN_ENCRYPTION_KEY", "").strip()
FRONTEND_ORIGIN = os.getenv("APP_ORIGIN", "").rstrip("/")

SCOPES = [
    "instagram_business_basic",
    "instagram_business_content_publish",
    "instagram_business_manage_comments",
    "instagram_business_manage_messages",
]
STATE_TTL_SECONDS = 600
HTTP_TIMEOUT = (8, 25)


def _configuration() -> None:
    missing = [
        name for name, value in {
            "SUPABASE_URL": SUPABASE_URL,
            "SUPABASE_ANON_KEY": SUPABASE_ANON_KEY,
            "SUPABASE_SERVICE_ROLE_KEY": SUPABASE_SERVICE_ROLE_KEY,
            "INSTAGRAM_APP_ID": INSTAGRAM_APP_ID,
            "INSTAGRAM_APP_SECRET": INSTAGRAM_APP_SECRET,
            "INSTAGRAM_REDIRECT_URI": INSTAGRAM_REDIRECT_URI,
            "STATE_SECRET": STATE_SECRET,
            "TOKEN_ENCRYPTION_KEY": TOKEN_ENCRYPTION_KEY,
        }.items() if not value
    ]
    if missing:
        raise HTTPException(status_code=503, detail="Instagram OAuth is not configured. Missing server settings: " + ", ".join(missing))


def _fernet() -> Fernet:
    try:
        key = TOKEN_ENCRYPTION_KEY.encode("ascii")
        # Fernet keys must be URL-safe base64 encoding of exactly 32 bytes.
        raw = base64.urlsafe_b64decode(key)
        if len(raw) != 32:
            raise ValueError("wrong key length")
        return Fernet(key)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="TOKEN_ENCRYPTION_KEY must be a Fernet-compatible URL-safe base64 key.") from exc


def _state_signature(payload: str) -> str:
    return hmac.new(STATE_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _make_state(user_id: str) -> str:
    nonce = secrets.token_urlsafe(32)
    issued_at = int(time.time())
    from datetime import datetime, timedelta, timezone
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=STATE_TTL_SECONDS)).isoformat()
    _db_request(
        "POST", "instagram_oauth_states",
        body=[{"nonce": nonce, "user_id": user_id, "expires_at": expires_at}],
        prefer="return=minimal",
    )
    payload = base64.urlsafe_b64encode(json.dumps({
        "sub": user_id, "iat": issued_at, "nonce": nonce
    }, separators=(",", ":")).encode()).decode().rstrip("=")
    return payload + "." + _state_signature(payload)


def _consume_state(data: dict[str, Any]) -> None:
    """Atomically consume a nonce so an OAuth callback cannot be replayed."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    response = _db_request(
        "PATCH", "instagram_oauth_states",
        params={
            "nonce": "eq." + str(data.get("nonce", "")),
            "user_id": "eq." + str(data.get("sub", "")),
            "used_at": "is.null",
            "expires_at": "gt." + now,
        },
        body={"used_at": now},
        prefer="return=representation",
    )
    rows = response.json() if response.content else []
    if not rows:
        raise HTTPException(status_code=400, detail="Instagram OAuth state was already used or expired. Start the connection again.")


def _read_state(state: str) -> dict[str, Any]:
    try:
        payload, signature = state.rsplit(".", 1)
        if not hmac.compare_digest(signature, _state_signature(payload)):
            raise ValueError("signature")
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        issued = int(data["iat"])
        if not data.get("sub") or abs(int(time.time()) - issued) > STATE_TTL_SECONDS:
            raise ValueError("expired")
        return data
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid or expired Instagram OAuth state. Start the connection again.") from exc


def _supabase_headers(user_token: str | None = None) -> dict[str, str]:
    key = SUPABASE_SERVICE_ROLE_KEY
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if user_token:
        headers["X-User-Authorization"] = f"Bearer {user_token}"
    return headers


def _current_user(request: Request) -> dict[str, Any]:
    _configuration()
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="A valid Supabase user access token is required.")
    user_token = auth.split(" ", 1)[1].strip()
    try:
        response = requests.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={"apikey": SUPABASE_ANON_KEY, "Authorization": f"Bearer {user_token}"},
            timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="Could not verify the Supabase user session.") from exc
    if response.status_code != 200:
        raise HTTPException(status_code=401, detail="Supabase user session is invalid or expired.")
    user = response.json()
    if not user.get("id"):
        raise HTTPException(status_code=401, detail="Supabase did not return a user identity.")
    return {"id": str(user["id"]), "access_token": user_token}


def _db_request(method: str, table: str, *, params: dict[str, str] | None = None, body: Any = None, prefer: str | None = None) -> requests.Response:
    headers = _supabase_headers()
    if prefer:
        headers["Prefer"] = prefer
    try:
        response = requests.request(
            method, f"{SUPABASE_URL}/rest/v1/{table}",
            headers=headers, params=params, json=body, timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="Supabase database request failed.") from exc
    if response.status_code >= 400:
        # Never return database response bodies; they may contain implementation details.
        raise HTTPException(status_code=502, detail=f"Supabase rejected the {table} operation (HTTP {response.status_code}).")
    return response


def _exchange_code(code: str) -> dict[str, Any]:
    try:
        short_response = requests.post(
            "https://api.instagram.com/oauth/access_token",
            data={
                "client_id": INSTAGRAM_APP_ID,
                "client_secret": INSTAGRAM_APP_SECRET,
                "grant_type": "authorization_code",
                "redirect_uri": INSTAGRAM_REDIRECT_URI,
                "code": code,
            },
            timeout=HTTP_TIMEOUT,
        )
        short_response.raise_for_status()
        short_data = short_response.json()
        short_token = short_data["access_token"]
        long_response = requests.get(
            "https://graph.instagram.com/access_token",
            params={
                "grant_type": "ig_exchange_token",
                "client_secret": INSTAGRAM_APP_SECRET,
                "access_token": short_token,
            },
            timeout=HTTP_TIMEOUT,
        )
        long_response.raise_for_status()
        long_data = long_response.json()
        return {
            "access_token": long_data["access_token"],
            "expires_in": int(long_data.get("expires_in", 0)),
            "user_id": str(short_data.get("user_id") or long_data.get("user_id") or ""),
        }
    except (requests.RequestException, KeyError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="Instagram token exchange failed. Verify the Meta app configuration and redirect URI.") from exc


def _profile(access_token: str) -> dict[str, Any]:
    try:
        response = requests.get(
            "https://graph.instagram.com/me",
            params={"fields": "user_id,username", "access_token": access_token},
            timeout=HTTP_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("user_id") and not data.get("id"):
            raise ValueError("Missing Instagram user id")
        return {"id": str(data.get("user_id") or data.get("id")), "username": str(data.get("username") or "")}
    except (requests.RequestException, ValueError) as exc:
        raise HTTPException(status_code=502, detail="Could not retrieve the connected Instagram profile.") from exc


@router.get("/start")
def start_instagram_connect(request: Request):
    """Begin Instagram Login for the currently authenticated SHIWA user."""
    _configuration()
    user = _current_user(request)
    state = _make_state(user["id"])
    query = urlencode({
        "client_id": INSTAGRAM_APP_ID,
        "redirect_uri": INSTAGRAM_REDIRECT_URI,
        "response_type": "code",
        "scope": ",".join(SCOPES),
        "state": state,
        "enable_fb_login": "0",
        "force_authentication": "1",
    })
    return {"authorization_url": f"https://www.instagram.com/oauth/authorize?{query}", "expires_in": STATE_TTL_SECONDS}


@router.get("/callback")
def instagram_callback(code: str | None = None, state: str | None = None, error: str | None = None, error_description: str | None = None):
    """OAuth callback. The redirect URI must exactly match the Meta app setting."""
    _configuration()
    if error:
        raise HTTPException(status_code=400, detail="Instagram authorization was declined: " + (error_description or error)[:200])
    if not code or not state:
        raise HTTPException(status_code=400, detail="Instagram did not return both code and state.")
    state_data = _read_state(state)
    _consume_state(state_data)
    token_data = _exchange_code(code)
    profile = _profile(token_data["access_token"])
    expires_at = None
    if token_data["expires_in"] > 0:
        from datetime import datetime, timedelta, timezone
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=token_data["expires_in"])).isoformat()
    encrypted = _fernet().encrypt(token_data["access_token"].encode()).decode()
    # Each Instagram account is stored as its own row and tied to the initiating Supabase user.
    _db_request(
        "POST", "integrations",
        params={"on_conflict": "user_id,provider,external_id"},
        body=[{
            "user_id": state_data["sub"],
            "provider": "instagram",
            "external_id": profile["id"],
            "external_username": profile["username"],
            "encrypted_token": encrypted,
            "token_expires_at": expires_at,
            "scopes": SCOPES,
            "status": "active",
        }],
        prefer="resolution=merge-duplicates,return=minimal",
    )
    result = {
        "ok": True,
        "provider": "instagram",
        "username": profile["username"],
        "message": "Instagram connected. Return to SHIWA to use the account.",
    }
    if FRONTEND_ORIGIN:
        # Keep the OAuth result free of access tokens and other secrets.
        return RedirectResponse(FRONTEND_ORIGIN + "/?instagram_connected=1", status_code=303)
    return result


@router.get("/accounts")
def list_instagram_accounts(request: Request):
    """List only Instagram accounts owned by the authenticated SHIWA user."""
    user = _current_user(request)
    response = _db_request(
        "GET", "integrations",
        params={
            "select": "id,external_id,external_username,scopes,status,token_expires_at,created_at",
            "user_id": f"eq.{user['id']}",
            "provider": "eq.instagram",
            "order": "created_at.desc",
        },
    )
    return {"ok": True, "accounts": response.json()}


@router.delete("/accounts/{integration_id}")
def disconnect_instagram_account(integration_id: str, request: Request):
    """Revoke an account link for the current user without exposing its token."""
    user = _current_user(request)
    response = _db_request(
        "PATCH", "integrations",
        params={"id": f"eq.{integration_id}", "user_id": f"eq.{user['id']}", "provider": "eq.instagram"},
        body={"status": "revoked", "encrypted_token": ""},
        prefer="return=representation",
    )
    rows = response.json() if response.content else []
    if not rows:
        raise HTTPException(status_code=404, detail="Instagram connection not found for this user.")
    return {"ok": True, "disconnected": True}
