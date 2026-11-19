"""Provider-specific W6 failure policy. No provider payload is persisted here.

AUTH predicates come from D3's real negative-authentication responses. DeepSeek
402 is documented at https://api-docs.deepseek.com/quick_start/error_codes/;
it is a documented contract, not a captured exhausted-account response.
OpenAI 401 invalid_api_key was captured on 11-15; 429 insufficient_quota is
documented at https://platform.openai.com/docs/guides/error-codes.
"""
from __future__ import annotations

from urllib.parse import urlsplit
import re

import httpx

LONG_FAILURE_CLASSES = frozenset({"AUTH", "QUOTA_EXHAUSTED"})
# Gemini words a bad key differently depending on the client: D3 and raw
# requests get the first, the OpenAI SDK route got "Invalid Auth key." (11-15
# staging). Compared case-folded; the host, path and status still have to match.
_GEMINI_BAD_KEY_MESSAGES = frozenset({"please pass a valid api key", "invalid auth key."})
ORDINARY_COOLDOWN_SECONDS = 120
LONG_COOLDOWN_SECONDS = 3600


def cooldown_seconds(failure_class: str | None, retry_after_seconds: float | None = None) -> int:
    if failure_class in LONG_FAILURE_CLASSES:
        return LONG_COOLDOWN_SECONDS
    if failure_class == "RATE_LIMIT" and retry_after_seconds is not None:
        return max(ORDINARY_COOLDOWN_SECONDS, min(LONG_COOLDOWN_SECONDS, int(retry_after_seconds)))
    return ORDINARY_COOLDOWN_SECONDS


def confirmed_long_failure(response: httpx.Response) -> str | None:
    """Match the actual provider route and its documented/captured error.

    A generic HTTP status or a substring such as 'quota' never establishes an
    account failure for an arbitrary relay. Google native errors are not copied
    onto its OpenAI-compatible route.
    """
    try:
        url = urlsplit(str(response.request.url))
    except (RuntimeError, ValueError):
        return None
    host, path, status = (url.hostname or "").lower(), url.path.rstrip("/"), response.status_code
    try:
        body = response.json()
    except (ValueError, UnicodeDecodeError):
        body = None
    if isinstance(body, list):
        body = body[0] if len(body) == 1 else None
    error = body.get("error") if isinstance(body, dict) else None
    error = error if isinstance(error, dict) else {}
    message = str(error.get("message") or "").strip()
    if host == "api.deepseek.com" and path in {"/chat/completions", "/v1/chat/completions"}:
        if status == 401 and error.get("type") == "authentication_error":
            return "AUTH"
        if status == 402:
            return "QUOTA_EXHAUSTED"
    if host == "generativelanguage.googleapis.com" and path == "/v1beta/openai/chat/completions":
        if status == 400 and error.get("status") == "INVALID_ARGUMENT" and message.casefold() in _GEMINI_BAD_KEY_MESSAGES:
            return "AUTH"
    if host == "api.openai.com" and path in {"/v1/responses", "/v1/chat/completions"}:
        if status == 401 and error.get("code") == "invalid_api_key":
            return "AUTH"
        if status == 429 and error.get("code") == "insufficient_quota":
            return "QUOTA_EXHAUSTED"
    return None
