"""Amap-backed POI resolution for the v0.6 data pipeline.

This module resolves normalized ``travel_place`` rows, then lets
``refresh_summary`` inherit the stored coordinates into
``travel_place_summary``. ``travel_raw_item`` v0.6 fields are still updated as
best-effort trace metadata for linked raw notes, but they are not the canonical
place-location store because one raw note can mention multiple places.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

import httpx
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.config import get_settings
from src.pipeline.db import get_session_factory
from src.pipeline.place_merge import lock_identity, lock_places, merge_source_places, group_snapshot, planner_visible

logger = logging.getLogger(__name__)
# Amap requires the API key in the query string. Keep httpx request logging
# above INFO so credentials never appear in normal command output.
logging.getLogger("httpx").setLevel(logging.WARNING)

MAX_POI_RESOLUTION_ATTEMPTS = 3
AMAP_BASE_URL = "https://restapi.amap.com"

_ADDRESS_MARKERS = ("路", "街", "巷", "号", "道", "大道", "支路", "环路")
_VAGUE_MARKERS = ("附近", "那家", "这家", "某家", "一家", "有家", "旁边")
_DISTRICT_SUFFIXES = ("省", "市", "区", "县", "镇", "乡", "街道")


class AmapTransientError(RuntimeError):
    """Raised for retryable Amap failures such as timeout, rate limit, or 5xx."""


class AmapDefinitiveNotFound(RuntimeError):
    """Raised when Amap definitively returns no result."""


class AmapQuotaError(RuntimeError):
    """Quota exhaustion stops dispatch without resolution persistence."""


@dataclass(frozen=True)
class GeocodeResult:
    formatted_address: str
    longitude: float
    latitude: float
    adcode: str | None = None
    province: str | None = None
    city: str | None = None
    district: str | None = None
    level: str | None = None
    match_count: int = 1


@dataclass(frozen=True)
class PoiCandidate:
    poi_id: str
    name: str
    type: str | None
    address: str | None
    longitude: float
    latitude: float
    adcode: str | None = None


@dataclass(frozen=True)
class DistrictRecord:
    adcode: str
    name: str
    level: str
    parent_code: str | None
    center_lng: float | None
    center_lat: float | None
    citycode: str | None = None


@dataclass(frozen=True)
class PoiResolution:
    status: str
    place_type_resolved: str | None = None
    amap_poi_id: str | None = None
    longitude: float | None = None
    latitude: float | None = None
    adcode: str | None = None
    address: str | None = None
    alternatives_count: int | None = None
    last_error: str | None = None
    retryable: bool = False


@dataclass(frozen=True)
class PlaceResolutionTarget:
    place_id: int
    city: str
    name: str
    attempts: int = 0


class AmapClientProtocol(Protocol):
    async def geocode(self, *, city: str, address: str) -> GeocodeResult:
        ...

    async def search_poi(
        self,
        *,
        city: str,
        keywords: str,
        longitude: float | None = None,
        latitude: float | None = None,
        radius: int = 1000,
    ) -> list[PoiCandidate]:
        ...


def _parse_location(value: str) -> tuple[float, float]:
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 2:
        raise ValueError(f"invalid Amap location: {value!r}")
    return float(parts[0]), float(parts[1])


def _as_text(value) -> str | None:
    if value is None or value == []:
        return None
    if isinstance(value, list):
        value = next((item for item in value if item), None)
    if value is None:
        return None
    text_value = str(value).strip()
    return text_value or None


def _looks_like_vague_place_name(place_name: str) -> bool:
    compact = re.sub(r"\s+", "", place_name)
    if not compact:
        return True
    if any(marker in compact for marker in _VAGUE_MARKERS):
        return True
    return len(compact) <= 4 and compact.endswith(_DISTRICT_SUFFIXES)


def _strip_admin_prefix(address: str) -> str:
    value = address.strip()
    for marker in ("省", "市", "区", "县"):
        index = value.find(marker)
        if index >= 0 and index + 1 < len(value):
            value = value[index + 1:]
    return value


def _is_concrete_address(address: str) -> bool:
    return any(marker in address for marker in _ADDRESS_MARKERS)


def _landmark_matches(address: str, place_name: str) -> bool:
    address_tail = _strip_admin_prefix(address)
    normalized_address = re.sub(r"\s+", "", address_tail)
    normalized_place = re.sub(r"\s+", "", place_name)
    if len(normalized_place) < 2 or len(normalized_address) < 2:
        return False
    return normalized_place in normalized_address or normalized_address in normalized_place


def _adcode_matches_city(adcode: str, request_city: str, resolved_city: str | None) -> bool:
    """Check if the resolved adcode belongs to the request city to prevent cross-city pollution."""
    # If resolved_city is available and matches, trust it
    if resolved_city:
        # Normalize city names (e.g., "重庆市" vs "重庆")
        req_norm = request_city.rstrip("市")
        res_norm = resolved_city.rstrip("市")
        if req_norm == res_norm:
            return True

    # Known city-level adcode prefixes (first 4 digits)
    # Some cities have multiple prefix ranges (e.g. Chongqing: 5001xx main districts + 5002xx outer counties)
    CITY_ADCODE_PREFIXES = {
        "重庆": ["5001", "5002"],  # 主城区 + 郊县区
        "成都": ["5101"],
        "北京": ["1101"],
        "上海": ["3101"],
        "天津": ["1201"],
        "广州": ["4401"],
        "深圳": ["4403"],
        "杭州": ["3301"],
        "西安": ["6101"],
        "武汉": ["4201"],
    }

    expected_prefixes = CITY_ADCODE_PREFIXES.get(request_city.rstrip("市"))
    if expected_prefixes:
        for prefix in expected_prefixes:
            if adcode.startswith(prefix):
                return True

    # If we don't have a prefix mapping and resolved_city doesn't match, reject
    if resolved_city and resolved_city.rstrip("市") != request_city.rstrip("市"):
        return False

    # No evidence of mismatch, allow it
    return True


class AmapClient:
    """Small async wrapper around Amap APIs used by the v0.6 pipeline."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        search_api_key: str | None = None,
        geocode_timeout: float | None = None,
        poi_search_timeout: float | None = None,
        qps: float | None = None,
    ) -> None:
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.amap_api_key
        configured_search_key = (
            search_api_key if search_api_key is not None else settings.amap_api_key_two
        )
        self.search_api_key = configured_search_key or self.api_key
        self.geocode_timeout = (
            geocode_timeout
            if geocode_timeout is not None
            else settings.amap_geocode_timeout
        )
        self.poi_search_timeout = (
            poi_search_timeout
            if poi_search_timeout is not None
            else settings.amap_poi_search_timeout
        )
        configured_qps = qps if qps is not None else settings.amap_poi_qps
        self.min_interval = 1 / max(configured_qps, 0.1)
        self._last_request_started = 0.0
        self._rate_lock = asyncio.Lock()
        self._client = httpx.AsyncClient(base_url=AMAP_BASE_URL)

    async def close(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _require_key(api_key: str, env_name: str) -> str:
        if not api_key:
            raise AmapTransientError(f"{env_name} is not configured")
        return api_key

    async def _wait_for_rate_limit(self) -> None:
        async with self._rate_lock:
            wait_seconds = (
                self._last_request_started + self.min_interval - time.monotonic()
            )
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)
            self._last_request_started = time.monotonic()

    async def _get_json(
        self,
        path: str,
        *,
        params: dict,
        timeout: float,
        api_key: str | None = None,
        env_name: str = "AMAP_API_KEY",
    ) -> dict:
        params = {
            **params,
            "key": self._require_key(api_key if api_key is not None else self.api_key, env_name),
            "output": "JSON",
        }
        await self._wait_for_rate_limit()
        try:
            response = await self._client.get(path, params=params, timeout=timeout)
            response.raise_for_status()
        except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as exc:
            raise AmapTransientError(str(exc)) from exc

        payload = response.json()
        if str(payload.get("infocode") or "") in {"10003", "10044"}:
            raise AmapQuotaError(f"Amap quota exhausted ({payload['infocode']})")
        if payload.get("status") != "1":
            info = payload.get("info") or "Amap API failed"
            infocode = str(payload.get("infocode") or "")
            if infocode in {"10000"}:
                raise AmapDefinitiveNotFound(info)
            raise AmapTransientError(f"{info} ({infocode})")
        return payload

    async def geocode(self, *, city: str, address: str) -> GeocodeResult:
        payload = await self._get_json(
            "/v3/geocode/geo",
            params={"city": city, "address": address},
            timeout=self.geocode_timeout,
        )
        geocodes = payload.get("geocodes") or []
        if not geocodes:
            raise AmapDefinitiveNotFound("geocode returned no result")
        item = geocodes[0]
        location = _as_text(item.get("location"))
        if not location:
            raise AmapDefinitiveNotFound("geocode returned no location")
        longitude, latitude = _parse_location(location)
        return GeocodeResult(
            formatted_address=_as_text(item.get("formatted_address")) or "",
            longitude=longitude,
            latitude=latitude,
            adcode=_as_text(item.get("adcode")),
            province=_as_text(item.get("province")),
            city=_as_text(item.get("city")),
            district=_as_text(item.get("district")),
            level=_as_text(item.get("level")),
            match_count=len(geocodes),
        )

    async def search_poi(
        self,
        *,
        city: str,
        keywords: str,
        longitude: float | None = None,
        latitude: float | None = None,
        radius: int = 1000,
    ) -> list[PoiCandidate]:
        params = {
            "city": city,
            "keywords": keywords,
            "offset": 10,
            "page": 1,
            "extensions": "base",
        }
        if longitude is not None and latitude is not None:
            # Location-biased search around a geocoded point.
            params["location"] = f"{longitude},{latitude}"
            params["radius"] = radius
        else:
            # Name-only fallback: restrict strictly to the requested city.
            params["citylimit"] = "true"
        payload = await self._get_json(
            "/v3/place/text",
            params=params,
            timeout=self.poi_search_timeout,
            api_key=self.search_api_key,
            env_name="AMAP_API_KEY_TWO",
        )
        pois = []
        for item in payload.get("pois") or []:
            location = _as_text(item.get("location"))
            poi_id = _as_text(item.get("id"))
            if not location or not poi_id:
                continue
            item_lng, item_lat = _parse_location(location)
            pois.append(
                PoiCandidate(
                    poi_id=poi_id,
                    name=_as_text(item.get("name")) or "",
                    type=_as_text(item.get("type")),
                    address=_as_text(item.get("address")),
                    longitude=item_lng,
                    latitude=item_lat,
                    adcode=_as_text(item.get("adcode")),
                )
            )
        return pois

    async def fetch_districts(
        self,
        *,
        keywords: str,
        subdistrict: int = 1,
        extensions: str = "base",
    ) -> list[DistrictRecord]:
        payload = await self._get_json(
            "/v3/config/district",
            params={
                "keywords": keywords,
                "subdistrict": subdistrict,
                "extensions": extensions,
            },
            timeout=self.geocode_timeout,
        )
        records: list[DistrictRecord] = []

        def visit(items: list[dict], parent_code: str | None = None) -> None:
            for item in items:
                adcode = _as_text(item.get("adcode"))
                name = _as_text(item.get("name"))
                level = _as_text(item.get("level"))
                if adcode and name and level:
                    center = _as_text(item.get("center"))
                    center_lng = center_lat = None
                    if center:
                        try:
                            center_lng, center_lat = _parse_location(center)
                        except ValueError:
                            center_lng = center_lat = None
                    records.append(
                        DistrictRecord(
                            adcode=adcode,
                            name=name,
                            level=level,
                            parent_code=parent_code,
                            center_lng=center_lng,
                            center_lat=center_lat,
                            citycode=_as_text(item.get("citycode")),
                        )
                    )
                children = item.get("districts") or []
                visit(children, adcode or parent_code)

        visit(payload.get("districts") or [])
        return records


def _poi_name_matches(query: str, candidate_name: str) -> bool:
    """Guard for the POI-search fallback: the candidate's name must overlap the
    query, so generic extractions ("酒店", "火车") don't grab an arbitrary POI."""
    q = re.sub(r"\s+", "", query)
    c = re.sub(r"\s+", "", candidate_name)
    if len(q) < 3 or len(c) < 2:
        return False
    return q in c or c in q


async def _resolve_via_geocode(
    *,
    city: str,
    place_name: str,
    client: AmapClientProtocol,
) -> PoiResolution:
    """Geocode-first strategy: treat the name as an address, then refine with a
    location-biased POI search. Returns unresolvable/retryable on failure."""
    try:
        geocode = await client.geocode(city=city, address=place_name)
    except AmapDefinitiveNotFound as exc:
        return PoiResolution(status="unresolvable", last_error=str(exc))
    except AmapTransientError as exc:
        return PoiResolution(status="unresolvable", last_error=str(exc), retryable=True)

    try:
        pois = await client.search_poi(
            city=city,
            keywords=place_name,
            longitude=geocode.longitude,
            latitude=geocode.latitude,
        )
    except AmapDefinitiveNotFound:
        pois = []
    except AmapTransientError as exc:
        return PoiResolution(status="unresolvable", last_error=str(exc), retryable=True)

    if pois:
        best = pois[0]
        # Verify the resolved city matches the request city to prevent cross-city pollution
        resolved_adcode = best.adcode or geocode.adcode
        if resolved_adcode and not _adcode_matches_city(resolved_adcode, city, geocode.city):
            return PoiResolution(
                status="unresolvable",
                last_error=f"resolved adcode {resolved_adcode} does not match request city {city}",
            )
        return PoiResolution(
            status="resolved",
            place_type_resolved="precise",
            amap_poi_id=best.poi_id,
            longitude=best.longitude,
            latitude=best.latitude,
            adcode=resolved_adcode,
            address=best.address or geocode.formatted_address,
            alternatives_count=len(pois),
        )

    if (
        _is_concrete_address(geocode.formatted_address)
        or _landmark_matches(geocode.formatted_address, place_name)
    ):
        # Verify the resolved city matches the request city
        if geocode.adcode and not _adcode_matches_city(geocode.adcode, city, geocode.city):
            return PoiResolution(
                status="unresolvable",
                last_error=f"geocode adcode {geocode.adcode} does not match request city {city}",
            )
        return PoiResolution(
            status="resolved",
            place_type_resolved="area",
            longitude=geocode.longitude,
            latitude=geocode.latitude,
            adcode=geocode.adcode,
            address=geocode.formatted_address,
            alternatives_count=0,
        )

    return PoiResolution(status="unresolvable", last_error="geocode result is too broad")


async def _resolve_via_city_poi_search(
    *,
    city: str,
    place_name: str,
    client: AmapClientProtocol,
) -> PoiResolution | None:
    """Fallback for names geocode can't handle (e.g. shop names returning 30001):
    search Amap's POI database by name, restricted to the city (citylimit=true).
    Returns a resolved PoiResolution for the first name-matching candidate, else
    None so the caller can keep the original (often retryable) failure."""
    try:
        pois = await client.search_poi(city=city, keywords=place_name)
    except (AmapDefinitiveNotFound, AmapTransientError):
        return None

    for candidate in pois:
        if not _poi_name_matches(place_name, candidate.name):
            continue
        return PoiResolution(
            status="resolved",
            place_type_resolved="precise",
            amap_poi_id=candidate.poi_id,
            longitude=candidate.longitude,
            latitude=candidate.latitude,
            adcode=candidate.adcode,
            address=candidate.address,
            alternatives_count=len(pois),
        )
    return None


async def resolve_place(
    *,
    city: str,
    place_name: str,
    client: AmapClientProtocol,
) -> PoiResolution:
    """Resolve one place name into coordinates and Amap metadata.

    Geocode-first; if geocode fails, is rejected as cross-city, or is too broad,
    fall back to a city-scoped POI text search by name. Vague/administrative-only
    names are rejected upfront without an API call.
    """
    if _looks_like_vague_place_name(place_name):
        return PoiResolution(
            status="unresolvable",
            last_error="place name is vague or administrative-only",
        )

    primary = await _resolve_via_geocode(city=city, place_name=place_name, client=client)
    if primary.status == "resolved":
        return primary

    fallback = await _resolve_via_city_poi_search(
        city=city, place_name=place_name, client=client
    )
    if fallback is not None:
        return fallback

    return primary


def apply_retry_policy(
    resolution: PoiResolution,
    *,
    current_attempts: int,
    max_attempts: int = MAX_POI_RESOLUTION_ATTEMPTS,
) -> tuple[PoiResolution, int]:
    """Convert transient failures into pending retry or terminal unresolvable."""
    if not resolution.retryable:
        return resolution, current_attempts

    next_attempts = current_attempts + 1
    if next_attempts >= max_attempts:
        return (
            PoiResolution(
                status="unresolvable",
                last_error=resolution.last_error,
                retryable=False,
            ),
            next_attempts,
        )
    return (
        PoiResolution(
            status="pending",
            last_error=resolution.last_error,
            retryable=True,
        ),
        next_attempts,
    )


async def fetch_place_resolution_targets(
    *,
    city: str,
    limit: int,
    force: bool = False,
) -> list[PlaceResolutionTarget]:
    factory = get_session_factory()
    where_status = ""
    if not force:
        where_status = """
          AND (
            p.poi_resolution_status IS NULL
            OR (
              p.poi_resolution_status = 'pending'
              AND COALESCE(p.poi_resolution_attempts, 0) < :max_attempts
            )
          )
        """
    async with factory() as session:
        rows = (await session.execute(text(f"""
            SELECT p.id, p.city, p.name, COALESCE(p.poi_resolution_attempts, 0) AS attempts
            FROM travel_place AS p
            WHERE p.city = :city
              AND p.merged_into_place_id IS NULL
              AND p.poi_resolution_status IS DISTINCT FROM 'resolved'
              {where_status}
            ORDER BY p.updated_time ASC, p.id ASC
            LIMIT :limit
        """), {
            "city": city,
            "limit": limit,
            "max_attempts": MAX_POI_RESOLUTION_ATTEMPTS,
        })).all()
    return [
        PlaceResolutionTarget(
            place_id=int(row.id),
            city=row.city,
            name=row.name,
            attempts=int(row.attempts or 0),
        )
        for row in rows
    ]


async def fetch_summary_backfill_targets(
    *,
    city: str,
    limit: int,
    force: bool = False,
) -> list[PlaceResolutionTarget]:
    factory = get_session_factory()
    where_status = ""
    if not force:
        where_status = """
          AND (
            s.latitude IS NULL
            OR s.longitude IS NULL
            OR s.adcode IS NULL
            OR p.poi_resolution_status = 'pending'
          )
        """
    async with factory() as session:
        rows = (await session.execute(text(f"""
            SELECT p.id, p.city, p.name, COALESCE(p.poi_resolution_attempts, 0) AS attempts
            FROM travel_place_summary AS s
            JOIN travel_place AS p ON p.id = s.place_id
            WHERE s.city = :city
              AND p.merged_into_place_id IS NULL
              AND p.poi_resolution_status IS DISTINCT FROM 'resolved'
              {where_status}
            ORDER BY s.recommend_score DESC, p.id ASC
            LIMIT :limit
        """), {
            "city": city,
            "limit": limit,
        })).all()
    return [
        PlaceResolutionTarget(
            place_id=int(row.id),
            city=row.city,
            name=row.name,
            attempts=int(row.attempts or 0),
        )
        for row in rows
    ]


async def _save_place_resolution(session,
    *,
    target: PlaceResolutionTarget,
    resolution: PoiResolution,
    attempts: int,
) -> None:
    resolved_at = datetime.now(timezone.utc) if resolution.status == "resolved" else None
    updated = await session.execute(text("""
        UPDATE travel_place
        SET address = COALESCE(:address, address),
            longitude = :longitude,
            latitude = :latitude,
            amap_poi_id = :amap_poi_id,
            adcode = :adcode,
            place_type_resolved = :place_type_resolved,
            poi_resolution_status = :status,
            poi_resolution_attempts = :attempts,
            poi_resolution_last_error = :last_error,
            poi_alternatives_count = :alternatives_count,
            poi_resolved_at = :resolved_at,
            updated_time = NOW()
        WHERE id = :place_id AND merged_into_place_id IS NULL
    """), {
        "place_id": target.place_id,
        "address": resolution.address,
        "longitude": resolution.longitude,
        "latitude": resolution.latitude,
        "amap_poi_id": resolution.amap_poi_id,
        "adcode": resolution.adcode,
        "place_type_resolved": resolution.place_type_resolved,
        "status": None if resolution.status == "pending" else resolution.status,
        "attempts": attempts,
        "last_error": resolution.last_error,
        "alternatives_count": resolution.alternatives_count,
        "resolved_at": resolved_at,
    })
    if updated.rowcount != 1:
        return False
    await session.execute(text("""
        UPDATE travel_raw_item AS raw
        SET amap_poi_id = :amap_poi_id,
            longitude = :longitude,
            latitude = :latitude,
            adcode = :adcode,
            place_type_resolved = :place_type_resolved,
            poi_resolution_status = :raw_status,
            poi_resolution_attempts = GREATEST(
                COALESCE(raw.poi_resolution_attempts, 0),
                :attempts
            ),
            poi_resolution_last_error = :last_error,
            poi_alternatives_count = :alternatives_count,
            poi_resolved_at = :resolved_at
        FROM travel_content AS content
        JOIN travel_content_place_mention AS mention
          ON mention.content_id = content.id
        WHERE raw.id = content.raw_item_id
          AND mention.place_id = :place_id
          AND (
            raw.poi_resolution_status IS NULL
            OR raw.poi_resolution_status <> 'resolved'
            OR :raw_status = 'resolved'
          )
    """), {
        "place_id": target.place_id,
        "longitude": resolution.longitude,
        "latitude": resolution.latitude,
        "amap_poi_id": resolution.amap_poi_id,
        "adcode": resolution.adcode,
        "place_type_resolved": resolution.place_type_resolved,
        "raw_status": None if resolution.status == "pending" else resolution.status,
        "attempts": attempts,
        "last_error": resolution.last_error,
        "alternatives_count": resolution.alternatives_count,
        "resolved_at": resolved_at,
    })
    return True


async def save_place_resolution(*, target: PlaceResolutionTarget,
                                resolution: PoiResolution, attempts: int) -> bool:
    """Savepoint retries are observable; the session owner commits once.

    Always acquire provider lock before group row locks, including when the
    fold is disabled. Resolved rows are never force re-queried by window tools.
    """
    async with get_session_factory()() as session:
        for retry in range(3):
            try:
                async with session.begin_nested():
                    if resolution.amap_poi_id:
                        await lock_identity(session, target.city, resolution.amap_poi_id)
                        ids = list((await session.execute(text("""SELECT id FROM travel_place
                            WHERE city=:city AND amap_poi_id=:amap AND merged_into_place_id IS NULL"""),
                            {"city": target.city, "amap": resolution.amap_poi_id})).scalars())
                    else:
                        ids = []
                    rows = await lock_places(session, [target.place_id, *ids])
                    source = next((r for r in rows if r['id'] == target.place_id), None)
                    if source is None or source['city'] != target.city or source['merged_into_place_id'] is not None:
                        return False
                    if not await _save_place_resolution(session, target=target, resolution=resolution, attempts=attempts):
                        return False
                    if (get_settings().place_same_amap_fold_enabled and resolution.status == 'resolved'
                            and resolution.amap_poi_id and ids):
                        members = sorted(set([target.place_id, *ids]))
                        snapshot = await group_snapshot(session, members)
                        summaries = {r['place_id']: r['canonical_place_id'] for r in snapshot['travel_place_summary']}
                        visible = {c['place_id'] for c in snapshot['travel_canonical_place'] if planner_visible(c)}
                        counts = {i: sum(r['place_id'] == i for table in ('travel_content_place_mention','travel_place_fact')
                                        for r in snapshot[table]) for i in members}
                        survivor = max(members, key=lambda i: (summaries.get(i) in visible, counts[i], -i))
                        await merge_source_places(session, city=target.city, amap_poi_id=resolution.amap_poi_id,
                            survivor_id=survivor, loser_ids=[i for i in members if i != survivor], batch_id='auto')
                await session.commit()
                return True
            except DBAPIError as exc:
                state = getattr(exc.orig, 'sqlstate', None) or getattr(exc.orig, 'pgcode', None)
                if state != '40P01' or retry == 2:
                    raise
                logger.warning('D1 savepoint deadlock retry place_id=%s retry=%s', target.place_id, retry + 1)
                await asyncio.sleep(0.01 * (retry + 1))
    return False


async def resolve_targets(
    targets: list[PlaceResolutionTarget],
    *,
    client: AmapClientProtocol,
) -> dict[str, int]:
    stats = {"resolved": 0, "unresolvable": 0, "pending": 0}
    for target in targets:
        raw_resolution = await resolve_place(
            city=target.city,
            place_name=target.name,
            client=client,
        )
        resolution, attempts = apply_retry_policy(
            raw_resolution,
            current_attempts=target.attempts,
        )
        await save_place_resolution(
            target=target,
            resolution=resolution,
            attempts=attempts,
        )
        if resolution.status in stats:
            stats[resolution.status] += 1
        logger.info(
            "POI resolved place_id=%s name=%s status=%s attempts=%s",
            target.place_id,
            target.name,
            resolution.status,
            attempts,
        )
    return stats


async def save_district_records(records: list[DistrictRecord]) -> int:
    if not records:
        return 0
    factory = get_session_factory()
    async with factory() as session:
        for record in records:
            await session.execute(text("""
                INSERT INTO travel_amap_district (
                    adcode, name, level, parent_code, center_lng, center_lat,
                    citycode, synced_at
                ) VALUES (
                    :adcode, :name, :level, :parent_code, :center_lng,
                    :center_lat, :citycode, NOW()
                )
                ON CONFLICT (adcode) DO UPDATE SET
                    name = EXCLUDED.name,
                    level = EXCLUDED.level,
                    parent_code = EXCLUDED.parent_code,
                    center_lng = EXCLUDED.center_lng,
                    center_lat = EXCLUDED.center_lat,
                    citycode = EXCLUDED.citycode,
                    synced_at = NOW()
            """), {
                "adcode": record.adcode,
                "name": record.name,
                "level": record.level,
                "parent_code": record.parent_code,
                "center_lng": record.center_lng,
                "center_lat": record.center_lat,
                "citycode": record.citycode,
            })
        await session.commit()
    return len(records)
