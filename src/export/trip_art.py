"""Shared cover and day illustrations for itinerary exports."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, wait
from contextvars import copy_context
from dataclasses import asdict, dataclass
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable
from uuid import uuid4
from weakref import WeakValueDictionary

from PIL import Image, ImageOps

from src.export.city_background import GeneratedCityBackground, open_image_bytes
from src.export.city_photo import CityPhotoEntry, CityPhotoResolver, CityPhotoResult, normalize_city_name
from src.export.image_prompt import TRIP_ART_PROMPT_VERSION, build_trip_art_prompt

if TYPE_CHECKING:
    from src.api.export_source import ExportSource


TRIP_ART_SIZE = (1536, 1024)
_REQUEST_SIZE = f"{TRIP_ART_SIZE[0]}x{TRIP_ART_SIZE[1]}"
TRIP_ART_MAX_PARALLEL_IMAGES = 4
TRIP_ART_GENERATION_TIMEOUT_SECONDS = 240.0
TRIP_ART_FALLBACK_TTL_SECONDS = 30 * 60
_COVER = 0
_cache_locks = WeakValueDictionary()
_cache_locks_guard = Lock()


@runtime_checkable
class TripArtImageClient(Protocol):
    def is_cooling_down(self) -> bool: ...

    def generate_background(self, prompt: str, *, size: str) -> bytes | GeneratedCityBackground: ...


@dataclass(frozen=True)
class TripArtImage:
    path: Path
    status: Literal["generated", "cached", "photo", "paper"]
    photo_entry: CityPhotoEntry | None = None


@dataclass(frozen=True)
class TripArt:
    cover: TripArtImage
    days: dict[int, TripArtImage]


def resolve_trip_art(
    export_source: ExportSource,
    storage_dir: Path,
    *,
    image_client: TripArtImageClient | None = None,
    city_photo_resolver: CityPhotoResolver | None = None,
) -> TripArt:
    """Return the primary itinerary's art, shared by PDF and share-image jobs.

    Successful illustrations are reusable until cache cleanup. Fallbacks are
    shared briefly so another export can retry missing illustrations later.
    """
    result = export_source.source["result"]
    result_id = result["result_id"]
    if type(result_id) is not int or result_id <= 0:
        raise ValueError("invalid trip art result identity")

    city = normalize_city_name(result["city"]["name"])
    prompts = {_COVER: build_trip_art_prompt(city)}
    for day in result["plans"][0]["days"]:
        number = day["day"]
        if type(number) is not int or number <= 0 or number in prompts:
            raise ValueError("invalid trip art day number")
        prompts[number] = build_trip_art_prompt(city, places=day["places"])

    content = json.dumps(
        [TRIP_ART_PROMPT_VERSION, _REQUEST_SIZE, prompts],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    cache_dir = (Path(storage_dir).expanduser() / "trip-art" / str(result_id) / digest).resolve()
    with _cache_lock(cache_dir):
        cache_dir.mkdir(parents=True, exist_ok=True)
        fallback_cutoff = time.time() - TRIP_ART_FALLBACK_TTL_SECONDS
        images: dict[int, TripArtImage] = {}
        missing: dict[int, Path] = {}
        for number in prompts:
            stem = cache_dir / ("cover" if number == _COVER else f"day-{number}")
            cached = _read_cached(stem, fallback_cutoff)
            if cached is None:
                missing[number] = stem
            else:
                images[number] = cached

        if missing and image_client is not None and not image_client.is_cooling_down():
            images.update(_generate_missing(image_client, prompts, missing))

        fallbacks = missing.keys() - images.keys()
        photo = _resolve_photo(city, city_photo_resolver) if fallbacks else None
        for number in fallbacks:
            images[number] = _fallback(missing[number], photo)

        os.utime(cache_dir, None)
        return TripArt(images[_COVER], {number: images[number] for number in prompts if number != _COVER})


def _cache_lock(cache_dir: Path):
    with _cache_locks_guard:
        lock = _cache_locks.get(cache_dir)
        if lock is None:
            lock = _cache_locks[cache_dir] = Lock()
        return lock


def cleanup_expired_trip_art(storage_dir: Path, *, ttl_days: int) -> int:
    """Remove inactive illustration sets from the dedicated cache tree."""
    root = Path(storage_dir).expanduser().resolve() / "trip-art"
    if not root.is_dir() or root.resolve() != root:
        return 0
    cutoff = time.time() - ttl_days * 24 * 60 * 60
    deleted = 0
    for result_dir in root.iterdir():
        if not (result_dir.name.isdecimal() and result_dir.is_dir() and result_dir.resolve() == result_dir):
            continue
        try:
            candidates = list(result_dir.iterdir())
        except FileNotFoundError:
            continue
        for cache_dir in candidates:
            if not (re.fullmatch(r"[0-9a-f]{64}", cache_dir.name) and cache_dir.resolve() == cache_dir):
                continue
            lock = _cache_lock(cache_dir)
            if not lock.acquire(blocking=False):
                continue
            try:
                if cache_dir.is_dir() and cache_dir.stat().st_mtime <= cutoff:
                    shutil.rmtree(cache_dir)
                    deleted += 1
            except FileNotFoundError:
                pass
            finally:
                lock.release()
        try:
            result_dir.rmdir()
        except OSError:
            pass
    return deleted


def _generate_missing(
    client: TripArtImageClient, prompts: dict[int, str], missing: dict[int, Path],
) -> dict[int, TripArtImage]:
    deadline = time.monotonic() + TRIP_ART_GENERATION_TIMEOUT_SECONDS
    pool = ThreadPoolExecutor(max_workers=min(len(missing), TRIP_ART_MAX_PARALLEL_IMAGES))
    try:
        futures = {
            pool.submit(copy_context().run, _generate, client, prompts[number], deadline): number
            for number in missing
        }
        completed, _ = wait(futures, timeout=max(0.0, deadline - time.monotonic()))
    finally:
        # In-flight synchronous requests can finish later. Only this thread writes to the cache.
        pool.shutdown(wait=False, cancel_futures=True)

    images = {}
    for future in completed:
        data = future.result()
        if data is not None:
            number = futures[future]
            path = missing[number].with_suffix(".jpg")
            _write_atomic(path, data)
            images[number] = TripArtImage(path, "generated")
    return images


def _generate(client: TripArtImageClient, prompt: str, deadline: float) -> bytes | None:
    # Earlier calls in this batch can put the client into cooldown.
    if time.monotonic() >= deadline or client.is_cooling_down():
        return None
    try:
        response = client.generate_background(prompt, size=_REQUEST_SIZE)
        data = response.image_bytes if isinstance(response, GeneratedCityBackground) else response
        if time.monotonic() < deadline:
            return _jpeg_bytes(open_image_bytes(data))
    except Exception:
        return None
    return None


def _resolve_photo(city: str, resolver: CityPhotoResolver | None) -> CityPhotoResult | None:
    if resolver is None:
        return None
    try:
        return resolver.resolve(city)
    except Exception:
        return None


def _fallback(stem: Path, photo: CityPhotoResult | None) -> TripArtImage:
    if photo is not None and photo.entry is not None:
        path = stem.with_suffix(".photo.jpg")
        credit = json.dumps(asdict(photo.entry), ensure_ascii=False).encode("utf-8")
        _write_atomic(stem.with_suffix(".photo.json"), credit)
        _write_atomic(path, _jpeg_bytes(photo.image))
        return TripArtImage(path, "photo", photo.entry)

    path = stem.with_suffix(".paper.jpg")
    _write_atomic(path, _paper_jpeg())
    return TripArtImage(path, "paper")


def _read_cached(stem: Path, fallback_cutoff: float) -> TripArtImage | None:
    generated_path = stem.with_suffix(".jpg")
    if _valid_jpeg(generated_path):
        return TripArtImage(generated_path, "cached")

    photo_path = stem.with_suffix(".photo.jpg")
    if _fresh_fallback(photo_path, fallback_cutoff):
        try:
            credit = json.loads(stem.with_suffix(".photo.json").read_text(encoding="utf-8"))
            return TripArtImage(photo_path, "photo", CityPhotoEntry(**credit))
        except (OSError, ValueError, TypeError):
            pass

    paper_path = stem.with_suffix(".paper.jpg")
    if _fresh_fallback(paper_path, fallback_cutoff):
        return TripArtImage(paper_path, "paper")
    return None


def _fresh_fallback(path: Path, cutoff: float) -> bool:
    try:
        return path.stat().st_mtime > cutoff and _valid_jpeg(path)
    except OSError:
        return False


def _valid_jpeg(path: Path) -> bool:
    try:
        with Image.open(path) as image:
            if image.format != "JPEG" or image.size != TRIP_ART_SIZE:
                return False
            image.load()
            return True
    except (OSError, ValueError, Image.DecompressionBombError):
        return False


@lru_cache(maxsize=1)
def _paper_jpeg() -> bytes:
    grain = Image.effect_noise(TRIP_ART_SIZE, 8)
    return _jpeg_bytes(ImageOps.colorize(grain, "#cec0a7", "#fff9eb"))


def _jpeg_bytes(image: Image.Image) -> bytes:
    banner = ImageOps.fit(image.convert("RGB"), TRIP_ART_SIZE, Image.Resampling.LANCZOS)
    output = BytesIO()
    banner.save(output, format="JPEG", quality=88, optimize=True)
    return output.getvalue()


def _write_atomic(path: Path, data: bytes) -> None:
    staging = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        staging.write_bytes(data)
        os.replace(staging, path)
    finally:
        staging.unlink(missing_ok=True)
