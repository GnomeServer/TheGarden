from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        1,
        (
            "CREATE INDEX IF NOT EXISTS ix_runs_status ON runs (status)",
            "CREATE INDEX IF NOT EXISTS ix_runs_created_at ON runs (created_at)",
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
)


async def migrate(connection: AsyncConnection) -> None:
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
