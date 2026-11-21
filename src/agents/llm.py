"""Shared LLM client for all agents (OpenAI-compat providers).

Each role (intent / extract / writer / review) resolves its own
provider, model, and API key from config at runtime.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
from contextlib import contextmanager
import logging
import random
import re
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from contextvars import Token
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

import httpx
from openai import AsyncOpenAI

from src.agents.role_relay_store import POOLED_ROLES, PostgresRoleRelayStore
from src.agents.writer_relay_router import WriterRelayRouter, WriterStreamEvent, classify_failure
from src.agents.writer_relay_store import PostgresWriterRelayStore
from src.agents.model_call_observation import enrich_record, internal_reason, observed_usage, provider_error_code
from src.config import get_settings

logger = logging.getLogger(__name__)


class LLMTransportError(RuntimeError):
    """Typed provider transport exhaustion safe for policy classification."""

    def __init__(self, transport_type: str, message: str) -> None:
        super().__init__(message)
        self.transport_type = transport_type


_call_context: contextvars.ContextVar[dict[str, object]] = contextvars.ContextVar(
    "llm_call_context",
    default={},
)
_call_records: contextvars.ContextVar[list[dict[str, Any]] | None] = (
    contextvars.ContextVar("llm_call_records", default=None)
)
_last_relay_retry_count: contextvars.ContextVar[int] = contextvars.ContextVar(
    "llm_last_relay_retry_count",
    default=0,
)
_last_endpoint_failover_count: contextvars.ContextVar[int] = contextvars.ContextVar(
    "llm_last_endpoint_failover_count",
    default=0,
)
_last_failed_endpoint_label: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "llm_last_failed_endpoint_label",
    default=None,
)
_last_chat_metadata: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "llm_last_chat_metadata",
    default={},
)

SPECULATIVE_DS_PROMPT_VERSION = "flash_v6"

TRACE_TRUTH_SCHEMA_VERSION = 1
TRACE_TRUTH_OBSERVATION_REVISION = 1
TRACE_TRUTH_MAX_RETURNED_CALLS = 100
_TRACE_TRUTH_ROLES = frozenset({
    "intent", "extract", "grouping", "selector", "writer", "review",
    "route_planner",
})
_TRACE_TRUTH_PURPOSES = frozenset({
    "INTENT_PARSE",
    "DATA_EXTRACTION",
    "SEMANTIC_GROUPING",
    "POI_SELECTOR",
    "ROUTE_PLANNER",
    "WRITER_PRIMARY",
    "WRITER_STANDBY",
    "PUBLISH_RETRY_WRITER",
    "REVIEW",
    "FRAGMENT_REPAIR",
    "FAILURE_MESSAGE_POLISH",
    "OTHER_SAFE",
})
_TRACE_TRUTH_GENERATORS = frozenset({"opus", "ds_flash", "safe"})

# DS uses the same system prompt as Opus (SINGLE_PLAN_SYSTEM_PROMPT) plus
# a short suffix with DeepSeek-specific execution constraints (no thinking
# mode, stricter JSON discipline).  Imported lazily to avoid circular deps.
_DS_EXECUTION_SUFFIX = """

DeepSeek 非思考模式专项执行约束：
- 先在内部逐项核对 slot key，最终只输出 JSON；不要输出核对过程。
- 把输入 slots 视为不可变账本：每个 (plan_index, day, place_id) 恰好返回一次，三项数值逐字复制；禁止自行创建、补全、合并、排序或遗漏 key。
- poi_fragments 的 text 不得重复当前地点名；除 arrival_from 或当天已经完成的更早站点外，不得出现任何其他专名。
- 不要在普通 fragment 中生成任何数字。只有 Food Stop 专项规则明确要求且输入给出对应值时，才可逐字复制评分、价格或步行分钟。
- day_openings 必须覆盖输入中的每个 Day，恰好一条；一句、60 字以内；地点名、数字和事实边界沿用上文 day_openings 规则。
- summary 必须为单个 80-120 个中文字符的字符串；不得出现任何候选/锁定地点名、数字、交通方式、价格、营业时间或外部事实。
- 顶层字段为 poi_fragments、day_openings、summary，以及授权输入支持的可选 packing_checklist 和 travel_tips；元素字段沿用上文 JSON 契约。
- 输出前静默检查：JSON 可解析、key 集合完全相等、无重复 key、无额外字段、opening 无数字、summary 无地点名。任何不确定信息直接不写，不要解释或道歉。
- [INTERNAL 开头的 writing_hint 是内部写作参考，不要输出到 text 中。"""

# v0.15.1 timeline prompt: the same discipline, with the new opening, wrap and
# summary rules instead of the one-sentence opening and place-free summary.
_DS_TIMELINE_EXECUTION_SUFFIX = """

DeepSeek 非思考模式专项执行约束：
- 先在内部逐项核对 slot key，最终只输出 JSON；不要输出核对过程。
- 把输入 slots 视为不可变账本：每个 (plan_index, day, place_id) 恰好返回一次，三项数值逐字复制；禁止自行创建、补全、合并、排序或遗漏 key。
- 每一天都要写：day_openings 覆盖输入中的每个 Day，恰好一条，2～4 句，不点具体地点名；不要只写第一天就停。
- poi_fragments 的 text 不得重复当前地点名；除当天更早的站和紧接着的下一站外，不得出现其他专名。
- 普通 fragment 里不写钟点、分钟数和交通方式。只有 Food Stop 专项规则明确要求且输入给出对应值时，才可逐字复制评分、价格或步行分钟。
- summary 是单个 80～150 个中文字符的字符串，可以出现本路线的地点名，不写价格、营业时间或交通方式。
- 输出前静默检查：JSON 可解析、key 集合完全相等、无重复 key、无额外字段、每天都有 opening。任何不确定的信息直接不写，不要解释或道歉。"""


def _ds_system_prompt() -> str:
    """Build DS system prompt = the primary's system prompt + DS suffix."""
    from src.agents.final_writer import single_plan_system_prompt, writer_prompt_version
    from src.agents.lodging_section import writer_system_prompt
    if writer_prompt_version() == "article":
        # The same occasion and facts as the primary; the JSON discipline does not apply.
        return writer_system_prompt(single_plan_system_prompt())
    suffix = (
        _DS_TIMELINE_EXECUTION_SUFFIX
        if writer_prompt_version() == "timeline"
        else _DS_EXECUTION_SUFFIX
    )
    return writer_system_prompt(single_plan_system_prompt()) + suffix


def _ds_prompt_label() -> str:
    from src.agents.final_writer import writer_prompt_version
    return {"timeline": "flash_timeline", "article": "flash_article"}.get(
        writer_prompt_version(), SPECULATIVE_DS_PROMPT_VERSION)
# Job-isolated observation sinks for timeout/cancel persist. Never use a single
# process-global slot: concurrent jobs would cross-contaminate.
_observation_sinks: dict[str, dict[str, Any]] = {}
_observation_sink_lock = asyncio.Lock() if False else None  # created lazily for import safety
import threading
_observation_sink_thread_lock = threading.Lock()


def bind_observation_sink(job_id: str) -> str:
    """Create/replace an empty job-owned observation sink. Returns job_id."""
    key = str(job_id or "").strip()
    if not key:
        raise ValueError("job_id required for observation sink")
    with _observation_sink_thread_lock:
        _observation_sinks[key] = {
            "job_id": key,
            "summary": None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    return key


def set_job_observation_flush(job_id: str | None, summary: dict[str, Any] | None) -> None:
    """Store a sanitized observation summary for one job only."""
    key = str(job_id or "").strip()
    if not key:
        return
    with _observation_sink_thread_lock:
        _observation_sinks[key] = {
            "job_id": key,
            "summary": summary,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }


def pop_job_observation_flush(job_id: str | None) -> dict[str, Any] | None:
    """Pop and clear one job's observation summary. Other jobs untouched."""
    key = str(job_id or "").strip()
    if not key:
        return None
    with _observation_sink_thread_lock:
        payload = _observation_sinks.pop(key, None)
    if not payload:
        return None
    summary = payload.get("summary")
    return summary if isinstance(summary, dict) else None


def current_llm_observation_summary() -> dict[str, Any]:
    """Summarize in-context observation records without clearing them."""
    records = _call_records.get() or []
    return summarize_llm_call_records(list(records))


def current_llm_observation_record_count() -> int:
    """Return the current source-observation cursor without exposing records."""
    return len(_call_records.get() or [])


def _trace_truth_role(record: dict[str, Any]) -> str | None:
    role = str(record.get("role") or "").strip().lower()
    aliases = {
        "final_writer": "writer",
        "writer_standby": "writer",
        "yuntu_review": "review",
        "semantic_grouping": "grouping",
        "poi_selector": "selector",
        "route_planner": "route_planner",
        "intent_parser": "intent",
        "data_extraction": "extract",
    }
    role = aliases.get(role, role)
    return role if role in _TRACE_TRUTH_ROLES else None


def _trace_truth_purpose(record: dict[str, Any], role: str) -> str:
    explicit = str(record.get("safe_purpose") or "").strip().upper()
    if explicit in _TRACE_TRUTH_PURPOSES:
        return explicit
    reason = str(record.get("call_reason") or "").strip().lower()
    if reason in {"intent_parse", "intent"} or role == "intent":
        return "INTENT_PARSE"
    if "extract" in reason or role == "extract":
        return "DATA_EXTRACTION"
    if "semantic_grouping" in reason or role == "grouping":
        return "SEMANTIC_GROUPING"
    if "poi_selector" in reason or role == "selector":
        return "POI_SELECTOR"
    if "route_planner" in reason or role == "route_planner":
        return "ROUTE_PLANNER"
    if "fragment_repair" in reason or "keyed_fragment_repair" in reason:
        return "FRAGMENT_REPAIR"
    if reason in {"publish_failure_polish", "city_clarification_polish"}:
        return "FAILURE_MESSAGE_POLISH"
    if role == "review":
        return "REVIEW"
    if role == "writer":
        if int(record.get("publish_retry_round") or 0) > 0:
            return "PUBLISH_RETRY_WRITER"
        return "WRITER_PRIMARY"
    return "OTHER_SAFE"


def public_llm_role_purpose(role: object, purpose: object) -> tuple[object, object]:
    """Alias the route planner only on the payload that leaves Travel.

    Observation records keep ``route_planner`` / ``ROUTE_PLANNER``. BFF
    ``LlmCallRole`` / ``LlmCallPurpose`` and Admin's matching unions have no
    pairing rule, and both accept ``selector`` with ``OTHER_SAFE``.
    """
    if role == "route_planner" or purpose == "ROUTE_PLANNER":
        return "selector", "OTHER_SAFE"
    return role, purpose


def _trace_truth_status(record: dict[str, Any]) -> str:
    status = str(record.get("status") or "").strip().lower()
    termination = str(record.get("termination_reason") or "").strip().lower()
    error_type = str(record.get("error_type") or "").strip().lower()
    if status == "success":
        return "SUCCESS"
    if status == "cancelled" or "cancel" in termination or "cancel" in error_type:
        return "CANCELLED"
    if (
        status == "timeout"
        or "timeout" in termination
        or "timeout" in error_type
        or "timedout" in error_type
    ):
        return "TIMEOUT"
    return "FAILED"


def project_llm_usage(
    records: list[dict[str, Any]] | None,
    *,
    complete: bool,
    adopted_generator: str | None = None,
) -> dict[str, Any]:
    """Map internal call observations to the frozen bounded safe contract."""
    if records is None:
        return {
            "schema_version": TRACE_TRUTH_SCHEMA_VERSION,
            "observation_revision": TRACE_TRUTH_OBSERVATION_REVISION,
            "state": "UNAVAILABLE",
            "total_call_count": 0,
            "returned_call_count": 0,
            "truncated": False,
            "adopted_generator": None,
            "calls": [],
        }

    generator = str(adopted_generator or "").strip().lower() or None
    if generator not in _TRACE_TRUTH_GENERATORS:
        generator = None

    calls: list[dict[str, Any]] = []
    for raw in records:
        if not isinstance(raw, dict):
            return project_llm_usage(None, complete=False)
        record = _sanitize_llm_record(raw)
        role = _trace_truth_role(record)
        provider = str(record.get("provider") or "").strip()
        model = str(record.get("model") or "").strip()
        if role is None or not provider or not model:
            return project_llm_usage(None, complete=False)
        try:
            sequence = max(0, int(record.get("observation_sequence") or 0))
            attempt = max(1, int(record.get("attempt") or 1))
            publish_retry_round = max(
                0, int(record.get("publish_retry_round") or 0)
            )
        except (TypeError, ValueError):
            return project_llm_usage(None, complete=False)
        public_role, public_purpose = public_llm_role_purpose(
            role, _trace_truth_purpose(record, role)
        )
        calls.append({
            "sequence": sequence,
            "provider": provider[:40],
            "model": model[:120],
            "role": public_role,
            "purpose": public_purpose,
            "status": _trace_truth_status(record),
            "attempt": attempt,
            "publish_retry_round": publish_retry_round,
        })

    if not calls:
        if records:
            return project_llm_usage(None, complete=False)
        return {
            "schema_version": TRACE_TRUTH_SCHEMA_VERSION,
            "observation_revision": TRACE_TRUTH_OBSERVATION_REVISION,
            "state": "NO_LLM" if complete else "UNAVAILABLE",
            "total_call_count": 0,
            "returned_call_count": 0,
            "truncated": False,
            "adopted_generator": generator if complete else None,
            "calls": [],
        }

    total = len(records)
    returned = calls[:TRACE_TRUTH_MAX_RETURNED_CALLS]
    return {
        "schema_version": TRACE_TRUTH_SCHEMA_VERSION,
        "observation_revision": TRACE_TRUTH_OBSERVATION_REVISION,
        "state": "OBSERVED" if complete else "PARTIAL",
        "total_call_count": total,
        "returned_call_count": len(returned),
        "truncated": total > TRACE_TRUTH_MAX_RETURNED_CALLS,
        "adopted_generator": generator,
        "calls": returned,
    }


def project_current_llm_usage(
    start_index: int,
    *,
    complete: bool,
    adopted_generator: str | None = None,
) -> dict[str, Any]:
    records = _call_records.get()
    if records is None:
        return project_llm_usage(
            None,
            complete=False,
            adopted_generator=adopted_generator,
        )
    return project_llm_usage(
        list(records[max(0, int(start_index)):]),
        complete=complete,
        adopted_generator=adopted_generator,
    )


def project_persisted_llm_usage_for_stage(
    observation: dict[str, Any] | None,
    *,
    stage: str,
    attempt: int,
    publish_retry_round: int,
    adopted_generator: str | None = None,
) -> dict[str, Any]:
    """Safely recover one step from a persisted whole-workflow observation."""
    if not isinstance(observation, dict):
        return project_llm_usage(None, complete=False)
    raw_records = observation.get("llm_call_records")
    if not isinstance(raw_records, list):
        return project_llm_usage(None, complete=False)
    target_stage = "FINAL_WRITER" if stage == "WRITER" else str(stage)
    selected: list[dict[str, Any]] = []
    explicit_ownership_complete = True
    for raw in raw_records:
        if not isinstance(raw, dict):
            explicit_ownership_complete = False
            continue
        owning_stage = str(raw.get("owning_stage") or "").strip()
        if not owning_stage:
            explicit_ownership_complete = False
            continue
        normalized_stage = (
            "FINAL_WRITER"
            if owning_stage in {"WRITER", "FINAL_WRITER"}
            else owning_stage
        )
        try:
            owning_attempt = int(raw["owning_attempt"])
            owning_round = int(raw["owning_publish_retry_round"])
        except (TypeError, ValueError):
            explicit_ownership_complete = False
            continue
        except KeyError:
            explicit_ownership_complete = False
            continue
        if normalized_stage != target_stage:
            continue
        if owning_attempt != max(1, int(attempt)):
            continue
        if owning_round != max(0, int(publish_retry_round)):
            continue
        selected.append(raw)
    complete = observation.get("trace_truth_observation_complete") is True
    if not explicit_ownership_complete:
        return project_llm_usage(None, complete=False)
    if not selected:
        return project_llm_usage([], complete=complete)
    return project_llm_usage(
        selected,
        complete=complete,
        adopted_generator=adopted_generator,
    )


# Backward-compatible aliases used by earlier O4 paths; route through job sinks
# when job_id is present in call context, otherwise no-op (no global slot).
def set_last_observation_flush(summary: dict[str, Any] | None) -> None:
    job_id = str((_call_context.get() or {}).get("job_id") or "").strip()
    if job_id:
        set_job_observation_flush(job_id, summary)


def pop_last_observation_flush() -> dict[str, Any] | None:
    job_id = str((_call_context.get() or {}).get("job_id") or "").strip()
    if job_id:
        return pop_job_observation_flush(job_id)
    return None

_PROVIDER_BASE_URLS: dict[str, str] = {
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "deepseek": "https://api.deepseek.com",
    "openai": "https://api.openai.com/v1",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",  # placeholder, overridden by config
}
# Profiles for the roles kept outside src/model_routes.py (intent, grouping,
# the closed review). The writer's "claude_opus" is only a label now.
_RELAY_PROFILES = frozenset({
    "gpt",
    "gpt_grouping",
    "gpt_review",
})
# Roles whose account, model and key come from src/model_routes.py.
_TABLE_ROLES = frozenset({"selector", "route_planner", "writer", "extract"})
_WRITER_RELAY_PROFILE = "claude_opus"
_RELAY_WIRE_APIS = frozenset({
    "openai_chat",
    "openai_responses",
    "anthropic_messages",
    "gemini_native",
})
_RELAY_REQUEST_TIMEOUT_SECONDS = 240
_RELAY_RETRY_DELAYS_SECONDS = (2.0, 5.0, 10.0)
_PRIMARY_FIRST_RELAY_PROFILES = frozenset({"claude_sonnet_backup"})
_RELAY_POOL_FAILOVER_STATUS_CODES = {
    401,
    403,
    408,
    409,
    429,
    500,
    502,
    503,
    504,
    520,
    521,
    522,
    523,
    524,
    525,
    526,
    527,
}

VALID_ROLES = frozenset({
    "intent",
    "extract",
    "writer",
    "review",
    "grouping",
    "selector",
    "route_planner",
})

# Keyed by (provider, base_url, api_key, verify_ssl) so relay profiles stay isolated.
_clients: dict[tuple[str, str, str, bool], AsyncOpenAI] = {}
_role_semaphores: dict[tuple[str, int], asyncio.Semaphore] = {}
_relay_endpoint_cooldowns: dict[tuple[str, str], float] = {}
_relay_endpoint_rotation: dict[str, int] = {}
_writer_relay_router: WriterRelayRouter | None = None
_role_relay_store: PostgresRoleRelayStore | None = None
_speculative_ds_bulkheads: dict[int, asyncio.Semaphore] = {}
_pooled_trial: contextvars.ContextVar[tuple[str, str, int] | None] = contextvars.ContextVar(
    "pooled_relay_trial", default=None
)
_pooled_generations: contextvars.ContextVar[dict[str, int]] = contextvars.ContextVar(
    "pooled_relay_generations", default={}
)
RECOVERY_PROBE_SYSTEM = "Reply with the single token ok."
RECOVERY_PROBE_USER = "ok"
ROLE_RELAY_HOT_PATH_TIMEOUT_SECONDS = 0.1
# HALF_OPEN recovery is opportunistic. Keep it short enough that the ordinary
# 40-second Review window still leaves a useful attempt for a healthy endpoint.
REVIEW_HALF_OPEN_PROBE_TIMEOUT_SECONDS = 5.0
REVIEW_CLOSED_SECOND_HOP_RESERVE_SECONDS = 20.0


@dataclass(frozen=True)
class RelayEndpoint:
    name: str
    base_url: str
    api_key: str
    wire_api: str
    model: str = ""


@dataclass(frozen=True)
class RoleConfig:
    provider: str
    model: str
    api_key: str
    base_url: str
    relay_profile: str = ""
    wire_api: str = "openai_chat"
    relay_endpoint: str = ""
    relay_pool: tuple[RelayEndpoint, ...] = ()

    def __iter__(self):
        yield self.provider
        yield self.model
        yield self.api_key


@dataclass(frozen=True)
class SpeculativeDSWriterResponse:
    text: str
    token_in: int
    token_out: int
    latency_ms: int
    model: str
    prompt_version: str = SPECULATIVE_DS_PROMPT_VERSION
    observed_usage: dict[str, int | None] | None = None


@contextmanager
def llm_call_context(**metadata: object) -> Iterator[None]:
    """Attach request-scoped metadata to LLM logs in the current async context."""
    current = _call_context.get()
    merged = {
        **current,
        **{key: value for key, value in metadata.items() if value is not None},
    }
    token = _call_context.set(merged)
    try:
        yield
    finally:
        _call_context.reset(token)


def current_llm_call_context() -> dict[str, object]:
    """Return a copy of request-scoped metadata for non-telemetry consumers."""
    return dict(_call_context.get() or {})


def _requested_max_tokens(default: int) -> int:
    value = (_call_context.get() or {}).get("max_tokens_request")
    try:
        requested = int(value) if value is not None else int(default)
    except (TypeError, ValueError):
        requested = int(default)
    return max(256, requested)


def _relay_request_timeout_seconds() -> float:
    """Cap every relay request by the absolute workflow deadline."""
    context = _call_context.get() or {}
    deadline = context.get(
        "workflow_deadline_monotonic"
    )
    try:
        remaining = float(deadline) - time.monotonic()
    except (TypeError, ValueError):
        return float(_RELAY_REQUEST_TIMEOUT_SECONDS)
    if remaining <= 0:
        raise asyncio.TimeoutError("workflow hard deadline exhausted")
    timeout_seconds = max(
        0.1,
        min(float(_RELAY_REQUEST_TIMEOUT_SECONDS), remaining),
    )
    request_cap = context.get("relay_request_timeout_seconds")
    try:
        timeout_seconds = min(timeout_seconds, float(request_cap))
    except (TypeError, ValueError):
        pass
    return max(0.1, timeout_seconds)


def _relay_pool_attempt_timeout_seconds(
    config: RoleConfig,
    *,
    role: str,
) -> float | None:
    """Resolve an endpoint-level cap without changing whole-call retry policy."""
    context = _call_context.get() or {}
    value = context.get("relay_request_timeout_seconds")
    if (
        value is None
        and role == "writer"
        and config.relay_profile in _PRIMARY_FIRST_RELAY_PROFILES
    ):
        value = getattr(
            get_settings(),
            "writer_relay_attempt_timeout_seconds",
            35.0,
        )
    try:
        return max(0.1, float(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _writer_relay_failover_hedge_delay_seconds() -> float:
    value = getattr(
        get_settings(),
        "writer_relay_failover_hedge_delay_seconds",
        0.75,
    )
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.75


def start_llm_observation() -> tuple[Token, list[dict[str, Any]]]:
    """Start collecting sanitized per-call LLM metadata in this async context."""
    records: list[dict[str, Any]] = []
    token = _call_records.set(records)
    return token, records


def stop_llm_observation(token: Token) -> None:
    _call_records.reset(token)


def _call_reason(role: str, context: dict[str, object]) -> str:
    explicit = str(context.get("call_reason") or "").strip()
    if explicit:
        return explicit
    stage = str(context.get("stage") or "").strip()
    if stage:
        return f"{stage}:{role}"
    return role


def _sanitize_value(value: Any, *, key: str = "") -> Any:
    """Recursively drop secrets/URLs/message bodies from nested structures."""
    blocked_keys = {
        "api_key",
        "authorization",
        "auth",
        "token",
        "base_url",
        "url",
        "host",
        "path",
        "system",
        "user",
        "messages",
        "prompt",
        "response",
        "body",
        "raw",
        "content",
    }
    lowered = str(key).lower()
    if lowered in blocked_keys or "url" in lowered or "key" in lowered or "secret" in lowered:
        return None
    if isinstance(value, str):
        if value.startswith("http://") or value.startswith("https://"):
            return None
        return value
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for child_key, child_value in value.items():
            sanitized = _sanitize_value(child_value, key=str(child_key))
            if sanitized is None and child_value is not None:
                continue
            cleaned[str(child_key)] = sanitized
        return cleaned
    if isinstance(value, list):
        cleaned_list = []
        for item in value:
            sanitized = _sanitize_value(item)
            if sanitized is None and item is not None:
                continue
            cleaned_list.append(sanitized)
        return cleaned_list
    if isinstance(value, tuple):
        return tuple(
            item
            for item in (_sanitize_value(v) for v in value)
            if item is not None
        )
    return value


def _sanitize_llm_record(record: dict[str, Any]) -> dict[str, Any]:
    """Drop secrets/URLs/message bodies; keep only configured endpoint labels."""
    cleaned_any = _sanitize_value(record)
    cleaned = cleaned_any if isinstance(cleaned_any, dict) else {}
    # Canonical O4 field aliases.
    if "endpoint_label" not in cleaned and cleaned.get("relay_endpoint"):
        cleaned["endpoint_label"] = cleaned.get("relay_endpoint")
    if "prompt_tokens" not in cleaned and "token_input" in cleaned:
        cleaned["prompt_tokens"] = cleaned.get("token_input")
    if "completion_tokens" not in cleaned and "token_output" in cleaned:
        cleaned["completion_tokens"] = cleaned.get("token_output")
    cleaned["internal_reason_code"] = internal_reason(cleaned)
    return cleaned


def _classify_transport_error(exc: BaseException) -> tuple[bool, str, str | None]:
    """Return (response_received, termination_reason, finish_reason_hint)."""
    # Prefer structured HTTP response objects over string matching.
    response = getattr(exc, "response", None)
    if response is not None:
        return True, "http_error", None
    text = f"{exc.__class__.__name__}: {exc}".lower()
    if "524" in text or re.search(r"\b[45]\d\d\b", text):
        # Status code in message usually means a response arrived.
        return True, "http_error", None
    if "timeout" in text or "timed out" in text:
        return False, "transport_timeout", None
    if "cancel" in text:
        return False, "workflow_cancelled", None
    return False, "http_error", None


def _http_status_code(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    try:
        return int(status_code) if status_code is not None else None
    except (TypeError, ValueError):
        return None


def _record_attempt(
    *,
    status: str,
    role: str,
    provider: str,
    config: RoleConfig,
    latency_ms: int,
    token_in: int = 0,
    token_out: int = 0,
    failover_attempt_index: int = 0,
    failover_from: str | None = None,
    response_received: bool = True,
    finish_reason: str | None = "stop",
    termination_reason: str | None = None,
    error_type: str | None = None,
    http_status_code: int | None = None,
    cancelled_at: str | None = None,
    ttfb_ms: int | None = None,
) -> dict[str, Any] | None:
    context = _call_context.get()
    generation_index = context.get("generation_index")
    if generation_index is None:
        generation_index = context.get("publish_retry_round") or 0
    retry_count = (
        int(_last_relay_retry_count.get() or 0)
        + int(failover_attempt_index or 0)
    )
    return _record_llm_call({
        "status": status,
        "role": role,
        "stage": str(context.get("stage") or ""),
        "call_reason": _call_reason(role, context),
        "provider": provider,
        "relay_profile": config.relay_profile,
        "relay_endpoint": config.relay_endpoint,
        "endpoint_label": config.relay_endpoint or config.relay_profile or provider,
        "wire_api": config.wire_api,
        "model": config.model,
        "max_tokens_request": context.get("max_tokens_request"),
        "latency_ms": latency_ms,
        "ttfb_ms": ttfb_ms if response_received else None,
        "prompt_tokens": token_in,
        "completion_tokens": token_out if response_received else 0,
        "token_input": token_in,
        "token_output": token_out if response_received else 0,
        "retry_count": retry_count,
        "failover_attempt_index": failover_attempt_index,
        "failover_from": failover_from,
        "generation_index": int(generation_index or 0),
        "attempt": context.get("attempt"),
        "publish_retry_round": context.get("publish_retry_round"),
        "owning_stage": context.get("owning_stage"),
        "owning_attempt": context.get("owning_attempt"),
        "owning_publish_retry_round": context.get(
            "owning_publish_retry_round"
        ),
        "observation_call_id": context.get("observation_call_id"),
        "job_id": context.get("job_id"),
        "request_id": context.get("request_id"),
        "response_received": response_received,
        "finish_reason": finish_reason if response_received else None,
        "termination_reason": termination_reason,
        "error_type": error_type,
        "http_status_code": http_status_code,
        "cancelled_at": cancelled_at or context.get("cancelled_at"),
        "observed_usage": _captured_usage(config),
        "transport_attempts": list((context.get("transport_attempts") or {}).get(_usage_capture_key(config), [])),
    })


def _record_llm_call(record: dict[str, Any]) -> dict[str, Any] | None:
    records = _call_records.get()
    if records is not None:
        stored = enrich_record(_sanitize_llm_record(record))
        stored.setdefault("observation_id", uuid.uuid4().hex)
        stored.setdefault("observation_sequence", len(records) + 1)
        records.append(stored)
        return stored
    return None


def _track_router_pending_cancellation(record: dict[str, Any] | None) -> None:
    if record is None:
        return
    tracker = (_call_context.get() or {}).get("router_observation_tracker")
    if isinstance(tracker, dict):
        pending = tracker.setdefault("pending_cancellations", [])
        if isinstance(pending, list):
            pending.append(record)


def _resolve_router_pending_cancellations(
    tracker: dict[str, Any],
    *,
    termination_reason: str,
) -> int:
    pending = tracker.get("pending_cancellations")
    if not isinstance(pending, list):
        return 0
    resolved = 0
    for record in pending:
        if not isinstance(record, dict):
            continue
        if record.get("termination_reason") != "router_cancel_pending":
            continue
        record["termination_reason"] = termination_reason
        record["status"] = (
            "cancelled" if "cancel" in termination_reason or termination_reason == "standby_ready" else "error"
        )
        resolved += 1
    return resolved


def summarize_llm_call_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Return compact per-job LLM totals and stage breakdown for persistence."""
    stages: dict[str, dict[str, int]] = {}
    provider_models: dict[str, int] = {}
    total_latency = 0
    token_input_total = 0
    token_output_total = 0
    retry_total = 0
    error_count = 0
    response_received_count = 0
    by_role: dict[str, int] = {}
    sanitized_records: list[dict[str, Any]] = []
    for raw in records:
        record = _sanitize_llm_record(raw)
        sanitized_records.append(record)
        stage = str(record.get("stage") or "UNKNOWN")
        status = str(record.get("status") or "success")
        role = str(record.get("role") or "unknown")
        latency = int(record.get("latency_ms") or 0)
        token_input = int(
            record.get("prompt_tokens")
            if record.get("prompt_tokens") is not None
            else (record.get("token_input") or 0)
        )
        token_output = int(
            record.get("completion_tokens")
            if record.get("completion_tokens") is not None
            else (record.get("token_output") or 0)
        )
        retry_count = int(record.get("retry_count") or 0)
        total_latency += latency
        token_input_total += token_input
        token_output_total += token_output
        retry_total += retry_count
        by_role[role] = by_role.get(role, 0) + 1
        if record.get("response_received") is True:
            response_received_count += 1
        if status != "success":
            error_count += 1
        stage_metrics = stages.setdefault(stage, {
            "call_count": 0,
            "error_count": 0,
            "latency_ms_total": 0,
            "token_input_total": 0,
            "token_output_total": 0,
            "retry_count_total": 0,
        })
        stage_metrics["call_count"] += 1
        stage_metrics["latency_ms_total"] += latency
        stage_metrics["token_input_total"] += token_input
        stage_metrics["token_output_total"] += token_output
        stage_metrics["retry_count_total"] += retry_count
        if status != "success":
            stage_metrics["error_count"] += 1
        provider_key = (
            f"{record.get('provider') or ''}:"
            f"{record.get('relay_profile') or ''}:"
            f"{record.get('model') or ''}"
        )
        provider_models[provider_key] = provider_models.get(provider_key, 0) + 1
    return {
        "trace_truth_observation_complete": True,
        "llm_call_count_total": len(sanitized_records),
        "llm_error_count_total": error_count,
        "llm_token_input_total": token_input_total,
        "llm_token_output_total": token_output_total,
        "llm_latency_ms_total": total_latency,
        "llm_retry_count_total": retry_total,
        "llm_response_received_count": response_received_count,
        "llm_calls_by_role": by_role,
        "llm_calls_by_stage": stages,
        "llm_provider_model_counts": provider_models,
        "llm_call_records": sanitized_records,
    }


def merge_llm_observation_summaries(
    *summaries: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge current-run observations without duplicating or inventing calls."""
    records: list[dict[str, Any]] = []
    complete = True
    saw_summary = False
    for summary in summaries:
        if not isinstance(summary, dict):
            complete = False
            continue
        saw_summary = True
        if summary.get("trace_truth_observation_complete") is not True:
            complete = False
        raw_records = summary.get("llm_call_records")
        if not isinstance(raw_records, list):
            complete = False
            continue
        for raw in raw_records:
            if not isinstance(raw, dict):
                complete = False
                continue
            copied = dict(raw)
            copied["observation_sequence"] = len(records) + 1
            records.append(copied)
    merged = summarize_llm_call_records(records)
    merged["trace_truth_observation_complete"] = bool(
        saw_summary and complete
    )
    return merged


def _validate_wire_api(wire_api: str, *, role: str) -> str:
    resolved = (wire_api or "openai_chat").strip().lower()
    if resolved not in _RELAY_WIRE_APIS:
        raise ValueError(
            f"Unsupported relay wire API {resolved!r} for role {role!r}. "
            f"Supported wire APIs: {sorted(_RELAY_WIRE_APIS)}"
        )
    return resolved


def _parse_relay_pool(
    raw_pool: str,
    *,
    role: str,
    profile_key: str,
    default_wire_api: str,
    default_api_key: str = "",
) -> tuple[RelayEndpoint, ...]:
    try:
        data = json.loads(raw_pool)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"RELAY_{profile_key.upper()}_POOL must be a JSON array"
        ) from exc
    if not isinstance(data, list) or not data:
        raise ValueError(f"RELAY_{profile_key.upper()}_POOL must be a non-empty JSON array")

    endpoints: list[RelayEndpoint] = []
    seen_names: set[str] = set()
    for index, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise ValueError(
                f"RELAY_{profile_key.upper()}_POOL entry {index} must be an object"
            )
        name = str(item.get("name") or f"{profile_key}_{index}").strip()
        if not name:
            name = f"{profile_key}_{index}"
        if name in seen_names:
            raise ValueError(
                f"RELAY_{profile_key.upper()}_POOL endpoint name {name!r} is duplicated"
            )
        seen_names.add(name)

        base_url = str(item.get("base_url") or "").strip()
        if not base_url:
            raise ValueError(
                f"RELAY_{profile_key.upper()}_POOL entry {index} requires base_url"
            )
        api_key = str(item.get("api_key") or default_api_key or "").strip()
        if not api_key:
            raise ValueError(
                f"RELAY_{profile_key.upper()}_POOL entry {index} requires api_key"
            )
        wire_api = _validate_wire_api(
            str(item.get("wire_api") or default_wire_api), role=role
        )
        endpoints.append(
            RelayEndpoint(
                name=name,
                base_url=base_url,
                api_key=api_key,
                wire_api=wire_api,
                model=str(item.get("model") or "").strip(),
            )
        )
    return tuple(endpoints)


def resolve_role_config(role: str) -> RoleConfig:
    """Return (provider, model, api_key) for a given role.

    Raises ValueError for:
    - unknown role
    - unsupported / misspelled provider (no silent fallback)
    - empty model or api_key
    """
    if role not in VALID_ROLES:
        raise ValueError(
            f"Unknown LLM role {role!r}. Valid roles: {sorted(VALID_ROLES)}"
        )
    s = get_settings()
    if role in _TABLE_ROLES:
        return _table_role_config(role, s)
    provider: str = getattr(s, f"{role}_provider", "deepseek")
    if provider not in _PROVIDER_BASE_URLS and provider != "relay":
        raise ValueError(
            f"Unsupported LLM provider {provider!r} for role {role!r}. "
            f"Supported providers: {sorted([*_PROVIDER_BASE_URLS, 'relay'])}"
        )
    model: str = getattr(s, f"{role}_model", "")
    if not model:
        raise ValueError(
            f"LLM model for role {role!r} is empty. "
            f"Set {role.upper()}_MODEL in .env"
        )
    relay_profile = ""
    base_url = ""
    relay_endpoint = ""
    relay_pool: tuple[RelayEndpoint, ...] = ()
    if provider == "relay":
        relay_profile = str(getattr(s, f"{role}_relay_profile", "") or "").strip()
        if not relay_profile:
            raise ValueError(
                f"Relay provider for role {role!r} requires "
                f"{role.upper()}_RELAY_PROFILE in .env"
            )
        profile_key = relay_profile.lower()
        if profile_key not in _RELAY_PROFILES:
            raise ValueError(
                f"Unsupported relay profile {relay_profile!r} for role {role!r}. "
                f"Supported relay profiles: {sorted(_RELAY_PROFILES)}"
            )
        profile_prefix = f"relay_{profile_key}"
        wire_api = str(
            getattr(s, f"{profile_prefix}_wire_api", "openai_chat") or "openai_chat"
        ).strip().lower()
        wire_api = _validate_wire_api(wire_api, role=role)
        raw_pool = str(getattr(s, f"{profile_prefix}_pool", "") or "").strip()
        profile_api_key = str(
            getattr(s, f"{profile_prefix}_api_key", "") or ""
        ).strip()
        if raw_pool:
            relay_pool = _parse_relay_pool(
                raw_pool,
                role=role,
                profile_key=profile_key,
                default_wire_api=wire_api,
                default_api_key=profile_api_key,
            )
            selected = relay_pool[0]
            base_url = selected.base_url
            api_key = selected.api_key
            wire_api = selected.wire_api
            relay_endpoint = selected.name
        else:
            base_url = str(getattr(s, f"{profile_prefix}_base_url", "") or "").strip()
            if not base_url:
                raise ValueError(
                    f"Relay provider for role {role!r} requires "
                    f"RELAY_{profile_key.upper()}_BASE_URL in .env"
                )
            api_key = profile_api_key
            if not api_key:
                raise ValueError(
                    f"No API key for role {role!r}. "
                    f"Set RELAY_{profile_key.upper()}_API_KEY in .env "
                    f"for relay profile {profile_key!r}"
                )
            relay_endpoint = profile_key
            relay_pool = (
                RelayEndpoint(
                    name=relay_endpoint,
                    base_url=base_url,
                    api_key=api_key,
                    wire_api=wire_api,
                ),
            )
    else:
        base_url = (
            str(getattr(s, f"{role}_base_url", "") or "").strip()
            or _PROVIDER_BASE_URLS[provider]
        )
        wire_api = "openai_chat"
        api_key: str = str(getattr(s, f"{role}_api_key", "") or "").strip()
        if not api_key:
            from src.model_routes import resolve_open_source_leg

            fallback = resolve_open_source_leg(s, "selector")
            if fallback.api_key:
                api_key = fallback.api_key
                if provider != fallback.provider and not str(getattr(s, f"{role}_api_key", "") or "").strip():
                    provider = fallback.provider
                    base_url = fallback.base_url
                    model = fallback.model
    if not api_key:
        raise ValueError(
            f"No API key for role {role!r}. "
            f"Set {role.upper()}_API_KEY, GEMINI_API_KEY, or DEEPSEEK_API_KEY in .env"
        )
    if model == "gpt-5.5":
        model = (
            "deepseek-chat"
            if provider == "deepseek"
            else "gemini-2.5-flash"
            if provider == "gemini"
            else "gpt-4.1-mini"
        )
    elif model == "gemini-flash-latest":
        model = "gemini-2.5-flash" if provider == "gemini" else "deepseek-chat"
    return RoleConfig(
        provider=provider,
        model=model,
        api_key=api_key,
        base_url=base_url,
        relay_profile=relay_profile,
        wire_api=wire_api,
        relay_endpoint=relay_endpoint,
        relay_pool=relay_pool,
    )


def _table_role_config(role: str, settings: Any, *, slot: str = "primary") -> RoleConfig:
    """Build a role's config from open-source settings and route defaults."""
    from src.model_routes import resolve_open_source_leg

    agent = "writer_gemini" if (role == "writer" and slot == "primary" and getattr(settings, "writer_primary_gemini", False)) else role
    resolved = resolve_open_source_leg(settings, agent, slot)
    if not resolved.api_key:
        raise ValueError(
            f"No API key for role {role!r}. Set {role.upper()}_API_KEY, GEMINI_API_KEY, or DEEPSEEK_API_KEY in .env"
        )
    return RoleConfig(
        provider=resolved.provider,
        model=resolved.model,
        api_key=resolved.api_key,
        base_url=resolved.base_url,
        wire_api="openai_chat",
    )


def _writer_gemini_config(settings: Any) -> RoleConfig:
    """WRITER_PRIMARY=gemini: the writer's primary seat, called directly."""
    return _table_role_config("writer", settings, slot="primary")


def route_planner_backup_config(settings: Any) -> RoleConfig:
    """The route planner's backup leg. Raises when its key is not set."""
    return _table_role_config("route_planner", settings, slot="backup")


def writer_standby_config(settings: Any, *, agent: str = "writer") -> RoleConfig:
    """Optional standby draft leg when a dedicated backup key is configured."""
    from src.model_routes import resolve_open_source_leg

    resolved = resolve_open_source_leg(settings, agent, "backup")
    return RoleConfig(
        provider=resolved.provider,
        model=resolved.model,
        api_key=resolved.api_key,
        base_url=resolved.base_url,
        wire_api="openai_chat",
    )


class _ObservedAsyncClient(httpx.AsyncClient):
    """Observe each SDK send while preserving the SDK's existing retries."""

    async def send(self, request, **kwargs):
        started = time.monotonic_ns()
        response = None
        error = None
        try:
            response = await super().send(request, **kwargs)
            return response
        except BaseException as exc:
            error = exc
            raise
        finally:
            context = _call_context.get() or {}
            config = context.get("usage_config")
            captures = context.get("transport_attempts")
            if isinstance(config, RoleConfig) and isinstance(captures, dict):
                data = {}
                if response is not None and response.is_stream_consumed:
                    try:
                        parsed = response.json()
                        data = parsed if isinstance(parsed, dict) else {}
                    except (ValueError, TypeError):
                        pass
                code = response.status_code if response is not None else None
                captures.setdefault(_usage_capture_key(config), []).append(enrich_record({
                    "observation_id": uuid.uuid4().hex, "provider": config.provider, "model": config.model,
                    "endpoint_label": config.relay_endpoint or config.relay_profile or config.provider,
                    "status": "cancelled" if isinstance(error, asyncio.CancelledError) else "error"
                        if error or (code is not None and code >= 400) else "success",
                    "http_status_code": code, "error_type": type(error).__name__ if error else None,
                    "error_code": provider_error_code(data),
                    "response_received": response is not None,
                    "latency_ms": (time.monotonic_ns() - started) // 1_000_000,
                    "observed_usage": observed_usage(data.get("usage")),
                }))


def _get_client(
    provider: str,
    api_key: str,
    base_url: str | None = None,
) -> AsyncOpenAI:
    # provider is already validated by resolve_role_config
    verify_ssl = get_settings().llm_verify_ssl
    resolved_base_url = base_url or _PROVIDER_BASE_URLS.get(provider, "")
    if not resolved_base_url:
        raise ValueError(f"No base URL configured for provider {provider!r}")
    key = (provider, resolved_base_url, api_key, verify_ssl)
    if key not in _clients:
        _clients[key] = AsyncOpenAI(
            api_key=api_key,
            base_url=resolved_base_url,
            http_client=_ObservedAsyncClient(verify=verify_ssl),
        )
    return _clients[key]


def _role_semaphore(role: str) -> asyncio.Semaphore | None:
    settings = get_settings()
    limit = 0
    if role == "writer":
        if bool(getattr(settings, "writer_relay_routing_enabled", False)):
            return None
        limit = int(settings.writer_llm_concurrency or 0)
    elif role == "review":
        limit = int(settings.review_llm_concurrency or 0)
    if limit <= 0:
        return None
    key = (role, limit)
    if key not in _role_semaphores:
        _role_semaphores[key] = asyncio.Semaphore(limit)
    return _role_semaphores[key]


def role_relay_store() -> PostgresRoleRelayStore:
    global _role_relay_store
    if _role_relay_store is None:
        _role_relay_store = PostgresRoleRelayStore()
    return _role_relay_store


async def _execute_durable_endpoint(role: str, name: str, config: RoleConfig, request, *, timeout_seconds: float | None = None, has_alternatives: bool = False):
    """Reuse the role ledger for direct and pre-router legacy endpoints.

    Expiry permits one normal business request to hold the recovery lease; no
    background requests are generated. A single path's unavailable state store
    cannot suppress the model request; an actual recorded stop still applies.
    """
    from src.agents.generation_model_routing import ModelEndpoint, endpoint_runtime_fingerprint

    if not role:
        return await request()
    deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
    def remaining_timeout(limit: float) -> float:
        return limit if deadline is None else max(0.001, min(limit, deadline - time.monotonic()))

    endpoint = ModelEndpoint(config.provider, config.model, config.base_url, config.api_key,
                             f"endpoint:{role}:{name}", name)
    store = None
    tracked = False
    trial = False
    generation = None
    try:
        store = role_relay_store()
        async def prepare():
            await store.synchronize_direct(
                role, name, provider=config.provider, failure_domain=endpoint.failure_domain,
                fingerprint=endpoint_runtime_fingerprint(endpoint, role=role),
                qualified_initial=bool(config.provider == "relay" and config.relay_pool),
                has_alternatives=has_alternatives,
            )
            return await store.admit(role, (name,))
        # State bookkeeping gets a small part of the existing call budget;
        # it must leave time for the single real provider on a slow database.
        admission_timeout = min(0.2, float(timeout_seconds) / 10) if timeout_seconds is not None else 0.2
        admitted, trial_name, generation = await asyncio.wait_for(prepare(), timeout=max(0.001, admission_timeout))
    except Exception as exc:
        if has_alternatives:
            error = LLMTransportError("relay_unavailable", "model endpoint is unavailable")
            error.model_request_not_started = True
            raise error from None
        logger.warning("model_endpoint_state_unavailable role=%s endpoint=%s error_type=%s",
                       role, name, type(exc).__name__)
    else:
        if name not in admitted:
            error = LLMTransportError("relay_unavailable", "model endpoint is unavailable")
            error.model_request_not_started = True
            raise error from None
        tracked = True
        trial = trial_name == name

    async def settle(*, success: bool, failure_class: str | None = None, retry_after_seconds=None):
        if not tracked:
            return
        try:
            if deadline is not None and time.monotonic() >= deadline:
                raise asyncio.TimeoutError("settlement budget exhausted")
            await asyncio.wait_for(store.record_direct_attempt(
                role, name, success=success, failure_class=failure_class,
                trial=trial, generation=generation,
                retry_after_seconds=retry_after_seconds,
                has_alternatives=has_alternatives,
            ), timeout=remaining_timeout(0.5))
        except Exception as exc:
            # Settlement must not replace the actual provider outcome or lose a
            # completed draft. A single path remains usable on state-store loss.
            logger.error("model_endpoint_settlement_failed role=%s endpoint=%s error_type=%s",
                         role, name, type(exc).__name__)

    try:
        if deadline is None:
            result = await request()
        else:
            reserve = min(0.1, float(timeout_seconds) / 10)
            result = await asyncio.wait_for(request(), timeout=max(0.001, deadline - time.monotonic() - reserve))
    except asyncio.CancelledError:
        if trial and generation is not None:
            try:
                await asyncio.shield(asyncio.wait_for(
                    store.release_direct_trial(role, name, generation=generation), timeout=remaining_timeout(0.5),
                ))
            except Exception as exc:
                logger.error("model_endpoint_release_failed role=%s endpoint=%s error_type=%s",
                             role, name, type(exc).__name__)
        raise
    except Exception as exc:
        failure = classify_failure(exc)
        await settle(success=False, failure_class=failure.failure_class, retry_after_seconds=failure.retry_after_seconds)
        raise
    else:
        await settle(success=True)
        return result


def pooled_endpoint_fingerprint(
    role: str, profile: str, endpoint: RelayEndpoint, model: str
) -> str:
    payload = json.dumps(
        {
            "role": role,
            "profile": profile,
            "name": endpoint.name,
            "base_url": str(endpoint.base_url).rstrip("/"),
            "wire_api": str(endpoint.wire_api),
            "model": str(endpoint.model or model),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def execute_recovery_probe(
    role: str,
    endpoint: RelayEndpoint,
    config: RoleConfig,
) -> tuple[bool, str | None]:
    selected = _role_config_for_endpoint(config, endpoint)
    timeout_seconds = float(get_settings().llm_recovery_probe_timeout_seconds)
    try:
        text, _token_in, _token_out = await _execute_relay_endpoint_attempt(
            selected,
            RECOVERY_PROBE_SYSTEM,
            RECOVERY_PROBE_USER,
            temperature=0.0,
            json_mode=False,
            hard_timeout_seconds=timeout_seconds,
        )
        # The recovery operation is a liveness/transport gate, not a prompt
        # obedience or output-quality gate.  Some compatible relays add
        # punctuation or provider-side framing around the requested token.
        # Successful protocol parsing plus non-empty model content is enough
        # to re-admit the endpoint; ordinary role validation and circuit
        # accounting remain authoritative for subsequent real traffic.
        if not text.strip():
            logger.warning(
                "llm_recovery_probe_failed role=%s endpoint=%s "
                "class=WIRE_INVALID error_type=EmptyProbeResponse",
                role,
                endpoint.name,
            )
            return False, "WIRE_INVALID"
        return True, None
    except Exception as exc:
        classified = classify_failure(exc)
        logger.warning(
            "llm_recovery_probe_failed role=%s endpoint=%s class=%s error_type=%s",
            role,
            endpoint.name,
            classified.failure_class,
            type(exc).__name__,
        )
        return False, classified.failure_class


async def _record_pooled_attempt(
    role: str,
    endpoint_name: str,
    *,
    success: bool,
    exc: BaseException | None = None,
) -> None:
    if role not in POOLED_ROLES:
        return
    trial = _pooled_trial.get()
    trial_name = trial[1] if trial else None
    generation = trial[2] if trial else None
    failure_class = None
    if exc is not None:
        failure_class = classify_failure(exc).failure_class
    try:
        await role_relay_store().record_attempt(
            role,
            endpoint_name,
            success=success,
            failure_class=failure_class,
            trial=bool(trial_name and trial_name == endpoint_name),
            generation=(generation if trial_name == endpoint_name
                        else _pooled_generations.get().get(endpoint_name)),
            retry_after_seconds=(classify_failure(exc).retry_after_seconds if exc is not None else None),
        )
    except Exception as record_exc:
        logger.error(
            "role_relay_record_failed role=%s endpoint=%s error_type=%s",
            role,
            endpoint_name,
            type(record_exc).__name__,
        )
        # Preserve the actual provider outcome and its P1 record. The next
        # admission still requires the durable store and fails closed if absent.


async def _record_pooled_attempt_on_hot_path(
    role: str,
    endpoint_name: str,
    *,
    success: bool,
    exc: BaseException | None = None,
) -> None:
    if role not in POOLED_ROLES:
        return
    await _record_pooled_attempt(
        role,
        endpoint_name,
        success=success,
        exc=exc,
    )


async def _admit_pooled_endpoints(
    role: str,
    config: RoleConfig,
    *,
    exclude_endpoints: frozenset[str] = frozenset(),
    allow_recovery: bool = True,
) -> tuple[tuple[str, ...], str | None, int | dict[str, int] | None]:
    fingerprints = {
        endpoint.name: pooled_endpoint_fingerprint(
            role, config.relay_profile, endpoint, config.model
        )
        for endpoint in config.relay_pool
    }
    store = role_relay_store()
    await store.synchronize(role, fingerprints)
    names = tuple(
        endpoint.name
        for endpoint in config.relay_pool
        if endpoint.name not in exclude_endpoints
    )
    if allow_recovery:
        return await store.admit(role, names)
    return await store.admit(role, names, allow_recovery=False)


def _with_admitted_relay_pool(
    config: RoleConfig,
    names: tuple[str, ...],
) -> RoleConfig:
    allowed = set(names)
    return RoleConfig(
        provider=config.provider,
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url,
        relay_profile=config.relay_profile,
        wire_api=config.wire_api,
        relay_endpoint=config.relay_endpoint,
        relay_pool=tuple(
            endpoint
            for endpoint in config.relay_pool
            if endpoint.name in allowed
        ),
    )


def _role_config_for_endpoint(config: RoleConfig, endpoint: RelayEndpoint) -> RoleConfig:
    return RoleConfig(
        provider=config.provider,
        model=endpoint.model or config.model,
        api_key=endpoint.api_key,
        base_url=endpoint.base_url,
        relay_profile=config.relay_profile,
        wire_api=endpoint.wire_api,
        relay_endpoint=endpoint.name,
        relay_pool=config.relay_pool,
    )


def _has_independent_relay_alternative(config: RoleConfig) -> bool:
    """Inspect the full configured pool, before admission narrows candidates."""
    from urllib.parse import urlsplit
    endpoints = []
    for endpoint in config.relay_pool:
        try:
            url = urlsplit(str(endpoint.base_url or "").strip())
        except ValueError:
            continue
        key = str(endpoint.api_key or "").strip()
        model = str(endpoint.model or config.model or "").strip()
        if key and model and url.scheme in {"http", "https"} and url.hostname:
            endpoints.append((url.hostname, key))
    return any(
        first[1] != second[1] and first[0] != second[0]
        for index, first in enumerate(endpoints) for second in endpoints[index + 1:]
    )


def _relay_endpoint_candidates(config: RoleConfig, *, durable_admitted: bool = False) -> list[RelayEndpoint]:
    endpoints = list(config.relay_pool)
    now = time.monotonic()
    active: list[RelayEndpoint] = []
    for endpoint in endpoints:
        cooldown_until = _relay_endpoint_cooldowns.get(
            (config.relay_profile, endpoint.name),
            0.0,
        )
        if durable_admitted or not _has_independent_relay_alternative(config) or cooldown_until <= now:
            active.append(endpoint)
    candidates = active
    if not candidates:
        return []
    if config.relay_profile in _PRIMARY_FIRST_RELAY_PROFILES:
        return candidates
    rotation_key = config.relay_profile
    if rotation_key not in _relay_endpoint_rotation:
        _relay_endpoint_rotation[rotation_key] = random.randrange(len(candidates))
    start = _relay_endpoint_rotation[rotation_key] % len(candidates)
    _relay_endpoint_rotation[rotation_key] = start + 1
    return candidates[start:] + candidates[:start]


def _relay_pool_cooldown_seconds() -> float:
    try:
        return max(0.0, float(get_settings().relay_pool_cooldown_seconds or 0.0))
    except (TypeError, ValueError):
        return 120.0


def _mark_relay_endpoint_cooldown(config: RoleConfig, endpoint: RelayEndpoint) -> None:
    if not _has_independent_relay_alternative(config):
        return
    cooldown = _relay_pool_cooldown_seconds()
    if cooldown <= 0:
        return
    _relay_endpoint_cooldowns[(config.relay_profile, endpoint.name)] = (
        time.monotonic() + cooldown
    )


def _is_relay_pool_retryable(
    exc: Exception,
    *,
    role: str = "",
    relay_profile: str = "",
) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        if status_code in _RELAY_POOL_FAILOVER_STATUS_CODES:
            return True
        return (
            role == "writer"
            and relay_profile in _PRIMARY_FIRST_RELAY_PROFILES
            and 400 <= status_code < 600
        )
    return isinstance(
        exc,
        (
            httpx.ConnectError,
            httpx.ReadError,
            httpx.ReadTimeout,
            httpx.RemoteProtocolError,
            httpx.WriteError,
            httpx.TimeoutException,
        ),
    )


def _relay_retry_delays_for_config(config: RoleConfig) -> tuple[float, ...]:
    if config.provider == "relay" and len(config.relay_pool) > 1:
        return ()
    return _RELAY_RETRY_DELAYS_SECONDS


def _join_endpoint(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + path


def _usage_capture_key(config: RoleConfig) -> str:
    return f"{config.provider}:{config.model}:{config.relay_endpoint or config.relay_profile}"


def _capture_usage(config: RoleConfig, value: object, *, complete: bool = True) -> None:
    captures = (_call_context.get() or {}).get("usage_capture")
    if not isinstance(captures, dict):
        return
    key = _usage_capture_key(config)
    prior = captures.setdefault(key, observed_usage(None))
    prior["complete"] = complete
    for field, count in observed_usage(value, config.wire_api).items():
        if count is not None:
            prior[field] = count


def _captured_usage(config: RoleConfig) -> dict:
    captures = (_call_context.get() or {}).get("usage_capture")
    if not isinstance(captures, dict):
        return observed_usage(None)
    return dict(captures.get(_usage_capture_key(config)) or observed_usage(None))


def _usage_value(usage: object, name: str) -> int:
    if isinstance(usage, dict):
        value = usage.get(name, 0)
    else:
        value = getattr(usage, name, 0)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _response_output_text(response: object) -> str:
    if isinstance(response, dict):
        output_text = response.get("output_text")
        if isinstance(output_text, str):
            return output_text
        chunks = []
        for item in response.get("output", []) or []:
            if not isinstance(item, dict):
                continue
            for content in item.get("content", []) or []:
                if isinstance(content, dict) and isinstance(content.get("text"), str):
                    chunks.append(content["text"])
        return "\n".join(chunks)

    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str):
        return output_text
    chunks = []
    for item in getattr(response, "output", []) or []:
        for content in getattr(item, "content", []) or []:
            text = getattr(content, "text", None)
            if isinstance(text, str):
                chunks.append(text)
    return "\n".join(chunks)


async def _relay_post_json(
    url: str,
    *,
    headers: dict[str, str],
    payload: dict,
    retry_delays: tuple[float, ...] = _RELAY_RETRY_DELAYS_SECONDS,
    observation_config: RoleConfig | None = None,
) -> dict:
    retry_statuses = {429, 500, 502, 503, 504}
    attempts = len(retry_delays) + 1
    last_exc: Exception | None = None
    async with httpx.AsyncClient(verify=get_settings().llm_verify_ssl) as client:
        for attempt in range(attempts):
            observation_started = time.monotonic_ns()
            observation_response = None
            observation_error = None
            observation_finished = None
            observation_latency = None
            try:
                response = await client.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=_relay_request_timeout_seconds(),
                )
                observation_response = response
                observation_finished = datetime.now(timezone.utc).isoformat()
                observation_latency = (time.monotonic_ns() - observation_started) // 1_000_000
                if (
                    response.status_code in retry_statuses
                    and attempt < attempts - 1
                ):
                    await asyncio.sleep(retry_delays[attempt])
                    continue
                response.raise_for_status()
                _last_relay_retry_count.set(attempt)
                return response.json()
            except (
                httpx.ConnectError,
                httpx.ReadTimeout,
                httpx.RemoteProtocolError,
                httpx.WriteError,
            ) as exc:
                observation_error = exc
                observation_finished = datetime.now(timezone.utc).isoformat()
                observation_latency = (time.monotonic_ns() - observation_started) // 1_000_000
                last_exc = exc
                if attempt >= attempts - 1:
                    _last_relay_retry_count.set(attempt)
                    raise
                await asyncio.sleep(retry_delays[attempt])
            except BaseException as exc:
                observation_error = exc
                raise
            finally:
                captures = (_call_context.get() or {}).get("transport_attempts")
                if observation_config is not None and isinstance(captures, dict):
                    data = {}
                    if observation_response is not None:
                        try:
                            parsed = observation_response.json()
                            data = parsed if isinstance(parsed, dict) else {}
                        except (ValueError, TypeError):
                            pass
                    status_code = observation_response.status_code if observation_response is not None else None
                    call = enrich_record({
                        "observation_id": uuid.uuid4().hex,
                        "finished_at": observation_finished or datetime.now(timezone.utc).isoformat(),
                        "provider": observation_config.provider, "model": observation_config.model,
                        "endpoint_label": observation_config.relay_endpoint or observation_config.relay_profile,
                        "status": "error" if status_code is not None and status_code >= 400 else "cancelled"
                            if isinstance(observation_error, asyncio.CancelledError) else "error" if observation_error else "success",
                        "http_status_code": status_code,
                        "error_type": "HTTPStatusError" if status_code is not None and status_code >= 400
                            else type(observation_error).__name__ if observation_error else None,
                        "error_code": provider_error_code(data),
                        "response_received": observation_response is not None,
                        "latency_ms": observation_latency if observation_latency is not None else (time.monotonic_ns() - observation_started) // 1_000_000,
                        "observed_usage": observed_usage(data.get("usage", data.get("usageMetadata")), observation_config.wire_api),
                    })
                    captures.setdefault(_usage_capture_key(observation_config), []).append(call)
    if last_exc is not None:
        raise last_exc
    raise LLMTransportError(
        "no_response",
        "relay request failed without response",
    )


def _speculative_ds_bulkhead(limit: int) -> asyncio.Semaphore:
    """Return the DS-only bulkhead; it is never shared with relay roles."""
    if limit not in _speculative_ds_bulkheads:
        _speculative_ds_bulkheads[limit] = asyncio.Semaphore(limit)
    return _speculative_ds_bulkheads[limit]


async def _call_speculative_ds_json(
    user: str,
    *,
    system: str,
    temperature: float,
    timeout_seconds: float,
    prompt_version: str,
    transport: httpx.AsyncBaseTransport | None = None,
    observation_facts: dict | None = None,
    key_agent: str = "writer",
) -> SpeculativeDSWriterResponse:
    """Run one zero-retry JSON request through the DS-only bulkhead."""
    settings = get_settings()
    standby = writer_standby_config(settings)
    base_url = standby.base_url
    # Same endpoint record as the standby draft; repair only swaps the key.
    request_key = _agent_key("repair", "backup") if key_agent == "repair" else standby.api_key
    started = time.monotonic_ns()
    payload: dict[str, object] = {
        "model": standby.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "response_format": {"type": "json_object"},
        "stream": False,
        "thinking": {"type": "disabled"},
    }
    from src.agents.final_writer import writer_prompt_version
    if key_agent == "writer" and writer_prompt_version() == "article":
        # v0.15.1: the standby writes the same article as the primary; repairs stay JSON.
        payload.pop("response_format")
    headers = {
        "authorization": f"Bearer {request_key}",
        "content-type": "application/json",
    }

    async def request_once() -> dict[str, object]:
        async with _speculative_ds_bulkhead(
            int(settings.trip_worker_concurrency)
        ):
            async with httpx.AsyncClient(
                verify=settings.llm_verify_ssl,
                timeout=None,
                transport=transport,
            ) as client:
                if observation_facts is not None:
                    observation_facts["dispatched"] = True
                response = await client.post(
                    _join_endpoint(base_url, "/chat/completions"),
                    headers=headers,
                    json=payload,
                )
                if observation_facts is not None:
                    observation_facts.update(response_received=True, http_status=response.status_code)
                    try:
                        observed_data = response.json()
                        if isinstance(observed_data, dict):
                            observation_facts.update(usage=observed_usage(observed_data.get("usage")),
                                                     error_code=provider_error_code(observed_data))
                    except (ValueError, TypeError):
                        pass
                response.raise_for_status()
                data = response.json()
                if observation_facts is not None and isinstance(data, dict):
                    observation_facts["usage"] = observed_usage(data.get("usage"))
                if not isinstance(data, dict):
                    raise json.JSONDecodeError(
                        "DeepSeek response must be a JSON object", "", 0
                    )
                return data

    try:
        data = await _execute_durable_endpoint(
            "writer", "deepseek-official-standby", standby, request_once,
            timeout_seconds=timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        raise httpx.ReadTimeout(
            f"speculative DS request exceeded {timeout_seconds:.1f}s"
        ) from exc

    usage = data.get("usage") or {}
    choices = data.get("choices") or []
    message: dict[str, object] = {}
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        candidate = choices[0].get("message") or {}
        if isinstance(candidate, dict):
            message = candidate
    content = message.get("content")
    if not isinstance(content, str):
        raise json.JSONDecodeError(
            "DeepSeek response is missing message content", "", 0
        )
    latency_ms = (time.monotonic_ns() - started) // 1_000_000
    return SpeculativeDSWriterResponse(
        text=content,
        token_in=_usage_value(usage, "prompt_tokens"),
        token_out=_usage_value(usage, "completion_tokens"),
        latency_ms=int(latency_ms),
        model=writer_standby_config(settings).model,
        prompt_version=prompt_version,
        observed_usage=observed_usage(usage),
    )


async def call_speculative_ds_writer(
    user: str,
    *,
    temperature: float,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SpeculativeDSWriterResponse:
    """Run one zero-retry DeepSeek official-API standby request.

    The timeout covers bulkhead wait, connection setup, response read and
    client teardown. A per-attempt client makes cancellation close only this
    standby request and cannot starve or tear down a relay connection.
    """
    settings = get_settings()
    started = time.monotonic_ns()
    facts: dict = {}
    try:
        response = await _call_speculative_ds_json(
            user,
            system=_ds_system_prompt(),
            temperature=temperature,
            timeout_seconds=float(settings.speculative_ds_timeout_seconds),
            prompt_version=_ds_prompt_label(),
            transport=transport,
            observation_facts=facts,
        )
    except asyncio.CancelledError:
        _record_direct_ds_attempt(
            status="cancelled",
            purpose="WRITER_STANDBY",
            model=writer_standby_config(settings).model,
            started=started,
            termination_reason="workflow_cancelled",
            error_type="CancelledError",
            usage=facts.get("usage"), response_received=facts.get("response_received", False),
            error_code=facts.get("error_code"),
            dispatched=facts.get("dispatched", False),
        )
        raise
    except Exception as exc:
        _record_direct_ds_attempt(
            status="error",
            purpose="WRITER_STANDBY",
            model=writer_standby_config(settings).model,
            started=started,
            termination_reason=_classify_transport_error(exc)[1],
            error_type=exc.__class__.__name__,
            http_status=facts.get("http_status", _http_status_code(exc)),
            usage=facts.get("usage"), response_received=facts.get("response_received", False),
            error_code=facts.get("error_code"),
            dispatched=facts.get("dispatched", False),
        )
        raise
    _record_direct_ds_attempt(
        status="success",
        purpose="WRITER_STANDBY",
        model=response.model,
        started=started,
        token_in=response.token_in,
        token_out=response.token_out,
        usage=response.observed_usage,
    )
    return response


async def call_speculative_ds_fragment_repair(
    user: str,
    *,
    system: str,
    timeout_seconds: float,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SpeculativeDSWriterResponse:
    """Run one bounded keyed-fragment repair on the independent DS client."""
    settings = get_settings()
    started = time.monotonic_ns()
    facts: dict = {}
    try:
        response = await _call_speculative_ds_json(
            user,
            system=system,
            temperature=0.1,
            timeout_seconds=min(
                float(settings.speculative_ds_timeout_seconds),
                float(timeout_seconds),
            ),
            prompt_version="flash_keyed_repair_v1",
            transport=transport,
            observation_facts=facts,
            key_agent="repair",
        )
    except asyncio.CancelledError:
        _record_direct_ds_attempt(
            status="cancelled",
            purpose="FRAGMENT_REPAIR",
            model=writer_standby_config(settings).model,
            started=started,
            termination_reason="workflow_cancelled",
            error_type="CancelledError",
            usage=facts.get("usage"), response_received=facts.get("response_received", False),
            error_code=facts.get("error_code"),
            dispatched=facts.get("dispatched", False),
        )
        raise
    except Exception as exc:
        _record_direct_ds_attempt(
            status="error",
            purpose="FRAGMENT_REPAIR",
            model=writer_standby_config(settings).model,
            started=started,
            termination_reason=_classify_transport_error(exc)[1],
            error_type=exc.__class__.__name__,
            http_status=facts.get("http_status", _http_status_code(exc)),
            usage=facts.get("usage"), response_received=facts.get("response_received", False),
            error_code=facts.get("error_code"),
            dispatched=facts.get("dispatched", False),
        )
        raise
    _record_direct_ds_attempt(
        status="success",
        purpose="FRAGMENT_REPAIR",
        model=response.model,
        started=started,
        token_in=response.token_in,
        token_out=response.token_out,
        usage=response.observed_usage,
    )
    return response


def _record_direct_ds_attempt(
    *,
    status: str,
    purpose: str,
    model: str,
    started: int,
    termination_reason: str | None = None,
    error_type: str | None = None,
    token_in: int = 0,
    token_out: int = 0,
    usage: dict | None = None,
    http_status: int | None = None,
    response_received: bool | None = None,
    error_code: str | None = None,
    dispatched: bool = True,
) -> dict[str, Any] | None:
    if not dispatched:
        return None
    context = _call_context.get() or {}
    return _record_llm_call({
        "status": status,
        "role": "writer",
        "stage": str(context.get("stage") or "FINAL_WRITER"),
        "call_reason": "writer_standby" if purpose == "WRITER_STANDBY" else "fragment_repair",
        "safe_purpose": purpose,
        "provider": "deepseek",
        "model": model,
        "endpoint_label": "deepseek-official-standby",
        "observed_usage": usage or observed_usage(None),
        "http_status_code": http_status,
        "error_code": error_code,
        "latency_ms": (time.monotonic_ns() - started) // 1_000_000,
        "prompt_tokens": max(0, int(token_in or 0)),
        "completion_tokens": max(0, int(token_out or 0)),
        "attempt": context.get("attempt") or 1,
        "publish_retry_round": context.get("publish_retry_round") or 0,
        "owning_stage": context.get("owning_stage"),
        "owning_attempt": context.get("owning_attempt"),
        "owning_publish_retry_round": context.get(
            "owning_publish_retry_round"
        ),
        "observation_call_id": context.get("observation_call_id"),
        "response_received": status == "success" if response_received is None else response_received,
        "termination_reason": termination_reason,
        "error_type": error_type,
    })


def _opus_writer_thinking_fields(
    config: RoleConfig, *, writer_stream: bool = False,
) -> dict[str, object]:
    """Request no thinking for the admitted Opus Writer model, not other roles.

    Compatible relays receive the native extension at the top level. Sending
    it does not prove upstream enforcement; each endpoint needs acceptance.
    """
    is_writer = writer_stream or (_call_context.get() or {}).get("role") == "writer"
    if is_writer and config.model.lower() == "claude-opus-4-6":
        return {"thinking": {"type": "disabled"}}
    return {}


def _writer_stream_request_parts(
    config: RoleConfig,
    system: str,
    user: str,
    *,
    temperature: float,
    json_mode: bool,
) -> tuple[str, dict[str, str], dict[str, object]]:
    """Build the existing Writer request envelope with streaming transport."""
    if config.wire_api == "openai_chat":
        payload: dict[str, object] = {
            "model": config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
            **_opus_writer_thinking_fields(config, writer_stream=True),
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        return (
            _join_endpoint(config.base_url, "/chat/completions"),
            {
                "authorization": f"Bearer {config.api_key}",
                "content-type": "application/json",
            },
            payload,
        )
    if config.wire_api == "anthropic_messages":
        prompt = user
        if json_mode:
            prompt = user + "\n\nReturn only a valid JSON object. No Markdown."
        return (
            _join_endpoint(config.base_url, "/v1/messages?beta=true"),
            {
                "x-api-key": config.api_key,
                "authorization": f"Bearer {config.api_key}",
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            {
                "model": config.model,
                "max_tokens": _requested_max_tokens(8192),
                **_opus_writer_thinking_fields(config, writer_stream=True),
                "temperature": temperature,
                "system": system,
                "messages": [{"role": "user", "content": prompt}],
                "stream": True,
            },
        )
    raise ValueError(
        "Writer stream probes support only openai_chat and anthropic_messages"
    )


def _writer_stream_event(wire_api: str, data: dict[str, object]) -> WriterStreamEvent:
    """Convert one provider SSE data object without treating metadata as liveness."""
    if wire_api == "openai_chat":
        choices = data.get("choices") or []
        delta: dict[str, object] = {}
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            candidate = choices[0].get("delta") or {}
            if isinstance(candidate, dict):
                delta = candidate
        usage = data.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        content = delta.get("content")
        return WriterStreamEvent(
            content_delta=content if isinstance(content, str) else "",
            token_in=(
                int(usage.get("prompt_tokens") or 0) if usage else None
            ),
            token_out=(
                int(usage.get("completion_tokens") or 0) if usage else None
            ),
        )

    event_type = str(data.get("type") or "")
    content = ""
    token_in: int | None = None
    token_out: int | None = None
    if event_type == "content_block_start":
        block = data.get("content_block") or {}
        if isinstance(block, dict) and block.get("type") == "text":
            text_value = block.get("text")
            content = text_value if isinstance(text_value, str) else ""
    elif event_type == "content_block_delta":
        delta = data.get("delta") or {}
        if isinstance(delta, dict) and delta.get("type") == "text_delta":
            text_value = delta.get("text")
            content = text_value if isinstance(text_value, str) else ""
    if event_type == "message_start":
        message = data.get("message") or {}
        usage = message.get("usage") if isinstance(message, dict) else {}
        if isinstance(usage, dict):
            token_in = int(usage.get("input_tokens") or 0)
            if usage.get("output_tokens") is not None:
                token_out = int(usage.get("output_tokens") or 0)
    elif event_type == "message_delta":
        usage = data.get("usage") or {}
        if isinstance(usage, dict):
            token_out = int(usage.get("output_tokens") or 0)
    return WriterStreamEvent(
        content_delta=content,
        token_in=token_in,
        token_out=token_out,
    )


class _WriterRelaySSEStream(AsyncIterator[WriterStreamEvent]):
    """One Writer attempt with an independently closable HTTP connection."""

    def __init__(
        self,
        config: RoleConfig,
        system: str,
        user: str,
        *,
        temperature: float,
        json_mode: bool,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        url, headers, payload = _writer_stream_request_parts(
            config,
            system,
            user,
            temperature=temperature,
            json_mode=json_mode,
        )
        self._wire_api = config.wire_api
        self._usage_config = config
        self._client = httpx.AsyncClient(
            verify=get_settings().llm_verify_ssl,
            timeout=None,
            transport=transport,
        )
        self._request = self._client.build_request(
            "POST",
            url,
            headers=headers,
            json=payload,
        )
        self._response: httpx.Response | None = None
        self._lines: AsyncIterator[str] | None = None
        self._anthropic_text_blocks: set[int] = set()
        self._closed = False
        self._close_confirmed = False

    def __aiter__(self) -> _WriterRelaySSEStream:
        return self

    async def _open(self) -> None:
        if self._response is not None:
            return
        response = await self._client.send(self._request, stream=True)
        self._response = response
        if not response.is_success:
            await response.aread()
            response.raise_for_status()
        self._lines = response.aiter_lines().__aiter__()

    async def __anext__(self) -> WriterStreamEvent:
        if self._closed:
            raise StopAsyncIteration
        await self._open()
        assert self._lines is not None
        data_lines: list[str] = []
        while True:
            try:
                line = await anext(self._lines)
            except StopAsyncIteration:
                if not data_lines:
                    raise
                line = ""
            if line == "":
                if not data_lines:
                    continue
                raw = "\n".join(data_lines)
                if raw.strip() == "[DONE]":
                    raise StopAsyncIteration
                parsed = json.loads(raw)
                if not isinstance(parsed, dict):
                    raise json.JSONDecodeError(
                        "Writer SSE data must be a JSON object", raw, 0
                    )
                if (
                    self._wire_api == "anthropic_messages"
                    and parsed.get("type") == "message_stop"
                ):
                    raise StopAsyncIteration
                usage = parsed.get("usage")
                if parsed.get("type") == "message_start":
                    usage = (parsed.get("message") or {}).get("usage")
                if usage is not None:
                    _capture_usage(self._usage_config, usage, complete=(
                        self._usage_config.wire_api == "openai_chat" or parsed.get("type") == "message_delta"))
                event = _writer_stream_event(self._wire_api, parsed)
                if self._wire_api == "anthropic_messages" and event.content_delta:
                    try:
                        block_index = int(parsed.get("index") or 0)
                    except (TypeError, ValueError):
                        block_index = 0
                    if (
                        block_index not in self._anthropic_text_blocks
                        and self._anthropic_text_blocks
                    ):
                        event = WriterStreamEvent(
                            content_delta="\n" + event.content_delta,
                            token_in=event.token_in,
                            token_out=event.token_out,
                        )
                    self._anthropic_text_blocks.add(block_index)
                return event
            if line.startswith(":"):
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())

    async def aclose(self) -> bool:
        """Close both response and client; return explicit confirmation evidence."""
        if self._closed:
            return self._close_confirmed
        confirmed = True
        try:
            if self._response is not None:
                await self._response.aclose()
                confirmed = confirmed and self._response.is_closed
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            confirmed = False
        try:
            await self._client.aclose()
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            confirmed = False
        self._closed = True
        self._close_confirmed = confirmed
        return confirmed


class _ObservedWriterStream(AsyncIterator[WriterStreamEvent]):
    """Keep transport closure evidence across the attempt-observation wrapper."""

    def __init__(
        self,
        iterator: AsyncGenerator[WriterStreamEvent, None],
        transport: _WriterRelaySSEStream,
    ) -> None:
        self._iterator = iterator
        self._transport = transport

    def __aiter__(self) -> _ObservedWriterStream:
        return self

    async def __anext__(self) -> WriterStreamEvent:
        return await anext(self._iterator)

    async def aclose(self) -> bool:
        try:
            await self._iterator.aclose()
        finally:
            confirmed = await self._transport.aclose()
        return confirmed


def _route_planner_gemini_fields(config: RoleConfig) -> dict[str, object]:
    """Gemini route-planner and writer requests carry reasoning_effort and a per-call timeout."""
    context = _call_context.get() or {}
    if config.provider == "gemini" and context.get("role") == "writer":
        from src.model_routes import route_leg

        params = route_leg("writer_gemini").params
        effort = str(params.get("reasoning_effort") or "low")
        if str(getattr(get_settings(), "writer_prompt_version", "") or "").strip() == "article":
            effort = str(get_settings().writer_article_reasoning_effort or effort)
        return {"extra_body": {"reasoning_effort": effort},
                "timeout": float(params.get("timeout_seconds") or 60)}
    if config.provider != "gemini" or context.get("role") != "route_planner":
        return {}
    effort = str(context.get("reasoning_effort") or "").strip()
    if not effort:
        effort = str(get_settings().route_planner_reasoning_effort or "low")
    fields: dict[str, object] = {"extra_body": {"reasoning_effort": effort}}
    timeout = context.get("route_planner_timeout_seconds")
    if timeout is None:
        timeout = get_settings().route_planner_timeout_seconds
    try:
        fields["timeout"] = max(0.1, float(timeout))
    except (TypeError, ValueError):
        pass
    return fields


async def _openai_chat_completion(
    config: RoleConfig,
    system: str,
    user: str,
    *,
    temperature: float,
    json_mode: bool,
    max_tokens: int | None = None,
) -> tuple[str, int, int]:
    if config.provider == "relay":
        payload: dict = {
            "model": config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
        }
        payload.update(_opus_writer_thinking_fields(config))
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if (
            config.model.lower().startswith("qwen3.8-")
            and (_call_context.get() or {}).get("role") == "review"
        ):
            # Qwen's OpenAI-compatible wire format uses a top-level switch.
            payload["enable_thinking"] = False
        headers = {
            "authorization": f"Bearer {config.api_key}",
            "content-type": "application/json",
        }
        data = await _relay_post_json(
            _join_endpoint(config.base_url, "/chat/completions"),
            headers=headers,
            payload=payload,
            retry_delays=_relay_retry_delays_for_config(config),
            observation_config=config,
        )
        usage = data.get("usage") or {}
        _capture_usage(config, usage)
        choices = data.get("choices") or []
        message = (choices[0].get("message") or {}) if choices else {}
        return (
            str(message.get("content") or ""),
            int(usage.get("prompt_tokens") or 0),
            int(usage.get("completion_tokens") or 0),
        )

    client = _get_client(config.provider, config.api_key, config.base_url)
    kwargs: dict = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    if max_tokens is not None:
        if config.provider == "deepseek" and config.model == "deepseek-chat" and max_tokens > 8192:
            max_tokens = 8192
        kwargs["max_tokens"] = max_tokens
    if (
        config.provider == "deepseek"
        and config.model.startswith("deepseek-v4-")
        and (_call_context.get() or {}).get("role") == "review"
    ):
        # V4 defaults to thinking, which can exhaust Review's bounded output
        # budget before producing any JSON. Keep that budget for the verdict.
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    opus_thinking = _opus_writer_thinking_fields(config)
    if opus_thinking:
        kwargs.setdefault("extra_body", {}).update(opus_thinking)
    route_planner_fields = _route_planner_gemini_fields(config)
    if route_planner_fields.get("extra_body"):
        kwargs.setdefault("extra_body", {}).update(route_planner_fields["extra_body"])
    if route_planner_fields.get("timeout") is not None:
        kwargs["timeout"] = route_planner_fields["timeout"]
    response = await client.chat.completions.create(**kwargs)
    usage = response.usage
    _capture_usage(config, usage)
    token_in = usage.prompt_tokens if usage else 0
    token_out = usage.completion_tokens if usage else 0
    return response.choices[0].message.content or "", token_in, token_out


async def _openai_responses(
    config: RoleConfig,
    system: str,
    user: str,
    *,
    temperature: float,
    json_mode: bool,
) -> tuple[str, int, int]:
    prompt = user
    if json_mode:
        prompt = user + "\n\nReturn only a valid JSON object. No Markdown."
    payload: dict = {
        "model": config.model,
        "instructions": system,
        "input": prompt,
        "max_output_tokens": _requested_max_tokens(2048),
        "store": False,
        "temperature": temperature,
    }
    headers = {
        "authorization": f"Bearer {config.api_key}",
        "content-type": "application/json",
    }
    data = await _relay_post_json(
        _join_endpoint(config.base_url, "/responses"),
        headers=headers,
        payload=payload,
        retry_delays=_relay_retry_delays_for_config(config),
            observation_config=config,
    )
    usage = data.get("usage") or {}
    _capture_usage(config, usage)
    token_in = _usage_value(usage, "input_tokens")
    token_out = _usage_value(usage, "output_tokens")
    return _response_output_text(data), token_in, token_out


async def _anthropic_messages(
    config: RoleConfig,
    system: str,
    user: str,
    *,
    temperature: float,
    json_mode: bool,
) -> tuple[str, int, int]:
    prompt = user
    if json_mode:
        prompt = user + "\n\nReturn only a valid JSON object. No Markdown."
    payload = {
        "model": config.model,
        "max_tokens": _requested_max_tokens(8192),
        **_opus_writer_thinking_fields(config),
        "temperature": temperature,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {
        "x-api-key": config.api_key,
        "authorization": f"Bearer {config.api_key}",
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    data = await _relay_post_json(
        _join_endpoint(config.base_url, "/v1/messages?beta=true"),
        headers=headers,
        payload=payload,
        retry_delays=_relay_retry_delays_for_config(config),
            observation_config=config,
    )
    chunks = [
        str(item.get("text") or "")
        for item in data.get("content", [])
        if isinstance(item, dict) and item.get("type") == "text"
    ]
    usage = data.get("usage") or {}
    _capture_usage(config, usage)
    return (
        "\n".join(chunk for chunk in chunks if chunk),
        int(usage.get("input_tokens") or 0),
        int(usage.get("output_tokens") or 0),
    )


async def _gemini_native(
    config: RoleConfig,
    system: str,
    user: str,
    *,
    temperature: float,
    json_mode: bool,
) -> tuple[str, int, int]:
    generation_config: dict[str, object] = {
        "temperature": temperature,
        "maxOutputTokens": _requested_max_tokens(2048),
    }
    if json_mode:
        generation_config["responseMimeType"] = "application/json"
    context = _call_context.get() or {}
    if context.get("role") == "route_planner":
        effort = str(context.get("reasoning_effort") or "").strip()
        if not effort:
            effort = str(get_settings().route_planner_reasoning_effort or "low")
        generation_config["thinkingConfig"] = {"thinkingLevel": effort}
    payload = {
        "systemInstruction": {
            "parts": [{"text": system}],
        },
        "contents": [
            {
                "role": "user",
                "parts": [{"text": user}],
            }
        ],
        "generationConfig": generation_config,
    }
    headers = {
        "x-goog-api-key": config.api_key,
        "authorization": f"Bearer {config.api_key}",
        "content-type": "application/json",
    }
    url = _join_endpoint(
        config.base_url,
        f"/v1beta/models/{config.model}:generateContent",
    )
    data = await _relay_post_json(
        url,
        headers=headers,
        payload=payload,
        retry_delays=_relay_retry_delays_for_config(config),
            observation_config=config,
    )
    parts = []
    for candidate in data.get("candidates", []) or []:
        content = candidate.get("content") if isinstance(candidate, dict) else None
        for part in (content or {}).get("parts", []) or []:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
    usage = data.get("usageMetadata") or {}
    _capture_usage(config, usage)
    return (
        "\n".join(parts),
        int(usage.get("promptTokenCount") or 0),
        int(usage.get("candidatesTokenCount") or 0),
    )


async def _execute_by_wire_api(
    config: RoleConfig,
    system: str,
    user: str,
    *,
    temperature: float,
    json_mode: bool,
    max_tokens: int | None = None,
) -> tuple[str, int, int]:
    config = _with_agent_key(config)
    if config.wire_api == "openai_chat":
        _mark_provider_transport_started()
        return await _openai_chat_completion(
            config,
            system,
            user,
            temperature=temperature,
            json_mode=json_mode,
            max_tokens=max_tokens,
        )
    if config.wire_api == "openai_responses":
        _mark_provider_transport_started()
        return await _openai_responses(
            config,
            system,
            user,
            temperature=temperature,
            json_mode=json_mode,
        )
    if config.wire_api == "anthropic_messages":
        _mark_provider_transport_started()
        return await _anthropic_messages(
            config,
            system,
            user,
            temperature=temperature,
            json_mode=json_mode,
        )
    if config.wire_api == "gemini_native":
        _mark_provider_transport_started()
        return await _gemini_native(
            config,
            system,
            user,
            temperature=temperature,
            json_mode=json_mode,
        )
    raise ValueError(f"Unsupported wire API {config.wire_api!r}")


def _agent_key(agent: str, slot: str) -> str:
    from src.model_routes import resolve_open_source_leg

    resolved = resolve_open_source_leg(get_settings(), agent, slot)
    if not resolved.api_key:
        raise ValueError(f"No API key for {agent}. Set {agent.upper()}_API_KEY or global key in .env")
    return resolved.api_key


def _with_agent_key(config: RoleConfig) -> RoleConfig:
    """Send a repair request with the repair key when configured."""
    if (_call_context.get() or {}).get("key_agent") != "repair":
        return config
    from src.model_routes import route_leg

    if config.provider != "relay" or config.relay_endpoint != route_leg("repair").endpoint_id:
        return config
    return replace(config, api_key=_agent_key("repair", "primary"))


def _mark_provider_transport_started() -> None:
    """Mark one actual upstream invocation at its transport boundary."""
    tracker = (_call_context.get() or {}).get("transport_observation_tracker")
    if isinstance(tracker, dict):
        tracker["started"] = int(tracker.get("started") or 0) + 1


async def _execute_relay_endpoint_attempt(
    config: RoleConfig,
    system: str,
    user: str,
    *,
    temperature: float,
    json_mode: bool,
    hard_timeout_seconds: float | None = None,
) -> tuple[str, int, int]:
    request = _execute_by_wire_api(
        config,
        system,
        user,
        temperature=temperature,
        json_mode=json_mode,
    )
    if hard_timeout_seconds is None:
        return await request
    try:
        return await asyncio.wait_for(
            request,
            timeout=max(0.1, float(hard_timeout_seconds)),
        )
    except asyncio.TimeoutError as exc:
        raise httpx.ReadTimeout(
            f"relay endpoint attempt exceeded {hard_timeout_seconds:.1f}s"
        ) from exc


async def _execute_with_relay_hedge(
    config: RoleConfig,
    candidates: list[RelayEndpoint],
    system: str,
    user: str,
    *,
    temperature: float,
    json_mode: bool,
    role: str,
    hedge_delay_seconds: float,
    attempt_timeout_seconds: float | None = None,
    index_offset: int = 0,
    prior_endpoint_label: str | None = None,
) -> tuple[str, int, int, RoleConfig]:
    """Race identical requests across relay endpoints; first success wins."""

    async def attempt(index: int, endpoint: RelayEndpoint):
        selected = _role_config_for_endpoint(config, endpoint)
        started = False
        try:
            if index:
                await asyncio.sleep(hedge_delay_seconds * index)
            attempt_t0 = time.monotonic_ns()
            async def request():
                nonlocal started
                started = True
                return await _execute_relay_endpoint_attempt(
                    selected, system, user, temperature=temperature, json_mode=json_mode,
                    hard_timeout_seconds=attempt_timeout_seconds,
                )
            if role not in POOLED_ROLES:
                text, token_in, token_out = await _execute_durable_endpoint(role, endpoint.name, selected, request,
                    has_alternatives=_has_independent_relay_alternative(config))
            else:
                text, token_in, token_out = await request()
            _record_attempt(
                status="success", role=role or "unknown", provider=config.provider,
                config=selected, latency_ms=(time.monotonic_ns() - attempt_t0) // 1_000_000,
                token_in=token_in, token_out=token_out, failover_attempt_index=index_offset + index,
                response_received=True,
            )
            return (
                "success",
                index_offset + index,
                selected,
                text,
                token_in,
                token_out,
                None,
            )
        except asyncio.CancelledError:
            if started:
                _record_attempt(
                    status="cancelled",
                    role=role or "unknown",
                    provider=config.provider,
                    config=selected,
                    latency_ms=(time.monotonic_ns() - attempt_t0) // 1_000_000,
                    failover_attempt_index=index_offset + index,
                    failover_from=(
                        prior_endpoint_label
                        or (candidates[0].name if index else None)
                    ),
                    response_received=False,
                    finish_reason=None,
                    termination_reason="hedge_loser_cancelled",
                    error_type="CancelledError",
                    cancelled_at=datetime.now(timezone.utc).isoformat(),
                    ttfb_ms=None,
                )
            raise
        except Exception as exc:
            if getattr(exc, "model_request_not_started", False):
                return ("error", index_offset + index, selected, "", 0, 0, exc)
            latency_ms = (time.monotonic_ns() - attempt_t0) // 1_000_000
            response_received, termination_reason, _ = _classify_transport_error(exc)
            _record_attempt(
                status="error",
                role=role or "unknown",
                provider=config.provider,
                config=selected,
                latency_ms=latency_ms,
                failover_attempt_index=index_offset + index,
                failover_from=(
                    prior_endpoint_label
                    or (candidates[0].name if index else None)
                ),
                response_received=response_received,
                finish_reason=None,
                termination_reason=termination_reason,
                error_type=exc.__class__.__name__,
                http_status_code=_http_status_code(exc),
                ttfb_ms=None,
            )
            if _is_relay_pool_retryable(
                exc,
                role=role,
                relay_profile=config.relay_profile,
            ):
                _mark_relay_endpoint_cooldown(config, endpoint)
            return ("error", index_offset + index, selected, "", 0, 0, exc)

    tasks = [
        asyncio.create_task(attempt(index, endpoint))
        for index, endpoint in enumerate(candidates)
    ]
    errors: list[Exception] = []
    try:
        for completed in asyncio.as_completed(tasks):
            result = await completed
            status, index, selected, text, token_in, token_out, exc = result
            if status == "success":
                _last_endpoint_failover_count.set(index)
                _last_failed_endpoint_label.set(
                    prior_endpoint_label
                    or (candidates[0].name if index else None)
                )
                await _record_pooled_attempt_on_hot_path(
                    role, selected.relay_endpoint, success=True
                )
                return text, token_in, token_out, selected
            if isinstance(exc, Exception):
                await _record_pooled_attempt_on_hot_path(
                    role, selected.relay_endpoint, success=False, exc=exc
                )
                errors.append(exc)
        if errors:
            _last_endpoint_failover_count.set(
                index_offset + len(candidates) - 1
            )
            _last_failed_endpoint_label.set(
                prior_endpoint_label or candidates[-1].name
            )
            raise errors[-1]
        raise LLMTransportError(
            "no_response",
            "relay hedge completed without a response",
        )
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _execute_with_relay_pool(
    config: RoleConfig,
    system: str,
    user: str,
    *,
    temperature: float,
    json_mode: bool,
    role: str = "",
    max_tokens: int | None = None,
) -> tuple[str, int, int, RoleConfig]:
    if config.provider != "relay" or not config.relay_pool:
        async def request():
            return await _execute_by_wire_api(
                config, system, user, temperature=temperature, json_mode=json_mode,
                max_tokens=max_tokens,
            )
        # A backup leg keeps its own health record, apart from the primary's.
        name = str((_call_context.get() or {}).get("direct_endpoint_name") or role)
        text, token_in, token_out = await _execute_durable_endpoint(role, name, config, request)
        return text, token_in, token_out, config

    if role == "writer" and bool(
        getattr(get_settings(), "writer_relay_routing_enabled", False)
    ):
        settings = get_settings()
        stream_probe_enabled = bool(
            getattr(settings, "writer_stream_probe_enabled", False)
        )
        global _writer_relay_router
        if _writer_relay_router is None:
            _writer_relay_router = WriterRelayRouter(PostgresWriterRelayStore())

        async def execute_attempt(
            selected_config,
            selected_system,
            selected_user,
            selected_temperature,
            selected_json_mode,
            timeout_seconds,
        ):
            attempt_t0 = time.monotonic_ns()
            try:
                return await _execute_relay_endpoint_attempt(
                    selected_config,
                    selected_system,
                    selected_user,
                    temperature=selected_temperature,
                    json_mode=selected_json_mode,
                    hard_timeout_seconds=timeout_seconds,
                )
            except asyncio.CancelledError:
                standby_event = current_llm_call_context().get("writer_standby_ready_cancel_event")
                standby_cancelled = standby_event is not None and standby_event.is_set()
                pending = _record_attempt(
                    status="cancelled" if standby_cancelled else "error",
                    role=role or "writer",
                    provider=config.provider,
                    config=selected_config,
                    latency_ms=(time.monotonic_ns() - attempt_t0) // 1_000_000,
                    response_received=False,
                    finish_reason=None,
                    termination_reason="standby_ready" if standby_cancelled else "router_cancel_pending",
                    error_type="CancelledError",
                    ttfb_ms=None,
                )
                _track_router_pending_cancellation(pending)
                raise
            except Exception as exc:
                response_received, termination_reason, _ = (
                    _classify_transport_error(exc)
                )
                _record_attempt(
                    status="error",
                    role=role or "writer",
                    provider=config.provider,
                    config=selected_config,
                    latency_ms=(time.monotonic_ns() - attempt_t0) // 1_000_000,
                    response_received=response_received,
                    finish_reason=None,
                    termination_reason=termination_reason,
                    error_type=exc.__class__.__name__,
                    http_status_code=_http_status_code(exc),
                    ttfb_ms=None,
                )
                raise

        def stream_attempt(
            selected_config,
            selected_system,
            selected_user,
            selected_temperature,
            selected_json_mode,
        ):
            stream = _WriterRelaySSEStream(
                selected_config,
                selected_system,
                selected_user,
                temperature=selected_temperature,
                json_mode=selected_json_mode,
            )

            async def observed_stream() -> AsyncGenerator[WriterStreamEvent, None]:
                attempt_t0 = time.monotonic_ns()
                try:
                    async for event in stream:
                        yield event
                except asyncio.CancelledError:
                    standby_event = current_llm_call_context().get("writer_standby_ready_cancel_event")
                    standby_cancelled = standby_event is not None and standby_event.is_set()
                    pending = _record_attempt(
                        status="cancelled" if standby_cancelled else "error",
                        role=role or "writer",
                        provider=config.provider,
                        config=selected_config,
                        latency_ms=(
                            time.monotonic_ns() - attempt_t0
                        ) // 1_000_000,
                        response_received=False,
                        finish_reason=None,
                        termination_reason="standby_ready" if standby_cancelled else "router_cancel_pending",
                        error_type="CancelledError",
                        ttfb_ms=None,
                    )
                    _track_router_pending_cancellation(pending)
                    raise
                except Exception as exc:
                    response_received, termination_reason, _ = (
                        _classify_transport_error(exc)
                    )
                    _record_attempt(
                        status="error",
                        role=role or "writer",
                        provider=config.provider,
                        config=selected_config,
                        latency_ms=(
                            time.monotonic_ns() - attempt_t0
                        ) // 1_000_000,
                        response_received=response_received,
                        finish_reason=None,
                        termination_reason=termination_reason,
                        error_type=exc.__class__.__name__,
                        http_status_code=_http_status_code(exc),
                        ttfb_ms=None,
                    )
                    raise

            return _ObservedWriterStream(observed_stream(), stream)

        def record_wire_invalid(selected_config: RoleConfig, latency_ms: int) -> None:
            _record_attempt(
                status="error",
                role=role or "writer",
                provider=config.provider,
                config=selected_config,
                latency_ms=latency_ms,
                response_received=True,
                finish_reason=None,
                termination_reason="wire_invalid",
                error_type="WIRE_INVALID",
                http_status_code=200,
                ttfb_ms=None,
            )

        (
            text_value,
            token_in,
            token_out,
            selected_config,
            failover_index,
            failed_endpoint,
        ) = await _writer_relay_router.execute(
            config=config,
            system=system,
            user=user,
            temperature=temperature,
            json_mode=json_mode,
            call_context=dict(_call_context.get() or {}),
            release_identity=settings.app_version,
            request=execute_attempt,
            stream_probe_enabled=stream_probe_enabled,
            stream_request=stream_attempt if stream_probe_enabled else None,
            first_token_deadline_seconds=(
                float(
                    getattr(
                        settings,
                        "writer_stream_first_token_deadline_seconds",
                        20.0,
                    )
                )
            ),
            stall_deadline_seconds=float(
                getattr(settings, "writer_stream_stall_deadline_seconds", 10.0)
            ),
            on_wire_invalid=record_wire_invalid,
        )
        _last_endpoint_failover_count.set(failover_index)
        _last_failed_endpoint_label.set(failed_endpoint)
        return text_value, token_in, token_out, selected_config

    review_deadline_monotonic: float | None = None
    review_allow_recovery = True
    if role == "review":
        context = _call_context.get() or {}
        deadline_value = context.get("review_request_deadline_monotonic")
        try:
            review_deadline_monotonic = float(deadline_value)
        except (TypeError, ValueError):
            request_cap = _relay_pool_attempt_timeout_seconds(
                config,
                role=role,
            )
            if request_cap is not None:
                review_deadline_monotonic = time.monotonic() + request_cap
        if review_deadline_monotonic is not None:
            remaining = review_deadline_monotonic - time.monotonic()
            minimum_probe_budget = (
                0.1 + (2 * ROLE_RELAY_HOT_PATH_TIMEOUT_SECONDS)
            )
            review_allow_recovery = remaining > (
                REVIEW_CLOSED_SECOND_HOP_RESERVE_SECONDS
                + minimum_probe_budget
            )

    trial_token = None
    generations_token = None
    trial_name: str | None = None
    original_config = config
    if role in POOLED_ROLES and config.provider == "relay" and config.relay_pool:
        admission_task = asyncio.create_task(
            _admit_pooled_endpoints(
                role,
                config,
                allow_recovery=review_allow_recovery,
            )
        )
        try:
            admitted, trial_name, trial_generation = await asyncio.wait_for(
                asyncio.shield(admission_task),
                timeout=ROLE_RELAY_HOT_PATH_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            admission_task.cancel()
            await asyncio.gather(admission_task, return_exceptions=True)
            raise
        except Exception as exc:
            if not admission_task.done():
                admission_task.cancel()
                await asyncio.gather(admission_task, return_exceptions=True)
            logger.warning(
                "role_relay_admit_failed role=%s error_type=%s",
                role,
                type(exc).__name__,
            )
            raise LLMTransportError(
                "relay_state_unavailable",
                "relay state is unavailable",
            ) from None
        if not admitted:
            raise LLMTransportError(
                "relay_unavailable",
                "relay pool has no candidate endpoints",
            )
        config = _with_admitted_relay_pool(config, admitted)
        generations_token = _pooled_generations.set(
            trial_generation if isinstance(trial_generation, dict) else
            ({admitted[0]: trial_generation} if len(admitted) == 1 and trial_generation is not None else {})
        )
        trial_token = _pooled_trial.set(
            (role, trial_name, int(trial_generation))
            if trial_name and trial_generation is not None
            else None
        )
    try:
        hard_timeout_seconds: float | None = None
        if role == "review" and trial_name:
            hard_timeout_seconds = REVIEW_HALF_OPEN_PROBE_TIMEOUT_SECONDS
            if review_deadline_monotonic is not None:
                remaining_for_probe = (
                    review_deadline_monotonic
                    - time.monotonic()
                    - REVIEW_CLOSED_SECOND_HOP_RESERVE_SECONDS
                    - ROLE_RELAY_HOT_PATH_TIMEOUT_SECONDS
                )
                hard_timeout_seconds = min(
                    hard_timeout_seconds,
                    max(0.1, remaining_for_probe),
                )
        return await _execute_with_relay_pool_candidates(
            config,
            system,
            user,
            temperature=temperature,
            json_mode=json_mode,
            role=role,
            hard_timeout_seconds=hard_timeout_seconds,
        )
    except Exception as exc:
        if not (
            role == "review"
            and trial_name
            and _is_relay_pool_retryable(
                exc,
                role=role,
                relay_profile=original_config.relay_profile,
            )
        ):
            raise
        if trial_token is not None:
            _pooled_trial.reset(trial_token)
            trial_token = None
        return await _execute_review_half_open_second_hop(
            original_config,
            system,
            user,
            temperature=temperature,
            json_mode=json_mode,
            failed_endpoint=trial_name,
            review_deadline_monotonic=review_deadline_monotonic,
        )
    finally:
        if trial_token is not None:
            _pooled_trial.reset(trial_token)
        if generations_token is not None:
            _pooled_generations.reset(generations_token)


async def _execute_review_half_open_second_hop(
    config: RoleConfig,
    system: str,
    user: str,
    *,
    temperature: float,
    json_mode: bool,
    failed_endpoint: str,
    review_deadline_monotonic: float | None,
) -> tuple[str, int, int, RoleConfig]:
    """Retry once on a CLOSED Review endpoint after a HALF_OPEN failure."""
    admission_task = asyncio.create_task(
        _admit_pooled_endpoints(
            "review",
            config,
            exclude_endpoints=frozenset({failed_endpoint}),
            allow_recovery=False,
        )
    )
    try:
        admitted, second_trial, _generation = await asyncio.wait_for(
            asyncio.shield(admission_task),
            timeout=ROLE_RELAY_HOT_PATH_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        admission_task.cancel()
        await asyncio.gather(admission_task, return_exceptions=True)
        raise
    except Exception as exc:
        if not admission_task.done():
            admission_task.cancel()
            await asyncio.gather(admission_task, return_exceptions=True)
        logger.warning(
            "role_relay_second_hop_admit_failed role=review error_type=%s",
            type(exc).__name__,
        )
        raise LLMTransportError(
            "relay_state_unavailable",
            "relay state is unavailable",
        ) from None
    if second_trial:
        raise LLMTransportError(
            "relay_state_unavailable",
            "relay state returned an invalid second-hop trial",
        )
    if not admitted:
        raise LLMTransportError(
            "relay_unavailable",
            "relay pool has no healthy CLOSED failover endpoint",
        )
    admitted_config = _with_admitted_relay_pool(config, admitted)
    _pooled_generations.set(
        _generation if isinstance(_generation, dict) else
        ({admitted[0]: _generation} if len(admitted) == 1 and _generation is not None else {})
    )
    selected = _relay_endpoint_candidates(admitted_config, durable_admitted=True)[0]
    second_config = _with_admitted_relay_pool(config, (selected.name,))
    _last_endpoint_failover_count.set(1)
    _last_failed_endpoint_label.set(failed_endpoint)
    second_hop_timeout_seconds: float | None = None
    if review_deadline_monotonic is not None:
        second_hop_timeout_seconds = max(
            0.1,
            review_deadline_monotonic - time.monotonic(),
        )
    result = await _execute_with_relay_pool_candidates(
        second_config,
        system,
        user,
        temperature=temperature,
        json_mode=json_mode,
        role="review",
        hard_timeout_seconds=second_hop_timeout_seconds,
    )
    _last_endpoint_failover_count.set(1)
    _last_failed_endpoint_label.set(failed_endpoint)
    return result


async def _execute_with_relay_pool_candidates(
    config: RoleConfig,
    system: str,
    user: str,
    *,
    temperature: float,
    json_mode: bool,
    role: str = "",
    hard_timeout_seconds: float | None = None,
) -> tuple[str, int, int, RoleConfig]:
    # Persistent admission is authoritative for pooled recovery. A stale local
    # cooldown must not suppress the exclusively leased HALF_OPEN request.
    candidates = _relay_endpoint_candidates(config, durable_admitted=role in POOLED_ROLES)
    attempt_timeout_seconds = _relay_pool_attempt_timeout_seconds(
        config,
        role=role,
    )
    endpoint_hard_timeout_seconds = (
        attempt_timeout_seconds
        if (
            role == "writer"
            and config.relay_profile in _PRIMARY_FIRST_RELAY_PROFILES
        )
        else hard_timeout_seconds
    )
    hedge_value = (_call_context.get() or {}).get("relay_hedge_delay_seconds")
    try:
        hedge_delay_seconds = float(hedge_value)
    except (TypeError, ValueError):
        hedge_delay_seconds = 0.0
    if hedge_delay_seconds > 0 and len(candidates) > 1:
        with llm_call_context(
            relay_request_timeout_seconds=attempt_timeout_seconds,
        ):
            return await _execute_with_relay_hedge(
                config,
                candidates,
                system,
                user,
                temperature=temperature,
                json_mode=json_mode,
                role=role,
                hedge_delay_seconds=hedge_delay_seconds,
                attempt_timeout_seconds=endpoint_hard_timeout_seconds,
            )
    configured_primary = config.relay_pool[0]
    if not candidates:
        error = LLMTransportError("relay_unavailable", "relay pool has no candidate endpoints")
        error.model_request_not_started = True
        raise error
    configured_primary_available = bool(
        candidates
        and candidates[0].name == configured_primary.name
    )
    if (
        role == "writer"
        and config.relay_profile in _PRIMARY_FIRST_RELAY_PROFILES
        and not configured_primary_available
    ):
        first_candidate_index = next(
            (
                index
                for index, endpoint in enumerate(config.relay_pool)
                if endpoint.name == candidates[0].name
            ),
            0,
        )
        with llm_call_context(
            relay_request_timeout_seconds=attempt_timeout_seconds,
        ):
            return await _execute_with_relay_hedge(
                config,
                candidates,
                system,
                user,
                temperature=temperature,
                json_mode=json_mode,
                role=role,
                hedge_delay_seconds=(
                    _writer_relay_failover_hedge_delay_seconds()
                ),
                attempt_timeout_seconds=endpoint_hard_timeout_seconds,
                index_offset=first_candidate_index,
                prior_endpoint_label=configured_primary.name,
            )
    last_exc: Exception | None = None
    previous_label: str | None = None
    _last_failed_endpoint_label.set(None)
    for index, endpoint in enumerate(candidates):
        selected_config = _role_config_for_endpoint(config, endpoint)
        attempt_t0 = time.monotonic_ns()
        try:
            with llm_call_context(
                relay_request_timeout_seconds=attempt_timeout_seconds,
            ):
                async def request():
                    return await _execute_relay_endpoint_attempt(
                        selected_config, system, user, temperature=temperature,
                        json_mode=json_mode, hard_timeout_seconds=endpoint_hard_timeout_seconds,
                    )
                if role not in POOLED_ROLES:
                    text, token_in, token_out = await _execute_durable_endpoint(role, endpoint.name, selected_config, request,
                        has_alternatives=_has_independent_relay_alternative(config))
                else:
                    text, token_in, token_out = await request()
            _last_endpoint_failover_count.set(index)
            # Preserve prior failed endpoint for the success record.
            _last_failed_endpoint_label.set(previous_label)
            await _record_pooled_attempt_on_hot_path(
                role, selected_config.relay_endpoint, success=True
            )
            return text, token_in, token_out, selected_config
        except Exception as exc:
            latency_ms = (time.monotonic_ns() - attempt_t0) // 1_000_000
            last_exc = exc
            if getattr(exc, "model_request_not_started", False):
                continue
            response_received, termination_reason, _ = _classify_transport_error(exc)
            _record_attempt(
                status="error",
                role=role or "unknown",
                provider=config.provider,
                config=selected_config,
                latency_ms=latency_ms,
                failover_attempt_index=index,
                failover_from=previous_label,
                response_received=response_received,
                finish_reason=None,
                termination_reason=termination_reason,
                error_type=exc.__class__.__name__,
                http_status_code=_http_status_code(exc),
                ttfb_ms=None,
            )
            previous_label = endpoint.name
            _last_failed_endpoint_label.set(previous_label)
            await _record_pooled_attempt_on_hot_path(
                role, selected_config.relay_endpoint, success=False, exc=exc
            )
            retryable = _is_relay_pool_retryable(
                exc,
                role=role,
                relay_profile=config.relay_profile,
            )
            if retryable:
                _mark_relay_endpoint_cooldown(config, endpoint)
            if not retryable or index >= len(candidates) - 1:
                _last_endpoint_failover_count.set(index)
                raise
            logger.warning(
                "llm_relay_endpoint_failover profile=%s endpoint=%s wire_api=%s "
                "model=%s status_code=%s error_type=%s",
                config.relay_profile,
                endpoint.name,
                endpoint.wire_api,
                config.model,
                _http_status_code(exc),
                type(exc).__name__,
            )
            remaining_candidates = candidates[index + 1:]
            if (
                role == "writer"
                and config.relay_profile in _PRIMARY_FIRST_RELAY_PROFILES
                and len(remaining_candidates) > 1
            ):
                with llm_call_context(
                    relay_request_timeout_seconds=attempt_timeout_seconds,
                ):
                    return await _execute_with_relay_hedge(
                        config,
                        remaining_candidates,
                        system,
                        user,
                        temperature=temperature,
                        json_mode=json_mode,
                        role=role,
                        hedge_delay_seconds=(
                            _writer_relay_failover_hedge_delay_seconds()
                        ),
                        attempt_timeout_seconds=endpoint_hard_timeout_seconds,
                        index_offset=index + 1,
                        prior_endpoint_label=previous_label,
                    )
    if last_exc is not None:
        raise last_exc
    raise LLMTransportError(
        "relay_unavailable",
        "relay pool has no candidate endpoints",
    )


async def _do_chat(
    system: str,
    user: str,
    *,
    role: str,
    temperature: float,
    json_mode: bool,
    role_config_override: RoleConfig | None = None,
    max_tokens: int | None = None,
) -> tuple[str, dict]:
    """Execute chat, emit structured log, return (text, usage_meta)."""
    role_config = role_config_override or resolve_role_config(role)
    provider, model, api_key = role_config
    used_config = role_config
    del model, api_key
    _last_relay_retry_count.set(0)
    _last_endpoint_failover_count.set(0)
    _last_failed_endpoint_label.set(None)

    observation_call_id = (
        f"{id(asyncio.current_task())}:{time.monotonic_ns()}"
    )
    current_context = _call_context.get() or {}
    call_context_token = _call_context.set({
        **current_context,
        "role": role,
        "observation_call_id": observation_call_id,
        "usage_capture": {},
        "transport_attempts": {},
        "usage_config": role_config,
    })
    t0 = time.monotonic_ns()
    router_observation_tracker: dict[str, Any] = {
        "pending_cancellations": [],
    }
    transport_observation_tracker: dict[str, int] = {"started": 0}
    try:
        semaphore = _role_semaphore(role)
        with llm_call_context(
            router_observation_tracker=router_observation_tracker,
            transport_observation_tracker=transport_observation_tracker,
        ):
            if semaphore is None:
                text, token_in, token_out, used_config = await _execute_with_relay_pool(
                    role_config,
                    system,
                    user,
                    temperature=temperature,
                    json_mode=json_mode,
                    role=role,
                    max_tokens=max_tokens,
                )
            else:
                async with semaphore:
                    text, token_in, token_out, used_config = await _execute_with_relay_pool(
                        role_config,
                        system,
                        user,
                        temperature=temperature,
                        json_mode=json_mode,
                        role=role,
                        max_tokens=max_tokens,
                    )
        _resolve_router_pending_cancellations(
            router_observation_tracker,
            termination_reason="transport_timeout",
        )
        latency_ms = (time.monotonic_ns() - t0) // 1_000_000
        failover_attempt_index = int(_last_endpoint_failover_count.get() or 0)
        failover_from = _last_failed_endpoint_label.get()
        if not any(r.get("observation_call_id") == observation_call_id and r.get("status") == "success"
                   and r.get("relay_endpoint") == used_config.relay_endpoint for r in (_call_records.get() or [])):
            _record_attempt(
                status="success", role=role, provider=provider, config=used_config,
                latency_ms=latency_ms, token_in=token_in, token_out=token_out,
                failover_attempt_index=failover_attempt_index, failover_from=failover_from,
                response_received=True,
            )
        meta = {
            "provider": provider,
            "relay_profile": used_config.relay_profile,
            "relay_endpoint": used_config.relay_endpoint,
            "wire_api": used_config.wire_api,
            "model": used_config.model,
            "token_input": token_in,
            "token_output": token_out,
            "latency_ms": latency_ms,
            "retry_count": int(_last_relay_retry_count.get() or 0) + failover_attempt_index,
        }
        return text, meta
    except asyncio.CancelledError:
        latency_ms = (time.monotonic_ns() - t0) // 1_000_000
        cancelled_at = datetime.now(timezone.utc).isoformat()
        standby_ready = current_llm_call_context().get("writer_standby_ready_cancel_event")
        cancellation_reason = (
            "standby_ready" if standby_ready is not None and standby_ready.is_set()
            else "workflow_cancelled"
        )
        resolved = _resolve_router_pending_cancellations(
            router_observation_tracker,
            termination_reason=cancellation_reason,
        )
        records = _call_records.get() or []
        already_recorded = any(
            record.get("observation_call_id") == observation_call_id
            for record in records
            if isinstance(record, dict)
        )
        transport_started = int(
            transport_observation_tracker.get("started") or 0
        ) > 0
        if transport_started and not resolved and not already_recorded:
            _record_attempt(
                status="cancelled",
                role=role,
                provider=provider,
                config=used_config,
                latency_ms=latency_ms,
                failover_attempt_index=int(_last_endpoint_failover_count.get() or 0),
                response_received=False,
                finish_reason=None,
                termination_reason=cancellation_reason,
                error_type="CancelledError",
                cancelled_at=cancelled_at,
                ttfb_ms=None,
            )
        raise
    except Exception as exc:
        _resolve_router_pending_cancellations(
            router_observation_tracker,
            termination_reason=(
                "workflow_cancelled"
                if exc.__class__.__name__ == "WriterBudgetCutoff"
                else "transport_timeout"
            ),
        )
        if (role_config.provider != "relay" or not role_config.relay_pool) and not getattr(exc, "model_request_not_started", False):
            latency_ms = (time.monotonic_ns() - t0) // 1_000_000
            response_received, termination_reason, _ = _classify_transport_error(exc)
            _record_attempt(
                status="error",
                role=role,
                provider=provider,
                config=used_config,
                latency_ms=latency_ms,
                failover_attempt_index=int(_last_endpoint_failover_count.get() or 0),
                response_received=response_received,
                finish_reason=None,
                termination_reason=termination_reason,
                error_type=exc.__class__.__name__,
                http_status_code=_http_status_code(exc),
                ttfb_ms=None,
            )
        raise
    finally:
        _call_context.reset(call_context_token)


async def chat(
    system: str,
    user: str,
    *,
    role: str,
    temperature: float = 0.3,
    json_mode: bool = False,
    role_config_override: RoleConfig | None = None,
    max_tokens: int | None = None,
) -> str:
    """Chat completion with per-role LLM governance. Returns assistant text."""
    _last_chat_metadata.set({})
    text, metadata = await _do_chat(
        system, user, role=role, temperature=temperature, json_mode=json_mode,
        role_config_override=role_config_override, max_tokens=max_tokens
    )
    _last_chat_metadata.set(dict(metadata))
    return text


def last_chat_metadata() -> dict[str, Any]:
    """Return usage metadata for the latest chat in the current async context."""
    return dict(_last_chat_metadata.get() or {})


async def chat_with_usage(
    system: str,
    user: str,
    *,
    role: str,
    temperature: float = 0.3,
    json_mode: bool = False,
    max_tokens: int | None = None,
    role_config_override: RoleConfig | None = None,
) -> tuple[str, dict]:
    """Like chat() but also returns usage metadata.

    Returns:
        (text, {"provider": str, "model": str,
                "token_input": int, "token_output": int, "latency_ms": int})
    """
    return await _do_chat(
        system,
        user,
        role=role,
        temperature=temperature,
        json_mode=json_mode,
        max_tokens=max_tokens,
        role_config_override=role_config_override,
    )
