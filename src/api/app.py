"""YunTu Travel API — thin HTTP layer over run_trip_workflow."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.agents.workflow import run_trip_workflow
from src.api.formatter import format_plans_markdown
from src.api.internal_admin import (
    InternalAdminError,
    internal_admin_error_handler,
)
from src.api.internal_admin import (
    router as internal_admin_router,
)
from src.api.destinations import router as destinations_router
from src.api.internal_city import router as internal_city_router
from src.api.internal_crawl import router as internal_crawl_router
from src.api.internal_feedback import router as internal_feedback_router
from src.api.internal_model_calls import router as internal_model_calls_router
from src.api.public_guard import verify_public_api_client
from src.api.trip_artifacts import router as trip_artifacts_router
from src.api.trip_async import router as trip_async_router
from src.api.trip_places import internal_router as internal_trip_places_router
from src.api.trip_places import router as trip_places_router
from src.api.trip_results import router as trip_results_router
from src.config import get_settings
from src.jobs.city_batch_worker import CityBatchWorker
from src.jobs.city_gate import CityGateRejected, gate_sync_trip_request
from src.jobs.crawl_worker import CrawlWorker
from src.jobs.export_worker import ExportWorker
from src.jobs.projection_publisher import ProjectionOutboxPublisher
from src.jobs.trip_worker import TripWorker

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    from src.model_routes import key_problems

    for problem in key_problems(settings):
        logger.error("model route config: %s", problem)
    trip_worker = TripWorker()
    crawl_worker = CrawlWorker()
    city_batch_worker = CityBatchWorker()
    export_worker = ExportWorker()
    projection_publisher = ProjectionOutboxPublisher()
    app.state.projection_publisher = projection_publisher
    background_tasks: list[asyncio.Task] = []

    if settings.trip_worker_enabled:
        background_tasks.append(asyncio.create_task(trip_worker.run()))
        logger.info("trip worker background task started")
    else:
        logger.info("trip worker disabled by TRIP_WORKER_ENABLED=false")

    if settings.crawl_worker_enabled:
        background_tasks.append(asyncio.create_task(crawl_worker.run()))
        logger.info("crawl worker background task started")
    else:
        logger.info("crawl worker disabled by CRAWL_WORKER_ENABLED=false")

    if settings.city_batch_worker_enabled:
        background_tasks.append(asyncio.create_task(city_batch_worker.run()))
        logger.info("city batch worker background task started")
    else:
        logger.info("city batch worker disabled by CITY_BATCH_WORKER_ENABLED=false")

    if settings.export_worker_enabled:
        background_tasks.append(asyncio.create_task(export_worker.run()))
        logger.info("export worker background task started")
    else:
        logger.info("export worker disabled by EXPORT_WORKER_ENABLED=false")

    if settings.projection_publisher_enabled:
        background_tasks.append(asyncio.create_task(projection_publisher.run()))
        logger.info("projection Outbox publisher background task started")
    else:
        logger.info(
            "projection Outbox publisher disabled by PROJECTION_PUBLISHER_ENABLED=false"
        )

    yield

    if settings.trip_worker_enabled:
        await trip_worker.stop()
    if settings.crawl_worker_enabled:
        await crawl_worker.stop()
    if settings.city_batch_worker_enabled:
        await city_batch_worker.stop()
    if settings.export_worker_enabled:
        await export_worker.stop()
    if settings.projection_publisher_enabled:
        await projection_publisher.stop()

    for task in background_tasks:
        task.cancel()
    for task in background_tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="YunTu Travel API",
    version=get_settings().app_version,
    lifespan=lifespan,
)
app.add_exception_handler(InternalAdminError, internal_admin_error_handler)
app.include_router(internal_admin_router)
app.include_router(trip_async_router)
app.include_router(trip_results_router)
app.include_router(trip_artifacts_router)
app.include_router(trip_places_router)
app.include_router(destinations_router)
app.include_router(internal_trip_places_router)
app.include_router(internal_city_router)
app.include_router(internal_crawl_router)
app.include_router(internal_feedback_router)
app.include_router(internal_model_calls_router)


class TripRequest(BaseModel):
    message: str


class TripResponse(BaseModel):
    ok: bool
    reply_text: str
    trip_request: dict
    plan_count: int
    record_saved: bool
    elapsed_ms: int


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "service": "yuntu-travel"}


@app.post(
    "/trip",
    response_model=TripResponse,
    dependencies=[Depends(verify_public_api_client)],
)
async def trip(req: TripRequest) -> TripResponse:
    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is empty")

    logger.info("POST /trip message=%s", message[:80])
    t0 = time.monotonic()

    async def on_trip_request(trip_request) -> None:
        decision = await gate_sync_trip_request(trip_request)
        if not decision.allowed:
            raise CityGateRejected(decision)

    try:
        result = await run_trip_workflow(
            message,
            on_trip_request=on_trip_request,
        )
    except CityGateRejected as exc:
        return JSONResponse(
            status_code=409,
            content={"ok": False, **exc.decision.to_dict()},
        )
    except Exception as e:
        logger.exception("workflow failed for message=%s", message[:80])
        raise HTTPException(status_code=500, detail="旅行规划失败，请稍后重试") from e

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    reply_text = format_plans_markdown(result)

    return TripResponse(
        ok=True,
        reply_text=reply_text,
        trip_request=result.trip_request.model_dump(),
        plan_count=len(result.plans),
        record_saved=result.record_id is not None,
        elapsed_ms=elapsed_ms,
    )
