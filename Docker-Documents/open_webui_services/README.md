# Open WebUI and Ollama

This Compose project runs Open WebUI and Ollama as separate containers on `infra-lab-services`.

## Current deployment

- Open WebUI container: `open-webui`
- Ollama container: `ollama`
- Open WebUI container port: `8080`, not published to the host
- Ollama container port: `11434`, not published to the host
- Docker network shared with Caddy: `caddy_proxy`
- Private network: `open-webui_internal`
- Compose `WEBUI_URL` default: `https://infra-lab-services.tail494f6d.ts.net:8443/`
- Compose `ENABLE_SIGNUP` default: `true` via `OPEN_WEBUI_ENABLE_SIGNUP`

There is no committed `.env.example`. Caddy terminates TLS and routes
`https://infra-lab-services.tail494f6d.ts.net:8443/` to `open-webui:8080`.
Open WebUI is not directly published on the host. Tailnet browsers must trust
Caddy's internal root CA.

## Files and volumes

```text
compose.yml       Compose service definition
.env              Optional local overrides; do not commit. There is no .env.example.
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

For a new Open WebUI deployment, start from the Compose defaults. Create a private `.env` only when overriding them:

```bash
cd "$REPO_ROOT/Docker-Documents/open_webui_services"
install -m 600 /dev/null .env
```

There is no `.env.example` to copy. `compose.yml` defaults are:

```dotenv
OLLAMA_IMAGE=ollama/ollama:latest
OPEN_WEBUI_IMAGE=ghcr.io/open-webui/open-webui:latest
OPEN_WEBUI_URL=https://infra-lab-services.tail494f6d.ts.net:8443/
OPEN_WEBUI_ENABLE_SIGNUP=true
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

From a tailnet browser, check `https://infra-lab-services.tail494f6d.ts.net:8443/health`; from another container on `caddy_proxy`, use `http://open-webui:8080/health`.

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

Open WebUI should use the Docker URL. Remote workers use the Caddy endpoint:

```text
https://infra-lab-services.tail494f6d.ts.net/litellm/v1
```

## Naming Agent Manager work from Open WebUI

Open WebUI's Pipe source is `agent_manager_pipe.py`; `test_agent_manager_pipe.py`
is a unit-test file and must not be imported as the Pipe. When a user submits a
task without `Project: <name>`, the Pipe asks for the project name in an input
dialog. A leading `Script: <filename>` directive overrides the filename
derived from the task:

```text
Project: quarterly-audit
Script: audit_report.py

Create a dependency-free script that writes a report.json file.
```

Named scripts are stored at `<workspace>/<project-slug>/<normalized-filename>`. `.py` is added only when omitted. Duplicates become `name_2.py`, `name_3.py`. The UUID remains the Agent Manager run ID. The Pipe requires the `FORGEJO_REPOSITORY` valve. It stops on `completed`, `failed`, or `cancelled`, not on `cancel_requested`.
API callers without an input dialog must include `Project: <name>`.

Completed work is returned as Manager-hosted artifact downloads, not inline
source. A single output file downloads directly; multiple output files are
packaged as a ZIP bundle. Download links are signed and expire after seven
days. Worker workspaces are temporary and are cleaned up after the job, so
download artifacts before the links expire rather than relying on workspace files remaining available.

The Pipe's `AGENT_MANAGER_API_TOKEN` valve must be set to the separate
run-only `AGENT_MANAGER_OPENWEBUI_TOKEN` from Agent Manager's private `.env`.
Do not use the administrator `AGENT_MANAGER_API_TOKEN` or a LiteLLM key.
Artifact links are Manager-issued signed URLs; the Pipe never exposes upload tokens.

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

- **Blank page:** use the configured `WEBUI_URL` at `https://infra-lab-services.tail494f6d.ts.net:8443/`; the Caddy route uses a separate origin because Open WebUI assets are root-relative.
- **502 from Caddy on port 8443:** verify the `open-webui` container is running and attached to `caddy_proxy`. A 502 on `/dashboard/` indicates the Agent Manager upstream; the root path falls back to Proxmox.
- **No models:** run `ollama list`; models must be pulled intentionally, or check that the LiteLLM virtual key is allowed to use `luna`.
- **Ollama connection error:** Open WebUI should use `http://ollama:11434`, not a host-published port.
- **LiteLLM 401:** use the generated `.luna-worker-api-key`, not `LITELLM_MASTER_KEY` or the backend `LLAMA_API_KEY`.
- **LiteLLM backend error:** verify `LUNA_API_BASE` and `LUNA_BACKEND_API_KEY` in the local LiteLLM `.env` file.
- **llama.cpp 401:** use the current `LLAMA_API_KEY` from the llama.cpp `.env`.
- **llama.cpp model 404:** use the exact model ID returned from `/v1/models`.

Keep `.env` files private and rotate credentials if they are disclosed.
