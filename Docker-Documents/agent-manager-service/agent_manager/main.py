from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncGenerator

import nats
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from nats.errors import NoRespondersError, TimeoutError as NatsTimeoutError
from nats.js.api import (
    AckPolicy,
    ConsumerConfig,
    DeliverPolicy,
    RetentionPolicy,
    StorageType,
    StreamConfig,
)
from nats.js.errors import NotFoundError
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import DateTime, JSON, String, desc, func, select, text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from prometheus_client import Counter, make_asgi_app

RUNS_CREATED = Counter(
    "agent_runs_created_total",
    "Number of agent runs successfully published to the queue.",
)
RUNS_COMPLETED = Counter(
    "agent_runs_completed_total",
    "Number of agent runs completed by a worker.",
)
RUNS_FAILED = Counter(
    "agent_runs_failed_total",
    "Number of agent runs failed by a worker.",
)

class Settings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore")

    database_url: str | None = Field(default=None, validation_alias="DATABASE_URL")
    database_host: str = Field(default="postgres", validation_alias="DB_HOST")
    database_port: int = Field(default=5432, validation_alias="DB_PORT")
    database_name: str = Field(default="agent_manager", validation_alias="DB_NAME")
    database_user: str = Field(default="agent_manager", validation_alias="DB_USER")
    database_password: str = Field(
        default="agent_manager", validation_alias="DB_PASSWORD"
    )
    nats_url: str = Field(
        default="nats://nats:4222",
        validation_alias="NATS_URL",
    )
    api_token: str = Field(
        validation_alias="AGENT_MANAGER_API_TOKEN",
    )
    forgejo_api_url: str = Field(
        default="http://forgejo:3000/api/v1",
        validation_alias="FORGEJO_API_URL",
    )
    forgejo_token: str = Field(default="", validation_alias="FORGEJO_TOKEN")
    model_base_url: str = Field(
        default="http://litellm:4000/v1",
        validation_alias="MODEL_BASE_URL",
    )
    model_api_key: str = Field(default="", validation_alias="MODEL_API_KEY")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")


settings = Settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("agent-manager")

STREAM_NAME = "AGENT_RUNS"
RUN_CREATED_SUBJECT = "agent.runs.created"
RUN_COMPLETED_SUBJECT = "agent.runs.completed"
RUN_CANCELLED_SUBJECT = "agent.runs.cancelled"


class Base(DeclarativeBase):
    pass


class RunRecord(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    goal: Mapped[str] = mapped_column(String(20000))
    forgejo_repository: Mapped[str] = mapped_column(String(512))
    base_ref: Mapped[str] = mapped_column(String(256), default="main")
    model: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(64), default="queued")
    error: Mapped[str | None] = mapped_column(String(4000), nullable=True)
    run_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class RunCreate(BaseModel):
    goal: str = Field(min_length=1, max_length=20000)
    forgejo_repository: str = Field(min_length=1, max_length=512)
    base_ref: str = Field(default="main", min_length=1, max_length=256)
    model: str | None = Field(default=None, max_length=512)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    goal: str
    forgejo_repository: str
    base_ref: str
    model: str | None
    status: str
    error: str | None
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, record: RunRecord) -> "RunOut":
        return cls(
            id=record.id,
            goal=record.goal,
            forgejo_repository=record.forgejo_repository,
            base_ref=record.base_ref,
            model=record.model,
            status=record.status,
            error=record.error,
            metadata=record.run_metadata or {},
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


class HealthOut(BaseModel):
    status: str
    database: str
    nats: str


database_url = settings.database_url or URL.create(
    drivername="postgresql+asyncpg",
    username=settings.database_user,
    password=settings.database_password,
    host=settings.database_host,
    port=settings.database_port,
    database=settings.database_name,
)
engine: AsyncEngine = create_async_engine(
    database_url,
    pool_pre_ping=True,
    pool_recycle=1800,
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
bearer = HTTPBearer(auto_error=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session


async def require_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> None:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    expected = settings.api_token.encode("utf-8")
    supplied = credentials.credentials.encode("utf-8")
    if not expected or not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def initialise_database() -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def connect_nats_with_retry() -> nats.NATS:
    last_error: Exception | None = None
    for attempt in range(1, 31):
        try:
            connection = await nats.connect(
                servers=[settings.nats_url],
                name="agent-manager",
                reconnect_time_wait=2,
                max_reconnect_attempts=-1,
            )
            logger.info("connected to NATS at %s", settings.nats_url)
            return connection
        except Exception as exc:  # NATS can still be starting when the app starts.
            last_error = exc
            logger.warning("NATS connection attempt %d failed: %s", attempt, exc)
            await asyncio.sleep(2)

    raise RuntimeError("unable to connect to NATS") from last_error


async def ensure_stream(connection: nats.NATS) -> None:
    jetstream = connection.jetstream()
    try:
        await jetstream.stream_info(STREAM_NAME)
        return
    except NotFoundError:
        pass

    await jetstream.add_stream(
        config=StreamConfig(
            name=STREAM_NAME,
            subjects=["agent.runs.>"],
            storage=StorageType.FILE,
            retention=RetentionPolicy.LIMITS,
        )
    )
    logger.info("created NATS JetStream stream %s", STREAM_NAME)


async def publish_run_event(request: Request, subject: str, payload: dict[str, Any]) -> None:
    connection: nats.NATS = request.app.state.nats
    message_id = payload["run_id"]
    await connection.jetstream().publish(
        subject,
        json.dumps(payload, default=str).encode("utf-8"),
        headers={"Nats-Msg-Id": f"{subject}:{message_id}"},
    )


async def find_available_worker(
    connection: nats.NATS,
    worker_role: str,
    target_worker: str | None,
) -> dict[str, Any] | None:
    """Return one live worker for a role, or None if none responds."""
    try:
        response = await connection.request(
            f"agent.workers.health.{worker_role}",
            json.dumps({"worker_id": target_worker}).encode("utf-8"),
            timeout=1,
        )
        worker = json.loads(response.data.decode("utf-8"))
    except (
        NatsTimeoutError,
        NoRespondersError,
        asyncio.TimeoutError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ):
        return None

    if worker.get("worker_role") != worker_role:
        return None
    if target_worker and worker.get("worker_id") != target_worker:
        return None
    return worker


async def apply_run_completion(payload: dict[str, Any]) -> None:
    run_id = payload.get("run_id")
    worker_status = payload.get("status")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("completion event is missing run_id")
    if worker_status not in {"completed", "failed"}:
        raise ValueError(f"unsupported worker status: {worker_status!r}")

    async with SessionLocal() as session:
        record = await session.get(RunRecord, run_id)
        if record is None:
            logger.warning("completion received for unknown run %s", run_id)
            return

        previous_status = record.status
        metadata = dict(record.run_metadata or {})
        completion = dict(payload)
        completion.pop("run_id", None)
        metadata["result"] = completion
        record.run_metadata = metadata
        record.status = worker_status

        if worker_status == "failed":
            error = payload.get("error") or payload.get("stderr") or "worker failed"
            record.error = str(error)[:4000]
        else:
            record.error = None

        await session.commit()

    # JetStream may redeliver a message after a manager restart. Do not count
    # the same terminal transition more than once in the process lifetime.
    if previous_status not in {"completed", "failed"}:
        if worker_status == "completed":
            RUNS_COMPLETED.inc()
        else:
            RUNS_FAILED.inc()

    logger.info(
        "updated run %s from worker %s: %s",
        run_id,
        payload.get("worker_id", "unknown"),
        worker_status,
    )


async def consume_completion_events(connection: nats.NATS) -> None:
    subscription = await connection.jetstream().pull_subscribe(
        RUN_COMPLETED_SUBJECT,
        stream=STREAM_NAME,
        durable="agent-manager-completions",
        config=ConsumerConfig(
            ack_policy=AckPolicy.EXPLICIT,
            deliver_policy=DeliverPolicy.ALL,
        ),
    )
    logger.info("started durable completion consumer")

    try:
        while True:
            try:
                messages = await subscription.fetch(1, timeout=1)
            except NatsTimeoutError:
                continue

            try:
                message = messages[0]
                payload = json.loads(message.data.decode("utf-8"))
                await apply_run_completion(payload)
                await message.ack()
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                logger.exception("discarding invalid completion event")
                await message.ack()
            except Exception:
                # Leave transient database errors unacknowledged so JetStream
                # can redeliver the completion after its ack wait expires.
                logger.exception("failed to process completion event")
    finally:
        await subscription.unsubscribe()
        logger.info("stopped durable completion consumer")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await initialise_database()
    connection = await connect_nats_with_retry()
    await ensure_stream(connection)
    app.state.nats = connection
    completion_task = asyncio.create_task(consume_completion_events(connection))
    app.state.completion_task = completion_task
    logger.info("agent manager started")
    try:
        yield
    finally:
        completion_task.cancel()
        await asyncio.gather(completion_task, return_exceptions=True)
        await connection.drain()
        await engine.dispose()
        logger.info("agent manager stopped")


app = FastAPI(
    title="Agent Manager",
    version=os.getenv("AGENT_MANAGER_VERSION", "0.1.0"),
    lifespan=lifespan,
)
metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)


@app.get("/healthz", response_model=HealthOut)
async def healthz() -> HealthOut:
    return HealthOut(status="ok", database="not_checked", nats="not_checked")


@app.get("/readyz", response_model=HealthOut)
async def readyz(request: Request) -> HealthOut:
    database_status = "ok"
    nats_status = "ok"

    try:
        async with SessionLocal() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        database_status = "unavailable"

    connection: nats.NATS | None = getattr(request.app.state, "nats", None)
    if connection is None or connection.is_closed or not connection.is_connected:
        nats_status = "unavailable"

    overall = "ok" if database_status == "ok" and nats_status == "ok" else "unavailable"
    if overall != "ok":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": overall,
                "database": database_status,
                "nats": nats_status,
            },
        )
    return HealthOut(status=overall, database=database_status, nats=nats_status)


@app.post(
    "/v1/runs",
    response_model=RunOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_token)],
)
async def create_run(
    body: RunCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RunOut:
    worker_role = body.metadata.get("worker_role", "coder")
    target_worker = body.metadata.get("worker_id")
    if (
        not isinstance(worker_role, str)
        or not worker_role
        or not worker_role.replace("-", "").replace("_", "").isalnum()
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="metadata.worker_role must be a simple worker role name",
        )
    if target_worker is not None and (
        not isinstance(target_worker, str) or not target_worker
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="metadata.worker_id must be a non-empty string",
        )

    worker = await find_available_worker(
        request.app.state.nats,
        worker_role,
        target_worker,
    )
    if worker is None:
        target_text = f" {target_worker!r}" if target_worker else ""
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "NO_WORKER_AVAILABLE",
                "message": (
                    f"No active{target_text} worker is available for role "
                    f"{worker_role!r}. Start a worker and retry."
                ),
            },
            headers={"Retry-After": "15"},
        )

    run_id = str(uuid.uuid4())
    record = RunRecord(
        id=run_id,
        goal=body.goal,
        forgejo_repository=body.forgejo_repository,
        base_ref=body.base_ref,
        model=body.model,
        status="queued",
        run_metadata=body.metadata,
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)

    event = {
        "event": "run.created",
        "run_id": run_id,
        "goal": body.goal,
        "forgejo_repository": body.forgejo_repository,
        "base_ref": body.base_ref,
        "model": body.model,
        "metadata": body.metadata,
    }
    try:
        await publish_run_event(request, RUN_CREATED_SUBJECT, event)
        RUNS_CREATED.inc()
    except Exception as exc:
        logger.exception("failed to publish run %s", run_id)
        record.status = "failed"
        record.error = f"failed to enqueue run: {exc}"[:4000]
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="run could not be queued",
        ) from exc

    return RunOut.from_record(record)


@app.get(
    "/v1/runs/{run_id}",
    response_model=RunOut,
    dependencies=[Depends(require_token)],
)
async def get_run(
    run_id: str,
    session: AsyncSession = Depends(get_session),
) -> RunOut:
    record = await session.get(RunRecord, run_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    return RunOut.from_record(record)


@app.get(
    "/v1/runs",
    response_model=list[RunOut],
    dependencies=[Depends(require_token)],
)
async def list_runs(
    session: AsyncSession = Depends(get_session),
    run_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[RunOut]:
    query = select(RunRecord).order_by(desc(RunRecord.created_at)).limit(limit)
    if run_status:
        query = query.where(RunRecord.status == run_status)
    result = await session.execute(query)
    return [RunOut.from_record(record) for record in result.scalars().all()]


@app.post(
    "/v1/runs/{run_id}/cancel",
    response_model=RunOut,
    dependencies=[Depends(require_token)],
)
async def cancel_run(
    run_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RunOut:
    record = await session.get(RunRecord, run_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")

    if record.status in {"completed", "failed", "cancelled"}:
        return RunOut.from_record(record)

    record.status = "cancel_requested"
    await session.commit()
    await session.refresh(record)

    try:
        await publish_run_event(
            request,
            RUN_CANCELLED_SUBJECT,
            {"event": "run.cancelled", "run_id": run_id},
        )
    except Exception as exc:
        logger.exception("failed to publish cancellation for run %s", run_id)
        record.error = f"failed to publish cancellation: {exc}"[:4000]
        await session.commit()

    return RunOut.from_record(record)
