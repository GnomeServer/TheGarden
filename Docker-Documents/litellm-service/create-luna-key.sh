#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
ENV_FILE="${ENV_FILE:-.env}"
KEY_FILE="${KEY_FILE:-.luna-worker-api-key}"

if [[ ! -r "$ENV_FILE" ]]; then
  printf 'Missing %s. Copy .env.example to .env first.\n' "$ENV_FILE" >&2
  exit 1
fi

# The deployment .env contains only shell-compatible KEY=value settings.
set -a
. "$ENV_FILE"
set +a

: "${LITELLM_MASTER_KEY:?LITELLM_MASTER_KEY is missing from .env}"
BASE_URL="${LITELLM_ADMIN_URL:-http://127.0.0.1:${LITELLM_PORT:-4000}}"
BASE_URL="${BASE_URL%/}"
CURL_ARGS=(--fail-with-body --silent --show-error)
if [[ "${LITELLM_INSECURE_TLS:-0}" == "1" ]]; then
  CURL_ARGS+=(--insecure)
fi

if [[ -s "$KEY_FILE" && "${ROTATE:-0}" != "1" ]]; then
  printf 'A Luna worker key already exists at %s.\n' "$KEY_FILE"
  printf 'Set ROTATE=1 only if you intentionally want another key.\n'
  exit 0
fi

# Liveness can succeed while LiteLLM is still connecting to PostgreSQL. Wait
# for the readiness endpoint before creating a key so startup races do not
# result in a connection reset.
ready=0
for _ in {1..30}; do
  if curl "${CURL_ARGS[@]}" --max-time 5 "${BASE_URL}/health/readiness" >/dev/null; then
    ready=1
    break
  fi
  sleep 2
done
if [[ "$ready" != "1" ]]; then
  printf 'LiteLLM did not become ready at %s. Check: docker compose logs --tail=100 litellm\n' "$BASE_URL" >&2
  exit 1
fi

response="$({
  curl "${CURL_ARGS[@]}" \
    --request POST "${BASE_URL}/key/generate" \
    --header "Authorization: Bearer ${LITELLM_MASTER_KEY}" \
    --header 'Content-Type: application/json' \
    --data '{
      "key_alias": "luna-worker",
      "key_type": "llm_api",
      "models": ["luna"],
      "metadata": {
        "service": "agent-workers",
        "model": "luna"
      }
    }'
})"

key="$(printf '%s' "$response" | python3 -c '
import json
import sys

payload = json.load(sys.stdin)
key = payload.get("key") or payload.get("token")
if not key and isinstance(payload.get("info"), dict):
    key = payload["info"].get("token") or payload["info"].get("key")
if not key:
    raise SystemExit("LiteLLM did not return a virtual key")
print(key)
')"

umask 077
printf '%s\n' "$key" > "$KEY_FILE"
printf 'Created the Luna virtual key and saved it to %s\n' "$KEY_FILE"
printf 'Use this value as Authorization: Bearer <key> on the worker nodes.\n'
printf 'The key is restricted to the LiteLLM model name: luna\n'
