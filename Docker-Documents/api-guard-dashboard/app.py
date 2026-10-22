from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore", hide_input_in_errors=True)

    state_db: str = Field(default="/data/api-guard.sqlite3", validation_alias="API_GUARD_STATE_DB")
    public_url: str = Field(default="https://infra-lab-services.tail494f6d.ts.net", validation_alias="API_GUARD_PUBLIC_URL")
    session_secret: str = Field(default="", validation_alias="API_GUARD_SESSION_SECRET")
    session_secure: bool = Field(default=True, validation_alias="API_GUARD_SESSION_SECURE")
    admin_logins_csv: str = Field(default="", validation_alias="API_GUARD_ADMINS")
    forgejo_api_url: str = Field(default="http://forgejo:3000/api/v1", validation_alias="API_GUARD_FORGEJO_API_URL")
    forgejo_public_url: str = Field(default="https://infra-lab-services.tail494f6d.ts.net/forgejo", validation_alias="API_GUARD_FORGEJO_PUBLIC_URL")
    forgejo_client_id: str = Field(default="", validation_alias="API_GUARD_FORGEJO_CLIENT_ID")
    forgejo_client_secret: str = Field(default="", validation_alias="API_GUARD_FORGEJO_CLIENT_SECRET")
    litellm_base_url: str = Field(default="http://litellm:4000", validation_alias="API_GUARD_LITELLM_BASE_URL")
    litellm_master_key: str = Field(default="", validation_alias="API_GUARD_LITELLM_MASTER_KEY")
    exposure_webhook_secret: str = Field(default="", validation_alias="API_GUARD_EXPOSURE_WEBHOOK_SECRET")
    annual_budget_usd: float = Field(default=25000.0, gt=0, validation_alias="API_GUARD_ANNUAL_BUDGET_USD")
    rotation_days: int = Field(default=14, ge=7, le=30, validation_alias="API_GUARD_ROTATION_DAYS")
    jev_model: str = Field(default="jev", validation_alias="API_GUARD_JEV_MODEL")
    jev_api_key: str = Field(default="", validation_alias="API_GUARD_JEV_API_KEY")

    @property
    def admin_logins(self) -> set[str]:
        return {login.strip().casefold() for login in self.admin_logins_csv.split(",") if login.strip()}

    @property
    def oauth_configured(self) -> bool:
        return bool(self.forgejo_client_id and self.forgejo_client_secret and self.session_secret and self.admin_logins)


settings = Settings()
SESSION_COOKIE = "api_guard_session"
OAUTH_STATE_COOKIE = "api_guard_oauth_state"
KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$")
MAX_SPEND_LOG_PAGES = 10


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@contextmanager
def db_connect():
    path = Path(settings.state_db)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS exposure_events ("
            "event_id TEXT PRIMARY KEY, key_id TEXT, key_alias TEXT NOT NULL, "
            "source TEXT NOT NULL, reason TEXT NOT NULL, occurred_at TEXT NOT NULL, "
            "received_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'flagged')"
        )
        connection.execute("CREATE INDEX IF NOT EXISTS ix_exposure_status_received ON exposure_events(status, received_at)")
        yield connection
        connection.commit()
    finally:
        connection.close()


def encode_signed(payload: dict[str, Any]) -> str:
    if not settings.session_secret:
        raise HTTPException(status_code=503, detail="Dashboard sign-in is not configured")
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = hmac.new(settings.session_secret.encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{base64.urlsafe_b64encode(signature).decode().rstrip('=')}"


def decode_signed(token: str) -> dict[str, Any] | None:
    try:
        body, supplied = token.split(".", 1)
        signature = base64.urlsafe_b64decode(supplied + "=" * (-len(supplied) % 4))
        expected = hmac.new(settings.session_secret.encode(), body.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if not isinstance(payload, dict) or int(payload.get("exp", 0)) <= int(time.time()):
            return None
        return payload
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def session_from_request(request: Request) -> dict[str, Any] | None:
    token = request.cookies.get(SESSION_COOKIE, "")
    payload = decode_signed(token) if token else None
    login = payload.get("login") if payload else None
    if not isinstance(login, str) or login.casefold() not in settings.admin_logins:
        return None
    return payload


def require_admin(request: Request) -> dict[str, Any]:
    user = session_from_request(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Administrator sign-in required")
    return user


def require_csrf(request: Request, user: dict[str, Any]) -> None:
    supplied = request.headers.get("X-CSRF-Token", "")
    expected = user.get("csrf", "")
    if not isinstance(expected, str) or not expected or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


class ExposureEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=128)
    key_id: str | None = Field(default=None, max_length=128)
    key_alias: str = Field(min_length=1, max_length=100)
    source: str = Field(default="omp", min_length=1, max_length=32)
    reason: Literal["configured_credential_echo", "credential_pattern_in_output"]
    occurred_at: datetime | None = None

    @field_validator("key_id")
    @classmethod
    def validate_key_id(cls, value: str | None) -> str | None:
        if value is not None and not KEY_ID_PATTERN.fullmatch(value):
            raise ValueError("key_id must be a safe LiteLLM key identifier")
        return value

    @field_validator("key_alias", "source")
    @classmethod
    def validate_labels(cls, value: str) -> str:
        if not value.isprintable() or any(character in value for character in "\r\n"):
            raise ValueError("label contains unsupported characters")
        return value.strip()


class KeyCreate(BaseModel):
    alias: str = Field(min_length=1, max_length=100)
    models: list[str] = Field(min_length=1, max_length=10)
    max_budget_usd: float = Field(gt=0)
    budget_duration: Literal["1d", "7d", "30d"] = "30d"

    @field_validator("alias")
    @classmethod
    def validate_alias(cls, value: str) -> str:
        value = value.strip()
        if not ALIAS_PATTERN.fullmatch(value):
            raise ValueError("alias must use letters, numbers, dot, underscore, colon, or hyphen")
        return value

    @field_validator("models")
    @classmethod
    def validate_models(cls, value: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(model.strip() for model in value if model.strip()))
        if not cleaned or any(len(model) > 128 or not model.replace("-", "").replace("_", "").replace(".", "").isalnum() for model in cleaned):
            raise ValueError("models must be simple configured model aliases")
        return cleaned


class KeyReference(BaseModel):
    key_id: str = Field(min_length=1, max_length=128)

    @field_validator("key_id")
    @classmethod
    def validate_key_id(cls, value: str) -> str:
        if not KEY_ID_PATTERN.fullmatch(value):
            raise ValueError("Invalid LiteLLM key identifier")
        return value


class ResolveEvent(BaseModel):
    event_id: str = Field(min_length=1, max_length=128)


async def litellm_request(method: str, path: str, *, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None) -> Any:
    if not settings.litellm_master_key:
        raise HTTPException(status_code=503, detail="LiteLLM administration is not configured")
    headers = {"Authorization": f"Bearer {settings.litellm_master_key}", "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.request(
                method,
                f"{settings.litellm_base_url.rstrip('/')}{path}",
                headers=headers,
                params=params,
                json=body,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="LiteLLM is unavailable") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="LiteLLM rejected the dashboard request")
    try:
        return response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="LiteLLM returned an invalid response") from exc


def key_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        rows = payload.get("keys", payload.get("data", []))
    else:
        rows = payload
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def key_id_for(row: dict[str, Any]) -> str | None:
    value = row.get("token_id") or row.get("token_hash") or row.get("token")
    if not isinstance(value, str) or not KEY_ID_PATTERN.fullmatch(value) or value.startswith("sk-"):
        return None
    return value


def safe_key_out(row: dict[str, Any], now: datetime) -> dict[str, Any]:
    created_at = row.get("created_at")
    created = created_at if isinstance(created_at, str) else None
    age_days: int | None = None
    if created:
        try:
            parsed = datetime.fromisoformat(created.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            age_days = max(0, (now - parsed.astimezone(timezone.utc)).days)
        except ValueError:
            created = None
    alias = row.get("key_alias") or row.get("key_name") or "Unnamed key"
    if not isinstance(alias, str):
        alias = "Unnamed key"
    models = row.get("models") if isinstance(row.get("models"), list) else []
    return {
        "key_id": key_id_for(row),
        "alias": alias[:100],
        "models": [model for model in models if isinstance(model, str)][:10],
        "spend_usd": _number(row.get("spend")),
        "max_budget_usd": _optional_number(row.get("max_budget")),
        "budget_duration": row.get("budget_duration") if isinstance(row.get("budget_duration"), str) else None,
        "created_at": created,
        "age_days": age_days,
        "expires": row.get("expires") if isinstance(row.get("expires"), str) else None,
        "blocked": bool(row.get("blocked")),
        "team_id": row.get("team_id") if isinstance(row.get("team_id"), str) else None,
    }


def _number(value: Any) -> float:
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _optional_number(value: Any) -> float | None:
    if value is None:
        return None
    parsed = _number(value)
    return parsed if parsed > 0 else None


async def fetch_spend_logs(start: datetime, end: datetime) -> tuple[list[dict[str, Any]], bool]:
    rows: list[dict[str, Any]] = []
    page = 1
    total_pages = 1
    truncated = False
    while page <= MAX_SPEND_LOG_PAGES:
        payload = await litellm_request(
            "GET",
            "/spend/logs/v2",
            params={
                "start_date": start.strftime("%Y-%m-%d %H:%M:%S"),
                "end_date": end.strftime("%Y-%m-%d %H:%M:%S"),
                "page": page,
                "page_size": 1000,
                "sort_by": "startTime",
                "sort_order": "desc",
                "exclude_internal_health_checks": "true",
            },
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise HTTPException(status_code=502, detail="LiteLLM returned invalid spend logs")
        rows.extend(row for row in payload["data"] if isinstance(row, dict))
        total_pages = max(1, int(_number(payload.get("total_pages"))))
        truncated |= bool(payload.get("total_is_capped")) or total_pages > MAX_SPEND_LOG_PAGES
        if page >= total_pages or page >= MAX_SPEND_LOG_PAGES:
            break
        page += 1
    return rows, truncated


def log_started_at(row: dict[str, Any]) -> datetime | None:
    value = row.get("startTime")
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def aggregate_spend_logs(
    rows: list[dict[str, Any]],
    rolling_start: datetime,
    month_start: datetime,
    now: datetime,
) -> tuple[dict[str, float | int], dict[str, float | int], dict[str, dict[str, float | int]]]:
    rolling = {"spend_usd": 0.0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    monthly = {"spend_usd": 0.0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    by_key: dict[str, dict[str, float | int]] = {}
    for row in rows:
        started = log_started_at(row)
        if started is None or started < rolling_start or started > now:
            continue
        spend = _number(row.get("spend"))
        prompt = max(0, int(_number(row.get("prompt_tokens"))))
        completion = max(0, int(_number(row.get("completion_tokens"))))
        total = max(0, int(_number(row.get("total_tokens")))) or prompt + completion
        rolling["spend_usd"] += spend
        rolling["prompt_tokens"] += prompt
        rolling["completion_tokens"] += completion
        rolling["total_tokens"] += total
        if started < month_start:
            continue
        monthly["spend_usd"] += spend
        monthly["prompt_tokens"] += prompt
        monthly["completion_tokens"] += completion
        monthly["total_tokens"] += total
        key_id = row.get("api_key")
        if isinstance(key_id, str) and KEY_ID_PATTERN.fullmatch(key_id) and not key_id.startswith("sk-"):
            usage = by_key.setdefault(key_id, {"spend_usd": 0.0, "tokens_monthly": 0})
            usage["spend_usd"] += spend
            usage["tokens_monthly"] += total
    rolling["spend_usd"] = round(rolling["spend_usd"], 6)
    monthly["spend_usd"] = round(monthly["spend_usd"], 6)
    for usage in by_key.values():
        usage["spend_usd"] = round(usage["spend_usd"], 6)
    return rolling, monthly, by_key


def event_rows() -> list[dict[str, Any]]:
    with db_connect() as connection:
        rows = connection.execute(
            "SELECT event_id, key_id, key_alias, source, reason, occurred_at, received_at, status "
            "FROM exposure_events ORDER BY received_at DESC LIMIT 100"
        ).fetchall()
    return [dict(row) for row in rows]


def find_key(rows: list[dict[str, Any]], key_id: str | None, alias: str | None = None) -> dict[str, Any] | None:
    for row in rows:
        if key_id and key_id_for(row) == key_id:
            return row
    if alias:
        matches = [row for row in rows if (row.get("key_alias") or row.get("key_name")) == alias]
        if len(matches) == 1:
            return matches[0]
    return None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    with db_connect():
        pass
    yield


app = FastAPI(title="Dark Factory API Guard", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/api-guard/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
    return response


@app.get("/healthz")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api-guard/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api-guard/auth/start")
async def oauth_start() -> Response:
    if not settings.oauth_configured:
        raise HTTPException(status_code=503, detail="Forgejo administrator sign-in is not configured")
    state = encode_signed({"nonce": secrets.token_urlsafe(24), "exp": int(time.time()) + 600})
    callback = f"{settings.public_url.rstrip('/')}/api-guard/auth/callback"
    query = urlencode({
        "client_id": settings.forgejo_client_id,
        "redirect_uri": callback,
        "response_type": "code",
        "state": state,
    })
    response = RedirectResponse(f"{settings.forgejo_public_url.rstrip('/')}/login/oauth/authorize?{query}")
    response.set_cookie(OAUTH_STATE_COOKIE, state, max_age=600, httponly=True, secure=settings.session_secure, samesite="lax", path="/api-guard")
    return response


@app.get("/api-guard/auth/callback")
async def oauth_callback(code: str, state: str, request: Request) -> Response:
    cookie_state = request.cookies.get(OAUTH_STATE_COOKIE, "")
    payload = decode_signed(cookie_state) if cookie_state else None
    if not payload or not hmac.compare_digest(cookie_state, state):
        raise HTTPException(status_code=400, detail="Invalid OAuth state")
    oauth_base = settings.forgejo_api_url.removesuffix("/api/v1")
    callback = f"{settings.public_url.rstrip('/')}/api-guard/auth/callback"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            token_response = await client.post(
                f"{oauth_base}/login/oauth/access_token",
                data={"client_id": settings.forgejo_client_id, "client_secret": settings.forgejo_client_secret, "code": code, "grant_type": "authorization_code", "redirect_uri": callback},
                headers={"Accept": "application/json"},
            )
            token_response.raise_for_status()
            access_token = token_response.json().get("access_token")
            if not isinstance(access_token, str) or not access_token:
                raise HTTPException(status_code=502, detail="Forgejo returned no OAuth token")
            user_response = await client.get(
                f"{settings.forgejo_api_url.rstrip('/')}/user",
                headers={"Authorization": f"token {access_token}"},
            )
            user_response.raise_for_status()
            login = user_response.json().get("login")
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Forgejo sign-in failed") from exc
    if not isinstance(login, str) or login.casefold() not in settings.admin_logins:
        response = RedirectResponse("/api-guard/?access=denied", status_code=303)
        response.delete_cookie(OAUTH_STATE_COOKIE, path="/api-guard")
        return response
    token = encode_signed({"login": login, "csrf": secrets.token_urlsafe(24), "exp": int(time.time()) + 12 * 60 * 60})
    response = RedirectResponse("/api-guard/", status_code=303)
    response.set_cookie(SESSION_COOKIE, token, max_age=12 * 60 * 60, httponly=True, secure=settings.session_secure, samesite="lax", path="/api-guard")
    response.delete_cookie(OAUTH_STATE_COOKIE, path="/api-guard")
    return response


@app.get("/api-guard/api/v1/session")
async def session_info(request: Request) -> dict[str, Any]:
    user = session_from_request(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Administrator sign-in required")
    return {"login": user["login"], "csrf_token": user["csrf"], "jev_configured": bool(settings.jev_api_key)}


@app.post("/api-guard/auth/logout")
async def logout(request: Request) -> Response:
    user = require_admin(request)
    require_csrf(request, user)
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE, path="/api-guard")
    return response


@app.get("/api-guard/api/v1/overview")
async def overview(request: Request) -> dict[str, Any]:
    require_admin(request)
    now = utcnow()
    keys_payload = await litellm_request("GET", "/key/list")
    rows = key_rows(keys_payload)
    rolling_start = now - timedelta(days=365)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    log_rows, usage_truncated = await fetch_spend_logs(rolling_start, now)
    annual, monthly, per_key = aggregate_spend_logs(log_rows, rolling_start, month_start, now)
    month_target = settings.annual_budget_usd / 12
    keys = [safe_key_out(row, now) for row in rows]
    for key in keys:
        usage = per_key.get(key["key_id"] or "", {})
        key["tokens_monthly"] = usage.get("tokens_monthly", 0)
        key["spend_monthly_usd"] = usage.get("spend_usd", 0.0)
    events = event_rows()
    for event in events:
        matched = find_key(rows, event.get("key_id"), event.get("key_alias"))
        event["matched_key_id"] = key_id_for(matched) if matched else None
        event["matched_alias"] = (matched.get("key_alias") or matched.get("key_name")) if matched else None
    return {
        "as_of": now.isoformat(),
        "budget": {
            "annual_limit_usd": settings.annual_budget_usd,
            "annual_spend_usd": annual["spend_usd"],
            "monthly_target_usd": round(month_target, 2),
            "monthly_spend_usd": monthly["spend_usd"],
            "annual_percent": round(min(100.0, annual["spend_usd"] / settings.annual_budget_usd * 100), 2),
            "monthly_target_percent": round(min(100.0, monthly["spend_usd"] / month_target * 100), 2),
            "monthly_tokens": monthly["total_tokens"],
            "annual_tokens": annual["total_tokens"],
            "prompt_tokens_monthly": monthly["prompt_tokens"],
            "completion_tokens_monthly": monthly["completion_tokens"],
        },
        "rotation_days": settings.rotation_days,
        "usage_truncated": usage_truncated,
        "keys": keys,
        "exposure_events": events,
        "jev_configured": bool(settings.jev_api_key),
        "jev_model": settings.jev_model,
    }


@app.get("/api-guard/api/v1/keys")
async def list_keys(request: Request) -> list[dict[str, Any]]:
    require_admin(request)
    return [safe_key_out(row, utcnow()) for row in key_rows(await litellm_request("GET", "/key/list"))]


@app.post("/api-guard/api/v1/keys")
async def create_key(body: KeyCreate, request: Request, response: Response) -> dict[str, Any]:
    user = require_admin(request)
    require_csrf(request, user)
    if body.max_budget_usd > settings.annual_budget_usd:
        raise HTTPException(status_code=422, detail="Per-key budget cannot exceed the configured annual budget")
    result = await litellm_request("POST", "/key/generate", body={
        "key_alias": body.alias,
        "models": body.models,
        "max_budget": body.max_budget_usd,
        "budget_duration": body.budget_duration,
        "duration": f"{settings.rotation_days * 2}d",
        "metadata": {"managed_by": "darkfactory-api-guard"},
    })
    key = result.get("key") if isinstance(result, dict) else None
    if not isinstance(key, str) or not key:
        raise HTTPException(status_code=502, detail="LiteLLM did not return a new key")
    response.headers["Cache-Control"] = "no-store"
    return {"key": key, "alias": body.alias, "expires_after_days": settings.rotation_days * 2, "rotate_after_days": settings.rotation_days}


@app.post("/api-guard/api/v1/keys/rotate")
async def rotate_key(body: KeyReference, request: Request, response: Response) -> dict[str, Any]:
    user = require_admin(request)
    require_csrf(request, user)
    rows = key_rows(await litellm_request("GET", "/key/list"))
    old = find_key(rows, body.key_id)
    if old is None:
        raise HTTPException(status_code=404, detail="LiteLLM key not found; refresh the inventory")
    alias = old.get("key_alias") or old.get("key_name") or "key"
    safe_alias = re.sub(r"[^A-Za-z0-9_.:-]", "-", str(alias))[:72]
    stored_models = old.get("models")
    scope_narrowed = not isinstance(stored_models, list) or not stored_models
    models = stored_models if not scope_narrowed else ["luna"]
    max_budget = _optional_number(old.get("max_budget"))
    if max_budget is None:
        raise HTTPException(status_code=409, detail="This key has no budget. Set a per-key budget before rotating it.")
    duration = old.get("budget_duration") if old.get("budget_duration") in {"1d", "7d", "30d"} else "30d"
    result = await litellm_request("POST", "/key/generate", body={
        "key_alias": f"{safe_alias}-rotated-{utcnow().strftime('%Y%m%d%H%M%S')}",
        "models": models,
        "max_budget": max_budget,
        "budget_duration": duration,
        "duration": f"{settings.rotation_days * 2}d",
        "team_id": old.get("team_id"),
        "metadata": {"managed_by": "darkfactory-api-guard", "rotated_from": body.key_id},
    })
    key = result.get("key") if isinstance(result, dict) else None
    if not isinstance(key, str) or not key:
        raise HTTPException(status_code=502, detail="LiteLLM did not return a replacement key")
    response.headers["Cache-Control"] = "no-store"
    return {
        "key": key,
        "alias": f"{safe_alias}-rotated",
        "old_key_id": body.key_id,
        "old_key_revoked": False,
        "models_narrowed_to": ["luna"] if scope_narrowed else [],
        "expires_after_days": settings.rotation_days * 2,
        "rotate_after_days": settings.rotation_days,
    }


@app.post("/api-guard/api/v1/keys/revoke")
async def revoke_key(body: KeyReference, request: Request) -> dict[str, Any]:
    user = require_admin(request)
    require_csrf(request, user)
    rows = key_rows(await litellm_request("GET", "/key/list"))
    old = find_key(rows, body.key_id)
    if old is None:
        raise HTTPException(status_code=404, detail="LiteLLM key not found; refresh the inventory")
    key_alias = old.get("key_alias") or old.get("key_name") or ""
    await litellm_request("POST", "/key/delete", body={"keys": [body.key_id]})
    with db_connect() as connection:
        connection.execute(
            "UPDATE exposure_events SET status='revoked' "
            "WHERE status='flagged' AND (key_id=? OR (key_id IS NULL AND key_alias=?))",
            (body.key_id, key_alias),
        )
    return {"revoked": True, "key_id": body.key_id}


@app.post("/api-guard/api/v1/exposures/resolve")
async def resolve_exposure(body: ResolveEvent, request: Request) -> dict[str, bool]:
    user = require_admin(request)
    require_csrf(request, user)
    with db_connect() as connection:
        cursor = connection.execute("UPDATE exposure_events SET status='resolved' WHERE event_id=? AND status='flagged'", (body.event_id,))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Open exposure event not found")
    return {"resolved": True}


@app.post("/api-guard/api/v1/webhooks/exposure", status_code=202)
async def exposure_webhook(request: Request) -> dict[str, Any]:
    if not settings.exposure_webhook_secret:
        raise HTTPException(status_code=503, detail="Exposure webhook is not configured")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 16_384:
            raise HTTPException(status_code=413, detail="Webhook body is too large")
    raw = bytes(raw)
    supplied = request.headers.get("X-Darkfactory-Signature", "")
    expected = hmac.new(settings.exposure_webhook_secret.encode(), raw, hashlib.sha256).hexdigest()
    normalized = supplied.removeprefix("sha256=")
    if not normalized or not hmac.compare_digest(normalized.lower(), expected):
        raise HTTPException(status_code=403, detail="Invalid webhook signature")
    try:
        event = ExposureEvent.model_validate_json(raw)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid exposure event; credentials must never be sent") from exc
    occurred = event.occurred_at or utcnow()
    if occurred.tzinfo is None:
        raise HTTPException(status_code=422, detail="occurred_at must include a timezone")
    with db_connect() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO exposure_events "
            "(event_id,key_id,key_alias,source,reason,occurred_at,received_at,status) "
            "VALUES (?,?,?,?,?,?,?,'flagged')",
            (event.event_id, event.key_id, event.key_alias, event.source, event.reason, occurred.astimezone(timezone.utc).isoformat(), utcnow().isoformat()),
        )
    return {"accepted": True, "event_id": event.event_id, "flagged": True}


@app.post("/api-guard/api/v1/decision-review")
async def decision_review(request: Request, response: Response) -> dict[str, Any]:
    require_admin(request)
    if not settings.jev_api_key:
        raise HTTPException(status_code=503, detail="JEV advisor is not configured")
    now = utcnow()
    overview_data = await overview(request)
    safe_input = {
        "as_of": overview_data["as_of"],
        "budget": overview_data["budget"],
        "keys": [
            {
                "alias": key["alias"] if ALIAS_PATTERN.fullmatch(key["alias"]) else "restricted-key",
                "models": [model for model in key["models"] if ALIAS_PATTERN.fullmatch(model)],
                "spend_usd": key["spend_usd"],
                "age_days": key["age_days"],
                "max_budget_usd": key["max_budget_usd"],
                "blocked": key["blocked"],
            }
            for key in overview_data["keys"]
        ],
        "open_exposure_count": sum(1 for event in overview_data["exposure_events"] if event["status"] == "flagged"),
        "rotation_days": settings.rotation_days,
    }
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            result = await client.post(
                f"{settings.litellm_base_url.rstrip('/')}/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.jev_api_key}", "Content-Type": "application/json"},
                json={
                    "model": settings.jev_model,
                    "messages": [
                        {"role": "system", "content": "You are JEV, an advisory decision model for Dark Factory API security and budget operations. Review only the supplied aggregate data. Never ask for or infer secrets. Give concise, evidence-based warnings and next steps. You do not authorize or execute key actions; deterministic policy and an administrator control those."},
                        {"role": "user", "content": json.dumps(safe_input, separators=(",", ":"))},
                    ],
                    "max_tokens": 600,
                    "temperature": 0,
                },
            )
            result.raise_for_status()
            output = result.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="JEV review is unavailable") from exc
    try:
        content = output["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise HTTPException(status_code=502, detail="JEV returned an invalid response") from exc
    if not isinstance(content, str):
        raise HTTPException(status_code=502, detail="JEV returned an invalid response")
    response.headers["Cache-Control"] = "no-store"
    return {"model": settings.jev_model, "advisory_only": True, "review": content[:4000], "as_of": now.isoformat()}


@app.get("/api-guard/api/v1/webhook-config")
async def webhook_config(request: Request) -> dict[str, Any]:
    require_admin(request)
    return {"configured": bool(settings.exposure_webhook_secret), "path": "/api-guard/api/v1/webhooks/exposure"}


@app.get("/api-guard/api/v1/auth-config")
async def auth_config() -> dict[str, bool]:
    return {"forgejo_oauth": settings.oauth_configured}
