"""Process-wide Amap key pool.

Keys come from ``amap_api_key`` and ``amap_api_key_two``. Empty values are
dropped and identical values collapse to one entry. Call sites share one
counter. Logs and printed counts use an 8-character sha256 fingerprint.

Quota exhaustion is tracked per key and per product bucket. Amap applies
search and direction limits separately: a search ``10044`` must not disable
direction calls on the same key.
"""

from __future__ import annotations

import hashlib
import threading
import time
from datetime import datetime, timedelta
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")

# Same codes as the area-food importer. 10001 is an invalid key; treating it
# as a daily outage avoids hammering that key until the next Beijing midnight.
QUOTA_INFOCODES = frozenset({"10003", "10044", "10045", "10001"})
# Direction QPS / concurrency, plus the infocodes the route client already
# treated as rate limits. 10003 moved to the quota set above.
OVER_LIMIT_INFOCODES = frozenset({
    "10019",
    "10020",
    "10021",
    "10004",
    "10010",
    "10014",
    "10029",
})

ROUTE_KEY_BUCKET = "route"
POI_DETAIL_KEY_BUCKET = "poi_detail"
SEARCH_KEY_BUCKET = "search"

_QUOTA_TEXT_MARKERS = (
    "USER_DAILY_QUERY_OVER_LIMIT",
    "DAILY_QUERY_OVER_LIMIT",
    "QUOTA",
)
_OVER_LIMIT_TEXT_MARKERS = (
    "QPS",
    "TOO_FREQUENT",
    "ACCESS_TOO_FREQUENT",
    "IP_QUERY_OVER_LIMIT",
    "HAS_EXCEEDED",
    "OVER_LIMIT",
    "超限",
    "超过",
    "频繁",
)


def key_fingerprint(api_key: str) -> str:
    """Return a short stable id. This is the only key form that may be logged."""

    return hashlib.sha256(str(api_key or "").encode("utf-8")).hexdigest()[:8]


def redact_amap_key(text: str, api_key: str) -> str:
    secret = str(api_key or "")
    if len(secret) < 4 or secret not in text:
        return text
    return text.replace(secret, key_fingerprint(secret))


def dedupe_amap_keys(values: Sequence[object]) -> list[str]:
    seen: set[str] = set()
    keys: list[str] = []
    for raw in values:
        key = str(raw or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        keys.append(key)
    return keys


def configured_amap_keys(settings: object | None = None) -> list[str]:
    if settings is None:
        from src.config import get_settings

        settings = get_settings()
    return dedupe_amap_keys((
        getattr(settings, "amap_api_key", ""),
        getattr(settings, "amap_api_key_two", ""),
    ))


def amap_keys_configured(settings: object | None = None) -> bool:
    return bool(configured_amap_keys(settings))


def next_beijing_midnight(now: datetime | None = None) -> datetime:
    """The next 00:00 in Asia/Shanghai. A key marked now stays out until then."""

    current = now or datetime.now(BEIJING)
    if current.tzinfo is None:
        current = current.replace(tzinfo=BEIJING)
    else:
        current = current.astimezone(BEIJING)
    tomorrow = current.date() + timedelta(days=1)
    return datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=BEIJING)


def classify_amap_infocode(infocode: str, info: str = "") -> str | None:
    """Return ``quota``, ``over_limit``, or None."""

    code = str(infocode or "").strip()
    text = f"{code} {info}".upper()
    raw_info = str(info or "")
    if code in QUOTA_INFOCODES or any(marker in text for marker in _QUOTA_TEXT_MARKERS):
        return "quota"
    if code in OVER_LIMIT_INFOCODES:
        return "over_limit"
    if any(marker in text for marker in _OVER_LIMIT_TEXT_MARKERS):
        return "over_limit"
    if any(marker in raw_info for marker in ("超限", "超过", "频繁")):
        return "over_limit"
    return None


def classify_amap_failure(payload: Mapping[str, object]) -> str | None:
    infocode = str(payload.get("infocode") or "")
    info = " ".join(
        str(payload.get(key) or "")
        for key in ("info", "errmsg", "message")
    )
    return classify_amap_infocode(infocode, info)


class AmapKeyCoordinator:
    """Round-robin counter plus per-key quota and per-mode cooldown."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counter = 0
        self._quota_until: dict[tuple[str, str], float] = {}
        self._over_limit_until: dict[tuple[str, str], float] = {}

    def reset(self) -> None:
        with self._lock:
            self._counter = 0
            self._quota_until.clear()
            self._over_limit_until.clear()

    def next_key(
        self,
        keys: Sequence[str],
        *,
        bucket: str,
        mode: str | None = None,
        exclude: set[str] | None = None,
        now_epoch: float | None = None,
        now_monotonic: float | None = None,
    ) -> str | None:
        with self._lock:
            usable = self._usable_unlocked(
                keys,
                bucket=bucket,
                mode=mode,
                exclude=exclude,
                now_epoch=now_epoch,
                now_monotonic=now_monotonic,
            )
            if not usable:
                return None
            index = self._counter
            self._counter += 1
        return usable[index % len(usable)]

    def usable_keys(
        self,
        keys: Sequence[str],
        *,
        bucket: str,
        mode: str | None = None,
        exclude: set[str] | None = None,
        now_epoch: float | None = None,
        now_monotonic: float | None = None,
    ) -> list[str]:
        with self._lock:
            return self._usable_unlocked(
                keys,
                bucket=bucket,
                mode=mode,
                exclude=exclude,
                now_epoch=now_epoch,
                now_monotonic=now_monotonic,
            )

    def mark_quota_exhausted(
        self,
        api_key: str,
        *,
        bucket: str,
        now: datetime | None = None,
    ) -> float:
        until = next_beijing_midnight(now).timestamp()
        fingerprint = key_fingerprint(api_key)
        with self._lock:
            current = self._quota_until.get((fingerprint, bucket), 0.0)
            self._quota_until[(fingerprint, bucket)] = max(current, until)
        return until

    def mark_over_limit(
        self,
        api_key: str,
        mode: str,
        seconds: float,
        *,
        now_monotonic: float | None = None,
    ) -> float:
        mono = time.monotonic() if now_monotonic is None else now_monotonic
        until = mono + max(0.0, float(seconds))
        fingerprint = key_fingerprint(api_key)
        with self._lock:
            slot = (fingerprint, mode)
            self._over_limit_until[slot] = max(self._over_limit_until.get(slot, 0.0), until)
        return until

    def _usable_unlocked(
        self,
        keys: Sequence[str],
        *,
        bucket: str,
        mode: str | None,
        exclude: set[str] | None,
        now_epoch: float | None,
        now_monotonic: float | None,
    ) -> list[str]:
        epoch = time.time() if now_epoch is None else now_epoch
        mono = time.monotonic() if now_monotonic is None else now_monotonic
        blocked = exclude or set()
        usable: list[str] = []
        for key in keys:
            if not key:
                continue
            fingerprint = key_fingerprint(key)
            if fingerprint in blocked:
                continue
            if epoch < self._quota_until.get((fingerprint, bucket), 0.0):
                continue
            if mode is not None and mono < self._over_limit_until.get((fingerprint, mode), 0.0):
                continue
            usable.append(key)
        return usable


_COORDINATOR = AmapKeyCoordinator()


def get_amap_key_coordinator() -> AmapKeyCoordinator:
    return _COORDINATOR


def reset_amap_key_coordinator() -> None:
    """Test-only. Production call sites keep the process coordinator."""

    _COORDINATOR.reset()
