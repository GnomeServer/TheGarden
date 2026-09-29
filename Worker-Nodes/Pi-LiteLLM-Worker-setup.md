# Pi + LiteLLM worker setup

This guide configures the Pi coding harness on a worker to use the shared
LiteLLM gateway over the Tailscale network.

## Current gateway

```text
Base URL:     https://infra-lab-services.tail494f6d.ts.net/litellm/v1
Model ID:     luna
Display name: GPT-5.6 Luna
```

The gateway currently exposes the model as `luna`. Do not use
`gpt-5.6-luna` in API requests unless it is added as a LiteLLM model alias.

## 1. Install the Caddy root certificate

The gateway uses Caddy's private local CA. Install the **root certificate**,
not the server certificate, intermediate certificate, or private root key.
The root certificate is public; never copy the corresponding `root.key`.

Copy `caddy-local-root.crt` to the worker, then run:

```bash
install -Dm644 caddy-local-root.crt \\
  "$HOME/.pi/agent/certs/caddy-local-root.crt"
```

Verify the certificate:

```bash
openssl x509 \\
  -in "$HOME/.pi/agent/certs/caddy-local-root.crt" \\
  -noout -subject -issuer -dates -fingerprint -sha256
```

The current root fingerprint is:

```text
F2:40:63:29:A9:EF:70:1A:9C:EE:4C:FA:4F:21:3A:2D:E0:37:4F:0B:4F:43:AA:73:25:AB:D2:91:74:BC:92:EA
```

If you administer the gateway VM directly, the source file is:

```text
/data/caddy/pki/authorities/local/root.crt
```

inside the Caddy container's `/data` volume. Copy only `root.crt` through a
trusted channel. If the Caddy CA is regenerated, distribute the new root and
update the fingerprint in this guide.

### Optional system-wide installation

For programs that use the operating system CA store:

```bash
sudo install -m0644 \\
  "$HOME/.pi/agent/certs/caddy-local-root.crt" \\
  /usr/local/share/ca-certificates/infra-lab-caddy-root.crt
sudo update-ca-certificates
```

Pi also uses `NODE_EXTRA_CA_CERTS` below because Node's bundled CA behavior can
differ from the system CA store.

## 2. Install the worker's LiteLLM virtual key

Put the worker-specific virtual key in a local file. Do not put it in this
Markdown file or commit it to source control.

```bash
install -m600 /path/to/worker-api-key "$HOME/.config/litellm/worker-api-key"
```

Alternatively, export it in the environment:

```bash
export LITELLM_API_KEY='your-virtual-key'
```

## 3. Configure Pi

Create `~/.pi/agent/models.json`:

```json
{
  "providers": {
    "litellm": {
      "name": "LiteLLM (infra lab)",
      "baseUrl": "https://infra-lab-services.tail494f6d.ts.net/litellm/v1",
      "api": "openai-completions",
      "apiKey": "!tr -d '\\r\\n' < $HOME/.config/litellm/worker-api-key",
      "models": [
        {
          "id": "luna",
          "name": "GPT-5.6 Luna",
          "reasoning": true,
          "input": ["text"],
          "contextWindow": 272000,
          "maxTokens": 32768,
          "cost": {
            "input": 0,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0
          },
          "compat": {
            "supportsStore": false,
            "supportsDeveloperRole": true,
            "supportsReasoningEffort": true,
            "supportsUsageInStreaming": true,
            "maxTokensField": "max_completion_tokens"
          }
        }
      ]
    }
  }
}
```

If using an environment variable instead of a key file, replace the `apiKey`
line with:

```json
"apiKey": "$LITELLM_API_KEY"
```

Set Pi's default model in `~/.pi/agent/settings.json`:

```json
{
  "defaultProvider": "litellm",
  "defaultModel": "luna"
}
```

Merge these keys into an existing settings file; do not overwrite unrelated
settings.

## 4. Launch Pi with the CA trusted

Create `~/.pi/agent/bin/pi` before the normal Pi binary in `PATH`:

```sh
#!/bin/sh
export NODE_EXTRA_CA_CERTS="${PI_CADDY_CA_FILE:-$HOME/.pi/agent/certs/caddy-local-root.crt}"
exec /absolute/path/to/the/real/pi "$@"
```

Then make it executable and ensure the directory comes first in `PATH`:

```bash
chmod 755 "$HOME/.pi/agent/bin/pi"
export PATH="$HOME/.pi/agent/bin:$PATH"
hash -r 2>/dev/null || true
```

Do **not** set `NODE_TLS_REJECT_UNAUTHORIZED=0`. That disables TLS verification
for the whole Pi process. The Caddy root certificate makes normal certificate
validation work.

## 5. Verify the worker

Check the gateway certificate and model list without exposing the key:

```bash
CA="$HOME/.pi/agent/certs/caddy-local-root.crt"
KEY="$(tr -d '\\r\\n' < "$HOME/.config/litellm/worker-api-key")"

curl --fail --silent --show-error --cacert "$CA" \\
  -H "Authorization: Bearer $KEY" \\
  https://infra-lab-services.tail494f6d.ts.net/litellm/v1/models
```

Expected model list contains `luna`.

Check Pi's registration:

```bash
pi --list-models luna
```

Expected output includes a `litellm` provider and model `luna`.

Start the harness:

```bash
pi
```

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `unable to get local issuer certificate` | The Caddy root is missing or `NODE_EXTRA_CA_CERTS` points to the wrong file. |
| HTTP 401 from `/v1/models` | Wrong, expired, or revoked LiteLLM virtual key. |
| HTTP 403 for `gpt-5.6-luna` | Use the configured LiteLLM alias `luna`. |
| HTTP 429 or deployment cooldown | The LiteLLM upstream deployment is unavailable or its provider credential is exhausted/invalid. |
| Pi lists no `luna` model | Check `~/.pi/agent/models.json`, JSON syntax, and Pi's config directory. |
| Pi command ignores the CA | Start a new shell, check `command -v pi`, or run `hash -r`. |

## Security notes

- Keep virtual keys at mode `600`.
- Keep `models.json` at mode `600` when it contains a literal key or a
  command that reads a private key file.
- The Caddy root certificate is not secret. The Caddy root private key is
  highly sensitive and must remain on the gateway host.
- Do not use `curl -k` or `NODE_TLS_REJECT_UNAUTHORIZED=0` as the permanent
  configuration.
