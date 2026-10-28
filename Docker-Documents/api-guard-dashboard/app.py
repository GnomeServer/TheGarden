from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
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
logger = logging.getLogger("api_guard")


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
    keybroker_base_url: str = Field(default="http://keybroker:8000", validation_alias="API_GUARD_KEYBROKER_BASE_URL")
    keybroker_dashboard_token: str = Field(default="", validation_alias="KEYBROKER_DASHBOARD_TOKEN")
    alert_webhook_url: str = Field(default="", validation_alias="API_GUARD_ALERT_WEBHOOK_URL")
    alert_webhook_secret: str = Field(default="", validation_alias="API_GUARD_ALERT_WEBHOOK_SECRET")
    alert_status_url: str = Field(
        default="http://agent-manager:8000/v1/webhooks/api-guard/status",
        validation_alias="API_GUARD_ALERT_STATUS_URL",
    )
    alert_ack_secret: str = Field(default="", validation_alias="API_GUARD_ALERT_ACK_SECRET")
    exposure_webhook_secret: str = Field(default="", validation_alias="API_GUARD_EXPOSURE_WEBHOOK_SECRET")
    openai_admin_api_key: str = Field(default="", validation_alias="API_GUARD_OPENAI_ADMIN_API_KEY")
    openai_project_id: str = Field(default="", validation_alias="API_GUARD_OPENAI_PROJECT_ID")
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
KNOWN_CREDENTIAL_PATTERN = re.compile(
    r"(?:sk-(?:proj-|live-|or-v1-)?[A-Za-z0-9_-]{20,}|sk-ant-api\d{2}-[A-Za-z0-9_-]{20,}|AIza[A-Za-z0-9_-]{30,})"
)
MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
MAX_SPEND_LOG_PAGES = 10
MAX_WORKER_SESSION_PAGES = 10
MAX_WORKER_SESSION_DISPLAY = 200
MAX_OPENAI_COST_PAGES = 4


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
            "received_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'flagged', "
            "worker_id TEXT, session_id TEXT)"
        )
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(exposure_events)")}
        if "worker_id" not in columns:
            connection.execute("ALTER TABLE exposure_events ADD COLUMN worker_id TEXT")
        if "session_id" not in columns:
            connection.execute("ALTER TABLE exposure_events ADD COLUMN session_id TEXT")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS ix_exposure_status_received ON exposure_events(status, received_at)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS alert_deliveries ("
            "event_id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', "
            "attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT NOT NULL, "
            "created_at TEXT NOT NULL, delivered_at TEXT, last_error TEXT)"
        )
        alert_columns = {row["name"] for row in connection.execute("PRAGMA table_info(alert_deliveries)")}
        if "acknowledged_at" not in alert_columns:
            connection.execute("ALTER TABLE alert_deliveries ADD COLUMN acknowledged_at TEXT")
        if "acknowledged_by" not in alert_columns:
            connection.execute("ALTER TABLE alert_deliveries ADD COLUMN acknowledged_by TEXT")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS ix_alert_deliveries_due "
            "ON alert_deliveries(status, next_attempt_at)"
        )
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
    worker_id: str | None = Field(default=None, max_length=128)
    session_id: str | None = Field(default=None, max_length=128)
    source: str = Field(default="omp", min_length=1, max_length=32)
    reason: Literal["configured_credential_echo", "credential_pattern_in_output"]
    occurred_at: datetime | None = None

    @field_validator("event_id", "key_id", "worker_id", "session_id")
    @classmethod
    def validate_identifiers(cls, value: str | None) -> str | None:
        if value is not None and (
            not KEY_ID_PATTERN.fullmatch(value) or KNOWN_CREDENTIAL_PATTERN.search(value)
        ):
            raise ValueError("identifier must be safe and contain no secret material")
        return value

    @field_validator("key_alias", "source")
    @classmethod
    def validate_labels(cls, value: str) -> str:
        value = value.strip()
        if not value.isprintable() or any(character in value for character in "\r\n"):
            raise ValueError("label contains unsupported characters")
        if KNOWN_CREDENTIAL_PATTERN.search(value):
            raise ValueError("credential material must never be sent")
        return value


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



class AlertAcknowledgement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=128)
    acknowledged_by: str = Field(min_length=1, max_length=100)
    acknowledged_at: datetime

    @field_validator("event_id")
    @classmethod
    def validate_event_id(cls, value: str) -> str:
        if not KEY_ID_PATTERN.fullmatch(value) or KNOWN_CREDENTIAL_PATTERN.search(value):
            raise ValueError("event_id must be a safe identifier")
        return value

    @field_validator("acknowledged_by")
    @classmethod
    def validate_acknowledger(cls, value: str) -> str:
        value = value.strip()
        if not ALIAS_PATTERN.fullmatch(value) or KNOWN_CREDENTIAL_PATTERN.search(value):
            raise ValueError("acknowledged_by must be a safe account name")
        return value

async def keybroker_request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> Any:
    if not settings.keybroker_dashboard_token:
        raise HTTPException(status_code=503, detail="Keybroker administration is not configured")
    headers = {
        "Authorization": f"Bearer {settings.keybroker_dashboard_token}",
        "Accept": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.request(
                method,
                f"{settings.keybroker_base_url.rstrip('/')}{path}",
                headers=headers,
                params=params,
                json=body,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Keybroker is unavailable") from exc
    if response.status_code >= 400:
        safe_pricing_errors = {
            "LiteLLM pricing is missing or zero; virtual-key issuance is blocked",
            "LiteLLM model pricing metadata is unavailable; virtual-key issuance is blocked",
            "LiteLLM pricing is missing or zero after model-cost-map refresh; virtual-key issuance is blocked",
        }
        detail = ""
        try:
            payload = response.json()
            detail = payload.get("detail", "") if isinstance(payload, dict) else ""
        except ValueError:
            pass
        if response.status_code == 503 and detail in safe_pricing_errors:
            raise HTTPException(status_code=503, detail=detail)
        raise HTTPException(status_code=502, detail="Keybroker rejected the dashboard request")
    try:
        return response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="Keybroker returned an invalid response") from exc


def key_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        rows = payload.get("keys", payload.get("data", []))
    else:
        rows = payload
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def key_id_for(row: dict[str, Any]) -> str | None:
    value = row.get("key_id") or row.get("token_id") or row.get("token_hash") or row.get("token")
    if (
        not isinstance(value, str)
        or not KEY_ID_PATTERN.fullmatch(value)
        or value.startswith("sk-")
        or KNOWN_CREDENTIAL_PATTERN.search(value)
    ):
        return None
    return value

def safe_key_alias(row: dict[str, Any]) -> str:
    alias = row.get("alias") or row.get("key_alias") or row.get("key_name") or "Unnamed key"
    if not isinstance(alias, str) or KNOWN_CREDENTIAL_PATTERN.search(alias):
        return "Restricted key alias"
    return alias[:100]


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
    alias = safe_key_alias(row)
    models = row.get("models") if isinstance(row.get("models"), list) else []
    return {
        "key_id": key_id_for(row),
        "alias": alias,
        "models": [model for model in models if isinstance(model, str)][:10],
        "spend_usd": _number(row.get("spend_usd", row.get("spend"))),
        "max_budget_usd": _optional_number(row.get("max_budget_usd", row.get("max_budget"))),
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
        payload = await keybroker_request(
            "GET",
            "/v1/admin/spend-logs",
            params={
                "start": start.strftime("%Y-%m-%d %H:%M:%S"),
                "end": end.strftime("%Y-%m-%d %H:%M:%S"),
                "page": page,
                "page_size": 1000,
            },
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise HTTPException(status_code=502, detail="Keybroker returned invalid spend logs")
        rows.extend(row for row in payload["data"] if isinstance(row, dict))
        total_pages = max(1, int(_number(payload.get("total_pages"))))
        truncated |= bool(payload.get("total_is_capped")) or total_pages > MAX_SPEND_LOG_PAGES
        if page >= total_pages or page >= MAX_SPEND_LOG_PAGES:
            break
        page += 1
    return rows, truncated


def safe_worker_session(row: dict[str, Any]) -> dict[str, Any]:
    key_id = row.get("key_id")
    if (
        not isinstance(key_id, str)
        or not KEY_ID_PATTERN.fullmatch(key_id)
        or key_id.casefold().startswith("sk-")
        or KNOWN_CREDENTIAL_PATTERN.search(key_id)
    ):
        key_id = None
    session_id = row.get("session_id")
    if (
        not isinstance(session_id, str)
        or not KEY_ID_PATTERN.fullmatch(session_id)
        or session_id.casefold().startswith("sk-")
        or KNOWN_CREDENTIAL_PATTERN.search(session_id)
    ):
        session_id = "unknown-session"
    worker_id = row.get("worker_id")
    if (
        not isinstance(worker_id, str)
        or not ALIAS_PATTERN.fullmatch(worker_id)
        or worker_id.casefold().startswith("sk-")
        or KNOWN_CREDENTIAL_PATTERN.search(worker_id)
    ):
        worker_id = "unmapped"
    status = row.get("status")
    if status not in {"active", "expired", "revoked"}:
        status = "unknown"
    return {
        "session_id": session_id,
        "worker_id": worker_id,
        "key_id": key_id,
        "key_id_suffix": key_id[-8:] if key_id else "",
        "alias": safe_key_alias({"alias": row.get("alias")}),
        "model": safe_key_alias({"alias": row.get("model")}) if row.get("model") else None,
        "created_at": row.get("created_at") if isinstance(row.get("created_at"), str) else None,
        "expires_at": row.get("expires_at") if isinstance(row.get("expires_at"), str) else None,
        "revoked_at": row.get("revoked_at") if isinstance(row.get("revoked_at"), str) else None,
        "usage_attribution": "no usage" if key_id else "unmapped",
        "status": status,
    }


async def fetch_worker_sessions(start: datetime, end: datetime) -> tuple[list[dict[str, Any]], bool]:
    rows: list[dict[str, Any]] = []
    page = 1
    total_pages = 1
    truncated = False
    while page <= MAX_WORKER_SESSION_PAGES:
        payload = await keybroker_request(
            "GET",
            "/v1/admin/sessions",
            params={
                "start": start.strftime("%Y-%m-%d %H:%M:%S"),
                "end": end.strftime("%Y-%m-%d %H:%M:%S"),
                "page": page,
                "page_size": 1000,
            },
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise HTTPException(status_code=502, detail="Keybroker returned invalid worker session history")
        rows.extend(
            safe_worker_session(row)
            for row in payload["data"]
            if isinstance(row, dict)
        )
        total_pages = max(1, int(_number(payload.get("total_pages"))))
        truncated |= bool(payload.get("total_is_capped")) or total_pages > MAX_WORKER_SESSION_PAGES
        if page >= total_pages or page >= MAX_WORKER_SESSION_PAGES:
            break
        page += 1
    return rows, truncated


async def fetch_worker_roster() -> list[dict[str, Any]]:
    payload = await keybroker_request("GET", "/v1/admin/workers")
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise HTTPException(status_code=502, detail="Keybroker returned invalid worker roster")
    workers = []
    for row in payload["data"][:100]:
        if not isinstance(row, dict):
            continue
        worker_id = row.get("worker_id")
        if (
            not isinstance(worker_id, str)
            or not KEY_ID_PATTERN.fullmatch(worker_id)
            or KNOWN_CREDENTIAL_PATTERN.search(worker_id)
        ):
            worker_id = "unmapped"
        host = safe_key_alias({"alias": row.get("host")})
        ip_value = row.get("tailnet_ip")
        if not isinstance(ip_value, str):
            tailnet_ip = "unknown"
        else:
            try:
                tailnet_ip = str(ipaddress.ip_address(ip_value))
            except ValueError:
                tailnet_ip = "unknown"
        last_session = row.get("last_session_at")
        if isinstance(last_session, str):
            try:
                parsed = datetime.fromisoformat(last_session.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                last_session = parsed.astimezone(timezone.utc).isoformat()
            except ValueError:
                last_session = None
        else:
            last_session = None
        workers.append(
            {
                "worker_id": worker_id,
                "host": host,
                "tailnet_ip": tailnet_ip,
                "session_count": max(0, int(_number(row.get("session_count")))),
                "last_session_at": last_session,
            }
        )
    workers.sort(key=lambda item: (item["host"], item["worker_id"]))
    return workers


def attribute_worker_usage(
    log_rows: list[dict[str, Any]],
    sessions: list[dict[str, Any]],
    known_key_ids: set[str],
    month_start: datetime,
    now: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, int]:
    sessions_by_key: dict[str, list[dict[str, Any]]] = {}
    for item in sessions:
        key_id = item.get("key_id")
        if key_id:
            sessions_by_key.setdefault(key_id, []).append(item)

    usage_by_key: dict[str, dict[str, float | int]] = {}
    for row in log_rows:
        started = log_started_at(row)
        key_id = row.get("api_key")
        if (
            started is None
            or started > now
            or not isinstance(key_id, str)
            or not KEY_ID_PATTERN.fullmatch(key_id)
            or key_id.casefold().startswith("sk-")
            or KNOWN_CREDENTIAL_PATTERN.search(key_id)
        ):
            continue
        usage = usage_by_key.setdefault(
            key_id,
            {
                "spend_365d_usd": 0.0,
                "tokens_365d": 0,
                "spend_monthly_usd": 0.0,
                "tokens_monthly": 0,
            },
        )
        spend = _number(row.get("spend"))
        tokens = max(0, int(_number(row.get("total_tokens"))))
        if not tokens:
            tokens = max(0, int(_number(row.get("prompt_tokens")))) + max(
                0, int(_number(row.get("completion_tokens")))
            )
        usage["spend_365d_usd"] += spend
        usage["tokens_365d"] += tokens
        if started >= month_start:
            usage["spend_monthly_usd"] += spend
            usage["tokens_monthly"] += tokens

    ambiguous = {
        key_id for key_id, matches in sessions_by_key.items() if len(matches) != 1
    }
    for key_id, matches in sessions_by_key.items():
        if len(matches) != 1:
            for item in matches:
                item["usage_attribution"] = "ambiguous"
            continue
        usage = usage_by_key.get(key_id, {})
        matches[0].update(
            {
                "spend_365d_usd": round(float(usage.get("spend_365d_usd", 0)), 6),
                "tokens_365d": int(usage.get("tokens_365d", 0)),
                "spend_monthly_usd": round(float(usage.get("spend_monthly_usd", 0)), 6),
                "tokens_monthly": int(usage.get("tokens_monthly", 0)),
                "usage_attribution": "matched" if usage else "no usage",
            }
        )
    for item in sessions:
        item.pop("key_id", None)

    unmatched = [
        {
            "key_id_suffix": key_id[-8:],
            "spend_365d_usd": round(float(usage.get("spend_365d_usd", 0)), 6),
            "tokens_365d": int(usage.get("tokens_365d", 0)),
            "spend_monthly_usd": round(float(usage.get("spend_monthly_usd", 0)), 6),
            "tokens_monthly": int(usage.get("tokens_monthly", 0)),
        }
        for key_id, usage in usage_by_key.items()
        if key_id not in known_key_ids and key_id not in sessions_by_key
    ]
    return sessions, unmatched[:20], len(ambiguous), len(unmatched)


async def openai_cost_report(
    rolling_start: datetime,
    month_start: datetime,
    end: datetime,
) -> dict[str, Any]:
    project_id = settings.openai_project_id.strip()
    result: dict[str, Any] = {
        "configured": bool(settings.openai_admin_api_key),
        "available": False,
        "scope": f"project {project_id}" if project_id else "organization",
        "rolling_365d_usd": None,
        "month_to_date_usd": None,
        "truncated": False,
        "as_of": end.isoformat(),
        "message": "",
    }
    if not settings.openai_admin_api_key:
        result["message"] = "Optional OpenAI organization billing connection is not configured."
        return result

    headers = {"Authorization": f"Bearer {settings.openai_admin_api_key}"}
    rolling_total = 0.0
    monthly_total = 0.0
    currencies: set[str] = set()
    cursor: str | None = None
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            for page_index in range(MAX_OPENAI_COST_PAGES):
                params: list[tuple[str, str | int]] = [
                    ("start_time", int(rolling_start.timestamp())),
                    ("end_time", int(end.timestamp())),
                    ("bucket_width", "1d"),
                    ("limit", 180),
                    ("group_by", "project_id"),
                ]
                if project_id:
                    params.append(("project_ids[]", project_id))
                if cursor:
                    params.append(("page", cursor))
                response = await client.get(
                    "https://api.openai.com/v1/organization/costs",
                    headers=headers,
                    params=params,
                )
                response.raise_for_status()
                payload = response.json()
                buckets = payload.get("data") if isinstance(payload, dict) else None
                if not isinstance(buckets, list):
                    result["message"] = "OpenAI returned an invalid cost report."
                    return result
                for bucket in buckets:
                    if not isinstance(bucket, dict):
                        continue
                    bucket_start = _number(bucket.get("start_time"))
                    raw_results = bucket.get("results")
                    if not isinstance(raw_results, list):
                        continue
                    for item in raw_results:
                        if not isinstance(item, dict):
                            continue
                        amount = item.get("amount")
                        if not isinstance(amount, dict):
                            continue
                        currency = str(amount.get("currency") or "").casefold()
                        currencies.add(currency or "unknown")
                        if currency != "usd":
                            continue
                        value = _number(amount.get("value"))
                        rolling_total += value
                        if bucket_start >= month_start.timestamp():
                            monthly_total += value
                has_more = bool(payload.get("has_more"))
                cursor = payload.get("next_page")
                if not has_more:
                    break
                if not isinstance(cursor, str) or not cursor:
                    result["truncated"] = True
                    break
                if page_index == MAX_OPENAI_COST_PAGES - 1:
                    result["truncated"] = True
    except httpx.HTTPStatusError as exc:
        result["message"] = (
            "OpenAI rejected the billing request; configure an organization Admin API key "
            "with Usage Read access."
            if exc.response.status_code in {401, 403}
            else "OpenAI cost data is temporarily unavailable."
        )
        return result
    except (httpx.HTTPError, ValueError):
        result["message"] = "OpenAI cost data is temporarily unavailable."
        return result

    if currencies and currencies != {"usd"}:
        result["message"] = "OpenAI returned non-USD or mixed-currency costs; no converted total is shown."
        return result
    result.update({
        "available": True,
        "rolling_365d_usd": round(rolling_total, 6),
        "month_to_date_usd": round(monthly_total, 6),
        "currency": "USD",
        "message": "Reported by the OpenAI organization Costs API; daily buckets may be delayed.",
    })
    return result


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
    rolling = {
        "spend_usd": 0.0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        "zero_cost_requests": 0, "zero_cost_tokens": 0,
    }
    monthly = {
        "spend_usd": 0.0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        "zero_cost_requests": 0, "zero_cost_tokens": 0,
    }
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
        if spend == 0 and total > 0:
            rolling["zero_cost_requests"] += 1
            rolling["zero_cost_tokens"] += total
        if started < month_start:
            continue
        monthly["spend_usd"] += spend
        monthly["prompt_tokens"] += prompt
        monthly["completion_tokens"] += completion
        if spend == 0 and total > 0:
            monthly["zero_cost_requests"] += 1
            monthly["zero_cost_tokens"] += total
        monthly["total_tokens"] += total
        key_id = row.get("api_key")
        if isinstance(key_id, str) and KEY_ID_PATTERN.fullmatch(key_id) and not key_id.casefold().startswith("sk-"):
            usage = by_key.setdefault(key_id, {"spend_usd": 0.0, "tokens_monthly": 0})
            usage["spend_usd"] += spend
            usage["tokens_monthly"] += total
    rolling["spend_usd"] = round(rolling["spend_usd"], 6)
    monthly["spend_usd"] = round(monthly["spend_usd"], 6)
    for usage in by_key.values():
        usage["spend_usd"] = round(usage["spend_usd"], 6)
    return rolling, monthly, by_key


def safe_model_label(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not MODEL_PATTERN.fullmatch(value)
        or value.casefold().startswith(("sk-", "AIza"))
        or KNOWN_CREDENTIAL_PATTERN.search(value)
    ):
        return "unmapped"
    return value


def zero_cost_model_breakdown(
    rows: list[dict[str, Any]],
    month_start: datetime,
    now: datetime,
) -> list[dict[str, Any]]:
    by_model: dict[str, dict[str, int]] = {}
    for row in rows:
        started = log_started_at(row)
        spend = _number(row.get("spend"))
        total = max(0, int(_number(row.get("total_tokens"))))
        if not total:
            total = max(0, int(_number(row.get("prompt_tokens")))) + max(
                0, int(_number(row.get("completion_tokens")))
            )
        if started is None or started < month_start or started > now or spend != 0 or total <= 0:
            continue
        model = safe_model_label(row.get("model"))
        bucket = by_model.setdefault(model, {"requests": 0, "tokens": 0})
        bucket["requests"] += 1
        bucket["tokens"] += total
    return [
        {"model": model, **usage}
        for model, usage in sorted(
            by_model.items(),
            key=lambda item: (item[1]["tokens"], item[1]["requests"]),
            reverse=True,
        )
    ][:10]


def event_rows() -> list[dict[str, Any]]:
    with db_connect() as connection:
        rows = connection.execute(
            "SELECT e.event_id, e.key_id, e.key_alias, e.source, e.reason, e.occurred_at, "
            "e.received_at, e.status, e.worker_id, e.session_id, "
            "COALESCE(a.status, 'not_configured') AS notification_status, "
            "COALESCE(a.attempts, 0) AS notification_attempts, "
            "a.acknowledged_at AS notification_acknowledged_at, "
            "a.acknowledged_by AS notification_acknowledged_by "
            "FROM exposure_events AS e LEFT JOIN alert_deliveries AS a ON a.event_id=e.event_id "
            "ORDER BY e.received_at DESC LIMIT 100"
        ).fetchall()
    return [dict(row) for row in rows]


def pending_alert_count() -> int:
    with db_connect() as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS count FROM alert_deliveries "
            "WHERE status IN ('pending','awaiting_ack')"
        ).fetchone()
    return int(row["count"])


def find_key(rows: list[dict[str, Any]], key_id: str | None, alias: str | None = None) -> dict[str, Any] | None:
    for row in rows:
        if key_id and key_id_for(row) == key_id:
            return row
    if alias:
        matches = [row for row in rows if (row.get("alias") or row.get("key_alias") or row.get("key_name")) == alias]
        if len(matches) == 1:
            return matches[0]
    return None


async def send_alert_notification(payload: bytes) -> None:
    if not settings.alert_webhook_url or not settings.alert_webhook_secret or not settings.alert_ack_secret:
        raise RuntimeError("alert delivery or acknowledgement is not configured")
    signature = hmac.new(
        settings.alert_webhook_secret.encode(), payload, hashlib.sha256
    ).hexdigest()
    async with httpx.AsyncClient(timeout=5) as client:
        response = await client.post(
            settings.alert_webhook_url,
            content=payload,
            headers={
                "Content-Type": "application/json",
                "X-Darkfactory-Signature": f"sha256={signature}",
            },
        )
        response.raise_for_status()


async def alert_receiver_readiness() -> dict[str, bool]:
    result = {
        "available": False,
        "receiver_configured": False,
        "external_notification_configured": False,
        "ack_callback_configured": False,
    }
    if (
        not settings.alert_webhook_url
        or not settings.alert_webhook_secret
        or not settings.alert_ack_secret
        or not settings.alert_status_url
    ):
        return result
    proof = b"GET:/v1/webhooks/api-guard/status"
    signature = hmac.new(
        settings.alert_webhook_secret.encode("utf-8"), proof, hashlib.sha256
    ).hexdigest()
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            response = await client.get(
                settings.alert_status_url,
                headers={"X-Darkfactory-Signature": f"sha256={signature}"},
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError):
        return result
    if not isinstance(payload, dict):
        return result
    result["available"] = True
    for field in (
        "receiver_configured",
        "external_notification_configured",
        "ack_callback_configured",
    ):
        result[field] = payload.get(field) is True
    return result


async def deliver_pending_alerts() -> int:
    if not settings.alert_webhook_url or not settings.alert_webhook_secret or not settings.alert_ack_secret:
        return 0
    now = utcnow()
    with db_connect() as connection:
        rows = connection.execute(
            "SELECT event_id, payload, attempts FROM alert_deliveries "
            "WHERE status IN ('pending','awaiting_ack') AND next_attempt_at<=? "
            "ORDER BY created_at LIMIT 25",
            (now.isoformat(),),
        ).fetchall()
    delivered = 0
    for row in rows:
        try:
            await send_alert_notification(row["payload"].encode("utf-8"))
        except (httpx.HTTPError, RuntimeError, ValueError):
            attempts = row["attempts"] + 1
            delay_seconds = min(60 * (2 ** min(attempts - 1, 6)), 3600)
            retry_at = (utcnow() + timedelta(seconds=delay_seconds)).isoformat()
            with db_connect() as connection:
                connection.execute(
                    "UPDATE alert_deliveries SET status='pending', attempts=?, next_attempt_at=?, "
                    "last_error='delivery_failed' WHERE event_id=? AND status IN ('pending','awaiting_ack')",
                    (attempts, retry_at, row["event_id"]),
                )
        else:
            accepted_at = utcnow()
            retry_at = (accepted_at + timedelta(minutes=5)).isoformat()
            with db_connect() as connection:
                connection.execute(
                    "UPDATE alert_deliveries SET status='awaiting_ack', attempts=attempts+1, "
                    "next_attempt_at=?, delivered_at=COALESCE(delivered_at,?), last_error=NULL "
                    "WHERE event_id=? AND status IN ('pending','awaiting_ack')",
                    (retry_at, accepted_at.isoformat(), row["event_id"]),
                )
            delivered += 1
    return delivered


async def alert_delivery_loop() -> None:
    while True:
        try:
            await deliver_pending_alerts()
        except Exception:
            # The durable outbox remains pending; the next loop retries it.
            logger.warning("API Guard alert outbox pass failed; pending events will retry")
        await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    with db_connect():
        pass
    delivery_task = asyncio.create_task(alert_delivery_loop())
    try:
        yield
    finally:
        delivery_task.cancel()
        try:
            await delivery_task
        except asyncio.CancelledError:
            pass
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
    keys_payload = await keybroker_request("GET", "/v1/admin/keys")
    rows = key_rows(keys_payload)
    rolling_start = now - timedelta(days=365)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    log_rows, usage_truncated = await fetch_spend_logs(rolling_start, now)
    worker_sessions, sessions_truncated = await fetch_worker_sessions(rolling_start, now)
    worker_roster = await fetch_worker_roster()
    alert_receiver = await alert_receiver_readiness()
    alert_delivery_configured = bool(
        settings.alert_webhook_url
        and settings.alert_webhook_secret
        and settings.alert_ack_secret
        and alert_receiver["available"]
        and alert_receiver["receiver_configured"]
        and alert_receiver["external_notification_configured"]
        and alert_receiver["ack_callback_configured"]
    )
    provider_costs = await openai_cost_report(rolling_start, month_start, now)
    annual, monthly, per_key = aggregate_spend_logs(log_rows, rolling_start, month_start, now)
    zero_cost_models_monthly = zero_cost_model_breakdown(log_rows, month_start, now)
    month_target = settings.annual_budget_usd / 12
    keys = [safe_key_out(row, now) for row in rows]
    known_key_ids = {key["key_id"] for key in keys if key["key_id"]}
    active_key_ids = {
        key["key_id"] for key in keys if key["key_id"] and not key["blocked"]
    }
    active_session_key_ids = {
        session["key_id"]
        for session in worker_sessions
        if session["status"] == "active" and session.get("key_id")
    }
    unmanaged_active_keys = [
        {
            "alias": key["alias"],
            "key_id_suffix": key["key_id"][-8:],
            "age_days": key["age_days"],
        }
        for key in keys
        if key["key_id"]
        and key["key_id"] in active_key_ids
        and key["key_id"] not in active_session_key_ids
    ]
    if sessions_truncated:
        unmanaged_active_keys = []
    unmanaged_active_key_count = (
        None if sessions_truncated else len(unmanaged_active_keys)
    )
    worker_sessions, unmatched_usage_keys, ambiguous_session_key_count, unmatched_usage_key_count = (
        attribute_worker_usage(log_rows, worker_sessions, known_key_ids, month_start, now)
    )
    for key in keys:
        usage = per_key.get(key["key_id"] or "", {})
        key["tokens_monthly"] = usage.get("tokens_monthly", 0)
        key["spend_monthly_usd"] = usage.get("spend_usd", 0.0)
    events = event_rows()
    for event in events:
        has_session_identity = event.get("worker_id") or event.get("session_id")
        if has_session_identity:
            matched = find_key(rows, event.get("key_id")) if event.get("key_id") else None
        else:
            matched = find_key(rows, event.get("key_id"), event.get("key_alias"))
        event["matched_key_id"] = key_id_for(matched) if matched else None
        event["matched_alias"] = safe_key_alias(matched) if matched else None
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
            "zero_cost_requests_monthly": monthly["zero_cost_requests"],
            "zero_cost_tokens_monthly": monthly["zero_cost_tokens"],
            "zero_cost_requests_rolling": annual["zero_cost_requests"],
            "zero_cost_models_monthly": zero_cost_models_monthly,
        },
        "openai_costs": provider_costs,
        "rotation_days": settings.rotation_days,
        "usage_truncated": usage_truncated,
        "worker_session_history_truncated": sessions_truncated,
        "worker_sessions_display_truncated": len(worker_sessions) > MAX_WORKER_SESSION_DISPLAY,
        "worker_session_count": len(worker_sessions),
        "worker_roster": worker_roster,
        "worker_roster_count": len(worker_roster),
        "worker_sessions": worker_sessions[:MAX_WORKER_SESSION_DISPLAY],
        "worker_session_status_counts": {
            status: sum(1 for item in worker_sessions if item["status"] == status)
            for status in ("active", "expired", "revoked", "unknown")
        },
        "unblocked_key_count": len(active_key_ids),
        "unmanaged_active_key_count": unmanaged_active_key_count,
        "unmanaged_active_keys": unmanaged_active_keys[:20],
        "known_key_count": len(known_key_ids),
        "ambiguous_session_key_count": ambiguous_session_key_count,
        "unmatched_usage_key_count": unmatched_usage_key_count,
        "unmatched_usage_keys": unmatched_usage_keys,
        "unmatched_usage_keys_truncated": unmatched_usage_key_count > len(unmatched_usage_keys),
        "keys": keys,
        "exposure_events": events,
        "alert_delivery": alert_receiver,
        "alert_notifications_configured": alert_delivery_configured,
        "alert_notification_pending_count": pending_alert_count(),
        "jev_configured": bool(settings.jev_api_key),
        "jev_model": settings.jev_model,
    }


@app.get("/api-guard/api/v1/keys")
async def list_keys(request: Request) -> list[dict[str, Any]]:
    require_admin(request)
    return [safe_key_out(row, utcnow()) for row in key_rows(await keybroker_request("GET", "/v1/admin/keys"))]


@app.post("/api-guard/api/v1/keys")
async def create_key(body: KeyCreate, request: Request, response: Response) -> dict[str, Any]:
    user = require_admin(request)
    require_csrf(request, user)
    if body.max_budget_usd > settings.annual_budget_usd:
        raise HTTPException(status_code=422, detail="Per-key budget cannot exceed the configured annual budget")
    result = await keybroker_request("POST", "/v1/admin/keys/generate", body={
        "alias": body.alias,
        "models": body.models,
        "max_budget_usd": body.max_budget_usd,
        "budget_duration": body.budget_duration,
        "duration": f"{settings.rotation_days * 2}d",
    })
    key = result.get("key") if isinstance(result, dict) else None
    if not isinstance(key, str) or not key:
        raise HTTPException(status_code=502, detail="Keybroker did not return a new key")
    response.headers["Cache-Control"] = "no-store"
    return {"key": key, "alias": body.alias, "expires_after_days": settings.rotation_days * 2, "rotate_after_days": settings.rotation_days}


@app.post("/api-guard/api/v1/keys/rotate")
async def rotate_key(body: KeyReference, request: Request, response: Response) -> dict[str, Any]:
    user = require_admin(request)
    require_csrf(request, user)
    rows = key_rows(await keybroker_request("GET", "/v1/admin/keys"))
    old = find_key(rows, body.key_id)
    if old is None:
        raise HTTPException(status_code=404, detail="Keybroker key not found; refresh the inventory")
    alias = safe_key_alias(old)
    safe_alias = re.sub(r"[^A-Za-z0-9_.:-]", "-", str(alias))[:72]
    stored_models = old.get("models")
    scope_narrowed = not isinstance(stored_models, list) or not stored_models
    models = stored_models if not scope_narrowed else ["luna"]
    max_budget = _optional_number(old.get("max_budget_usd", old.get("max_budget")))
    if max_budget is None:
        raise HTTPException(status_code=409, detail="This key has no budget. Set a per-key budget before rotating it.")
    duration = old.get("budget_duration") if old.get("budget_duration") in {"1d", "7d", "30d"} else "30d"
    rotated_alias = f"{safe_alias}-rotated-{utcnow().strftime('%Y%m%d%H%M%S')}"
    result = await keybroker_request("POST", "/v1/admin/keys/generate", body={
        "alias": rotated_alias,
        "models": models,
        "max_budget_usd": max_budget,
        "budget_duration": duration,
        "duration": f"{settings.rotation_days * 2}d",
    })
    key = result.get("key") if isinstance(result, dict) else None
    if not isinstance(key, str) or not key:
        raise HTTPException(status_code=502, detail="Keybroker did not return a replacement key")
    response.headers["Cache-Control"] = "no-store"
    return {
        "key": key,
        "alias": rotated_alias,
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
    rows = key_rows(await keybroker_request("GET", "/v1/admin/keys"))
    old = find_key(rows, body.key_id)
    if old is None:
        raise HTTPException(status_code=404, detail="Keybroker key not found; refresh the inventory")
    key_alias = old.get("alias") or old.get("key_alias") or old.get("key_name") or ""
    await keybroker_request("POST", "/v1/admin/keys/revoke", body={"key_id": body.key_id})
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
    received_at = utcnow().isoformat()
    notification = {
        "event_id": event.event_id,
        "worker_id": event.worker_id,
        "session_id": event.session_id,
        "key_alias": event.key_alias,
        "reason": event.reason,
        "occurred_at": occurred.astimezone(timezone.utc).isoformat(),
    }
    notification_payload = json.dumps(notification, separators=(",", ":"), ensure_ascii=False)
    with db_connect() as connection:
        inserted = connection.execute(
            "INSERT OR IGNORE INTO exposure_events "
            "(event_id,key_id,key_alias,source,reason,occurred_at,received_at,status,worker_id,session_id) "
            "VALUES (?,?,?,?,?,?,?,'flagged',?,?)",
            (
                event.event_id,
                event.key_id,
                event.key_alias,
                event.source,
                event.reason,
                occurred.astimezone(timezone.utc).isoformat(),
                received_at,
                event.worker_id,
                event.session_id,
            ),
        ).rowcount
        if (
            inserted
            and settings.alert_webhook_url
            and settings.alert_webhook_secret
            and settings.alert_ack_secret
        ):
            connection.execute(
                "INSERT INTO alert_deliveries "
                "(event_id,payload,status,attempts,next_attempt_at,created_at) "
                "VALUES (?,?,'pending',0,?,?)",
                (event.event_id, notification_payload, received_at, received_at),
            )
    return {
        "accepted": True,
        "event_id": event.event_id,
        "flagged": True,
        "notification_queued": bool(
            inserted
            and settings.alert_webhook_url
            and settings.alert_webhook_secret
            and settings.alert_ack_secret
        ),
    }


@app.post("/api-guard/api/v1/webhooks/alerts/ack", status_code=202)
async def alert_acknowledgement_webhook(request: Request) -> dict[str, Any]:
    if not settings.alert_ack_secret:
        raise HTTPException(status_code=503, detail="Alert acknowledgement is not configured")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 4096:
            raise HTTPException(status_code=413, detail="Acknowledgement body is too large")
    raw = bytes(raw)
    supplied = request.headers.get("X-Darkfactory-Signature", "")
    expected = hmac.new(settings.alert_ack_secret.encode(), raw, hashlib.sha256).hexdigest()
    normalized = supplied.removeprefix("sha256=")
    if not normalized or not hmac.compare_digest(normalized.lower(), expected):
        raise HTTPException(status_code=403, detail="Invalid acknowledgement signature")
    try:
        acknowledgement = AlertAcknowledgement.model_validate_json(raw)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid alert acknowledgement") from exc
    if acknowledgement.acknowledged_at.tzinfo is None:
        raise HTTPException(status_code=422, detail="acknowledged_at must include a timezone")
    acknowledged_at = acknowledgement.acknowledged_at.astimezone(timezone.utc).isoformat()
    with db_connect() as connection:
        row = connection.execute(
            "SELECT status FROM alert_deliveries WHERE event_id=?",
            (acknowledgement.event_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Alert delivery not found")
        if row["status"] != "acknowledged":
            connection.execute(
                "UPDATE alert_deliveries SET status='acknowledged', acknowledged_at=?, "
                "acknowledged_by=?, next_attempt_at=?, last_error=NULL WHERE event_id=?",
                (
                    acknowledged_at,
                    acknowledgement.acknowledged_by,
                    acknowledged_at,
                    acknowledgement.event_id,
                ),
            )
    return {"accepted": True, "event_id": acknowledgement.event_id}


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
    provider_costs = overview_data["openai_costs"]
    safe_input["openai_costs"] = {
        field: provider_costs.get(field)
        for field in (
            "configured", "available", "scope", "rolling_365d_usd",
            "month_to_date_usd", "truncated",
        )
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
