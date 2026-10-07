from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


SCRIPT = Path(__file__).resolve().parents[1] / "deploy_central_dashboard.py"
SPEC = importlib.util.spec_from_file_location("deploy_central_dashboard", SCRIPT)
deploy = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = deploy
SPEC.loader.exec_module(deploy)
HOST = "infra-lab-services.tail494f6d.ts.net"
CADDY = f"""{{
    admin localhost:2019
}}
{HOST} {{
    tls internal
    @grafana {{
        path /grafana /grafana/*
    }}
    handle @grafana {{
        reverse_proxy grafana:3000
    }}
    handle {{
        reverse_proxy https://10.1.10.156:8006 {{
            transport http {{
                tls_insecure_skip_verify
            }}
        }}
    }}
}}
https://{HOST}:8443 {{
    tls internal
    reverse_proxy open-webui:8080
}}
other.example.com {{
    reverse_proxy extra-service:1234
}}
"""


def fixture(root: Path, env: str = "") -> tuple[Path, Path, Path]:
    source, live = root / "checkout", root / "live"
    for directory in (source, live):
        (directory / "agent_manager/static").mkdir(parents=True)
        for name in deploy.SOURCE_FILES:
            (directory / name).write_text("reviewed source\n")
        (directory / ".dockerignore").write_text(".env\n")
        for name in ("main.py", "dashboard.py", "static/index.html", "static/app.js", "static/styles.css"):
            (directory / "agent_manager" / name).write_text("source\n")
    (live / ".env").write_text(env)
    caddyfile = root / "Caddyfile"
    caddyfile.write_text(CADDY)
    return source, live, caddyfile


def effective_fixture():
    db_env = {"POSTGRES_USER": "manager", "POSTGRES_DB": "manager", "POSTGRES_PASSWORD": "db-private"}
    config = {
        "name": "agent-manager",
        "volumes": {key: {"name": value} for key, value in deploy.VOLUMES.items()},
        "services": {
            "postgres": {"environment": db_env, "volumes": [{"type": "volume", "source": "manager_postgres_data", "target": "/var/lib/postgresql/data"}]},
            "nats": {"volumes": [{"type": "volume", "source": "nats_data", "target": "/data"}], "ports": [{"target": 4222, "published": "4222", "host_ip": "100.94.49.45", "protocol": "tcp"}]},
            "agent-manager": {"environment": {"DB_HOST": "postgres", "DB_NAME": "manager", "DB_USER": "manager", "DB_PASSWORD": "db-private", "NATS_URL": "nats://nats:4222"}, "ports": [{"target": 8000, "published": "8090", "host_ip": "127.0.0.1", "protocol": "tcp"}]},
        },
    }
    containers = {
        "postgres": {"Config": {"Env": [f"{key}={value}" for key, value in db_env.items()]}, "Mounts": [{"Type": "volume", "Name": deploy.VOLUMES["manager_postgres_data"], "Destination": "/var/lib/postgresql/data", "RW": True}], "NetworkSettings": {"Ports": {"5432/tcp": None}}},
        "nats": {"Mounts": [{"Type": "volume", "Name": deploy.VOLUMES["nats_data"], "Destination": "/data", "RW": True}], "NetworkSettings": {"Ports": {"4222/tcp": [{"HostIp": "100.94.49.45", "HostPort": "4222"}], "8222/tcp": None}}},
        "agent-manager": {"NetworkSettings": {"Ports": {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8090"}]}}},
    }
    return config, copy.deepcopy(config), containers


class EnvironmentTests(unittest.TestCase):
    def test_preserves_entries_comments_and_existing_credentials(self):
        original = "# live settings\r\nPOSTGRES_PASSWORD='db#private' # preserve\r\nCUSTOM=value=with=equals\r\nFORGEJO_OAUTH_CLIENT_SECRET=oauth-private\r\nDASHBOARD_SESSION_SECRET=session-private\r\nAGENT_MANAGER_OPENWEBUI_TOKEN=pipe-private\r\n"
        updated, status = deploy.prepare_env(original, deploy.PUBLIC_URL, generate=True)
        self.assertTrue(updated.startswith(original))
        self.assertEqual(status["FORGEJO_OAUTH_CLIENT_SECRET"], "configured")
        self.assertIn("PROMETHEUS_URL=http://prometheus:9090\n", updated)
        self.assertIn("GRAFANA_NODE_DASHBOARD_UID=server-overview\n", updated)
        self.assertEqual(deploy.prepare_env(updated, deploy.PUBLIC_URL, generate=True)[0], updated)

    def test_empty_credentials_get_independent_values_and_retain_comments(self):
        original = "DASHBOARD_SESSION_SECRET= # session comment\nAGENT_MANAGER_OPENWEBUI_TOKEN='' # pipe comment\nAGENT_MANAGER_API_TOKEN=bootstrap-private"
        with patch.object(deploy.secrets, "token_urlsafe", side_effect=["new-session", "new-pipe"]):
            updated, _ = deploy.prepare_env(original, deploy.PUBLIC_URL, generate=True)
        self.assertIn("DASHBOARD_SESSION_SECRET= new-session # session comment\n", updated)
        self.assertIn("AGENT_MANAGER_OPENWEBUI_TOKEN=new-pipe # pipe comment\n", updated)
        values = deploy.parse_env(updated)
        self.assertEqual(values["DASHBOARD_SESSION_SECRET"].value, "new-session")
        self.assertEqual(values["AGENT_MANAGER_OPENWEBUI_TOKEN"].value, "new-pipe")
        self.assertEqual(values["AGENT_MANAGER_API_TOKEN"].value, "bootstrap-private")
        with patch.object(deploy.secrets, "token_urlsafe", side_effect=AssertionError("must not regenerate")):
            self.assertEqual(deploy.prepare_env(updated, deploy.PUBLIC_URL, generate=True)[0], updated)

    def test_plan_does_not_generate_credentials(self):
        with patch.object(deploy.secrets, "token_urlsafe", side_effect=AssertionError("plan must not generate")):
            updated, status = deploy.prepare_env("", deploy.PUBLIC_URL, generate=False)
        self.assertNotIn("DASHBOARD_SESSION_SECRET=", updated)
        self.assertEqual(status["AGENT_MANAGER_OPENWEBUI_TOKEN"], "not-configured")

    def test_unsafe_or_ambiguous_values_fail_without_secret_output(self):
        for text in ("DASHBOARD_SESSION_SECURE=false\n", "DASHBOARD_PUBLIC_URL=http://private-secret.example.com\n", "PROMETHEUS_URL=http://private-secret:9090\n", "DASHBOARD_SESSION_SECRET=same-private\nAGENT_MANAGER_OPENWEBUI_TOKEN=same-private\n", "DASHBOARD_SESSION_SECRET=${PRIVATE_SECRET}\n", "TOKEN=first-private\nTOKEN=second-private\n"):
            with self.subTest(text=text), self.assertRaises(deploy.DeploymentError) as error:
                deploy.prepare_env(text, deploy.PUBLIC_URL, generate=True)
            self.assertNotIn("private", str(error.exception).replace("privately", ""))

    def test_env_write_is_private_and_atomic(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / ".env"
            path.write_text("OLD=value\n")
            path.chmod(0o644)
            deploy.replace_env(path, "OLD=value\nNEW=secret\n")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.read_text(), "OLD=value\nNEW=secret\n")
            self.assertEqual(list(Path(root).iterdir()), [path])


class CaddyTests(unittest.TestCase):
    def test_insertion_preserves_arbitrary_other_routes_and_is_idempotent(self):
        original = CADDY.replace("\n", "\r\n")
        updated, status = deploy.caddy_candidate(original, HOST)
        self.assertEqual(status, "insert named dashboard snippet")
        without_insert = updated.replace(f"\n\timport {deploy.SNIPPET}", "", 1)
        self.assertEqual(without_insert.replace(f"({deploy.SNIPPET}) {{\n{deploy.ROUTE}\n}}\n\n", "", 1), original)
        self.assertIn(f"https://{HOST}:8443 {{\r\n", updated)
        self.assertIn("reverse_proxy extra-service:1234", updated)
        self.assertEqual(deploy.caddy_candidate(updated, HOST)[0], updated)

    def test_matching_existing_route_is_not_rewritten(self):
        existing = CADDY.replace(f"{HOST} {{", f"{HOST} {{\n{deploy.ROUTE}", 1)
        self.assertEqual(deploy.caddy_candidate(existing, HOST), (existing, "already matched (existing route)"))

    def test_unknown_structure_or_conflicting_route_is_rejected(self):
        examples = [
            "import sites/*\n", CADDY.replace(f"{HOST} {{", "{$HOST} {", 1),
            CADDY + f"{HOST} {{ respond ok }}\n",
            CADDY.replace(f"{HOST} {{", f"{HOST}, second.example.com {{", 1),
            CADDY.replace("tls internal", "import unknown-routes", 1),
            CADDY.replace("tls internal", "handle /dashboard* { reverse_proxy unknown:8000 }", 1),
            CADDY.replace("tls internal", "@maybe path_regexp hidden ^/dash", 1),
            CADDY.replace("tls internal", f"handle /nested/* {{ {deploy.ROUTE} }}", 1),
            CADDY.replace("tls internal", "handle /* { reverse_proxy wildcard:8000 }", 1),
            CADDY + "}\n",
        ]
        for text in examples:
            with self.subTest(text=text), self.assertRaises(deploy.DeploymentError):
                deploy.caddy_candidate(text, HOST)

    def test_validation_failure_restores_original_bytes_and_inode(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "Caddyfile"
            original = CADDY.replace("\n", "\r\n")
            path.write_bytes(original.encode())
            inode = path.stat().st_ino
            candidate, _ = deploy.caddy_candidate(original, HOST)
            docker = Mock()
            docker.run.side_effect = [deploy.DeploymentError("validation rejected"), SimpleNamespace(returncode=0)]
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(deploy.DeploymentError):
                deploy.integrate_caddy(docker, path, original, candidate)
            self.assertEqual(path.read_bytes(), original.encode())
            self.assertEqual(path.stat().st_ino, inode)
            calls = [call.args[0] for call in docker.run.call_args_list]
            self.assertEqual(calls[0][3], "validate")
            self.assertEqual(calls[1][3], "reload")

    def test_reload_failure_restores_original_bytes(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "Caddyfile"
            path.write_text(CADDY)
            candidate, _ = deploy.caddy_candidate(CADDY, HOST)
            docker = Mock()
            docker.run.side_effect = [SimpleNamespace(returncode=0), deploy.DeploymentError("reload rejected"), SimpleNamespace(returncode=0)]
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(deploy.DeploymentError):
                deploy.integrate_caddy(docker, path, CADDY, candidate)
            self.assertEqual(path.read_text(), CADDY)
            self.assertEqual(docker.run.call_count, 3)

    def test_concurrent_edit_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "Caddyfile"
            path.write_text("new operator edit\n")
            docker = Mock()
            with self.assertRaises(deploy.DeploymentError):
                deploy.integrate_caddy(docker, path, CADDY, "replacement")
            self.assertEqual(path.read_text(), "new operator edit\n")
            docker.run.assert_not_called()

    def test_matching_file_still_validated_and_activated(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "Caddyfile"
            path.write_text(CADDY)
            docker = Mock()
            deploy.integrate_caddy(docker, path, CADDY, CADDY)
            self.assertEqual([call.args[0][3] for call in docker.run.call_args_list], ["validate", "reload"])


class IdentityAndVolumeTests(unittest.TestCase):
    def test_known_effective_configuration_passes(self):
        live, candidate, containers = effective_fixture()
        deploy.guard_effective(live, candidate, "agent-manager", containers)

    def test_project_volume_mount_and_port_drift_fail(self):
        changes = [
            lambda live, candidate, containers: candidate.update(name="new-project"),
            lambda live, candidate, containers: candidate["volumes"]["manager_postgres_data"].update(name="fresh-db"),
            lambda live, candidate, containers: candidate["volumes"].update(extra={"name": "unknown"}),
            lambda live, candidate, containers: containers["postgres"]["Mounts"][0].update(Name="fresh-db"),
            lambda live, candidate, containers: candidate["services"]["nats"].update(ports=[]),
            lambda live, candidate, containers: containers["nats"]["NetworkSettings"]["Ports"]["4222/tcp"][0].update(HostIp="0.0.0.0"),
            lambda live, candidate, containers: candidate["services"]["postgres"]["volumes"][0].update(type="bind"),
            lambda live, candidate, containers: candidate["services"]["agent-manager"]["environment"].update(DB_NAME="new-db"),
            lambda live, candidate, containers: containers["postgres"]["Config"].update(Env=[]),
        ]
        for change in changes:
            live, candidate, containers = effective_fixture()
            change(live, candidate, containers)
            with self.subTest(change=change), self.assertRaises(deploy.DeploymentError):
                deploy.guard_effective(live, candidate, "agent-manager", containers)

    def test_wrong_operator_fails_before_files_or_docker(self):
        with patch.object(deploy.pwd, "getpwuid", return_value=SimpleNamespace(pw_name="developer")):
            with self.assertRaisesRegex(deploy.DeploymentError, "infra-lab-user"):
                deploy.guard_identity(Path("/does/not/exist"), Path("/also/missing"))

    def test_root_is_not_accepted_even_if_account_lookup_is_wrong(self):
        with patch.object(deploy.pwd, "getpwuid", return_value=SimpleNamespace(pw_name="infra-lab-user")), patch.object(deploy.os, "getuid", return_value=0):
            with self.assertRaises(deploy.DeploymentError):
                deploy.guard_identity(Path("/does/not/exist"), Path("/also/missing"))

    def test_missing_live_containers_cannot_initialize_database(self):
        docker = Mock()
        docker.run.return_value = SimpleNamespace(stdout=b"")
        with self.assertRaisesRegex(deploy.DeploymentError, "initialization is forbidden"):
            deploy.discover(docker, Path("/home/infra-lab-user/agent-manager-service"))
        docker.json.assert_not_called()

    def test_compose_arguments_preserve_identity_and_overrides(self):
        live = Path("/home/infra-lab-user/agent-manager-service")
        files = [live / "compose.yml", live / "compose.worker-test.yml"]
        args = deploy.compose_args(live, "existing-project", files, live / ".env")
        self.assertIn("existing-project", args)
        self.assertEqual(args[-4:], ["-f", str(files[0]), "-f", str(files[1])])


class PlanAndOutputTests(unittest.TestCase):
    def test_real_plan_needs_no_credentials_or_commands_and_does_not_mutate(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            source, live, caddyfile = fixture(root)
            before = {path: (path.read_bytes(), path.stat().st_mtime_ns, stat.S_IMODE(path.stat().st_mode)) for path in root.rglob("*") if path.is_file()}
            output = io.StringIO()
            with patch.object(deploy.subprocess, "run", side_effect=AssertionError("plan cannot run commands")), contextlib.redirect_stdout(output):
                code = deploy.main(["--source", str(source), "--live-dir", str(live), "--caddyfile", str(caddyfile), "--backup-root", str(root / "backups")])
            self.assertEqual(code, 0)
            self.assertIn("PLAN (no mutation", output.getvalue())
            self.assertIn("FORGEJO_OAUTH_CLIENT_SECRET: not-configured", output.getvalue())
            after = {path: (path.read_bytes(), path.stat().st_mtime_ns, stat.S_IMODE(path.stat().st_mode)) for path in root.rglob("*") if path.is_file()}
            self.assertEqual(before, after)
            self.assertFalse((root / "backups").exists())

    def test_configured_secrets_never_appear_in_plan_output(self):
        values = {"POSTGRES_PASSWORD": "database-private", "AGENT_MANAGER_API_TOKEN": "bootstrap-private", "FORGEJO_OAUTH_CLIENT_ID": "oauth-client-private", "FORGEJO_OAUTH_CLIENT_SECRET": "oauth-secret-private", "DASHBOARD_SESSION_SECRET": "session-private", "AGENT_MANAGER_OPENWEBUI_TOKEN": "pipe-private"}
        with tempfile.TemporaryDirectory() as root:
            source, live, caddyfile = fixture(Path(root), "".join(f"{key}={value}\n" for key, value in values.items()))
            output = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                code = deploy.main(["--plan", "--source", str(source), "--live-dir", str(live), "--caddyfile", str(caddyfile)])
            self.assertEqual(code, 0)
            for value in values.values():
                self.assertNotIn(value, output.getvalue())
            self.assertIn("FORGEJO_OAUTH_CLIENT_ID: configured", output.getvalue())

    def test_apply_requires_pause_acknowledgement_before_runtime_mutation(self):
        with tempfile.TemporaryDirectory() as root:
            source, live, caddyfile = fixture(Path(root))
            output = io.StringIO()
            with patch.object(deploy.Docker, "run", side_effect=AssertionError("must not call Docker")), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                code = deploy.main(["--apply", "--source", str(source), "--live-dir", str(live), "--caddyfile", str(caddyfile)])
            self.assertEqual(code, 1)
            self.assertIn("--submissions-paused", output.getvalue())
            self.assertEqual((live / ".env").read_text(), "")

    def test_child_output_and_remote_docker_environment_never_leak(self):
        secret = b"sensitive-provider-token"
        with patch.dict(os.environ, {"DOCKER_HOST": "ssh://remote", "DOCKER_CONTEXT": "production", "POSTGRES_PASSWORD": "ambient-secret"}), patch.object(deploy.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, secret, secret)) as run:
            with self.assertRaises(deploy.DeploymentError) as error:
                deploy.Docker().run(["compose", "config"], "Resolve candidate")
        self.assertNotIn(secret.decode(), str(error.exception))
        argv = run.call_args.args[0]
        self.assertEqual(argv[:5], ["sudo", "-n", "docker", "--host", "unix:///var/run/docker.sock"])
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertNotIn("DOCKER_HOST", run.call_args.kwargs["env"])
        self.assertNotIn("DOCKER_CONTEXT", run.call_args.kwargs["env"])
        self.assertNotIn("POSTGRES_PASSWORD", run.call_args.kwargs["env"])


if __name__ == "__main__":
    unittest.main()
