from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import secrets
import uuid
import weakref
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .database import get_session
from .models import ArtifactBlobRecord, MAX_ARTIFACT_BYTES, RunRecord
from .settings import settings

router = APIRouter()
DOWNLOAD_TTL_SECONDS = 7 * 24 * 60 * 60
FILENAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,254}$")
CONTENT_TYPE_PATTERN = re.compile(
    r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+"
    r"(?:;[ \t]*[A-Za-z0-9!#$&^_.+-]+=(?:[A-Za-z0-9!#$&^_.+-]+|\"[A-Za-z0-9!#$&^_.+-]*\"))*$"
)
# Weak values discard idle per-run locks while active callers retain their lock.
UPLOAD_LOCKS: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()


def upload_token_hash(token: str) -> str:
    message = b"agent-manager:artifact-upload:v1:" + token.encode("utf-8")
    return hmac.new(settings.signing_key, message, hashlib.sha256).hexdigest()


def provision_upload_capability(record: RunRecord) -> tuple[str, str]:
    """Persist only the hash and return the secret and URL for a JetStream event."""
    token = secrets.token_urlsafe(32)
    metadata = dict(record.run_metadata or {})
    metadata["artifact_upload_token_hash"] = upload_token_hash(token)
    record.run_metadata = metadata
    upload_url = (
        f"{settings.public_url.rstrip('/')}/v1/omp/missions/{record.id}/artifact"
    )
    return token, upload_url


def upload_event_metadata(
    record: RunRecord, token: str, upload_url: str
) -> dict[str, Any]:
    metadata = dict(record.run_metadata or {})
    metadata.pop("artifact_upload_token_hash", None)
    metadata["artifact_upload_url"] = upload_url
    metadata["artifact_upload_token"] = token
    return metadata


def validate_artifact_id(artifact_id: str) -> bool:
    try:
        return str(uuid.UUID(artifact_id)) == artifact_id
    except (ValueError, TypeError, AttributeError):
        return False


def validate_filename(filename: str | None) -> str:
    if (
        not isinstance(filename, str)
        or not FILENAME_PATTERN.fullmatch(filename)
        or filename in {".", ".."}
    ):
        raise HTTPException(status_code=400, detail="invalid artifact filename")
    return filename


def _public_descriptor(artifact: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": artifact["id"],
        "filename": artifact["filename"],
        "content_type": artifact["content_type"],
        "size_bytes": artifact["size_bytes"],
        "sha256": artifact["sha256"],
    }


def artifact_signature(run_id: str, artifact_id: str, expires: int) -> str:
    message = (
        f"agent-manager:artifact-download:v1:{run_id}:{artifact_id}:{expires}"
    ).encode("utf-8")
    return hmac.new(settings.signing_key, message, hashlib.sha256).hexdigest()


def verify_artifact_signature(
    run_id: str, artifact_id: str, expires: int, signature: str, *, now: int | None = None
) -> bool:
    current_time = int(datetime.now(timezone.utc).timestamp()) if now is None else now
    if expires <= current_time or expires > current_time + DOWNLOAD_TTL_SECONDS:
        return False
    expected = artifact_signature(run_id, artifact_id, expires)
    return hmac.compare_digest(expected.encode("ascii"), signature.encode("utf-8"))


def artifact_download_url(run_id: str, artifact: dict[str, Any]) -> str:
    expires = int(datetime.now(timezone.utc).timestamp()) + DOWNLOAD_TTL_SECONDS
    signature = artifact_signature(run_id, str(artifact["id"]), expires)
    query = urlencode({"expires": expires, "signature": signature})
    return (
        f"{settings.public_url.rstrip('/')}/v1/omp/missions/{run_id}/artifacts/"
        f"{artifact['id']}/download?{query}"
    )


def mission_artifact(metadata: dict[str, Any], run_id: str) -> dict[str, Any] | None:
    artifact = metadata.get("artifact")
    if not isinstance(artifact, dict):
        return None
    try:
        descriptor = _public_descriptor(artifact)
    except (KeyError, TypeError):
        return None
    if not validate_artifact_id(descriptor["id"]):
        return None
    descriptor["download_url"] = artifact_download_url(run_id, artifact)
    return descriptor


@router.put("/v1/omp/missions/{run_id}/artifact")
async def upload_artifact(
    run_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, dict[str, Any]]:
    authorization = request.headers.get("authorization", "")
    scheme, separator, token = authorization.partition(" ")
    if scheme.casefold() != "bearer" or not separator or not token or " " in token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid artifact upload capability",
            headers={"WWW-Authenticate": "Bearer"},
        )

    record = await session.get(RunRecord, run_id)
    metadata = dict(record.run_metadata or {}) if record is not None else {}
    stored_hash = metadata.get("artifact_upload_token_hash")
    if (
        record is None
        or not isinstance(stored_hash, str)
        or not hmac.compare_digest(
            stored_hash.encode("utf-8"), upload_token_hash(token).encode("ascii")
        )
    ):
        raise HTTPException(status_code=401, detail="invalid artifact upload capability")
    if metadata.get("operation") != "mission":
        raise HTTPException(status_code=409, detail="artifact uploads are only available for missions")
    if record.status not in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="mission is not accepting an artifact")

    filename = validate_filename(request.headers.get("x-artifact-filename"))
    content_type = request.headers.get("content-type", "")
    if (
        len(content_type) > 255
        or not CONTENT_TYPE_PATTERN.fullmatch(content_type)
    ):
        raise HTTPException(status_code=400, detail="invalid artifact content type")
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_size = int(content_length)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid content length") from exc
        if declared_size < 0:
            raise HTTPException(status_code=400, detail="invalid content length")
        if declared_size > MAX_ARTIFACT_BYTES:
            raise HTTPException(status_code=413, detail="artifact exceeds 50 MiB limit")
    await session.rollback()

    lock = UPLOAD_LOCKS.setdefault(run_id, asyncio.Lock())
    async with lock:
        payload = bytearray()
        digest = hashlib.sha256()
        async for chunk in request.stream():
            if len(payload) + len(chunk) > MAX_ARTIFACT_BYTES:
                raise HTTPException(status_code=413, detail="artifact exceeds 50 MiB limit")
            payload.extend(chunk)
            digest.update(chunk)
        content = bytes(payload)
        size_bytes = len(content)
        sha256 = digest.hexdigest()

        record = await session.get(RunRecord, run_id)
        if record is None:
            raise HTTPException(status_code=401, detail="invalid artifact upload capability")
        # Serialize artifact metadata updates across Manager processes.
        await session.refresh(record, with_for_update=True)
        metadata = dict(record.run_metadata or {})
        stored_hash = metadata.get("artifact_upload_token_hash")
        if (
            record is None
            or not isinstance(stored_hash, str)
            or not hmac.compare_digest(
                stored_hash.encode("utf-8"), upload_token_hash(token).encode("ascii")
            )
        ):
            raise HTTPException(status_code=401, detail="invalid artifact upload capability")
        if metadata.get("operation") != "mission":
            raise HTTPException(status_code=409, detail="artifact uploads are only available for missions")
        if record.status not in {"queued", "running"}:
            raise HTTPException(status_code=409, detail="mission is not accepting an artifact")
        existing = metadata.get("artifact")
        if isinstance(existing, dict):
            same_content = (
                existing.get("sha256") == sha256
                and existing.get("size_bytes") == size_bytes
            )
            if same_content:
                return {"artifact": _public_descriptor(existing)}
            raise HTTPException(
                status_code=409, detail="mission already has a different artifact"
            )
        if metadata.get("artifact_upload_consumed"):
            raise HTTPException(status_code=409, detail="mission artifact upload is already used")

        artifact_id = str(uuid.uuid4())
        uploaded_at = datetime.now(timezone.utc)
        artifact = {
            "id": artifact_id,
            "filename": filename,
            "content_type": content_type,
            "size_bytes": size_bytes,
            "sha256": sha256,
            "created_at": uploaded_at.isoformat(),
        }
        metadata["artifact"] = artifact
        metadata["artifact_upload_consumed"] = True
        record.run_metadata = metadata
        session.add(
            ArtifactBlobRecord(
                id=artifact_id,
                run_id=run_id,
                filename=filename,
                content_type=content_type,
                size_bytes=size_bytes,
                sha256=sha256,
                content=content,
                created_at=uploaded_at,
            )
        )
        await session.commit()
        return {"artifact": _public_descriptor(artifact)}


@router.get(
    "/v1/omp/missions/{run_id}/artifacts/{artifact_id}/download",
    response_class=Response,
)
async def download_artifact(
    run_id: str,
    artifact_id: str,
    expires: int,
    signature: str,
    session: AsyncSession = Depends(get_session),
) -> Response:
    if not validate_artifact_id(artifact_id) or not verify_artifact_signature(
        run_id, artifact_id, expires, signature
    ):
        raise HTTPException(status_code=403, detail="artifact link is invalid or expired")
    artifact = await session.get(ArtifactBlobRecord, artifact_id)
    if artifact is None or artifact.run_id != run_id:
        raise HTTPException(status_code=404, detail="artifact not found")
    return Response(
        content=artifact.content,
        media_type=artifact.content_type,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'attachment; filename="{artifact.filename}"',
        },
    )


async def cleanup_expired_artifacts(session: AsyncSession) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.artifact_retention_days)
    result = await session.execute(
        select(ArtifactBlobRecord).where(ArtifactBlobRecord.created_at < cutoff)
    )
    removed = 0
    for blob in result.scalars().all():
        record = await session.get(RunRecord, blob.run_id)
        if record is not None:
            metadata = dict(record.run_metadata or {})
            artifact = metadata.get("artifact")
            if isinstance(artifact, dict) and artifact.get("id") == blob.id:
                metadata.pop("artifact", None)
            completion = metadata.get("result")
            if isinstance(completion, dict):
                completion_artifact = completion.get("artifact")
                if isinstance(completion_artifact, dict) and completion_artifact.get("id") == blob.id:
                    completion = dict(completion)
                    completion.pop("artifact", None)
                    metadata["result"] = completion
            metadata["artifact_upload_consumed"] = True
            record.run_metadata = metadata
        await session.delete(blob)
        removed += 1
    if removed:
        await session.commit()
    return removed
