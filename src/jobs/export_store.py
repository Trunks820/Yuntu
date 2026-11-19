"""Persistence helpers for backend export artifact metadata and quota."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import text

from src.config import get_settings
from src.pipeline.db import get_session_factory

ARTIFACT_TYPES = frozenset({"pdf", "share_image"})
ARTIFACT_STATUSES = frozenset({"pending", "running", "ready", "failed"})
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


class UnsupportedArtifactTypeError(ValueError):
    """Raised when an artifact type is outside the v0.8.10.1 contract."""


class ExportQuotaExceededError(RuntimeError):
    """Raised when a client exceeds the DB-backed daily export quota."""

    def __init__(self, *, artifact_type: str, quota_day: date, limit: int) -> None:
        super().__init__(
            f"export quota exceeded for {artifact_type} on {quota_day} limit={limit}",
        )
        self.artifact_type = artifact_type
        self.quota_day = quota_day
        self.limit = limit


@dataclass(frozen=True)
class ExportArtifactRecord:
    id: int
    artifact_id: str
    result_record_id: int
    artifact_type: str
    status: str
    source_hash: str
    export_version: str
    storage_backend: str
    storage_key: str | None
    filename: str | None
    mime_type: str | None
    byte_size: int | None
    sha256: str | None
    text_length: int | None
    width_px: int | None
    height_px: int | None
    page_count: int | None
    metadata: dict
    attempt_count: int
    error_code: str | None
    error_message: str | None
    client_ip_hash: str | None
    created_time: datetime
    started_time: datetime | None
    finished_time: datetime | None
    expires_time: datetime | None
    updated_time: datetime


def _validate_artifact_type(artifact_type: str) -> str:
    normalized = (artifact_type or "").strip()
    if normalized not in ARTIFACT_TYPES:
        raise UnsupportedArtifactTypeError(
            f"unsupported artifact_type={artifact_type!r}",
        )
    return normalized


def _default_mime_type(artifact_type: str) -> str:
    if artifact_type == "pdf":
        return "application/pdf"
    if artifact_type == "share_image":
        return "image/png"
    raise UnsupportedArtifactTypeError(f"unsupported artifact_type={artifact_type!r}")


def _quota_limit_for_artifact_type(artifact_type: str) -> int:
    settings = get_settings()
    if artifact_type == "pdf":
        return settings.export_pdf_daily_ip_limit
    if artifact_type == "share_image":
        return settings.export_share_image_daily_ip_limit
    raise UnsupportedArtifactTypeError(f"unsupported artifact_type={artifact_type!r}")


def compute_export_quota_day(now: datetime | None = None) -> date:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(SHANGHAI_TZ).date()


def hash_client_ip(client_ip: str, *, salt: str = "") -> str:
    normalized = (client_ip or "").strip()
    payload = f"{salt}:{normalized}" if salt else normalized
    payload = payload.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _new_artifact_id() -> str:
    return f"exp_{uuid.uuid4().hex[:24]}"


def _json_metadata(metadata: Mapping | None) -> str:
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, Mapping):
        raise ValueError("metadata must be a JSON object")
    _reject_binary_metadata(metadata)
    try:
        return json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
    except TypeError as exc:
        raise ValueError("metadata must be JSON serializable") from exc


def _reject_binary_metadata(value) -> None:
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise ValueError("metadata must not contain raw binary")
    if isinstance(value, Mapping):
        for child in value.values():
            _reject_binary_metadata(child)
        return
    if isinstance(value, list):
        for child in value:
            _reject_binary_metadata(child)


def _row_to_record(row) -> ExportArtifactRecord:
    data = row._mapping
    metadata = data["metadata"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata) if metadata else {}
    if metadata is None:
        metadata = {}
    return ExportArtifactRecord(
        id=int(data["id"]),
        artifact_id=data["artifact_id"],
        result_record_id=int(data["result_record_id"]),
        artifact_type=data["artifact_type"],
        status=data["status"],
        source_hash=data["source_hash"],
        export_version=data["export_version"],
        storage_backend=data["storage_backend"],
        storage_key=data["storage_key"],
        filename=data["filename"],
        mime_type=data["mime_type"],
        byte_size=data["byte_size"],
        sha256=data["sha256"],
        text_length=data["text_length"],
        width_px=data["width_px"],
        height_px=data["height_px"],
        page_count=data["page_count"],
        metadata=dict(metadata),
        attempt_count=int(data["attempt_count"]),
        error_code=data["error_code"],
        error_message=data["error_message"],
        client_ip_hash=data["client_ip_hash"],
        created_time=data["created_time"],
        started_time=data["started_time"],
        finished_time=data["finished_time"],
        expires_time=data["expires_time"],
        updated_time=data["updated_time"],
    )


async def _lock_artifact_identity(
    session,
    *,
    result_record_id: int,
    artifact_type: str,
    source_hash: str,
    export_version: str,
) -> None:
    lock_key = json.dumps(
        [int(result_record_id), artifact_type, source_hash, export_version],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": lock_key},
    )


async def _consume_export_quota_in_session(
    session,
    *,
    artifact_type: str,
    client_ip_hash: str,
    quota_day: date,
    limit: int,
) -> int:
    if limit <= 0:
        raise ExportQuotaExceededError(
            artifact_type=artifact_type,
            quota_day=quota_day,
            limit=limit,
        )
    result = await session.execute(
        text("""
            INSERT INTO travel_export_quota (
                quota_day, client_ip_hash, artifact_type, generate_count
            )
            VALUES (
                :quota_day, :client_ip_hash, :artifact_type, 1
            )
            ON CONFLICT (quota_day, client_ip_hash, artifact_type)
            DO UPDATE SET generate_count = travel_export_quota.generate_count + 1
            WHERE travel_export_quota.generate_count < :limit
            RETURNING generate_count
        """),
        {
            "quota_day": quota_day,
            "client_ip_hash": client_ip_hash,
            "artifact_type": artifact_type,
            "limit": limit,
        },
    )
    row = result.one_or_none()
    if row is None:
        raise ExportQuotaExceededError(
            artifact_type=artifact_type,
            quota_day=quota_day,
            limit=limit,
        )
    return int(row.generate_count)


async def consume_export_quota(
    artifact_type: str,
    client_ip: str | None = None,
    *,
    client_ip_hash: str | None = None,
    quota_day: date | None = None,
    limit: int | None = None,
    salt: str = "",
) -> int:
    artifact_type = _validate_artifact_type(artifact_type)
    hashed_ip = client_ip_hash or hash_client_ip(client_ip or "", salt=salt)
    effective_day = quota_day or compute_export_quota_day()
    effective_limit = (
        _quota_limit_for_artifact_type(artifact_type)
        if limit is None
        else int(limit)
    )
    factory = get_session_factory()
    async with factory() as session:
        count = await _consume_export_quota_in_session(
            session,
            artifact_type=artifact_type,
            client_ip_hash=hashed_ip,
            quota_day=effective_day,
            limit=effective_limit,
        )
        await session.commit()
        return count


async def get_or_create_artifact(
    result_record_id: int,
    artifact_type: str,
    source_hash: str,
    export_version: str,
    client_ip: str,
    metadata: Mapping | None = None,
    force_retry_failed: bool = False,
) -> ExportArtifactRecord:
    artifact_type = _validate_artifact_type(artifact_type)
    metadata_json = _json_metadata(metadata)
    client_ip_hash = hash_client_ip(client_ip)
    quota_day = compute_export_quota_day()
    quota_limit = _quota_limit_for_artifact_type(artifact_type)
    factory = get_session_factory()

    async with factory() as session:
        await _lock_artifact_identity(
            session,
            result_record_id=result_record_id,
            artifact_type=artifact_type,
            source_hash=source_hash,
            export_version=export_version,
        )
        existing = await session.execute(
            text("""
                SELECT *
                FROM travel_export_artifact
                WHERE result_record_id = :result_record_id
                  AND artifact_type = :artifact_type
                  AND source_hash = :source_hash
                  AND export_version = :export_version
                FOR UPDATE
            """),
            {
                "result_record_id": result_record_id,
                "artifact_type": artifact_type,
                "source_hash": source_hash,
                "export_version": export_version,
            },
        )
        row = existing.one_or_none()
        if row is not None:
            record = _row_to_record(row)
            if record.status in {"pending", "running", "ready"}:
                return record
            if record.status == "failed" and not force_retry_failed:
                return record

            await _consume_export_quota_in_session(
                session,
                artifact_type=artifact_type,
                client_ip_hash=client_ip_hash,
                quota_day=quota_day,
                limit=quota_limit,
            )
            retried = await session.execute(
                text("""
                    UPDATE travel_export_artifact
                    SET status = 'pending',
                        storage_backend = 'local',
                        storage_key = NULL,
                        filename = NULL,
                        mime_type = :mime_type,
                        byte_size = NULL,
                        sha256 = NULL,
                        text_length = NULL,
                        width_px = NULL,
                        height_px = NULL,
                        page_count = NULL,
                        metadata = CAST(:metadata AS jsonb)
                            || CASE WHEN metadata ? 'model_call_records'
                                THEN jsonb_build_object('model_call_records', metadata->'model_call_records')
                                ELSE '{}'::jsonb END,
                        attempt_count = 0,
                        error_code = NULL,
                        error_message = NULL,
                        client_ip_hash = :client_ip_hash,
                        started_time = NULL,
                        finished_time = NULL,
                        expires_time = NULL
                    WHERE id = :id
                    RETURNING *
                """),
                {
                    "id": record.id,
                    "mime_type": _default_mime_type(artifact_type),
                    "metadata": metadata_json,
                    "client_ip_hash": client_ip_hash,
                },
            )
            await session.commit()
            return _row_to_record(retried.one())

        await _consume_export_quota_in_session(
            session,
            artifact_type=artifact_type,
            client_ip_hash=client_ip_hash,
            quota_day=quota_day,
            limit=quota_limit,
        )
        created = await session.execute(
            text("""
                INSERT INTO travel_export_artifact (
                    artifact_id, result_record_id, artifact_type, status,
                    source_hash, export_version, storage_backend, mime_type,
                    metadata, client_ip_hash
                )
                VALUES (
                    :artifact_id, :result_record_id, :artifact_type, 'pending',
                    :source_hash, :export_version, 'local', :mime_type,
                    CAST(:metadata AS jsonb), :client_ip_hash
                )
                RETURNING *
            """),
            {
                "artifact_id": _new_artifact_id(),
                "result_record_id": result_record_id,
                "artifact_type": artifact_type,
                "source_hash": source_hash,
                "export_version": export_version,
                "mime_type": _default_mime_type(artifact_type),
                "metadata": metadata_json,
                "client_ip_hash": client_ip_hash,
            },
        )
        await session.commit()
        return _row_to_record(created.one())


async def get_artifact(
    result_record_id: int,
    artifact_type: str,
    source_hash: str | None = None,
    export_version: str | None = None,
) -> ExportArtifactRecord | None:
    artifact_type = _validate_artifact_type(artifact_type)
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            text("""
                SELECT *
                FROM travel_export_artifact
                WHERE result_record_id = :result_record_id
                  AND artifact_type = :artifact_type
                  AND (
                    CAST(:source_hash AS VARCHAR) IS NULL
                    OR source_hash = CAST(:source_hash AS VARCHAR)
                  )
                  AND (
                    CAST(:export_version AS VARCHAR) IS NULL
                    OR export_version = CAST(:export_version AS VARCHAR)
                  )
                ORDER BY created_time DESC, id DESC
                LIMIT 1
            """),
            {
                "result_record_id": result_record_id,
                "artifact_type": artifact_type,
                "source_hash": source_hash,
                "export_version": export_version,
            },
        )
        row = result.one_or_none()
        if row is None:
            return None
        return _row_to_record(row)


async def get_artifact_by_artifact_id(
    artifact_id: str,
) -> ExportArtifactRecord | None:
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            text("""
                SELECT *
                FROM travel_export_artifact
                WHERE artifact_id = :artifact_id
            """),
            {"artifact_id": artifact_id},
        )
        row = result.one_or_none()
        if row is None:
            return None
        return _row_to_record(row)


async def claim_next_pending_artifact(
    artifact_type: str,
    max_concurrency: int,
) -> ExportArtifactRecord | None:
    artifact_type = _validate_artifact_type(artifact_type)
    if max_concurrency <= 0:
        return None
    factory = get_session_factory()
    async with factory() as session:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"export-artifact-claim:{artifact_type}"},
        )
        running = await session.execute(
            text("""
                SELECT COUNT(*)::int
                FROM travel_export_artifact
                WHERE artifact_type = :artifact_type
                  AND status = 'running'
            """),
            {"artifact_type": artifact_type},
        )
        if int(running.scalar_one()) >= max_concurrency:
            return None

        picked = await session.execute(
            text("""
                SELECT id
                FROM travel_export_artifact
                WHERE artifact_type = :artifact_type
                  AND status = 'pending'
                ORDER BY created_time ASC, id ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            """),
            {"artifact_type": artifact_type},
        )
        row = picked.one_or_none()
        if row is None:
            return None
        updated = await session.execute(
            text("""
                UPDATE travel_export_artifact
                SET status = 'running',
                    started_time = NOW(),
                    finished_time = NULL,
                    attempt_count = attempt_count + 1,
                    error_code = NULL,
                    error_message = NULL
                WHERE id = :id
                RETURNING *
            """),
            {"id": row.id},
        )
        await session.commit()
        return _row_to_record(updated.one())


async def expire_stale_running_artifacts(
    artifact_type: str,
    *,
    timeout_seconds: int,
) -> int:
    artifact_type = _validate_artifact_type(artifact_type)
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            text("""
                UPDATE travel_export_artifact
                SET status = 'failed',
                    error_code = 'EXPORT_TIMEOUT',
                    error_message = '导出超时，请稍后重试',
                    finished_time = NOW()
                WHERE artifact_type = :artifact_type
                  AND status = 'running'
                  AND started_time IS NOT NULL
                  AND started_time < NOW() - make_interval(secs => :timeout_seconds)
                RETURNING artifact_id
            """),
            {
                "artifact_type": artifact_type,
                "timeout_seconds": timeout_seconds,
            },
        )
        rows = result.fetchall()
        await session.commit()
        return len(rows)


async def mark_artifact_running(artifact_id: str) -> ExportArtifactRecord | None:
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            text("""
                UPDATE travel_export_artifact
                SET status = 'running',
                    started_time = NOW(),
                    finished_time = NULL,
                    attempt_count = attempt_count + 1,
                    error_code = NULL,
                    error_message = NULL
                WHERE artifact_id = :artifact_id
                  AND status = 'pending'
                RETURNING *
            """),
            {"artifact_id": artifact_id},
        )
        row = result.one_or_none()
        await session.commit()
        if row is None:
            return None
        return _row_to_record(row)


async def list_expired_ready_local_artifacts(
    *,
    limit: int = 100,
    now: datetime | None = None,
) -> list[ExportArtifactRecord]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            text("""
                SELECT *
                FROM travel_export_artifact
                WHERE status = 'ready'
                  AND storage_backend = 'local'
                  AND storage_key IS NOT NULL
                  AND expires_time IS NOT NULL
                  AND expires_time <= :now
                ORDER BY expires_time ASC, id ASC
                LIMIT :limit
            """),
            {"now": current, "limit": max(1, int(limit))},
        )
        return [_row_to_record(row) for row in result]


async def mark_artifact_ready(
    artifact_id: str,
    *,
    storage_key: str,
    filename: str,
    mime_type: str,
    byte_size: int,
    sha256: str,
    storage_backend: str = "local",
    text_length: int | None = None,
    width_px: int | None = None,
    height_px: int | None = None,
    page_count: int | None = None,
    metadata: Mapping | None = None,
    expires_time: datetime | None = None,
) -> ExportArtifactRecord | None:
    metadata_json = None if metadata is None else _json_metadata(metadata)
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            text("""
                UPDATE travel_export_artifact
                SET status = 'ready',
                    storage_backend = :storage_backend,
                    storage_key = :storage_key,
                    filename = :filename,
                    mime_type = :mime_type,
                    byte_size = :byte_size,
                    sha256 = :sha256,
                    text_length = :text_length,
                    width_px = :width_px,
                    height_px = :height_px,
                    page_count = :page_count,
                    metadata = COALESCE(CAST(:metadata AS jsonb), metadata)
                        || CASE WHEN metadata ? 'model_call_records'
                            THEN jsonb_build_object('model_call_records', metadata->'model_call_records')
                            ELSE '{}'::jsonb END,
                    error_code = NULL,
                    error_message = NULL,
                    finished_time = NOW(),
                    expires_time = COALESCE(
                        CAST(:expires_time AS timestamptz),
                        NOW() + make_interval(days => :ttl_days)
                    )
                WHERE artifact_id = :artifact_id
                  AND status IN ('pending', 'running', 'ready')
                RETURNING *
            """),
            {
                "artifact_id": artifact_id,
                "storage_backend": storage_backend,
                "storage_key": storage_key,
                "filename": filename,
                "mime_type": mime_type,
                "byte_size": byte_size,
                "sha256": sha256,
                "text_length": text_length,
                "width_px": width_px,
                "height_px": height_px,
                "page_count": page_count,
                "metadata": metadata_json,
                "expires_time": expires_time,
                "ttl_days": get_settings().export_artifact_ttl_days,
            },
        )
        row = result.one_or_none()
        await session.commit()
        if row is None:
            return None
        return _row_to_record(row)


async def mark_artifact_failed(
    artifact_id: str,
    *,
    error_code: str,
    error_message: str,
) -> ExportArtifactRecord | None:
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            text("""
                UPDATE travel_export_artifact
                SET status = 'failed',
                    error_code = :error_code,
                    error_message = :error_message,
                    finished_time = NOW()
                WHERE artifact_id = :artifact_id
                  AND status IN ('pending', 'running', 'failed')
                RETURNING *
            """),
            {
                "artifact_id": artifact_id,
                "error_code": error_code[:50],
                "error_message": error_message[:2000],
            },
        )
        row = result.one_or_none()
        await session.commit()
        if row is None:
            return None
        return _row_to_record(row)


async def requeue_ready_artifact_if_file_missing_or_expired(
    artifact_id: str,
    *,
    file_missing: bool = False,
    now: datetime | None = None,
) -> ExportArtifactRecord | None:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            text("""
                UPDATE travel_export_artifact
                SET status = 'pending',
                    storage_key = NULL,
                    filename = NULL,
                    byte_size = NULL,
                    sha256 = NULL,
                    text_length = NULL,
                    width_px = NULL,
                    height_px = NULL,
                    page_count = NULL,
                    started_time = NULL,
                    finished_time = NULL,
                    expires_time = NULL,
                    error_code = NULL,
                    error_message = NULL
                WHERE artifact_id = :artifact_id
                  AND status = 'ready'
                  AND (
                    :file_missing
                    OR (expires_time IS NOT NULL AND expires_time <= :now)
                  )
                RETURNING *
            """),
            {
                "artifact_id": artifact_id,
                "file_missing": file_missing,
                "now": current,
            },
        )
        row = result.one_or_none()
        await session.commit()
        if row is None:
            return None
        return _row_to_record(row)
