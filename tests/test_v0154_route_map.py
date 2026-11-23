"""Use offline Amap responses to exercise the PDF map request boundary."""

import logging
from io import BytesIO

import httpx
import pytest
from PIL import Image, PngImagePlugin

from src.agents.amap_keys import (
    POI_DETAIL_KEY_BUCKET,
    ROUTE_KEY_BUCKET,
    SEARCH_KEY_BUCKET,
    STATICMAP_KEY_BUCKET,
    AmapKeyCoordinator,
)
from src.export import route_map


PLACES = [
    {"place_id": 1, "longitude": 114.0, "latitude": 30.1},
    {"place_id": 2, "longitude": 114.01, "latitude": 30.1},
    {"place_id": 3},
    {"place_id": 4, "longitude": 114.02, "latitude": 30.1},
]
KEYS = ["fixture-map-key-first", "fixture-map-key-second"]


@pytest.fixture
def coordinator(monkeypatch):
    coordinator = AmapKeyCoordinator()
    monkeypatch.setattr(route_map, "get_amap_key_coordinator", lambda: coordinator)
    monkeypatch.setattr(route_map, "configured_amap_keys", lambda: KEYS)
    return coordinator


def test_map_sends_numbered_stops_and_bounded_routes(coordinator, monkeypatch, caplog):
    requests = []
    output = BytesIO()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("upstream metadata", KEYS[0])
    Image.new("RGB", (416, 240), "white").save(output, "PNG", pnginfo=metadata)

    def respond(request):
        requests.append(request)
        return httpx.Response(200, headers={"Content-Type": "image/png"}, content=output.getvalue())

    def get(*args, timeout, follow_redirects):
        assert timeout == 5.0 and not follow_redirects
        with httpx.Client(transport=httpx.MockTransport(respond), timeout=timeout) as client:
            return client.get(*args)

    monkeypatch.setattr(route_map.httpx, "get", get)
    legs = {
        (1, 2): {"encoded_polyline": ";".join(f"{114 + i / 25000:.8f},30.1" for i in range(251))},
        (3, 4): {"encoded_polyline": "114.015,30.11;114.02,30.1"},
    }
    with caplog.at_level(logging.INFO, logger="httpx"):
        result = route_map.AmapRouteMaps().fetch(PLACES, legs)
    assert result.failure_code is None
    with Image.open(BytesIO(result.jpeg)) as image:
        assert image.format == "JPEG"
    request, = requests
    assert len(str(request.url)) <= 4096
    assert KEYS[0] not in caplog.text
    assert KEYS[0].encode() not in result.jpeg
    params = request.url.params
    assert params["scale"] == "2"
    assert [marker.split(":")[0].split(",")[-1] for marker in params["markers"].split("|")] == ["1", "2", "4"]
    first, second = [path.split(":")[1].split(";") for path in params["paths"].split("|")]
    assert len(first) < 251 and len(first) + len(second) <= 120
    assert first[0] == "114.000000,30.100000" and first[-1] == "114.010000,30.100000"
    assert second == ["114.015000,30.110000", "114.020000,30.100000"]


@pytest.mark.parametrize("infocode", [10003, "10044", "10045"])
def test_quota_is_recorded_without_retrying_day(coordinator, monkeypatch, infocode):
    called = []

    def get(url, **kwargs):
        called.append(url.params["key"])
        return httpx.Response(200, json={"infocode": infocode})

    monkeypatch.setattr(route_map.httpx, "get", get)
    maps = route_map.AmapRouteMaps()
    assert maps.fetch(PLACES, {}).failure_code == "quota_exhausted"
    assert called == KEYS[:1]
    assert coordinator.usable_keys(KEYS, bucket=STATICMAP_KEY_BUCKET) == KEYS[1:]
    assert coordinator.next_key(KEYS, bucket=ROUTE_KEY_BUCKET) == KEYS[0]
    assert coordinator.usable_keys(KEYS, bucket=SEARCH_KEY_BUCKET) == KEYS
    assert coordinator.usable_keys(KEYS, bucket=POI_DETAIL_KEY_BUCKET) == KEYS
    assert maps.fetch(PLACES, {}).failure_code == "quota_exhausted"
    assert maps.fetch(PLACES, {}).failure_code == "no_usable_key"
    assert called == KEYS


@pytest.mark.parametrize(("response", "failure_code"), [
    (httpx.ReadTimeout("timed out"), "timeout"),
    (httpx.ConnectError("connection failed"), "request_failed"),
    (httpx.Response(503), "http_error"),
    (httpx.Response(200, json={"infocode": "20000"}), "not_image"),
    (httpx.Response(200, headers={"Content-Type": "image/jpeg"}, content=b"invalid"), "invalid_image"),
    (httpx.Response(429, json={"infocode": "10019"}), "over_limit"),
])
def test_request_failure_codes(coordinator, monkeypatch, response, failure_code):
    def get(*args, **kwargs):
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(route_map.httpx, "get", get)
    assert route_map.AmapRouteMaps().fetch(PLACES, {}) == route_map.RouteMap(failure_code=failure_code)
    if failure_code == "over_limit":
        assert coordinator.usable_keys(KEYS, bucket=STATICMAP_KEY_BUCKET, mode=STATICMAP_KEY_BUCKET) == KEYS[1:]


def test_missing_key_and_pdf_request_cap(coordinator, monkeypatch):
    attempts = []

    def get(*args, **kwargs):
        attempts.append(1)
        return httpx.Response(200, json={})

    monkeypatch.setattr(route_map.httpx, "get", get)
    maps = route_map.AmapRouteMaps()
    monkeypatch.setattr(route_map, "configured_amap_keys", lambda: [])
    assert maps.fetch(PLACES, {}).failure_code == "no_key"
    assert not attempts
    monkeypatch.setattr(route_map, "configured_amap_keys", lambda: KEYS)
    assert maps.fetch([{"place_id": 1}], {}).failure_code == "no_geometry"
    assert [maps.fetch(PLACES, {}).failure_code for _ in range(8)] == ["not_image"] * 7 + ["request_limit"]
    assert len(attempts) == 7
