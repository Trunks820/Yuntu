"""Narrow Amap POI 2.0 ID-detail adapter for v0.9.9.6 opening facts.

Queries only budget-valid locked places that already have ``amap_poi_id``.
The shared wall-clock timeout starts at task-handle creation (t0/deadline)
and is reused by fetch and the Writer barrier; it is not reset per batch.
All fail-open paths return zero or partial facts and content-free metrics.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from src.agents.pretrip_advice import (
    AMAP_SOURCE,
    AmapTipFact,
    amap_evidence_ref,
    normalize_advice_text,
)
from src.agents.amap_keys import (
    POI_DETAIL_KEY_BUCKET,
    AmapKeyCoordinator,
    classify_amap_infocode,
    configured_amap_keys,
    dedupe_amap_keys,
    get_amap_key_coordinator,
    key_fingerprint,
)
from src.agents.schema import CandidatePlace
from src.config import Settings, get_settings

logger = logging.getLogger(__name__)

AMAP_POI_DETAIL_URL = "https://restapi.amap.com/v5/place/detail"
AMAP_POI_DETAIL_BATCH_SIZE = 10
OPENING_FIELDS = ("opentime_today", "opentime_week")

STATUS_OK = "ok"
STATUS_PARTIAL = "partial"
STATUS_DISABLED = "skipped_disabled"
STATUS_API_KEY_MISSING = "skipped_api_key_missing"
STATUS_TIMEOUT = "skipped_timeout"
STATUS_FORBIDDEN = "skipped_forbidden"
STATUS_RATE_LIMITED = "skipped_rate_limited"
STATUS_API_ERROR = "skipped_api_error"
STATUS_MALFORMED = "skipped_malformed"

_FORBIDDEN_INFOCODES = {"10001", "10002", "10009", "10010"}
_RATE_LIMIT_INFOCODES = {"10003", "10004"}
_STOP_BATCHING_STATUSES = {
    STATUS_TIMEOUT,
    STATUS_FORBIDDEN,
    STATUS_RATE_LIMITED,
}


@dataclass
class _LockedPoiTarget:
    place_id: int
    place_name: str
    amap_poi_id: str


@dataclass
class AmapPoiDetailResult:
    facts: list[AmapTipFact] = field(default_factory=list)
    status: str = STATUS_OK
    metrics: dict[str, Any] = field(default_factory=dict)

    def content_free_metrics(self) -> dict[str, Any]:
        return dict(self.metrics)


class AmapPoiDetailClient:
    """Injectable async client for Amap POI 2.0 ``/v5/place/detail``."""

    def __init__(
        self,
        *,
        api_key: str = "",
        api_keys: Sequence[str] | None = None,
        enabled: bool = False,
        timeout_seconds: float = 5.0,
        client: Any | None = None,
        monotonic: Callable[[], float] | None = None,
        queried_at: str | None = None,
        coordinator: AmapKeyCoordinator | None = None,
    ) -> None:
        if api_keys is None:
            stripped = str(api_key or "").strip()
            self._keys = [stripped] if stripped else []
            # An explicit one-key client stays on its own coordinator so tests
            # do not mark a process-wide key exhausted.
            self._key_coordinator = coordinator or AmapKeyCoordinator()
        else:
            self._keys = dedupe_amap_keys(api_keys)
            self._key_coordinator = coordinator or get_amap_key_coordinator()
        self.api_key = self._keys[0] if self._keys else ""
        self.enabled = bool(enabled)
        self.timeout_seconds = float(timeout_seconds)
        self._client = client
        self._monotonic = monotonic or time.monotonic
        self._queried_at = queried_at

    async def fetch_opening_facts(
        self,
        places: Sequence[CandidatePlace | Any],
        *,
        t0: float | None = None,
        deadline: float | None = None,
    ) -> AmapPoiDetailResult:
        started = self._monotonic() if t0 is None else float(t0)
        resolved_deadline = (
            float(deadline)
            if deadline is not None
            else started + max(self.timeout_seconds, 0.0)
        )
        try:
            return await self._fetch_opening_facts(
                places,
                t0=started,
                deadline=resolved_deadline,
            )
        except Exception:
            logger.warning(
                "amap_poi_detail fail-open status=%s",
                STATUS_API_ERROR,
            )
            return _result(
                status=STATUS_API_ERROR,
                t0=started,
                monotonic=self._monotonic,
                batch_count=0,
                batch_statuses=[STATUS_API_ERROR],
            )

    async def _fetch_opening_facts(
        self,
        places: Sequence[CandidatePlace | Any],
        *,
        t0: float,
        deadline: float,
    ) -> AmapPoiDetailResult:
        if not self.enabled:
            return _result(
                status=STATUS_DISABLED,
                t0=t0,
                monotonic=self._monotonic,
            )
        if not self._keys:
            return _result(
                status=STATUS_API_KEY_MISSING,
                t0=t0,
                monotonic=self._monotonic,
            )

        targets, skipped_no_poi_id = _collect_locked_targets(places)
        if not targets:
            return _result(
                status=STATUS_OK,
                t0=t0,
                monotonic=self._monotonic,
                skipped_no_poi_id=skipped_no_poi_id,
                unique_poi_count=0,
            )
        queried_at = self._queried_at or _utc_now_iso()
        snapshot_label = _snapshot_label(queried_at)
        facts: list[AmapTipFact] = []
        batch_statuses: list[str] = []
        dropped_poi_count = 0
        batches = _batched(
            [target.amap_poi_id for target in targets],
            AMAP_POI_DETAIL_BATCH_SIZE,
        )
        target_by_poi = {target.amap_poi_id: target for target in targets}

        for batch_ids in batches:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                batch_statuses.append(STATUS_TIMEOUT)
                break
            batch_facts, dropped, batch_status = await self._fetch_batch(
                batch_ids,
                target_by_poi=target_by_poi,
                remaining=remaining,
                queried_at=queried_at,
                snapshot_label=snapshot_label,
            )
            facts.extend(batch_facts)
            dropped_poi_count += dropped
            batch_statuses.append(batch_status)
            if batch_status in _STOP_BATCHING_STATUSES:
                break

        status = _aggregate_status(batch_statuses, has_facts=bool(facts))
        _log_status(
            status,
            batch_count=len(batch_statuses),
            fact_count=len(facts),
        )
        return _result(
            status=status,
            t0=t0,
            monotonic=self._monotonic,
            facts=facts,
            batch_count=len(batch_statuses),
            batch_statuses=batch_statuses,
            authorized_fact_count=len(facts),
            skipped_no_poi_id=skipped_no_poi_id,
            unique_poi_count=len(targets),
            dropped_poi_count=dropped_poi_count,
        )

    async def _fetch_batch(
        self,
        batch_ids: list[str],
        *,
        target_by_poi: dict[str, _LockedPoiTarget],
        remaining: float,
        queried_at: str,
        snapshot_label: str,
    ) -> tuple[list[AmapTipFact], int, str]:
        excluded: set[str] = set()
        last_status = STATUS_API_KEY_MISSING
        while True:
            key = self._key_coordinator.next_key(
                self._keys,
                bucket=POI_DETAIL_KEY_BUCKET,
                exclude=excluded,
            )
            if key is None:
                return [], 0, last_status
            params = {
                "key": key,
                "id": "|".join(batch_ids),
                "show_fields": "business",
            }
            try:
                response = await self._get(params, remaining)
            except httpx.TimeoutException:
                logger.warning("amap_poi_detail fail-open status=%s", STATUS_TIMEOUT)
                return [], 0, STATUS_TIMEOUT
            except httpx.TransportError:
                logger.warning("amap_poi_detail fail-open status=%s", STATUS_API_ERROR)
                return [], 0, STATUS_API_ERROR
            except Exception:
                logger.warning("amap_poi_detail fail-open status=%s", STATUS_API_ERROR)
                return [], 0, STATUS_API_ERROR

            http_status = int(getattr(response, "status_code", 0) or 0)
            http_fail = _status_from_http(http_status)
            if http_fail is not None:
                logger.warning(
                    "amap_poi_detail fail-open status=%s http=%s",
                    http_fail,
                    http_status,
                )
                return [], 0, http_fail

            payload = _read_json(response)
            if payload is None:
                logger.warning("amap_poi_detail fail-open status=%s", STATUS_MALFORMED)
                return [], 0, STATUS_MALFORMED

            provider_status = _status_from_payload(payload)
            if provider_status is not None:
                infocode = str(payload.get("infocode") or "")
                info = str(payload.get("info") or "")
                if classify_amap_infocode(infocode, info) == "quota":
                    self._key_coordinator.mark_quota_exhausted(
                        key,
                        bucket=POI_DETAIL_KEY_BUCKET,
                    )
                    excluded.add(key_fingerprint(key))
                    last_status = provider_status
                    logger.warning(
                        "amap_poi_detail quota fingerprint=%s infocode=%s",
                        key_fingerprint(key),
                        infocode or "-",
                    )
                    if self._key_coordinator.usable_keys(
                        self._keys,
                        bucket=POI_DETAIL_KEY_BUCKET,
                        exclude=excluded,
                    ):
                        continue
                logger.warning(
                    "amap_poi_detail fail-open status=%s infocode=%s",
                    provider_status,
                    infocode or "-",
                )
                return [], 0, provider_status
            break

        pois = payload.get("pois")
        if not isinstance(pois, list):
            logger.warning("amap_poi_detail fail-open status=%s", STATUS_MALFORMED)
            return [], 0, STATUS_MALFORMED

        requested = set(batch_ids)
        facts: list[AmapTipFact] = []
        matched_ids: set[str] = set()
        dropped = 0
        for item in pois:
            poi_facts, poi_id, drop = _facts_from_poi(
                item,
                requested=requested,
                target_by_poi=target_by_poi,
                queried_at=queried_at,
                snapshot_label=snapshot_label,
            )
            if poi_id:
                matched_ids.add(poi_id)
            dropped += drop
            facts.extend(poi_facts)
        dropped += len(requested - matched_ids)
        batch_status = STATUS_PARTIAL if dropped else STATUS_OK
        return facts, dropped, batch_status

    async def _get(self, params: dict[str, str], remaining: float) -> Any:
        timeout = remaining
        if self._client is not None:
            return await self._client.get(
                AMAP_POI_DETAIL_URL,
                params=params,
                timeout=timeout,
            )
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.get(
                AMAP_POI_DETAIL_URL,
                params=params,
                timeout=timeout,
            )


def amap_poi_detail_client_from_settings(
    settings: Settings | None = None,
    *,
    client: Any | None = None,
) -> AmapPoiDetailClient:
    resolved = settings or get_settings()
    keys = configured_amap_keys(resolved)
    return AmapPoiDetailClient(
        api_keys=keys,
        enabled=resolved.amap_poi_detail_enabled,
        timeout_seconds=resolved.amap_poi_detail_timeout,
        client=client,
        coordinator=get_amap_key_coordinator(),
    )


@dataclass
class AmapPoiDetailTaskHandle:
    """Lifecycle handle for one Amap detail task.

    ``t0`` / ``deadline`` are recorded before ``asyncio.create_task``. Disabled
    or missing-key starts return ``task is None`` and never create a task.
    """

    task: asyncio.Task[AmapPoiDetailResult] | None
    t0: float
    deadline: float
    timeout_seconds: float
    skip_status: str | None = None
    monotonic: Callable[[], float] = time.monotonic

    @property
    def started(self) -> bool:
        return self.task is not None


def start_amap_poi_detail_task(
    client: AmapPoiDetailClient,
    places: Sequence[CandidatePlace | Any],
    *,
    monotonic: Callable[[], float] | None = None,
) -> AmapPoiDetailTaskHandle:
    """Record t0/deadline, then create at most one fetch task."""
    clock = monotonic or client._monotonic
    timeout_seconds = max(float(client.timeout_seconds), 0.0)
    t0 = clock()
    deadline = t0 + timeout_seconds
    if not client.enabled:
        return AmapPoiDetailTaskHandle(
            task=None,
            t0=t0,
            deadline=deadline,
            timeout_seconds=timeout_seconds,
            skip_status=STATUS_DISABLED,
            monotonic=clock,
        )
    if not client._keys:
        return AmapPoiDetailTaskHandle(
            task=None,
            t0=t0,
            deadline=deadline,
            timeout_seconds=timeout_seconds,
            skip_status=STATUS_API_KEY_MISSING,
            monotonic=clock,
        )
    task = asyncio.create_task(
        client.fetch_opening_facts(places, t0=t0, deadline=deadline),
        name="amap_poi_detail",
    )
    return AmapPoiDetailTaskHandle(
        task=task,
        t0=t0,
        deadline=deadline,
        timeout_seconds=timeout_seconds,
        monotonic=clock,
    )


async def await_amap_poi_detail_at_writer_barrier(
    handle: AmapPoiDetailTaskHandle,
) -> AmapPoiDetailResult:
    """Await only remaining budget and consume the task outcome."""
    if handle.task is None:
        return _lifecycle_result(
            handle.skip_status or STATUS_DISABLED,
            handle,
        )
    if handle.task.done():
        return await _consume_amap_poi_detail_outcome(handle)
    remaining = max(handle.deadline - handle.monotonic(), 0.0)
    if remaining <= 0:
        await settle_amap_poi_detail_task(handle)
        return _lifecycle_result(STATUS_TIMEOUT, handle)
    try:
        result = await asyncio.wait_for(handle.task, timeout=remaining)
    except asyncio.TimeoutError:
        await settle_amap_poi_detail_task(handle)
        return _lifecycle_result(STATUS_TIMEOUT, handle)
    except asyncio.CancelledError:
        await settle_amap_poi_detail_task(handle)
        return _lifecycle_result(STATUS_TIMEOUT, handle)
    except Exception:
        await settle_amap_poi_detail_task(handle)
        logger.warning("amap_poi_detail fail-open status=%s", STATUS_API_ERROR)
        return _lifecycle_result(STATUS_API_ERROR, handle)
    if isinstance(result, AmapPoiDetailResult):
        return result
    return _lifecycle_result(STATUS_API_ERROR, handle)


async def settle_amap_poi_detail_task(handle: AmapPoiDetailTaskHandle) -> None:
    """Cancel a pending task, await it, and consume CancelledError."""
    task = handle.task
    if task is None:
        return
    if not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return
    except Exception:
        return


async def _consume_amap_poi_detail_outcome(
    handle: AmapPoiDetailTaskHandle,
) -> AmapPoiDetailResult:
    task = handle.task
    if task is None:
        return _lifecycle_result(
            handle.skip_status or STATUS_DISABLED,
            handle,
        )
    try:
        result = await task
    except asyncio.CancelledError:
        return _lifecycle_result(STATUS_TIMEOUT, handle)
    except Exception:
        logger.warning("amap_poi_detail fail-open status=%s", STATUS_API_ERROR)
        return _lifecycle_result(STATUS_API_ERROR, handle)
    if isinstance(result, AmapPoiDetailResult):
        return result
    return _lifecycle_result(STATUS_API_ERROR, handle)


def _lifecycle_result(
    status: str,
    handle: AmapPoiDetailTaskHandle,
) -> AmapPoiDetailResult:
    skip = status in {STATUS_DISABLED, STATUS_API_KEY_MISSING}
    return _result(
        status=status,
        t0=handle.t0,
        monotonic=handle.monotonic,
        batch_count=0 if skip else 1,
        batch_statuses=[] if skip else [status],
    )


def _collect_locked_targets(
    places: Sequence[CandidatePlace | Any],
) -> tuple[list[_LockedPoiTarget], int]:
    targets: list[_LockedPoiTarget] = []
    seen_poi_ids: set[str] = set()
    skipped_no_poi_id = 0
    for place in places:
        poi_id = normalize_advice_text(getattr(place, "amap_poi_id", None))
        if not poi_id:
            skipped_no_poi_id += 1
            continue
        if poi_id in seen_poi_ids:
            continue
        try:
            place_id = int(getattr(place, "place_id"))
        except (TypeError, ValueError):
            skipped_no_poi_id += 1
            continue
        seen_poi_ids.add(poi_id)
        targets.append(
            _LockedPoiTarget(
                place_id=place_id,
                place_name=normalize_advice_text(getattr(place, "name", "")),
                amap_poi_id=poi_id,
            )
        )
    return targets, skipped_no_poi_id


def _batched(values: list[str], size: int) -> list[list[str]]:
    return [values[index:index + size] for index in range(0, len(values), size)]


def _read_json(response: Any) -> dict[str, Any] | None:
    try:
        payload = response.json()
    except (ValueError, TypeError, AttributeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _status_from_http(status_code: int) -> str | None:
    if status_code in {401, 403}:
        return STATUS_FORBIDDEN
    if status_code == 429:
        return STATUS_RATE_LIMITED
    if status_code >= 400:
        return STATUS_API_ERROR
    return None


def _status_from_payload(payload: dict[str, Any]) -> str | None:
    infocode = str(payload.get("infocode") or "").strip()
    info = str(payload.get("info") or "")
    status = str(payload.get("status") or "").strip()
    if status == "1" and infocode in {"", "10000"}:
        return None
    text = f"{infocode} {info}".lower()
    if infocode in _FORBIDDEN_INFOCODES:
        return STATUS_FORBIDDEN
    if infocode in _RATE_LIMIT_INFOCODES or "quota" in text or "limit" in text:
        return STATUS_RATE_LIMITED
    if status == "1" and not infocode:
        return None
    return STATUS_API_ERROR


def _facts_from_poi(
    item: Any,
    *,
    requested: set[str],
    target_by_poi: dict[str, _LockedPoiTarget],
    queried_at: str,
    snapshot_label: str,
) -> tuple[list[AmapTipFact], str, int]:
    if not isinstance(item, dict):
        return [], "", 1
    poi_id = normalize_advice_text(item.get("id"))
    if not poi_id or poi_id not in requested:
        return [], poi_id, 1
    target = target_by_poi.get(poi_id)
    if target is None:
        return [], poi_id, 1
    business = item.get("business")
    if not isinstance(business, dict):
        return [], poi_id, 1
    facts: list[AmapTipFact] = []
    for field_name in OPENING_FIELDS:
        value = _normalize_opening_value(business.get(field_name))
        if not value:
            continue
        facts.append(
            AmapTipFact(
                place_id=target.place_id,
                place_name=target.place_name,
                amap_poi_id=target.amap_poi_id,
                field=field_name,  # type: ignore[arg-type]
                value=value,
                queried_at=queried_at,
                snapshot_label=snapshot_label,
                source=AMAP_SOURCE,
                evidence_ref=amap_evidence_ref(target.place_id, field_name),
            )
        )
    dropped = 0 if facts else 1
    return facts, poi_id, dropped


def _normalize_opening_value(raw: Any) -> str:
    if isinstance(raw, list) or not isinstance(raw, str):
        return ""
    return normalize_advice_text(raw)


def _aggregate_status(batch_statuses: list[str], *, has_facts: bool) -> str:
    if not batch_statuses:
        return STATUS_OK
    failures = [status for status in batch_statuses if status != STATUS_OK]
    if has_facts and failures:
        return STATUS_PARTIAL
    if has_facts:
        return STATUS_OK
    if failures:
        return failures[0]
    return STATUS_OK


def _log_status(status: str, *, batch_count: int, fact_count: int) -> None:
    logger.info(
        "amap_poi_detail status=%s batches=%s facts=%s",
        status,
        batch_count,
        fact_count,
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _snapshot_label(queried_at: str) -> str:
    if len(queried_at) >= 10 and queried_at[4] == "-":
        return f"{queried_at[:10]} 快照"
    return "当日快照"


def _result(
    *,
    status: str,
    t0: float,
    monotonic: Callable[[], float],
    facts: list[AmapTipFact] | None = None,
    batch_count: int = 0,
    batch_statuses: list[str] | None = None,
    authorized_fact_count: int = 0,
    skipped_no_poi_id: int = 0,
    unique_poi_count: int = 0,
    dropped_poi_count: int = 0,
) -> AmapPoiDetailResult:
    metrics = {
        "amap_poi_detail_status": status,
        "amap_poi_detail_batch_count": batch_count,
        "amap_poi_detail_batch_statuses": list(batch_statuses or []),
        "amap_poi_detail_elapsed_ms": max(0, int((monotonic() - t0) * 1000)),
        "amap_poi_detail_authorized_fact_count": authorized_fact_count,
        "amap_poi_detail_unique_poi_count": unique_poi_count,
        "amap_poi_detail_skipped_no_poi_id": skipped_no_poi_id,
        "amap_poi_detail_dropped_poi_count": dropped_poi_count,
    }
    return AmapPoiDetailResult(
        facts=list(facts or []),
        status=status,
        metrics=metrics,
    )
