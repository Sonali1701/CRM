from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
from app.crypto import decrypt_str, encrypt_str
from app.models import MailAccount


GRAPH_BASE = "https://graph.microsoft.com/v1.0"


@dataclass
class GraphToken:
    access_token: str
    refresh_token: str | None
    expires_at: datetime | None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def build_authorize_url(state: str) -> str:
    settings = get_settings()
    if not settings.ms_client_id or not settings.ms_redirect_uri:
        raise ValueError("Microsoft OAuth is not configured")

    tenant = settings.ms_tenant_id or "common"
    scope = "offline_access Mail.Read Mail.Send User.Read"

    params = {
        "client_id": settings.ms_client_id,
        "response_type": "code",
        "redirect_uri": settings.ms_redirect_uri,
        "response_mode": "query",
        "scope": scope,
        "state": state,
    }
    return str(httpx.URL(f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize").copy_add_params(params))


async def exchange_code_for_token(code: str) -> GraphToken:
    settings = get_settings()
    tenant = settings.ms_tenant_id or "common"

    data = {
        "client_id": settings.ms_client_id,
        "client_secret": settings.ms_client_secret,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.ms_redirect_uri,
        "scope": "offline_access Mail.Read Mail.Send User.Read",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        payload = resp.json()

    expires_in = int(payload.get("expires_in") or 0)
    expires_at = _now() + timedelta(seconds=expires_in) if expires_in else None

    return GraphToken(
        access_token=str(payload.get("access_token") or ""),
        refresh_token=payload.get("refresh_token"),
        expires_at=expires_at,
    )


async def refresh_access_token(refresh_token: str) -> GraphToken:
    settings = get_settings()
    tenant = settings.ms_tenant_id or "common"

    data = {
        "client_id": settings.ms_client_id,
        "client_secret": settings.ms_client_secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "redirect_uri": settings.ms_redirect_uri,
        "scope": "offline_access Mail.Read Mail.Send User.Read",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        payload = resp.json()

    expires_in = int(payload.get("expires_in") or 0)
    expires_at = _now() + timedelta(seconds=expires_in) if expires_in else None

    return GraphToken(
        access_token=str(payload.get("access_token") or ""),
        refresh_token=payload.get("refresh_token"),
        expires_at=expires_at,
    )


async def ensure_account_access_token(db: Session, account: MailAccount) -> str:
    token_enc = account.access_token_enc
    refresh_enc = account.refresh_token_enc
    access = decrypt_str(token_enc) if token_enc else ""
    refresh = decrypt_str(refresh_enc) if refresh_enc else ""

    if account.token_expires_at and account.token_expires_at.tzinfo is None:
        account.token_expires_at = account.token_expires_at.replace(tzinfo=timezone.utc)

    if access and account.token_expires_at and account.token_expires_at > _now() + timedelta(seconds=60):
        return access

    if not refresh:
        raise ValueError("Mailbox is not connected")

    new_token = await refresh_access_token(refresh)
    account.access_token_enc = encrypt_str(new_token.access_token)
    if new_token.refresh_token:
        account.refresh_token_enc = encrypt_str(new_token.refresh_token)
    account.token_expires_at = new_token.expires_at
    db.commit()

    return new_token.access_token


async def graph_get(access_token: str, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    url = path if path.startswith("https://") else f"{GRAPH_BASE}{path}"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, params=params, headers={"Authorization": f"Bearer {access_token}"})
        resp.raise_for_status()
        return resp.json()


async def graph_post(access_token: str, path: str, json_body: dict[str, Any]) -> dict[str, Any] | None:
    url = path if path.startswith("https://") else f"{GRAPH_BASE}{path}"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=json_body, headers={"Authorization": f"Bearer {access_token}"})
        if resp.status_code == 204:
            return None
        resp.raise_for_status()
        if not resp.content:
            return None
        return resp.json()


def _encode_state(data: dict[str, Any]) -> str:
    raw = json.dumps(data).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_state(state: str) -> dict[str, Any]:
    raw = base64.urlsafe_b64decode(state.encode("ascii"))
    return json.loads(raw.decode("utf-8"))


def create_oauth_state(user_id: int) -> str:
    settings = get_settings()
    if not settings.mail_sync_key:
        raise ValueError("MAIL_SYNC_KEY must be set")
    payload = {"uid": user_id, "ts": int(_now().timestamp())}
    state_json = _encode_state(payload)
    sig = encrypt_str(state_json)
    return _encode_state({"p": state_json, "s": sig})


def validate_oauth_state(state: str) -> int:
    data = _decode_state(state)
    p = str(data.get("p") or "")
    s = str(data.get("s") or "")
    if not p or not s:
        raise ValueError("Invalid state")
    if decrypt_str(s) != p:
        raise ValueError("Invalid state")
    payload = _decode_state(p)
    return int(payload.get("uid"))
