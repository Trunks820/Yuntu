"""Shared city-background generation for PDF and share-image artifacts."""

from __future__ import annotations

import base64
import re
import threading
import time
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Protocol

from PIL import Image, ImageDraw, ImageFilter
from src.export.model_call_observation import capture_image_response, image_channel, observe_image_call


FALLBACK_BACKGROUND_PATH = Path(__file__).resolve().parent / "assets" / "share_fallback.png"
CITY_BACKGROUND_ASSET_DIR = Path(__file__).resolve().parent / "assets" / "city_backgrounds"
CITY_BACKGROUND_ASSET_KEYS = {
    "北京": "beijing",
    "上海": "shanghai",
    "重庆": "chongqing",
    "成都": "chengdu",
    "杭州": "hangzhou",
    "西安": "xian",
    "南京": "nanjing",
    "长沙": "changsha",
    "青岛": "qingdao",
    "桂林": "guilin",
}
IMAGE_MAX_DIMENSION = 4096
IMAGE_MAX_PIXELS = 16_000_000
# v0.15 W21: one retry after a 5xx or a dropped connection, only when this much
# of the export's time limit is left. Official flare took 38-144s on 11-10.
IMAGE_RETRY_MIN_SECONDS = 150.0
# After a failed export, later exports skip the model for this long and use the
# packaged background at once instead of each waiting on a broken account.
IMAGE_FAILURE_COOLDOWN_SECONDS = 600.0


@dataclass(frozen=True)
class GeneratedCityBackground:
    image_bytes: bytes
    metadata: dict[str, Any]


class BackgroundGenerationError(RuntimeError):
    def __init__(self, error_code: str, metadata: dict[str, Any]) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.metadata = metadata


class CityBackgroundClient(Protocol):
    def generate_background(self, prompt: str) -> bytes | GeneratedCityBackground:
        ...


class OfficialImageClient:
    """OpenAI official images/generations, from the export_image route leg."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        endpoint_id: str,
        params: dict[str, Any],
        timeout_seconds: float,
        http_transport: Any | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.endpoint_id = endpoint_id
        self.params = dict(params)
        self.timeout_seconds = timeout_seconds
        self.http_transport = http_transport
        self._clock = clock
        self._lock = threading.Lock()
        self._cooldown_until = 0.0

    @classmethod
    def from_settings(cls, settings: Any | None = None) -> "OfficialImageClient | None":
        from src.model_routes import resolve_open_source_leg

        if settings is None:
            from src.config import get_settings

            settings = get_settings()
        resolved = resolve_open_source_leg(settings, "export_image")
        if not resolved.api_key:
            return None
        return cls(
            base_url=resolved.base_url,
            api_key=resolved.api_key,
            model=resolved.model,
            endpoint_id=resolved.endpoint_id,
            params=dict(resolved.params),
            timeout_seconds=_positive_float(
                getattr(settings, "export_image_timeout_seconds", 360.0),
                360.0,
            ),
        )

    def generate_background(self, prompt: str) -> GeneratedCityBackground:
        started = self._clock()
        deadline = started + self.timeout_seconds
        attempts: list[dict[str, Any]] = []
        if self._cooling_down():
            raise BackgroundGenerationError("official_cooling_down", _attempt_summary(attempts, "official_cooling_down"))
        while True:
            attempt_started = self._clock()
            channel_token = image_channel.set(self.endpoint_id)
            try:
                image_bytes = self._request_once(prompt, timeout=max(1.0, deadline - attempt_started))
                open_image_bytes(image_bytes)
            except Exception as exc:
                error_code = _provider_error_code(exc)
                attempts.append(_attempt_metadata(
                    self.endpoint_id, result="failed", error_code=error_code,
                    elapsed_ms=_elapsed_ms(attempt_started, self._clock())))
                if (len(attempts) == 1 and _retryable(exc)
                        and deadline - self._clock() >= IMAGE_RETRY_MIN_SECONDS):
                    continue
                self._start_cooldown()
                raise BackgroundGenerationError(error_code, _attempt_summary(attempts, error_code)) from None
            finally:
                image_channel.reset(channel_token)
            attempts.append(_attempt_metadata(
                self.endpoint_id, result="success", error_code=None,
                elapsed_ms=_elapsed_ms(attempt_started, self._clock())))
            return GeneratedCityBackground(
                image_bytes=image_bytes,
                metadata={
                    "ai_call_attempted": True,
                    "ai_call_count": len(attempts),
                    "provider_attempt_count": len(attempts),
                    "selected_provider_name": self.endpoint_id,
                    "provider_attempts": attempts,
                },
            )

    @observe_image_call
    def _request_once(self, prompt: str, *, timeout: float) -> bytes:
        import httpx

        with httpx.Client(timeout=timeout, transport=self.http_transport, follow_redirects=False) as client:
            response = client.post(
                f"{self.base_url}/images/generations",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                # The current official form. gpt-image models always answer
                # with base64; style and response_format belong to older models.
                json={"model": self.model, "prompt": prompt, "n": 1, **self.params},
            )
            capture_image_response(response)
            response.raise_for_status()
            encoded = first_image_data(response.json()).get("b64_json")
        if not isinstance(encoded, str) or not encoded.strip():
            raise ValueError("image response did not contain b64_json")
        return decode_base64_image(encoded)

    def _cooling_down(self) -> bool:
        with self._lock:
            return self._clock() < self._cooldown_until

    def _start_cooldown(self) -> None:
        with self._lock:
            self._cooldown_until = self._clock() + IMAGE_FAILURE_COOLDOWN_SECONDS


def build_city_background_client(settings: Any | None = None) -> CityBackgroundClient | None:
    """The official image client, or None when its key is not set."""

    return OfficialImageClient.from_settings(settings)


def _retryable(exc: Exception) -> bool:
    import httpx

    if isinstance(exc, (httpx.ConnectError, httpx.RemoteProtocolError)):
        return True
    status_code = getattr(getattr(exc, "response", None), "status_code", None)
    return isinstance(status_code, int) and 500 <= status_code <= 599


def _attempt_summary(attempts: list[dict[str, Any]], error_code: str) -> dict[str, Any]:
    return {
        "ai_call_attempted": bool(attempts),
        "ai_call_count": len(attempts),
        "provider_attempt_count": len(attempts),
        "provider_attempts": attempts,
        "background_error_code": error_code,
    }


def _provider_error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int):
        return f"HTTP_{status_code}"
    name = exc.__class__.__name__
    return name if re.fullmatch(r"[A-Za-z0-9_]{1,80}", name) else "ProviderError"


def _attempt_metadata(
    provider_name: str,
    *,
    result: str,
    error_code: str | None,
    elapsed_ms: int,
) -> dict[str, Any]:
    return {
        "provider_name": provider_name,
        "result": result,
        "error_code": error_code,
        "elapsed_ms": elapsed_ms,
    }


def _elapsed_ms(started: float, finished: float) -> int:
    return max(0, int(round((finished - started) * 1000)))


@dataclass(frozen=True)
class CityBackground:
    image: Image.Image
    metadata: dict[str, Any]


def resolve_city_background(
    prompt: str,
    client: CityBackgroundClient | None,
) -> CityBackground:
    """Resolve a generated image or always return a usable packaged fallback."""

    fallback_key = fallback_city_key_from_prompt(prompt)
    if client is not None:
        started = time.monotonic()
        try:
            generated = client.generate_background(prompt)
            if isinstance(generated, GeneratedCityBackground):
                image_bytes = generated.image_bytes
                provider_metadata = dict(generated.metadata)
            else:
                image_bytes = generated
                provider_metadata = {
                    "ai_call_attempted": True,
                    "ai_call_count": 1,
                    "provider_attempt_count": 1,
                    "selected_provider_name": "legacy",
                    "provider_attempts": [
                        _attempt_metadata(
                            "legacy",
                            result="success",
                            error_code=None,
                            elapsed_ms=_elapsed_ms(started, time.monotonic()),
                        )
                    ],
                }
            return CityBackground(
                image=open_image_bytes(image_bytes),
                metadata={
                    "background_status": "generated",
                    "background_byte_size": len(image_bytes),
                    **provider_metadata,
                },
            )
        except Exception as exc:
            image, fallback_metadata = fallback_city_background_with_metadata(fallback_key)
            if isinstance(exc, BackgroundGenerationError):
                provider_metadata = dict(exc.metadata)
            else:
                error_code = _provider_error_code(exc)
                provider_metadata = {
                    "ai_call_attempted": True,
                    "ai_call_count": 1,
                    "provider_attempt_count": 1,
                    "provider_attempts": [
                        _attempt_metadata(
                            "legacy",
                            result="failed",
                            error_code=error_code,
                            elapsed_ms=_elapsed_ms(started, time.monotonic()),
                        )
                    ],
                    "background_error_code": error_code,
                }
            return CityBackground(
                image=image,
                metadata={
                    "background_status": "fallback",
                    **provider_metadata,
                    **fallback_metadata,
                },
            )
    image, fallback_metadata = fallback_city_background_with_metadata(fallback_key)
    return CityBackground(
        image=image,
        metadata={
            "background_status": "fallback",
            "ai_call_attempted": False,
            "ai_call_count": 0,
            "provider_attempt_count": 0,
            "provider_attempts": [],
            "background_error_code": "not_configured",
            **fallback_metadata,
        },
    )


def open_image_bytes(data: bytes) -> Image.Image:
    if not data:
        raise ValueError("empty image")
    with Image.open(BytesIO(data)) as image:
        _validate_image_dimensions(image)
        return image.convert("RGB")


def fallback_city_background(
    fallback_key: str | None = None,
) -> Image.Image:
    return fallback_city_background_with_metadata(fallback_key)[0]


def fallback_city_background_with_metadata(
    fallback_key: str | None = None,
) -> tuple[Image.Image, dict[str, Any]]:
    normalized_key = _normalized_fallback_key(fallback_key)
    city_asset_path = _city_background_asset_path(normalized_key)
    if city_asset_path is not None:
        loaded = _load_background_asset(city_asset_path)
        if loaded is not None:
            return loaded, {
                "background_fallback_source": "packaged_city_asset",
                "background_fallback_key": normalized_key,
                "background_fallback_filename": city_asset_path.name,
            }

    loaded = _load_background_asset(FALLBACK_BACKGROUND_PATH)
    if loaded is not None:
        return loaded, {
            "background_fallback_source": "generic_fallback_asset",
            "background_fallback_key": "generic",
            "background_fallback_filename": FALLBACK_BACKGROUND_PATH.name,
        }

    return _generated_fallback_background(), {
        "background_fallback_source": "generated_generic_fallback",
        "background_fallback_key": "generic",
        "background_fallback_filename": None,
    }


def fallback_city_key_from_prompt(prompt: str) -> str | None:
    match = re.search(
        r"\bReference fallback city key:\s*([a-z0-9_-]+)\b",
        prompt or "",
        re.IGNORECASE,
    )
    if not match:
        return None
    return _normalized_fallback_key(match.group(1))


def first_image_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise ValueError("image response data is missing")
    return data[0]


def decode_base64_image(value: str) -> bytes:
    raw = re.sub(r"\s+", "", value or "")
    try:
        decoded = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise ValueError("image response b64_json is invalid") from exc
    if not decoded:
        raise ValueError("image response b64_json is empty")
    return decoded


def _validate_image_dimensions(image: Image.Image) -> None:
    width, height = image.size
    if width <= 0 or height <= 0:
        raise ValueError("image dimensions are invalid")
    if width > IMAGE_MAX_DIMENSION or height > IMAGE_MAX_DIMENSION:
        raise ValueError("image dimensions exceed the supported maximum")
    if width * height > IMAGE_MAX_PIXELS:
        raise ValueError("image pixel count exceeds the supported maximum")


def _generated_fallback_background() -> Image.Image:
    width, height = 1440, 2400
    image = Image.new("RGB", (width, height), "#E9EEE9")
    draw = ImageDraw.Draw(image)
    for y in range(height):
        ratio = y / max(1, height - 1)
        draw.line(
            (0, y, width, y),
            fill=(
                int(230 - 45 * ratio),
                int(238 - 28 * ratio),
                int(232 - 18 * ratio),
            ),
        )
    ridges = (
        ((0, 1370), (280, 1170), (570, 1360), (890, 1110), (1190, 1320), (1440, 1190)),
        ((0, 1700), (310, 1460), (640, 1640), (980, 1390), (1230, 1580), (1440, 1480)),
        ((0, 2020), (330, 1740), (680, 1940), (1040, 1670), (1280, 1880), (1440, 1800)),
    )
    for ridge, color in zip(ridges, ((155, 180, 168), (113, 153, 145), (68, 113, 108))):
        draw.polygon(list(ridge) + [(width, height), (0, height)], fill=color)
    return image.filter(ImageFilter.GaussianBlur(2.0))


def _positive_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _normalized_fallback_key(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().lower()
    if normalized in set(CITY_BACKGROUND_ASSET_KEYS.values()):
        return normalized
    return None


def _city_background_asset_path(fallback_key: str | None) -> Path | None:
    if not fallback_key:
        return None
    return CITY_BACKGROUND_ASSET_DIR / f"{fallback_key}.png"


def _load_background_asset(path: Path) -> Image.Image | None:
    try:
        return open_image_bytes(path.read_bytes())
    except Exception:
        return None
