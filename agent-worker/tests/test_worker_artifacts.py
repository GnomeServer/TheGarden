from __future__ import annotations

import importlib.util
import io
import json
import stat
import tempfile
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


class ArtifactPackagingTests(unittest.TestCase):
    def test_single_file_is_uploaded_raw_with_its_requested_filename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work_dir = Path(directory)
            (work_dir / "reports").mkdir()
            (work_dir / "reports" / "summary.txt").write_bytes(b"single file")

            filename, content_type, payload = worker.package_artifacts(
                work_dir, ["reports/summary.txt"]
            )

        self.assertEqual(filename, "summary.txt")
        self.assertEqual(content_type, "text/plain")
        self.assertEqual(payload, b"single file")

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
                self.assertNotIn(secret, " ".join(command))
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
                self.assertEqual(filename, "result.txt")
                self.assertEqual(content_type, "text/plain")
                self.assertEqual(content, b"result")
                self.assertNotIn(secret.encode(), content)
                self.assertTrue(observed_work_dir[0].is_dir())
                self.assertEqual((observed_work_dir[0] / filename).read_bytes(), content)
                return descriptor

            with (
                patch.object(worker, "WORKSPACE", workspace),
                patch.object(worker, "MODEL_BASE_URL", "https://models.example/v1"),
                patch.object(worker, "MODEL_API_KEY", "model-key"),
                patch.object(worker, "MODEL_CA_FILE", str(ca_file)),
                patch.object(worker.shutil, "which", return_value="/usr/bin/docker"),
                patch.object(worker.subprocess, "run", side_effect=fake_docker),
                patch.object(worker, "upload_artifact", side_effect=fake_upload),
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
                )

            self.assertEqual(result["artifact"], descriptor)
            self.assertNotIn("artifacts", result)
            self.assertNotIn("script", result)
            self.assertTrue(observed_work_dir)
            self.assertFalse(observed_work_dir[0].exists())
            self.assertEqual(list(workspace.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
