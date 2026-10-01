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
