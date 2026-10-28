from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Any, Literal

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import DateTime, Index, JSON, String, UniqueConstraint, create_engine, func, select, text, update
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


WORKER_IPS = {
    "100.98.125.127": "worker-02",  # inkii
    "100.88.92.78": "worker-04",  # naruto
    "100.116.35.103": "worker-01",  # raphael
    "100.94.145.104": "worker-03",  # donatello
    "100.79.110.117": "worker-05",  # sammy
    "100.70.246.104": "worker-06",  # michelangelo
}
WORKER_NAMES = {
    "worker-01": "raphael",
    "worker-02": "inkii",
    "worker-03": "donatello",
    "worker-04": "naruto",
    "worker-05": "sammy",
    "worker-06": "michelangelo",
}
logger = logging.getLogger("keybroker")

SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
SAFE_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$")
SAFE_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
SAFE_MODEL_SECRET = re.compile(
    r"(?:sk-(?:or-v1-|ant-api\d{2}-)?[A-Za-z0-9_-]{20,}|AIza[A-Za-z0-9_-]{30,})",
    re.IGNORECASE,
)
SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
DURATION = re.compile(r"^[1-9][0-9]{0,4}[smhd]$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KEYBROKER_", case_sensitive=False, extra="ignore")

    database_url: str = ""
    db_host: str = "postgres"
    db_port: int = 5432
    db_name: str = "keybroker"
    db_user: str = "keybroker"
    db_password: SecretStr = SecretStr("")
    litellm_base_url: str = "http://litellm:4000"
    litellm_master_key: SecretStr = SecretStr("")
    dashboard_token: SecretStr = SecretStr("")
    proxy_token: SecretStr = SecretStr("")
    exposure_webhook_url: str = "http://api-guard:8000/api-guard/api/v1/webhooks/exposure"
    exposure_webhook_secret: SecretStr = SecretStr("")
    model_base_url: str = "https://infra-lab-services.tail494f6d.ts.net/litellm/v1"
    default_model: str = "luna"
    allowed_models: list[str] = Field(default_factory=lambda: ["luna"])
    allow_unpriced_local_model_bases: list[str] = Field(
        default_factory=lambda: ["http://llama:8080/v1"]
    )
    session_max_budget_usd: float = 25.0
    admin_max_budget_usd: float = 25000.0
    session_ttl_seconds: int = 3600
    reaper_interval_seconds: int = 60
    worker_ip_map: dict[str, str] = Field(default_factory=lambda: dict(WORKER_IPS))

    @field_validator("worker_ip_map")
    @classmethod
    def normalize_worker_ip_map(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for address, worker_id in value.items():
            try:
                parsed = str(ipaddress.ip_address(address))
            except ValueError as exc:
                raise ValueError("worker_ip_map contains an invalid IP address") from exc
            if not SAFE_ID.fullmatch(worker_id) or worker_id.lower().startswith("sk-"):
                raise ValueError("worker_ip_map contains an invalid worker ID")
            normalized[parsed] = worker_id
        return normalized

    @field_validator("allowed_models")
    @classmethod
    def validate_allowed_models(cls, value: list[str]) -> list[str]:
        models = list(dict.fromkeys(model.strip() for model in value if model.strip()))
        if not models or any(not SAFE_ALIAS.fullmatch(model) or model.lower().startswith("sk-") for model in models):
            raise ValueError("allowed_models must contain configured model aliases")
        return models

    @field_validator("allow_unpriced_local_model_bases")
    @classmethod
    def validate_unpriced_local_model_bases(cls, value: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(base.rstrip("/") for base in value if base.strip()))
        for base in normalized:
            parsed = httpx.URL(base)
            if parsed.scheme not in {"http", "https"} or not parsed.host or parsed.username or parsed.password:
                raise ValueError("allow_unpriced_local_model_bases must contain HTTP(S) URLs without credentials")
        return normalized

    @model_validator(mode="after")
    def validate_policy(self) -> Settings:
        if self.default_model not in self.allowed_models:
            raise ValueError("default_model must be present in allowed_models")
        if (
            not isfinite(self.session_max_budget_usd)
            or not isfinite(self.admin_max_budget_usd)
            or not 0 < self.session_max_budget_usd <= self.admin_max_budget_usd
        ):
            raise ValueError("session_max_budget_usd must be positive and no greater than admin_max_budget_usd")
        if not 0 < self.session_ttl_seconds <= 86400:
            raise ValueError("session_ttl_seconds must be between 1 and 86400")
        if not 1 <= self.reaper_interval_seconds <= 3600:
            raise ValueError("reaper_interval_seconds must be between 1 and 3600")
        if self.db_port < 1 or self.db_port > 65535:
            raise ValueError("db_port must be a valid TCP port")
        return self

    def sqlalchemy_url(self) -> str | URL:
        if self.database_url:
            return self.database_url
        return URL.create(
            "postgresql+psycopg",
            username=self.db_user,
            password=self.db_password.get_secret_value(),
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )


settings = Settings()


class Base(DeclarativeBase):
    pass


class SessionRecord(Base):
    __tablename__ = "sessions"

    session_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    worker_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    token_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    token_hash: Mapped[str | None] = mapped_column(String(256), nullable=True)
    key_alias: Mapped[str] = mapped_column(String(100), nullable=False)
    policy: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("worker_id", "run_id", name="uq_sessions_worker_run"),
        Index("ix_sessions_worker_id", "worker_id"),
        Index("ix_sessions_token_id", "token_id"),
        Index("ix_sessions_token_hash", "token_hash"),
        Index("ix_sessions_expiry", "expires_at", "revoked_at"),
    )


class ExposureOutboxRecord(Base):
    __tablename__ = "exposure_outbox"

    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    attempts: Mapped[int] = mapped_column(default=0, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_exposure_outbox_due", "delivered_at", "next_attempt_at"),)


engine = create_engine(settings.sqlalchemy_url(), pool_pre_ping=True)


class SessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, max_length=128)
    operation: str = Field(min_length=1, max_length=128)

    @field_validator("run_id")
    @classmethod
    def validate_run_id(cls, value: str) -> str:
        value = value.strip()
        if not SAFE_RUN_ID.fullmatch(value) or value.lower().startswith("sk-"):
            raise ValueError("run_id contains unsupported characters")
        return value

    @field_validator("operation")
    @classmethod
    def validate_operation(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(char) < 32 for char in value):
            raise ValueError("operation must be non-empty printable text")
        return value


class ExposureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=128)
    reason: Literal[
        "configured_credential_echo",
        "credential_pattern_in_output",
        "session_key_echo_detected",
        "credential_pattern_detected",
    ]
    session_key_match: bool

    @field_validator("event_id")
    @classmethod
    def validate_event_id(cls, value: str) -> str:
        if not SAFE_ID.fullmatch(value) or value.lower().startswith("sk-"):
            raise ValueError("event_id contains unsupported characters")
        return value


class AdminKeyGenerate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias: str = Field(min_length=1, max_length=100)
    models: list[str] = Field(min_length=1, max_length=10)
    max_budget_usd: float = Field(gt=0)
    budget_duration: Literal["1d", "7d", "30d"] = "30d"
    duration: str = "30d"

    @field_validator("max_budget_usd")
    @classmethod
    def validate_finite_budget(cls, value: float) -> float:
        if not isfinite(value):
            raise ValueError("max_budget_usd must be finite")
        return value

    @field_validator("alias")
    @classmethod
    def validate_alias(cls, value: str) -> str:
        value = value.strip()
        if not SAFE_ALIAS.fullmatch(value) or value.lower().startswith("sk-"):
            raise ValueError("alias contains unsupported characters")
        return value

    @field_validator("models")
    @classmethod
    def deduplicate_models(cls, value: list[str]) -> list[str]:
        models = list(dict.fromkeys(model.strip() for model in value if model.strip()))
        if not models:
            raise ValueError("models must contain at least one configured alias")
        return models

    @field_validator("duration")
    @classmethod
    def validate_duration(cls, value: str) -> str:
        if not DURATION.fullmatch(value):
            raise ValueError("duration must be a simple LiteLLM duration such as 30d")
        return value


class AdminKeyReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key_id: str = Field(min_length=1, max_length=128)

    @field_validator("key_id")
    @classmethod
    def validate_key_id(cls, value: str) -> str:
        if not SAFE_ID.fullmatch(value) or value.lower().startswith("sk-"):
            raise ValueError("key_id must be a non-secret LiteLLM identifier")
        return value


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def get_session(session_id: str) -> SessionRecord | None:
    with Session(engine) as session:
        return session.get(SessionRecord, session_id)


def get_session_for_run(worker_id: str, run_id: str) -> SessionRecord | None:
    with Session(engine) as session:
        return session.execute(
            select(SessionRecord).where(
                SessionRecord.worker_id == worker_id,
                SessionRecord.run_id == run_id,
            )
        ).scalar_one_or_none()




def mark_session_revoked(session_id: str, when: datetime) -> None:
    with engine.begin() as connection:
        connection.execute(
            update(SessionRecord)
            .where(SessionRecord.session_id == session_id, SessionRecord.revoked_at.is_(None))
            .values(revoked_at=when)
        )


def mark_key_revoked(key_id: str, when: datetime) -> None:
    with engine.begin() as connection:
        connection.execute(
            update(SessionRecord)
            .where(
                SessionRecord.revoked_at.is_(None),
                (SessionRecord.token_id == key_id) | (SessionRecord.token_hash == key_id),
            )
            .values(revoked_at=when)
        )


def normalize_datetime(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def require_worker(request: Request) -> str:
    proxy_token = request.headers.get("X-Darkfactory-Proxy-Token", "")
    configured_token = settings.proxy_token.get_secret_value()
    remote_ip = request.headers.get("X-Darkfactory-Client-IP", "")
    if (
        not configured_token
        or not proxy_token
        or not hmac.compare_digest(proxy_token.encode("utf-8"), configured_token.encode("utf-8"))
    ):
        raise HTTPException(status_code=403, detail="Worker identity is not authorized")
    try:
        normalized_ip = str(ipaddress.ip_address(remote_ip))
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Worker identity is not authorized") from exc
    worker_id = settings.worker_ip_map.get(normalized_ip)
    if worker_id is None:
        raise HTTPException(status_code=403, detail="Worker identity is not authorized")
    return worker_id


def require_dashboard(authorization: str | None) -> None:
    expected = settings.dashboard_token.get_secret_value()
    scheme, separator, supplied = (authorization or "").partition(" ")
    if (
        not expected
        or not separator
        or scheme.lower() != "bearer"
        or not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))
    ):
        raise HTTPException(status_code=403, detail="Admin authentication required")


async def litellm_request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    allow_404: bool = False,
) -> Any:
    master_key = settings.litellm_master_key.get_secret_value()
    if not master_key:
        raise HTTPException(status_code=503, detail="LiteLLM administration is not configured")
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.request(
                method,
                f"{settings.litellm_base_url.rstrip('/')}{path}",
                headers={"Authorization": f"Bearer {master_key}", "Accept": "application/json"},
                params=params,
                json=body,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="LiteLLM is unavailable") from exc
    if response.status_code == 404 and allow_404:
        return {}
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="LiteLLM rejected the Keybroker request")
    try:
        return response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="LiteLLM returned an invalid response") from exc


def upstream_key_id(result: dict[str, Any]) -> tuple[str, str | None, str | None]:
    token_id = result.get("token_id")
    token_hash = result.get("token_hash")
    if not isinstance(token_id, str) or not SAFE_ID.fullmatch(token_id) or token_id.lower().startswith("sk-"):
        token_id = None
    if not isinstance(token_hash, str) or not SAFE_ID.fullmatch(token_hash) or token_hash.lower().startswith("sk-"):
        token_hash = None
    key_id = token_id or token_hash
    if key_id is None:
        raise HTTPException(status_code=502, detail="LiteLLM did not return a safe key identifier")
    return key_id, token_id, token_hash


def created_key(result: Any) -> tuple[str, str, str | None, str | None]:
    if not isinstance(result, dict):
        raise HTTPException(status_code=502, detail="LiteLLM returned an invalid key result")
    key = result.get("key")
    if not isinstance(key, str) or not key:
        raise HTTPException(status_code=502, detail="LiteLLM did not return a new key")
    key_id, token_id, token_hash = upstream_key_id(result)
    return key, key_id, token_id, token_hash


def lite_duration(seconds: int) -> str:
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def safe_number(value: Any) -> float:
    try:
        parsed = float(value)
        return parsed if isfinite(parsed) else 0.0
    except (TypeError, ValueError):
        return 0.0


MODEL_PRICING_CACHE_TTL_SECONDS = 30.0
MODEL_PRICING_CACHE: dict[str, tuple[float, bool]] = {}


def model_rows_have_budgetable_pricing(rows: list[dict[str, Any]]) -> bool:
    for row in rows:
        info = row.get("model_info") if isinstance(row.get("model_info"), dict) else {}
        params = row.get("litellm_params") if isinstance(row.get("litellm_params"), dict) else {}
        input_cost = safe_number(info.get("input_cost_per_token", row.get("input_cost_per_token")))
        output_cost = safe_number(info.get("output_cost_per_token", row.get("output_cost_per_token")))
        if input_cost > 0 and output_cost > 0:
            continue
        api_base = params.get("api_base") or row.get("api_base")
        normalized_base = api_base.rstrip("/") if isinstance(api_base, str) else ""
        if normalized_base not in settings.allow_unpriced_local_model_bases:
            return False
    return True


async def require_budgetable_model(model: str) -> None:
    cached = MODEL_PRICING_CACHE.get(model)
    now_monotonic = time.monotonic()
    if cached and now_monotonic - cached[0] < MODEL_PRICING_CACHE_TTL_SECONDS:
        if cached[1]:
            return
        raise HTTPException(
            status_code=503,
            detail="LiteLLM pricing is missing or zero; virtual-key issuance is blocked",
        )

    rows = key_rows(await litellm_request("GET", "/model/info"))
    matching = [row for row in rows if row.get("model_name") == model]
    if not matching:
        raise HTTPException(
            status_code=503,
            detail="LiteLLM model pricing metadata is unavailable; virtual-key issuance is blocked",
        )

    if not model_rows_have_budgetable_pricing(matching):
        try:
            await litellm_request("POST", "/reload/model_cost_map")
            rows = key_rows(await litellm_request("GET", "/model/info"))
            matching = [row for row in rows if row.get("model_name") == model]
        except HTTPException:
            matching = []
        if not matching or not model_rows_have_budgetable_pricing(matching):
            MODEL_PRICING_CACHE[model] = (now_monotonic, False)
            raise HTTPException(
                status_code=503,
                detail="LiteLLM pricing is missing or zero after model-cost-map refresh; virtual-key issuance is blocked",
            )

    MODEL_PRICING_CACHE[model] = (now_monotonic, True)

def key_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        rows = payload.get("keys", payload.get("data", []))
    else:
        rows = payload
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def safe_key_row(row: dict[str, Any], now: datetime) -> dict[str, Any]:
    token_id = row.get("token_id")
    token_hash = row.get("token_hash")
    if isinstance(token_id, str) and SAFE_ID.fullmatch(token_id) and not token_id.lower().startswith("sk-"):
        key_id = token_id
    elif isinstance(token_hash, str) and SAFE_ID.fullmatch(token_hash) and not token_hash.lower().startswith("sk-"):
        key_id = token_hash
    else:
        key_id = None
    alias = row.get("key_alias") or row.get("key_name") or "Unnamed key"
    models = row.get("models") if isinstance(row.get("models"), list) else []
    created = row.get("created_at") if isinstance(row.get("created_at"), str) else None
    age_days: int | None = None
    if created:
        try:
            parsed = datetime.fromisoformat(created.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            age_days = max(0, (now - parsed.astimezone(timezone.utc)).days)
        except ValueError:
            created = None
    return {
        "key_id": key_id,
        "alias": alias[:100] if isinstance(alias, str) else "Unnamed key",
        "models": [model for model in models if isinstance(model, str)][:10],
        "spend_usd": safe_number(row.get("spend")),
        "max_budget_usd": safe_number(row.get("max_budget")) if row.get("max_budget") is not None else None,
        "budget_duration": row.get("budget_duration") if isinstance(row.get("budget_duration"), str) else None,
        "created_at": created,
        "age_days": age_days,
        "expires": row.get("expires") if isinstance(row.get("expires"), str) else None,
        "blocked": bool(row.get("blocked")),
        "team_id": row.get("team_id") if isinstance(row.get("team_id"), str) else None,
    }


def safe_session_row(record: SessionRecord, now: datetime) -> dict[str, Any]:
    token_id = record.token_id
    token_hash = record.token_hash
    key_id = (
        token_id
        if isinstance(token_id, str) and SAFE_ID.fullmatch(token_id) and not token_id.lower().startswith("sk-")
        else token_hash
        if isinstance(token_hash, str) and SAFE_ID.fullmatch(token_hash) and not token_hash.lower().startswith("sk-")
        else None
    )
    alias = record.key_alias
    if not isinstance(alias, str) or not SAFE_ALIAS.fullmatch(alias) or alias.lower().startswith("sk-"):
        alias = "restricted-session"
    policy = record.policy if isinstance(record.policy, dict) else {}
    model = policy.get("model") if isinstance(policy.get("model"), str) else None
    if model and (not SAFE_ALIAS.fullmatch(model) or model.lower().startswith("sk-")):
        model = None
    expires_at = normalize_datetime(record.expires_at)
    created_at = normalize_datetime(record.created_at)
    revoked_at = normalize_datetime(record.revoked_at) if record.revoked_at else None
    status = "revoked" if revoked_at else "expired" if expires_at <= now else "active"
    return {
        "session_id": record.session_id,
        "worker_id": record.worker_id if SAFE_ID.fullmatch(record.worker_id) else "unmapped",
        "key_id": key_id,
        "alias": alias,
        "model": model,
        "max_budget_usd": safe_number(policy.get("max_budget_usd")),
        "budget_duration": policy.get("budget_duration") if isinstance(policy.get("budget_duration"), str) else None,
        "created_at": created_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "revoked_at": revoked_at.isoformat() if revoked_at else None,
        "status": status,
    }


def parse_spend_time(value: str, field_name: str) -> str:
    try:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{field_name} must be an ISO timestamp or YYYY-MM-DD HH:MM:SS") from exc
    return normalize_datetime(parsed).strftime("%Y-%m-%d %H:%M:%S")


def spend_rows(payload: Any) -> tuple[list[dict[str, Any]], int, bool]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise HTTPException(status_code=502, detail="LiteLLM returned invalid spend logs")
    safe: list[dict[str, Any]] = []
    for row in payload["data"]:
        if not isinstance(row, dict):
            continue
        safe_row: dict[str, Any] = {}
        for field in ("startTime", "spend", "prompt_tokens", "completion_tokens", "total_tokens"):
            value = row.get(field)
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                safe_row[field] = value
        api_key = row.get("api_key")
        if isinstance(api_key, str) and SAFE_ID.fullmatch(api_key) and not api_key.lower().startswith("sk-"):
            safe_row["api_key"] = api_key
        model = row.get("model")
        if (
            isinstance(model, str)
            and SAFE_MODEL.fullmatch(model)
            and not model.casefold().startswith(("sk-", "AIza"))
            and not SAFE_MODEL_SECRET.search(model)
        ):
            safe_row["model"] = model
        safe.append(safe_row)
    try:
        total_pages = max(1, int(payload.get("total_pages", 1)))
    except (TypeError, ValueError):
        total_pages = 1
    return safe, total_pages, bool(payload.get("total_is_capped"))


def signed_event(event: dict[str, Any]) -> tuple[bytes, str]:
    raw = json.dumps(event, separators=(",", ":"), sort_keys=True).encode("utf-8")
    secret = settings.exposure_webhook_secret.get_secret_value()
    if not secret:
        raise HTTPException(status_code=503, detail="Exposure webhook is not configured")
    signature = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return raw, f"sha256={signature}"


async def forward_exposure(event: dict[str, Any]) -> bool:
    raw, signature = signed_event(event)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                settings.exposure_webhook_url,
                content=raw,
                headers={"Content-Type": "application/json", "X-Darkfactory-Signature": signature},
            )
    except httpx.HTTPError:
        logger.warning("Keybroker could not deliver exposure event %s", event.get("event_id", "unknown"))
        return False
    if response.status_code >= 400:
        logger.warning("API Guard rejected exposure event %s", event.get("event_id", "unknown"))
        return False
    return True


def queue_exposure_event(event: dict[str, Any]) -> None:
    now = utcnow()
    with Session(engine) as session:
        record = session.get(ExposureOutboxRecord, event["event_id"])
        if record is None:
            session.add(
                ExposureOutboxRecord(
                    event_id=event["event_id"],
                    payload=event,
                    attempts=0,
                    next_attempt_at=now,
                    created_at=now,
                )
            )
            session.commit()


async def deliver_exposure_event(event_id: str) -> bool:
    with Session(engine) as session:
        record = session.get(ExposureOutboxRecord, event_id)
        if record is None:
            return False
        if record.delivered_at is not None:
            return True
        if normalize_datetime(record.next_attempt_at) > utcnow():
            return False
        event = dict(record.payload)

    delivered = await forward_exposure(event)
    now = utcnow()
    with Session(engine) as session:
        record = session.get(ExposureOutboxRecord, event_id)
        if record is None or record.delivered_at is not None:
            return delivered
        attempts = record.attempts + 1
        record.attempts = attempts
        if delivered:
            record.delivered_at = now
        else:
            delay_seconds = min(60 * (2 ** min(attempts - 1, 6)), 3600)
            record.next_attempt_at = now + timedelta(seconds=delay_seconds)
        session.commit()
    return delivered


async def deliver_pending_exposure_events_once() -> int:
    now = utcnow()
    with Session(engine) as session:
        event_ids = list(
            session.execute(
                select(ExposureOutboxRecord.event_id)
                .where(
                    ExposureOutboxRecord.delivered_at.is_(None),
                    ExposureOutboxRecord.next_attempt_at <= now,
                )
                .order_by(ExposureOutboxRecord.created_at)
                .limit(100)
            ).scalars()
        )
    delivered = 0
    for event_id in event_ids:
        if await deliver_exposure_event(event_id):
            delivered += 1
    return delivered


async def revoke_litellm_key(key_id: str, *, not_found_ok: bool = False) -> None:
    await litellm_request(
        "POST",
        "/key/delete",
        body={"keys": [key_id]},
        allow_404=not_found_ok,
    )

async def cleanup_untracked_key(key_id: str) -> None:
    try:
        await revoke_litellm_key(key_id, not_found_ok=True)
    except HTTPException:
        logger.warning("Keybroker could not clean up an untracked LiteLLM key")



def expired_session_keys(now: datetime, limit: int = 100) -> list[tuple[str, str | None, str | None]]:
    with engine.connect() as connection:
        rows = connection.execute(
            select(SessionRecord.session_id, SessionRecord.token_id, SessionRecord.token_hash)
            .where(SessionRecord.expires_at <= now, SessionRecord.revoked_at.is_(None))
            .order_by(SessionRecord.expires_at)
            .limit(limit)
        )
        return [(row.session_id, row.token_id, row.token_hash) for row in rows]


async def reap_expired_sessions_once() -> int:
    expired = expired_session_keys(utcnow())
    revoked = 0
    for session_id, token_id, token_hash in expired:
        key_id = token_id or token_hash
        if key_id is None:
            continue
        try:
            await revoke_litellm_key(key_id, not_found_ok=True)
        except HTTPException:
            logger.warning("Keybroker could not revoke an expired LiteLLM key")
            continue
        try:
            mark_session_revoked(session_id, utcnow())
        except SQLAlchemyError:
            logger.warning("Keybroker could not persist expired-key revocation")
            continue
        revoked += 1
    return revoked


async def expiration_reaper() -> None:
    while True:
        await asyncio.sleep(settings.reaper_interval_seconds)
        try:
            await reap_expired_sessions_once()
            await deliver_pending_exposure_events_once()
        except Exception:
            logger.warning("Keybroker maintenance pass failed")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    Base.metadata.create_all(bind=engine)
    reaper_task = asyncio.create_task(expiration_reaper())
    try:
        yield
    finally:
        reaper_task.cancel()
        with suppress(asyncio.CancelledError):
            await reaper_task


app = FastAPI(title="Keybroker", docs_url=None, redoc_url=None, lifespan=lifespan)


@app.get("/healthz")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
def readiness() -> dict[str, str]:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Keybroker database is unavailable") from exc
    return {"status": "ready"}
@app.get("/v1/worker/identity")
def worker_identity(request: Request) -> dict[str, str]:
    return {"worker_id": require_worker(request)}




@app.post("/v1/sessions")
async def create_session(body: SessionRequest, request: Request, response: Response) -> dict[str, Any]:
    worker_id = require_worker(request)
    existing = get_session_for_run(worker_id, body.run_id)
    if existing is not None:
        if existing.revoked_at is None:
            key_id = existing.token_id or existing.token_hash
            if key_id is not None:
                try:
                    await revoke_litellm_key(key_id, not_found_ok=True)
                except HTTPException:
                    logger.warning("Keybroker could not clean up a duplicate-run session key")
                else:
                    try:
                        mark_session_revoked(existing.session_id, utcnow())
                    except SQLAlchemyError:
                        logger.warning("Keybroker could not persist duplicate-run key revocation")
        raise HTTPException(
            status_code=409,
            detail="A session already exists for this worker/run; no new key was issued",
        )
    now = utcnow()
    session_id = str(uuid.uuid4())
    alias = f"session-{worker_id}-{session_id[:12]}"
    ttl = settings.session_ttl_seconds
    model = settings.default_model
    await require_budgetable_model(model)
    budget = settings.session_max_budget_usd
    policy = {
        "model": model,
        "max_budget_usd": budget,
        "budget_duration": "30d",
        "ttl_seconds": ttl,
    }
    result = await litellm_request(
        "POST",
        "/key/generate",
        body={
            "key_alias": alias,
            "models": [model],
            "max_budget": budget,
            "budget_duration": "30d",
            "duration": lite_duration(ttl),
            "metadata": {"managed_by": "keybroker", "worker_id": worker_id, "session_id": session_id},
        },
    )
    key, key_id, token_id, token_hash = created_key(result)
    expires_at = now + timedelta(seconds=ttl)
    record = SessionRecord(
        session_id=session_id,
        worker_id=worker_id,
        run_id=body.run_id,
        token_id=token_id,
        token_hash=token_hash,
        key_alias=alias,
        policy=policy,
        created_at=now,
        expires_at=expires_at,
        revoked_at=None,
    )
    try:
        with engine.begin() as connection:
            connection.execute(
                SessionRecord.__table__.insert().values(
                    session_id=record.session_id,
                    worker_id=record.worker_id,
                    run_id=record.run_id,
                    token_id=record.token_id,
                    token_hash=record.token_hash,
                    key_alias=record.key_alias,
                    policy=record.policy,
                    created_at=record.created_at,
                    expires_at=record.expires_at,
                    revoked_at=record.revoked_at,
                )
            )
    except IntegrityError:
        await cleanup_untracked_key(key_id)
        raise HTTPException(
            status_code=409,
            detail="A session already exists for this worker/run; no new key was issued",
        ) from None
    except SQLAlchemyError:
        await cleanup_untracked_key(key_id)
        raise HTTPException(status_code=503, detail="Session credentials could not be stored") from None
    response.headers["Cache-Control"] = "no-store"
    return {
        "session_id": session_id,
        "key": key,
        "key_id": key_id,
        "key_alias": alias,
        "expires_at": expires_at.isoformat(),
        "model_base_url": settings.model_base_url.rstrip("/"),
        "model": model,
    }


@app.delete("/v1/sessions/{session_id}")
async def delete_session(session_id: str, request: Request) -> dict[str, Any]:
    worker_id = require_worker(request)
    try:
        uuid.UUID(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Session not found") from exc
    record = get_session(session_id)
    if record is None or record.worker_id != worker_id:
        raise HTTPException(status_code=404, detail="Session not found")
    if record.revoked_at is None:
        await revoke_litellm_key(record.token_id or record.token_hash or "")
        mark_session_revoked(session_id, utcnow())
    return {"revoked": True, "session_id": session_id}


@app.post("/v1/sessions/{session_id}/exposure", status_code=202)
async def report_exposure(body: ExposureRequest, session_id: str, request: Request) -> dict[str, Any]:
    worker_id = require_worker(request)
    try:
        uuid.UUID(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Session not found") from exc
    record = get_session(session_id)
    if record is None or record.worker_id != worker_id:
        raise HTTPException(status_code=404, detail="Session not found")

    revoked = record.revoked_at is not None
    if body.session_key_match and not revoked:
        try:
            await revoke_litellm_key(record.token_id or record.token_hash or "")
        except HTTPException:
            # The alert must still reach API Guard even if LiteLLM is temporarily unavailable.
            revoked = False
        else:
            revoked = True
            try:
                mark_session_revoked(session_id, utcnow())
            except SQLAlchemyError:
                # Keep the urgent API Guard alert flowing if local state cannot be updated.
                pass

    occurred_at = utcnow().isoformat()
    event = {
        "event_id": body.event_id,
        "key_id": (record.token_id or record.token_hash) if body.session_key_match else None,
        "key_alias": record.key_alias if body.session_key_match else "unmapped",
        "source": "worker-omp",
        "reason": {
            "session_key_echo_detected": "configured_credential_echo",
            "credential_pattern_detected": "credential_pattern_in_output",
        }.get(body.reason, body.reason),
        "occurred_at": occurred_at,
        "worker_id": worker_id,
        "session_id": session_id,
    }
    queue_exposure_event(event)
    notification_delivered = await deliver_exposure_event(body.event_id)
    return {
        "accepted": True,
        "event_id": body.event_id,
        "flagged": True,
        "revoked": revoked,
        "stop_run": True,
        "notification_pending": not notification_delivered,
    }


@app.get("/v1/admin/workers")
def admin_list_workers(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    require_dashboard(authorization)
    with Session(engine) as session:
        usage = session.execute(
            select(
                SessionRecord.worker_id,
                func.count(SessionRecord.session_id).label("session_count"),
                func.max(SessionRecord.created_at).label("last_session_at"),
            ).group_by(SessionRecord.worker_id)
        ).all()
    sessions_by_worker = {
        row.worker_id: {
            "session_count": int(row.session_count),
            "last_session_at": normalize_datetime(row.last_session_at).isoformat()
            if row.last_session_at
            else None,
        }
        for row in usage
    }
    workers = [
        {
            "worker_id": worker_id,
            "host": WORKER_NAMES.get(worker_id, "unmapped"),
            "tailnet_ip": address,
            **sessions_by_worker.get(worker_id, {"session_count": 0, "last_session_at": None}),
        }
        for address, worker_id in settings.worker_ip_map.items()
    ]
    workers.sort(key=lambda row: row["host"])
    return {"data": workers, "total": len(workers)}

@app.get("/v1/admin/keys")
async def admin_list_keys(authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
    require_dashboard(authorization)
    rows = key_rows(await litellm_request("GET", "/key/list"))
    now = utcnow()
    return [safe_key_row(row, now) for row in rows]


@app.get("/v1/admin/sessions")
def admin_list_sessions(
    start: str = Query(min_length=1, max_length=64),
    end: str = Query(min_length=1, max_length=64),
    page: int = Query(default=1, ge=1, le=10000),
    page_size: int = Query(default=1000, ge=1, le=1000),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_dashboard(authorization)
    start_value = datetime.strptime(
        parse_spend_time(start, "start"), "%Y-%m-%d %H:%M:%S"
    ).replace(tzinfo=timezone.utc)
    end_value = datetime.strptime(
        parse_spend_time(end, "end"), "%Y-%m-%d %H:%M:%S"
    ).replace(tzinfo=timezone.utc)
    if end_value < start_value:
        raise HTTPException(status_code=422, detail="end must not precede start")
    conditions = (
        SessionRecord.created_at >= start_value,
        SessionRecord.created_at <= end_value,
    )
    with Session(engine) as session:
        total = session.scalar(
            select(func.count()).select_from(SessionRecord).where(*conditions)
        ) or 0
        records = session.scalars(
            select(SessionRecord)
            .where(*conditions)
            .order_by(SessionRecord.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
    total_pages = max(1, (total + page_size - 1) // page_size)
    now = utcnow()
    return {
        "data": [safe_session_row(record, now) for record in records],
        "total": total,
        "total_pages": total_pages,
        "total_is_capped": False,
    }

@app.get("/v1/admin/spend-logs")
async def admin_spend_logs(
    start: str = Query(min_length=1, max_length=64),
    end: str = Query(min_length=1, max_length=64),
    page: int = Query(default=1, ge=1, le=10000),
    page_size: int = Query(default=1000, ge=1, le=1000),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_dashboard(authorization)
    start_value = parse_spend_time(start, "start")
    end_value = parse_spend_time(end, "end")
    if end_value < start_value:
        raise HTTPException(status_code=422, detail="end must not precede start")
    payload = await litellm_request(
        "GET",
        "/spend/logs/v2",
        params={
            "start_date": start_value,
            "end_date": end_value,
            "page": page,
            "page_size": page_size,
            "sort_by": "startTime",
            "sort_order": "desc",
            "exclude_internal_health_checks": "true",
        },
    )
    data, total_pages, total_is_capped = spend_rows(payload)
    return {"data": data, "total_pages": total_pages, "total_is_capped": total_is_capped}


@app.post("/v1/admin/keys/generate")
async def admin_generate_key(body: AdminKeyGenerate, response: Response, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    require_dashboard(authorization)
    if body.max_budget_usd > settings.admin_max_budget_usd:
        raise HTTPException(status_code=422, detail="Requested key budget exceeds the configured administrator limit")
    if any(model not in settings.allowed_models for model in body.models):
        raise HTTPException(status_code=422, detail="Requested model is not allowed by Keybroker policy")
    for model in body.models:
        await require_budgetable_model(model)
    result = await litellm_request(
        "POST",
        "/key/generate",
        body={
            "key_alias": body.alias,
            "models": body.models,
            "max_budget": body.max_budget_usd,
            "budget_duration": body.budget_duration,
            "duration": body.duration,
            "metadata": {"managed_by": "keybroker-admin"},
        },
    )
    key, key_id, _, _ = created_key(result)
    response.headers["Cache-Control"] = "no-store"
    return {"key": key, "key_id": key_id, "alias": body.alias}


@app.post("/v1/admin/keys/revoke")
async def admin_revoke_key(body: AdminKeyReference, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    require_dashboard(authorization)
    await revoke_litellm_key(body.key_id)
    mark_key_revoked(body.key_id, utcnow())
    return {"revoked": True, "key_id": body.key_id}
