# Worker Topology and Ephemeral Agent Delegation

Companion to `small-lab-open-source-architecture.md`.

This document is the node-level view of the five Ubuntu workers. The end-to-end Dark Factory flow lives in architecture §3. Here:

- Every worker runs the same software.
- A plan occupies one worker.
- That worker's Oh My Pi harness runs a planner, which spawns ephemeral agents.
- The planner chooses the tasker and reviewer types.
- Storage, ports, sandbox, and failure behavior per node.

Garage S3 is not in this lab. Evidence lives on the worker that produced it, is referenced from the run index, and is rsynced to the server. S3 can return later as a thin evidence layer without changing delegation.

---

# 1. Inventory and naming

All five workers are the same hardware and the same software:

```text
CPU:  Intel Core Ultra 5 125H (Meteor Lake)
RAM:  32 GB shared (UMA)
GPU:  Intel Arc integrated graphics (Xe-LPG, ~112 EU)
Disk: ~512 GB NVMe
OS:   Ubuntu 24.04 LTS, bare metal
Net:  Tailscale
```

| Node | MagicDNS name | Role |
|---|---|---|
| Worker 01 | `lab-gpu-01` | Same as the others. First node used to prove the path. |
| Worker 02 | `lab-gpu-02` | Same image. |
| Worker 03 | `lab-gpu-03` | Same image. |
| Worker 04 | `lab-gpu-04` | Same image. |
| Worker 05 | `lab-gpu-05` | Same image. Also the first Forgejo Actions runner. |

```text
lab-gpu-01  lab-gpu-02  lab-gpu-03  lab-gpu-04  lab-gpu-05
```

There is no `agent_role`. A worker is either free or holding one plan. Ansible does not configure a coder queue or a reviewer queue.

---

# 2. Logical topology

```text
                         Tailscale tailnet
                                |
            +-------------------+------------------+
            |                                      |
      infra-01 VM                           lab-gpu-01..05
      Caddy, Open WebUI                     identical compute
      server Oh My Pi                       worker Oh My Pi
      orchestrator agent                    planner + ephemeral agents
      dispatch helper                       llama-server (Vulkan)
      Forgejo, PostgreSQL                   local model cache
      Prometheus, Grafana                   evidence on local disk
            |                                      |
            +------------------+-------------------+
                               |
              plan goes server -> one worker
              planner reply goes worker -> server
              code goes worker -> Forgejo
              users never open :8080
```

Workers do not expose PostgreSQL, Forgejo's database, or each other. Work does not hop from worker to worker. The orchestrator picks one free node and that node finishes the plan.

---

# 3. Services on a worker

## 3.1 Base, every node

| Service | Unit | Listens | Purpose |
|---|---|---|---|
| Time sync | `systemd-timesyncd` | outbound UDP 123 | TLS, logs, run timestamps |
| Host firewall | UFW, §8 | — | Tailscale sources only |
| Node Exporter | container | `:9100` | CPU, RAM, disk, temperature |
| Docker | `docker.service` | unix socket, local | sandboxes and CI jobs |
| llama-server | systemd or container | `0.0.0.0:8080` | OpenAI-compatible inference, Vulkan |
| Oh My Pi | started per plan by forced command | stdio, not a public port | planner session and ephemeral children |
| Models | `/srv/models` | — | GGUF cache and checksum manifest |
| Agent data | `/srv/agent` | — | sessions, workspaces, checkouts, evidence |

Oh My Pi is not a standing multi-tenant daemon on `:some-port`. The dispatch helper SSHes in and starts `omp --mode rpc` in `/srv/agent/sessions/<run_id>/`. When the planner replies, that process exits. A second SSH into a busy worker is refused by the forced command.

## 3.2 Capacity

| State | Meaning |
|---|---|
| `free` | No planner session. Eligible for a plan. Eligible to serve orchestrator inference. |
| `busy` | One planner session. llama-server on this node serves that plan's agents only. |
| `draining` | Finish the current plan, accept no new one. Used for updates. |

One plan per worker. Five workers means five plans, not five roles.

## 3.3 Forgejo Actions runner

`lab-gpu-05` runs `forgejo-runner` first, separate from any planner session. Add runners to other workers with the same unit. Do not place a CI job on a worker whose state is `busy`. Do not mount `/var/run/docker.sock` into untrusted jobs (architecture §5.3).

---

# 4. Delegation

The architecture rule: the orchestrator sends a plan; the planner on the chosen worker creates the task list and spawns ephemeral agents; the usual loop is tasker then reviewer; the planner picks the types.

## 4.1 Who runs where

| Agent | Node | Why |
|---|---|---|
| Orchestrator | `infra-01` only | Writes the plan. Never shares a filesystem with the coder. |
| Planner | The one worker that received the plan | Owns the task list and the verdict for that plan. |
| Ephemeral tasker / coder | Child session on that same worker | Isolated workspace. Dies with the task. |
| Ephemeral reviewer, tester, security reviewer | Child sessions on that same worker | Fresh checkout, blank conversation. Dies with the review. |

Specialists are not laptop roles. The planner of a software-engineering plan spawns a coder, a reviewer, a tester, and a security reviewer. The planner of another plan spawns whatever that plan needs. Adding a type is an agent definition under `.omp/agents/`, not an Ansible variable.

## 4.2 Loop the planner actually runs

```text
for task in task_list:
    spawn tasker          blank session, assignment + acceptance criteria only
    wait for yield
    spawn reviewer        blank session, Forgejo ref + criteria, not the tasker transcript
    wait for yield
    if reviewer failed and attempts < 3:
        spawn a new tasker attempt
    else if reviewer failed:
        stop and reply blocked
planner checks every required task
reply to orchestrator
```

Software-engineering task list the planner should produce when the plan changes code:

```text
scout              optional, read-only, only if the repo is unfamiliar
coder              implements on agent/<run>/<task>
reviewer           correctness against acceptance criteria
tester             build, unit tests, lint, typecheck
security-reviewer  diff and dependency findings
```

A documentation or research plan skips the code specialists. The planner is wrong if it always emits the software-engineering list.

## 4.3 Independence

1. Ephemeral sessions start blank. The reviewer prompt contains the ref and the criteria. It does not contain the coder transcript.
2. Reviewer and tester clone Forgejo into `/srv/agent/checkouts/`. They do not open `/srv/agent/workspaces/`.
3. The coder uses a harness isolated workspace and may push only `agent/<run>/<task>`.
4. The planner writes the verdict. A tasker cannot set its own task to passed.
5. Same physical node is required, not forbidden. The plan was sent to one worker so its agents share that worker's model endpoint and disk. Independence is the session boundary.

## 4.4 What the planner must not do

- Message Open WebUI or the orchestrator except through its final reply.
- Spawn a reviewer that shares the coder session.
- Merge to a protected branch.
- Start work on a second plan.
- Call another worker's llama-server. Local endpoint only: `http://127.0.0.1:8080/v1`.

## 4.5 Attempts

```text
attempt 1  coder, then the checker that applies
attempt 2  new coder session, same worker, then the failed checker again
attempt 3  same
then       planner reply status=blocked
           orchestrator escalates in Open WebUI
```

A retry stays on the same worker unless that worker has died. A dead worker is the orchestrator's problem: new planner session on a free node, same plan, plus any Forgejo refs that already exist.

---

# 5. One-run walkthrough

```text
1.  User prompt in Open WebUI                              [infra-01]
2.  DF front door prompts server omp --mode rpc            [infra-01]
3.  Orchestrator writes the plan                           [server session]
4.  Run index row: queued                                 [PostgreSQL]
5.  Dispatch helper picks a free worker, state=busy        [lab-gpu-02 example]
6.  Forced command starts omp --mode rpc                   [worker]
7.  Planner writes the task list                           [worker session]
8.  Coder child: clone, local llama-server, push branch    [Forgejo]
9.  Coder stages logs under evidence/<run>/<task>/1/       [worker disk]
10. Reviewer child: fresh checkout, no coder transcript    [worker]
11. Tester child: fresh checkout, test/lint/typecheck      [worker]
12. Security reviewer child: fresh checkout                [worker]
13. Planner verifies all required tasks                    [worker]
14. Planner reply returns over the dispatch channel        [tailnet]
15. Orchestrator accepts or names one gap                  [infra-01]
16. Finished response posted to the same Open WebUI thread
17. Worker state returns to free when the session exits
```

Step 15 is not a second implementation pass. One gap may be sent back once. A second miss is an escalation.

---

# 6. Model endpoints

Agents on a worker call loopback. The server orchestrator and admin diagnostic chat call MagicDNS.

| Endpoint | Who may call it | Model |
|---|---|---|
| `http://127.0.0.1:8080/v1` | Planner and ephemeral agents on that node | Local GGUF |
| `http://lab-gpu-NN:8080/v1` | `infra-01` and admins, and only when that node is `free` | Same file |

Default file on every worker: `qwen3-8b` Q4_K_M under `/srv/models`. Optional second file, also on every worker so placement does not matter: `qwen2.5-coder-7b`. Do not pin a model to a former "reviewer node". If a reviewer should use a different model, the planner sets that in the child spawn. The file still has to exist on every worker.

Phase 1 is direct URLs. LiteLLM waits until routing or fallback is actually painful. Its key would stay on `infra-01`.

The DF front door sets the server session model URL to a `free` worker before each orchestrator turn. It must not select a worker whose state is `busy`. If none are free, the turn waits.

---

# 7. Storage

## 7.1 Layout, all five

```text
/                   50-70 GB       OS + packages
/var/lib/docker     50-80 GB       images, sandbox layers, runner cache
/srv/models        220-280 GB      GGUF cache
/srv/agent          60-80 GB
    sessions/<run_id>/             planner session, ephemeral artifacts
    workspaces/<run>/<task>/       coder isolated worktrees
    checkouts/<run>/<task>/        reviewer and tester clones
    evidence/<run>/<task>/<attempt>/
free                80-100 GB
```

Disk alerts at 75% and 85%. A timer purges sandbox layers, workspaces and checkouts for finished runs, evidence past retention, and build caches.

## 7.2 Evidence

```text
/srv/agent/evidence/<run_id>/<task_id>/<attempt>/
    test-report.json
    lint-report.txt
    security-report.json
    diff.patch
    build.log
```

The planner reply names these paths. PostgreSQL stores the paths, not the files. Nightly `rsync` to `infra-01:/srv/backup-staging/agent/`, then Restic to the external disk. No S3.

A worker disk that dies before rsync loses that attempt's logs. The branch in Forgejo is the code. The next attempt rebuilds the logs.

## 7.3 Model cache

- Same canonical GGUF on every worker. Inference for a plan must not depend on another machine.
- Seed with `rsync` from `lab-gpu-01` or from USB.
- Verify checksum and GGUF metadata before the unit starts.
- `/srv/models/MANIFEST` lives in the Ansible repo.

---

# 8. Network and firewall

## 8.1 Tailscale

```text
MagicDNS: lab-gpu-01..05.<tailnet>
Tags:
  tag:workstation   human laptops
  tag:worker        lab-gpu-01..05
  tag:server        infra-01
ACLs:
  users        -> infra-01 :443 only
  admin        -> workers :22, :9100, :8080
  infra-01     -> workers :22 (dispatch key + admin), :8080, :9100
  workers      -> infra-01 :443 (Forgejo via Caddy) and Git SSH if used
  worker <-> worker: none
```

Users do not open `lab-gpu-*:8080`. After bring-up, sandbox egress is Forgejo plus loopback llama-server, nothing else.

The dispatch key is not the admin key. Authorized key on the worker:

```text
command="/usr/local/bin/df-planner-session",restrict ssh-ed25519 AAAA... df-dispatch
```

`df-planner-session` checks the node is `free`, marks it `busy`, starts `omp --mode rpc` in the run directory, and clears `busy` on exit. No shell, no port forward, no second plan.

## 8.2 Ports

| Port | Service | Who |
|---|---|---|
| 22 | SSH | Admins. Dispatch key via forced command only. |
| 9100 | node-exporter | Prometheus on `infra-01`, admins |
| 8080 | llama-server | `infra-01` and admins. Bound on the tailnet interface if the host has a second NIC. |
| 443 | Caddy on `infra-01` | Users |

No NATS port. No Garage ports. No Matrix ports.

```bash
sudo ufw default deny incoming
# confirm the tailnet prefix: ip addr show tailscale0
sudo ufw allow from 100.64.0.0/10 to any port 22   proto tcp
sudo ufw allow from 100.64.0.0/10 to any port 8080 proto tcp
sudo ufw allow from 100.64.0.0/10 to any port 9100 proto tcp
sudo ufw enable
```

---

# 9. Sandbox

Ephemeral agents are Oh My Pi child sessions, not long-running NATS consumers.

```text
coder
  workspace: harness isolation under /srv/agent/workspaces/<run>/<task>/
  git:       scoped credential, push agent/<run>/<task> only
  model:     http://127.0.0.1:8080/v1
  tests:     constrained container, no docker.sock
  evidence:  /srv/agent/evidence/<run>/<task>/<attempt>/

reviewer | tester | security-reviewer
  workspace: fresh clone under /srv/agent/checkouts/<run>/<task>/
  git:       read credential, or a credential that cannot push protected branches
  model:     http://127.0.0.1:8080/v1
  checks:    the planner named the command; the agent does not invent a merge
```

Policy for every ephemeral session:

- No Docker socket.
- No host GPU device in the sandbox. The GPU is llama-server's.
- No dispatch key, no admin SSH key, no PostgreSQL password.
- CPU, RAM, and PID limits. A wall-clock timeout kills the session.
- Network from a sandbox: Forgejo on `infra-01`, and loopback `:8080`. Default deny elsewhere.

---

# 10. Observability

```text
workers -- :9100 --> Prometheus (infra-01) --> Grafana
llama-server metrics --> Prometheus
run index --> PostgreSQL
planner session + ephemeral yields --> /srv/agent/sessions/<run_id>/
evidence --> /srv/agent/evidence/...  -- rsync --> infra-01 staging --> Restic
user-visible completion and escalation --> Open WebUI
```

Labels: `instance="lab-gpu-02"`, `run_id`, `state=free|busy|draining`. Do not label nodes `coder` or `reviewer`.

Dashboards: GPU temperature and memory, tokens/second, disk percent, sandbox failures, evidence bytes, count of busy workers.

---

# 11. Failure and growth

## 11.1 Worker dies mid-plan

```text
dispatch helper sees the RPC session drop
  -> run index status=failed, unless a complete planner reply was already stored
  -> evidence not yet rsynced may be gone
  -> Forgejo keeps any pushed branch
  -> orchestrator retries on a different free worker, or escalates
  -> the retry is a new planner session, not a revived coder
```

Do not mark the Open WebUI conversation complete.

## 11.2 Adding a laptop

1. Ansible worker role: Vulkan, llama-server, models, Node Exporter, Oh My Pi, `df-planner-session`, UFW.
2. MagicDNS name and `tag:worker`.
3. Install the dispatch forced command.
4. It is eligible when state is `free`. No role flag.

## 11.3 Backpressure

If all five workers are `busy`, the dispatch helper does not start a sixth plan on a busy node. The run index stays `queued`. Open WebUI can show that. It must not show the plan as running.

---

# 12. Bring-up

Maps to architecture stages 5–7.

```text
A  all workers: OS, Vulkan, Node Exporter, UFW, /srv layout
B  lab-gpu-01: llama-server + qwen3-8b + admin diagnostic chat
C  same inference role on 02-05; same model file and checksum
D  Oh My Pi + planner agents on 01; df-planner-session forced command
E  one non-code plan: tasker, reviewer, reply in Open WebUI
F  one code plan: coder, reviewer, tester, security reviewer,
   branch in Forgejo, evidence on disk
G  harness role on 02-05; two plans at once land on two nodes;
   a third waits
H  rsync + Restic; kill a worker mid-plan and confirm status=failed
```

First useful worker milestone: one Intel GPU endpoint, one planner session, one ephemeral tasker/reviewer loop, reply visible in Open WebUI.

---

# 13. Decision log

- **Oh My Pi on the worker, not a NATS consumer.** The server orchestrator sends a plan. The worker planner spawns ephemeral agents with the harness task tool. A queue service would duplicate that.
- **No fixed coder/reviewer nodes.** The old split (01/02 coder, 03/04 reviewer, 05 overflow) fought the new rule that one worker owns a plan's task loop. Independence is a blank child session plus a fresh checkout.
- **Planner chooses types.** Software engineering gets coder, reviewer, tester, and security reviewer. Other plans do not inherit that list.
- **One plan per worker.** 32 GB UMA, one llama-server, serial tasker-then-reviewer. Parallel large contexts are a later measurement, not the default.
- **Vulkan llama-server is the only GPU user.** Sandboxes call the HTTP API. No GPU passthrough into agent containers.
- **Garage stays out.** Evidence on disk, run index for paths, nightly rsync, Restic on the external disk. S3 later does not change delegation.
- **Element and Conduit stay out.** Completion and escalation return to the Open WebUI conversation that started the run.
- **Dispatch key is a forced command.** The orchestrator never receives a worker shell.
