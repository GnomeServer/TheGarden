#!/usr/bin/env python3
"""Plan or apply a VM-local, manager-only central dashboard deployment."""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit


PUBLIC_URL = "https://infra-lab-services.tail494f6d.ts.net"
EXPECTED_USER = "infra-lab-user"
VOLUMES = {"manager_postgres_data": "agent-manager_postgres_data", "nats_data": "agent-manager_nats_data"}
SERVICES = {"agent-manager", "postgres", "nats", "nats-exporter"}
SOURCE_FILES = ("compose.yml", "Dockerfile", "requirements.txt")
SNIPPET = "central_dashboard_manager"
MANAGER_PATHS = "/dashboard /dashboard/* /auth/* /v1/* /docs /docs/* /openapi.json"
ROUTE = f"\t@agent_manager {{\n\t\tpath {MANAGER_PATHS}\n\t}}\n\thandle @agent_manager {{\n\t\treverse_proxy agent-manager:8000\n\t}}"


class DeploymentError(Exception):
    """A deliberately non-secret operator-facing error."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise DeploymentError(message)


@dataclass(frozen=True)
class EnvEntry:
    line: int
    value: str
    start: int
    end: int


def parse_env(text: str) -> dict[str, EnvEntry]:
    """Read a conservative single-line dotenv subset without evaluating anything."""
    entries = {}
    for number, line in enumerate(text.splitlines(keepends=True)):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*", line.rstrip("\r\n"))
        require(match is not None, "Unsupported .env syntax; normalize it privately before deployment.")
        key = match[1]
        require(key not in entries, "Duplicate .env keys are ambiguous; normalize them before deployment.")
        start = match.end()
        tail = line[start:].rstrip("\r\n")
        if tail.startswith(("'", '"')):
            quote = tail[0]
            end = 1
            while end < len(tail):
                if tail[end] == "\\" and quote == '"':
                    end += 2
                elif tail[end] == quote:
                    break
                else:
                    end += 1
            require(end < len(tail), "Multiline or unclosed .env quoting is unsupported; normalize it privately.")
            require(not tail[end + 1:].strip() or tail[end + 1:].lstrip().startswith("#"), "Unsupported trailing .env syntax.")
            value = tail[1:end]
            end += 1
        else:
            raw = re.split(r"(?:^|\s)#", tail, maxsplit=1)[0].rstrip()
            value, end = raw, len(raw)
        entries[key] = EnvEntry(number, value, start, start + end)
    return entries


def public_origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == "https" and parsed.hostname and "." in parsed.hostname
                 and not parsed.username and not parsed.password and parsed.port in (None, 443)
                 and parsed.path in ("", "/") and not parsed.query and not parsed.fragment
                 and re.fullmatch(r"[a-zA-Z0-9.-]+", parsed.hostname))
    except ValueError:
        valid = False
    require(bool(valid), "DASHBOARD_PUBLIC_URL must be an HTTPS hostname origin, without credentials or a path.")
    return "https://" + parsed.hostname


def prepare_env(text: str, origin: str, *, generate: bool) -> tuple[str, dict[str, str]]:
    entries = parse_env(text)
    lines = text.splitlines(keepends=True)
    changes = {}
    required = {
        "DASHBOARD_PUBLIC_URL": origin,
        "DASHBOARD_SESSION_SECURE": "true",
        "PROMETHEUS_URL": "http://prometheus:9090",
        "GRAFANA_NODE_DASHBOARD_UID": "server-overview",
    }
    for key, value in required.items():
        current = entries[key].value if key in entries else ""
        if current:
            matches = public_origin(current) == origin if key == "DASHBOARD_PUBLIC_URL" else current == value
            require(matches, f"Existing {key} conflicts with the reviewed central setting; correct it privately first.")
        else:
            changes[key] = value
    credential_keys = ("DASHBOARD_SESSION_SECRET", "AGENT_MANAGER_OPENWEBUI_TOKEN")
    existing_credentials = [entries[key].value for key in (*credential_keys, "AGENT_MANAGER_API_TOKEN")
                            if key in entries and entries[key].value]
    require(len(existing_credentials) == len(set(existing_credentials)), "Session, Open WebUI and bootstrap credentials must be independent.")
    for key in credential_keys:
        current = entries[key].value if key in entries else ""
        require("$" not in current and "\\" not in current, f"{key} must be a literal independent credential, not an interpolation.")
        if not current and generate:
            changes[key] = secrets.token_urlsafe(48)
    for key, value in changes.items():
        if key in entries:
            entry = entries[key]
            suffix = lines[entry.line][entry.end:]
            if suffix.startswith("#"):
                suffix = " " + suffix
            lines[entry.line] = lines[entry.line][:entry.start] + value + suffix
        else:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append(f"{key}={value}\n")
    status = {key: "configured" if key in entries and entries[key].value else "not-configured"
              for key in ("FORGEJO_OAUTH_CLIENT_ID", "FORGEJO_OAUTH_CLIENT_SECRET", *credential_keys)}
    return "".join(lines), status


@dataclass(frozen=True)
class Token:
    value: str
    start: int
    end: int


def caddy_tokens(text: str) -> list[Token]:
    require("`" not in text and "<<" not in text, "Unsupported Caddy quoting/heredoc structure; merge the route manually.")
    tokens = []
    for match in re.finditer(r'"(?:\\.|[^"\\])*"|#[^\n]*|[^\s#]+', text):
        value = match[0]
        if value.startswith("#"):
            continue
        require('"' not in value or (value.startswith('"') and value.endswith('"')), "Unrecognized Caddy quoting.")
        tokens.append(Token(value, match.start(), match.end()))
    return tokens


def caddy_candidate(text: str, hostname: str) -> tuple[str, str]:
    """Insert one import; never rewrite or normalize any existing service bytes."""
    tokens = caddy_tokens(text)
    pairs, stack, roots = {}, [], []
    for index, token in enumerate(tokens):
        if token.value == "{":
            if not stack:
                roots.append(index)
            stack.append(index)
        elif token.value == "}":
            require(bool(stack), "Unbalanced Caddy blocks.")
            pairs[stack.pop()] = index
    require(not stack, "Unbalanced Caddy blocks.")
    targets, snippets = [], []
    previous = -1
    for opening in roots:
        header = [token.value for token in tokens[previous + 1:opening]]
        if hostname in header or "https://" + hostname in header:
            require(header in ([hostname], ["https://" + hostname]), "Ambiguous Caddy host header; a single explicit central host is required.")
            targets.append(opening)
        if f"({SNIPPET})" in header:
            require(header == [f"({SNIPPET})"], "Ambiguous dashboard snippet declaration.")
            snippets.append(opening)
        require("import" not in header, "Top-level Caddy imports obscure route ownership; merge manually.")
        previous = pairs[opening]
    require(previous == len(tokens) - 1, "Unknown top-level Caddy structure.")
    require(len(targets) == 1 and len(snippets) <= 1, "Expected exactly one explicit central Caddy host and at most one dashboard snippet.")
    opening = targets[0]
    body_tokens = tokens[opening + 1:pairs[opening]]
    values = [token.value for token in body_tokens]
    route_values = [token.value for token in caddy_tokens(ROUTE)]
    imports = [i for i, value in enumerate(values) if value == "import"]
    if snippets:
        snippet = snippets[0]
        require([token.value for token in tokens[snippet + 1:pairs[snippet]]] == route_values,
                "Existing dashboard snippet differs from the reviewed route.")
        require(len(imports) == 1 and values[imports[0]:imports[0] + 2] == ["import", SNIPPET],
                "Existing dashboard snippet must have exactly one direct host import.")
        start = imports[0]
        remaining = values[:start] + values[start + 2:]
        require(sum(token.value == SNIPPET for token in tokens) == 1, "Dashboard snippet has ambiguous extra imports.")
        status = "already matched (named snippet)"
    else:
        require(not imports, "Host imports can hide conflicting routes; merge them manually before deployment.")
        matches = [i for i in range(len(values)) if values[i:i + len(route_values)] == route_values]
        require(len(matches) <= 1, "Duplicate manager routes in Caddy.")
        if matches:
            start = matches[0]
            remaining = values[:start] + values[start + len(route_values):]
            status = "already matched (existing route)"
        else:
            remaining, status = values, "insert named dashboard snippet"
    depth = 0
    for i, value in enumerate(values):
        if (snippets and i in imports) or (not snippets and status.startswith("already") and i == start):
            require(depth == 0, "Manager integration must be a direct host route, not a nested block.")
        depth += (value == "{") - (value == "}")
    samples = ("/dashboard", "/dashboard/asset.js", "/auth/login", "/v1/runs", "/docs", "/docs/a", "/openapi.json")
    for value in remaining:
        plain = value.strip('"')
        conflict = ("agent-manager" in plain or "agent_manager" in plain or SNIPPET in plain
                    or plain == "path_regexp"
                    or (plain.startswith("/") and ("{" in plain or any(fnmatch.fnmatchcase(sample, plain) for sample in samples)))
                    or any(plain.startswith(prefix) for prefix in ("/dashboard", "/auth/", "/v1/", "/docs", "/openapi.json")))
        require(not conflict, "Ambiguous existing manager/path routes in Caddy; reconcile them manually.")
    if status.startswith("already"):
        return text, status
    # Caddy resolves imports in order: the snippet must precede its host site.
    host_start = tokens[opening - 1].start
    return (text[:host_start] + f"({SNIPPET}) {{\n{ROUTE}\n}}\n\n"
            + text[host_start:tokens[opening].end] + f"\n\timport {SNIPPET}"
            + text[tokens[opening].end:]), status


def plain_file(path: Path) -> None:
    require(path.is_file() and not path.is_symlink(), "A required deployment input is missing, non-regular, or a symlink.")


def safe_tree(path: Path) -> None:
    require(path.is_dir() and not path.is_symlink(), "Required manager source directory is missing or symlinked.")
    for child in path.rglob("*"):
        require(not child.is_symlink() and (child.is_file() or child.is_dir()), "Manager source contains a symlink or special file.")


def validate_inputs(source: Path, live: Path, caddyfile: Path) -> None:
    require(source != live and source not in live.parents and live not in source.parents,
            "Reviewed source and live manager directories must be separate, non-nested trees.")
    for directory in (source, live):
        require(directory.is_dir() and not directory.is_symlink(), "Source/live directories must already exist without symlinks.")
        for name in SOURCE_FILES:
            plain_file(directory / name)
        safe_tree(directory / "agent_manager")
    for name in ("main.py", "dashboard.py", "static/index.html", "static/app.js", "static/styles.css"):
        path = source / "agent_manager" / name
        plain_file(path)
        require(path.stat().st_size > 0, "Reviewed manager/dashboard/static source must not be empty.")
    plain_file(live / ".env")
    plain_file(caddyfile)
    plain_file(source / ".dockerignore")
    ignore_patterns = [line.strip() for line in (source / ".dockerignore").read_text().splitlines() if line.strip() and not line.lstrip().startswith("#")]
    require(".env" in ignore_patterns and not any(line.startswith("!") for line in ignore_patterns), "Reviewed .dockerignore must explicitly exclude .env without re-inclusion rules.")
    for name in (".dockerignore", "compose.worker-test.yml"):
        for directory in (source, live):
            if (directory / name).exists():
                plain_file(directory / name)


def guard_identity(live: Path, caddyfile: Path) -> None:
    require(pwd.getpwuid(os.getuid()).pw_name == EXPECTED_USER and os.getuid() != 0,
            "Apply must run locally as infra-lab-user, not root; sudo -n Docker access is required.")
    for path in (live, live / ".env", caddyfile):
        require(path.stat().st_uid == os.getuid(), "Live directory, .env and Caddyfile must belong to infra-lab-user.")


class Docker:
    """Capture all child output: Compose/build diagnostics can contain secrets."""

    def run(self, args: list[str], action: str, *, stdin=None, stdout=None, check: bool = True,
            timeout: int = 120) -> subprocess.CompletedProcess:
        try:
            result = subprocess.run(
                ["sudo", "-n", "docker", "--host", "unix:///var/run/docker.sock", *args],
                stdin=stdin, stdout=stdout if stdout is not None else subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=timeout, check=False,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(Path.home()), "LC_ALL": "C"},
            )
        except (OSError, subprocess.TimeoutExpired):
            raise DeploymentError(f"{action} could not complete; child output is withheld to protect secrets.") from None
        if check and result.returncode:
            raise DeploymentError(f"{action} failed (exit {result.returncode}); child output is withheld to protect secrets.")
        return result

    def json(self, args: list[str], action: str):
        result = self.run(args, action)
        try:
            return json.loads(result.stdout)
        except (ValueError, TypeError):
            raise DeploymentError(f"{action} returned invalid JSON; output is withheld.") from None


def compose_args(live: Path, project: str, files: list[Path], env_file: Path) -> list[str]:
    args = ["compose", "--project-directory", str(live), "--env-file", str(env_file), "--project-name", project]
    for path in files:
        args.extend(["-f", str(path)])
    return args


def discover(docker: Docker, live: Path) -> tuple[str, list[Path], dict]:
    result = docker.run(["ps", "-a", "--filter", f"label=com.docker.compose.project.working_dir={live}", "--format", "{{.ID}}"], "Discover live manager containers")
    identifiers = result.stdout.decode().split()
    require(bool(identifiers) and all(re.fullmatch(r"[a-f0-9]{12,64}", item) for item in identifiers), "No unambiguous existing live Compose containers found; initialization is forbidden.")
    containers = docker.json(["inspect", *identifiers], "Inspect live manager identity")
    by_service, projects, configurations = {}, set(), set()
    for container in containers:
        labels = container.get("Config", {}).get("Labels", {}) or {}
        service = labels.get("com.docker.compose.service")
        require(service in SERVICES and service not in by_service, "Unknown or duplicate service in the live manager Compose project.")
        require(labels.get("com.docker.compose.project.working_dir") == str(live), "Live Compose working directory differs from the requested destination.")
        require(labels.get("com.docker.compose.oneoff", "False").lower() == "false", "One-off Compose containers make live identity ambiguous.")
        by_service[service] = container
        projects.add(labels.get("com.docker.compose.project"))
        configurations.add(labels.get("com.docker.compose.project.config_files"))
    require(len(projects) == len(configurations) == 1 and {"postgres", "nats", "agent-manager"} <= by_service.keys(), "Live manager, postgres and nats must share one existing project/configuration.")
    project, configuration = projects.pop(), configurations.pop()
    require(isinstance(project, str) and bool(re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project)), "Unknown live Compose project name.")
    require(isinstance(configuration, str), "Live Compose config-file identity is unavailable.")
    files = [Path(name) for name in configuration.split(",")]
    require(files in ([live / "compose.yml"], [live / "compose.yml", live / "compose.worker-test.yml"]), "Unknown live Compose override set; reconcile it manually rather than guessing.")
    for path in files:
        plain_file(path)
    for service in ("postgres", "nats", "agent-manager"):
        state = by_service[service].get("State", {})
        require(state.get("Running") is True and state.get("Health", {}).get("Status") in (None, "healthy"), "Existing manager, postgres and nats must be running and healthy before apply.")
    return project, files, by_service


def port_signature(service: dict) -> list[tuple]:
    result = []
    for port in service.get("ports", []):
        require(isinstance(port, dict), "Unsupported effective Compose port format.")
        require(port.get("mode", "ingress") == "ingress", "Unsupported Compose port publishing mode.")
        result.append((str(port.get("target")), str(port.get("published")), port.get("host_ip", "0.0.0.0"), port.get("protocol", "tcp")))
    return sorted(result)


def guard_effective(live: dict, candidate: dict, project: str, containers: dict) -> None:
    require(live.get("name") == candidate.get("name") == project, "Effective Compose project identity would change.")
    for config in (live, candidate):
        require(set(config.get("volumes", {})) == set(VOLUMES), "Unknown Compose named volumes; refusing to initialize or remap data.")
        for key, expected in VOLUMES.items():
            require(config["volumes"][key].get("name") == expected, "Compose data volume identity differs from the known central volumes.")
        require({"postgres", "nats", "agent-manager"} <= config.get("services", {}).keys() <= SERVICES, "Unknown effective Compose service configuration.")
    require(live["volumes"] == candidate["volumes"], "Effective volume options would change; manual reconciliation is required.")
    for service in live["services"].keys() | candidate["services"].keys():
        require(port_signature(live["services"].get(service, {})) == port_signature(candidate["services"].get(service, {})), "Candidate Compose changes published ports; retain the approved live override first.")
    for service, key, target in (("postgres", "manager_postgres_data", "/var/lib/postgresql/data"), ("nats", "nats_data", "/data")):
        expected = [{"type": "volume", "source": key, "target": target}]
        for config in (live, candidate):
            mounts = config["services"][service].get("volumes", [])
            require(len(mounts) == 1 and all(mounts[0].get(field) == value for field, value in expected[0].items()) and not mounts[0].get("read_only"), "Database/NATS mount configuration is unknown or changed.")
        mounts = containers[service].get("Mounts", [])
        require(len(mounts) == 1 and mounts[0].get("Type") == "volume" and mounts[0].get("Name") == VOLUMES[key] and mounts[0].get("Destination") == target and mounts[0].get("RW") is True, "Running database/NATS mounts do not match the known data volumes.")
    require(not candidate["services"]["agent-manager"].get("volumes"), "Candidate manager mounts are outside this deployment's scope.")
    database_env = live["services"]["postgres"].get("environment", {})
    require(all(database_env.get(key) for key in ("POSTGRES_USER", "POSTGRES_DB", "POSTGRES_PASSWORD")), "Effective PostgreSQL credentials/database must be explicitly configured.")
    running_env = dict(item.split("=", 1) for item in containers["postgres"].get("Config", {}).get("Env", []) if "=" in item)
    require(all(running_env.get(key) == database_env[key] for key in ("POSTGRES_USER", "POSTGRES_DB", "POSTGRES_PASSWORD")), "Running PostgreSQL configuration differs from live .env; reconcile drift privately first.")
    require(candidate["services"]["postgres"].get("environment", {}) == database_env, "Candidate PostgreSQL settings differ from the existing database.")
    manager_env = candidate["services"]["agent-manager"].get("environment", {})
    require(all(manager_env.get(key) == value for key, value in {"DB_HOST": "postgres", "DB_NAME": database_env["POSTGRES_DB"], "DB_USER": database_env["POSTGRES_USER"], "DB_PASSWORD": database_env["POSTGRES_PASSWORD"], "NATS_URL": "nats://nats:4222"}.items()), "Candidate manager would use different database/NATS settings.")
    for service, container in containers.items():
        published = []
        for target, bindings in (container.get("NetworkSettings", {}).get("Ports") or {}).items():
            number, protocol = target.split("/")
            for binding in bindings or []:
                published.append((number, binding["HostPort"], binding["HostIp"], protocol))
        require(sorted(published) == port_signature(live["services"].get(service, {})), "Running published ports differ from live effective Compose; reconcile drift first.")


def private_write(path: Path, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def replace_env(path: Path, content: str) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".dashboard-env-", dir=path.parent)
    os.close(fd)
    try:
        private_write(Path(temporary), content)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def inplace_bytes(path: Path, content: bytes) -> None:
    # Preserve the inode: the running Caddy container bind-mounts this exact file.
    with path.open("r+b") as stream:
        stream.write(content)
        stream.truncate()
        stream.flush()
        os.fsync(stream.fileno())


def integrate_caddy(docker: Docker, caddyfile: Path, original: str, candidate: str) -> None:
    require(caddyfile.read_bytes() == original.encode(), "Caddyfile changed during rollout; refusing to overwrite another edit.")
    changed = candidate != original
    try:
        if changed:
            inplace_bytes(caddyfile, candidate.encode())
        docker.run(["exec", "caddy", "caddy", "validate", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"], "Validate dashboard Caddy routes")
        # Matching on-disk bytes may not yet be the active configuration.
        docker.run(["exec", "caddy", "caddy", "reload", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"], "Activate dashboard Caddy routes")
    except (DeploymentError, OSError, KeyboardInterrupt):
        if changed:
            inplace_bytes(caddyfile, original.encode())
            print("Restored original Caddyfile bytes after integration failure.")
            docker.run(["exec", "caddy", "caddy", "reload", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"], "Reload restored Caddy routes")
        raise


def check_runtime(docker: Docker, candidate: dict, containers: dict, caddyfile: Path) -> None:
    for name in VOLUMES.values():
        volume = docker.json(["volume", "inspect", name], "Inspect existing data volume")
        require(len(volume) == 1 and volume[0].get("Name") == name and volume[0].get("Driver") == "local" and not volume[0].get("Options"), "Existing central data volume is missing or has unknown storage options.")
    networks = candidate.get("networks", {})
    manager_networks = candidate["services"]["agent-manager"].get("networks", {})
    require({"manager_internal", "caddy_proxy", "open_webui_internal"} == set(manager_networks), "Unknown candidate manager network topology.")
    expected_networks = {"manager_internal": "agent_manager_internal", "caddy_proxy": "caddy_proxy", "open_webui_internal": "open-webui_internal"}
    for key, name in expected_networks.items():
        require(networks.get(key, {}).get("name") == name, "Candidate network identity is unknown.")
        inspected = docker.json(["network", "inspect", name], "Inspect existing manager network")
        require(len(inspected) == 1 and inspected[0].get("Name") == name, "A required existing network is unavailable; this helper never initializes topology.")
    for service in ("postgres", "nats"):
        require("agent_manager_internal" in containers[service].get("NetworkSettings", {}).get("Networks", {}), "Running dependencies are not on the expected internal network.")
    caddy = docker.json(["inspect", "caddy"], "Inspect Caddy integration")
    require(len(caddy) == 1 and caddy[0].get("State", {}).get("Running") is True, "The existing caddy container must be running.")
    mounts = [mount for mount in caddy[0].get("Mounts", []) if mount.get("Destination") == "/etc/caddy/Caddyfile"]
    require(len(mounts) == 1 and mounts[0].get("Type") == "bind" and mounts[0].get("Source") == str(caddyfile), "Caddy does not bind-mount the requested live Caddyfile.")
    require("caddy_proxy" in caddy[0].get("NetworkSettings", {}).get("Networks", {}), "Caddy is not on the reviewed proxy network.")
    environment = candidate["services"]["agent-manager"].get("environment", {})
    for key in ("DASHBOARD_SESSION_SECRET", "AGENT_MANAGER_OPENWEBUI_TOKEN", "AGENT_MANAGER_API_TOKEN"):
        require(bool(environment.get(key)), f"Candidate manager is missing {key}; reconcile the reviewed Compose file first.")
    require(len({environment[key] for key in ("DASHBOARD_SESSION_SECRET", "AGENT_MANAGER_OPENWEBUI_TOKEN", "AGENT_MANAGER_API_TOKEN")}) == 3, "Effective manager credentials must be independent.")
    expected_env = {"DASHBOARD_SESSION_SECURE": "true", "PROMETHEUS_URL": "http://prometheus:9090", "GRAFANA_NODE_DASHBOARD_UID": "server-overview"}
    require(all(str(environment.get(key)).lower() == value for key, value in expected_env.items()), "Candidate Compose does not pass through the required central dashboard settings.")
    public_origin(environment.get("DASHBOARD_PUBLIC_URL", ""))


def backup_live(docker: Docker, live: Path, caddyfile: Path, root: Path, files: list[Path], project: str, manager: dict) -> tuple[Path, str]:
    require(not root.is_symlink(), "Backup root must not be a symlink.")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(root.stat().st_uid == os.getuid() and stat.S_IMODE(root.stat().st_mode) == 0o700, "Backup root must be owned by the deploying user and mode 0700.")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(4)
    backup = root / stamp
    backup.mkdir(mode=0o700)
    saved = backup / "agent-manager-service"
    saved.mkdir(mode=0o700)
    for name in {*SOURCE_FILES, ".env", ".dockerignore", *(path.name for path in files)}:
        if (live / name).exists():
            shutil.copy2(live / name, saved / name)
    os.chmod(saved / ".env", 0o600)
    shutil.copytree(live / "agent_manager", saved / "agent_manager")
    shutil.copy2(caddyfile, backup / "Caddyfile")
    image = manager.get("Image", "")
    require(bool(re.fullmatch(r"sha256:[a-f0-9]{64}", image)), "Running manager image identity is unavailable.")
    tag = "local/agent-manager:before-dashboard-" + stamp
    # Print the recovery location before any backup command can fail.
    print(f"Private backup: {backup}")
    print(f"Previous manager image tag: {tag}")
    docker.run(["image", "tag", image, tag], "Tag previous manager image")
    with (backup / "manager-image.tar").open("xb") as stream:
        docker.run(["image", "save", tag], "Save previous manager image", stdout=stream, timeout=600)
    private_write(backup / "recovery.json", json.dumps({"project": project, "compose_files": [str(path) for path in files], "live_directory": str(live), "caddyfile": str(caddyfile), "previous_image": image, "previous_image_tag": tag}, indent=2) + "\n")
    return backup, tag


READINESS_CODE = """import json, urllib.request
with urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=3) as r:
    assert r.status == 200 and json.load(r)['status'] == 'ok'
with urllib.request.urlopen('http://127.0.0.1:8000/dashboard', timeout=3) as r:
    assert r.status == 200
"""


def wait_ready(docker: Docker, compose: list[str]) -> None:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        result = docker.run([*compose, "exec", "-T", "agent-manager", "python", "-c", READINESS_CODE], "Check manager readiness/dashboard", check=False, timeout=15)
        if result.returncode == 0:
            return
        time.sleep(2)
    raise DeploymentError("Manager readiness/dashboard did not pass within 120 seconds; Caddy has not been changed.")


def install_source(source: Path, live: Path) -> None:
    # Replace the reviewed package, not an overlay that leaves obsolete Python behind.
    shutil.rmtree(live / "agent_manager")
    shutil.copytree(source / "agent_manager", live / "agent_manager", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in SOURCE_FILES:
        shutil.copy2(source / name, live / name)
    if (source / ".dockerignore").exists():
        shutil.copy2(source / ".dockerignore", live / ".dockerignore")


def apply(args, source: Path, live: Path, caddyfile: Path, env_text: str, caddy_text: str, candidate_caddy: str) -> None:
    require(args.submissions_paused, "Apply requires --submissions-paused after pausing submitters and draining/pausing workers; manager downtime is expected.")
    guard_identity(live, caddyfile)
    docker = Docker()
    project, files, containers = discover(docker, live)
    compose = compose_args(live, project, files, live / ".env")
    updated_env, _ = prepare_env(env_text, args.public_url, generate=True)
    with tempfile.TemporaryDirectory(prefix="central-dashboard-") as staging:
        staged_env = Path(staging) / ".env"
        private_write(staged_env, updated_env)
        candidate_files = [source / "compose.yml", *files[1:]]
        candidate_compose = compose_args(live, project, candidate_files, staged_env)
        effective_live = docker.json([*compose, "config", "--format", "json"], "Resolve live Compose")
        effective_candidate = docker.json([*candidate_compose, "config", "--format", "json"], "Resolve candidate Compose")
        guard_effective(effective_live, effective_candidate, project, containers)
        build = effective_candidate["services"]["agent-manager"].get("build", {})
        require(isinstance(build, dict) and build.get("context") == str(live) and build.get("dockerfile", "Dockerfile") == "Dockerfile" and not build.get("additional_contexts"), "Candidate manager build must use only the reviewed live directory/Dockerfile.")
        check_runtime(docker, effective_candidate, containers, caddyfile)
        require(public_origin(effective_candidate["services"]["agent-manager"]["environment"]["DASHBOARD_PUBLIC_URL"]) == args.public_url, "Candidate dashboard public origin does not match the Caddy host.")
        require((live / ".env").read_bytes() == env_text.encode() and caddyfile.read_bytes() == caddy_text.encode(), "Live .env/Caddyfile changed during preflight; rerun the plan.")
        backup, _ = backup_live(docker, live, caddyfile, args.backup_root, files, project, containers["agent-manager"])
        docker.run([*compose, "stop", "agent-manager"], "Pause only the manager", timeout=120)
        database_env = effective_live["services"]["postgres"].get("environment", {})
        db_user, db_name = database_env.get("POSTGRES_USER"), database_env.get("POSTGRES_DB")
        with (backup / "agent-manager.dump").open("xb") as stream:
            docker.run([*compose, "exec", "-T", "postgres", "pg_dump", "-U", db_user, "-d", db_name, "-Fc"], "Back up the existing manager database", stdout=stream, timeout=600)
        require((backup / "agent-manager.dump").stat().st_size > 0, "Database backup is empty; no source was installed.")
        with (backup / "agent-manager.dump").open("rb") as stream:
            catalog = docker.run([*compose, "exec", "-T", "postgres", "pg_restore", "--list"], "Validate database archive catalog", stdin=stream)
        require(any(line and not line.startswith(b";") for line in catalog.stdout.splitlines()), "Database archive catalog has no objects; no source was installed.")
        install_source(source, live)
        replace_env(live / ".env", updated_env)
        # Compose build has no --no-deps flag: without --with-dependencies it builds only this service.
        docker.run([*compose, "build", "agent-manager"], "Build only the reviewed manager", timeout=1800)
        docker.run([*compose, "up", "-d", "--no-deps", "--no-build", "agent-manager"], "Start only the reviewed manager", timeout=180)
        wait_ready(docker, compose)
        docker.run(["exec", "caddy", "wget", "-q", "-O", "/dev/null", "http://agent-manager:8000/dashboard"], "Check dashboard from Caddy network")
        print("Checked before Caddy reload: /readyz ok; /dashboard HTTP 200; Caddy upstream reachable.")
        integrate_caddy(docker, caddyfile, caddy_text, candidate_caddy)
        wait_ready(docker, compose)
        docker.run(["exec", "caddy", "wget", "-q", "-O", "/dev/null", "http://agent-manager:8000/dashboard"], "Recheck dashboard upstream after reload")
        print("After checks: manager ready, dashboard HTTP 200, Caddy reload successful, upstream reachable.")
        print(f"Browser check (trusted central TLS CA required): {args.public_url}/dashboard")
        print(f"Recovery inventory: {backup / 'recovery.json'}; no database is ever restored automatically.")
        print("Keep submissions paused until the browser/login check passes; workflow installation is a separate administrator action.")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    mode = result.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="Read-only filesystem plan (the default); no Docker/sudo/SSH or credentials needed")
    mode.add_argument("--apply", action="store_true", help="Apply locally on the VM as infra-lab-user")
    result.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1] / "Docker-Documents/agent-manager-service")
    result.add_argument("--live-dir", type=Path, default=Path.home() / "agent-manager-service")
    result.add_argument("--caddyfile", type=Path, default=Path.home() / "caddy-service/Caddyfile")
    result.add_argument("--backup-root", type=Path, default=Path.home() / "central-dashboard-backups")
    result.add_argument("--public-url", default=PUBLIC_URL)
    result.add_argument("--submissions-paused", action="store_true", help="Acknowledge submitters/workers paused or drained and manager downtime approved")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    old_umask = os.umask(0o077) if args.apply else None
    try:
        # Reject direct symlinks before canonicalizing paths for Docker identity checks.
        for path in (args.source, args.live_dir, args.caddyfile, args.backup_root):
            require(not path.is_symlink(), "Deployment paths must not be symlinks.")
        source, live, caddyfile = args.source.resolve(), args.live_dir.resolve(), args.caddyfile.resolve()
        args.backup_root = args.backup_root.resolve()
        require(args.backup_root != live and live not in args.backup_root.parents and args.backup_root != source and source not in args.backup_root.parents, "Backups must be outside both source and live service trees.")
        args.public_url = public_origin(args.public_url)
        validate_inputs(source, live, caddyfile)
        env_text, caddy_text = (live / ".env").read_bytes().decode("utf-8"), caddyfile.read_bytes().decode("utf-8")
        _, status = prepare_env(env_text, args.public_url, generate=False)
        candidate_caddy, route_status = caddy_candidate(caddy_text, urlsplit(args.public_url).hostname)
        print(f"Mode: {'APPLY' if args.apply else 'PLAN (no mutation; runtime checks deferred)'}")
        print(f"Reviewed manager source: {source}")
        print(f"Live manager destination: {live}")
        print(f"Live Caddyfile: {caddyfile}; route: {route_status}")
        print(f"Public dashboard: {args.public_url}/dashboard")
        for key, state in status.items():
            print(f"{key}: {state}")
        print("Required: VM-local infra-lab-user, sudo -n Docker, paused submitters/drained workers, running existing manager/postgres/nats/Caddy.")
        print("Apply: compare live/candidate project, known data volumes and ports; require existing networks; fail on unknown topology.")
        print("Back up source/.env/Caddyfile, current image and catalog-validated pg_dump in a private directory before installing source.")
        print("Preserve .env entries; fill empty/missing independent session/Open WebUI credentials privately; keep OAuth operator-configured.")
        print("Central settings: SESSION_SECURE=true; PROMETHEUS_URL=http://prometheus:9090; GRAFANA_NODE_DASHBOARD_UID=server-overview.")
        print("Build only agent-manager; up --no-deps only agent-manager; verify readiness/dashboard before Caddy validation/reload.")
        print("No SSH, new databases, volume removal, NATS restart/port remap, Grafana provisioning or workflow installation.")
        if args.apply:
            apply(args, source, live, caddyfile, env_text, caddy_text, candidate_caddy)
        return 0
    except DeploymentError as error:
        print(f"STOP: {error}", file=sys.stderr)
        if args.apply:
            print("Keep submissions paused. If a backup was reported, use its recovery inventory and the deployment runbook; no database restore was attempted.", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError):
        print("STOP: local input/runtime data could not be handled safely; details withheld to avoid exposing secrets. Keep submissions paused if apply began.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("STOP: interrupted. Keep submissions paused if apply began; consult the reported private backup before recovery.", file=sys.stderr)
        return 130
    finally:
        if old_umask is not None:
            os.umask(old_umask)


if __name__ == "__main__":
    raise SystemExit(main())
