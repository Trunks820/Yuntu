"""Bounded v0.12 Selector/Review model routing.

This module owns wire validation, safe failure classification and the count of
actual provider requests.  It is inert for legacy generation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from dataclasses import dataclass, field, replace
from collections.abc import Callable
from typing import Any, Awaitable, Literal

import httpx

from src.jobs.generation_delivery import ExecutionCall, ExecutionObservation

logger = logging.getLogger(__name__)

_REDACT_EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)
_REDACT_KEY_RE = re.compile(
    r"(?:sk-|rk-|pk-|api[_-]?key|token|secret|bearer\s+)[A-Za-z0-9._\-/=+]{8,}",
    re.I,
)
_REDACT_URL_CRED_RE = re.compile(r"https?://[^/\s:]+:[^/@\s]+@[^\s]+", re.I)
_REDACT_SESSION_RE = re.compile(
    r"(?:session[_-]?id|sid|otp|verify(?:_?code)?|验证码)\s*[=:：]\s*\S+",
    re.I,
)
_DIAGNOSTIC_SNIPPET_LIMIT = 400
_FINISH_REASON_ENUM = frozenset({
    "stop", "length", "max_tokens", "content_filter", "tool_calls", "function_call",
})
_KNOWN_DIAGNOSTIC_SCHEMA_KEYS = frozenset({
    "issues", "overall_notes", "category", "publish_action", "reason",
    "plan_index", "day", "place_id", "names", "snippet", "evidence", "tags",
    "side_effects", "choices", "message", "content", "finish_reason", "index",
    "logprobs", "role", "id", "object", "created", "model", "usage",
    "prompt_tokens", "completion_tokens", "total_tokens",
    "schema_failures", "path", "expected", "actual_type", "redacted_output",
    "output_length", "max_tokens", "invalid_output_type", "sibling_failures",
    "error_code", "failure_scope", "redacted", "snippet", "redaction_reason",
    "root_type", "count", "value_types", "_unknown",
})
_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9_.\[\]$]{0,120}$")
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.|\[\]$]{0,80}$")
_SCHEMA_VALUE_DOMAINS = frozenset({
    "bool", "zero", "negative_int", "positive_int", "digit_string", "other_str", "other_type",
    "plan_not_locked", "day_not_locked", "place_on_other_day", "place_in_other_plan", "place_not_locked",
})
_DIAGNOSTIC_KEEP_KEYS = frozenset({
    "finish_reason", "schema_failures", "redacted_output", "max_tokens",
    "output_length", "invalid_output_type", "sibling_failures",
})


FailureCode = Literal[
    "AUTH", "QUOTA_EXHAUSTED", "MODEL_PERMISSION", "RATE_LIMIT", "TRANSPORT", "TIMEOUT",
    "HTTP_5XX", "WIRE_INVALID", "SCHEMA_INVALID", "TRUNCATED",
    "CANCELLED", "UNKNOWN",
]
FailureScope = Literal["MODEL", "PROVIDER", "ACCOUNT", "UNKNOWN"]


class GenerationModelCallError(RuntimeError):
    def __init__(
        self,
        code: FailureCode,
        scope: FailureScope,
        *,
        status: int | None = None,
        retry_after_seconds: int | None = None,
        diagnostic: dict[str, Any] | None = None,
    ):
        super().__init__(code)
        self.code = code
        self.scope = scope
        self.status = status
        self.retry_after_seconds = retry_after_seconds
        self.diagnostic = _sanitize_call_diagnostic(dict(diagnostic or {}))


@dataclass(frozen=True)
class ModelEndpoint:
    provider: str
    model: str
    base_url: str
    api_key: str
    failure_domain: str
    endpoint_id: str
    wire_api: str = "chat_completions"
    # OpenAI Responses only. "none" turns reasoning off.
    reasoning_effort: str = "none"


def table_endpoint(settings: Any, *, agent: str, slot: str) -> ModelEndpoint:
    """One leg of src/model_routes.py as a routable endpoint."""
    from src.model_routes import resolve_open_source_leg

    resolved = resolve_open_source_leg(settings, agent, slot)
    return ModelEndpoint(
        provider=resolved.provider,
        model=resolved.model,
        base_url=resolved.base_url,
        api_key=resolved.api_key,
        failure_domain=resolved.account,
        endpoint_id=resolved.endpoint_id,
        wire_api=resolved.wire_api,
        reasoning_effort=str(resolved.params.get("reasoning_effort") or "none"),
    )


def endpoint_runtime_fingerprint(endpoint: ModelEndpoint, *, role: str) -> str:
    return hashlib.sha256(
        (f"v012:{role}:{endpoint.provider}:{endpoint.model}:"
         f"{endpoint.failure_domain}:{endpoint.base_url}:"
         f"{hashlib.sha256(endpoint.api_key.encode()).hexdigest()}"
         + (f":{endpoint.wire_api}" if endpoint.wire_api != "chat_completions" else "")).encode()
    ).hexdigest()


def configured_candidate(settings: Any, *, role: str, candidate: str) -> ModelEndpoint:
    """Resolve one review candidate. The selector reads src/model_routes.py."""
    model_override = None
    if candidate == "official-ds":
        prefix, provider, label, wire = "v012_official_ds", "deepseek", "official-ds", "chat_completions"
    elif candidate in {"command-glm", "command-luna"}:
        label = candidate.removeprefix("command-")
        prefix = f"v012_review_{label}"
        provider, wire = "command", "chat_completions"
    elif candidate == "command-ds41-flash":
        # Additional model on the configured review backup account.
        prefix = "v012_review_glm"
        provider, label, wire = "command", candidate, "chat_completions"
        model_override = getattr(settings, "v015_command_ds41_flash_model", "deepseek/deepseek-v4.1-flash")
    else:
        raise ValueError("unknown model candidate")
    return ModelEndpoint(
        provider=provider, model=(model_override if model_override is not None else getattr(settings, prefix + "_model", "")),
        base_url=getattr(settings, prefix + "_base_url", ""),
        api_key=getattr(settings, prefix + "_api_key", ""),
        failure_domain=getattr(settings, ("v012_command_failure_domain" if provider == "command" else prefix + "_failure_domain"), ""),
        endpoint_id=f"{role}-{label}", wire_api=wire,
    )


def independent_backup(primary: ModelEndpoint, backup: ModelEndpoint) -> bool:
    # Account aliases cannot turn an identical credential into an independent
    # backup. Empty domains are unknown rather than evidence of independence.
    return bool(primary.failure_domain and backup.failure_domain
                and primary.failure_domain != backup.failure_domain
                and primary.api_key != backup.api_key)


async def _observe_endpoint_attempt(
    endpoint: ModelEndpoint, *, role: str, success: bool, failure: str | None,
    failure_scope: str | None, trial: bool, generation: int | None,
    retry_after_seconds: int | None = None,
    has_alternatives: bool = False,
) -> None:
    """Best-effort safe runtime evidence; observation cannot change generation."""
    try:
        from src.agents.role_relay_store import PostgresRoleRelayStore

        store = PostgresRoleRelayStore()
        await asyncio.wait_for(
            store.record_direct_attempt(
                role, endpoint.endpoint_id, success=success, failure_class=failure,
                failure_scope=failure_scope, trial=trial, generation=generation,
                retry_after_seconds=retry_after_seconds,
                has_alternatives=has_alternatives,
            ), timeout=0.2,
        )
    except Exception as exc:
        logger.warning(
            "endpoint runtime observation failed role=%s endpoint=%s error_type=%s",
            role, endpoint.endpoint_id, type(exc).__name__,
        )


async def _release_endpoint_trial(endpoint: ModelEndpoint, *, role: str, generation: int) -> None:
    try:
        from src.agents.role_relay_store import PostgresRoleRelayStore
        await asyncio.wait_for(
            PostgresRoleRelayStore().release_direct_trial(
                role, endpoint.endpoint_id, generation=generation
            ), timeout=0.2,
        )
    except Exception as exc:
        logger.warning(
            "endpoint runtime lease release failed role=%s endpoint=%s error_type=%s",
            role, endpoint.endpoint_id, type(exc).__name__,
        )


async def _prepare_endpoint_attempt(
    endpoint: ModelEndpoint, *, role: str, has_alternatives: bool = False,
) -> tuple[bool, bool, int | None]:
    """Synchronize config and enforce persisted circuit/qualification state."""
    try:
        from src.agents.role_relay_store import PostgresRoleRelayStore

        store = PostgresRoleRelayStore()
        fingerprint = endpoint_runtime_fingerprint(endpoint, role=role)
        await asyncio.wait_for(store.synchronize_direct(
            role, endpoint.endpoint_id, provider=endpoint.provider,
            failure_domain=endpoint.failure_domain, fingerprint=fingerprint,
            has_alternatives=has_alternatives,
        ), timeout=0.2)
        allowed, trial_name, generation = await asyncio.wait_for(
            store.admit(role, (endpoint.endpoint_id,)), timeout=0.2
        )
        if endpoint.endpoint_id not in allowed:
            raise GenerationModelCallError("UNKNOWN", "UNKNOWN")
        return True, trial_name == endpoint.endpoint_id, generation
    except GenerationModelCallError:
        raise
    except Exception as exc:
        logger.warning(
            "endpoint runtime admission unavailable role=%s endpoint=%s error_type=%s",
            role, endpoint.endpoint_id, type(exc).__name__,
        )
        if not has_alternatives:
            # The auxiliary state store must not suppress the only configured
            # route. Explicit persisted cooldown/disable above still rejects.
            return False, False, None
        raise GenerationModelCallError("UNKNOWN", "UNKNOWN") from exc


@dataclass(frozen=True)
class RoutedModelResponse:
    text: str
    endpoint: ModelEndpoint
    call_sequence: int
    adopted_at: str | None = field(default=None, compare=False)


@dataclass
class _SharedCallLedger:
    max_calls: int
    calls_started: int = 0
    backup_claimed: bool = False
    format_repair_claimed: bool = False
    calls: list[dict[str, Any]] = field(default_factory=list)
    path_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    adopted_call_sequence: int | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


@dataclass
class SharedCallBudget:
    deadline_monotonic: float
    max_calls: int
    reserve_seconds: float = 0.0
    _ledger: _SharedCallLedger | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self._ledger is None:
            self._ledger = _SharedCallLedger(max_calls=self.max_calls)
        elif self._ledger.max_calls != self.max_calls:
            raise ValueError("phase budget must preserve the job-wide call limit")

    @property
    def calls_started(self) -> int:
        assert self._ledger is not None
        return self._ledger.calls_started

    @property
    def backup_claimed(self) -> bool:
        assert self._ledger is not None
        return self._ledger.backup_claimed

    @property
    def calls(self) -> list[dict[str, Any]]:
        assert self._ledger is not None
        return self._ledger.calls

    @property
    def path_diagnostics(self) -> list[dict[str, Any]]:
        assert self._ledger is not None
        return self._ledger.path_diagnostics

    @property
    def adopted_call_sequence(self) -> int | None:
        assert self._ledger is not None
        return self._ledger.adopted_call_sequence

    def mark_adopted(self, sequence: int) -> None:
        assert self._ledger is not None
        self._ledger.adopted_call_sequence = sequence

    def for_phase(
        self,
        *,
        deadline_monotonic: float,
        reserve_seconds: float = 0.0,
    ) -> "SharedCallBudget":
        """Return a phase deadline that shares job-wide calls and backup use."""
        return SharedCallBudget(
            deadline_monotonic=min(self.deadline_monotonic, deadline_monotonic),
            max_calls=self.max_calls,
            reserve_seconds=reserve_seconds,
            _ledger=self._ledger,
        )

    async def claim(self) -> tuple[int, float]:
        assert self._ledger is not None
        async with self._ledger.lock:
            remaining = self.deadline_monotonic - time.monotonic() - self.reserve_seconds
            if remaining <= 0:
                raise GenerationModelCallError("TIMEOUT", "UNKNOWN")
            if self._ledger.calls_started >= self.max_calls:
                raise GenerationModelCallError("UNKNOWN", "UNKNOWN")
            self._ledger.calls_started += 1
            return self._ledger.calls_started, remaining

    async def claim_backup(self) -> None:
        assert self._ledger is not None
        async with self._ledger.lock:
            self._ledger.backup_claimed = True

    async def claim_format_repair(self) -> None:
        assert self._ledger is not None
        async with self._ledger.lock:
            if self._ledger.format_repair_claimed:
                raise GenerationModelCallError("SCHEMA_INVALID", "MODEL")
            self._ledger.format_repair_claimed = True

    def record(
        self,
        *,
        sequence: int,
        endpoint: ModelEndpoint,
        status: str,
        duration_ms: int,
        error: GenerationModelCallError | None = None,
    ) -> None:
        assert self._ledger is not None
        self._ledger.calls.append({
            "sequence": sequence,
            "provider": endpoint.provider,
            "model": endpoint.model,
            "endpoint_id": endpoint.endpoint_id,
            "status": status,
            "duration_ms": duration_ms,
            "error_code": error.code if error else None,
            "failure_scope": error.scope if error else None,
            "http_status": error.status if error else None,
        })
        diagnostic = _sanitize_call_diagnostic(
            dict(error.diagnostic) if error is not None and error.diagnostic else {}
        )
        if diagnostic:
            self._ledger.path_diagnostics.append({
                "sequence": sequence,
                "provider": endpoint.provider,
                "model": endpoint.model,
                "endpoint_id": endpoint.endpoint_id,
                "status": status,
                "duration_ms": duration_ms,
                "error_code": error.code if error else None,
                "failure_scope": error.scope if error else None,
                **diagnostic,
            })


def project_execution_observation(
    calls: list[dict[str, Any]],
    *,
    role: str,
    purpose: str,
    state: str,
    reason_code: str | None = None,
    adopted_call_sequence: int | None = None,
) -> ExecutionObservation:
    """Project only actual routed requests; an unrecorded call is never invented."""
    bounded_raw = list(calls[:20])
    if adopted_call_sequence is not None and not any(
        int(item.get("sequence") or 0) == adopted_call_sequence
        for item in bounded_raw
    ):
        adopted_raw = next(
            (
                item for item in calls
                if int(item.get("sequence") or 0) == adopted_call_sequence
            ),
            None,
        )
        if adopted_raw is not None:
            bounded_raw = [*bounded_raw[:19], adopted_raw]
    projected = [
        ExecutionCall(
            sequence=int(item["sequence"]),
            provider=str(item["provider"]),
            model=str(item["model"]),
            role=role,
            purpose=purpose,
            endpoint_id=(str(item["endpoint_id"]) if item.get("endpoint_id") else None),
            status=str(item["status"]),
            duration_ms=(
                max(0, int(item["duration_ms"]))
                if item.get("duration_ms") is not None else None
            ),
            error_code=item.get("error_code"),
            failure_scope=item.get("failure_scope"),
        )
        for item in bounded_raw
    ]
    return ExecutionObservation(
        state=state,
        reason_code=reason_code,
        adopted_call_sequence=adopted_call_sequence,
        calls=projected,
        truncated=len(calls) > 20,
        total_call_count=len(calls),
    )


def _json_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int) and not isinstance(value, bool):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return "unknown_type"


def _safe_finish_reason(value: Any) -> str:
    if isinstance(value, str) and value in _FINISH_REASON_ENUM:
        return value
    return f"unknown/{_json_type_name(value)}"


def _safe_schema_token(value: Any, *, fallback: str) -> str:
    text = str(value or "")
    if _SAFE_TOKEN_RE.fullmatch(text):
        return text
    if _SAFE_PATH_RE.fullmatch(text):
        return text
    return fallback


def _safe_schema_failures(items: Any) -> list[dict[str, str]]:
    bounded: list[dict[str, str]] = []
    if not isinstance(items, list):
        return [{
            "path": "unknown_path",
            "expected": "review_schema",
            "actual_type": "unknown_type",
        }]
    for item in items[:8]:
        if not isinstance(item, dict):
            bounded.append({
                "path": "unknown_path",
                "expected": "object",
                "actual_type": _json_type_name(item),
            })
            continue
        path = item.get("path")
        expected = item.get("expected")
        actual_type = item.get("actual_type")
        bounded.append({
            "path": path if isinstance(path, str) and _SAFE_PATH_RE.fullmatch(path) else "unknown_path",
            "expected": _safe_schema_token(expected, fallback="unknown_expected"),
            "actual_type": _safe_schema_token(actual_type, fallback="unknown_type"),
            **({"value_domain": item["value_domain"]}
               if isinstance(item.get("value_domain"), str)
               and item["value_domain"] in _SCHEMA_VALUE_DOMAINS else {}),
        })
    return bounded


def _skeleton_json(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return f"<{_json_type_name(value)}>"
    if isinstance(value, dict):
        known: dict[str, Any] = {}
        unknown_count = 0
        unknown_types: list[str] = []
        for key, item in list(value.items())[:24]:
            key_text = str(key)
            if key_text in _KNOWN_DIAGNOSTIC_SCHEMA_KEYS:
                known[key_text] = _skeleton_json(item, depth=depth + 1)
            else:
                unknown_count += 1
                if len(unknown_types) < 8:
                    unknown_types.append(_json_type_name(item))
        if unknown_count:
            known["_unknown"] = {
                "count": unknown_count,
                "value_types": unknown_types,
            }
        return known
    if isinstance(value, list):
        return [_skeleton_json(item, depth=depth + 1) for item in value[:8]]
    if isinstance(value, str):
        return f"<str:len={len(value)}>"
    if value is None or isinstance(value, (int, float, bool)):
        return _json_type_name(value)
    return f"<{_json_type_name(value)}>"


def _sanitize_call_diagnostic(diagnostic: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    finish_reason = diagnostic.get("finish_reason")
    if "finish_reason" in diagnostic:
        cleaned["finish_reason"] = _safe_finish_reason(finish_reason)
    if "schema_failures" in diagnostic:
        cleaned["schema_failures"] = _safe_schema_failures(diagnostic.get("schema_failures"))
    redacted = diagnostic.get("redacted_output")
    if isinstance(redacted, dict):
        cleaned["redacted_output"] = {
            "redacted": True,
            "snippet": redacted.get("snippet") if redacted.get("redacted") else None,
            "redaction_reason": str(redacted.get("redaction_reason") or "")[:80],
            "output_length": redacted.get("output_length"),
            "root_type": _safe_schema_token(redacted.get("root_type"), fallback="unknown_type")
            if redacted.get("root_type") is not None else None,
        }
        if cleaned["redacted_output"]["root_type"] is None:
            cleaned["redacted_output"].pop("root_type")
        if cleaned["redacted_output"]["output_length"] is None:
            cleaned["redacted_output"].pop("output_length", None)
    if "max_tokens" in diagnostic:
        max_tokens = diagnostic.get("max_tokens")
        cleaned["max_tokens"] = max_tokens if isinstance(max_tokens, int) else None
    if "output_length" in diagnostic:
        output_length = diagnostic.get("output_length")
        cleaned["output_length"] = output_length if isinstance(output_length, int) else 0
    if "invalid_output_type" in diagnostic:
        cleaned["invalid_output_type"] = _safe_schema_token(
            diagnostic.get("invalid_output_type"),
            fallback="unknown_type",
        )
    siblings = diagnostic.get("sibling_failures")
    if isinstance(siblings, list):
        cleaned["sibling_failures"] = []
        for item in siblings[:8]:
            if not isinstance(item, dict):
                continue
            nested = item.get("diagnostic")
            cleaned["sibling_failures"].append({
                "error_code": _safe_schema_token(item.get("error_code"), fallback="UNKNOWN"),
                "failure_scope": _safe_schema_token(item.get("failure_scope"), fallback="UNKNOWN"),
                "diagnostic": _sanitize_call_diagnostic(nested) if isinstance(nested, dict) else {},
            })
    extra = set(diagnostic) - _DIAGNOSTIC_KEEP_KEYS
    if extra:
        cleaned["dropped_untrusted_keys"] = {
            "count": len(extra),
            "value_types": [_json_type_name(diagnostic[key]) for key in list(extra)[:8]],
        }
    return cleaned


def redact_model_output_for_diagnostics(
    raw: Any,
    *,
    max_chars: int = _DIAGNOSTIC_SNIPPET_LIMIT,
) -> dict[str, Any]:
    """Structural skeleton only. Failure yields no snippet plus an explicit reason."""
    if raw is None:
        return {
            "redacted": True,
            "snippet": None,
            "redaction_reason": "empty_input",
        }
    text = raw if isinstance(raw, str) else None
    parsed: Any = None
    if isinstance(raw, (dict, list)):
        parsed = raw
    elif isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            parsed = None
    if parsed is None:
        sample = str(raw) if not isinstance(raw, str) else raw
        if _REDACT_EMAIL_RE.search(sample) or _REDACT_KEY_RE.search(sample) or _REDACT_URL_CRED_RE.search(sample) or _REDACT_SESSION_RE.search(sample):
            return {
                "redacted": True,
                "snippet": None,
                "redaction_reason": "sensitive_non_json",
                "output_length": len(sample),
            }
        return {
            "redacted": True,
            "snippet": None,
            "redaction_reason": "non_json_output",
            "output_length": len(sample),
        }
    try:
        skeleton = _skeleton_json(parsed)
        encoded = json.dumps(skeleton, ensure_ascii=True)
        if len(encoded) > max_chars:
            return {
                "redacted": True,
                "snippet": None,
                "redaction_reason": "snippet_too_long",
                "output_length": len(text) if text is not None else len(encoded),
                "root_type": _json_type_name(parsed),
            }
        decoded_probe = json.loads(encoded)
        probe_text = json.dumps(decoded_probe, ensure_ascii=False)
        if (
            _REDACT_EMAIL_RE.search(probe_text)
            or _REDACT_KEY_RE.search(probe_text)
            or _REDACT_URL_CRED_RE.search(probe_text)
            or _REDACT_SESSION_RE.search(probe_text)
        ):
            return {
                "redacted": True,
                "snippet": None,
                "redaction_reason": "sensitive_residual",
                "output_length": len(text) if text is not None else None,
            }
        return {
            "redacted": True,
            "snippet": encoded,
            "redaction_reason": "",
            "output_length": len(text) if text is not None else None,
            "root_type": _json_type_name(parsed),
        }
    except (TypeError, ValueError, json.JSONDecodeError):
        return {
            "redacted": True,
            "snippet": None,
            "redaction_reason": "skeleton_failed",
        }


def _schema_failures_from_exc(exc: BaseException) -> list[dict[str, str]]:
    attached = getattr(exc, "schema_failures", None)
    if isinstance(attached, list):
        bounded = _safe_schema_failures(attached)
        if bounded:
            return bounded
    invalid_type = getattr(exc, "invalid_output_type", None)
    if invalid_type:
        return [{
            "path": "unknown_path",
            "expected": _safe_schema_token(invalid_type, fallback="unknown_expected"),
            "actual_type": _safe_schema_token(type(exc).__name__, fallback="unknown_type"),
        }]
    return [{
        "path": "unknown_path",
        "expected": "review_schema",
        "actual_type": _safe_schema_token(type(exc).__name__, fallback="unknown_type"),
    }]


_REQUEST_FIELDS = frozenset({
    "model", "messages", "temperature", "response_format", "max_tokens",
    "reasoning_effort", "thinking",
})


def _request_payload(
    endpoint: ModelEndpoint,
    *,
    system: str,
    user: str,
    temperature: float,
    max_tokens: int,
    json_mode: bool,
) -> dict[str, Any]:
    if endpoint.wire_api == "responses":
        effort = endpoint.reasoning_effort or "none"
        payload = {
            "model": endpoint.model, "instructions": system, "input": user,
            "max_output_tokens": max_tokens, "reasoning": {"effort": effort},
            "store": False, "stream": False, "tools": [], "service_tier": "default",
        }
        # Models that reason reject a temperature.
        if effort == "none":
            payload["temperature"] = temperature
        if json_mode:
            # OpenAI rejects JSON mode unless the word "json" appears in the
            # input messages; the instructions field does not count.
            del payload["instructions"]
            payload["input"] = [
                {"role": "developer", "content": system},
                {"role": "user", "content": user},
            ]
            payload["text"] = {"format": {"type": "json_object"}}
        return payload
    if endpoint.wire_api == "interactions":
        # W20 verified this native interface. Argon ignores temperature, so do
        # not pretend a requested temperature controls this candidate.
        return {
            "model": endpoint.model, "system_instruction": system, "input": user,
            "store": False,
            "generation_config": {"max_output_tokens": max_tokens,
                                  "thinking_level": "low", "thinking_summaries": "none"},
        }
    payload: dict[str, Any] = {
        "model": endpoint.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    if endpoint.provider == "command":
        payload["reasoning_effort"] = "low"
    elif endpoint.provider == "deepseek" and endpoint.model.startswith(("deepseek-v4", "deepseek-flash")):
        payload["thinking"] = {"type": "disabled"}
    if set(payload) - _REQUEST_FIELDS:
        raise AssertionError("provider request contains a non-allowlisted field")
    return payload


def _error_payload(response: httpx.Response) -> dict[str, Any]:
    try:
        parsed = response.json()
    except (ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _classify_status(response: httpx.Response) -> GenerationModelCallError:
    from src.agents.model_failure_policy import confirmed_long_failure

    status = response.status_code
    long_failure = confirmed_long_failure(response)
    if long_failure is not None:
        return GenerationModelCallError(long_failure, "ACCOUNT", status=status)
    if status == 429:
        retry_after = response.headers.get("retry-after", "").strip()
        seconds = None
        try:
            seconds = int(retry_after)
        except ValueError:
            try:
                instant = parsedate_to_datetime(retry_after)
                seconds = int((instant - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
        bounded = min(3600, max(1, seconds)) if seconds is not None else None
        return GenerationModelCallError(
            "RATE_LIMIT", "PROVIDER", status=status, retry_after_seconds=bounded
        )
    if 500 <= status <= 599:
        return GenerationModelCallError("HTTP_5XX", "PROVIDER", status=status)
    return GenerationModelCallError("UNKNOWN", "UNKNOWN", status=status)


def _parse_response(response: httpx.Response, *, max_tokens: int | None = None,
                    wire_api: str = "chat_completions") -> str:
    if response.status_code != 200:
        raise _classify_status(response)
    content_type = response.headers.get("content-type", "").lower()
    if "application/json" not in content_type or not response.content.strip():
        raise GenerationModelCallError("WIRE_INVALID", "PROVIDER", status=200)
    try:
        data = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise GenerationModelCallError("WIRE_INVALID", "PROVIDER", status=200) from exc
    if wire_api in {"responses", "interactions"}:
        if not isinstance(data, dict):
            raise GenerationModelCallError("WIRE_INVALID", "PROVIDER", status=200)
        status = data.get("status")
        if status != "completed":
            code = "TRUNCATED" if status == "incomplete" else "SCHEMA_INVALID"
            raise GenerationModelCallError(code, "MODEL", status=200)
        containers = data.get("output", []) if wire_api == "responses" else data.get("steps", data.get("outputs", []))
        def texts(value):
            if isinstance(value, list):
                return [text for item in value for text in texts(item)]
            if not isinstance(value, dict) or value.get("type") in {"thought", "thinking", "reasoning", "thought_summary"}:
                return []
            if value.get("type") in {"text", "output_text"} and isinstance(value.get("text"), str):
                return [value["text"]]
            return [text for key in ("content", "outputs", "parts") for text in texts(value.get(key, []))]
        text = "".join(texts(containers))
        if not text.strip():
            raise GenerationModelCallError("SCHEMA_INVALID", "MODEL", status=200)
        return text
    finish_reason: Any = None
    content: Any = None
    try:
        choice = data["choices"][0]
        finish_reason = choice["finish_reason"]
        content = choice["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise GenerationModelCallError(
            "SCHEMA_INVALID",
            "MODEL",
            status=200,
            diagnostic={
                "schema_failures": [{
                    "path": "choices[0].message.content",
                    "expected": "object",
                    "actual_type": _json_type_name(data),
                }],
                "redacted_output": redact_model_output_for_diagnostics(data),
                "max_tokens": max_tokens,
            },
        ) from exc
    output_length = len(content) if isinstance(content, str) else 0
    if not isinstance(finish_reason, str):
        raise GenerationModelCallError(
            "SCHEMA_INVALID",
            "MODEL",
            status=200,
            diagnostic={
                "schema_failures": [{
                    "path": "choices[0].finish_reason",
                    "expected": "str",
                    "actual_type": _json_type_name(finish_reason),
                }],
                "finish_reason": None,
                "output_length": output_length,
                "max_tokens": max_tokens,
                "redacted_output": redact_model_output_for_diagnostics(content),
            },
        )
    if finish_reason != "stop":
        code: FailureCode = "TRUNCATED" if finish_reason in {"length", "max_tokens"} else "SCHEMA_INVALID"
        raise GenerationModelCallError(
            code,
            "MODEL",
            status=200,
            diagnostic={
                "finish_reason": finish_reason,
                "output_length": output_length,
                "max_tokens": max_tokens,
                "schema_failures": [] if code == "TRUNCATED" else [{
                    "path": "choices[0].finish_reason",
                    "expected": "stop",
                    "actual_type": "str",
                }],
                "redacted_output": redact_model_output_for_diagnostics(content),
            },
        )
    if not isinstance(content, str) or not content.strip():
        raise GenerationModelCallError(
            "SCHEMA_INVALID",
            "MODEL",
            status=200,
            diagnostic={
                "schema_failures": [{
                    "path": "choices[0].message.content",
                    "expected": "non_empty_str",
                    "actual_type": _json_type_name(content),
                }],
                "finish_reason": finish_reason,
                "output_length": output_length,
                "max_tokens": max_tokens,
                "redacted_output": redact_model_output_for_diagnostics(content),
            },
        )
    return content


async def call_endpoint(
    endpoint: ModelEndpoint,
    *,
    system: str,
    user: str,
    budget: SharedCallBudget,
    temperature: float,
    max_tokens: int,
    json_mode: bool = True,
    client: httpx.AsyncClient | None = None,
    content_validator: Callable[[str], Any] | None = None,
    validated_callback: Callable[[RoutedModelResponse], None] | None = None,
    role: str | None = None,
    enforce_runtime: bool = False,
    has_alternatives: bool = False,
) -> RoutedModelResponse:
    if (
        not endpoint.model.strip()
        or not endpoint.api_key.strip()
        or not endpoint.base_url.startswith(("https://", "http://"))
    ):
        raise GenerationModelCallError("UNKNOWN", "UNKNOWN")
    tracked = trial = False
    generation = None
    if enforce_runtime and role in {"selector", "review"}:
        tracked, trial, generation = await _prepare_endpoint_attempt(
            endpoint, role=role, has_alternatives=has_alternatives)
    try:
        sequence, _ = await budget.claim()
    except BaseException:
        if tracked and trial and generation is not None and role is not None:
            await _release_endpoint_trial(endpoint, role=role, generation=generation)
        raise
    payload = _request_payload(
        endpoint, system=system, user=user, temperature=temperature,
        max_tokens=max_tokens, json_mode=json_mode,
    )
    owned_client = client is None
    http = client or httpx.AsyncClient()
    started = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    response: httpx.Response | None = None
    observed_success: bool | None = None
    observed_failure: str | None = None
    observed_scope: str | None = None
    observed_retry_after: int | None = None
    try:
        try:
            timeout = budget.deadline_monotonic - time.monotonic() - budget.reserve_seconds
            if timeout <= 0:
                raise asyncio.TimeoutError("model call deadline exhausted")
            response = await asyncio.wait_for(
                http.post(
                    endpoint.base_url.rstrip("/") + {
                        "responses": "/responses", "interactions": "/interactions"
                    }.get(endpoint.wire_api, "/chat/completions"),
                    headers=({"x-goog-api-key": endpoint.api_key, "Api-Revision": "2026-05-20"}
                             if endpoint.wire_api == "interactions" else
                             {"Authorization": f"Bearer {endpoint.api_key}"}),
                    json=payload,
                    timeout=max(0.001, timeout),
                ),
                timeout=max(0.001, timeout),
            )
        except asyncio.CancelledError:
            error = GenerationModelCallError("CANCELLED", "UNKNOWN")
            budget.record(
                sequence=sequence, endpoint=endpoint, status="CANCELLED",
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                error=error,
            )
            raise
        except asyncio.TimeoutError as exc:
            error = GenerationModelCallError("TIMEOUT", "PROVIDER")
            observed_success, observed_failure = False, error.code
            observed_scope = error.scope
            observed_retry_after = error.retry_after_seconds
            budget.record(
                sequence=sequence, endpoint=endpoint, status="TIMEOUT",
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                error=error,
            )
            raise error from exc
        except httpx.TimeoutException as exc:
            error = GenerationModelCallError("TIMEOUT", "PROVIDER")
            observed_success, observed_failure = False, error.code
            observed_scope = error.scope
            observed_retry_after = error.retry_after_seconds
            budget.record(
                sequence=sequence, endpoint=endpoint, status="TIMEOUT",
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                error=error,
            )
            raise error from exc
        except httpx.TransportError as exc:
            error = GenerationModelCallError("TRANSPORT", "PROVIDER")
            observed_success, observed_failure = False, error.code
            observed_scope = error.scope
            observed_retry_after = error.retry_after_seconds
            budget.record(
                sequence=sequence, endpoint=endpoint, status="FAILED",
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                error=error,
            )
            raise error from exc
        try:
            parsed = _parse_response(response, max_tokens=max_tokens, wire_api=endpoint.wire_api)
            if content_validator is not None:
                try:
                    content_validator(parsed)
                except GenerationModelCallError:
                    raise
                except Exception as exc:
                    raise GenerationModelCallError(
                        "SCHEMA_INVALID",
                        "MODEL",
                        status=200,
                        diagnostic={
                            "finish_reason": "stop",
                            "schema_failures": _schema_failures_from_exc(exc),
                            "invalid_output_type": (
                                str(getattr(exc, "invalid_output_type", "") or type(exc).__name__)
                            ),
                            "output_length": len(parsed) if isinstance(parsed, str) else 0,
                            "max_tokens": max_tokens,
                            "redacted_output": redact_model_output_for_diagnostics(parsed),
                        },
                    ) from exc
            if time.monotonic() >= budget.deadline_monotonic - budget.reserve_seconds:
                raise GenerationModelCallError("TIMEOUT", "PROVIDER")
        except GenerationModelCallError as error:
            if error.code == "SCHEMA_INVALID" and isinstance(locals().get("parsed"), str):
                # Private, in-memory input for the one same-endpoint formatting
                # correction. It is never copied into telemetry or diagnostics.
                error.repair_input = parsed
            observed_success, observed_failure = False, error.code
            observed_scope = error.scope
            observed_retry_after = error.retry_after_seconds
            budget.record(
                sequence=sequence, endpoint=endpoint,
                status="TIMEOUT" if error.code == "TIMEOUT" else "FAILED",
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                error=error,
            )
            raise
        except Exception as exc:
            error = GenerationModelCallError("SCHEMA_INVALID", "MODEL", status=200)
            observed_success, observed_failure = False, error.code
            observed_scope = error.scope
            observed_retry_after = error.retry_after_seconds
            budget.record(
                sequence=sequence, endpoint=endpoint, status="FAILED",
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                error=error,
            )
            raise error from exc
        budget.record(
            sequence=sequence, endpoint=endpoint, status="SUCCESS",
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
        )
        observed_success = True
        result = RoutedModelResponse(parsed, endpoint, sequence)
        if validated_callback is not None:
            validated_callback(result)
        return result
    finally:
        # Add to the existing per-job ledger without changing dispatch or budgets.
        try:
            from src.agents import llm
            from src.agents.model_call_observation import observed_usage
            call = next((c for c in reversed(budget.calls) if c["sequence"] == sequence), {})
            call.update(started_at=started_at, finished_at=datetime.now(timezone.utc).isoformat(),
                        wire_api=endpoint.wire_api)
            usage = None
            if response is not None:
                try:
                    payload = response.json()
                    usage = payload.get("usage") if isinstance(payload, dict) else None
                    if endpoint.wire_api == "interactions" and isinstance(usage, dict):
                        usage = {"input_tokens": usage.get("total_input_tokens"),
                                 "output_tokens": usage.get("total_output_tokens"),
                                 "output_tokens_details": {"reasoning_tokens": usage.get("total_thought_tokens")},
                                 "input_tokens_details": {"cached_tokens": usage.get("total_cached_tokens", usage.get("cached_tokens"))}}
                except (ValueError, TypeError):
                    pass
            context = llm.current_llm_call_context()
            call["review_round"] = context.get("review_round") if role == "review" else None
            call["request_parameters"] = {
                key: value for key, value in _request_payload(
                    endpoint, system="", user="", temperature=temperature,
                    max_tokens=max_tokens, json_mode=json_mode).items()
                if key not in {"messages", "instructions", "system_instruction", "input"}
            }
            if response is not None:
                try:
                    response_data = response.json()
                    model = response_data.get("model") if isinstance(response_data, dict) else None
                    if isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9._:/-]{1,120}", model):
                        call["response_model"] = model
                except ValueError:
                    pass
            llm._record_llm_call({
                "status": str(call.get("status") or "FAILED").lower(),
                "role": role or "unknown", "stage": context.get("stage"),
                "safe_purpose": "POI_SELECTOR" if role == "selector" else "REVIEW",
                "provider": endpoint.provider, "model": endpoint.model,
                "wire_api": endpoint.wire_api,
                "endpoint_label": endpoint.endpoint_id,
                "relay_endpoint": endpoint.endpoint_id,
                "latency_ms": max(0, int((time.monotonic() - started) * 1000)),
                "observed_usage": observed_usage(usage),
                "error_code": call.get("error_code"),
                "http_status_code": call.get("http_status") or (response.status_code if response is not None else None),
                "response_received": response is not None,
                "attempt": context.get("attempt") or 1,
                "publish_retry_round": context.get("publish_retry_round") or 0,
                "owning_stage": context.get("owning_stage"),
                "owning_attempt": context.get("owning_attempt"),
                "owning_publish_retry_round": context.get("owning_publish_retry_round"),
            })
        except Exception:
            logger.warning("model call observation unavailable")
        async def finish_attempt() -> None:
            if owned_client:
                await http.aclose()
            if observed_success is not None and role in {"selector", "review"}:
                await _observe_endpoint_attempt(
                    endpoint, role=role, success=observed_success,
                    failure=observed_failure,
                    failure_scope=observed_scope,
                    trial=trial if tracked else False,
                    generation=generation if tracked else None,
                    retry_after_seconds=observed_retry_after,
                    has_alternatives=has_alternatives,
                )
            elif tracked and trial and generation is not None and role is not None:
                await _release_endpoint_trial(endpoint, role=role, generation=generation)

        cleanup = asyncio.create_task(finish_attempt())
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise


async def _first_valid_response(
    calls: list[Callable[[Callable[[RoutedModelResponse], None]], Any]],
) -> RoutedModelResponse:
    """Return the first fully validated response and clean up every sibling."""
    validated: asyncio.Queue[tuple[RoutedModelResponse, asyncio.Task[Any]]] = asyncio.Queue()

    def on_validated(response: RoutedModelResponse) -> None:
        task = asyncio.current_task()
        assert task is not None
        validated.put_nowait((response, task))

    tasks = [asyncio.create_task(call(on_validated)) for call in calls]
    failures: list[BaseException] = []
    winner_wait: asyncio.Task[tuple[RoutedModelResponse, asyncio.Task[Any]]] | None = None
    try:
        remaining = set(tasks)
        while remaining:
            winner_wait = asyncio.create_task(validated.get())
            done, _ = await asyncio.wait(
                [winner_wait, *remaining], return_when=asyncio.FIRST_COMPLETED
            )
            if winner_wait in done or not validated.empty():
                if winner_wait in done:
                    winner, winner_task = winner_wait.result()
                else:
                    winner, winner_task = validated.get_nowait()
                    winner_wait.cancel()
                    await asyncio.gather(winner_wait, return_exceptions=True)
                    winner_wait = None
                for sibling in remaining:
                    if sibling is not winner_task and not sibling.done():
                        sibling.cancel()
                winner = replace(winner, adopted_at=datetime.now(timezone.utc).isoformat())
                await asyncio.gather(*tasks, return_exceptions=True)
                return winner
            winner_wait.cancel()
            await asyncio.gather(winner_wait, return_exceptions=True)
            winner_wait = None
            for task in tuple(remaining):
                if not task.done():
                    continue
                remaining.remove(task)
                try:
                    task.result()
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:
                    failures.append(exc)
        if failures:
            last = failures[-1]
            if isinstance(last, GenerationModelCallError):
                siblings = []
                for item in failures:
                    if isinstance(item, GenerationModelCallError):
                        siblings.append({
                            "error_code": item.code,
                            "failure_scope": item.scope,
                            "diagnostic": dict(item.diagnostic or {}),
                        })
                last.diagnostic = _sanitize_call_diagnostic({
                    **dict(last.diagnostic or {}),
                    "sibling_failures": siblings,
                })
            raise last
        raise GenerationModelCallError("UNKNOWN", "UNKNOWN")
    except asyncio.CancelledError:
        if winner_wait is not None and not winner_wait.done():
            winner_wait.cancel()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    finally:
        if winner_wait is not None and not winner_wait.done():
            winner_wait.cancel()
            await asyncio.gather(winner_wait, return_exceptions=True)
        remaining = [task for task in tasks if not task.done()]
        for task in remaining:
            task.cancel()
        if remaining:
            await asyncio.gather(*remaining, return_exceptions=True)


async def route_selector(
    *,
    primary: ModelEndpoint,
    official_ds: ModelEndpoint,
    system: str,
    user: str,
    budget: SharedCallBudget,
    max_tokens: int,
    temperature: float,
    client: httpx.AsyncClient | None = None,
    content_validator: Callable[[str], Any] | None = None,
    enforce_runtime: bool = False,
    backup_delay_seconds: float | None = None,
) -> RoutedModelResponse:
    return await _primary_then_backup(
        primary=primary, backup=official_ds, role="selector", system=system, user=user,
        budget=budget, max_tokens=max_tokens, temperature=temperature, client=client,
        content_validator=content_validator, enforce_runtime=enforce_runtime,
        backup_delay_seconds=backup_delay_seconds, format_repair=None,
    )


async def route_review(
    *,
    primary_glm: ModelEndpoint,
    same_site_luna: ModelEndpoint,
    official_ds: ModelEndpoint,
    writer_family: Literal["OPUS", "DS"],
    system: str,
    user: str,
    budget: SharedCallBudget,
    max_tokens: int,
    temperature: float,
    client: httpx.AsyncClient | None = None,
    content_validator: Callable[[str], Any] | None = None,
    allow_backup: bool = True,
    enforce_runtime: bool = False,
    backup_delay_seconds: float | None = None,
    format_repair: Callable[..., Awaitable[RoutedModelResponse]] | None = None,
) -> RoutedModelResponse:
    # Writer identity no longer excludes the official DeepSeek reviewer.
    return await _primary_then_backup(
        primary=primary_glm, backup=official_ds if allow_backup else None,
        role="review", system=system, user=user, budget=budget, max_tokens=max_tokens,
        temperature=temperature, client=client, content_validator=content_validator,
        enforce_runtime=enforce_runtime, backup_delay_seconds=backup_delay_seconds,
        format_repair=format_repair,
    )


async def _wait_backup_trigger(
    primary_failed: asyncio.Event,
    delay_seconds: float | None,
    *,
    monotonic: Callable[[], float] | None = None,
) -> str:
    if delay_seconds is None:
        await primary_failed.wait()
        return "primary_failed_or_unavailable"
    clock = monotonic or time.monotonic
    switch_at = clock() + delay_seconds
    while not primary_failed.is_set():
        remaining = switch_at - clock()
        if remaining <= 0:
            return "primary_slow"
        try:
            await asyncio.wait_for(primary_failed.wait(), timeout=remaining)
        except asyncio.TimeoutError:
            # asyncio may wake a timer up to one clock-resolution early. The
            # configured switch time, rather than that wakeup, opens backup.
            continue
    return "primary_failed_or_unavailable"


async def _primary_then_backup(
    *, primary: ModelEndpoint, backup: ModelEndpoint | None, role: str,
    system: str, user: str, budget: SharedCallBudget, max_tokens: int,
    temperature: float, client: httpx.AsyncClient | None,
    content_validator: Callable[[str], Any] | None, enforce_runtime: bool,
    backup_delay_seconds: float | None,
    format_repair: Callable[..., Awaitable[RoutedModelResponse]] | None,
) -> RoutedModelResponse:
    """Start only the primary; a waiting coroutine is not a provider call."""
    primary_failed = asyncio.Event()
    primary_valid = asyncio.Event()
    reasons: dict[str, str] = {primary.endpoint_id: "primary"}
    def configured(endpoint):
        return bool(endpoint and endpoint.model.strip() and endpoint.api_key.strip()
                    and endpoint.base_url.startswith(("https://", "http://")))
    has_alternatives = bool(
        configured(primary) and configured(backup) and independent_backup(primary, backup))

    async def invoke(endpoint, callback):
        try:
            return await call_endpoint(
                endpoint, system=system, user=user, budget=budget,
                temperature=temperature, max_tokens=max_tokens, client=client,
                content_validator=content_validator, role=role,
                validated_callback=callback, enforce_runtime=enforce_runtime,
                has_alternatives=has_alternatives,
            )
        except GenerationModelCallError as exc:
            if format_repair is None or not hasattr(exc, "repair_input"):
                raise
            try:
                return await format_repair(endpoint, exc.repair_input, callback,
                                           has_alternatives=has_alternatives)
            except GenerationModelCallError as repair_error:
                # A transport failure while correcting invalid output must not
                # erase the original non-technical failure for W9.
                repair_error.diagnostic = _sanitize_call_diagnostic({
                    **repair_error.diagnostic,
                    "sibling_failures": [{"error_code": exc.code, "failure_scope": exc.scope}],
                })
                raise

    async def run_primary(callback):
        def validated(response):
            primary_valid.set()
            callback(response)
        try:
            return await invoke(primary, validated)
        except Exception:
            primary_failed.set()
            raise

    async def run_backup(callback):
        assert backup is not None
        reason = await _wait_backup_trigger(primary_failed, backup_delay_seconds)
        if primary_valid.is_set():
            # The selected primary may still be settling its state/telemetry.
            await asyncio.Future()
        reasons[backup.endpoint_id] = reason
        try:
            return await invoke(backup, callback)
        finally:
            # Reaching the switch point is not a provider request. Admission or
            # the unchanged phase deadline can still prevent the backup call.
            if role == "review" and any(
                call["sequence"] > start_sequence and call["endpoint_id"] == backup.endpoint_id
                for call in budget.calls
            ):
                await budget.claim_backup()

    calls = [run_primary]
    if backup is not None and independent_backup(primary, backup):
        calls.append(run_backup)
    start_sequence = budget.calls_started
    try:
        response = await _first_valid_response(calls)
    finally:
        for call in budget.calls:
            if call["sequence"] > start_sequence:
                call["switch_reason"] = reasons.get(call["endpoint_id"], "primary")
    budget.mark_adopted(response.call_sequence)
    for call in budget.calls:
        if call["sequence"] > start_sequence:
            call["adopted"] = call["sequence"] == response.call_sequence
            call["adopted_at"] = response.adopted_at if call["adopted"] else None
    return response
