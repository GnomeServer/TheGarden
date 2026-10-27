from __future__ import annotations

import io
import time
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from agent_manager import artifacts
from agent_manager.artifacts import (
    DOWNLOAD_TTL_SECONDS,
    artifact_signature,
    cleanup_expired_artifacts,
    download_artifact,
    provision_upload_capability,
    upload_artifact,
    upload_event_metadata,
    upload_token_hash,
    verify_artifact_signature,
)
from agent_manager.models import ArtifactBlobRecord, RunRecord
from agent_manager.main import RunCreate, RunOut, apply_run_completion, create_run
from agent_manager.omp import mission_status


class UploadRequest:
    def __init__(self, token: str, filename: str, body: tuple[bytes, ...]):
        self.headers = {
            "authorization": f"Bearer {token}",
            "x-artifact-filename": filename,
            "content-type": "application/zip",
        }
        self.body = body
        self.consumed = False

    async def stream(self):
        self.consumed = True
        for chunk in self.body:
            yield chunk


class MemorySession:
    def __init__(self, record):
        self.record = record
        self.blobs = {}
        self.commit = AsyncMock()
        self.rollback = AsyncMock()

    async def get(self, model, key, **_kwargs):
        if model is RunRecord:
            return self.record if self.record.id == key else None
        if model is ArtifactBlobRecord:
            return self.blobs.get(key)
        raise AssertionError(f"unexpected model lookup: {model}")

    async def refresh(self, _record, **_kwargs):
        return None

    def add(self, blob):
        self.blobs[blob.id] = blob

    async def delete(self, blob):
        self.blobs.pop(blob.id, None)

    async def execute(self, _query):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(self.blobs.values())))



class SessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, traceback):
        return False

class ArtifactTests(unittest.IsolatedAsyncioTestCase):
    def make_run(self):
        token = "run-scoped-capability"
        record = SimpleNamespace(
            id="run-1",
            status="queued",
            run_metadata={
                "operation": "mission",
                "artifact_upload_token_hash": upload_token_hash(token),
            },
        )
        return token, record

    async def test_upload_auth_scope_size_and_idempotency(self):
        token, record = self.make_run()
        session = MemorySession(record)
        unauthorized = UploadRequest("wrong-token", "result.zip", (b"bytes",))
        with self.assertRaises(HTTPException) as caught:
            await upload_artifact("run-1", unauthorized, session)
        self.assertEqual(caught.exception.status_code, 401)
        self.assertFalse(unauthorized.consumed)
        other_record = SimpleNamespace(
            id="run-2",
            status="queued",
            run_metadata={"artifact_upload_token_hash": upload_token_hash("other-run-token")},
        )
        with self.assertRaises(HTTPException) as caught:
            await upload_artifact(
                "run-2",
                UploadRequest(token, "result.zip", (b"bytes",)),
                MemorySession(other_record),
            )
        self.assertEqual(caught.exception.status_code, 401)
        nonmission = SimpleNamespace(
            id="run-3",
            status="queued",
            run_metadata={
                "operation": "create-python-script",
                "artifact_upload_token_hash": upload_token_hash(token),
            },
        )
        nonmission_request = UploadRequest(token, "result.zip", (b"bytes",))
        with self.assertRaises(HTTPException) as caught:
            await upload_artifact(
                "run-3", nonmission_request, MemorySession(nonmission)
            )
        self.assertEqual(caught.exception.status_code, 409)
        self.assertFalse(nonmission_request.consumed)

        with self.assertRaises(HTTPException) as caught:
            await upload_artifact(
                "run-1", UploadRequest(token, "../escape.zip", (b"bytes",)), session
            )
        self.assertEqual(caught.exception.status_code, 400)

        request = UploadRequest(token, "result.zip", (b"zip", b" bytes"))
        with patch("agent_manager.artifacts.MAX_ARTIFACT_BYTES", 5):
            with self.assertRaises(HTTPException) as caught:
                await upload_artifact("run-1", request, session)
        self.assertEqual(caught.exception.status_code, 413)
        self.assertNotIn("artifact", record.run_metadata)
        self.assertEqual(session.blobs, {})

        body = (b"zip", b" bytes")
        with patch("agent_manager.artifacts.MAX_ARTIFACT_BYTES", 64):
            first = await upload_artifact(
                "run-1", UploadRequest(token, "result.zip", body), session
            )
            retry = await upload_artifact(
                "run-1", UploadRequest(token, "renamed-result.zip", body), session
            )
        self.assertEqual(first, retry)
        self.assertEqual(len(session.blobs), 1)
        self.assertEqual(session.blobs[first["artifact"]["id"]].content, b"zip bytes")
        self.assertNotIn(token, str(record.run_metadata))
        self.assertNotIn("artifact_upload_url", record.run_metadata)
        self.assertNotIn("/app/", str(record.run_metadata))
        record.status = "completed"
        terminal_retry = UploadRequest(token, "result.zip", body)
        with self.assertRaises(HTTPException) as caught:
            await upload_artifact("run-1", terminal_retry, session)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertFalse(terminal_retry.consumed)

    async def test_signed_download_returns_exact_bytes_until_expiry(self):
        token, record = self.make_run()
        session = MemorySession(record)
        result = await upload_artifact(
            "run-1", UploadRequest(token, "result.zip", (b"exact", b" bytes")), session
        )
        artifact_id = result["artifact"]["id"]
        expiry = int(time.time()) + 300
        signature = artifact_signature("run-1", artifact_id, expiry)
        response = await download_artifact(
            "run-1", artifact_id, expiry, signature, session
        )
        self.assertEqual(response.body, b"exact bytes")
        self.assertEqual(response.headers["content-disposition"], 'attachment; filename="result.zip"')
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

        expired = int(time.time()) - 1
        with self.assertRaises(HTTPException) as caught:
            await download_artifact(
                "run-1", artifact_id, expired,
                artifact_signature("run-1", artifact_id, expired), session,
            )
        self.assertEqual(caught.exception.status_code, 403)
        self.assertTrue(verify_artifact_signature("run-1", artifact_id, expiry, signature))
        self.assertFalse(verify_artifact_signature("run-1", artifact_id, expiry, signature, now=expiry))

    def test_run_api_output_hides_upload_capability_metadata(self):
        now = datetime.now(timezone.utc)
        secret_hash = upload_token_hash("never-return-this-capability")
        record = SimpleNamespace(
            id="run-1",
            goal="deliver",
            forgejo_repository="owner/repo",
            base_ref="main",
            model=None,
            status="queued",
            error=None,
            run_metadata={"artifact_upload_token_hash": secret_hash},
            worker_id=None,
            started_at=None,
            completed_at=None,
            created_at=now,
            updated_at=now,
        )
        result = RunOut.from_record(record)
        self.assertNotIn("artifact_upload_token_hash", result.metadata)
        self.assertNotIn(secret_hash, str(result.model_dump()))
        self.assertNotIn("/app/artifacts", str(result.model_dump()))

    async def test_direct_run_publishes_capability_without_persisting_it(self):
        records = []

        def add(record):
            records.append(record)

        async def refresh(record):
            now = datetime.now(timezone.utc)
            record.created_at = now
            record.updated_at = now

        session = SimpleNamespace(
            add=add,
            flush=AsyncMock(),
            commit=AsyncMock(),
            refresh=AsyncMock(side_effect=refresh),
        )
        request = SimpleNamespace(
            state=SimpleNamespace(auth_method="bearer"),
            app=SimpleNamespace(state=SimpleNamespace(nats=object())),
        )
        actor = SimpleNamespace(id="user-1", display_name="Worker Owner", forgejo_login="owner")
        publish = AsyncMock()
        with (
            patch("agent_manager.main.find_available_worker", new=AsyncMock(return_value={})),
            patch("agent_manager.main.publish_run_event", new=publish),
            patch("agent_manager.main.record_activity", new=AsyncMock()),
            patch("agent_manager.main.live_broker.publish", new=AsyncMock()),
        ):
            response = await create_run(
                RunCreate(
                    goal="Create a result bundle",
                    forgejo_repository="owner/repo",
                    metadata={"operation": "mission"},
                ),
                request,
                actor,
                session,
            )
        record = records[0]
        event_metadata = publish.await_args.args[2]["metadata"]
        self.assertTrue(event_metadata["artifact_upload_token"])
        self.assertTrue(event_metadata["artifact_upload_url"].endswith(
            f"/v1/omp/missions/{record.id}/artifact"
        ))
        self.assertNotIn("artifact_upload_token_hash", event_metadata)
        self.assertEqual(
            record.run_metadata["artifact_upload_token_hash"],
            upload_token_hash(event_metadata["artifact_upload_token"]),
        )
        self.assertNotIn("artifact_upload_token", record.run_metadata)
        self.assertNotIn("artifact_upload_url", record.run_metadata)
        self.assertNotIn("artifact_upload_token_hash", response.metadata)

    async def test_direct_nonmission_run_gets_no_artifact_capability(self):
        records = []

        async def refresh(record):
            now = datetime.now(timezone.utc)
            record.created_at = now
            record.updated_at = now

        session = SimpleNamespace(
            add=records.append,
            flush=AsyncMock(),
            commit=AsyncMock(),
            refresh=AsyncMock(side_effect=refresh),
        )
        request = SimpleNamespace(
            state=SimpleNamespace(auth_method="bearer"),
            app=SimpleNamespace(state=SimpleNamespace(nats=object())),
        )
        actor = SimpleNamespace(id="user-1", display_name="Owner", forgejo_login="owner")
        publish = AsyncMock()
        with (
            patch("agent_manager.main.find_available_worker", new=AsyncMock(return_value={})),
            patch("agent_manager.main.publish_run_event", new=publish),
            patch("agent_manager.main.record_activity", new=AsyncMock()),
            patch("agent_manager.main.live_broker.publish", new=AsyncMock()),
        ):
            await create_run(
                RunCreate(
                    goal="Create a Python script",
                    forgejo_repository="owner/repo",
                    metadata={"operation": "create-python-script"},
                ),
                request,
                actor,
                session,
            )
        event_metadata = publish.await_args.args[2]["metadata"]
        self.assertEqual(event_metadata["operation"], "create-python-script")
        self.assertNotIn("artifact_upload_token", event_metadata)
        self.assertNotIn("artifact_upload_url", event_metadata)
        self.assertNotIn("artifact_upload_token_hash", event_metadata)
        self.assertNotIn("artifact_upload_token_hash", records[0].run_metadata)

    async def test_status_exposes_signed_singular_artifact_descriptor(self):
        artifact = {
            "id": "47e03d42-1a02-4ccf-8e1f-006d53f5ddde",
            "filename": "result.zip",
            "content_type": "application/zip",
            "size_bytes": 10,
            "sha256": "a" * 64,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        record = SimpleNamespace(
            id="run-1",
            status="completed",
            run_metadata={"operation": "mission", "artifact": artifact, "ledger": {}},
        )
        session = SimpleNamespace(get=AsyncMock(return_value=record))
        issued_at = int(time.time())
        status_out = await mission_status("run-1", SimpleNamespace(), session)
        self.assertEqual(set(status_out["artifact"]), {
            "id", "filename", "content_type", "size_bytes", "sha256", "download_url"
        })
        self.assertNotIn("script", status_out)
        query = parse_qs(urlparse(status_out["artifact"]["download_url"]).query)
        expires = int(query["expires"][0])
        self.assertIn(expires - issued_at, {DOWNLOAD_TTL_SECONDS, DOWNLOAD_TTL_SECONDS + 1})
        self.assertTrue(verify_artifact_signature(
            "run-1", artifact["id"], expires, query["signature"][0]
        ))

    async def test_upload_capability_is_only_in_event_metadata(self):
        record = SimpleNamespace(id="run-1", run_metadata={"operation": "mission"})
        token, upload_url = provision_upload_capability(record)
        event_metadata = upload_event_metadata(record, token, upload_url)
        self.assertEqual(event_metadata["artifact_upload_token"], token)
        self.assertEqual(event_metadata["artifact_upload_url"], upload_url)
        self.assertNotIn("artifact_upload_token_hash", event_metadata)
        self.assertEqual(record.run_metadata["artifact_upload_token_hash"], upload_token_hash(token))
        self.assertNotIn(token, str(record.run_metadata))
        self.assertNotIn(upload_url, str(record.run_metadata))

    async def test_completion_persists_only_the_worker_artifact_descriptor(self):
        record = SimpleNamespace(
            id="run-1",
            status="running",
            run_metadata={
                "artifact_upload_token_hash": upload_token_hash("private-capability"),
                "artifact_upload_url": "https://manager.invalid/upload",
            },
            error=None,
            worker_id=None,
            started_at=None,
            completed_at=None,
        )

        async def get_record(model, _record_id, **_kwargs):
            return record if model is RunRecord else None

        session = SimpleNamespace(
            get=AsyncMock(side_effect=get_record),
            scalar=AsyncMock(return_value=None),
            commit=AsyncMock(),
        )
        descriptor = {
            "id": "47e03d42-1a02-4ccf-8e1f-006d53f5ddde",
            "filename": "result.zip",
            "content_type": "application/zip",
            "size_bytes": 4,
            "sha256": "c" * 64,
        }
        with (
            patch("agent_manager.main.SessionLocal", return_value=SessionContext(session)),
            patch("agent_manager.main.record_activity", new_callable=AsyncMock, return_value=object()),
            patch("agent_manager.main.activity_out", return_value={}),
            patch("agent_manager.main.live_broker.publish", new_callable=AsyncMock),
        ):
            await apply_run_completion(
                {
                    "run_id": "run-1",
                    "status": "completed",
                    "artifact": descriptor,
                    "artifact_upload_token": "must-not-persist",
                    "artifact_upload_url": "https://manager.invalid/upload",
                }
            )
        self.assertEqual(record.run_metadata["result"]["artifact"], descriptor)
        self.assertNotIn("artifact_upload_token", record.run_metadata["result"])
        self.assertNotIn("artifact_upload_url", record.run_metadata["result"])
        self.assertNotIn("artifact_upload_token_hash", record.run_metadata)
        self.assertNotIn("artifact_upload_url", record.run_metadata)

    async def test_retention_removes_blob_and_preserves_single_upload_consumption(self):
        _token, record = self.make_run()
        expired_at = datetime.now(timezone.utc) - timedelta(days=40)
        old_artifact = {
            "id": "47e03d42-1a02-4ccf-8e1f-006d53f5ddde",
            "filename": "result.zip",
            "content_type": "application/zip",
            "size_bytes": 4,
            "sha256": "b" * 64,
            "created_at": expired_at.isoformat(),
        }
        record.run_metadata["artifact"] = old_artifact
        record.run_metadata["result"] = {"artifact": dict(old_artifact)}
        session = MemorySession(record)
        session.blobs[old_artifact["id"]] = SimpleNamespace(
            id=old_artifact["id"], run_id=record.id, created_at=expired_at
        )
        removed = await cleanup_expired_artifacts(session)
        self.assertEqual(removed, 1)
        self.assertNotIn(old_artifact["id"], session.blobs)
        self.assertNotIn("artifact", record.run_metadata)
        self.assertTrue(record.run_metadata["artifact_upload_consumed"])
        self.assertNotIn("artifact", record.run_metadata["result"])


class ArtifactHttpRoundTripTests(unittest.TestCase):
    def test_single_file_and_project_zip_round_trip_through_http_routes(self) -> None:
        archive_buffer = io.BytesIO()
        with zipfile.ZipFile(
            archive_buffer, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            archive.writestr("src/main.py", b"print('project')\n")
            archive.writestr("README.md", b"project files")

        cases = (
            (
                "00000000-0000-4000-8000-000000000001",
                "tool.py",
                "text/x-python",
                b"print('downloaded')\n",
            ),
            (
                "00000000-0000-4000-8000-000000000002",
                "project.zip",
                "application/zip",
                archive_buffer.getvalue(),
            ),
        )
        for run_id, filename, content_type, payload in cases:
            with self.subTest(filename=filename):
                token = f"capability-{run_id}"
                record = SimpleNamespace(
                    id=run_id,
                    status="queued",
                    run_metadata={
                        "operation": "mission",
                        "artifact_upload_token_hash": upload_token_hash(token),
                    },
                )
                session = MemorySession(record)
                app = FastAPI()
                app.include_router(artifacts.router)

                async def override_session():
                    yield session

                app.dependency_overrides[artifacts.get_session] = override_session
                with TestClient(app) as client:
                    uploaded = client.put(
                        f"/v1/omp/missions/{run_id}/artifact",
                        content=payload,
                        headers={
                            "Authorization": f"Bearer {token}",
                            "X-Artifact-Filename": filename,
                            "Content-Type": content_type,
                        },
                    )
                    self.assertEqual(uploaded.status_code, 200)
                    descriptor = uploaded.json()["artifact"]
                    self.assertEqual(descriptor["filename"], filename)
                    signed_url = artifacts.artifact_download_url(run_id, descriptor)
                    parsed = urlparse(signed_url)
                    downloaded = client.get(f"{parsed.path}?{parsed.query}")
                    self.assertEqual(downloaded.status_code, 200)
                    self.assertEqual(downloaded.content, payload)
                    self.assertEqual(
                        downloaded.headers["content-disposition"],
                        f'attachment; filename="{filename}"',
                    )
                    if content_type == "application/zip":
                        with zipfile.ZipFile(io.BytesIO(downloaded.content)) as archive:
                            self.assertEqual(
                                archive.namelist(), ["src/main.py", "README.md"]
                            )
                            self.assertEqual(
                                archive.read("src/main.py"), b"print('project')\n"
                            )
if __name__ == "__main__":
    unittest.main()
