"""Rotate the public Amap keys already configured for routing.

Fingerprints are logged. Raw keys are not.
"""

from __future__ import annotations

import hashlib
import threading
import time

SEARCH_KEY_BUCKET = "place_search"

_QUOTA_INFOCODES = {"10003", "10010", "10044"}
_OVER_LIMIT_INFOCODES = {"10004", "10014", "10029"}


def configured_amap_keys(settings) -> list[str]:
    keys: list[str] = []
    for name in ("amap_api_key", "amap_api_key_two"):
        value = str(getattr(settings, name, "") or "").strip()
        if value and value not in keys:
            keys.append(value)
    return keys


def key_fingerprint(key: str) -> str:
    return hashlib.sha256((key or "").encode()).hexdigest()[:12]


def classify_amap_failure(payload: dict) -> str:
    code = str((payload or {}).get("infocode") or "")
    if code in _QUOTA_INFOCODES:
        return "quota"
    if code in _OVER_LIMIT_INFOCODES:
        return "over_limit"
    return "error"


class AmapKeyCoordinator:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._exhausted: set[str] = set()
        self._backoff: dict[tuple[str, str], float] = {}
        self._cursor = 0

    def next_key(self, keys, *, bucket, mode, exclude):
        del bucket
        now = time.monotonic()
        with self._lock:
            usable = []
            for key in keys or []:
                fingerprint = key_fingerprint(key)
                if fingerprint in exclude or fingerprint in self._exhausted:
                    continue
                if self._backoff.get((fingerprint, mode), 0) > now:
                    continue
                usable.append(key)
            if not usable:
                return None
            self._cursor = (self._cursor + 1) % len(usable)
            return usable[self._cursor]

    def mark_quota_exhausted(self, key, *, bucket) -> None:
        del bucket
        with self._lock:
            self._exhausted.add(key_fingerprint(key))

    def mark_over_limit(self, key, mode, seconds: float) -> None:
        with self._lock:
            self._backoff[(key_fingerprint(key), mode)] = time.monotonic() + float(seconds)


_COORDINATOR = AmapKeyCoordinator()


def get_amap_key_coordinator() -> AmapKeyCoordinator:
    return _COORDINATOR
