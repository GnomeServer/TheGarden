from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        1,
        (
            "CREATE INDEX IF NOT EXISTS ix_runs_status ON runs (status)",
            "CREATE INDEX IF NOT EXISTS ix_runs_created_at ON runs (created_at)",
            "CREATE INDEX IF NOT EXISTS ix_runs_worker_id ON runs (worker_id)",
        ),
    ),
    (
        2,
        (
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_work_sessions_running_user "
            "ON work_sessions (user_id) WHERE status = 'running'",
            "CREATE INDEX IF NOT EXISTS ix_access_events_service_time "
            "ON access_events (service, occurred_at)",
            "CREATE INDEX IF NOT EXISTS ix_notifications_task_kind "
            "ON notifications (task_id, kind)",
        ),
    ),
    (
        3,
        (
            "ALTER TABLE runs ADD COLUMN IF NOT EXISTS worker_id VARCHAR(255)",
            "ALTER TABLE runs ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ",
            "ALTER TABLE runs ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ",
        ),
    ),
    (
        4,
        (
            "CREATE INDEX IF NOT EXISTS ix_api_guard_incidents_received "
            "ON api_guard_incidents (received_at)",
            "CREATE INDEX IF NOT EXISTS ix_api_guard_incidents_notification_due "
            "ON api_guard_incidents (notification_status, notification_next_attempt_at)",
            "CREATE INDEX IF NOT EXISTS ix_api_guard_incidents_ack_callback_due "
            "ON api_guard_incidents (ack_callback_status, ack_callback_next_attempt_at)",
        ),
    ),
    (
        5,
        (
            "ALTER TABLE api_guard_incidents ADD COLUMN IF NOT EXISTS "
            "escalation_status VARCHAR(32) NOT NULL DEFAULT 'waiting_for_delivery'",
            "ALTER TABLE api_guard_incidents ADD COLUMN IF NOT EXISTS "
            "escalation_level INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE api_guard_incidents ADD COLUMN IF NOT EXISTS "
            "escalation_attempts INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE api_guard_incidents ADD COLUMN IF NOT EXISTS "
            "escalation_next_attempt_at TIMESTAMPTZ",
            "ALTER TABLE api_guard_incidents ADD COLUMN IF NOT EXISTS "
            "escalation_delivered_at TIMESTAMPTZ",
            "UPDATE api_guard_incidents SET escalation_status = 'acknowledged', "
            "escalation_next_attempt_at = NULL WHERE acknowledged_at IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS ix_api_guard_incidents_escalation_due "
            "ON api_guard_incidents (escalation_status, escalation_next_attempt_at)",
        ),
    ),
)


async def migrate(connection: AsyncConnection) -> None:
    """Apply additive compatibility changes to pre-dashboard run databases."""
    if connection.dialect.name != "postgresql":
        return
    await connection.execute(
        text(
            "CREATE TABLE IF NOT EXISTS agent_manager_schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
    )
    rows = await connection.execute(text("SELECT version FROM agent_manager_schema_migrations"))
    applied = {int(row[0]) for row in rows}
    for version, statements in MIGRATIONS:
        if version in applied:
            continue
        for statement in statements:
            await connection.execute(text(statement))
        await connection.execute(
            text("INSERT INTO agent_manager_schema_migrations (version) VALUES (:version)"),
            {"version": version},
        )
