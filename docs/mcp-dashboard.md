# MCP contract for Dark Factory dashboard operations

This document defines how an MCP server may expose the Agent Manager dashboard API to an approved assistant. It is an integration contract, not evidence that an MCP server is deployed.

## Transport and authority

The adapter calls Agent Manager over HTTPS or the private Docker network. It authenticates with a dedicated, rotated Agent Manager bearer token. It must not expose that token as an MCP resource, tool result, prompt, log field, or model-visible environment value.

PostgreSQL remains authoritative for tasks and run state. Forgejo remains authoritative for Git state. MCP clients must not cache a successful mutation as authoritative without returning the API response.

Recommended server name:

```text
dark-factory
```

## Read tools

### `dark_factory.dashboard_summary`

Purpose: return task, run, and online/busy worker counts plus overdue count.

Input:

```json
{}
```

Maps to `GET /v1/dashboard/summary`.

### `dark_factory.list_tasks`

Input schema:

```json
{
  "type": "object",
  "properties": {
    "status": {"enum": ["todo", "in_progress", "blocked", "done", "cancelled"]},
    "assignee_user_id": {"type": "string"},
    "overdue": {"type": "boolean", "default": false},
    "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 200}
  },
  "additionalProperties": false
}
```

Maps to `GET /v1/tasks`. Preserve IDs, versions, and absolute deadline timestamps exactly.

### `dark_factory.get_task`

Input:

```json
{
  "type": "object",
  "required": ["task_id"],
  "properties": {"task_id": {"type": "string", "format": "uuid"}},
  "additionalProperties": false
}
```

Maps to `GET /v1/tasks/{task_id}` and includes up to 100 newest audit entries.

### `dark_factory.list_workers`

Input: `{}`. Maps to `GET /v1/workers`.

`status` is computed from the heartbeat threshold. `reported_status` is the worker's last claim. Consumers should display `status`, not infer online state themselves.

### `dark_factory.list_users`

Input:

```json
{
  "type": "object",
  "properties": {"active": {"type": "boolean"}},
  "additionalProperties": false
}
```

Maps to `GET /v1/users`.

### `dark_factory.list_activity`

Input schema:

```json
{
  "type": "object",
  "properties": {
    "source": {"enum": ["forgejo", "worker", "task", "run"]},
    "repository": {"type": "string"},
    "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100}
  },
  "additionalProperties": false
}
```

Maps to `GET /v1/activity`.

### `dark_factory.list_runs`

Input schema:

```json
{
  "type": "object",
  "properties": {
    "status": {"type": "string"},
    "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50}
  },
  "additionalProperties": false
}
```

Maps to `GET /v1/runs`.

## Mutation tools

Mutation tools must return the complete API response or the exact HTTP error. Never convert `403`, `409`, or `422` into success text.

### `dark_factory.create_task`

Input schema:

```json
{
  "type": "object",
  "required": ["title"],
  "properties": {
    "title": {"type": "string", "minLength": 1, "maxLength": 500},
    "description": {"type": "string", "maxLength": 50000, "default": ""},
    "status": {"enum": ["todo", "in_progress", "blocked", "done", "cancelled"], "default": "todo"},
    "priority": {"enum": ["low", "normal", "high", "urgent"], "default": "normal"},
    "due_at": {"type": ["string", "null"], "format": "date-time"},
    "assignee_user_id": {"type": ["string", "null"]},
    "assignee_worker_id": {"type": ["string", "null"]},
    "repository": {"type": ["string", "null"], "maxLength": 512},
    "forgejo_issue_number": {"type": ["integer", "null"], "minimum": 1},
    "run_id": {"type": ["string", "null"]}
  },
  "additionalProperties": false
}
```

Maps to `POST /v1/tasks`.

The adapter must require a timezone offset in `due_at`. It must not silently interpret a date or timezone-naive value.

### `dark_factory.update_task`

Input contains `task_id`, required `version`, and only fields being changed. `version` must come from a prior API response.

Maps to `PATCH /v1/tasks/{task_id}`.

On `409 Conflict`, return the current task supplied by the API and require the caller to decide whether to reapply the update. Never automatically retry with the new version; that would defeat lost-update protection.

### `dark_factory.archive_task`

Input requires `task_id`. Maps to `DELETE /v1/tasks/{task_id}`.

This is a manager/admin operation. The MCP server should describe it as archival, not permanent deletion. Require explicit user intent because the item disappears from normal task lists.

### `dark_factory.create_run`

Input schema:

```json
{
  "type": "object",
  "required": ["goal", "forgejo_repository"],
  "properties": {
    "goal": {"type": "string", "minLength": 1, "maxLength": 20000},
    "forgejo_repository": {"type": "string", "minLength": 1, "maxLength": 512},
    "base_ref": {"type": "string", "default": "main", "maxLength": 256},
    "model": {"type": ["string", "null"], "maxLength": 512},
    "metadata": {"type": "object", "default": {}}
  },
  "additionalProperties": false
}
```

Maps to `POST /v1/runs`.

Do not let a general MCP client place secrets, arbitrary credentials, or private keys in `metadata`.

### `dark_factory.cancel_run`

Input requires `run_id`. Maps to `POST /v1/runs/{run_id}/cancel`.

Cancellation is a request. Return the resulting `cancel_requested` state; do not claim the worker stopped until a terminal state is observed.

## Operations intentionally not exposed

Do not expose these as general assistant tools:

- Forgejo webhook ingestion. Only Forgejo should sign and call it.
- User role or account activation changes. Keep these in the human admin UI unless a separate privileged MCP server is deliberately approved.
- Session and OAuth endpoints.
- Raw database, NATS, Docker, filesystem, or `/metrics` access.
- Arbitrary task deletion, SQL, webhook replay, or event insertion.

## Resources

An MCP adapter may expose read-only resources with these URI patterns:

```text
dark-factory://tasks/{task_id}
dark-factory://runs/{run_id}
dark-factory://workers/{worker_id}
dark-factory://activity?repository={owner/repo}
```

Resource contents should be the current API JSON plus a retrieval timestamp. Mark them stale after 30 seconds; never use a cached task version for mutation without first refreshing it.

## Error mapping

| HTTP | MCP behavior |
|---:|---|
| 400 | Invalid request or webhook data; return the server detail. |
| 401 | Authentication configuration failure; do not ask the model to invent a token. |
| 403 | Caller lacks permission; do not retry as another role. |
| 404 | Resource no longer exists or is archived. |
| 409 | Concurrent task update; return current state for human/model resolution. |
| 422 | Schema or linked-resource error; identify the rejected field. |
| 503 | Agent Manager dependency or optional integration is unavailable. |

## Safety requirements

- Use a dedicated adapter credential, not a human OAuth token.
- Keep the adapter on `caddy_proxy` or the tailnet; never expose PostgreSQL or NATS.
- Bound every list call and preserve server limits.
- Log tool name, actor, target ID, HTTP status, and request correlation ID. Redact authorization, cookies, session payloads, webhook signatures, OAuth codes, and task descriptions that may contain sensitive text.
- Require explicit confirmation before archive or run cancellation operations.
- Treat Forgejo activity as server-visible history, not proof of uncommitted local edits.

## Adapter verification

A conforming adapter must demonstrate:

1. Summary and list tools return the same records as their API endpoints.
2. Task creation returns version 1.
3. A versioned update returns version 2.
4. Reusing version 1 returns `409` rather than overwriting version 2.
5. A viewer credential cannot mutate.
6. Archive requires the privileged tool and removes the task from normal lists.
7. Credentials and cookies never appear in MCP results or logs.

## Access evidence tools

### `dark_factory.list_access_events`

Maps to `GET /v1/access-events`. Inputs are optional `node_id`, `user_id`,
`source`, and bounded `limit`. Results are normalized evidence, not raw logs.
Do not infer worked hours from login and logout events.

Node credential creation, revocation, identity mapping, and
`POST /v1/ingest/events` are intentionally excluded from a general assistant
server. They are infrastructure-administrator operations. Collectors call the
ingestion endpoint directly with their node-scoped key.

## Work-time tools

### `dark_factory.start_timer`

Maps to `POST /v1/work-sessions/start`. Inputs are optional `task_id` and
`notes`. A `409` means the person already has a running timer; never stop or
replace it automatically.

### `dark_factory.stop_timer`

Maps to `POST /v1/work-sessions/{id}/stop`. This records explicit elapsed time
and leaves the entry in `stopped` state.

### `dark_factory.submit_time`

Maps to `POST /v1/work-sessions/{id}/submit`. Only the owner may submit a
stopped or rejected entry.

### `dark_factory.review_time`

Maps to `POST /v1/work-sessions/{id}/decision`. Restrict this tool to manager
and administrator MCP credentials. Required fields are `approved` and optional
`reason`. Never describe submitted time as approved before this operation
succeeds.

### `dark_factory.list_work_sessions`

Maps to `GET /v1/work-sessions`. Inputs are optional `user_id`, `status`, and
bounded `limit`. Server-side authorization determines whether other people's
entries are visible.

## Node health tool

### `dark_factory.node_health`

Maps to `GET /v1/nodes/health`. It returns `available=false` when Prometheus is
unreachable; that is degraded observability, not proof that all nodes are
offline. Never convert missing metrics into zero utilization.

## Notification resource

The adapter may expose `dark-factory://notifications`. Notification status is
delivery evidence only: `sent` means the configured adapter accepted the
request, while `failed` includes a bounded error and attempt count. MCP clients
must not replay a successfully sent deadline notification.
