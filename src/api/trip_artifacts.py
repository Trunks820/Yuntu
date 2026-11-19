"""Public API for v0.8.10.1 backend export artifacts."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from src.api.export_source import build_export_source
from src.api.public_guard import _request_ip, verify_public_api_client
from src.api.trip_results import ResultContractUnsupported
from src.config import get_settings
from src.jobs.export_store import (
    ARTIFACT_TYPES,
    ExportArtifactRecord,
    ExportQuotaExceededError,
    get_artifact,
    get_or_create_artifact,
    requeue_ready_artifact_if_file_missing_or_expired,
)

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(verify_public_api_client)])

_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
_FORBIDDEN_METADATA_KEYS = {
    "model_call_records",
    "storage_key",
    "local_path",
    "file_path",
    "absolute_path",
    "path",
}


class ArtifactError(BaseModel):
    code: str
    message: str


class ArtifactResponse(BaseModel):
    ok: bool
    artifact_id: str | None = None
    result_record_id: int
    artifact_type: str
    status: str | None = None
    download_url: str | None = None
    filename: str | None = None
    mime_type: str | None = None
    byte_size: int | None = None
    page_count: int | None = None
    width_px: int | None = None
    height_px: int | None = None
    expires_time: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    error: ArtifactError | None = None


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "ok": False,
            "error": {
                "code": code,
                "message": message,
            },
        },
    )


def _unsupported_artifact_type_response() -> JSONResponse:
    return _error_response(
        400,
        "UNSUPPORTED_ARTIFACT_TYPE",
        "不支持该导出类型",
    )


def _validate_artifact_type(artifact_type: str) -> bool:
    return artifact_type in ARTIFACT_TYPES


def _download_url(record: ExportArtifactRecord) -> str:
    return (
        f"/trip/results/{record.result_record_id}"
        f"/artifacts/{record.artifact_type}/download"
    )


def _public_filename(filename: str | None) -> str | None:
    text = str(filename or "").strip()
    if not text:
        return None
    if _metadata_value_is_path(text) or "/" in text or "\\" in text:
        text = text.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return text or None


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _is_expired(record: ExportArtifactRecord) -> bool:
    if record.expires_time is None:
        return False
    expires_time = record.expires_time
    if expires_time.tzinfo is None:
        expires_time = expires_time.replace(tzinfo=timezone.utc)
    return expires_time <= datetime.now(timezone.utc)


def _is_under_base(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
    except ValueError:
        return False
    return True


def _local_file_path(record: ExportArtifactRecord) -> Path | None:
    if record.storage_backend != "local" or not record.storage_key:
        return None

    base = Path(get_settings().export_storage_dir).expanduser().resolve()
    raw_key = Path(record.storage_key)
    if raw_key.is_absolute():
        return None
    candidate = base / raw_key
    if raw_key.parts and raw_key.parts[0] == base.name:
        candidate = base.parent / raw_key
    resolved = candidate.expanduser().resolve()
    if not _is_under_base(resolved, base):
        return None
    return resolved


def _metadata_value_is_path(value: str) -> bool:
    return (
        value.startswith("/")
        or value.startswith("\\")
        or bool(_WINDOWS_ABSOLUTE_PATH_RE.match(value))
    )


def _public_metadata(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, child in value.items():
            key_text = str(key)
            if key_text.lower() in _FORBIDDEN_METADATA_KEYS:
                continue
            cleaned_value = _public_metadata(child)
            if cleaned_value is not None:
                cleaned[key_text] = cleaned_value
        return cleaned
    if isinstance(value, list):
        return [
            cleaned
            for item in value
            if (cleaned := _public_metadata(item)) is not None
        ]
    if isinstance(value, str) and _metadata_value_is_path(value):
        return None
    return value


async def _record_for_public_response(
    record: ExportArtifactRecord,
) -> tuple[ExportArtifactRecord, Path | None]:
    file_path: Path | None = None
    if record.status != "ready":
        return record, file_path

    file_path = _local_file_path(record)
    missing = file_path is None or not file_path.is_file()
    if missing or _is_expired(record):
        requeued = await requeue_ready_artifact_if_file_missing_or_expired(
            record.artifact_id,
            file_missing=missing,
        )
        if requeued is not None:
            return requeued, None
        return record, None
    return record, file_path


def _artifact_response(
    record: ExportArtifactRecord,
    *,
    file_available: bool,
) -> ArtifactResponse:
    failed = record.status == "failed"
    return ArtifactResponse(
        ok=not failed,
        artifact_id=record.artifact_id,
        result_record_id=record.result_record_id,
        artifact_type=record.artifact_type,
        status=record.status,
        download_url=_download_url(record) if record.status == "ready" and file_available else None,
        filename=_public_filename(record.filename) if record.status == "ready" and file_available else None,
        mime_type=record.mime_type,
        byte_size=record.byte_size if record.status == "ready" and file_available else None,
        page_count=record.page_count if record.status == "ready" and file_available else None,
        width_px=record.width_px if record.status == "ready" and file_available else None,
        height_px=record.height_px if record.status == "ready" and file_available else None,
        expires_time=_iso(record.expires_time) if record.status == "ready" and file_available else None,
        metadata=_public_metadata(record.metadata or {}),
        error=(
            ArtifactError(
                code=record.error_code or "EXPORT_CREATE_FAILED",
                message=record.error_message or "导出失败，请稍后重试",
            )
            if failed
            else None
        ),
    )


async def _source_or_error(result_record_id: int, artifact_type: str):
    try:
        source = await build_export_source(result_record_id, artifact_type)
    except ResultContractUnsupported:
        return _error_response(
            422,
            "RESULT_CONTRACT_UNSUPPORTED",
            "该攻略由旧版本生成，暂不支持导出，请重新生成",
        )
    if source is None:
        return _error_response(
            404,
            "RESULT_NOT_FOUND",
            "攻略不存在",
        )
    return source


@router.post(
    "/trip/results/{result_record_id}/artifacts/{artifact_type}",
    response_model=ArtifactResponse,
)
async def create_trip_artifact(
    result_record_id: int,
    artifact_type: str,
    request: Request,
):
    if not _validate_artifact_type(artifact_type):
        return _unsupported_artifact_type_response()

    source = await _source_or_error(result_record_id, artifact_type)
    if isinstance(source, JSONResponse):
        return source

    try:
        record = await get_or_create_artifact(
            result_record_id,
            artifact_type,
            source.source_hash,
            source.export_version,
            _request_ip(request),
            metadata={
                "export_version": source.export_version,
                "source_schema_version": source.source["result"].get("schema_version"),
            },
            force_retry_failed=True,
        )
    except ExportQuotaExceededError:
        return _error_response(
            429,
            "EXPORT_RATE_LIMITED",
            "今天的导出次数已用完，请明天再试",
        )
    except Exception as exc:
        logger.exception(
            "create export artifact failed result_record_id=%s artifact_type=%s",
            result_record_id,
            artifact_type,
        )
        return _error_response(
            500,
            "EXPORT_CREATE_FAILED",
            "导出任务创建失败，请稍后重试",
        )

    public_record, file_path = await _record_for_public_response(record)
    return _artifact_response(public_record, file_available=file_path is not None)


@router.get(
    "/trip/results/{result_record_id}/artifacts/{artifact_type}",
    response_model=ArtifactResponse,
)
async def get_trip_artifact_status(
    result_record_id: int,
    artifact_type: str,
):
    if not _validate_artifact_type(artifact_type):
        return _unsupported_artifact_type_response()

    source = await _source_or_error(result_record_id, artifact_type)
    if isinstance(source, JSONResponse):
        return source

    record = await get_artifact(
        result_record_id,
        artifact_type,
        source_hash=source.source_hash,
        export_version=source.export_version,
    )
    if record is None:
        return _error_response(
            404,
            "EXPORT_ARTIFACT_NOT_FOUND",
            "导出任务不存在，请先创建导出",
        )

    public_record, file_path = await _record_for_public_response(record)
    return _artifact_response(public_record, file_available=file_path is not None)


@router.get("/trip/results/{result_record_id}/artifacts/{artifact_type}/download")
async def download_trip_artifact(
    result_record_id: int,
    artifact_type: str,
):
    if not _validate_artifact_type(artifact_type):
        return _unsupported_artifact_type_response()

    source = await _source_or_error(result_record_id, artifact_type)
    if isinstance(source, JSONResponse):
        return source

    record = await get_artifact(
        result_record_id,
        artifact_type,
        source_hash=source.source_hash,
        export_version=source.export_version,
    )
    if record is None:
        return _error_response(
            404,
            "EXPORT_ARTIFACT_NOT_FOUND",
            "导出任务不存在，请先创建导出",
        )

    public_record, file_path = await _record_for_public_response(record)
    if public_record.status != "ready" or file_path is None:
        return _artifact_response(public_record, file_available=False)

    return FileResponse(
        file_path,
        media_type=public_record.mime_type or "application/octet-stream",
        filename=_public_filename(public_record.filename) or file_path.name,
    )
