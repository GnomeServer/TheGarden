# TheGarden Software Factory Roadmap

**Project:** TheGarden / Dark Factory  
**Last updated:** 2026-10-15
**Purpose:** Give the team one simple place to record what exists, what is missing, who is doing what, and what must happen next.

> This document is a roadmap and checklist. A checked box means the team has verified that the item works. It does not merely mean that the item has been discussed or documented.

---

## 1. The goal in simple words

We are building a small software factory that can take a well-defined software problem, give the work to a worker laptop, check the result independently, and show humans the evidence.

The long-term loop is:

```text
User feedback, issues, logs, or ideas
        ↓
Mission and definition of done
        ↓
Planner and supervisor
        ↓
Worker laptop performs a task
        ↓
Independent tests and review
        ↓
Human approval or policy decision
        ↓
Release to a test or production environment
        ↓
Logs, metrics, and user feedback create future work
```

The immediate goal is much smaller:

```text
Create one harmless run
  → save it in PostgreSQL
  → send it through NATS
  → have one worker receive it
  → have the worker report its result
  → validate the result
  → let a human inspect the evidence
```

Do not try to build the entire factory before this small loop works.

---

## 2. How to use this document

For every task:

1. Add a person in the **Owner** column.
2. Add a target date if the team needs one.
3. Keep notes and links to issues, pull requests, or test evidence.
4. Check the box only after someone has tested the result.
5. Record important decisions in the decision log near the bottom.

Suggested status labels:

- `[ ]` Not started
- `[~]` In progress
- `[x]` Completed and verified
- `[!]` Blocked
- `[?]` Needs a team decision

Suggested task format:

```text
- [ ] Task — Owner: ______ — Target: ______ — Evidence: ______
```

---

## 3. Current state

### Foundation that exists or is already partly working

- [x] TheGarden Git repository exists.
- [x] A Proxmox server laptop exists.
- [x] An infrastructure VM exists for Docker services.
- [x] Tailscale is being used for private network connectivity.
- [x] Docker Compose projects exist for Caddy, Forgejo, Open WebUI, llama.cpp, LiteLLM, Grafana, and Prometheus. Ansible deploys every one of those except LiteLLM.
- [x] Agent Manager has an API for creating, listing, reading, and requesting cancellation of runs.
- [x] Agent Manager stores run state in PostgreSQL.
- [x] Agent Manager publishes subject `agent.runs.created`. The payload field is `event: run.created`.
- [x] Forgejo is the intended home for source code, branches, pull requests, and reviews.
- [x] The project has architecture and security documentation.
- [x] Ansible can audit the controller and deploy the Compose projects listed in `inventory/group_vars/docker_hosts.yml`. That playbook does not apply `compose.worker-test.yml`.

### Important things that are only partly complete

Unchecked means not verified in the lab. It does not mean the repository has no code.

- [ ] A worker can consume a run from NATS and report its result. — `agent-worker/worker.py` consumes mission events, obtains a per-run budget-limited LiteLLM virtual key from Keybroker, and returns generated artifacts. Not verified as a live worker run on each enrolled host.
- [ ] Agent Manager consumes worker status events and updates PostgreSQL. — durable status and completion consumers are implemented; stale queued/running runs are reconciled when no fresh worker heartbeat owns them. Live recovery remains unverified.
- [ ] The run state machine rejects every invalid transition. — late status/completion events are ignored for terminal runs; other transition rules remain incomplete.
- [ ] A deterministic product validator independently checks generated artifacts. — model reviewers inspect acceptance criteria and files; generated code is not executed and no browser journey is run.
- [ ] Evidence is stored and linked to a run. — planner criteria, reviewer findings, artifact paths, and bounded file contents are stored in `metadata.result`; artifact files remain in the worker workspace. Live retention is unverified.
- [ ] A worker laptop can join through a documented, secure connection process.
- [ ] The tracked Agent Manager source and the live deployment checkout are kept in sync.
- [ ] Ansible can configure a standard worker laptop. — the role installs the mission runner, systemd supervision, Keybroker endpoint, and Caddy CA trust; the one-time migration removes the local static key file. End-to-end NATS/model reachability and legacy-key revocation are not verified on each host.
- [ ] Machine secrets are deployed reproducibly without putting plaintext secrets in Git.

### Not built yet

- [ ] A formal mission contract and definition-of-done format.
- [ ] A versioned skill format, skill selection, and capability registry.
- [ ] A real coding worker that checks out a disposable Forgejo repository, creates a branch, and submits a PR.
- [ ] A fresh-checkout reviewer and deterministic execution of approved project checks.
- [ ] A browser or user-journey validator.
- [ ] Per-task model routing across multiple configured models; the current gateway exposes one public model alias (`luna`), and JEV routing for agent missions is not integrated. The separate API Guard has a JEV budget/security advisor, not mission routing.
- [ ] Preview, staging, release, and rollback workflows.
- [ ] Production signals automatically becoming new missions.
- [ ] A continuous-learning process that turns repeated failures into tests, skills, or documentation.

---

## 4. Current architecture in plain language

```text
User
  ↓
Open WebUI + Agent Manager Pipe
  ├── New mission → POST /v1/omp/turns
  └── Status follow-up → GET /v1/omp/missions/{mission_id}
  ↓
Central OMP: validates goal, repository label, and crew size
  ↓
Agent Manager: stores run and publishes agent.runs.created to NATS JetStream
  ↓
Available coder worker laptop
  ↓
Worker-local OMP with worker-scoped LiteLLM access
  ├── Planner: acceptance criteria and file tasks
  ├── 1–3 model-backed coders: bounded text artifacts
  ├── 1–2 independent model-backed reviewers: up to three attempts
  └── Supervisor: validates plan, files, and review verdicts
  ↓
Worker result + artifact contents → NATS → Agent Manager/PostgreSQL
  ↓
Open WebUI returns status and artifact code
```

The current mission path generates artifact files from the request, stores them
in the worker workspace, and returns their text to Open WebUI. It does not clone
or modify Forgejo, execute generated code, or run browser tests.

### What the main services mean

| Service | Simple explanation | Should it be the permanent record? |
|---|---|---:|
| Agent Manager | Central OMP/API, run ledger, and NATS dispatch | No, it uses PostgreSQL |
| PostgreSQL | The factory ledger | Yes, for run and task state |
| NATS JetStream | The conveyor belt for messages | No, it carries work and events |
| Forgejo | Repository and review system; not used by the current crew demo | Yes, for code and branches |
| Garage | Large evidence/artifact storage, when deployed | For artifacts, not workflow state |
| Prometheus | Numeric measurements | No, metrics only |
| Grafana | Human dashboards | No, dashboards only |
| Loki, later | Searchable logs | No, logs and investigation evidence |
| Matrix/Element, later | Human notifications and escalation | No, communication only |

---

# 5. Roadmap phases

## Phase 0 — Agree on safety and ownership

**Goal:** Make sure every laptop and credential has an owner and an agreed purpose.

- [ ] Decide who owns the infrastructure VM.
- [ ] Decide who owns each worker laptop.
- [ ] Get explicit permission before managing another person's laptop.
- [ ] Define who may approve code merges.
- [ ] Define who may approve deployment changes.
- [ ] Decide which repository is disposable test code.
- [ ] Decide which repositories are never allowed on the first worker.
- [ ] Define a contact person for infrastructure outages.
- [ ] Record these decisions in the decision log.

### Security rules

- [ ] Do not put the shared human password vault on a worker laptop.
- [ ] Do not give an agent KeePassXC, Bitwarden, or personal password-manager access.
- [ ] Do not give an agent Proxmox administrator credentials.
- [ ] Do not give an agent Tailscale administration credentials.
- [ ] Do not give generated code the Docker socket.
- [ ] Do not allow a worker to connect directly to PostgreSQL unless there is a specific, approved reason.
- [ ] Use worker-specific credentials rather than one shared worker credential.
- [ ] Keep production access disabled until the team explicitly approves it.

**Exit condition:** Everyone knows which machines and accounts are allowed to participate, and everyone knows what agents are forbidden to access.

---

## Phase 1 — Build the factory kernel

**Goal:** Prove one complete run without changing real code.

### Mission contract

- [ ] Define the required fields for a mission:
  - [ ] Goal
  - [ ] Repository
  - [ ] Base branch
  - [ ] Allowed actions
  - [ ] Forbidden actions
  - [ ] Required outputs
  - [ ] Required evidence
  - [ ] Maximum runtime
  - [ ] Maximum attempts
  - [ ] Human approval requirements

### Run state

- [ ] Define the allowed states:

```text
queued
running
validating
passed
failed
cancel_requested
cancelled
awaiting_approval
```

- [ ] Define allowed transitions.
- [ ] Reject invalid transitions.
- [ ] Treat duplicate events safely and idempotently.
- [ ] Record worker ID, attempt number, timestamps, and error information.

### Events

- [ ] Document the event format.
- [ ] Support `agent.runs.created`.
- [ ] Add a worker-start event.
- [ ] Add a worker-completed event.
- [ ] Add a worker-failed event.
- [ ] Add worker heartbeat events.
- [ ] Give every event a unique event ID.
- [ ] Make sure events never contain passwords, private keys, or unnecessary secrets.

### Mock worker

- [ ] Create a harmless mock worker.
- [ ] Make it consume `agent.runs.created`.
- [ ] Make it publish `started`.
- [ ] Make it wait or perform a harmless local action.
- [ ] Make it publish `completed`.
- [ ] Make it acknowledge the original NATS message.
- [ ] Run the mock worker in an isolated development Compose project.
- [ ] Do not reuse production network names, volumes, or ports for experiments.

### Validator

- [ ] Add a deterministic validator.
- [ ] Confirm the run reached the expected state.
- [ ] Confirm the worker ID exists.
- [ ] Confirm the required result fields exist.
- [ ] Confirm evidence exists.
- [ ] Confirm forbidden actions were not reported.
- [ ] Record validator results in PostgreSQL or an approved artifact store.

### Phase 1 acceptance test

The team should be able to demonstrate:

```text
POST /v1/runs
  → PostgreSQL says queued
  → NATS contains the task
  → worker receives the task
  → PostgreSQL says running
  → worker reports completion
  → validator checks the evidence
  → PostgreSQL says passed or failed
```

- [ ] Complete the acceptance test.
- [ ] Save the test commands and results.
- [ ] Create a short troubleshooting guide.

**Exit condition:** One harmless mission can go from `queued` to `passed` or `failed` without manually editing the database.

---

## Phase 2 — Prepare the first worker laptop

**Goal:** Turn one approved laptop into a safe, observable, replaceable worker.

### Owner and identity

- [ ] Laptop owner has explicitly agreed.
- [ ] Choose a stable hostname, for example `df-worker-01`.
- [ ] Record the laptop owner and physical location.
- [ ] Record the Ubuntu version and hardware.
- [ ] Install and verify Tailscale.
- [ ] Confirm the laptop can reach only the services it needs.

### Worker baseline

- [ ] Install operating-system updates.
- [ ] Configure time synchronization.
- [ ] Install Git.
- [ ] Install the worker runtime and its dependencies.
- [ ] Install Docker or the approved sandbox runtime.
- [ ] Create a dedicated worker service account.
- [ ] Create a temporary workspace directory.
- [ ] Create a separate evidence directory.
- [ ] Configure log rotation.
- [ ] Configure resource limits.
- [ ] Prevent the laptop from sleeping while an approved task is running.
- [ ] Decide how the worker behaves after reboot.

### Connectivity

- [ ] Define the approved NATS endpoint.
- [ ] Use a worker-specific NATS identity or credential.
- [ ] Restrict the worker to the NATS subjects it needs.
- [ ] Decide whether the worker may access Forgejo.
- [ ] Decide whether the worker may access a model endpoint.
- [ ] Confirm the worker does not need PostgreSQL access.
- [ ] Confirm firewall rules allow only the required connections.
- [ ] Test connectivity without exposing NATS to the public Internet.

### Worker behavior

- [ ] Worker sends a heartbeat.
- [ ] Worker reports its worker ID and capabilities.
- [ ] Worker receives a harmless test task.
- [ ] Worker reports success and failure correctly.
- [ ] Worker cleans up temporary files after a task.
- [ ] Worker stops or escalates when a task exceeds its limit.
- [ ] Worker does not read the human user's private home directory.

**Exit condition:** The worker can receive and complete a harmless task, can be observed, and can be removed or rebuilt without losing the factory's permanent records.

---

## Phase 3 — Define skills and capabilities

**Goal:** Give workers reusable, versioned recipe cards instead of relying on vague instructions.

A skill should contain:

- Purpose
- When to use it
- Required inputs
- Allowed tools
- Step-by-step instructions
- Expected outputs
- Evidence requirements
- Forbidden actions
- Validation behavior
- Version and owner

Suggested future layout:

```text
worker/
├── runtime/
├── contracts/
└── skills/
    ├── repository-inspector/
    │   ├── SKILL.md
    │   └── examples/
    ├── python-tests/
    │   └── SKILL.md
    ├── forgejo-branch/
    │   └── SKILL.md
    └── security-scan/
        └── SKILL.md
```

### Initial skills

- [ ] `repository-inspector` — read-only repository report.
- [ ] `python-tests` — run an approved Python test command.
- [ ] `git-status` — report branch and working-tree state.
- [ ] `forgejo-read` — clone or inspect an approved repository.
- [ ] `security-scan` — run approved security checks.

### Capability registry

- [ ] Define how a worker advertises capabilities.
- [ ] Define how a task declares required capabilities.
- [ ] Make the supervisor select workers by capability, not only hostname.
- [ ] Record skill versions used by each run.
- [ ] Record which tools a skill actually used.

Example:

```text
Task requires:
  git_read
  python_tests

Eligible workers:
  df-worker-01
  df-worker-03
```

**Exit condition:** A worker can be given a named skill, follow its rules, and produce the evidence defined by that skill.

---

## Phase 4 — Run the first real coding mission

**Goal:** Let one worker make a small change to a disposable repository.

- [ ] Create a disposable Forgejo repository.
- [ ] Add a README with setup and test instructions.
- [ ] Add a small test suite.
- [ ] Protect the default branch.
- [ ] Define one small coding mission.
- [ ] Give the coder only a feature branch.
- [ ] Give the worker a fresh checkout.
- [ ] Allow the worker to use only approved skills.
- [ ] Run tests before and after the change.
- [ ] Save the test output as evidence.
- [ ] Commit the work to a branch.
- [ ] Open a pull request instead of pushing to the protected branch.
- [ ] Have a reviewer use a fresh checkout.
- [ ] Require human approval before merging.

The first coding task should not deploy anything and should not use production credentials.

**Exit condition:** A small code change is created, tested, reviewed, evidenced, and approved through Forgejo.

---

## Phase 5 — Make worker laptops repeatable with Ansible

**Goal:** Make a new approved worker look like the first worker without manually guessing what was installed.

### Ansible design

- [ ] Create a `worker` inventory group.
- [ ] Create a common worker role.
- [ ] Create a worker runtime role.
- [ ] Create a sandbox role.
- [ ] Create an optional inference-worker role.
- [ ] Create an optional reviewer-worker role.
- [ ] Configure users and groups.
- [ ] Configure packages and directories.
- [ ] Configure firewall rules.
- [ ] Configure systemd or container startup.
- [ ] Configure worker metrics and logs.
- [ ] Document how to remove a worker safely.

### Secrets

- [ ] Decide whether SOPS + age or Ansible Vault is the machine-secret mechanism.
- [ ] Keep plaintext `.env` files out of Git.
- [ ] Give each worker only its own credentials.
- [ ] Rotate credentials when a worker is removed.
- [ ] Document how a worker credential is revoked.
- [ ] Keep human password management separate from machine-secret deployment.

**Exit condition:** A new, approved laptop can be rebuilt from documented automation and can join the worker pool with limited permissions.

---

## Phase 6 — Add independent product validation

**Goal:** Check that the software works for a user, not only that the code compiles.

- [ ] Create a disposable test or preview environment.
- [ ] Deploy a branch into that environment.
- [ ] Run database migrations safely.
- [ ] Run API and integration tests.
- [ ] Add browser automation or another user-journey test method.
- [ ] Test important workflows from the user's point of view.
- [ ] Store screenshots, logs, and test reports as evidence.
- [ ] Destroy temporary environments after the mission.
- [ ] Prevent the validator from changing the acceptance tests.

**Exit condition:** A branch can be deployed to a temporary environment and independently checked through a real user workflow.

---

## Phase 7 — Add release, feedback, and learning loops

**Goal:** Turn the coding workflow into a continuing software factory.

### Release

- [ ] Define development, test, staging, and production environments.
- [ ] Build versioned artifacts.
- [ ] Add human approval gates.
- [ ] Define deployment health checks.
- [ ] Define rollback behavior.
- [ ] Define database migration rollback or recovery procedures.
- [ ] Record release notes and deployment evidence.

### Feedback

- [ ] Collect application logs.
- [ ] Collect application metrics.
- [ ] Define important alerts.
- [ ] Turn serious alerts into issues or missions.
- [ ] Link incidents back to runs and releases.
- [ ] Add human notification and escalation later.

### Learning

- [ ] Review failed missions.
- [ ] Record the root cause.
- [ ] Turn repeated failures into regression tests.
- [ ] Improve skills when workers repeatedly make the same mistake.
- [ ] Improve tool descriptions and project instructions.
- [ ] Track model and worker performance.
- [ ] Create evaluation missions for important skills.

**Exit condition:** Production or user feedback can create a traceable new mission, and repeated failures cause the factory to improve.

---

# 6. Mission contract template

Copy this template when defining a test mission.

```markdown
## Mission: <short name>

Owner: <person>
Repository: <Forgejo owner/repository>
Base ref: <branch or tag>

### Goal
<What should change or be discovered?>

### Allowed actions
- <action>

### Forbidden actions
- Access production
- Read unrelated secrets
- Push to protected branches
- Change tests to hide a failure

### Required outputs
- <output>

### Definition of done
- [ ] <condition>
- [ ] <condition>

### Required evidence
- Commands run
- Exit codes
- Test report
- Commit or branch reference
- Artifact links

### Limits
Maximum runtime: <amount>
Maximum attempts: <number>
Maximum model calls: <number>

### Approval
<Who must approve the result?>
```

---

# 7. Skill template

```markdown
# Skill: <name>

Version: 0.1.0
Owner: <person or team>

## Purpose
<What this skill does>

## Use this skill when
- <condition>

## Required inputs
- <input>

## Allowed tools
- <tool>

## Steps
1. <step>
2. <step>
3. <step>

## Required evidence
- <evidence>

## Forbidden actions
- <action>

## Validation
<How another worker or program checks the result>

## Known limitations
- <limitation>
```

---

# 8. First acceptance test checklist

This is the most important near-term milestone.

- [ ] A disposable mission is written.
- [ ] A run is created through the API.
- [ ] The run is recorded as `queued`.
- [ ] NATS receives the creation event.
- [ ] A worker receives the event.
- [ ] The worker reports `running`.
- [ ] The worker performs only the harmless assigned action.
- [ ] The worker produces evidence.
- [ ] The worker reports completion or failure.
- [ ] Agent Manager updates PostgreSQL.
- [ ] The validator checks the evidence.
- [ ] Invalid transitions are rejected.
- [ ] The final result can be retrieved by a human.
- [ ] Logs identify the run, task, worker, and attempt.
- [ ] No secret appears in the event or evidence.

Expected first result:

```text
queued → running → validating → passed
```

---

# 9. What not to do yet

Until the first controlled run works, avoid:

- [ ] Adding all five workers at once.
- [ ] Giving workers production access.
- [ ] Giving agents the shared password vault.
- [ ] Giving generated code the Docker socket.
- [ ] Exposing PostgreSQL to workers.
- [ ] Exposing NATS to the public Internet.
- [ ] Building a large agent-to-agent chat system.
- [ ] Adding Kubernetes, Ray, or another large orchestration platform.
- [ ] Adding Loki, Conduit, and Element before the core run loop is reliable.
- [ ] Depending on another person's laptop without their permission.
- [ ] Treating an architecture diagram as proof that a feature works.

---

# 10. Team work board

Use this section for the current sprint. Move completed items into the phase checklists above.

| Priority | Task | Owner | Status | Evidence/link | Notes |
|---|---|---|---|---|---|
| 1 | Define the first harmless mission |  | [ ] |  |  |
| 2 | Document event fields and transitions |  | [ ] |  |  |
| 3 | Build or test the mock worker |  | [ ] |  |  |
| 4 | Add Agent Manager status-event handling |  | [ ] | `agent_manager/main.py` consumes started and completed events | Still unverified in the lab. Cancellation is published and not applied to `cancelled`. |
| 5 | Add the deterministic validator |  | [ ] |  |  |
| 6 | Decide the first approved worker laptop |  | [ ] |  |  |
| 7 | Document the secure worker connection |  | [ ] |  |  |
| 8 | Create the disposable Forgejo repository |  | [ ] |  |  |
| 9 | Write the first `repository-inspector` skill |  | [ ] |  |  |
| 10 | Run the first complete acceptance test |  | [ ] |  |  |

---

# 11. Decisions and blockers

## Decisions

| Date | Decision | Reason | People involved |
|---|---|---|---|
|  |  |  |  |

## Blockers

| Date | Blocker | Owner | Next action | Status |
|---|---|---|---|---|
|  |  |  |  |  |

## Important links

- TheGarden repository: `https://github.com/GnomeServer/TheGarden`
- Architecture overview: [`../small-lab-open-source-architecture.md`](../small-lab-open-source-architecture.md)
- Security and network layers: [`lab-layers.md`](lab-layers.md)
- Ansible project: [`../Ansible/README.md`](../Ansible/README.md)
- Docker services overview: [`../Docker-Documents/README.md`](../Docker-Documents/README.md)
- Agent Manager documentation: [`../Docker-Documents/agent-manager-service/README.md`](../Docker-Documents/agent-manager-service/README.md)

---

## The guiding rule

> Build one complete, observable, verifiable workflow before adding more agents, more workers, or more services.

```text
First make one task work.
Then make it safe.
Then make it repeatable.
Then add skills.
Then add workers.
Then automate the larger software lifecycle.
```
