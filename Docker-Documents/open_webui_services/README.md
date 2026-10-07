# Open WebUI and Ollama

This Compose project runs Open WebUI and Ollama as separate containers on `infra-lab-services`.

## Current deployment

- Open WebUI container: `open-webui`
- Ollama container: `ollama`
- LiteLLM gateway: `litellm` (optional shared model gateway)
- Open WebUI URL: `https://infra-lab-services.tail494f6d.ts.net:8443/`
- Open WebUI internal port: `8080`
- Ollama internal port: `11434`
- Docker network shared with Caddy: `caddy_proxy`
- Private network between Open WebUI and Ollama: `open-webui_internal`

Neither service publishes its application port directly to the VM LAN. Caddy publishes HTTPS port `8443` and proxies to Open WebUI.

## Why port 8443 is used

Open WebUI serves root-relative frontend assets such as `/static/...` and `/_app/...`. Serving it under `/open-webui/` caused the HTML to load while the JavaScript assets were requested from the wrong path, resulting in a blank page.

Caddy therefore gives Open WebUI its own HTTPS listener:

```text
https://infra-lab-services.tail494f6d.ts.net:8443/
```

## Files and volumes

```text
compose.yml       Compose service definition
.env              Local settings; do not commit
.env.example      Non-secret configuration template
agent_manager_pipe.py  Admin-installed Agent Manager coder Function
tests/                 Pipe unittest regression scenarios
```

Persistent Docker volumes:

```text
open-webui_data          Open WebUI users, settings, and data
open-webui_ollama_data   Ollama models and runtime data
```

## Start the services

Caddy must already be running and must create the external `caddy_proxy` network:

```bash
export REPO_ROOT=/path/to/TheGarden
cd "$REPO_ROOT/Docker-Documents/caddy_service"
sudo docker compose up -d caddy
```

For a new Open WebUI deployment:

```bash
cd "$REPO_ROOT/Docker-Documents/open_webui_services"
cp .env.example .env
chmod 600 .env
```

Review `.env` before starting. The current settings are:

```dotenv
OLLAMA_IMAGE=ollama/ollama:latest
OPEN_WEBUI_IMAGE=ghcr.io/open-webui/open-webui:latest
OPEN_WEBUI_URL=https://infra-lab-services.tail494f6d.ts.net:8443/
OPEN_WEBUI_ENABLE_SIGNUP=false
```

Start or update the stack:

```bash
sudo docker compose config --quiet
sudo docker compose pull
sudo docker compose up -d
sudo docker compose ps
```

After changing `WEBUI_URL` or an image setting, recreate Open WebUI:

```bash
sudo docker compose up -d --force-recreate open-webui
```

## Verify Open WebUI

Health endpoint:

```bash
curl -k -sS \
  -o /dev/null \
  -w 'HTTP status: %{http_code}\n' \
  https://infra-lab-services.tail494f6d.ts.net:8443/health
```

Expected result:

```text
HTTP status: 200
```

The browser URL is:

```text
https://infra-lab-services.tail494f6d.ts.net:8443/
```

The `-k` option is needed for command-line tests until the client trusts Caddy's internal CA.

Check logs:

```bash
sudo docker compose logs --tail=100 open-webui
sudo docker compose logs --tail=100 ollama
```

## Ollama models

List models without downloading anything:

```bash
sudo docker compose exec ollama ollama list
```

Download a model intentionally when needed:

```bash
sudo docker compose exec ollama ollama pull llama3.2:3b
```

Run a model test using the exact name shown by `ollama list`:

```bash
sudo docker compose exec ollama \
  ollama run llama3.2:3b 'Reply with exactly OK'
```

Ollama is configured in Open WebUI through the internal URL:

```text
http://ollama:11434
```

## Using LiteLLM as the shared gateway

LiteLLM is deployed from `TheGarden/Docker-Documents/litellm-service`. It
exposes the stable model name `luna`, manages virtual API keys in PostgreSQL,
and routes requests to the configured inference worker. Start that project
before adding the connection:

```bash
cd "$REPO_ROOT/Docker-Documents/litellm-service"
sudo docker compose up -d
./create-luna-key.sh
```

In **Admin Panel → Settings → Connections → OpenAI API Connections**, use:

```text
Base URL: http://litellm:4000/v1
API key:  contents of the local LiteLLM `.luna-worker-api-key` file
Model:    luna
```

Enter this URL in Open WebUI's server-side connection settings; do not open it directly in Firefox. `litellm` is a Docker-only hostname. Open WebUI and LiteLLM are both attached to `caddy_proxy`, so the internal service name works from the Open WebUI container without publishing LiteLLM to the LAN. The external worker URL is:

```text
https://infra-lab-services.tail494f6d.ts.net/litellm/v1
```

## Agent Manager coder Function (manual, opt-in)

[`agent_manager_pipe.py`](agent_manager_pipe.py) is a native Open WebUI **Function/Pipe**, not a Tool, external Pipelines server, or ordinary OpenAI connection. It submits real work through `POST /v1/runs` and follows `GET /v1/runs/{id}` until the Manager reports a terminal state. It uses the official [`Pipe` class and async `pipe()` interface](https://docs.openwebui.com/features/extensibility/plugin/functions/pipe), [admin Valves](https://docs.openwebui.com/features/extensibility/plugin/development/valves), and [`status` events](https://docs.openwebui.com/features/extensibility/plugin/development/events). `httpx` and `pydantic` are Open WebUI dependencies; the Function does not install packages or access local secret files.

This repository does **not** install, enable, or mutate a live Function. An administrator must review and explicitly install it. Do not run the new-stack instructions above against the existing deployment just to install this Function: preserve the existing `open-webui_data` volume, accounts, settings, secret key, and deployed image tag. No Compose change, image pull, volume replacement, or account reset is required for the Function.

### Prerequisites and credential boundary

- Agent Manager and Open WebUI must both join the external Docker network **`caddy_proxy`**. The existing Open WebUI Compose file already does. Use `http://agent-manager:8000` from the Open WebUI backend; do not use `/dashboard`, a Caddy login page, `localhost`, or a browser-facing API proxy as the Valve URL. The Function sends requests directly, ignores HTTP proxy environment variables, and does not follow redirects. It needs only POST run submission and GET individual-run access.
- Configure the Manager's separate **`AGENT_MANAGER_OPENWEBUI_TOKEN`** through its private deployment settings. The same value goes into the Function's **`AGENT_MANAGER_API_KEY`** admin Valve. This is an independent **run-only service credential**; it must not be the bootstrap admin token, a dashboard cookie, a Forgejo password, a provider key, or a LiteLLM master key. Dashboard/admin endpoints reject the run-only token. Normal users must never receive it through chat, browser connection settings, or `UserValves`.
- Admin Valves live in the existing Open WebUI database. The password input masks the credential on screen but is not encryption at rest. Restrict administrator/database/backup access; use the documented Valve encryption option only with a deliberately managed, stable existing `WEBUI_SECRET_KEY`. Do not rotate that key or change existing authentication settings as part of this installation.
- At least one approved worker must be running with `WORKER_ROLE=coder`, NATS connectivity, and **`ALLOW_GENERATED_CODE=1`**. Disabled workers do not pass the Manager admission check; a stopped or disabled pool produces `503 NO_WORKER_AVAILABLE`, not a successful queued run. Current pool: `worker-01` Raphael, `worker-02` Inkii, `worker-03` Donatello, `worker-04` Naruto, all coder role. See the [worker environment and execution requirements](../../agent-worker/README.md#worker-environment).
- The enabled worker needs its own valid `MODEL_BASE_URL`, restricted `MODEL_API_KEY`, and `MODEL_NAME=luna`, plus model connectivity and a writable isolated workspace. The Pipe sends `model=luna`, but the current worker's generation code uses its environment `MODEL_NAME`; setting the Valve alone does not reconfigure worker inference. Admission checks reachability, not model health or a reserved execution slot.
- Use an approved disposable/test worker: generated Python executes as the worker user and is **not sandboxed**. Prompt restrictions are not a security boundary. Do not enable this on a worker that exposes production data or secrets to generated code.

### Exact admin installation and model selection

Follow Open WebUI's official [manual Function installation](https://docs.openwebui.com/features/extensibility/plugin/functions/#create-manually):

1. Sign in as an administrator at `https://infra-lab-services.tail494f6d.ts.net:8443/`.
2. Open **Admin Panel → Functions → Create**. Set ID **`agent_manager_coder`**, name **`Agent Manager Coder`**, and a description such as “Submit a pooled create-python-script job”. IDs use letters, digits, and underscores.
3. Paste the **entire** reviewed `agent_manager_pipe.py` into the Python editor, replacing the example. Click **Save**. Open WebUI detects `class Pipe` automatically. Do not overwrite an unrelated existing live Function; if this ID already exists, review its source and save a backup before intentionally replacing it.
4. Open the Function's **Valves** gear and save these **administrator-only** values:

   | Valve | Value / bounds |
   | --- | --- |
   | `AGENT_MANAGER_URL` | `http://agent-manager:8000` |
   | `AGENT_MANAGER_API_KEY` | The private Manager `AGENT_MANAGER_OPENWEBUI_TOKEN` value |
   | `FORGEJO_REPOSITORY` | Explicit approved `owner/repository`; empty by default, so no job can submit until configured |
   | `MODEL_NAME` | `luna` |
   | `POLL_INTERVAL_SECONDS` | Default `2`; range `0.5`–`30` seconds |
   | `MAX_WAIT_SECONDS` | Default `300`; range `5`–`1800` seconds after submission |

5. Turn the Function's **Active** toggle on only when its permitted users and disposable worker environment are ready. It is a selectable Pipe, not a global Filter or Action. Apply the instance's normal model-access restrictions before exposing a job-executing model to other users.
6. Start a **new chat** and choose **Agent Manager Coder** from the model selector. Do not select the ordinary `luna` connection: that chats with inference directly and does not submit Manager jobs. Send a textual task once. Automatic title/tag/follow-up generation requests are ignored by the Pipe and cannot create jobs.

### Safe synthetic smoke and result interpretation

First use only an isolated test worker, a non-sensitive test repository in `FORGEJO_REPOSITORY`, and synthetic input. In the selected **Agent Manager Coder** chat, send once:

```text
Create a deterministic Python standard-library script in the current run directory.
Use the fixed values [2, 4, 6], assert their sum is 12, write smoke-result.json
containing {"sum": 12}, and print exactly PIPE_SMOKE_SUM=12.
Do not read existing files, use the network, invoke subprocesses, install
dependencies, request input, access credentials, or modify anything outside the run directory.
```

Observe submission, queued/running, and final completed/failed status events. The final response should contain the real run ID, reported worker ID, return code, stdout, stderr, and any worker/Manager error. Copy that run ID and inspect the same record in the Manager dashboard. A genuine worker-reported completion should include return code `0` and `PIPE_SMOKE_SUM=12`; a failed run must remain a failure even if it printed some expected output. Independently inspect `smoke-result.json` and the generated code in that disposable worker's run directory before claiming the artifact or task was validated. The Pipe itself does not perform that independent validation.

For a **throwaway local real-Manager integration smoke**, the actual class can also be invoked without installing anything into Open WebUI. Start an isolated Manager/database/NATS and disposable execution-enabled worker using the normal test setup, configure a synthetic run-only Manager token, and run the following from this directory in a Python environment with `httpx` and `pydantic`. Use only the isolated Manager port/token, not a live service. The token prompt is hidden; it is not supplied in command arguments or printed:

```bash
python -c '
import asyncio
import getpass
from agent_manager_pipe import Pipe

async def main():
    pipe = Pipe()
    pipe.valves = pipe.Valves(
        AGENT_MANAGER_URL=input("Isolated Manager origin (http://127.0.0.1:PORT): ").strip(),
        AGENT_MANAGER_API_KEY=getpass.getpass("Synthetic run-only Manager token: "),
        FORGEJO_REPOSITORY="smoke/disposable",
        MODEL_NAME="luna",
        MAX_WAIT_SECONDS=300,
    )
    async def status(event):
        print(event["data"]["description"], "done=", event["data"]["done"])
    result = await pipe.pipe(
        {"messages": [{"role": "user", "content": (
            "Create a Python standard-library script using only the fixed values [2,4,6]. "
            "Assert their sum is 12 and print PIPE_SMOKE_SUM=12. Do not read files, "
            "use the network or subprocesses, install packages, or access credentials."
        )}]},
        __user__={"id": "synthetic-smoke", "name": "Synthetic smoke"},
        __event_emitter__=status,
    )
    print(result)

asyncio.run(main())
'
```

Run the independent unittest scenarios from the repository root with:

```bash
python -m unittest discover -s Docker-Documents/open_webui_services/tests -p 'test_*.py'
```

These regression tests import the actual Function class and use independent queued/running/completed/failed Manager response scenarios. They cover failure recovery and credential hygiene; mocked HTTP transport is not proof of a live worker or deployed Function. The local real-Manager smoke above is a separate integration check and likewise does not establish that the live Open WebUI instance has installed/enabled this Function.

### Limits and recovery

- Each direct message to this Pipe is a real job. **Regenerate**, editing/resending a message, or retrying manually may create another job. There is no idempotency key or exactly-once guarantee. The Pipe sends **one POST only** and never automatically resubmits after a timeout or ambiguous response.
- On an unconfirmed POST response, a run may exist even though no ID reached the client. Do not resend. An operator must search the Manager dashboard by goal, requester, and submission time first. A non-admission `503` can occur after a record was created; it is not treated as proof that no run exists.
- Polling expiry, connection/auth failure while reading, closing the chat, or cancelling the UI wait **does not cancel the Manager run**. Preserve its ID. An operator can inspect it in the dashboard or use authenticated `GET /v1/runs/{id}` with the run-only credential from a trusted server-side client. The Pipe does not expose a browser token or provide a cancel action. Requests are bounded to 15 seconds, with reads additionally bounded by the remaining poll deadline; a known run's result remains in the Manager after the Pipe stops waiting.
- HTTP authorization failures return only administrator-action guidance, not raw response bodies or credentials. Worker stdout/stderr/error are reported as untrusted text; the configured service token is redacted if it appears. Do not ask workers to print any secrets.
- Only textual parts of the **latest user message** become `goal`; system/assistant prompts, previous goals, images, files, browser-selected models, and user-supplied metadata are not forwarded. An attachment-only latest message fails rather than silently rerunning an earlier task. No conversation memory or interactive input is implemented.
- Metadata is fixed to `worker_role=coder`, `operation=create-python-script`, `source=open-webui`, and `requester={id,name}` supplied by Open WebUI's `__user__`. Requester fields are **service-supplied attribution, not verified Forgejo identity or authorization**. The Pipe omits `worker_id` for pooled execution and uses `base_ref=main`.
- The current worker generates and executes a Python script; repository/base-ref are Manager context, not a claim that the worker clones, commits, or opens a Forgejo change. Worker output can be truncated by worker limits. Completion and exit code are not proof of independent validation or review. OMP/JEV/planner/reviewer orchestration remains planned and is not provided by this Pipe.

## Using llama.cpp directly

The direct llama.cpp connection remains useful for backend diagnostics and bypass testing. It is also attached to `caddy_proxy` and can be added under:

```text
Admin Panel → Settings → Connections → OpenAI API Connections
```

Use:

```text
Base URL: http://llama:8080/v1
API key:  `LLAMA_API_KEY` from the local llama.cpp `.env` file
```

Select the exact model ID returned by llama.cpp's `/v1/models` endpoint. Use LiteLLM for normal Open WebUI and worker traffic when the request should be authenticated with a virtual key and routed as `luna`.

## Testing backend connectivity from the Open WebUI container

The Open WebUI image may not include `wget`, so an error such as `executable file not found in $PATH` means the diagnostic tool is missing, not that the backend is unavailable.

If Python is available in the image, test llama.cpp with:

```bash
sudo docker compose exec open-webui \
  python3 -c "import urllib.request; print(urllib.request.urlopen('http://litellm:4000/health/liveliness', timeout=5).read().decode())"
```

If that fails because `python3` is not present, use the service logs and the health endpoint instead. Do not install diagnostic packages into the application container just for this check.

## All-in-one alternative

Open WebUI also provides an image that includes Ollama:

```text
ghcr.io/open-webui/open-webui:ollama
```

The separate-container arrangement used here is preferred because Open WebUI and Ollama can be upgraded, restarted, monitored, and backed up independently.

## Troubleshooting

- **Blank page:** use the dedicated `:8443` URL, not the old `/open-webui/` path. Open WebUI's root-relative assets do not work reliably behind that path prefix.
- **502 from Caddy:** check that `open-webui`, `litellm`, or the selected backend is running and attached to `caddy_proxy`.
- **No models:** run `ollama list`; models must be pulled intentionally, or check that the LiteLLM virtual key is allowed to use `luna`.
- **Ollama connection error:** Open WebUI should use `http://ollama:11434`, not a host-published port.
- **LiteLLM 401:** use the generated `.luna-worker-api-key`, not `LITELLM_MASTER_KEY` or the backend `LLAMA_API_KEY`.
- **LiteLLM backend error:** verify `LUNA_API_BASE` and `LUNA_BACKEND_API_KEY` in the local LiteLLM `.env` file.
- **llama.cpp 401:** use the current `LLAMA_API_KEY` from the llama.cpp `.env`.
- **llama.cpp model 404:** use the exact model ID returned from `/v1/models`.

Keep `.env` files private and rotate credentials if they are disclosed.
