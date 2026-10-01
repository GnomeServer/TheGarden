# llama.cpp service

This Compose project runs the CPU-based `llama.cpp` server for the local VM.

## Current deployment

- Image: `ghcr.io/ggml-org/llama.cpp:server`
- Model: `gemma-3-4b-it-Q4_K_M.gguf`
- Internal service name: `llama`
- Internal port: `8080`
- Docker network: `caddy_proxy`
- GPU offload: disabled (`-ngl 0`)
- Current VM allocation: 6 virtual CPUs and approximately 15 GiB RAM

The current model is stored under this repository project:

```text
TheGarden/Docker-Documents/llama_services/models/gemma-3-4b-it-Q4_K_M.gguf
```

The Compose file mounts `./models` read-only into the container at `/models`. The Compose project does not download models automatically.

## Files

```text
compose.yml       Compose service definition
.env              Local secrets and runtime settings; do not commit
.env.example      Non-secret configuration template
models/           GGUF model files
```

## Setup

Create the environment file if this is a new deployment:

```bash
export REPO_ROOT=/path/to/TheGarden
cd "$REPO_ROOT/Docker-Documents/llama_services"
cp .env.example .env
chmod 600 .env
```

Edit `.env` and set a strong API key and the name of a model that exists in `models/`:

```dotenv
LLAMA_IMAGE=ghcr.io/ggml-org/llama.cpp:server
LLAMA_MODEL=gemma-3-4b-it-Q4_K_M.gguf
LLAMA_THREADS=5
LLAMA_CONTEXT_SIZE=4096
LLAMA_API_KEY=<long-random-secret>
```

The external `caddy_proxy` network must exist before starting this service. Start Caddy first if necessary:

```bash
cd "$REPO_ROOT/Docker-Documents/caddy_service"
sudo docker compose up -d caddy
```

Then start llama.cpp:

```bash
cd "$REPO_ROOT/Docker-Documents/llama_services"
sudo docker compose config --quiet
sudo docker compose pull
sudo docker compose up -d
sudo docker compose ps
```

`docker compose pull` downloads the container image if needed. It does not download the GGUF model.

## API access

The service is available to other containers on `caddy_proxy` at:

```text
http://llama:8080/v1
```

Caddy also exposes the API through the Tailscale-only HTTPS host under `/llama/`:

```text
https://infra-lab-services.tail494f6d.ts.net/llama/v1/models
```

The external endpoint requires the API key from `.env`:

```bash
cd "$REPO_ROOT/Docker-Documents/llama_services"
set -a
. ./.env
set +a

curl -k -H "Authorization: Bearer $LLAMA_API_KEY" \
  https://infra-lab-services.tail494f6d.ts.net/llama/v1/models
```

The model ID returned by `/v1/models` should be used when making chat-completion requests. With the current model it is typically:

```text
/models/gemma-3-4b-it-Q4_K_M.gguf
```

Example generation request:

```bash
curl -k \
  -H "Authorization: Bearer $LLAMA_API_KEY" \
  -H 'Content-Type: application/json' \
  https://infra-lab-services.tail494f6d.ts.net/llama/v1/chat/completions \
  -d '{
    "model": "/models/gemma-3-4b-it-Q4_K_M.gguf",
    "messages": [{"role": "user", "content": "Reply with exactly OK"}],
    "max_tokens": 16,
    "temperature": 0
  }'
```

## Open WebUI integration

Open WebUI is on the same Docker network, so add an OpenAI-compatible connection in its administrator settings:

```text
Base URL: http://llama:8080/v1
API key:  LLAMA_API_KEY from this project's .env
```

Select the exact model ID returned by `/v1/models`.

## Performance tuning

The VM does not currently have a usable NVIDIA GPU, so inference is CPU-only. The important settings are:

```text
-t 5       CPU threads
-c 4096    context size
-ngl 0     GPU layers; zero means CPU-only
```

The VM has six virtual CPUs. Benchmark `LLAMA_THREADS=4`, `5`, and `6` rather than assuming the highest value is fastest. Lowering `LLAMA_CONTEXT_SIZE` to `2048` can reduce prompt-processing work when long conversations are not needed. A smaller quantized model will generally generate faster.

After changing `.env`, recreate the service:

```bash
sudo docker compose up -d --force-recreate llama
```

## Logs and troubleshooting

```bash
sudo docker compose ps
sudo docker compose logs --tail=100 llama
```

Common issues:

- **Model not found:** confirm the filename in `LLAMA_MODEL` exactly matches a file in `models/`.
- **401 Unauthorized:** the Open WebUI API key does not match `LLAMA_API_KEY`.
- **404 model not found:** use the exact model ID returned by `/v1/models`.
- **Connection refused:** confirm both llama.cpp and the caller are attached to `caddy_proxy`.
- **Slow responses:** expected on CPU-only inference; check thread count, context size, and model size.

Do not expose the API key in logs, screenshots, or committed files. Rotate it if it has been disclosed.
