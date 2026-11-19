"""Background worker for v0.8.10.1 export artifacts."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.api.export_source import ExportSource, build_export_source
from src.api.trip_results import ResultContractUnsupported
from src.config import get_settings
from src.export.city_background import (
    CityBackgroundClient,
    build_city_background_client,
)
from src.export.city_photo import CityPhotoResolver, build_city_photo_resolver
from src.export.model_call_observation import image_call_sink
from src.jobs.model_call_store import persist_export_call
from src.export.pdf_renderer import PdfRenderError, render_pdf_artifact
from src.export.share_image_renderer import (
    ShareImageRenderError,
    render_share_image_artifact,
)
from src.jobs.export_store import (
    ExportArtifactRecord,
    claim_next_pending_artifact,
    expire_stale_running_artifacts,
    list_expired_ready_local_artifacts,
    mark_artifact_failed,
    mark_artifact_ready,
)

logger = logging.getLogger(__name__)

ARTIFACT_ORDER = ("pdf", "share_image")
STALE_SWEEP_INTERVAL_SECONDS = 10.0
CLEANUP_INTERVAL_SECONDS = 60.0
EXPIRED_CLEANUP_BATCH_SIZE = 100
AI_RENDER_TIMEOUT_BUFFER_SECONDS = 30.0
SAFE_FAILURE_MESSAGE = "导出失败，请稍后重试"
TIMEOUT_FAILURE_MESSAGE = "导出超时，请稍后重试"


@dataclass(frozen=True)
class _RenderTarget:
    storage_key: str
    final_path: Path
    staging_path: Path


RenderPdf = Callable[[ExportSource, Path], Any]
RenderShareImage = Callable[[ExportSource, Path], Any]
SourceBuilder = Callable[[int, str], Awaitable[ExportSource | None]]
_AUTO_BACKGROUND_CLIENT = object()
_AUTO_CITY_PHOTO_RESOLVER = object()


class ExportWorker:
    """Claim and render export artifacts without touching trip generation."""

    def __init__(
        self,
        *,
        pdf_renderer: RenderPdf | None = None,
        share_image_renderer: RenderShareImage | None = None,
        source_builder: SourceBuilder | None = None,
        city_background_client: CityBackgroundClient | None | object = _AUTO_BACKGROUND_CLIENT,
        city_photo_resolver: CityPhotoResolver | None | object = _AUTO_CITY_PHOTO_RESOLVER,
    ) -> None:
        self._stop = asyncio.Event()
        self._tasks: dict[str, set[asyncio.Task[None]]] = {
            artifact_type: set()
            for artifact_type in ARTIFACT_ORDER
        }
        if city_background_client is _AUTO_BACKGROUND_CLIENT:
            self._city_background_client = build_city_background_client(get_settings())
        else:
            self._city_background_client = city_background_client
        if city_photo_resolver is _AUTO_CITY_PHOTO_RESOLVER:
            self._city_photo_resolver = build_city_photo_resolver(get_settings())
        else:
            self._city_photo_resolver = city_photo_resolver
        self._pdf_renderer = pdf_renderer or self._default_pdf_renderer
        self._share_image_renderer = (
            share_image_renderer or self._default_share_image_renderer
        )
        self._source_builder = source_builder or build_export_source

    async def run(self) -> None:
        settings = get_settings()
        logger.info(
            "export worker started pdf_concurrency=%s share_image_concurrency=%s",
            settings.export_pdf_concurrency,
            settings.export_share_image_concurrency,
        )
        next_stale_sweep = 0.0
        next_cleanup = 0.0
        try:
            while not self._stop.is_set():
                try:
                    now = time.monotonic()
                    await self.run_once(
                        sweep_stale=now >= next_stale_sweep,
                        cleanup_expired=now >= next_cleanup,
                    )
                    if now >= next_stale_sweep:
                        next_stale_sweep = now + STALE_SWEEP_INTERVAL_SECONDS
                    if now >= next_cleanup:
                        next_cleanup = now + CLEANUP_INTERVAL_SECONDS
                    await asyncio.sleep(
                        max(0.05, settings.export_worker_poll_interval_seconds),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("export worker loop failed; retrying")
                    await asyncio.sleep(
                        max(0.05, settings.export_worker_poll_interval_seconds),
                    )
        finally:
            all_tasks = [task for tasks in self._tasks.values() for task in tasks]
            if all_tasks:
                await asyncio.gather(*all_tasks, return_exceptions=True)
            logger.info("export worker stopped")

    async def stop(self) -> None:
        self._stop.set()

    async def run_once(
        self,
        *,
        sweep_stale: bool = True,
        cleanup_expired: bool = True,
    ) -> int:
        self._prune_done_tasks()
        if sweep_stale:
            await self.expire_stale_running()
        if cleanup_expired:
            await self.cleanup_expired_files()

        claimed = 0
        for artifact_type in ARTIFACT_ORDER:
            claimed += await self._claim_available(artifact_type)
        if claimed:
            await asyncio.sleep(0)
        return claimed

    async def wait_for_active_tasks(self) -> None:
        all_tasks = [task for tasks in self._tasks.values() for task in tasks]
        if all_tasks:
            await asyncio.gather(*all_tasks)
        self._prune_done_tasks()

    async def expire_stale_running(self) -> int:
        pdf_count = await expire_stale_running_artifacts(
            "pdf",
            timeout_seconds=self._timeout_for("pdf"),
        )
        share_count = await expire_stale_running_artifacts(
            "share_image",
            timeout_seconds=self._timeout_for("share_image"),
        )
        count = pdf_count + share_count
        if count:
            logger.warning("export worker expired stale running artifacts count=%s", count)
        return count

    async def cleanup_expired_files(self) -> int:
        records = await list_expired_ready_local_artifacts(
            limit=EXPIRED_CLEANUP_BATCH_SIZE,
        )
        deleted = 0
        for record in records:
            path = _local_path_for_key(record.storage_key)
            if path is None:
                continue
            try:
                path.unlink(missing_ok=True)
                deleted += 1
            except Exception:
                logger.warning(
                    "export worker failed to delete expired artifact file artifact_id=%s",
                    record.artifact_id,
                    exc_info=True,
                )
        return deleted

    async def _claim_available(self, artifact_type: str) -> int:
        max_concurrency = _concurrency_for(artifact_type)
        if max_concurrency <= 0:
            return 0

        tasks = self._tasks[artifact_type]
        claimed = 0
        while len(tasks) < max_concurrency:
            record = await claim_next_pending_artifact(
                artifact_type,
                max_concurrency,
            )
            if record is None:
                break
            task = asyncio.create_task(self._execute_artifact(record))
            tasks.add(task)
            task.add_done_callback(lambda _task, kind=artifact_type: self._tasks[kind].discard(_task))
            claimed += 1
        return claimed

    async def _execute_artifact(self, record: ExportArtifactRecord) -> None:
        logger.info(
            "export worker executing artifact_id=%s artifact_type=%s",
            record.artifact_id,
            record.artifact_type,
        )
        target: _RenderTarget | None = None
        observation_loop = asyncio.get_running_loop()
        observation_writes = []

        async def save_observation(call):
            try:
                await asyncio.wait_for(persist_export_call(record.artifact_id, call), timeout=0.2)
            except Exception:
                logger.warning("export model call observation unavailable")

        def observation_sink(call):
            # to_thread inherits this context. A timed-out renderer can still
            # finish a paid call later; keep that fact without changing its work.
            if not observation_loop.is_closed():
                observation_writes.append(asyncio.run_coroutine_threadsafe(save_observation(call), observation_loop))

        observation_token = image_call_sink.set(observation_sink)
        try:
            source = await self._source_builder(record.result_record_id, record.artifact_type)
            if source is None:
                await mark_artifact_failed(
                    record.artifact_id,
                    error_code="RESULT_NOT_FOUND",
                    error_message="攻略不存在",
                )
                return
            if (
                source.source_hash != record.source_hash
                or source.export_version != record.export_version
            ):
                await mark_artifact_failed(
                    record.artifact_id,
                    error_code="EXPORT_SOURCE_CHANGED",
                    error_message=SAFE_FAILURE_MESSAGE,
                )
                return

            target = _render_target(record)
            target.final_path.parent.mkdir(parents=True, exist_ok=True)
            _unlink_silent(target.staging_path)

            render_result = await asyncio.wait_for(
                asyncio.to_thread(
                    self._render_to_staging,
                    record,
                    source,
                    target,
                ),
                timeout=self._timeout_for(record.artifact_type),
            )
            os.replace(target.staging_path, target.final_path)
            await self._mark_ready(record, source, target, render_result)
        except asyncio.TimeoutError:
            if target is not None:
                _unlink_silent(target.staging_path)
            logger.warning("export worker timeout artifact_id=%s", record.artifact_id)
            await mark_artifact_failed(
                record.artifact_id,
                error_code="EXPORT_TIMEOUT",
                error_message=TIMEOUT_FAILURE_MESSAGE,
            )
        except asyncio.CancelledError:
            if target is not None:
                _unlink_silent(target.staging_path)
            raise
        except ResultContractUnsupported:
            if target is not None:
                _unlink_silent(target.staging_path)
            await mark_artifact_failed(
                record.artifact_id,
                error_code="RESULT_CONTRACT_UNSUPPORTED",
                error_message="该攻略由旧版本生成，暂不支持导出，请重新生成",
            )
        except (PdfRenderError, ShareImageRenderError) as exc:
            if target is not None:
                _unlink_silent(target.staging_path)
            code = _stable_render_error_code(record.artifact_type, exc.code)
            logger.warning(
                "export worker renderer failed artifact_id=%s code=%s",
                record.artifact_id,
                code,
                exc_info=True,
            )
            await mark_artifact_failed(
                record.artifact_id,
                error_code=code,
                error_message=SAFE_FAILURE_MESSAGE,
            )
        except OSError:
            if target is not None:
                _unlink_silent(target.staging_path)
            logger.exception("export worker file failure artifact_id=%s", record.artifact_id)
            await mark_artifact_failed(
                record.artifact_id,
                error_code="EXPORT_FILE_WRITE_FAILED",
                error_message=SAFE_FAILURE_MESSAGE,
            )
        except Exception:
            if target is not None:
                _unlink_silent(target.staging_path)
            logger.exception("export worker failed artifact_id=%s", record.artifact_id)
            await mark_artifact_failed(
                record.artifact_id,
                error_code=_generic_render_error_code(record.artifact_type),
                error_message=SAFE_FAILURE_MESSAGE,
            )
        finally:
            image_call_sink.reset(observation_token)
            if observation_writes:
                await asyncio.wait([asyncio.wrap_future(f) for f in tuple(observation_writes)], timeout=0.2)

    def _render_to_staging(
        self,
        record: ExportArtifactRecord,
        source: ExportSource,
        target: _RenderTarget,
    ) -> Any:
        if record.artifact_type == "pdf":
            return self._pdf_renderer(source, target.staging_path)
        if record.artifact_type == "share_image":
            return self._share_image_renderer(source, target.staging_path)
        raise ValueError(f"unsupported artifact_type={record.artifact_type!r}")

    async def _mark_ready(
        self,
        record: ExportArtifactRecord,
        source: ExportSource,
        target: _RenderTarget,
        render_result: Any,
    ) -> None:
        if not target.final_path.is_file():
            raise OSError("final artifact file was not published")
        byte_size = int(getattr(render_result, "byte_size", target.final_path.stat().st_size))
        sha256 = str(getattr(render_result, "sha256", "") or "")
        if byte_size <= 0 or len(sha256) != 64:
            raise OSError("renderer returned invalid file metadata")

        metadata = {
            **(record.metadata or {}),
            **dict(getattr(render_result, "metadata", {}) or {}),
            "export_version": source.export_version,
            "source_schema_version": source.source["result"].get("schema_version"),
        }
        await mark_artifact_ready(
            record.artifact_id,
            storage_key=target.storage_key,
            filename=_filename_for(source, record.artifact_type),
            mime_type=str(getattr(render_result, "mime_type", _mime_type_for(record.artifact_type))),
            byte_size=byte_size,
            sha256=sha256,
            text_length=getattr(render_result, "text_length", None),
            width_px=getattr(render_result, "width_px", None),
            height_px=getattr(render_result, "height_px", None),
            page_count=getattr(render_result, "page_count", None),
            metadata=metadata,
        )

    def _default_pdf_renderer(self, source: ExportSource, output_path: Path) -> Any:
        return render_pdf_artifact(
            source,
            output_path,
            city_photo_resolver=self._city_photo_resolver,
            storage_key=None,
        )

    def _default_share_image_renderer(self, source: ExportSource, output_path: Path) -> Any:
        return render_share_image_artifact(
            source,
            output_path,
            ai_background_client=self._city_background_client,
            storage_key=None,
        )

    def _timeout_for(self, artifact_type: str) -> float:
        configured = _timeout_for(artifact_type)
        client_timeout = getattr(self._city_background_client, "timeout_seconds", None)
        if (
            artifact_type == "share_image"
            and isinstance(client_timeout, (int, float))
            and client_timeout > 0
        ):
            return max(configured, float(client_timeout) + AI_RENDER_TIMEOUT_BUFFER_SECONDS)
        return configured

    def _prune_done_tasks(self) -> None:
        for artifact_type, tasks in self._tasks.items():
            self._tasks[artifact_type] = {task for task in tasks if not task.done()}


def _concurrency_for(artifact_type: str) -> int:
    settings = get_settings()
    if artifact_type == "pdf":
        return settings.export_pdf_concurrency
    if artifact_type == "share_image":
        return settings.export_share_image_concurrency
    return 0


def _timeout_for(artifact_type: str) -> float:
    settings = get_settings()
    if artifact_type == "pdf":
        return float(settings.export_pdf_timeout_seconds)
    if artifact_type == "share_image":
        return float(settings.export_share_image_timeout_seconds)
    return 1.0


def _mime_type_for(artifact_type: str) -> str:
    if artifact_type == "pdf":
        return "application/pdf"
    if artifact_type == "share_image":
        return "image/png"
    return "application/octet-stream"


def _extension_for(artifact_type: str) -> str:
    if artifact_type == "pdf":
        return "pdf"
    if artifact_type == "share_image":
        return "png"
    raise ValueError(f"unsupported artifact_type={artifact_type!r}")


def _render_target(record: ExportArtifactRecord) -> _RenderTarget:
    now = datetime.now(timezone.utc)
    ext = _extension_for(record.artifact_type)
    storage_key = (
        f"exports/{now:%Y}/{now:%m}/result-{record.result_record_id}/"
        f"{record.artifact_type}-{record.artifact_id}.{ext}"
    )
    final_path = _local_path_for_key(storage_key)
    if final_path is None:
        raise OSError("invalid export storage key")
    staging_path = final_path.with_name(
        f".{final_path.name}.{uuid.uuid4().hex}.rendering",
    )
    return _RenderTarget(
        storage_key=storage_key,
        final_path=final_path,
        staging_path=staging_path,
    )


def _local_path_for_key(storage_key: str | None) -> Path | None:
    if not storage_key:
        return None
    raw_key = Path(storage_key)
    if raw_key.is_absolute():
        return None
    base = Path(get_settings().export_storage_dir).expanduser().resolve()
    candidate = base / raw_key
    if raw_key.parts and raw_key.parts[0] == base.name:
        candidate = base.parent / raw_key
    resolved = candidate.expanduser().resolve()
    try:
        resolved.relative_to(base)
    except ValueError:
        return None
    return resolved


def _filename_for(source: ExportSource, artifact_type: str) -> str:
    result = source.source.get("result", {})
    city = _safe_filename_text(
        (result.get("city") or {}).get("name") if isinstance(result.get("city"), dict) else "",
        default="旅行",
    )
    request = result.get("request") if isinstance(result.get("request"), dict) else {}
    days = _safe_filename_text(str(request.get("days") or ""), default="")
    if artifact_type == "pdf":
        suffix = "攻略.pdf"
    elif artifact_type == "share_image":
        suffix = "分享图.png"
    else:
        suffix = "导出文件"
    if days:
        return f"{city}{days}天{suffix}"
    return f"{city}{suffix}"


def _safe_filename_text(value: str, *, default: str) -> str:
    text = re.sub(r"[\r\n\t/\\:*?\"<>|]+", "", str(value or "")).strip()
    return text or default


def _stable_render_error_code(artifact_type: str, code: str) -> str:
    normalized = (code or "").strip().upper()
    if normalized and len(normalized) <= 50:
        return normalized
    return _generic_render_error_code(artifact_type)


def _generic_render_error_code(artifact_type: str) -> str:
    if artifact_type == "pdf":
        return "PDF_RENDER_FAILED"
    if artifact_type == "share_image":
        return "SHARE_IMAGE_RENDER_FAILED"
    return "EXPORT_RENDER_FAILED"


def _unlink_silent(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass
