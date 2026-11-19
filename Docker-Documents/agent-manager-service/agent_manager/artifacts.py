from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import re
import secrets
import stat
import uuid
import weakref
import zipfile
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
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
HTML_SUFFIXES = frozenset({".html", ".htm"})
PREVIEW_MEDIA_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".txt": "text/plain; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
}
PREVIEW_CSP = (
    "default-src 'none'; script-src 'none'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
    "font-src 'self' data:; media-src 'self' data: blob:; "
    "connect-src 'none'; object-src 'none'; frame-src 'none'; "
    "base-uri 'none'; form-action 'none'"
)
MAX_PREVIEW_FILES = 100
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
    metadata.pop("mission_cancel_token_hash", None)
    metadata["artifact_upload_url"] = upload_url
    metadata["artifact_control_url"] = (
        f"{settings.public_url.rstrip('/')}/v1/omp/missions/{record.id}/control"
    )
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

def artifact_preview_url(run_id: str, artifact: dict[str, Any]) -> str:
    expires = int(datetime.now(timezone.utc).timestamp()) + DOWNLOAD_TTL_SECONDS
    signature = artifact_signature(run_id, str(artifact["id"]), expires)
    return (
        f"{settings.public_url.rstrip('/')}/v1/omp/missions/{run_id}/artifacts/"
        f"{artifact['id']}/preview/{expires}/{signature}/"
    )


def _safe_archive_path(value: str) -> bool:
    if not value or "\x00" in value or "\\" in value or any(c in value for c in "\r\n"):
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and path.as_posix() == value
        and bool(path.parts)
        and not any(part in {"", ".", ".."} for part in path.parts)
        and not re.match(r"^[A-Za-z]:", path.parts[0])
    )


def html_preview_entrypoint(paths: Any) -> str | None:
    if not isinstance(paths, list):
        return None
    candidates = {
        path
        for path in paths
        if isinstance(path, str)
        and _safe_archive_path(path)
        and PurePosixPath(path).suffix.casefold() in HTML_SUFFIXES
    }
    root_indexes = [
        path for path in candidates
        if "/" not in path and PurePosixPath(path).name.casefold() in {"index.html", "index.htm"}
    ]
    if len(root_indexes) == 1:
        return root_indexes[0]
    index_files = [
        path for path in candidates
        if PurePosixPath(path).name.casefold() in {"index.html", "index.htm"}
    ]
    if len(index_files) == 1:
        return index_files[0]
    if len(candidates) == 1:
        return next(iter(candidates))
    return None


def _preview_archive(content: bytes) -> tuple[zipfile.ZipFile, dict[str, zipfile.ZipInfo]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
        infos = archive.infolist()
    except (OSError, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise HTTPException(status_code=422, detail="artifact is not a previewable ZIP") from exc
    if len(infos) > MAX_PREVIEW_FILES:
        archive.close()
        raise HTTPException(status_code=422, detail="artifact has too many preview files")
    files: dict[str, zipfile.ZipInfo] = {}
    total_size = 0
    for info in infos:
        name = info.filename[:-1] if info.is_dir() else info.filename
        if not _safe_archive_path(name):
            archive.close()
            raise HTTPException(status_code=422, detail="artifact contains an unsafe preview path")
        if info.is_dir():
            continue
        mode = stat.S_IFMT(info.external_attr >> 16)
        if (
            mode not in {0, stat.S_IFREG}
            or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
            or name in files
        ):
            archive.close()
            raise HTTPException(status_code=422, detail="artifact contains an unsupported preview entry")
        total_size += info.file_size
        if info.file_size < 0 or total_size > MAX_ARTIFACT_BYTES:
            archive.close()
            raise HTTPException(status_code=413, detail="preview exceeds the artifact size limit")
        files[name] = info
    return archive, files


def _preview_response(content: bytes, media_type: str, *, html_page: bool = False) -> Response:
    headers = {
        "Cache-Control": "private, no-store",
        "Content-Disposition": "inline",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }
    if html_page:
        headers["Content-Security-Policy"] = PREVIEW_CSP
    return Response(content=content, media_type=media_type, headers=headers)


async def _signed_preview_artifact(
    run_id: str,
    artifact_id: str,
    expires: int,
    signature: str,
    session: AsyncSession,
) -> ArtifactBlobRecord:
    if not validate_artifact_id(artifact_id) or not verify_artifact_signature(
        run_id, artifact_id, expires, signature
    ):
        raise HTTPException(status_code=403, detail="artifact preview link is invalid or expired")
    artifact = await session.get(ArtifactBlobRecord, artifact_id)
    if artifact is None or artifact.run_id != run_id:
        raise HTTPException(status_code=404, detail="artifact not found")
    if artifact.content_type != "application/zip":
        raise HTTPException(status_code=415, detail="only ZIP artifacts can be previewed")
    return artifact


def mission_artifact(
    metadata: dict[str, Any],
    run_id: str,
    *,
    preview_paths: Any = None,
) -> dict[str, Any] | None:
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
    if (
        descriptor["content_type"] == "application/zip"
        and html_preview_entrypoint(preview_paths) is not None
    ):
        descriptor["preview_url"] = artifact_preview_url(run_id, artifact)
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


@router.get(
    "/v1/omp/missions/{run_id}/artifacts/{artifact_id}/preview/{expires}/{signature}/",
    response_class=Response,
)
async def preview_artifact(
    run_id: str,
    artifact_id: str,
    expires: int,
    signature: str,
    session: AsyncSession = Depends(get_session),
) -> Response:
    artifact = await _signed_preview_artifact(
        run_id, artifact_id, expires, signature, session
    )
    archive, files = _preview_archive(artifact.content)
    try:
        entrypoint = html_preview_entrypoint(list(files))
    finally:
        archive.close()
    if entrypoint is None:
        return _preview_response(
            b"<!doctype html><meta charset=utf-8><title>Preview unavailable</title>"
            b"<p>This ZIP has no unambiguous HTML entry point. Use its download link.</p>",
            "text/html; charset=utf-8",
            html_page=True,
        )
    return RedirectResponse(quote(entrypoint, safe="/"), status_code=307)


@router.get(
    "/v1/omp/missions/{run_id}/artifacts/{artifact_id}/preview/{expires}/{signature}/{path:path}",
    response_class=Response,
)
async def preview_artifact_file(
    run_id: str,
    artifact_id: str,
    expires: int,
    signature: str,
    path: str,
    session: AsyncSession = Depends(get_session),
) -> Response:
    if not _safe_archive_path(path):
        raise HTTPException(status_code=404, detail="preview file not found")
    artifact = await _signed_preview_artifact(
        run_id, artifact_id, expires, signature, session
    )
    archive, files = _preview_archive(artifact.content)
    try:
        info = files.get(path)
        media_type = PREVIEW_MEDIA_TYPES.get(PurePosixPath(path).suffix.casefold())
        if info is None or media_type is None or info.flag_bits & 1:
            raise HTTPException(status_code=404, detail="preview file not found")
        try:
            with archive.open(info) as stream:
                content = stream.read(info.file_size + 1)
        except (OSError, RuntimeError, EOFError, zlib.error, zipfile.BadZipFile) as exc:
            raise HTTPException(status_code=422, detail="preview file is invalid") from exc
        if len(content) != info.file_size or len(content) > MAX_ARTIFACT_BYTES:
            raise HTTPException(status_code=422, detail="preview file size is invalid")
    finally:
        archive.close()
    return _preview_response(
        content,
        media_type,
        html_page=PurePosixPath(path).suffix.casefold() in HTML_SUFFIXES,
    )


@router.get(
    "/v1/omp/missions/{run_id}/artifacts/{artifact_id}/preview/{expires}/{signature}",
    include_in_schema=False,
)
async def redirect_preview_slash(
    run_id: str,
    artifact_id: str,
    expires: int,
    signature: str,
) -> RedirectResponse:
    return RedirectResponse(
        f"/v1/omp/missions/{run_id}/artifacts/{artifact_id}/preview/{expires}/{signature}/",
        status_code=307,
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
