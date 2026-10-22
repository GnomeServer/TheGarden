#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
DEST="$REPO_ROOT/.secrets/caddy-root.crt"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

sudo -n docker cp caddy:/data/caddy/pki/authorities/local/root.crt "$TMP_DIR/root.crt"
openssl x509 -in "$TMP_DIR/root.crt" -noout >/dev/null
mkdir -p "$(dirname -- "$DEST")"
install -m 0644 "$TMP_DIR/root.crt" "$DEST"
printf 'Exported the public Caddy root certificate to %s\n' "$DEST"
