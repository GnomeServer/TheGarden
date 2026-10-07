from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import nats
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from nats.errors import NoRespondersError, TimeoutError as NatsTimeoutError
from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy, RetentionPolicy, StorageType, StreamConfig
from nats.js.errors import NotFoundError
from prometheus_client import Counter, Gauge, make_asgi_app
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import desc, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import current_user, require_csrf, require_run_submission, run_client
from .dashboard import activity_out, record_activity, router as dashboard_router, worker_out
from .database import SessionLocal, engine, get_session, initialise_database
from .live import live_broker
from .models import ActivityRecord, RunRecord, UserRecord, WorkerRecord
from .settings import settings
from .tracking import router as tracking_router, scan_deadline_notifications

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("agent-manager")

RUN_STREAM = "AGENT_RUNS"
WORKER_STREAM = "AGENT_WORKERS"
RUN_CREATED_SUBJECT = "agent.runs.created"
RUN_STATUS_SUBJECT = "agent.runs.status"
RUN_COMPLETED_SUBJECT = "agent.runs.completed"
RUN_CANCELLED_SUBJECT = "agent.runs.cancelled"
WORKER_SUBJECT = "agent.workers.>"

RUNS_CREATED = Counter("agent_runs_created_total", "Runs successfully published to the queue.")
RUNS_COMPLETED = Counter("agent_runs_completed_total", "Runs completed by a worker.")
RUNS_FAILED = Counter("agent_runs_failed_total", "Runs failed by a worker.")
WORKER_EVENTS = Counter("agent_worker_events_total", "Valid worker state events consumed.", ["status"])
WORKERS_ONLINE = Gauge("agent_workers_online", "Workers seen within the online threshold.")
WORKERS_BUSY = Gauge("agent_workers_busy", "Online workers reporting busy state.")
TASKS_OVERDUE = Gauge("agent_tasks_overdue", "Non-terminal tasks past their deadline.")


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
        except Exception as exc:
            last_error = exc
            logger.warning("NATS connection attempt %d failed: %s", attempt, exc)
            await asyncio.sleep(2)
    raise RuntimeError("unable to connect to NATS") from last_error


async def ensure_stream(connection: nats.NATS, name: str, subjects: list[str]) -> None:
    jetstream = connection.jetstream()
    try:
        info = await jetstream.stream_info(name)
        configured = set(info.config.subjects or [])
        if configured != set(subjects):
            config = info.config
            config.subjects = subjects
            await jetstream.update_stream(config=config)
        return
    except NotFoundError:
        pass
    await jetstream.add_stream(
        config=StreamConfig(
            name=name,
            subjects=subjects,
            storage=StorageType.FILE,
            retention=RetentionPolicy.LIMITS,
        )
    )
    logger.info("created NATS JetStream stream %s", name)


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
    """Probe a live worker before accepting work; this is not an execution lease."""
    try:
        response = await connection.request(
            f"agent.workers.health.{worker_role}",
            json.dumps({"worker_id": target_worker}).encode("utf-8"),
            timeout=1,
        )
        worker = json.loads(response.data.decode("utf-8"))
    except (
        NoRespondersError,
        NatsTimeoutError,
        asyncio.TimeoutError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ):
        return None
    if not isinstance(worker, dict) or worker.get("worker_role") != worker_role:
        return None
    if not isinstance(worker.get("worker_id"), str) or not worker["worker_id"]:
        return None
    if target_worker and worker["worker_id"] != target_worker:
        return None
    return worker


async def apply_run_status(payload: dict[str, Any]) -> None:
    run_id = payload.get("run_id")
    worker_id = payload.get("worker_id")
    run_status = payload.get("status")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("status event is missing run_id")
    if not isinstance(worker_id, str) or not worker_id:
        raise ValueError("status event is missing worker_id")
    if run_status != "running":
        raise ValueError(f"unsupported run status: {run_status!r}")
    event_id = str(payload.get("event_id") or f"run-status:{run_id}:{run_status}")
    async with SessionLocal() as session:
        if await session.scalar(
            select(ActivityRecord.id).where(
                ActivityRecord.source == "worker", ActivityRecord.source_event_id == event_id
            )
        ):
            return
        record = await session.get(RunRecord, run_id)
        if record is None:
            logger.warning("status received for unknown run %s", run_id)
            return
        if record.status == "queued":
            record.status = "running"
        worker = await session.get(WorkerRecord, worker_id)
        if worker is not None:
            worker.reported_status = "busy"
            worker.current_run_id = run_id
        event = await record_activity(
            session,
            source="worker",
            source_event_id=event_id,
            event_type="run.running",
            worker_id=worker_id if worker is not None else None,
            run_id=run_id,
            summary=f"{worker_id} started run {run_id}",
        )
        await session.commit()
    await live_broker.publish("run", {"id": run_id, "status": "running", "worker_id": worker_id})
    await live_broker.publish("activity", activity_out(event))


async def apply_run_completion(payload: dict[str, Any]) -> None:
    run_id = payload.get("run_id")
    worker_status = payload.get("status")
    worker_id = payload.get("worker_id")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("completion event is missing run_id")
    if worker_status not in {"completed", "failed"}:
        raise ValueError(f"unsupported worker status: {worker_status!r}")
    event_id = str(payload.get("event_id") or f"run-completion:{run_id}:{worker_status}")
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
        record.error = (
            str(payload.get("error") or payload.get("stderr") or "worker failed")[:4000]
            if worker_status == "failed"
            else None
        )
        worker = await session.get(WorkerRecord, worker_id) if isinstance(worker_id, str) else None
        if worker is not None:
            worker.reported_status = "idle" if worker_status == "completed" else "degraded"
            worker.current_run_id = None
            worker.last_error = record.error
        existing = await session.scalar(
            select(ActivityRecord.id).where(
                ActivityRecord.source == "worker", ActivityRecord.source_event_id == event_id
            )
        )
        event = None
        if not existing:
            event = await record_activity(
                session,
                source="worker",
                source_event_id=event_id,
                event_type=f"run.{worker_status}",
                worker_id=worker.id if worker else None,
                run_id=run_id,
                summary=f"{worker_id or 'worker'} {worker_status} run {run_id}",
                payload={"error": record.error} if record.error else {},
            )
        await session.commit()
    if previous_status not in {"completed", "failed"}:
        (RUNS_COMPLETED if worker_status == "completed" else RUNS_FAILED).inc()
    await live_broker.publish("run", {"id": run_id, "status": worker_status, "worker_id": worker_id})
    if event is not None:
        await live_broker.publish("activity", activity_out(event))


async def apply_worker_event(payload: dict[str, Any]) -> None:
    event_id = payload.get("event_id")
    worker_id = payload.get("worker_id")
    worker_role = payload.get("worker_role")
    worker_status = payload.get("status")
    sent_at = payload.get("sent_at")
    if not all(isinstance(value, str) and value for value in (event_id, worker_id, worker_role, worker_status)):
        raise ValueError("worker event requires event_id, worker_id, worker_role, and status")
    if worker_status not in {"idle", "busy", "degraded"}:
        raise ValueError(f"unsupported worker status: {worker_status!r}")
    try:
        seen_at = datetime.fromisoformat(sent_at.replace("Z", "+00:00")) if isinstance(sent_at, str) else datetime.now(timezone.utc)
        if seen_at.tzinfo is None:
            seen_at = seen_at.replace(tzinfo=timezone.utc)
    except ValueError:
        seen_at = datetime.now(timezone.utc)
    capabilities = payload.get("capabilities")
    if not isinstance(capabilities, list) or not all(isinstance(item, str) for item in capabilities):
        capabilities = []
    async with SessionLocal() as session:
        worker = await session.get(WorkerRecord, worker_id)
        previous_status = worker.reported_status if worker else None
        was_online = bool(
            worker
            and worker.last_seen_at
            >= datetime.now(timezone.utc)
            - timedelta(seconds=settings.worker_offline_after_seconds)
        )
        if worker is None:
            worker = WorkerRecord(
                id=worker_id,
                role=worker_role,
                last_seen_at=seen_at,
            )
            session.add(worker)
        worker.role = worker_role
        worker.hostname = str(payload.get("hostname") or "")[:255]
        worker.reported_status = worker_status
        worker.current_run_id = payload.get("current_run_id") if isinstance(payload.get("current_run_id"), str) else None
        worker.version = str(payload.get("version") or "unknown")[:128]
        worker.capabilities = capabilities
        worker.last_error = str(payload.get("error"))[:2000] if payload.get("error") else None
        worker.last_seen_at = seen_at
        event = None
        if previous_status != worker_status:
            duplicate = await session.scalar(
                select(ActivityRecord.id).where(
                    ActivityRecord.source == "worker", ActivityRecord.source_event_id == event_id
                )
            )
            if not duplicate:
                event = await record_activity(
                    session,
                    source="worker",
                    source_event_id=event_id,
                    event_type="worker.registered" if previous_status is None else "worker.status",
                    worker_id=worker_id,
                    run_id=worker.current_run_id,
                    summary=f"{worker_id} is {worker_status}",
                    payload={"previous_status": previous_status, "status": worker_status},
                    occurred_at=seen_at,
                )
        await session.commit()
        await session.refresh(worker)
        output = worker_out(worker)
    WORKER_EVENTS.labels(status=worker_status).inc()
    if previous_status != worker_status or not was_online:
        await live_broker.publish("worker", output)
    if event is not None:
        await live_broker.publish("activity", activity_out(event))


async def consume_events(
    connection: nats.NATS,
    *,
    subject: str,
    stream: str,
    durable: str,
    handler: Any,
) -> None:
    subscription = await connection.jetstream().pull_subscribe(
        subject,
        stream=stream,
        durable=durable,
        config=ConsumerConfig(ack_policy=AckPolicy.EXPLICIT, deliver_policy=DeliverPolicy.ALL),
    )
    logger.info("started durable consumer %s", durable)
    try:
        while True:
            try:
                messages = await subscription.fetch(1, timeout=1)
            except NatsTimeoutError:
                continue
            message = messages[0]
            try:
                payload = json.loads(message.data.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("event body is not an object")
                await handler(payload)
                await message.ack()
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                logger.exception("discarding invalid event on %s", subject)
                await message.ack()
            except Exception:
                logger.exception("failed to process event on %s", subject)
    finally:
        await subscription.unsubscribe()
        logger.info("stopped durable consumer %s", durable)


async def refresh_operational_gauges() -> None:
    while True:
        try:
            cutoff = datetime.now(timezone.utc).timestamp() - settings.worker_offline_after_seconds
            async with SessionLocal() as session:
                online = await session.scalar(
                    select(func.count()).select_from(WorkerRecord).where(
                        func.extract("epoch", WorkerRecord.last_seen_at) >= cutoff
                    )
                )
                busy = await session.scalar(
                    select(func.count()).select_from(WorkerRecord).where(
                        func.extract("epoch", WorkerRecord.last_seen_at) >= cutoff,
                        WorkerRecord.reported_status == "busy",
                    )
                )
                overdue = await session.scalar(
                    text(
                        "SELECT count(*) FROM dashboard_tasks "
                        "WHERE deleted_at IS NULL AND due_at < now() "
                        "AND status NOT IN ('done', 'cancelled')"
                    )
                )
            WORKERS_ONLINE.set(online or 0)
            WORKERS_BUSY.set(busy or 0)
            TASKS_OVERDUE.set(overdue or 0)
        except Exception:
            logger.exception("failed to refresh operational gauges")
        await asyncio.sleep(15)


async def deadline_notification_loop() -> None:
    while True:
        try:
            sent = await scan_deadline_notifications()
            if sent:
                logger.info("sent %d deadline notifications", sent)
        except Exception:
            logger.exception("deadline notification scan failed")
        await asyncio.sleep(settings.notification_scan_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await initialise_database()
    connection = await connect_nats_with_retry()
    await ensure_stream(connection, RUN_STREAM, ["agent.runs.>"])
    # Health probes are Core NATS request/reply. A wildcard stream would reply
    # with a storage acknowledgement before the worker's actual health response.
    await ensure_stream(connection, WORKER_STREAM, ["agent.workers.heartbeat"])
    app.state.nats = connection
    tasks = [
        asyncio.create_task(
            consume_events(
                connection, subject=RUN_COMPLETED_SUBJECT, stream=RUN_STREAM,
                durable="agent-manager-completions", handler=apply_run_completion,
            )
        ),
        asyncio.create_task(
            consume_events(
                connection, subject=RUN_STATUS_SUBJECT, stream=RUN_STREAM,
                durable="agent-manager-status", handler=apply_run_status,
            )
        ),
        asyncio.create_task(
            consume_events(
                connection, subject=WORKER_SUBJECT, stream=WORKER_STREAM,
                durable="agent-manager-workers", handler=apply_worker_event,
            )
        ),
        asyncio.create_task(refresh_operational_gauges()),
        asyncio.create_task(deadline_notification_loop()),
    ]
    app.state.background_tasks = tasks
    logger.info("agent manager started")
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await connection.drain()
        await engine.dispose()
        logger.info("agent manager stopped")


app = FastAPI(
    title="Dark Factory Agent Manager",
    version=os.getenv("AGENT_MANAGER_VERSION", "0.2.0"),
    lifespan=lifespan,
)
app.include_router(dashboard_router)
app.include_router(tracking_router)
app.mount("/metrics", make_asgi_app())
static_dir = Path(__file__).with_name("static")
app.mount("/dashboard/static", StaticFiles(directory=static_dir), name="dashboard-static")


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse("/dashboard/")


@app.get("/dashboard", include_in_schema=False)
async def dashboard_redirect() -> RedirectResponse:
    return RedirectResponse("/dashboard/")


@app.get("/dashboard/", include_in_schema=False)
async def dashboard_page() -> FileResponse:
    return FileResponse(
        static_dir / "index.html",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


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
            detail={"status": overall, "database": database_status, "nats": nats_status},
        )
    return HealthOut(status=overall, database=database_status, nats=nats_status)


@app.post("/v1/runs", response_model=RunOut, status_code=202)
async def create_run(
    body: RunCreate,
    request: Request,
    actor: UserRecord = Depends(require_run_submission),
    session: AsyncSession = Depends(get_session),
) -> RunOut:
    worker_role = body.metadata.get("worker_role", "coder")
    target_worker = body.metadata.get("worker_id")
    if (
        not isinstance(worker_role, str)
        or not worker_role
        or not worker_role.replace("-", "").replace("_", "").isalnum()
    ):
        raise HTTPException(status_code=422, detail="metadata.worker_role must be a simple worker role name")
    if target_worker is not None and (not isinstance(target_worker, str) or not target_worker):
        raise HTTPException(status_code=422, detail="metadata.worker_id must be a non-empty string")
    worker = await find_available_worker(request.app.state.nats, worker_role, target_worker)
    if worker is None:
        target_text = f" {target_worker!r}" if target_worker else ""
        raise HTTPException(
            status_code=503,
            detail={
                "code": "NO_WORKER_AVAILABLE",
                "message": f"No active{target_text} worker is available for role {worker_role!r}. Start a worker and retry.",
            },
            headers={"Retry-After": "15"},
        )
    run_id = str(uuid.uuid4())
    metadata = {**body.metadata, "submitted_by": actor.id}
    if getattr(request.state, "auth_method", "") == "open-webui":
        metadata["source"] = "open-webui"
    record = RunRecord(
        id=run_id,
        goal=body.goal,
        forgejo_repository=body.forgejo_repository,
        base_ref=body.base_ref,
        model=body.model,
        status="queued",
        run_metadata=metadata,
    )
    session.add(record)
    await session.flush()
    await record_activity(
        session,
        source="run",
        source_event_id=f"run-created:{run_id}",
        event_type="run.created",
        actor_user_id=actor.id,
        run_id=run_id,
        repository=body.forgejo_repository,
        summary=f"{actor.display_name or actor.forgejo_login} created run {run_id}",
    )
    await session.commit()
    await session.refresh(record)
    event = {
        "event": "run.created",
        "event_id": str(uuid.uuid4()),
        "run_id": run_id,
        "goal": body.goal,
        "forgejo_repository": body.forgejo_repository,
        "base_ref": body.base_ref,
        "model": body.model,
        "metadata": metadata,
    }
    try:
        await publish_run_event(request, RUN_CREATED_SUBJECT, event)
        RUNS_CREATED.inc()
    except Exception as exc:
        logger.exception("failed to publish run %s", run_id)
        record.status = "failed"
        record.error = f"failed to enqueue run: {exc}"[:4000]
        await session.commit()
        raise HTTPException(status_code=503, detail="run could not be queued") from exc
    await live_broker.publish("run", {"id": run_id, "status": "queued"})
    return RunOut.from_record(record)


@app.get("/v1/runs/{run_id}", response_model=RunOut)
async def get_run(
    run_id: str,
    request: Request,
    user: UserRecord = Depends(run_client),
    session: AsyncSession = Depends(get_session),
) -> RunOut:
    record = await session.get(RunRecord, run_id)
    if record is None or (
        getattr(request.state, "auth_method", "") == "open-webui"
        and (record.run_metadata or {}).get("submitted_by") != user.id
    ):
        raise HTTPException(status_code=404, detail="run not found")
    return RunOut.from_record(record)


@app.get("/v1/runs", response_model=list[RunOut])
async def list_runs(
    run_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    user: UserRecord = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[RunOut]:
    del user
    query = select(RunRecord).order_by(desc(RunRecord.created_at)).limit(limit)
    if run_status:
        query = query.where(RunRecord.status == run_status)
    result = await session.execute(query)
    return [RunOut.from_record(record) for record in result.scalars().all()]


@app.post("/v1/runs/{run_id}/cancel", response_model=RunOut)
async def cancel_run(
    run_id: str,
    request: Request,
    actor: UserRecord = Depends(require_csrf),
    session: AsyncSession = Depends(get_session),
) -> RunOut:
    del actor
    record = await session.get(RunRecord, run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="run not found")
    if record.status in {"completed", "failed", "cancelled"}:
        return RunOut.from_record(record)
    record.status = "cancel_requested"
    await session.commit()
    await session.refresh(record)
    try:
        await publish_run_event(
            request,
            RUN_CANCELLED_SUBJECT,
            {"event": "run.cancelled", "event_id": str(uuid.uuid4()), "run_id": run_id},
        )
    except Exception as exc:
        logger.exception("failed to publish cancellation for run %s", run_id)
        record.error = f"failed to publish cancellation: {exc}"[:4000]
        await session.commit()
    await live_broker.publish("run", {"id": run_id, "status": "cancel_requested"})
    return RunOut.from_record(record)
