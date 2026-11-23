"""P4.4-H1 read-only service contract for yuntu-admin."""

from __future__ import annotations

import logging
import re
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse

from src.agents.role_relay_store import (
    CONTROLLED_ROLES,
    ENDPOINT_NAME_MAX,
    RecoverError,
    RecoverSuccess,
    PostgresRoleRelayStore,
    SafeEndpoint,
    normalize_role,
)
from src.agents.writer_relay_router import WRITER_MODEL, frozen_writer_pool
from src.agents.writer_relay_store import (
    ALLOWED_ENDPOINTS,
    PostgresWriterRelayStore,
)
from src.config import get_settings
from src.jobs.internal_admin_store import (
    ARTIFACT_STATUSES,
    ARTIFACT_TYPES,
    DETAILED_REASON_BY_ERROR_CODE,
    ERROR_CODE_BY_DETAILED_REASON,
    TRIP_JOB_STATUSES,
    TRIP_RESULT_TYPES,
    AdminArtifactRecord,
    artifact_business_status,
    get_admin_artifact,
    get_admin_failed_draft,
    get_admin_trip_job,
    list_admin_artifacts,
    list_admin_trip_jobs,
)
from src.jobs.projection_outbox import (
    get_trip_job_snapshot_page,
    get_trip_step_snapshot_page,
)
from src.jobs.internal_guide_store import (
    get_internal_guide_artifacts,
    get_internal_guide_source,
)
from src.jobs.trip_failed_draft import project_failed_draft_plans
from src.api.trip_results import ResultContractUnsupported, get_trip_result


logger = logging.getLogger(__name__)
CONTRACT_VERSION = "v1"
INTERNAL_ADMIN_PREFIX = "/internal/v1/admin"
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
_SAFE_ERROR_CODE_RE = re.compile(r"^[A-Z0-9_]{1,64}$")
_INTERNAL_RETRYABLE_CODES = frozenset(
    {
        "INTERNAL_ADMIN_NOT_CONFIGURED",
        "GUIDE_RESULT_INCONSISTENT",
        "INTERNAL_ADMIN_INTERNAL_ERROR",
        "INTERNAL_ADMIN_UNAVAILABLE",
        "LLM_ENDPOINT_RECOVERY_IN_PROGRESS",
        "LLM_ENDPOINT_BUSY",
    }
)
_LLM_ENDPOINT_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_LLM_ENDPOINT_MESSAGES = {
    "LLM_ENDPOINT_NOT_FOUND": "endpoint was not found",
    "LLM_ENDPOINT_DISABLED": "endpoint is disabled",
    "LLM_ENDPOINT_NOT_RECOVERABLE": "endpoint is not recoverable",
    "LLM_ENDPOINT_RECOVERY_IN_PROGRESS": "endpoint recovery is in progress",
    "LLM_ENDPOINT_BUSY": "endpoint is busy",
    "LLM_ENDPOINT_OPERATION_CONFLICT": "operation id conflict",
    "LLM_ENDPOINT_PROBE_FAILED": "endpoint probe failed",
    "LLM_ENDPOINT_PROBE_TIMEOUT": "endpoint probe timed out",
    "INTERNAL_ADMIN_UNAVAILABLE": "internal admin is unavailable",
}

_TRIP_SAFE_ERROR_MESSAGES = {
    "PUBLISH_GATE_FAILED": "攻略未通过发布校验",
    "SAFE_RENDER_FAILED": "基础行程渲染失败",
    "TIMEOUT": "规划超时",
    "LLM_ERROR": "规划服务暂时不可用",
    "DB_ERROR": "规划保存失败",
    "WORKFLOW_ERROR": "规划流程失败",
    "UNKNOWN": "规划失败",
    "CANCELLED": "规划已取消",
    "CITY_PREPARING": "城市数据准备中",
    "CITY_COLLECTION_FAILED": "城市数据采集失败",
    "CITY_DATA_INSUFFICIENT": "城市数据不足",
    "CITY_DISABLED": "城市暂不可用",
    "CITY_CLARIFICATION_REQUIRED": "需要补充目的地城市",
}


class InternalAdminError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        request_id: str,
        *,
        retry_after: str | None = None,
        retryable: bool | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.request_id = request_id
        self.retry_after = retry_after
        self.retryable = retryable
        super().__init__(code)


def _request_id_from_request(request: Request) -> str:
    existing = getattr(request.state, "internal_admin_request_id", None)
    if existing:
        return str(existing)
    supplied = (request.headers.get("X-Request-ID") or "").strip()
    request_id = supplied[:120] or f"iadm_{uuid.uuid4().hex}"
    request.state.internal_admin_request_id = request_id
    return request_id


def _error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    request_id: str,
    retry_after: str | None = None,
    retryable: bool | None = None,
) -> JSONResponse:
    payload: dict = {
        "ok": False,
        "contract_version": CONTRACT_VERSION,
        "request_id": request_id,
        "error": {
            "code": code,
            "message": message,
            "retryable": (
                bool(retryable)
                if retryable is not None
                else code in _INTERNAL_RETRYABLE_CODES
            ),
        },
    }
    if retry_after:
        payload["retry_after"] = retry_after
    return JSONResponse(
        status_code=status_code,
        content=payload,
        headers={
            "X-Request-ID": request_id,
            "Cache-Control": "no-store",
        },
    )

async def internal_admin_error_handler(
    _request: Request,
    exc: InternalAdminError,
) -> JSONResponse:
    return _error_response(
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        request_id=exc.request_id,
        retry_after=exc.retry_after,
        retryable=exc.retryable,
    )


async def verify_internal_admin_credential(request: Request) -> None:
    request_id = _request_id_from_request(request)
    expected = get_settings().yuntu_travel_bff_internal_admin_credential.strip()
    if not expected:
        raise InternalAdminError(
            503,
            "INTERNAL_ADMIN_NOT_CONFIGURED",
            "internal admin credential is not configured",
            request_id,
        )

    supplied = request.headers.get("X-Internal-Credential") or ""
    if not secrets.compare_digest(supplied, expected):
        raise InternalAdminError(
            401,
            "INTERNAL_ADMIN_UNAUTHORIZED",
            "internal admin credential is missing or invalid",
            request_id,
        )


router = APIRouter(
    prefix=INTERNAL_ADMIN_PREFIX,
    dependencies=[Depends(verify_internal_admin_credential)],
)


def _success_response(http_request: Request, **payload) -> JSONResponse:
    request_id = _request_id_from_request(http_request)
    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "contract_version": CONTRACT_VERSION,
            "request_id": request_id,
            **payload,
        },
        headers={
            "X-Request-ID": request_id,
            "Cache-Control": "no-store",
        },
    )


def _invalid(request: Request, message: str) -> InternalAdminError:
    return InternalAdminError(
        400,
        "INTERNAL_ADMIN_INVALID_ARGUMENT",
        message,
        _request_id_from_request(request),
    )


def _unexpected(request: Request) -> InternalAdminError:
    return InternalAdminError(
        500,
        "INTERNAL_ADMIN_INTERNAL_ERROR",
        "internal admin request failed",
        _request_id_from_request(request),
    )


def _query_int(
    request: Request,
    name: str,
    *,
    default: int | None = None,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int | None:
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise _invalid(request, f"{name} must be an integer") from exc
    if minimum is not None and value < minimum:
        raise _invalid(request, f"{name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise _invalid(request, f"{name} must be at most {maximum}")
    return value


def _query_time(request: Request, name: str) -> datetime | None:
    raw = request.query_params.get(name)
    if raw is None or not raw.strip():
        return None
    normalized = raw.strip()
    if normalized.endswith(("Z", "z")):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        value = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise _invalid(request, f"{name} must be an ISO-8601 timestamp") from exc
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _snapshot_query(request: Request) -> tuple[int, int | None, int]:
    after_id = _query_int(request, "after_id", default=0, minimum=0)
    if request.query_params.get("limit") in (None, ""):
        raise _invalid(request, "limit is required")
    limit = _query_int(request, "limit", minimum=1, maximum=1000)
    snapshot_max_id = _query_int(
        request,
        "snapshot_max_id",
        minimum=0,
    )
    assert after_id is not None and limit is not None
    if after_id == 0 and snapshot_max_id is not None:
        raise _invalid(request, "snapshot_max_id must be omitted on the first page")
    if after_id > 0 and snapshot_max_id is None:
        raise _invalid(request, "snapshot_max_id is required after the first page")
    if snapshot_max_id is not None and after_id > snapshot_max_id:
        raise _invalid(request, "after_id must not exceed snapshot_max_id")
    return after_id, snapshot_max_id, limit


def _query_enum(
    request: Request,
    name: str,
    allowed: frozenset[str],
    *,
    upper: bool = True,
) -> str | None:
    raw = request.query_params.get(name)
    if raw is None or not raw.strip():
        return None
    value = raw.strip().upper() if upper else raw.strip().lower()
    if value not in allowed:
        raise _invalid(request, f"{name} is not supported")
    return value


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _total_duration_ms(
    created_time: datetime,
    finished_time: datetime | None,
) -> int:
    start = created_time
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    end = finished_time or datetime.now(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return max(0, int((end - start).total_seconds() * 1000))


def _safe_trip_error(code: str | None) -> dict | None:
    if not code:
        return None
    safe_code = str(code)
    if safe_code not in _TRIP_SAFE_ERROR_MESSAGES:
        safe_code = "UNKNOWN"
    return {
        "code": safe_code,
        "message": _TRIP_SAFE_ERROR_MESSAGES[safe_code],
    }


def _safe_artifact_error(code: str | None) -> dict | None:
    if not code:
        return None
    safe_code = str(code)
    if not _SAFE_ERROR_CODE_RE.fullmatch(safe_code):
        safe_code = "EXPORT_FAILED"
    message = (
        "导出超时"
        if safe_code == "EXPORT_TIMEOUT"
        else "攻略不存在"
        if safe_code == "RESULT_NOT_FOUND"
        else "攻略版本不支持导出"
        if safe_code == "RESULT_CONTRACT_UNSUPPORTED"
        else "导出失败"
    )
    return {
        "code": safe_code,
        "message": message,
    }


def _public_filename(value: str | None) -> str | None:
    filename = str(value or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(
        character for character in filename if ord(character) >= 32
    ).strip()
    return cleaned or None


def _trip_projection(item: dict, *, include_steps: bool = False) -> dict:
    projected = {
        "job_id": str(item["job_id"]),
        "result_record_id": (
            str(item["result_record_id"])
            if item.get("result_record_id") is not None
            else None
        ),
        "status": item["status"],
        "current_stage": item["current_stage"],
        "city": item.get("city"),
        "result_type": item.get("result_type"),
        "safe_error": _safe_trip_error(item.get("error_code")),
        "detailed_reason": DETAILED_REASON_BY_ERROR_CODE.get(
            str(item.get("error_code") or "")
        ),
        "created_at": _iso(item["created_time"]),
        "started_at": _iso(item.get("started_time")),
        "finished_at": _iso(item.get("finished_time")),
        "total_duration_ms": _total_duration_ms(
            item["created_time"],
            item.get("finished_time"),
        ),
        "retry_count": int(item.get("retry_count") or 0),
        "failed_draft_available": bool(
            item.get("failed_draft_available")
        ),
    }
    if include_steps:
        projected["steps"] = [
            {
                "stage": step["stage"],
                "status": step["status"],
                "attempt": int(step["attempt"]),
                "publish_retry_round": int(step["publish_retry_round"]),
                "started_at": _iso(step.get("started_time")),
                "finished_at": _iso(step.get("finished_time")),
                "duration_ms": (
                    int(step["duration_ms"])
                    if step.get("duration_ms") is not None
                    else None
                ),
            }
            for step in item.get("steps", [])
        ]
    return projected


def _artifact_projection(item: dict) -> dict:
    status = str(item["status"]).upper()
    return {
        "artifact_id": str(item["artifact_id"]),
        "result_record_id": str(item["result_record_id"]),
        "artifact_type": item["artifact_type"],
        "status": status,
        "filename": _public_filename(item.get("filename")),
        "mime_type": item.get("mime_type"),
        "byte_size": item.get("byte_size"),
        "sha256": item.get("sha256"),
        "text_length": item.get("text_length"),
        "width_px": item.get("width_px"),
        "height_px": item.get("height_px"),
        "page_count": item.get("page_count"),
        "attempt_count": int(item.get("attempt_count") or 0),
        "safe_error": _safe_artifact_error(item.get("error_code")),
        "created_at": _iso(item["created_time"]),
        "started_at": _iso(item.get("started_time")),
        "finished_at": _iso(item.get("finished_time")),
        "expires_at": _iso(item.get("expires_time")),
    }


def _artifact_record_projection(record: AdminArtifactRecord) -> dict:
    status = artifact_business_status(record)
    item = {
        "artifact_id": record.artifact_id,
        "result_record_id": record.result_record_id,
        "artifact_type": record.artifact_type,
        "status": status,
        "filename": record.filename,
        "mime_type": record.mime_type,
        "byte_size": record.byte_size,
        "sha256": record.sha256,
        "text_length": record.text_length,
        "width_px": record.width_px,
        "height_px": record.height_px,
        "page_count": record.page_count,
        "attempt_count": record.attempt_count,
        "error_code": record.error_code,
        "created_time": record.created_time,
        "started_time": record.started_time,
        "finished_time": record.finished_time,
        "expires_time": record.expires_time,
    }
    return _artifact_projection(item)


def _is_under_base(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
    except ValueError:
        return False
    return True


def _local_artifact_path(record: AdminArtifactRecord) -> Path | None:
    if record.storage_backend != "local" or not record.storage_key:
        return None
    storage_key = str(record.storage_key)
    if _WINDOWS_ABSOLUTE_PATH_RE.match(storage_key):
        return None
    raw_key = Path(storage_key)
    if raw_key.is_absolute():
        return None
    base = Path(get_settings().export_storage_dir).expanduser().resolve()
    candidate = base / raw_key
    if raw_key.parts and raw_key.parts[0] == base.name:
        candidate = base.parent / raw_key
    resolved = candidate.expanduser().resolve()
    if not _is_under_base(resolved, base) or not resolved.is_file():
        return None
    return resolved


def _download_filename(record: AdminArtifactRecord, path: Path) -> str:
    extension = ".pdf" if record.artifact_type == "pdf" else ".png"
    fallback = f"artifact-{record.artifact_id}{extension}"
    raw = Path(str(record.filename or "")).name.strip()
    if not raw:
        raw = path.name or fallback
    cleaned = "".join(
        character
        for character in raw
        if ord(character) >= 32 and character not in {'"', "\\", "/"}
    ).strip()
    return cleaned or fallback


def _content_disposition(filename: str) -> str:
    ascii_name = filename.encode("ascii", "ignore").decode("ascii").strip()
    ascii_name = ascii_name or "artifact"
    return (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(filename, safe='')}"
    )


@router.get("/projection/trip-jobs")
async def get_trip_job_snapshot_page_endpoint(request: Request):
    after_id, snapshot_max_id, limit = _snapshot_query(request)
    try:
        page = await get_trip_job_snapshot_page(
            after_id=after_id,
            snapshot_max_id=snapshot_max_id,
            limit=limit,
        )
    except Exception:
        logger.exception("internal admin trip-job snapshot failed")
        raise _unexpected(request)
    return _success_response(request, **page)


@router.get("/projection/metrics")
async def get_projection_source_metrics_endpoint(request: Request):
    publisher = getattr(request.app.state, "projection_publisher", None)
    if publisher is None:
        raise _unexpected(request)
    try:
        metrics = await publisher.metrics()
    except Exception:
        logger.exception("internal admin projection metrics failed")
        raise _unexpected(request)
    settings = get_settings()
    return _success_response(
        request,
        publisher_enabled=bool(settings.projection_publisher_enabled),
        runtime_policy={
            "trip_job_timeout_seconds": settings.trip_job_timeout_seconds,
            "trip_job_long_timeout_seconds": settings.trip_job_long_timeout_seconds,
            "stale_sweep_interval_seconds": (
                settings.trip_stale_sweep_interval_seconds
            ),
            "projection_heartbeat_interval_seconds": (
                settings.projection_heartbeat_interval_seconds
            ),
        },
        metrics=metrics,
    )


@router.get("/projection/trip-steps")
async def get_trip_step_snapshot_page_endpoint(request: Request):
    after_id, snapshot_max_id, limit = _snapshot_query(request)
    try:
        page = await get_trip_step_snapshot_page(
            after_id=after_id,
            snapshot_max_id=snapshot_max_id,
            limit=limit,
        )
    except Exception:
        logger.exception("internal admin trip-step snapshot failed")
        raise _unexpected(request)
    return _success_response(request, **page)


@router.get("/trip-jobs")
async def list_trip_jobs_endpoint(request: Request):
    page = _query_int(request, "page", default=1, minimum=1)
    limit = _query_int(request, "limit", default=20, minimum=1, maximum=100)
    time_from = _query_time(request, "time_from")
    time_to = _query_time(request, "time_to")
    if time_from is not None and time_to is not None and time_from > time_to:
        raise _invalid(request, "time_from must not be after time_to")
    city_raw = request.query_params.get("city")
    city = city_raw.strip() if city_raw and city_raw.strip() else None
    status = _query_enum(request, "status", TRIP_JOB_STATUSES)
    result_type = _query_enum(request, "result_type", TRIP_RESULT_TYPES)
    error_code_raw = request.query_params.get("error_code")
    error_code = (
        error_code_raw.strip().upper()
        if error_code_raw and error_code_raw.strip()
        else None
    )
    detailed_reason_raw = request.query_params.get("detailed_reason")
    detailed_reason = (
        detailed_reason_raw.strip().lower()
        if detailed_reason_raw and detailed_reason_raw.strip()
        else None
    )
    if (
        detailed_reason is not None
        and detailed_reason not in ERROR_CODE_BY_DETAILED_REASON
    ):
        raise _invalid(request, "detailed_reason is not supported")
    try:
        total, items = await list_admin_trip_jobs(
            time_from=time_from,
            time_to=time_to,
            city=city,
            status=status,
            result_type=result_type,
            error_code=error_code,
            detailed_reason=detailed_reason,
            page=int(page),
            limit=int(limit),
        )
    except InternalAdminError:
        raise
    except Exception:
        logger.exception("internal admin trip-job list failed")
        raise _unexpected(request)
    return _success_response(
        request,
        page=page,
        limit=limit,
        total=total,
        items=[_trip_projection(item) for item in items],
    )


@router.get("/trip-jobs/{job_id}")
async def get_trip_job_endpoint(job_id: str, request: Request):
    try:
        item = await get_admin_trip_job(job_id)
    except Exception:
        logger.exception("internal admin trip-job detail failed")
        raise _unexpected(request)
    if item is None:
        raise InternalAdminError(
            404,
            "TRIP_JOB_NOT_FOUND",
            "trip job was not found",
            _request_id_from_request(request),
        )
    return _success_response(
        request,
        trip_job=_trip_projection(item, include_steps=True),
    )


@router.get("/trip-jobs/{job_id}/guide-result")
async def get_trip_job_guide_result_endpoint(job_id: str, request: Request):
    try:
        source = await get_internal_guide_source(job_id)
    except Exception:
        logger.exception("internal admin guide source lookup failed")
        raise _unexpected(request)
    if source is None:
        raise InternalAdminError(
            404,
            "TRIP_JOB_NOT_FOUND",
            "trip job was not found",
            _request_id_from_request(request),
        )
    if (
        source["guide_result_state"] != source["computed_guide_result_state"]
        or source["guide_result_state"] == "INCONSISTENT"
    ):
        raise InternalAdminError(
            500,
            "GUIDE_RESULT_INCONSISTENT",
            "guide result state is inconsistent",
            _request_id_from_request(request),
        )
    if source["guide_result_state"] != "AVAILABLE":
        raise InternalAdminError(
            409,
            "GUIDE_NOT_AVAILABLE",
            "guide result is not available",
            _request_id_from_request(request),
        )
    result_record_id = source["result_record_id"]
    if (
        source["status"] != "SUCCESS"
        or source["result_type"] != "PLAN_READY"
        or not isinstance(result_record_id, int)
        or result_record_id < 1
    ):
        raise InternalAdminError(
            500,
            "GUIDE_RESULT_INCONSISTENT",
            "guide result identity is inconsistent",
            _request_id_from_request(request),
        )
    try:
        final_guide = await get_trip_result(result_record_id, job_id=job_id)
        artifacts = await get_internal_guide_artifacts(result_record_id)
    except ResultContractUnsupported:
        raise InternalAdminError(
            500,
            "GUIDE_RESULT_INCONSISTENT",
            "canonical guide does not satisfy the result contract",
            _request_id_from_request(request),
        ) from None
    except Exception:
        logger.exception("internal admin canonical guide lookup failed")
        raise _unexpected(request)
    if final_guide is None or int(final_guide.result_id) != result_record_id:
        raise InternalAdminError(
            500,
            "GUIDE_RESULT_INCONSISTENT",
            "canonical guide record is missing",
            _request_id_from_request(request),
        )
    return _success_response(
        request,
        job_id=job_id,
        guide_result_state="AVAILABLE",
        result_type="PLAN_READY",
        result_record_id=result_record_id,
        request=source["request"],
        final_guide=final_guide.model_dump(mode="json", exclude_none=True),
        artifacts=artifacts,
    )


@router.get("/trip-jobs/{job_id}/failed-draft")
async def get_failed_draft_endpoint(job_id: str, request: Request):
    try:
        draft = await get_admin_failed_draft(job_id)
    except Exception:
        logger.exception("internal admin failed-draft detail failed")
        raise _unexpected(request)
    if draft is None or not draft.get("plans"):
        raise InternalAdminError(
            404,
            "FAILED_DRAFT_NOT_FOUND",
            "failed draft was not found",
            _request_id_from_request(request),
        )
    plans = project_failed_draft_plans(draft["plans"])
    if not plans:
        raise InternalAdminError(
            404,
            "FAILED_DRAFT_NOT_FOUND",
            "failed draft was not found",
            _request_id_from_request(request),
        )
    return _success_response(
        request,
        failed_draft={
            "job_id": str(draft["job_id"]),
            "created_at": _iso(draft["created_time"]),
            "plans": plans,
        },
    )


@router.get("/artifacts")
async def list_artifacts_endpoint(request: Request):
    page = _query_int(request, "page", default=1, minimum=1)
    limit = _query_int(request, "limit", default=20, minimum=1, maximum=100)
    time_from = _query_time(request, "time_from")
    time_to = _query_time(request, "time_to")
    if time_from is not None and time_to is not None and time_from > time_to:
        raise _invalid(request, "time_from must not be after time_to")
    artifact_type = _query_enum(
        request,
        "artifact_type",
        ARTIFACT_TYPES,
        upper=False,
    )
    status = _query_enum(request, "status", ARTIFACT_STATUSES)
    result_record_id = _query_int(
        request,
        "result_record_id",
        minimum=1,
    )
    try:
        total, items = await list_admin_artifacts(
            time_from=time_from,
            time_to=time_to,
            artifact_type=artifact_type,
            status=status,
            result_record_id=result_record_id,
            page=int(page),
            limit=int(limit),
        )
    except InternalAdminError:
        raise
    except Exception:
        logger.exception("internal admin artifact list failed")
        raise _unexpected(request)
    return _success_response(
        request,
        page=page,
        limit=limit,
        total=total,
        items=[_artifact_projection(item) for item in items],
    )


@router.get("/artifacts/{artifact_id}")
async def get_artifact_endpoint(artifact_id: str, request: Request):
    try:
        record = await get_admin_artifact(artifact_id)
    except Exception:
        logger.exception("internal admin artifact detail failed")
        raise _unexpected(request)
    if record is None:
        raise InternalAdminError(
            404,
            "ARTIFACT_NOT_FOUND",
            "artifact was not found",
            _request_id_from_request(request),
        )
    return _success_response(
        request,
        artifact=_artifact_record_projection(record),
    )


@router.get("/artifacts/{artifact_id}/download")
async def download_artifact_endpoint(artifact_id: str, request: Request):
    request_id = _request_id_from_request(request)
    try:
        record = await get_admin_artifact(artifact_id)
    except Exception:
        logger.exception("internal admin artifact download lookup failed")
        raise _unexpected(request)
    if record is None:
        raise InternalAdminError(
            404,
            "ARTIFACT_NOT_FOUND",
            "artifact was not found",
            request_id,
        )

    status = artifact_business_status(record)
    if status == "EXPIRED":
        raise InternalAdminError(
            410,
            "ARTIFACT_EXPIRED",
            "artifact has expired",
            request_id,
        )
    if status != "READY":
        raise InternalAdminError(
            409,
            "ARTIFACT_NOT_READY",
            "artifact is not ready",
            request_id,
        )

    path = _local_artifact_path(record)
    if path is None:
        raise InternalAdminError(
            409,
            "ARTIFACT_FILE_MISSING",
            "artifact file is missing",
            request_id,
        )

    try:
        stat_result = path.stat()
    except OSError as exc:
        raise InternalAdminError(
            409,
            "ARTIFACT_FILE_MISSING",
            "artifact file is missing",
            request_id,
        ) from exc
    filename = _download_filename(record, path)
    media_type = (
        "application/pdf"
        if record.artifact_type == "pdf"
        else "image/png"
    )
    return FileResponse(
        path,
        media_type=media_type,
        stat_result=stat_result,
        headers={
            "Content-Length": str(stat_result.st_size),
            "Content-Disposition": _content_disposition(filename),
            "Cache-Control": "no-store",
            "X-Request-ID": request_id,
        },
    )


_writer_runtime_store: PostgresWriterRelayStore | None = None
_role_runtime_store: PostgresRoleRelayStore | None = None


def _writer_runtime() -> PostgresWriterRelayStore:
    global _writer_runtime_store
    if _writer_runtime_store is None:
        _writer_runtime_store = PostgresWriterRelayStore()
    return _writer_runtime_store


def _role_runtime() -> PostgresRoleRelayStore:
    global _role_runtime_store
    if _role_runtime_store is None:
        _role_runtime_store = PostgresRoleRelayStore()
    return _role_runtime_store


def _llm_error(
    request: Request,
    code: str,
    *,
    retry_after: str | None = None,
) -> InternalAdminError:
    status = {
        "LLM_ENDPOINT_NOT_FOUND": 404,
        "LLM_ENDPOINT_PROBE_FAILED": 502,
        "LLM_ENDPOINT_PROBE_TIMEOUT": 504,
        "INTERNAL_ADMIN_UNAVAILABLE": 503,
    }.get(code, 409)
    return InternalAdminError(
        status,
        code,
        _LLM_ENDPOINT_MESSAGES.get(code, "endpoint request failed"),
        _request_id_from_request(request),
        retry_after=retry_after,
    )


def _role_config_or_none(role: str):
    from src.agents import llm

    try:
        return llm.resolve_role_config(role)
    except ValueError:
        return None


def _writer_models() -> dict[str, str]:
    config = _role_config_or_none("writer")
    if config is None:
        return {}
    try:
        endpoints, _fingerprints = frozen_writer_pool(config)
    except ValueError:
        return {
            endpoint.name: str(endpoint.model or config.model)
            for endpoint in config.relay_pool
            if endpoint.name in ALLOWED_ENDPOINTS
        }
    return {
        name: str(endpoint.model or WRITER_MODEL)
        for name, endpoint in endpoints.items()
    }


def _direct_endpoints(role: str):
    """Return configured v0.12 endpoints without exposing credentials in projections."""
    from src.agents.llm import RelayEndpoint, RoleConfig

    if role not in {"selector", "review"}:
        return {}
    settings = get_settings()
    value = lambda name, default="": getattr(settings, name, default)
    if role == "selector":
        # The selector's two legs come from src/model_routes.py. The runtime
        # endpoint is built the same way the order path builds it, so listing
        # or probing never changes the endpoint's recorded identity.
        from src.agents.generation_model_routing import table_endpoint

        result = {}
        for slot in ("primary", "backup"):
            runtime = table_endpoint(settings, agent="selector", slot=slot)
            if not runtime.base_url or not runtime.api_key:
                continue
            wire = "openai_responses" if runtime.wire_api == "responses" else "openai_chat"
            endpoint = RelayEndpoint(name=runtime.endpoint_id, base_url=runtime.base_url,
                                     api_key=runtime.api_key, wire_api=wire, model=runtime.model)
            config = RoleConfig(provider=runtime.provider, model=runtime.model,
                                api_key=runtime.api_key, base_url=runtime.base_url, wire_api=wire)
            result[runtime.endpoint_id] = (
                endpoint, config, runtime.provider, runtime.failure_domain, runtime)
        return result
    specs = {
        "review": (
            ("review-glm", "command", value("v012_review_glm_base_url"),
             value("v012_review_glm_api_key"), value("v012_review_glm_model"),
             value("v012_command_failure_domain")),
            ("review-luna", "command", value("v012_review_luna_base_url"),
             value("v012_review_luna_api_key"), value("v012_review_luna_model"),
             value("v012_command_failure_domain")),
            ("review-official-ds", "deepseek", value("v012_official_ds_base_url"),
             value("v012_official_ds_api_key"), value("v012_official_ds_model"),
             value("v012_official_ds_failure_domain")),
        ),
    }
    result = {}
    for name, provider, url, key, model, domain in specs.get(role, ()):
        if not url or not key:
            continue
        endpoint = RelayEndpoint(name=name, base_url=url, api_key=key,
                                 wire_api="openai_chat", model=model)
        config = RoleConfig(provider=provider, model=model, api_key=key,
                            base_url=url, wire_api="openai_chat")
        result[name] = (endpoint, config, provider, domain, None)
    return result


def _endpoint_lookup(role: str, endpoint_name: str):
    from src.agents import llm

    direct = _direct_endpoints(role)
    for name, values in direct.items():
        if name.lower() == endpoint_name.lower():
            return values[0], values[1]
    config = _role_config_or_none(role)
    if config is None:
        return None, None
    if role == "writer":
        try:
            endpoints, _fingerprints = frozen_writer_pool(config)
        except ValueError:
            endpoints = {
                endpoint.name: endpoint for endpoint in config.relay_pool
            }
        match = endpoints.get(endpoint_name) or endpoints.get(endpoint_name.lower())
        if match is None:
            for candidate in endpoints.values():
                if candidate.name.lower() == endpoint_name.lower():
                    match = candidate
                    break
        if match is None:
            return None, config
        return match, config
    for endpoint in config.relay_pool:
        if endpoint.name == endpoint_name or endpoint.name.lower() == endpoint_name.lower():
            return endpoint, config
    return None, config


async def _list_llm_endpoints() -> list[dict]:
    items: list[SafeEndpoint] = []
    writer_models = _writer_models()
    if writer_models:
        config = _role_config_or_none("writer")
        if config is not None:
            try:
                _endpoints, fingerprints = frozen_writer_pool(config)
                await _writer_runtime().synchronize_endpoints(fingerprints)
            except ValueError:
                pass
        items.extend(await _writer_runtime().list_safe_projections(writer_models))
    # v0.12 current-operation inventory contains only active Selector/Review
    # routing inputs. Persisted legacy pool rows remain available as history.
    for role in ("review", "selector"):
        from src.agents.generation_model_routing import (
            ModelEndpoint, endpoint_runtime_fingerprint,
        )
        direct = _direct_endpoints(role)
        if not direct:
            continue
        for name, (_endpoint, _config, provider, domain, runtime) in direct.items():
            runtime_endpoint = runtime or ModelEndpoint(
                provider=provider, model=_endpoint.model,
                base_url=_endpoint.base_url, api_key=_endpoint.api_key,
                failure_domain=domain, endpoint_id=name,
            )
            await _role_runtime().synchronize_direct(
                role, name, provider=provider, failure_domain=domain,
                fingerprint=endpoint_runtime_fingerprint(runtime_endpoint, role=role),
            )
        current_models = {name: value[0].model for name, value in direct.items()}
        items.extend(await _role_runtime().list_projections(role, current_models))
    return [item.to_api() for item in items]


@router.get("/llm-endpoints")
async def list_llm_endpoints_endpoint(request: Request):
    if request.query_params:
        raise _invalid(request, "this route accepts no query parameters")
    try:
        items = await _list_llm_endpoints()
    except InternalAdminError:
        raise
    except Exception as exc:
        logger.warning("llm_endpoint_list_failed error_type=%s", type(exc).__name__)
        raise InternalAdminError(
            503,
            "INTERNAL_ADMIN_UNAVAILABLE",
            "internal admin is unavailable",
            _request_id_from_request(request),
        )
    return _success_response(request, items=items)


@router.post("/llm-endpoints/{role}/{endpoint_name}/recover")
async def recover_llm_endpoint_endpoint(
    role: str,
    endpoint_name: str,
    request: Request,
):
    from src.agents import llm

    if request.query_params:
        raise _invalid(request, "this route accepts no query parameters")
    normalized_role = normalize_role(role)
    if normalized_role not in CONTROLLED_ROLES:
        raise _llm_error(request, "LLM_ENDPOINT_NOT_FOUND")
    if (
        len(endpoint_name) > ENDPOINT_NAME_MAX
        or not _LLM_ENDPOINT_NAME_RE.fullmatch(endpoint_name or "")
    ):
        raise _invalid(request, "endpoint_name is not supported")
    try:
        body = await request.json()
    except Exception:
        raise _invalid(request, "request body must be a JSON object") from None
    if not isinstance(body, dict) or set(body.keys()) != {"operation_id"}:
        raise _invalid(request, "request body must contain only operation_id")
    try:
        operation_id = uuid.UUID(str(body["operation_id"]))
    except (TypeError, ValueError):
        raise _invalid(request, "operation_id must be a UUID") from None

    endpoint, config = _endpoint_lookup(normalized_role, endpoint_name)
    if endpoint is None or config is None:
        try:
            replayed = await _role_runtime().remember_closed_rejection(
                operation_id,
                normalized_role,
                endpoint_name,
                "LLM_ENDPOINT_NOT_FOUND",
            )
        except RecoverError as exc:
            raise InternalAdminError(
                exc.http_status,
                exc.code,
                exc.message,
                _request_id_from_request(request),
                retry_after=_iso(exc.retry_after) if exc.retry_after else None,
            ) from None
        except Exception as exc:
            logger.warning(
                "llm_endpoint_reject_failed role=%s error_type=%s",
                normalized_role,
                type(exc).__name__,
            )
            raise _llm_error(request, "INTERNAL_ADMIN_UNAVAILABLE") from None
        if replayed is not None:
            return _success_response(request, endpoint=replayed.endpoint.to_api())
        raise _llm_error(request, "LLM_ENDPOINT_NOT_FOUND")
    canonical_name = endpoint.name
    owner = f"recover:{operation_id}"
    try:
        if normalized_role == "writer":
            _endpoints, fingerprints = frozen_writer_pool(config)
            await _writer_runtime().synchronize_endpoints(fingerprints)
            status, generation = await _writer_runtime().claim_manual_recover(
                canonical_name,
                operation_id=operation_id,
                owner=owner,
            )
            if status == "REPLAY":
                return await _replay_recover_response(request, operation_id)
            if status != "CLAIMED" or generation is None:
                return await _replay_recover_response(request, operation_id)
            success, failure_class = await llm.execute_recovery_probe(
                normalized_role, endpoint, config
            )
            result = await _writer_runtime().finish_manual_recover(
                canonical_name,
                operation_id=operation_id,
                owner=owner,
                generation=generation,
                success=success,
                failure_class=failure_class,
                model=str(endpoint.model or config.model or WRITER_MODEL),
            )
        else:
            direct_values = _direct_endpoints(normalized_role).get(canonical_name)
            if direct_values is not None:
                from src.agents.generation_model_routing import (
                    ModelEndpoint, endpoint_runtime_fingerprint,
                )
                _direct_endpoint, _direct_config, provider, domain, runtime = direct_values
                runtime_endpoint = runtime or ModelEndpoint(
                    provider=provider, model=_direct_endpoint.model,
                    base_url=_direct_endpoint.base_url, api_key=_direct_endpoint.api_key,
                    failure_domain=domain, endpoint_id=canonical_name,
                )
                await _role_runtime().synchronize_direct(
                    normalized_role, canonical_name, provider=provider,
                    failure_domain=domain,
                    fingerprint=endpoint_runtime_fingerprint(
                        runtime_endpoint, role=normalized_role
                    ),
                )
            fingerprints = {
                item.name: llm.pooled_endpoint_fingerprint(
                    normalized_role, config.relay_profile, item, config.model
                )
                for item in config.relay_pool
            }
            await _role_runtime().synchronize(normalized_role, fingerprints)
            status, generation = await _role_runtime().claim_manual_probe(
                normalized_role,
                canonical_name,
                operation_id=operation_id,
                owner=owner,
            )
            if status == "REPLAY":
                return await _replay_recover_response(request, operation_id)
            if status != "CLAIMED" or generation is None:
                return await _replay_recover_response(request, operation_id)
            success, failure_class = await llm.execute_recovery_probe(
                normalized_role, endpoint, config
            )
            projection = SafeEndpoint(
                role=normalized_role,
                endpoint_id=canonical_name,
                display_name=canonical_name,
                model=str(endpoint.model or config.model),
                participation="ACTIVE",
                qualification="QUALIFIED",
                circuit_state="CLOSED",
                recoverable=False,
                cooldown_until=None,
                inflight=None,
                max_inflight=None,
                updated_at=datetime.now(timezone.utc),
            )
            result = await _role_runtime().finish_manual_probe(
                normalized_role,
                canonical_name,
                operation_id=operation_id,
                owner=owner,
                generation=generation,
                success=success,
                failure_class=failure_class,
                endpoint=projection,
            )
    except RecoverError as exc:
        raise InternalAdminError(
            exc.http_status,
            exc.code,
            exc.message,
            _request_id_from_request(request),
            retry_after=_iso(exc.retry_after) if exc.retry_after else None,
        ) from None
    except InternalAdminError:
        raise
    except Exception as exc:
        logger.warning(
            "llm_endpoint_recover_failed role=%s endpoint=%s error_type=%s",
            normalized_role,
            canonical_name,
            type(exc).__name__,
        )
        raise InternalAdminError(
            503,
            "INTERNAL_ADMIN_UNAVAILABLE",
            "internal admin is unavailable",
            _request_id_from_request(request),
        )
    if isinstance(result, RecoverSuccess):
        return _success_response(request, endpoint=result.endpoint.to_api())
    raise _llm_error(request, "INTERNAL_ADMIN_UNAVAILABLE")


def _recover_status_code(status: str) -> tuple[str, int, str]:
    mapping = {
        "NOT_FOUND": ("LLM_ENDPOINT_NOT_FOUND", 404, "endpoint was not found"),
        "DISABLED": ("LLM_ENDPOINT_DISABLED", 409, "endpoint is disabled"),
        "NOT_RECOVERABLE": (
            "LLM_ENDPOINT_NOT_RECOVERABLE",
            409,
            "endpoint is not recoverable",
        ),
        "BUSY_PROBE": (
            "LLM_ENDPOINT_RECOVERY_IN_PROGRESS",
            409,
            "endpoint recovery is in progress",
        ),
        "BUSY": ("LLM_ENDPOINT_BUSY", 409, "endpoint is busy"),
        "UNAVAILABLE": (
            "INTERNAL_ADMIN_UNAVAILABLE",
            503,
            "internal admin is unavailable",
        ),
    }
    return mapping.get(
        status,
        ("LLM_ENDPOINT_NOT_RECOVERABLE", 409, "endpoint is not recoverable"),
    )


async def _replay_recover_response(request: Request, operation_id: uuid.UUID):
    payload = await _role_runtime().load_operation_replay(operation_id)
    if payload is None:
        raise _llm_error(request, "LLM_ENDPOINT_RECOVERY_IN_PROGRESS")
    if payload.get("ok"):
        return _success_response(request, endpoint=payload["endpoint"])
    raise InternalAdminError(
        int(payload.get("http_status") or 409),
        str(payload.get("error_code") or "LLM_ENDPOINT_NOT_RECOVERABLE"),
        str(payload.get("message") or "endpoint is not recoverable"),
        _request_id_from_request(request),
        retry_after=payload.get("retry_after")
        if isinstance(payload.get("retry_after"), str)
        else None,
    )
