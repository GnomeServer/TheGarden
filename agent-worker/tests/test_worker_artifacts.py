from __future__ import annotations

import importlib.util
import io
import json
import stat
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SPEC = importlib.util.spec_from_file_location(
    "worker_under_test", Path(__file__).parents[1] / "worker.py"
)
assert SPEC and SPEC.loader
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


def valid_model_session() -> dict[str, str]:
    return {
        "key": "model-session-key",
        "model_base_url": "https://models.example/v1",
        "model": "luna",
    }


class ArtifactPackagingTests(unittest.TestCase):
    def test_single_file_is_uploaded_as_zip_and_preserves_its_relative_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work_dir = Path(directory)
            (work_dir / "reports").mkdir()
            (work_dir / "reports" / "summary.txt").write_bytes(b"single file")

            filename, content_type, payload = worker.package_artifacts(
                work_dir, ["reports/summary.txt"]
            )

        self.assertEqual(filename, "artifacts.zip")
        self.assertEqual(content_type, "application/zip")
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.assertEqual(archive.namelist(), ["reports/summary.txt"])
            self.assertEqual(archive.read("reports/summary.txt"), b"single file")

    def test_multiple_files_produce_deterministic_deflated_zip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work_dir = Path(directory)
            (work_dir / "src").mkdir()
            (work_dir / "src" / "app.py").write_bytes(b"print('app')\n")
            (work_dir / "README.txt").write_bytes(b"project")

            filename, content_type, payload = worker.package_artifacts(
                work_dir, ["src/app.py", "README.txt"]
            )
            _same_filename, _same_content_type, reordered_payload = (
                worker.package_artifacts(work_dir, ["README.txt", "src/app.py"])
            )

        self.assertEqual(filename, "artifacts.zip")
        self.assertEqual(content_type, "application/zip")
        self.assertEqual(payload, reordered_payload)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.assertEqual(archive.namelist(), ["README.txt", "src/app.py"])
            self.assertEqual(archive.read("src/app.py"), b"print('app')\n")
            self.assertEqual(archive.read("README.txt"), b"project")
            for info in archive.infolist():
                self.assertEqual(info.date_time, (1980, 1, 1, 0, 0, 0))
                self.assertEqual(
                    info.external_attr,
                    (stat.S_IFREG | 0o644) << 16,
                )
                self.assertEqual(info.create_system, 3)
                self.assertEqual(info.compress_type, zipfile.ZIP_DEFLATED)

    def test_rejects_absolute_and_parent_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work_dir = Path(directory)
            for path in ("/etc/passwd", "../outside.txt", "folder/../outside.txt"):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    worker.package_artifacts(work_dir, [path])

    def test_rejects_symlinked_files_and_parents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work_dir = Path(directory)
            (work_dir / "target.txt").write_bytes(b"target")
            (work_dir / "file-link.txt").symlink_to(work_dir / "target.txt")
            (work_dir / "real-dir").mkdir()
            (work_dir / "real-dir" / "nested.txt").write_bytes(b"nested")
            (work_dir / "dir-link").symlink_to(work_dir / "real-dir", target_is_directory=True)

            for path in ("file-link.txt", "dir-link/nested.txt"):
                with self.subTest(path=path), self.assertRaisesRegex(ValueError, "symlink"):
                    worker.package_artifacts(work_dir, [path])

    def test_rejects_artifact_sets_over_total_size_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work_dir = Path(directory)
            oversized = work_dir / "large.bin"
            with oversized.open("wb") as artifact_file:
                artifact_file.truncate(worker.MAX_ARTIFACT_BYTES + 1)

            with self.assertRaisesRegex(ValueError, "total limit"):
                worker.package_artifacts(work_dir, ["large.bin"])

    def test_rejects_more_than_one_hundred_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work_dir = Path(directory)
            paths = []
            for index in range(worker.MAX_ARTIFACT_FILES + 1):
                name = f"file-{index}.txt"
                (work_dir / name).touch()
                paths.append(name)

            with self.assertRaisesRegex(ValueError, "100 files"):
                worker.package_artifacts(work_dir, paths)

    def test_upload_uses_bearer_header_and_verified_tls_without_token_payload(self) -> None:
        descriptor = {
            "id": "artifact-1",
            "filename": "summary.txt",
            "content_type": "text/plain",
            "size_bytes": 7,
            "sha256": "digest",
        }
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {"artifact": descriptor}
        ).encode()
        opener = MagicMock()
        opener.open.return_value = response
        secret = "run-scoped-capability"
        with tempfile.TemporaryDirectory() as directory:
            ca_file = Path(directory) / "root.crt"
            ca_file.write_text("certificate", encoding="utf-8")
            with (
                patch.object(worker.ssl, "create_default_context", return_value=object()) as tls,
                patch.object(worker.urllib.request, "build_opener", return_value=opener) as build_opener,
            ):
                result = worker.upload_artifact(
                    f"https://{worker.CADDY_HOST}/v1/omp/missions/run-1/artifact",
                    secret,
                    "run-1",
                    "summary.txt",
                    "text/plain",
                    b"payload",
                    ca_file,
                )

        self.assertEqual(result, descriptor)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.method, "PUT")
        self.assertEqual(request.data, b"payload")
        self.assertNotIn(secret.encode(), request.data)
        self.assertEqual(request.headers["Authorization"], f"Bearer {secret}")
        self.assertEqual(request.headers["X-artifact-filename"], "summary.txt")
        tls.assert_called_once_with(cafile=str(ca_file))
        handlers = build_opener.call_args.args
        self.assertIsInstance(handlers[0], worker.urllib.request.HTTPSHandler)
        self.assertIs(handlers[0]._context, tls.return_value)
        self.assertIsInstance(handlers[1], worker._RejectArtifactRedirects)


    def test_run_id_is_quoted_as_one_upload_path_segment(self) -> None:
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"artifact": {}}'
        opener = MagicMock()
        opener.open.return_value = response
        run_id = "run id"
        upload_url = (
            f"https://{worker.CADDY_HOST}/v1/omp/missions/run%20id/artifact"
        )
        with tempfile.TemporaryDirectory() as directory:
            ca_file = Path(directory) / "root.crt"
            ca_file.write_text("certificate", encoding="utf-8")
            with (
                patch.object(worker.ssl, "create_default_context", return_value=object()),
                patch.object(worker.urllib.request, "build_opener", return_value=opener),
            ):
                worker.upload_artifact(
                    upload_url,
                    "run-scoped-capability",
                    run_id,
                    "result.txt",
                    "text/plain",
                    b"result",
                    ca_file,
                )

        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, upload_url)

    def test_upload_rejects_untrusted_hosts_and_paths_before_networking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ca_file = Path(directory) / "root.crt"
            ca_file.write_text("certificate", encoding="utf-8")
            invalid_urls = (
                "https://attacker.example/v1/omp/missions/run-1/artifact",
                f"https://{worker.CADDY_HOST}/v1/omp/missions/run-2/artifact",
                f"https://{worker.CADDY_HOST}:444/v1/omp/missions/run-1/artifact",
            )
            with patch.object(worker.urllib.request, "build_opener") as build_opener:
                for upload_url in invalid_urls:
                    with self.subTest(upload_url=upload_url), self.assertRaisesRegex(
                        ValueError, "configured manager route"
                    ):
                        worker.upload_artifact(
                            upload_url,
                            "run-scoped-capability",
                            "run-1",
                            "summary.txt",
                            "text/plain",
                            b"payload",
                            ca_file,
                        )
                build_opener.assert_not_called()

    def test_redirect_handler_does_not_forward_uploads(self) -> None:
        handler = worker._RejectArtifactRedirects()
        self.assertIsNone(
            handler.redirect_request(
                None, None, 302, "Found", {}, "https://attacker.example/upload"
            )
        )


class ParentArtifactInputTests(unittest.TestCase):
    def descriptor(self, download_url: str, payload: bytes) -> dict:
        return {
            "parent_mission_id": "8a9e15ac-4f53-45b1-a7c8-34f7c8b0e3a1",
            "artifact_id": "4ba9c3c2-61b8-4b66-b5d5-804547e64ab1",
            "filename": "artifacts.zip",
            "content_type": "application/zip",
            "size_bytes": len(payload),
            "sha256": worker.hashlib.sha256(payload).hexdigest(),
            "download_url": download_url,
        }

    def test_download_checks_signed_manager_url_size_and_checksum(self) -> None:
        payload = b"signed parent archive"
        descriptor = self.descriptor(
            "https://"
            + worker.CADDY_HOST
            + "/v1/omp/missions/8a9e15ac-4f53-45b1-a7c8-34f7c8b0e3a1/"
            "artifacts/4ba9c3c2-61b8-4b66-b5d5-804547e64ab1/"
            "download?expires=123&signature="
            + "a" * 64,
            payload,
        )
        response = MagicMock()
        response.__enter__.return_value.read.return_value = payload
        opener = MagicMock()
        opener.open.return_value = response
        with tempfile.TemporaryDirectory() as directory:
            ca_file = Path(directory) / "root.crt"
            ca_file.write_text("certificate", encoding="utf-8")
            with (
                patch.object(worker.ssl, "create_default_context", return_value=object()),
                patch.object(worker.urllib.request, "build_opener", return_value=opener),
            ):
                result = worker.download_parent_artifact(descriptor, ca_file)

        self.assertEqual(result, payload)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.headers["Accept"], "application/zip")
        self.assertNotIn("Authorization", request.headers)

    def test_download_rejects_non_manager_hosts_before_networking(self) -> None:
        payload = b"archive"
        descriptor = self.descriptor(
            "https://attacker.example/artifact?expires=123&signature=" + "a" * 64,
            payload,
        )
        with patch.object(worker.urllib.request, "build_opener") as build_opener:
            with self.assertRaisesRegex(ValueError, "Manager download route"):
                worker.download_parent_artifact(descriptor, None)
        build_opener.assert_not_called()

    def test_zip_extraction_rejects_traversal_and_symlink_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, member in enumerate(
                ("../escape.txt", "/absolute.txt", "folder\\\\escape.txt")
            ):
                payload = io.BytesIO()
                with zipfile.ZipFile(payload, "w") as archive:
                    archive.writestr(member, b"unsafe")
                workspace = root / f"workspace-{index}"
                workspace.mkdir()
                with self.subTest(member=member), self.assertRaises(ValueError):
                    worker.extract_artifact_zip(payload.getvalue(), workspace)
                self.assertFalse((root / "escape.txt").exists())

            payload = io.BytesIO()
            symlink = zipfile.ZipInfo("link.txt")
            symlink.create_system = 3
            symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(payload, "w") as archive:
                archive.writestr(symlink, "target.txt")
            workspace = root / "symlink-workspace"
            workspace.mkdir()
            with self.assertRaisesRegex(ValueError, "unsafe path"):
                worker.extract_artifact_zip(payload.getvalue(), workspace)
            self.assertFalse((workspace / "link.txt").exists())


class MissionArtifactLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_workspace_is_removed_only_after_upload_finishes(self) -> None:
        descriptor = {
            "id": "artifact-1",
            "filename": "result.txt",
            "content_type": "text/plain",
            "size_bytes": 6,
            "sha256": "digest",
        }
        secret = "run-scoped-capability"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "worker-workspace"
            ca_file = root / "model-root.crt"
            ca_file.write_text("certificate", encoding="utf-8")
            observed_work_dir: list[Path] = []

            def fake_docker(command: list[str], **_kwargs: object) -> SimpleNamespace:
                work_mount = next(
                    item
                    for item in command
                    if item.startswith("type=bind,src=") and item.endswith(",dst=/work")
                )
                work_dir = Path(work_mount[len("type=bind,src=") : -len(",dst=/work")])
                observed_work_dir.append(work_dir)
                (work_dir / "result.txt").write_bytes(b"result")

                mission_mount = next(
                    item
                    for item in command
                    if item.startswith("type=bind,src=")
                    and item.endswith(",dst=/mission.json,readonly")
                )
                mission_path = Path(
                    mission_mount[len("type=bind,src=") : -len(",dst=/mission.json,readonly")]
                )
                env_path = Path(command[command.index("--env-file") + 1])
                self.assertNotIn(secret, mission_path.read_text(encoding="utf-8"))
                self.assertNotIn(secret, env_path.read_text(encoding="utf-8"))
                env_contents = env_path.read_text(encoding="utf-8")
                self.assertIn("MODEL_BASE_URL=https://models.example/v1", env_contents)
                self.assertIn("MODEL_API_KEY=model-session-key", env_contents)
                self.assertIn("OMP_MODEL=worker-litellm/luna", env_contents)
                self.assertEqual(stat.S_IMODE(env_path.stat().st_mode), 0o600)
                self.assertNotIn(secret, " ".join(command))
                self.assertNotIn("model-session-key", " ".join(command))
                output = {
                    "mission_id": "run-1",
                    "status": "completed",
                    "reviewer_approved": True,
                    "reviewer_count": 0,
                    "summary": "Review approved.",
                    "artifacts": ["result.txt"],
                }
                return SimpleNamespace(stdout=json.dumps(output), stderr=None)

            def fake_upload(
                upload_url: str,
                upload_token: str,
                run_id: str,
                filename: str,
                content_type: str,
                content: bytes,
                _ca_file: Path,
            ) -> dict:
                self.assertEqual(
                    upload_url,
                    f"https://{worker.CADDY_HOST}/v1/omp/missions/run-1/artifact",
                )
                self.assertEqual(upload_token, secret)
                self.assertEqual(run_id, "run-1")
                self.assertEqual(filename, "artifacts.zip")
                self.assertEqual(content_type, "application/zip")
                self.assertNotIn(secret.encode(), content)
                self.assertTrue(observed_work_dir[0].is_dir())
                with zipfile.ZipFile(io.BytesIO(content)) as archive:
                    self.assertEqual(archive.namelist(), ["result.txt"])
                    self.assertEqual(archive.read("result.txt"), b"result")
                return descriptor

            with (
                patch.object(worker, "WORKSPACE", workspace),
                patch.object(worker, "MODEL_CA_FILE", str(ca_file)),
                patch.object(worker.shutil, "which", return_value="/usr/bin/docker"),
                patch.object(worker.subprocess, "run", side_effect=fake_docker),
                patch.object(worker, "upload_artifact", side_effect=fake_upload),
                patch.object(
                    worker,
                    "get_mission_control",
                    return_value={
                        "mission_id": "run-1",
                        "status": "running",
                        "cancel_requested": False,
                    },
                ),
            ):
                result = await worker.run_mission_image(
                    {
                        "run_id": "run-1",
                        "goal": "Create a result file",
                    },
                    {
                        "artifact_upload_url": (
                            f"https://{worker.CADDY_HOST}/v1/omp/missions/run-1/artifact"
                        ),
                        "artifact_upload_token": secret,
                    },
                    model_session=valid_model_session(),
                )

            self.assertEqual(result["artifact"], descriptor)
            self.assertNotIn("artifacts", result)
            self.assertNotIn("script", result)
            self.assertTrue(observed_work_dir)
            self.assertFalse(observed_work_dir[0].exists())
            self.assertEqual(list(workspace.iterdir()), [])

    async def test_cancel_event_stops_named_omp_container(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "worker-workspace"
            ca_file = root / "model-root.crt"
            ca_file.write_text("certificate", encoding="utf-8")
            docker_started = threading.Event()
            docker_stopped = threading.Event()
            stopped_containers: list[str] = []
            control_states = iter(
                (
                    {"mission_id": "cancel-run", "status": "running", "cancel_requested": False},
                    {"mission_id": "cancel-run", "status": "cancel_requested", "cancel_requested": True},
                )
            )

            def fake_control(*_args: object) -> dict[str, object]:
                return next(control_states)

            def fake_docker(command: list[str], **_kwargs: object) -> SimpleNamespace:
                docker_started.set()
                self.assertIn("--name", command)
                if not docker_stopped.wait(timeout=3):
                    self.fail("cancellation did not stop the named OMP container")
                return SimpleNamespace(stdout="", stderr="stopped", returncode=143)

            def fake_stop(container_name: str) -> None:
                stopped_containers.append(container_name)
                docker_stopped.set()

            metadata = {
                "artifact_upload_url": (
                    f"https://{worker.CADDY_HOST}/v1/omp/missions/cancel-run/artifact"
                ),
                "artifact_control_url": (
                    f"https://{worker.CADDY_HOST}/v1/omp/missions/cancel-run/control"
                ),
                "artifact_upload_token": "run-scoped-token",
            }
            with (
                patch.object(worker, "WORKSPACE", workspace),
                patch.object(worker, "MODEL_CA_FILE", str(ca_file)),
                patch.object(worker, "CANCEL_POLL_INTERVAL_SECONDS", 0.01),
                patch.object(worker, "ACTIVE_MISSIONS", {}),
                patch.object(worker.shutil, "which", return_value="/usr/bin/docker"),
                patch.object(worker, "get_mission_control", side_effect=fake_control),
                patch.object(worker, "stop_omp_container", side_effect=fake_stop),
                patch.object(worker.subprocess, "run", side_effect=fake_docker),
            ):
                result = await worker.run_mission_image(
                    {"run_id": "cancel-run", "goal": "Create a result file"},
                    metadata,
                    model_session=valid_model_session(),
                )

        self.assertTrue(docker_started.is_set())
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(len(stopped_containers), 1)
        self.assertTrue(stopped_containers[0].startswith("omp-mission-"))


    async def test_queued_cancelled_mission_never_starts_docker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ca_file = Path(directory) / "model-root.crt"
            ca_file.write_text("certificate", encoding="utf-8")
            metadata = {
                "artifact_upload_url": (
                    f"https://{worker.CADDY_HOST}/v1/omp/missions/queued-run/artifact"
                ),
                "artifact_control_url": (
                    f"https://{worker.CADDY_HOST}/v1/omp/missions/queued-run/control"
                ),
                "artifact_upload_token": "run-scoped-token",
            }
            with (
                patch.object(worker, "MODEL_CA_FILE", str(ca_file)),
                patch.object(
                    worker,
                    "get_mission_control",
                    return_value={
                        "mission_id": "queued-run",
                        "status": "cancelled",
                        "cancel_requested": True,
                    },
                ),
                patch.object(worker.shutil, "which") as docker_lookup,
                patch.object(worker.subprocess, "run") as docker_run,
            ):
                result = await worker.run_mission_image(
                    {"run_id": "queued-run", "goal": "Create a result file"},
                    metadata,
                    model_session=valid_model_session(),
                )

        self.assertEqual(result["status"], "cancelled")
        docker_lookup.assert_not_called()
        docker_run.assert_not_called()
    async def test_needs_input_checkpoint_resumes_same_workspace_and_resets_count(self) -> None:
        run_id = "957b111b-2aa5-4fae-b3aa-5f658f27b1ef"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "worker-workspace"
            ca_file = root / "model-root.crt"
            ca_file.write_text("certificate", encoding="utf-8")
            outputs = [
                {
                    "mission_id": run_id,
                    "status": "needs_input",
                    "reviewer_approved": False,
                    "reviewer_count": 10,
                    "summary": "The reviewer still rejects the interface.",
                    "question": "Should the interface prioritize speed or accuracy?",
                    "artifacts": [],
                },
                {
                    "mission_id": run_id,
                    "status": "completed",
                    "reviewer_approved": True,
                    "reviewer_count": 0,
                    "summary": "The clarification resolved the review.",
                    "artifacts": ["result.txt"],
                },
            ]
            mission_documents: list[dict] = []

            def fake_docker(command: list[str], **_kwargs: object) -> SimpleNamespace:
                mission_mount = next(
                    item
                    for item in command
                    if item.startswith("type=bind,src=")
                    and item.endswith(",dst=/mission.json,readonly")
                )
                mission_path = Path(
                    mission_mount[len("type=bind,src=") : -len(",dst=/mission.json,readonly")]
                )
                mission_documents.append(json.loads(mission_path.read_text()))
                work_mount = next(
                    item
                    for item in command
                    if item.startswith("type=bind,src=") and item.endswith(",dst=/work")
                )
                work_dir = Path(work_mount[len("type=bind,src=") : -len(",dst=/work")])
                if len(mission_documents) == 1:
                    (work_dir / "draft.txt").write_text("first draft", encoding="utf-8")
                else:
                    self.assertEqual((work_dir / "draft.txt").read_text(), "first draft")
                    (work_dir / "result.txt").write_text("revised product", encoding="utf-8")
                return SimpleNamespace(stdout=json.dumps(outputs.pop(0)), stderr=None)

            metadata = {
                "operation": "mission",
                "artifact_upload_url": (
                    f"https://{worker.CADDY_HOST}/v1/omp/missions/{run_id}/artifact"
                ),
                "artifact_control_url": (
                    f"https://{worker.CADDY_HOST}/v1/omp/missions/{run_id}/control"
                ),
                "artifact_upload_token": "run-scoped-token",
                "project_name": "clarification-test",
            }
            with (
                patch.object(worker, "WORKSPACE", workspace),
                patch.object(worker, "MODEL_CA_FILE", str(ca_file)),
                patch.object(worker.shutil, "which", return_value="/usr/bin/docker"),
                patch.object(
                    worker,
                    "get_mission_control",
                    return_value={
                        "mission_id": run_id,
                        "status": "running",
                        "cancel_requested": False,
                    },
                ),
                patch.object(worker.subprocess, "run", side_effect=fake_docker),
                patch.object(
                    worker,
                    "upload_artifact",
                    return_value={"id": "artifact-1", "filename": "artifacts.zip"},
                ),
            ):
                first = await worker.run_mission_image(
                    {"run_id": run_id, "goal": "Build the requested interface."},
                    metadata,
                    model_session=valid_model_session(),
                )
                checkpoint = worker.mission_checkpoint_path(run_id)
                self.assertEqual(first["status"], "needs_input")
                self.assertTrue((checkpoint / "draft.txt").is_file())

                resumed = await worker.run_mission_image(
                    {"run_id": run_id, "goal": "Build the requested interface."},
                    {
                        **metadata,
                        "resume_from_checkpoint": True,
                        "clarification": "Prioritize accuracy.",
                        "reviewer_feedback": first["question"],
                        "reviewer_count_reset": 0,
                    },
                    model_session=valid_model_session(),
                )

        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(resumed["reviewer_count"], 0)
        self.assertEqual(
            mission_documents[1]["clarification"], "Prioritize accuracy."
        )
        self.assertEqual(mission_documents[1]["reviewer_count_start"], 0)
        self.assertFalse(checkpoint.exists())

    async def test_modification_starts_from_parent_artifact_and_uploads_updated_files(self) -> None:
        parent_id = "8a9e15ac-4f53-45b1-a7c8-34f7c8b0e3a1"
        child_id = "abc6aa27-03dc-47e1-9357-f5ca3cb1f6e0"
        artifact_id = "4ba9c3c2-61b8-4b66-b5d5-804547e64ab1"
        parent_archive_file = io.BytesIO()
        with zipfile.ZipFile(parent_archive_file, "w") as archive:
            archive.writestr("app/index.html", b"old product")
        parent_archive = parent_archive_file.getvalue()
        source_descriptor = {
            "parent_mission_id": parent_id,
            "artifact_id": artifact_id,
            "filename": "artifacts.zip",
            "content_type": "application/zip",
            "size_bytes": len(parent_archive),
            "sha256": worker.hashlib.sha256(parent_archive).hexdigest(),
            "download_url": (
                f"https://{worker.CADDY_HOST}/v1/omp/missions/{parent_id}/artifacts/"
                f"{artifact_id}/download?expires=123&signature={'a' * 64}"
            ),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "worker-workspace"
            ca_file = root / "model-root.crt"
            ca_file.write_text("certificate", encoding="utf-8")
            observed_work_dir: list[Path] = []

            def fake_docker(command: list[str], **_kwargs: object) -> SimpleNamespace:
                mission_mount = next(
                    item for item in command
                    if item.startswith("type=bind,src=")
                    and item.endswith(",dst=/mission.json,readonly")
                )
                mission_path = Path(
                    mission_mount[len("type=bind,src=") : -len(",dst=/mission.json,readonly")]
                )
                mission = json.loads(mission_path.read_text(encoding="utf-8"))
                self.assertEqual(mission["parent_mission_id"], parent_id)
                self.assertTrue(mission["modifies_existing_artifact"])
                work_mount = next(
                    item for item in command
                    if item.startswith("type=bind,src=") and item.endswith(",dst=/work")
                )
                work_dir = Path(work_mount[len("type=bind,src=") : -len(",dst=/work")])
                observed_work_dir.append(work_dir)
                original = work_dir / "app" / "index.html"
                self.assertEqual(original.read_bytes(), b"old product")
                original.write_bytes(b"updated product")
                (work_dir / "app" / "style.css").write_bytes(b"body { color: navy; }\n")
                result = {
                    "mission_id": child_id,
                    "status": "completed",
                    "reviewer_approved": True,
                    "reviewer_count": 0,
                    "summary": "The requested change passed review.",
                    "artifacts": ["app/index.html", "app/style.css"],
                }
                return SimpleNamespace(stdout=json.dumps(result), stderr=None, returncode=0)

            def fake_upload(
                _upload_url: str,
                _upload_token: str,
                run_id: str,
                filename: str,
                content_type: str,
                content: bytes,
                _ca_file: Path,
            ) -> dict:
                self.assertEqual(run_id, child_id)
                self.assertEqual(filename, "artifacts.zip")
                self.assertEqual(content_type, "application/zip")
                with zipfile.ZipFile(io.BytesIO(content)) as archive:
                    self.assertEqual(
                        archive.namelist(), ["app/index.html", "app/style.css"]
                    )
                    self.assertEqual(
                        archive.read("app/index.html"), b"updated product"
                    )
                    self.assertEqual(
                        archive.read("app/style.css"), b"body { color: navy; }\n"
                    )
                return {"id": "child-artifact", "filename": filename}

            with (
                patch.object(worker, "WORKSPACE", workspace),
                patch.object(worker, "MODEL_CA_FILE", str(ca_file)),
                patch.object(worker.shutil, "which", return_value="/usr/bin/docker"),
                patch.object(worker, "download_parent_artifact", return_value=parent_archive),
                patch.object(worker.subprocess, "run", side_effect=fake_docker),
                patch.object(worker, "upload_artifact", side_effect=fake_upload),
                patch.object(
                    worker,
                    "get_mission_control",
                    return_value={
                        "mission_id": child_id,
                        "status": "running",
                        "cancel_requested": False,
                    },
                ),
            ):
                result = await worker.run_mission_image(
                    {"run_id": child_id, "goal": "Add a dark mode to the app."},
                    {
                        "artifact_upload_url": (
                            f"https://{worker.CADDY_HOST}/v1/omp/missions/{child_id}/artifact"
                        ),
                        "artifact_control_url": (
                            f"https://{worker.CADDY_HOST}/v1/omp/missions/{child_id}/control"
                        ),
                        "artifact_upload_token": "child-upload-capability",
                        "parent_mission_id": parent_id,
                        "artifact_input": source_descriptor,
                    },
                    model_session=valid_model_session(),
                )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["artifact"]["filename"], "artifacts.zip")
        self.assertFalse(observed_work_dir[0].exists())


    async def test_cancel_during_source_download_prevents_container_launch(self) -> None:
        parent_id = "8a9e15ac-4f53-45b1-a7c8-34f7c8b0e3a1"
        child_id = "abc6aa27-03dc-47e1-9357-f5ca3cb1f6e0"
        parent_archive_file = io.BytesIO()
        with zipfile.ZipFile(parent_archive_file, "w") as archive:
            archive.writestr("app/index.html", b"existing product")
        control_states = iter(
            (
                {"mission_id": child_id, "status": "running", "cancel_requested": False},
                {"mission_id": child_id, "status": "cancel_requested", "cancel_requested": True},
            )
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ca_file = root / "model-root.crt"
            ca_file.write_text("certificate", encoding="utf-8")
            workspace = root / "worker-workspace"
            with (
                patch.object(worker, "WORKSPACE", workspace),
                patch.object(worker, "MODEL_CA_FILE", str(ca_file)),
                patch.object(worker.shutil, "which", return_value="/usr/bin/docker"),
                patch.object(
                    worker,
                    "get_mission_control",
                    side_effect=lambda *_args: next(control_states),
                ),
                patch.object(
                    worker,
                    "download_parent_artifact",
                    return_value=parent_archive_file.getvalue(),
                ),
                patch.object(worker.subprocess, "run") as docker_run,
            ):
                result = await worker.run_mission_image(
                    {"run_id": child_id, "goal": "Update the existing app."},
                    {
                        "artifact_upload_url": (
                            f"https://{worker.CADDY_HOST}/v1/omp/missions/{child_id}/artifact"
                        ),
                        "artifact_control_url": (
                            f"https://{worker.CADDY_HOST}/v1/omp/missions/{child_id}/control"
                        ),
                        "artifact_upload_token": "child-capability",
                        "parent_mission_id": parent_id,
                        "artifact_input": {"parent_mission_id": parent_id},
                    },
                    model_session=valid_model_session(),
                )

        self.assertEqual(result["status"], "cancelled")
        docker_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
