"""Draw the published itinerary geometry without loading a map service."""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any


Point = tuple[float, float]
_WIDTH, _HEIGHT = 320, 180
_PADDING = 23


def _point(longitude: Any, latitude: Any) -> Point | None:
    try:
        lng, lat = float(longitude), float(latitude)
    except (TypeError, ValueError):
        return None
    if -180 <= lng <= 180 and -90 <= lat <= 90 and (lng, lat) != (0, 0):
        return lng, lat
    return None


def _amap_path(encoded: Any) -> list[Point]:
    # Amap sends semicolon-separated longitude,latitude pairs.
    if not isinstance(encoded, str):
        return []
    points = []
    for token in encoded.split(";"):
        pair = token.strip().split(",")
        if len(pair) == 2 and (point := _point(*pair)) is not None:
            points.append(point)
    return points


def route_sketch_svg(
    places: list[dict[str, Any]], legs: dict[tuple[int, int], dict[str, Any]],
) -> str:
    markers: dict[Point, list[str]] = defaultdict(list)
    stops = [_point(place.get("longitude"), place.get("latitude")) for place in places]
    for number, point in enumerate(stops, 1):
        if point is not None:
            markers[point].append(str(number))

    paths: list[tuple[list[Point], bool]] = []
    for index, (origin, destination) in enumerate(zip(places, places[1:])):
        leg = legs.get((int(origin["place_id"]), int(destination["place_id"])), {})
        geometry = _amap_path(leg.get("encoded_polyline"))
        if len(geometry) >= 2:
            paths.append((geometry, True))
        elif stops[index] is not None and stops[index + 1] is not None:
            paths.append(([stops[index], stops[index + 1]], False))

    all_points = [*markers, *(point for path, _ in paths for point in path)]
    if not all_points:
        return ""
    longitudes, latitudes = zip(*all_points)
    lng_center = (min(longitudes) + max(longitudes)) / 2
    lat_center = (min(latitudes) + max(latitudes)) / 2
    lng_scale = max(math.cos(math.radians(lat_center)), 0.001)
    lng_span = (max(longitudes) - min(longitudes)) * lng_scale
    lat_span = max(latitudes) - min(latitudes)
    scale = min(
        (_WIDTH - 2 * _PADDING) / max(lng_span, 0.000001),
        (_HEIGHT - 2 * _PADDING) / max(lat_span, 0.000001),
    )

    def xy(point: Point) -> Point:
        return (
            _WIDTH / 2 + (point[0] - lng_center) * lng_scale * scale,
            _HEIGHT / 2 - (point[1] - lat_center) * scale,
        )

    lines = []
    for path, actual in paths:
        points = " ".join(f"{x:.2f},{y:.2f}" for x, y in map(xy, path))
        dash = "" if actual else ' stroke-dasharray="5 4"'
        lines.append(f'<polyline points="{points}"{dash}/>')

    pins = []
    for point, numbers in markers.items():
        x, y = xy(point)
        label = "/".join(numbers)
        pins.append(
            f'<g transform="translate({x:.2f} {y:.2f})">'
            f'<ellipse rx="{max(9, len(label) * 3.2):g}" ry="9"/>'
            f'<text y="3.5">{label}</text></g>'
        )
    return (
        f'<svg viewBox="0 0 {_WIDTH} {_HEIGHT}" role="img" aria-label="当天路线示意图">'
        f'{"".join(lines)}{"".join(pins)}</svg>'
    )
