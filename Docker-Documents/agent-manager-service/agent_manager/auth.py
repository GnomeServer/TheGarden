from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert

from .database import get_session
from .models import UserRecord
from .settings import settings

SESSION_COOKIE = "dark_factory_session"
OAUTH_STATE_COOKIE = "dark_factory_oauth_state"
SESSION_TTL_SECONDS = 12 * 60 * 60
bearer = HTTPBearer(auto_error=False)


def _encode(payload: dict[str, Any]) -> str:
    body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).rstrip(b"=")
    signature = hmac.new(settings.signing_key, body, hashlib.sha256).digest()
    return f"{body.decode()}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"


def _decode(token: str) -> dict[str, Any] | None:
    try:
        body_text, signature_text = token.split(".", 1)
        body = body_text.encode("ascii")
        supplied = base64.urlsafe_b64decode(signature_text + "=" * (-len(signature_text) % 4))
        expected = hmac.new(settings.signing_key, body, hashlib.sha256).digest()
        if not hmac.compare_digest(supplied, expected):
            return None
        decoded = base64.urlsafe_b64decode(body_text + "=" * (-len(body_text) % 4))
        payload = json.loads(decoded)
        if not isinstance(payload, dict) or int(payload.get("exp", 0)) <= int(time.time()):
            return None
        return payload
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def create_session(user_id: str) -> tuple[str, str]:
    csrf = secrets.token_urlsafe(24)
    return _encode({"uid": user_id, "csrf": csrf, "exp": int(time.time()) + SESSION_TTL_SECONDS}), csrf


def create_oauth_state() -> str:
    return _encode({"nonce": secrets.token_urlsafe(24), "exp": int(time.time()) + 600})


def validate_oauth_state(value: str) -> bool:
    payload = _decode(value)
    return bool(payload and payload.get("nonce"))


async def _api_admin(session: AsyncSession) -> UserRecord:
    result = await session.execute(
        select(UserRecord).where(UserRecord.forgejo_login == "api-admin")
    )
    user = result.scalar_one_or_none()
    if user is None:
        user = UserRecord(
            id=str(uuid.uuid4()),
            forgejo_login="api-admin",
            display_name="API administrator",
            role="admin",
            active=True,
            last_login_at=datetime.now(timezone.utc),
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return user


async def api_token_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    session: AsyncSession = Depends(get_session),
) -> UserRecord:
    expected = settings.api_token.encode("utf-8")
    supplied = credentials.credentials.encode("utf-8") if credentials is not None else b""
    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or not expected
        or not hmac.compare_digest(supplied, expected)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await _api_admin(session)


async def session_user(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> UserRecord:
    token = request.cookies.get(SESSION_COOKIE, "")
    payload = _decode(token) if token else None
    if payload is None or not isinstance(payload.get("uid"), str):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Forgejo dashboard session required",
        )
    user = await session.get(UserRecord, payload["uid"])
    if (
        user is None
        or not user.active
        or user.role == "service"
        or user.forgejo_login.lower() not in settings.dashboard_members
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session is no longer valid")
    request.state.auth_method = "session"
    request.state.csrf = payload.get("csrf")
    return user


async def require_session_csrf(
    request: Request,
    user: UserRecord = Depends(session_user),
) -> UserRecord:
    supplied = request.headers.get("X-CSRF-Token", "")
    expected = str(getattr(request.state, "csrf", ""))
    if not expected or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")
    return user



async def run_client(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    session: AsyncSession = Depends(get_session),
) -> UserRecord:
    """Accept the WebUI key only on explicitly opted-in run endpoints.

    This service identity is not a Forgejo user. Chat-supplied identity is
    recorded as metadata, never trusted to grant human dashboard permissions.
    """
    if credentials is not None and credentials.scheme.lower() == "bearer":
        expected = settings.openwebui_token.encode("utf-8")
        if expected and hmac.compare_digest(credentials.credentials.encode("utf-8"), expected):
            request.state.auth_method = "open-webui"
            login = "service:open-webui"
            user = await session.scalar(select(UserRecord).where(UserRecord.forgejo_login == login))
            if user is None:
                # Concurrent first submissions must share a single audit actor.
                await session.execute(insert(UserRecord).values(
                    id=str(uuid.uuid4()), forgejo_login=login,
                    display_name="Open WebUI service", role="service", active=True,
                ).on_conflict_do_nothing(index_elements=["forgejo_login"]))
                await session.commit()
                user = await session.scalar(select(UserRecord).where(UserRecord.forgejo_login == login))
            if user is None or not user.active:
                raise HTTPException(status_code=401, detail="Service access disabled")
            return user
    api_token = settings.api_token.encode("utf-8")
    supplied = credentials.credentials.encode("utf-8") if credentials is not None else b""
    if (
        credentials is not None
        and credentials.scheme.lower() == "bearer"
        and api_token
        and hmac.compare_digest(supplied, api_token)
    ):
        request.state.auth_method = "bearer"
        return await _api_admin(session)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="API bearer token required",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def require_run_submission(
    request: Request, actor: UserRecord = Depends(run_client),
) -> UserRecord:
    if getattr(request.state, "auth_method", "") not in {"open-webui", "bearer"}:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Machine API token required")
    if getattr(request.state, "auth_method", "") != "open-webui" and actor.role != "admin":
        raise HTTPException(status_code=403, detail="Run submission is not permitted")
    return actor


def session_payload(request: Request) -> dict[str, Any] | None:
    token = request.cookies.get(SESSION_COOKIE, "")
    return _decode(token) if token else None
