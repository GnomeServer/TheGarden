# Worker Topology and Role Delegation

Companion to `small-lab-open-source-architecture.md` and
`worker-ai-runtime-requirements.md`.

This document defines the detailed topology of the five Ubuntu worker laptops and
the **coder / reviewer** role-delegation plan for the agentic coding loop
(architecture section 6). It answers:

- Which worker does what, and why.
- How the five nodes are wired into the tailnet, NATS, Forgejo, and the model
  endpoints.
- How a coder and a reviewer stay independent (no shared context, no self-review).
- Storage, ports, services, and failure behavior per node.

> **Change from the architecture document:** Garage S3 is **not implemented** in
> this prototype. It does not justify the operational cost for a five-laptop lab.
> Artifacts and evidence therefore live on worker local disk, referenced by
> PostgreSQL, and are synced to the server's backup staging. S3 (Garage or MinIO)
> can be reintroduced later without changing the role topology — see §13.

---

# 1. Inventory and naming

All five workers are identical hardware **and identical software**:

```text
CPU:  Intel Core Ultra 5 125H (Meteor Lake)
RAM:  32 GB shared (UMA)
GPU:  Intel Arc integrated graphics (Xe-LPG, ~112 EU)
Disk: ~512 GB NVMe
OS:   Ubuntu 24.04 LTS (bare metal)
Net:  Tailscale (MagicDNS names below)
```

| Node | Tailscale / MagicDNS name | Agent role (default) | Notes |
|---|---|---|---|
| Worker 01 | `lab-gpu-01` | **Coder** (primary) | strongest dedicated coder slot |
| Worker 02 | `lab-gpu-02` | **Coder** (secondary) | parallel coder tasks / spill |
| Worker 03 | `lab-gpu-03` | **Reviewer** (primary) | fresh-checkout reviews |
| Worker 04 | `lab-gpu-04` | **Reviewer** (secondary) | more review capacity |
| Worker 05 | `lab-gpu-05` | **Overflow** (role flips) | joins coder or reviewer queue group; hosts Forgejo runner |

Stable identities used everywhere (Ansible inventory, NATS `worker_id`, Prometheus
labels, service units):

```text
lab-gpu-01  lab-gpu-02  lab-gpu-03  lab-gpu-04  lab-gpu-05
```

Role is a per-node variable set in Ansible (`agent_role`), not a code change. The
base image is identical on all five; only the NATS consumer enabled differs.

---

# 2. Logical topology (one picture)

```text
                             Tailscale tailnet
                                  |
              +-------------------+------------------+
              |                                      |
        infra-01 VM                          lab-gpu-01..05
   [control plane]                         [compute plane, all five equal]
    Caddy, Forgejo                          llama-server (Vulkan) x5
    Postgres, NATS                          coder agents   (01, 02)
    OpenWebUI, Prometheus                   reviewer agents (03, 04)
    Gitea/runner control                    overflow agent (05)
              |                                      |
              +------------------+-------------------+
                                 |
                Forgejo (code) / NATS (jobs) / Prometheus (metrics)
                PostgreSQL (run state) / server staging (evidence backup)
```

Workers never expose PostgreSQL, NATS, or the Forgejo database to anything. There
is **no storage plane** in this prototype: code lives in Forgejo (server),
durable state in PostgreSQL (server), and bulky evidence on the worker that
produced it (synced to the server periodically).

---

# 3. Service topology per worker

### 3.1 Every worker (base, identical)

| Service | Unit/container | Listens on | Purpose |
|---|---|---|---|
| Time sync | `systemd-timesyncd` | UDP 123 out | TLS, logs, metrics |
| Host firewall | UFW (rules in §8) | — | Tailscale + server traffic only |
| Node Exporter | container | `:9100` | Prometheus metrics (CPU/RAM/disk/temp) |
| Docker | `docker.service` | unix socket (internal) | runs grader/sandbox containers |
| llama-server (Vulkan) | systemd or container | `0.0.0.0:8080` | OpenAI-compatible model API |
| NATS agent | container | outbound only | coder, reviewer, or overflow consumer |
| Models | `/srv/models` (ro) | — | local GGUF cache (+ sha256 manifests) |
| Evidence staging | `/srv/agent/evidence` | — | per-run/task artifact store |

### 3.2 Role-specific activation (variable, not code)

| Node | `agent_role` | NATS subject consumed | Primary model served |
|---|---|---|---|
| 01 | `coder` | `lab.command.task.assign.coder` | `qwen3-8b` + `qwen2.5-coder-7b` |
| 02 | `coder` | `lab.command.task.assign.coder` | `qwen3-8b` |
| 03 | `reviewer` | `lab.command.task.assign.reviewer` | `qwen3-4b` (+ `qwen3-8b` fallback) |
| 04 | `reviewer` | `lab.command.task.assign.reviewer` | `qwen3-4b` (+ `qwen3-8b` fallback) |
| 05 | `overflow` | joins coder **or** reviewer group | `qwen3-8b` |

### 3.3 Optional: Forgejo Actions runner

`lab-gpu-05` also runs a `forgejo-runner` (separate from the NATS agent) so CI jobs
execute off-server. Runners can be added to any worker later with the same pattern;
do not mount `/var/run/docker.sock` into untrusted jobs (architecture §5.3).

---

# 4. Role delegation plan: coder and reviewer

The architecture mandates coder and reviewer be **separate worker roles, separate
tasks, independent context, fresh checkout**, with all communication through
supervisor + NATS + Forgejo. Concrete delegation:

### 4.1 Role-to-node assignment

| Role | Node(s) | Why assigned here |
|---|---|---|
| **Coder** | `lab-gpu-01` primary, `lab-gpu-02` secondary, `lab-gpu-05` overflow | coder always lands **on a different physical node than the reviewer of the same run** |
| **Reviewer/tester** | `lab-gpu-03` primary, `lab-gpu-04` secondary | fresh-checkout reviewers never share a node with the coder that produced the code |
| **Planner/supervisor** | `infra-01` VM | control plane; never collocates with coder/reviewer |
| **Model endpoints** | coder nodes serve coder models; reviewer nodes serve reviewer models | a different model reviews the coder's output → independent error surfaces |

### 4.2 Fan-out rule (the core guarantee)

A run's coder task and its review task are scheduled so that:

```text
coder   -> lab-gpu-01 | lab-gpu-02 | lab-gpu-05
reviewer-> lab-gpu-03 | lab-gpu-04 | (other pool)

Constraint: worker_id(coder) != worker_id(reviewer) for the same task.
```

The supervisor records `worker_id` per task in PostgreSQL and refuses to assign a
review to the node that produced the code. This is a hard check, not a habit.

### 4.3 Design rules enforced by this split

1. **No worker runs both coder and reviewer consumers** — a node is in exactly one
   queue group at a time (`coder` / `reviewer` / `overflow`-currently-one), so a
   coder cannot review its own work, and worker-scoped NATS credentials bind each
   task.
2. **Different models on each side.** Coder calls `lab-gpu-01/02`'s `qwen3-8b`;
   reviewer calls `lab-gpu-03/04`'s `qwen3-4b`. Shared-model blind spots are
   avoided.
3. **Fresh checkout, fresh sandbox.** Reviewer never reuses the coder workspace;
   it re-clones from Forgejo (`refs/heads/agent/<run>/<task>`) and executes in a
   new container.
4. **NATS subjects separate jobs** so a coder cannot consume review requests and
   vice versa; queue groups `coder-q` and `reviewer-q` let us add/remove members by
   changing `agent_role` on a node.
5. **Supervisor owns the verdict.** Workers only report `task.completed` /
   `task.failed` / `review.passed|failed|changes_requested`; they cannot flip their
   own status in PostgreSQL — the supervisor writes every state transition.

### 4.4 NATS subjects (mirror of architecture §6.1, minus storage-side subjects)

```text
lab.command.task.assign.coder        ->  coder queue group (01, 02, overflow)
lab.command.task.assign.reviewer     ->  reviewer queue group (03, 04, overflow)
lab.event.task.started
lab.event.task.progress
lab.event.task.completed
lab.event.task.failed
lab.event.review.requested
lab.event.review.completed
lab.event.worker.heartbeat           ->  all workers (every 10-15 s)
lab.event.approval.required          ->  supervisor only
```

Durable JetStream consumers; workers ACK after a successful claim. Supervisor
requeues unfinished tasks after the missed-heartbeat window (architecture §6.1).

### 4.5 One-run walkthrough (who touches what, Garage-free)

```text
1.  Human -> Open WebUI -> AgentAPI (planner)              [infra-01]
2.  Planner writes task graph -> PostgreSQL                 [infra-01]
3.  Supervisor publishes coder task                         [NATS]
4.  lab-gpu-01 coder claims, clones team/project            [Forgejo]
5.  coder calls its own llama endpoint (Vulkan)             [node :8080]
6.  coder branches, commits, pushes agent/<run>/<task>      [Forgejo]
7.  coder stages reports/logs under /srv/agent/evidence/... [node 01]
8.  coder publishes task.completed (+ artifact_refs)        [NATS]
9.  Supervisor creates reviewer task                         [infra-01]
10. lab-gpu-03 reviewer claims, FRESH checkout              [Forgejo]
11. reviewer runs build/test/lint/typecheck/security        [fresh sandbox node 03]
12. reviewer stages its evidence                             [node 03]
13. reviewer publishes review.{passed,failed,changes}        [NATS]
14. Supervisor decides accept / retry / escalate             [infra-01]
15. Human paged only if approval gate or failure             [Matrix later]
```

### 4.6 Retry and spill cadence

```text
coder attempt 1 -> lab-gpu-01
coder attempt 2 -> lab-gpu-02            (different node, different attempt worker_id)
coder attempt 3 -> lab-gpu-05 (overflow) (if enabled)
max_attempts = 3, then escalate to human (architecture §6.1)

reviewer runs once per coder attempt, always on a node != coder node.
```

### 4.7 Anti-patterns this design rejects

- Coder and reviewer on the same host (kills independence + unfair resource share).
- Reviewer using the coder's working tree (no self-blinding).
- Agent writing to PostgreSQL state or reading NATS admin credentials.
- Coder merging to a protected branch (merge = human gate).

---

# 5. Model endpoint topology

Open WebUI and agent workers reach each worker's inference API the same way:
`http://<magicdns-name>:8080/v1` (OpenAI-compatible).

| Endpoint | Node | Default model | Consumers |
|---|---|---|---|
| `http://lab-gpu-01:8080/v1` | coder (primary) | `qwen3-8b` Q4_K_M (+ `qwen2.5-coder-7b` on demand) | coder agent 01 + OpenWebUI direct test |
| `http://lab-gpu-02:8080/v1` | coder (secondary) | `qwen3-8b` Q4_K_M | coder agent 02 |
| `http://lab-gpu-03:8080/v1` | reviewer (primary) | `qwen3-4b` (+ `qwen3-8b` fallback) | reviewer agent 03 |
| `http://lab-gpu-04:8080/v1` | reviewer (secondary) | `qwen3-4b` (+ `qwen3-8b` fallback) | reviewer agent 04 |
| `http://lab-gpu-05:8080/v1` | overflow | `qwen3-8b` Q4_K_M | whichever role is active |

Phase 1: Open WebUI talks directly to `lab-gpu-01` only (the architecture's "Direct
model API initially"). Phase 2: add LiteLLM in front of all five so routing,
fallback, and per-model queues are centralized. LiteLLM key stays on infra-01;
workers never receive it.

---

# 6. Storage topology (no Garage)

### 6.1 Single uniform SSD layout for all five workers (512 GB)

```text
/                   50-70 GB          OS + packages
/var/lib/docker     50-80 GB          images, sandbox layers, runner cache
/srv/models        220-280 GB         GGUF cache (the big consumer)
/srv/agent/        ~60-80 GB          agent runtime data:
    workspaces/     (coder)            isolated per-task code dirs
    checkouts/      (reviewer)         fresh-per-review checkouts
    evidence/<run>/<task>/             durable reports, test logs, diffs
free               80-100 GB          15-20% headroom + automated cleanup
```

Disk alerts at 75% / 85% usage; a systemd timer purges: old sandbox layers, stale
`workspaces|checkouts` older than N runs, old evidence older than retention
policy, and build caches.

### 6.2 Evidence (replaces the S3/`agent-results` bucket)

Without Garage, each worker keeps its own evidence directory:

```text
/srv/agent/evidence/<run_id>/<task_id>/<attempt>/
    test-report.json
    lint-report.txt
    security-report.json
    diff.patch
    build.log
```

The worker returns `artifact_refs` in its NATS result like
`lab-gpu-03://srv/agent/evidence/run-123/task-456/1/...`. PostgreSQL holds the
references (architecture §6.5). A nightly `rsync` pushes `evidence/` to
`infra-01:/srv/backup-staging/agent/` so evidence outlives a worker disk failure
(see §9). Restic on infra-01 then backs up staging to the external disk without any
S3 dependency.

> When the lab outgrows this, reintroduce S3 as a thin layer: workers `rsync` or
> `s5cmd` evidence into a Garage/MinIO bucket, `artifact_refs` become
> `s3://agent-results/...`, and nothing else in the topology changes.

### 6.3 Model cache strategy

- One canonical model on every worker: `qwen3-8b-q4_k_m.gguf` under `/srv/models`,
  because inference must never depend on another machine (architecture §4 guidance:
  store one or two standard models per worker, don't hoard variants).
- Reviewer nodes add `qwen3-4b`; coder node 01 adds `qwen2.5-coder-7b`.
- Seed by `rsync` from `lab-gpu-01` or USB; verify `sha256` + `gguf` metadata with
  `llama-gguf`. Manifest file `/srv/models/MANIFEST` tracked in the Ansible repo.

---

# 7. Network and connectivity topology

### 7.1 Tailscale (already present)

```text
MagicDNS:
  lab-gpu-01..05.<tailnet>            e.g. lab-gpu-01.lab.ts.net
Tags:
  tag:workstation   (plain human laptops)
  tag:worker        (lab-gpu-01..05)  restricted outbound
  tag:server        (infra-01, ansible host)
ACL highlights:
  admin          -> workers ssh:22, node-exporter:9100
  infra-01       -> workers :8080 (model API, from approved actors), :9100
  workers        -> infra-01 :4222 (NATS), :3000 (Forgejo git), :443 (apps)
  users          -> infra-01 :443 only (Caddy fronts everything)
  worker <-> worker: none by default (work flows through server, never direct)
```

Workers are **not** directly reachable from user laptops except the approved
SSH/metrics/admin paths — users talk to Open WebUI, not to `lab-gpu-*:8080`.
After bring-up, cut worker outbound to only the DNS/ports the sandbox needs;
sandbox containers themselves get egress disabled except Forgejo + model API.

---

# 8. Port and firewall matrix

| Port | Service | Side | Who may connect |
|---|---|---|---|
| 22 | SSH | all workers | admins only (Tailscale ACL) |
| 9100 | node-exporter | all workers | Prometheus (infra-01), admins |
| 8080 | llama-server | all workers | infra-01 + admin (UFW/ACL restricted) |
| 4222 | NATS | infra-01 (listen) | workers outbound, server listens |
| 3000 | Forgejo | infra-01 | via Caddy :443 only |
| 443 | Caddy (all apps) | infra-01 | users HTTPS |

There is **no 3902/3903** anymore (Garage removed).

UFW per worker:

```bash
sudo ufw default deny incoming
# tailnet prefix — confirm via: ip addr show tailscale0  (commonly 100.64.0.0/10)
sudo ufw allow from 100.64.0.0/10 to any port 22   proto tcp
sudo ufw allow from 100.64.0.0/10 to any port 8080 proto tcp
sudo ufw allow from 100.64.0.0/10 to any port 9100 proto tcp
sudo ufw allow from 100.64.0.0/10 to any port 3000 proto tcp   # only for forgejo runners
sudo ufw enable
```

---

# 9. Agent executables and sandboxing per worker

### 9.1 Coder agent (nodes 01/02, overflow 05)

```text
/runtime/agent-coder
  - subscribes: lab.command.task.assign.coder (queue group coder-q)
  - claims task; builds /srv/agent/workspaces/<run>-<task>-<attempt>/
  - git clone <repository> (scoped SSH key/token; it may only push agent branches)
  - calls its own llama endpoint http://lab-gpu-01:8080/v1 for code-gen/tool turns
  - runs quick local tests in constrained container (NO docker.sock mount)
  - commits, pushes agent/<run>-<task>, stages evidence to /srv/agent/evidence
  - publishes task.completed/{failed} with commit_sha + artifact_refs
```

### 9.2 Reviewer/tester agent (nodes 03/04, overflow 05)

```text
/runtime/agent-reviewer
  - subscribes: lab.command.task.assign.reviewer (queue group reviewer-q)
  - FRESH checkout to /srv/agent/checkouts/<run>-<task>-<attempt>/
  - pipeline: build -> unit tests -> lint -> typecheck -> security scan (bandit/trivy)
  - calls its own llama endpoint (lab-gpu-03/04) for review + acceptance checks
  - posts review comment to Forgejo PR/commit
  - stages evidence under /srv/agent/evidence/<run>/<task>/
  - publishes review.passed | failed | changes_requested
```

### 9.3 Sandbox policy (both roles)

- Per-task container; `--network none` except proxy to approved hosts.
- `readonly` rootfs where possible; only the task workspace and evidence dir
  writable.
- `pids_limit`, `memory`, `cpu` cgroup limits; hard `timeout(1)` wrappers.
- No `/var/run/docker.sock`, no host GPU pass-through into test containers (the
  generator uses GPU only via llama-server's API).
- Agent credentials: NATS user per role (`coder`/`reviewer`), Forgejo token per
  role — least privilege, rotated by Ansible.

---

# 10. Observability topology

```text
all workers ---- metrics:9100 ----> Prometheus (infra-01)
all workers ---- heartbeats ------> NATS        -> supervisor (infra-01)
test/ci/build ---- runs ----------> Forgejo Actions logs
run state (system of record) ------> PostgreSQL (infra-01)
evidence ----------------------------> worker /srv/agent/evidence
        └ rsync nightly ------------> infra-01 /srv/backup-staging -> Restic -> ext disk
```

Prometheus label scheme:

```text
job="worker", instance="lab-gpu-01", role="coder", task_id="...", run_id="..."
```

Worker dashboards: GPU busy/clock/temp (later `intel_gpu_top` exporter; until then
llama-server metrics + node_exporter), tokens/s, queue-drain rate, fresh-checkout
failure rate, sandbox failures, evidence staging size.

---

# 11. Failure and scaling behavior

### 11.1 Worker failure

```text
supervisor misses heartbeat for N intervals
  -> mark worker `unavailable` in PostgreSQL
  -> verify task completion via Forgejo commit / evidence presence
  -> requeue unfinished task with attempt++ to another node in the same queue group
  -> escalate when max_attempts exceeded
```

If the failed worker produced evidence that was not yet rsynced, the run's
evidence may be lost for that attempt — the code is always safe in Forgejo, and the
next attempt regenerates evidence. Keep rsync nightly for inventory-level
durability, not for per-attempt guarantees.

### 11.2 Adding a sixth laptop (or repurposing a node)

1. Install the Ansible worker role (identical base: Vulkan stack, node-exporter,
   llama-server, models, agent runtime).
2. Give it a MagicDNS name; add to inventory.
3. Set `agent_role: coder|reviewer|overflow` — that's the entire role switch.
4. Point LiteLLM at its `:8080` if it carries a model.

### 11.3 Backpressure

If both coder nodes are busy, NATS holds the task in the coder durable
(JetStream); the run-view shows `blocked`, never a false "queued-but-running".
Surface queue depth on the run dashboard (architecture §6.6).

---

# 12. Bring-up order (maps onto architecture Stage 6/7, Garage-free)

```text
Stage A  all workers: base image + Vulkan stack + node exporter       (Section 3-4)
Stage B  lab-gpu-01: llama-server + qwen3-8b + Open WebUI direct chat test
Stage C  lab-gpu-03: llama-server + reviewer model; verify a manual fresh
         checkout + test pipeline runs
Stage D  NATS consumers: coder role on 01, reviewer role on 03
Stage E  scale roles: coder-q += 02, reviewer-q += 04, overflow 05 enabled
Stage F  one full controlled coding run visible end-to-end in PostgreSQL/
         Forgejo/evidence + Grafana
Stage G  evidence rsync to infra-01 + Restic to external disk enable; LiteLLM
         added for routing/fallback
```

First goal (architecture §8, adjusted): one worker serving an Intel-GPU inference
endpoint; Open WebUI chatting to it directly; one coder task and one independent
reviewer task completing in Forgejo; run state in PostgreSQL; evidence on disk,
synced to the server.

---

# 13. Decision log (why things are shaped this way)

- **Vulkan over SYCL/OpenVINO** — see `worker-ai-runtime-requirements.md` §1:
  longest model support, best concurrency, easiest ops; OpenVINO's GGUF backend is
  not viable for servers yet (dynamic-shape crashes, no MoE/SSM).
- **Garage dropped.** A three-node S3 replica, zones, layouts, and key management
  add real operational load with no benefit at prototype scale. Evidence-on-disk +
  PostgreSQL refs + nightly rsync + Restic covers the lab's only real need (the
  server's own backups). Reintroduce S3 later as a thin evidence layer if needed —
  nothing in the role topology depends on it.
- **Coder on 01/02, reviewer on 03/04, overflow on 05** — physically independent,
  different models, separate NATS subjects/credentials → the architecture's
  "reviewer independent from coder context" rule holds with no extra machinery, and
  every node stays part of a same kind compute pool.
- **All five workers identical** — no storage vs compute split anymore, so the
  Ansible `worker` role is one playbook with one variable, and any node can take
  any role. This is simpler to reason about and to recover from.
- **Inference only through the API** (no GPU pass-through into agent sandboxes) —
  keeps sandboxes small, makes llama-server the single GPU consumer, and leaves the
  door open to cont-batching/LiteLLM without rework.