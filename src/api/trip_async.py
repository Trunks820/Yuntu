"""Async trip job HTTP routes."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_serializer

from src.agents.schema import (
    DeliveryStatus,
    PublishedVariant,
    RequestedCommuteMode,
    TripRequest,
)
from src.api.public_guard import verify_public_api_client
from src.jobs import city_gate as city_gate_module
from src.jobs import trip_worker as trip_worker_module

from src.jobs.trip_store import (
    GenerationPolicyNotEnabledError,
    RequestIdConflictError,
    TERMINAL_STATUSES,
    TripJobRecord,
    compute_elapsed_ms,
    compute_queue_wait_ms,
    compute_run_elapsed_ms,
    compute_total_elapsed_ms,
    create_async_trip_job,
    get_trip_job_by_id,
    get_trip_result_delivery_metadata,
    queue_position_for_job,
    user_message_for_job,
    route_failure_code_for_job,
)
from src.jobs.generation_delivery import (
    GenerationDelivery,
    GenerationPolicyVersion,
    validate_delivery_for_job,
)
from src.config import get_settings

logger = logging.getLogger(__name__)

OWNER_TEST_SOURCE = "OWNER_TEST"

# The frozen v0.10.3 contract classifies OWNER_TEST as non-demand traffic. The
# City Gate algorithm and thresholds remain unchanged; only its existing source
# exclusion set is extended at the API composition boundary.
city_gate_module.NON_DEMAND_SOURCES = (
    city_gate_module.NON_DEMAND_SOURCES | {OWNER_TEST_SOURCE}
)
trip_worker_module.NON_DEMAND_SOURCES = (
    trip_worker_module.NON_DEMAND_SOURCES | {OWNER_TEST_SOURCE}
)

router = APIRouter(dependencies=[Depends(verify_public_api_client)])

SSE_KEEPALIVE_SECONDS = 15.0
SSE_POLL_INTERVAL_SECONDS = 2.0
GUIDE_REQUEST_FIELD_ALLOWLIST = frozenset(
    {
        "from_city",
        "to_city",
        "start_date",
        "end_date",
        "days",
        "people_count",
        "preferences",
        "avoid",
        "notes",
        "budget",
        "must_include",
        "commute_mode",
        "daily_start",
        "daily_end",
        "rest_windows",
        "accommodation",
    }
)


class TripAsyncStructuredRequest(BaseModel):
    """Typed request boundary without changing existing handler-owned errors."""

    model_config = ConfigDict(extra="allow")

    commute_mode: RequestedCommuteMode = "driving"


class TripAsyncCreateRequest(BaseModel):
    message: str | None = None
    trip_request: TripAsyncStructuredRequest | None = None
    request_id: str = Field(max_length=512)
    source: str = Field(default="web", max_length=30)
    conversation_id: str = Field(default="web-guest", max_length=200)
    user_display_name: str | None = Field(default=None, max_length=100)
    generation_policy_version: GenerationPolicyVersion | None = None


class TripAsyncCreateResponse(BaseModel):
    ok: bool = True
    job_id: str
    status: str
    current_stage: str
    queue_position: int
    message: str
    cached: bool
    generation_policy_version: GenerationPolicyVersion
    delivery: GenerationDelivery | None = None

    @model_serializer(mode="wrap")
    def serialize_delivery(self, handler):
        payload = handler(self)
        if self.delivery is None:
            payload.pop("delivery", None)
        return payload


def _validation_error_mentions_time_preferences(
    exc: ValidationError,
    raw_request: dict | None = None,
) -> bool:
    time_fields = {"daily_start", "daily_end", "rest_windows"}
    unprocessable_fields = {*time_fields, "commute_mode"}
    if isinstance(raw_request, dict) and any(
        field in raw_request for field in time_fields
    ):
        return True
    for error in exc.errors():
        loc = tuple(error.get("loc") or ())
        if loc and loc[0] in unprocessable_fields:
            return True
    return False


class TripJobStatusResponse(BaseModel):
    ok: bool
    job_id: str | None = None
    status: str | None = None
    current_stage: str | None = None
    message: str | None = None
    queue_position: int | None = None
    error_message: str | None = None
    error_code: str | None = None
    city_notice_code: str | None = None
    city_status: str | None = None
    city_batch_status: str | None = None
    city_batch_error_code: str | None = None
    trip_request: dict | None = None
    plan_count: int | None = None
    result_type: str | None = None
    result_record_id: int | None = None
    published_variant: PublishedVariant | None = None
    delivery_status: DeliveryStatus | None = None
    elapsed_ms: int | None = None
    queue_wait_ms: int | None = None
    run_elapsed_ms: int | None = None
    total_elapsed_ms: int | None = None
    created_time: str | None = None
    updated_time: str | None = None
    generation_policy_version: GenerationPolicyVersion
    delivery: GenerationDelivery | None = None

    @model_serializer(mode="wrap")
    def serialize_delivery(self, handler):
        payload = handler(self)
        if self.delivery is None:
            payload.pop("delivery", None)
        return payload


def _iso(dt) -> str | None:
    if dt is None:
        return None
    return dt.isoformat().replace("+00:00", "Z")


def _job_to_status_response(job: TripJobRecord) -> TripJobStatusResponse:
    is_terminal = job.status in TERMINAL_STATUSES
    show_trip_request = is_terminal and job.status in {"SUCCESS", "REJECTED"}
    trip_request_json = job.trip_request_json if isinstance(job.trip_request_json, dict) else {}

    return TripJobStatusResponse(
        ok=job.status not in {"FAILED", "TIMEOUT", "REJECTED"},
        job_id=job.job_id,
        status=job.status,
        current_stage=job.current_stage,
        message=user_message_for_job(job),
        queue_position=0 if job.status != "PENDING" else None,
        error_message=job.error_message if is_terminal and job.status != "SUCCESS" else None,
        error_code=job.error_code if is_terminal and job.status != "SUCCESS" else route_failure_code_for_job(job),
        city_notice_code=trip_request_json.get("city_notice_code"),
        city_status=trip_request_json.get("city_status"),
        city_batch_status=trip_request_json.get("city_batch_status"),
        city_batch_error_code=trip_request_json.get("city_batch_error_code"),
        trip_request=job.trip_request_json if show_trip_request else None,
        plan_count=job.plan_count if is_terminal and job.status == "SUCCESS" else None,
        result_type=job.result_type if is_terminal and job.status == "SUCCESS" else None,
        result_record_id=(
            job.result_record_id
            if is_terminal and job.status == "SUCCESS"
            else None
        ),
        elapsed_ms=compute_elapsed_ms(job),
        queue_wait_ms=compute_queue_wait_ms(job),
        run_elapsed_ms=compute_run_elapsed_ms(job),
        total_elapsed_ms=compute_total_elapsed_ms(job),
        created_time=_iso(job.created_time),
        updated_time=_iso(job.updated_time),
        generation_policy_version=getattr(job, "generation_policy_version", "legacy"),
        delivery=getattr(job, "delivery", None),
    )


async def _job_to_status_response_async(job: TripJobRecord) -> TripJobStatusResponse:
    response = _job_to_status_response(job)
    if job.status == "PENDING":
        response.queue_position = await queue_position_for_job(job)
    if job.status == "SUCCESS":
        delivery = await get_trip_result_delivery_metadata(
            job.result_record_id,
            job_id=job.job_id,
        )
        if delivery is not None:
            response.published_variant = delivery["published_variant"]
            response.delivery_status = delivery["delivery_status"]
    _validate_v012_job_delivery(
        job,
        published_variant=response.published_variant,
        delivery_status=response.delivery_status,
    )
    return response


def _validate_v012_job_delivery(
    job: TripJobRecord,
    *,
    published_variant: str | None = None,
    delivery_status: str | None = None,
) -> None:
    if getattr(job, "generation_policy_version", "legacy") != "v0.12":
        return
    delivery = getattr(job, "delivery", None)
    if delivery is None:
        raise ValueError("v0.12 job delivery is missing")
    validate_delivery_for_job(
        delivery,
        status=job.status,
        result_type=job.result_type,
        result_record_id=job.result_record_id,
        published_variant=published_variant,
        delivery_status=delivery_status,
    )


def _sse_event_for_job(job: TripJobRecord) -> str:
    if job.status == "SUCCESS":
        return "complete"
    if job.status in TERMINAL_STATUSES:
        return "failed"
    return "progress"


def _sse_status_for_progress(job: TripJobRecord) -> str:
    return "RUNNING" if job.status == "RUNNING" else job.status


def _sse_failure_code(job: TripJobRecord) -> str:
    if job.error_code:
        return job.error_code
    if job.status == "SUCCESS" and job.result_record_id is None:
        return "NO_RESULT_RECORD"
    return "GENERATION_FAILED"


def _sse_failure_message(job: TripJobRecord) -> str:
    if job.error_message:
        return job.error_message
    if job.status == "SUCCESS" and job.result_record_id is None:
        return "生成完成但没有可打开的结果记录"
    return user_message_for_job(job) or "生成失败"


async def _sse_payload_for_job(job: TripJobRecord) -> tuple[str, dict]:
    generation_policy_version = getattr(job, "generation_policy_version", "legacy")
    generation_delivery = getattr(job, "delivery", None)
    event = _sse_event_for_job(job)
    if event == "complete":
        delivery = await get_trip_result_delivery_metadata(
            job.result_record_id,
            job_id=job.job_id,
        )
        _validate_v012_job_delivery(
            job,
            published_variant=(delivery or {}).get("published_variant"),
            delivery_status=(delivery or {}).get("delivery_status"),
        )
        return event, {
            "status": "COMPLETED",
            "generation_policy_version": generation_policy_version,
            **(
                {"delivery": generation_delivery.model_dump(mode="json")}
                if generation_delivery is not None
                else {}
            ),
            "result_type": job.result_type,
            "result_record_id": (
                str(job.result_record_id)
                if job.result_record_id is not None
                else None
            ),
            "message": user_message_for_job(job),
            **(delivery or {}),
        }
    if event == "failed":
        _validate_v012_job_delivery(job)
        return event, {
            "status": "FAILED",
            "generation_policy_version": generation_policy_version,
            **(
                {"delivery": generation_delivery.model_dump(mode="json")}
                if generation_delivery is not None
                else {}
            ),
            "error": {
                "code": _sse_failure_code(job),
                "message": _sse_failure_message(job),
            },
        }
    _validate_v012_job_delivery(job)
    return event, {
        "status": _sse_status_for_progress(job),
        "generation_policy_version": generation_policy_version,
        **(
            {"delivery": generation_delivery.model_dump(mode="json")}
            if generation_delivery is not None
            else {}
        ),
        "current_stage": job.current_stage,
        "result_record_id": None,
        "error": None,
    }


def _sse_encode_event(event: str, payload: dict) -> str:
    data = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"event: {event}\ndata: {data}\n\n"


async def _trip_job_sse_events(
    job_id: str,
    *,
    first_job: TripJobRecord | None = None,
) -> AsyncIterator[str]:
    job = first_job
    last_event: str | None = None
    last_data: str | None = None
    last_keepalive = time.monotonic()

    while True:
        if job is None:
            job = await get_trip_job_by_id(job_id)
        if job is None:
            yield _sse_encode_event(
                "failed",
                {
                    "status": "FAILED",
                    "error": {
                        "code": "JOB_NOT_FOUND",
                        "message": "任务不存在",
                    },
                },
            )
            return

        event, payload = await _sse_payload_for_job(job)
        data = _sse_encode_event(event, payload)

        if data != last_data or event != last_event or event != "progress":
            yield data
            last_event = event
            last_data = data

        if event != "progress":
            return

        now = time.monotonic()
        if now - last_keepalive >= SSE_KEEPALIVE_SECONDS:
            yield ": keepalive\n\n"
            last_keepalive = now

        job = None
        await asyncio.sleep(SSE_POLL_INTERVAL_SECONDS)


@router.post("/trip/async", response_model=TripAsyncCreateResponse)
async def trip_async_create(req: TripAsyncCreateRequest) -> TripAsyncCreateResponse:
    message = (req.message or "").strip()
    trip_request_json: dict | None = None
    request_field_provenance: dict[str, str] = {}
    request_user_supplied_json: dict = {
        "_request_metadata": {"source": req.source},
    }
    has_trip_request = req.trip_request is not None
    if message and has_trip_request:
        raise HTTPException(
            status_code=400,
            detail="message and trip_request are mutually exclusive",
        )
    if has_trip_request:
        raw_trip_request = req.trip_request.model_dump()
        request_field_provenance = {
            field_name: "USER_SUPPLIED"
            for field_name in req.trip_request.model_fields_set
            if field_name in GUIDE_REQUEST_FIELD_ALLOWLIST
        }
        raw_to_city = raw_trip_request.get("to_city")
        if not isinstance(raw_to_city, str) or not raw_to_city.strip():
            raise HTTPException(
                status_code=400,
                detail="trip_request.to_city is required",
            )
        try:
            trip_request_json = TripRequest(**raw_trip_request).model_dump()
            request_user_supplied_json.update({
                field_name: (
                    trip_request_json[field_name]
                    if field_name in trip_request_json
                    else raw_trip_request[field_name]
                )
                for field_name in request_field_provenance
                if field_name in raw_trip_request
            })
        except ValidationError as exc:
            raise HTTPException(
                status_code=(
                    422
                    if _validation_error_mentions_time_preferences(
                        exc,
                        raw_trip_request,
                    )
                    else 400
                ),
                detail=exc.errors(),
            ) from exc
    elif not message:
        raise HTTPException(
            status_code=400,
            detail="message or trip_request is required",
        )

    logger.info(
        "POST /trip/async request_id=%s source=%s conversation_id=%s",
        req.request_id,
        req.source,
        req.conversation_id,
    )

    settings = get_settings()
    policy_version: GenerationPolicyVersion = (
        req.generation_policy_version
        if req.generation_policy_version is not None
        else ("v0.12" if settings.v012_trip_creation_enabled else "legacy")
    )

    try:
        created = await create_async_trip_job(
            message=message,
            trip_request_json=trip_request_json,
            request_field_provenance=request_field_provenance,
            request_user_supplied_json=request_user_supplied_json,
            request_id=req.request_id,
            source=req.source,
            conversation_id=req.conversation_id,
            user_display_name=req.user_display_name,
            generation_policy_version=policy_version,
            allow_new_v012=settings.v012_trip_creation_enabled,
        )
    except RequestIdConflictError as exc:
        return JSONResponse(
            status_code=409,
            content={"ok": False, "message": str(exc)},
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except GenerationPolicyNotEnabledError as exc:
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "code": "GENERATION_POLICY_NOT_ENABLED",
                "message": str(exc),
            },
        )

    return TripAsyncCreateResponse(
        job_id=created.job_id,
        status=created.status,
        current_stage=created.current_stage,
        queue_position=created.queue_position,
        message=created.message,
        cached=created.cached,
        generation_policy_version=getattr(created, "generation_policy_version", "legacy"),
        delivery=getattr(created, "delivery", None),
    )


@router.get("/trip/jobs/{job_id}", response_model=TripJobStatusResponse)
async def trip_job_status(job_id: str):
    job = await get_trip_job_by_id(job_id)
    if job is None:
        return JSONResponse(
            status_code=404,
            content={"ok": False, "message": "任务不存在"},
        )

    return await _job_to_status_response_async(job)


@router.get("/trip/jobs/{job_id}/stream")
async def trip_job_stream(job_id: str):
    job = await get_trip_job_by_id(job_id)
    if job is None:
        return JSONResponse(
            status_code=404,
            content={"ok": False, "message": "任务不存在"},
        )

    return StreamingResponse(
        _trip_job_sse_events(job_id, first_job=job),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
