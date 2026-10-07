# JEV routing, token budgeting, OMP migration, and dashboard placement

## Decision summary

For the current lab size, use one web application shell with clearly separated modules rather than several unrelated dashboards:

```text
Dark Factory Control
  /dashboard/          Missions, tasks, workers, runs, access, people, approvals
  /dashboard/budget/   Tokens, cost, reservations, budgets, routing decisions

Grafana
  /grafana/            Infrastructure metrics, logs, alerts, historical diagnostics

Forgejo
  /forgejo/            Repositories, branches, pull requests, reviews, Git identity
```

Initially keep Control, Budget, JEV policy, and the supervisor in Agent Manager with separate modules and database tables. Do not create another deployable service until measured scale or failure isolation requires it.

Keep Grafana and Forgejo separate because they have different security models and authoritative data. Link between the applications rather than embedding credentials or duplicating data.

`JEV` is used here as the operator-provided component name. Do not invent or assume an expansion of that name without a project decision.

---

## Why this separation

### Control dashboard

The Control module answers:

- What work exists?
- Who owns it?
- Which worker or role is executing it?
- What state is the mission in?
- Is human approval required?
- What evidence proves completion?
- Is a deadline blocked or overdue?

It may mutate tasks, assignments, deadlines, approvals, and timers under role-based access control.

### Budget dashboard

The Budget module answers:

- How many tokens were used?
- Which mission, task, attempt, model, worker, role, and person caused usage?
- How much was reserved, committed, released, and unattributed?
- Which model route did JEV choose and why?
- Is spend approaching a warning or hard limit?
- How accurate were route estimates?

It may mutate budget policy and approve exceptions, but ordinary usage rows are append-only.

Budget information should not be authoritative in Grafana. Grafana may visualize exported aggregates, but PostgreSQL must preserve the financial ledger, pricing version, attribution, and approval history.

### Grafana

Grafana answers infrastructure questions:

- Is a node or service healthy?
- What happened over time?
- Was a task delayed by CPU, memory, disk, network, queue, or service failure?
- Are resource trends approaching capacity?

Grafana remains administrator-focused and separately authenticated.

### Forgejo

Forgejo answers source-control questions:

- Which account pushed code?
- Which branch and commit belong to the task?
- Who reviewed and approved the pull request?
- What entered the protected branch?

Do not recreate pull requests, repository permissions, or Git history in Control or Budget.

---

## Recommended code boundaries

Start with a modular monolith inside Agent Manager:

```text
agent_manager/
  auth.py
  dashboard.py
  tracking.py
  missions.py
  supervisor.py
  routing/
    contracts.py
    jev.py
    policies.py
    registry.py
  budget/
    ledger.py
    pricing.py
    reservations.py
    reports.py
  omp/
    contracts.py
    adapter.py
  notifications.py
```

Recommended frontend structure when the current single script is split:

```text
static/
  control/
  budget/
  shared/
```

Use one authentication session, one permission model, and one navigation shell. Keep route and table ownership explicit so Budget or JEV can be extracted later without rewriting unrelated workflow code.

Do not split into microservices merely because the names differ. The current deployment benefits more from transactional consistency and simple operations than from network boundaries.

---

## JEV responsibility

JEV decides which eligible model and agent route should receive a task while respecting capability, privacy, quality, latency, and budget constraints.

JEV does not execute tasks and does not own task state. The supervisor remains authoritative.

```text
Planner produces ready task
  -> Supervisor requests JEV decision
  -> JEV filters ineligible routes
  -> JEV estimates tokens, cost, quality, and latency
  -> Budget ledger reserves capacity
  -> JEV returns versioned decision
  -> Supervisor leases task to selected role/worker
  -> OMP executes through selected model route
  -> LiteLLM reports actual usage
  -> Budget ledger reconciles reservation
  -> Supervisor accepts or rejects result
```

### Deterministic first policy

The first JEV implementation should be deterministic. Do not spend model tokens asking another model which model to use.

Candidate filtering order:

1. Required role.
2. Required tools and capabilities.
3. Data/privacy policy.
4. Context-window requirement.
5. Model and endpoint availability.
6. Remaining mission and period budget.
7. Maximum expected latency.
8. Minimum configured quality threshold.
9. Lowest expected cost among eligible routes.

Later policies may use historical outcomes, but every policy must be versioned, replayable, and explainable.

### Routing decision contract

```json
{
  "decision_id": "uuid",
  "policy_version": "jev-policy-1",
  "mission_id": "uuid",
  "task_id": "uuid",
  "attempt_id": "uuid",
  "task_features": {
    "role": "coder",
    "required_capabilities": ["git_write", "python_tests"],
    "estimated_input_tokens": 12000,
    "estimated_output_tokens": 4000,
    "privacy_tier": "private-code"
  },
  "selected": {
    "agent_role": "coder",
    "worker_pool": "agent-workers-coder",
    "model_route": "luna"
  },
  "estimate": {
    "input_tokens": 12000,
    "output_tokens": 4000,
    "cost_microunits": 0,
    "latency_class": "interactive"
  },
  "reservation_id": "uuid",
  "rejected_candidates": [
    {"route": "example", "reason": "budget_exceeded"}
  ],
  "created_at": "RFC3339 timestamp"
}
```

The example cost is not a pricing claim. Pricing must come from a versioned local model-price record or provider contract.

---

## Capability and model registry

JEV needs a durable registry, not hard-coded `if model == ...` branches.

### Model version record

```text
model_id
public_route_name
provider
provider_model
context_window
input_price_microunits_per_million
output_price_microunits_per_million
cached_input_price_microunits_per_million
privacy_tier
tool_support
status
pricing_effective_at
pricing_version
```

Use integer micro-units rather than floating-point currency.

### Agent/worker capability record

```text
worker_id
role
capability
capability_version
status
last_verified_at
policy_tags
```

Advertisement is not proof. High-risk capabilities must be provisioned and verified through Ansible or another approved deployment path.

---

## Token and cost ledger

### Authoritative sources

Use two cooperating sources:

1. LiteLLM or provider response supplies authoritative model/token usage.
2. OMP supplies mission, task, attempt, worker, role, and human attribution.

Neither source alone is sufficient.

Do not ingest model API keys, complete prompts, private source files, or response bodies into the cost ledger.

### Required tables

```text
model_prices
budgets
budget_reservations
usage_events
cost_entries
routing_decisions
budget_overrides
```

### Usage event

```text
usage_event_id
provider_request_id
mission_id
task_id
attempt_id
routing_decision_id
reservation_id
user_id
worker_id
agent_role
model_id
input_tokens
output_tokens
cached_input_tokens
cost_microunits
pricing_version
occurred_at
received_at
```

`provider_request_id` or another stable gateway request ID must be unique. Replayed callbacks must not double-charge.

### Reservation lifecycle

```text
requested -> reserved -> committed
                    \-> released
                    \-> expired
```

Before dispatch:

1. Estimate tokens and cost.
2. Lock relevant budget rows.
3. Verify hard limits.
4. Create reservation atomically.
5. Attach reservation to routing decision and attempt.

After completion:

1. Ingest actual usage.
2. Commit actual cost.
3. Release unused amount.
4. Record estimate error.
5. Flag unpriced or unattributed usage.

Failed tasks can still incur usage and must still be committed.

### Budget scopes

Support:

```text
mission
project/repository
user
team
agent role
model
calendar period
```

Precedence must be documented. A task must satisfy every applicable hard limit, not merely one permissive budget.

### Budget dashboard views

Overview:

- Current period committed cost.
- Reserved cost.
- Remaining budget.
- Forecast.
- Warning and hard-limit status.
- Unattributed or unpriced usage.

Breakdowns:

- Tokens and cost by model.
- Tokens and cost by role.
- Tokens and cost by worker.
- Tokens and cost by person.
- Tokens and cost by mission/task.
- Estimate versus actual.
- Success rate versus cost.

Audit:

- JEV decision and rejected alternatives.
- Reservation lifecycle.
- Pricing version.
- Budget overrides and approver.
- Duplicate or rejected usage callbacks.

---

## OMP replacing Pi

Use a clean cutover after OMP reaches acceptance. Do not keep two permanent execution paths.

### OMP adapter contract

Supervisor gives OMP:

```text
mission/task/attempt IDs
fencing token
lease expiry
allowed tools
forbidden actions
workspace path
repository and base ref
JEV model route
budget reservation
runtime and model-call limits
cancellation channel
required evidence
```

OMP returns:

```text
started/progress lifecycle
tool usage summary
model request attribution
changed files
commands and exit codes
evidence manifest
terminal result
usage reconciliation references
```

### Migration steps

1. Inventory all Pi files, commands, settings, Ansible tasks, and documentation.
2. Implement the OMP adapter behind the supervisor contract.
3. Run OMP on one disposable worker and repository.
4. Verify lease, fencing, cancellation, evidence, and token attribution.
5. Migrate every worker and caller.
6. Remove Pi services, configuration, documentation, aliases, and dead code.
7. Run the complete mission acceptance test.

OMP must not receive password-manager, Proxmox-admin, Ansible-controller, Tailscale-admin, database, or unrestricted Docker-socket access.

---

## Agent communication model

Do not create unrestricted agent-to-agent conversations.

Use typed, durable supervisor-mediated events:

```text
planner -> versioned task graph
supervisor -> fenced lease
coder -> branch and evidence
reviewer -> structured decision
validator -> deterministic result
human -> approval
notification adapter -> delivery evidence
```

This prevents hidden loops, duplicate work, unbounded token usage, and unauditable decisions.

Relevant event identity:

```text
event_id
schema_version
mission_id
task_id
attempt_id
worker_id
role
fencing_token
routing_decision_id
reservation_id
occurred_at
```

---

## Placement decision

### Keep the control plane on `infra-lab-services` initially

Place these on the services VM for now:

```text
Caddy
Forgejo
Agent Manager Control and Budget modules
JEV policy module
Supervisor/orchestrator
Agent Manager PostgreSQL
NATS JetStream
Prometheus
Grafana
LiteLLM gateway
```

Reasoning:

- One private network and one backup boundary are easier to operate.
- Agent Manager, JEV, and Budget are low-compute coordination services.
- PostgreSQL transactions can atomically connect routing, reservation, attempt, and usage state.
- Caddy, Forgejo, Prometheus, and Grafana already live there.
- Splitting before measurement adds network, credentials, backup, and failure modes.

### Keep heavy execution off the VM

Run these on worker nodes:

```text
model inference
OMP task execution
repository checkout and tests
coder/reviewer/validator processes
Node Exporter
normalized activity collector
```

Generated repository code must never execute on the central services VM.

### Protect the VM

Before enabling a full fleet:

- Apply container memory and CPU limits.
- Give PostgreSQL, NATS, Forgejo, and Caddy higher operational priority than dashboards.
- Bound Prometheus retention and disk use.
- Keep structured logs bounded.
- Alert at disk and memory thresholds.
- Back up state stores separately.
- Keep at least 20% disk headroom.
- Measure baseline before and after each new role.

### Proposed split trigger

Do not split based on fear alone. Split after sustained evidence such as:

- Memory above 70% under normal fleet load.
- Disk above 75% or retention growth threatens backup capacity.
- CPU above 70% for 15 minutes from control-plane work.
- I/O wait above 10% during normal operations.
- Prometheus/Loki retention materially affects PostgreSQL, Forgejo, or NATS latency.
- Observability restart or upgrade risk must be isolated from control-plane availability.

Tune these thresholds after baseline measurements; they are initial policy, not measured facts.

### First split if required

Move observability first:

```text
observability VM
  Prometheus
  Grafana
  Loki
  Alloy receiver
```

Keep together initially:

```text
control VM
  Caddy
  Forgejo
  Agent Manager
  JEV
  Budget ledger
  PostgreSQL
  NATS
  LiteLLM gateway
```

Reason: observability has the most independent retention and disk-I/O growth. Routing, reservation, attempts, and usage reconciliation benefit from transactional proximity.

Do not place application services directly on `server-debian`; preserve the Proxmox host as the virtualization and recovery boundary.

---

## Phased implementation

### Phase 0: recover and measure

1. Restore `server-debian` and `infra-lab-services`.
2. Reconcile `factory-testing`.
3. Back up all state.
4. Measure idle and current-service CPU, memory, disk, I/O, and retention.

### Phase 1: execution correctness

1. Canonical OMP worker service and process lock.
2. Attempts, leases, and fencing tokens.
3. JetStream in-progress acknowledgement.
4. Idempotent terminal results.
5. Active cancellation.
6. Restart/redelivery regression tests.

### Phase 2: real coder and reviewer

1. Mission contract.
2. Disposable Forgejo checkout and branch.
3. Evidence manifest.
4. Pull request.
5. Fresh independent reviewer.
6. Deterministic validator.

### Phase 3: JEV and budget enforcement

1. Model/capability registry.
2. Versioned deterministic routing policy.
3. Budget tables and reservation transaction.
4. LiteLLM usage ingestion.
5. OMP attribution.
6. Reconciliation and alerts.
7. Budget UI.

### Phase 4: orchestration

1. Mission/task graph.
2. Explicit state machines.
3. Capability scheduling.
4. Human approval gates.
5. Bounded retries and cancellation.

### Phase 5: fleet rollout

```text
Inkii -> Donatello -> Raphael -> Naruto
```

Add one worker only after the previous worker passes execution, identity, evidence, usage, and revocation acceptance.

### Phase 6: placement review

Measure full-fleet behavior. Keep one VM if thresholds remain healthy. If not, move observability first using a documented backup, DNS, network, and rollback plan.

---

## Acceptance criteria

JEV:

- Same inputs and policy version produce the same decision.
- Ineligible routes are rejected with explicit reasons.
- No dispatch occurs without a valid reservation.
- Supervisor, not JEV, owns task state and leases.

Budget:

- Replayed usage does not double-charge.
- Failed model calls can still record cost.
- Every attributed usage row links to mission, task, attempt, model, and route.
- Unattributed usage is visible and cannot silently disappear.
- Hard-limit override requires authorized actor, reason, and audit history.

OMP:

- No Pi runtime remains after cutover.
- OMP obeys tools, workspace, network, runtime, model-call, and budget policy.
- Cancellation terminates execution.
- Stale fencing token cannot complete a task.
- Evidence and usage references are complete.

Placement:

- Central services remain responsive under the tested fleet load.
- Generated code never executes on the service VM.
- Observability failure does not corrupt workflow state.
- Backups and restore procedures cover every authoritative store.

---

## Tracked dashboard backlog

The implementation tasks are stored in:

```text
docs/dark-factory-backlog.json
```

Import them idempotently with:

```bash
export AGENT_MANAGER_URL=http://127.0.0.1:8090
export AGENT_MANAGER_API_TOKEN='<local-token>'
python3 scripts/seed_dark_factory_backlog.py --dry-run
python3 scripts/seed_dark_factory_backlog.py
```

The importer never prints the token and skips existing tasks matching repository and exact title.
