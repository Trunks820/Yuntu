"""Fetch one printable Amap static map for a published itinerary day."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from io import BytesIO
from typing import Any

import httpx
from PIL import Image

from src.agents.amap_keys import (
    STATICMAP_KEY_BUCKET,
    classify_amap_failure,
    configured_amap_keys,
    get_amap_key_coordinator,
)
from src.export.route_sketch import Point, _amap_path, _point


_STATICMAP_URL = httpx.URL("https://restapi.amap.com/v3/staticmap")
_TIMEOUT_SECONDS = 5.0
_OVER_LIMIT_SECONDS = 60.0
_MAX_REQUESTS = 7
_PATH_POINT_BUDGET = 120
_MAX_URL_BYTES = 4096
_MAP_IMAGE_BYTES = 128 * 1024
_JPEG_PNG = {"image/jpeg": "JPEG", "image/png": "PNG"}


class _StaticMapLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # HTTPX logs the full query string at INFO, including the key.
        args = record.args
        return not (
            isinstance(args, tuple) and len(args) >= 2
            and isinstance(url := args[1], httpx.URL)
            and (url.host, url.path) == (_STATICMAP_URL.host, _STATICMAP_URL.path)
        )


logging.getLogger("httpx").addFilter(_StaticMapLogFilter())


@dataclass(frozen=True)
class RouteMap:
    jpeg: bytes | None = None
    failure_code: str | None = None


def _coordinates(point: Point) -> str:
    return f"{point[0]:.6f},{point[1]:.6f}"


def _sample(path: list[Point], limit: int) -> list[Point]:
    if len(path) <= limit:
        return path
    last = len(path) - 1
    return [path[index * last // (limit - 1)] for index in range(limit)]


def _map_params(
    places: list[dict[str, Any]], legs: dict[tuple[int, int], dict[str, Any]],
) -> dict[str, str]:
    params = {"size": "416*240", "scale": "2"}
    markers, labels = [], []
    for index, place in enumerate(places, 1):
        point = _point(place.get("longitude"), place.get("latitude"))
        if point is None:
            continue
        coords = _coordinates(point)
        if index < 10:
            markers.append(f"mid,0x166C5B,{index}:{coords}")
        else:
            # Amap pin labels are single characters. Text labels support 10+.
            labels.append(f"{index},0,1,16,0xFFFFFF,0x166C5B:{coords}")
    if len(labels) > 10:
        raise ValueError("static map label limit")
    if markers:
        params["markers"] = "|".join(markers)
    if labels:
        params["labels"] = "|".join(labels)

    paths = [
        _amap_path(legs.get((int(origin["place_id"]), int(destination["place_id"])), {}).get("encoded_polyline"))
        for origin, destination in zip(places, places[1:])
    ]
    path_count = sum(len(path) >= 2 for path in paths)
    points_per_leg = _PATH_POINT_BUDGET // max(1, path_count)
    if points_per_leg < 2:
        raise ValueError("static map point limit")

    # Join adjacent routed legs at their shared stop. Keep gaps for legs with
    # no published geometry so the map never draws an estimated road segment.
    runs: list[list[Point]] = []
    previous_routed = False
    for path in paths:
        routed = len(path) >= 2
        if routed:
            points = _sample(path, points_per_leg)
            if previous_routed:
                previous = runs[-1]
                previous.extend(points[1:] if previous[-1] == points[0] else points)
            else:
                runs.append(points)
        previous_routed = routed
    if len(runs) > 4:
        raise ValueError("static map path limit")
    if runs:
        params["paths"] = "|".join(
            "4,0x166C5B,1,,:" + ";".join(map(_coordinates, run)) for run in runs
        )
    return params


def _response_image(response: httpx.Response, image_format: str) -> RouteMap:
    try:
        # Re-encode to bound PDF size and discard upstream image metadata.
        with Image.open(BytesIO(response.content), formats=[image_format]) as image:
            image.thumbnail((832, 480), Image.Resampling.LANCZOS)
            rgb = image.convert("RGB")
        for quality in (85, 70):
            output = BytesIO()
            rgb.save(output, "JPEG", quality=quality, optimize=True)
            if output.tell() <= _MAP_IMAGE_BYTES:
                return RouteMap(jpeg=output.getvalue())
    except (OSError, ValueError, Image.DecompressionBombError):
        return RouteMap(failure_code="invalid_image")
    return RouteMap(failure_code="image_size_limit")


class AmapRouteMaps:
    """Share a request budget across all days of one PDF render."""

    def __init__(self) -> None:
        self._remaining = _MAX_REQUESTS

    def fetch(
        self, places: list[dict[str, Any]], legs: dict[tuple[int, int], dict[str, Any]],
    ) -> RouteMap:
        if not self._remaining:
            return RouteMap(failure_code="request_limit")
        keys = configured_amap_keys()
        if not keys:
            return RouteMap(failure_code="no_key")
        try:
            params = _map_params(places, legs)
        except ValueError:
            return RouteMap(failure_code="geometry_limit")
        if not any(name in params for name in ("markers", "labels", "paths")):
            return RouteMap(failure_code="no_geometry")
        coordinator = get_amap_key_coordinator()
        key = coordinator.next_key(keys, bucket=STATICMAP_KEY_BUCKET, mode=STATICMAP_KEY_BUCKET)
        if key is None:
            return RouteMap(failure_code="no_usable_key")
        url = _STATICMAP_URL.copy_merge_params({**params, "key": key})
        if len(str(url)) > _MAX_URL_BYTES:
            return RouteMap(failure_code="geometry_limit")

        self._remaining -= 1
        try:
            response = httpx.get(url, timeout=_TIMEOUT_SECONDS, follow_redirects=False)
        except httpx.TimeoutException:
            return RouteMap(failure_code="timeout")
        except httpx.HTTPError:
            return RouteMap(failure_code="request_failed")

        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        image_format = _JPEG_PNG.get(content_type)
        if response.is_success and image_format:
            return _response_image(response, image_format)
        try:
            payload = response.json()
        except ValueError:
            payload = None
        failure = classify_amap_failure(payload) if isinstance(payload, dict) else None
        if failure == "quota":
            coordinator.mark_quota_exhausted(key, bucket=STATICMAP_KEY_BUCKET)
            return RouteMap(failure_code="quota_exhausted")
        if failure == "over_limit" or response.status_code == 429:
            coordinator.mark_over_limit(key, STATICMAP_KEY_BUCKET, _OVER_LIMIT_SECONDS)
            return RouteMap(failure_code="over_limit")
        return RouteMap(failure_code="not_image" if response.is_success else "http_error")
