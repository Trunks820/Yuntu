"""One-shot repair for excessive keyed Writer fragment fallbacks."""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from src.agents.food_review import parse_food_spans
from src.agents.model_json import decode_model_json
from src.agents.evidence_strength import StructuredEvidencePayload
from src.agents.llm import (
    call_speculative_ds_fragment_repair,
    chat,
    llm_call_context,
)
from src.agents.poi_fragments import normalize_repaired_body, repair_prompt_body
from src.agents.schema import PlanOutput, RoutePlan

FragmentKey = tuple[int, int, int]

FALLBACK_TOLERANCE_NUMERATOR = 1
FALLBACK_TOLERANCE_DENOMINATOR = 4
KEYED_FRAGMENT_REPAIR_MIN_TIMEOUT_SECONDS = 30.0
KEYED_FRAGMENT_REPAIR_TIMEOUT_SECONDS = 30.0
KEYED_FRAGMENT_REPAIR_MAX_TIMEOUT_SECONDS = 45.0
KEYED_FRAGMENT_REPAIR_SECONDS_PER_TARGET = 2.5
KEYED_FRAGMENT_REVIEW_TIMEOUT_SECONDS = 30.0


def keyed_fragment_repair_timeout_seconds(target_count: int) -> float:
    """Size the localized Writer window without consuming the Review reserve."""
    count = max(1, int(target_count or 0))
    return min(
        KEYED_FRAGMENT_REPAIR_MAX_TIMEOUT_SECONDS,
        KEYED_FRAGMENT_REPAIR_TIMEOUT_SECONDS
        + KEYED_FRAGMENT_REPAIR_SECONDS_PER_TARGET * (count - 1),
    )

KEYED_FRAGMENT_REPAIR_SYSTEM_PROMPT = """你只补写指定的旅行攻略地点片段。
严格输出一个 JSON object，顶层只能有 poi_fragments：
{"poi_fragments":[{"plan_index":1,"day":1,"place_id":123,"text":"正文"}]}

规则：
- 只能返回输入 targets 中的 key，不得增加、重复或改动 key。
- text 只写该地点正文，不要地点名冒号前缀，不要标题、路线、交通、住宿或总结。
- 动作使用该 target 的 authorized_actions；事实仅使用该 target 的 authorization.direct_facts / weak_experience，保持其强弱和地点归属。risk_only_warnings 只能作提醒，不能当卖点；not_authorized_reasons 和 omitted_count 不授权补造事实。writing_hint 仅用于语气，不得逐字输出。
- plan_contexts 是同版非餐饮正文和总述，仅用于衔接及跨段去重，不是事实授权；不得挪用其他地点的材料，已有正文也不自动证明事实。优先保留本地点已授权的具体信息，不能用通用动作替掉具体玩法。
- 对照整篇的活动内容而不是只换词：各地点写清有依据的具体玩法；材料不足可以简短，不用通用拍照建议填满篇幅。
- required_body_elements 是已准入的用户偏好正文要求，必须在上述动作边界内兑现；它不增加事实或动作授权，不要逐字照抄指引。
- 不得编造门票、价格、开放时间、历史、排名、最佳、必去、交通班次或其他外部事实。
- 每个 text 必须非空、具体、自然；不要解释、道歉、Markdown 或代码围栏。
"""

KEYED_FRAGMENT_REVIEW_SYSTEM_PROMPT = """你只复审刚刚局部补写的旅行攻略地点片段。
严格输出一个 JSON object，顶层只能有 rejected_keys：
{"rejected_keys":[{"plan_index":1,"day":1,"place_id":123,"reason":"unsupported_fact"}]}

authorized_actions 与 writing_hint 共同构成该 target 的确定性动作边界：writing_hint
可以支持对应地点类型的自然行动，但不新增外部事实。事实只按该target的authorization核对：
direct_facts可自然表达，weak_experience不得升级承诺，risk_only_warnings只能提醒。
plan_contexts只供衔接和重复判断，不授权其他地点的事实。不得逐字泄露内部提示。
仅在片段存在以下任一问题时拒绝：超出动作或事实边界、缺少具体行动、
把其他地点改写成本站或新增游览、泄露内部提示、包含无法授权的门票/价格/开放时间/排名/交通细节。
本地授权事实中的观景对象不等于新增游览地点。
没有问题时返回 {"rejected_keys":[]}。不得改写正文。
"""


def _unique_keys(keys: Iterable[FragmentKey]) -> list[FragmentKey]:
    return list(dict.fromkeys(
        (int(plan_index), int(day), int(place_id))
        for plan_index, day, place_id in keys
    ))


def fallback_ratio_exceeded(
    fallback_keys: Iterable[FragmentKey],
    *,
    total_fragment_count: int,
) -> bool:
    """Return true only when unique fallback ownership exceeds 25 percent."""
    count = len(_unique_keys(fallback_keys))
    total = max(0, int(total_fragment_count))
    if count <= 0:
        return False
    if total <= 0:
        return True
    return (
        count * FALLBACK_TOLERANCE_DENOMINATOR
        > total * FALLBACK_TOLERANCE_NUMERATOR
    )


@dataclass(frozen=True)
class KeyedFragmentRepairTarget:
    plan_index: int
    day: int
    place_id: int
    place_name: str
    authorized_actions: tuple[str, ...]
    writing_hint: str = ""
    # Retain the original locally for echo checks; serialize only its body.
    current_text: str = ""
    issue_codes: tuple[str, ...] = ()
    required_body_elements: tuple[str, ...] = ()
    authorization: dict[str, Any] = field(default_factory=dict)
    plan_context: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> FragmentKey:
        return (self.plan_index, self.day, self.place_id)

    def to_prompt_dict(self) -> dict[str, Any]:
        body = repair_prompt_body(self.current_text, key=self.key)
        if body is None:
            raise ValueError("target_attachment_contract_invalid")
        payload = {
            "plan_index": self.plan_index,
            "day": self.day,
            "place_id": self.place_id,
            "place_name": self.place_name,
            "authorized_actions": list(self.authorized_actions),
            "writing_hint": self.writing_hint,
            "current_text": body,
            "issue_codes": list(self.issue_codes),
            "authorization": self.authorization,
        }
        if self.required_body_elements:
            payload["required_body_elements"] = list(self.required_body_elements)
        return payload

    def to_review_prompt_dict(self) -> dict[str, Any]:
        payload = {
            "plan_index": self.plan_index,
            "day": self.day,
            "place_id": self.place_id,
            "place_name": self.place_name,
            "authorized_actions": list(self.authorized_actions),
            "writing_hint": self.writing_hint,
            "issue_codes": list(self.issue_codes),
            "authorization": self.authorization,
        }
        if self.required_body_elements:
            payload["required_body_elements"] = list(self.required_body_elements)
        return payload


@dataclass
class KeyedFragmentRepairCallResult:
    replacements: dict[FragmentKey, str] = field(default_factory=dict)
    latency_ms: int = 0
    failure_reason: str = ""
    invalid_details: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class KeyedFragmentReviewResult:
    rejected_keys: set[FragmentKey] = field(default_factory=set)
    latency_ms: int = 0
    failure_reason: str = ""


def build_keyed_fragment_targets(
    keys: Iterable[FragmentKey],
    *,
    route_plans: list[RoutePlan],
    structured_evidence_payload: StructuredEvidencePayload,
    plans: list[PlanOutput] | None = None,
    issue_codes_by_key: dict[FragmentKey, set[str]] | None = None,
) -> list[KeyedFragmentRepairTarget]:
    targets: list[KeyedFragmentRepairTarget] = []
    for key in _unique_keys(keys):
        plan_index, day, place_id = key
        zero = plan_index - 1
        if zero < 0 or zero >= len(route_plans):
            continue
        route_plan = route_plans[zero]
        place = next(
            (
                place
                for day_group in route_plan.day_groups
                if int(day_group.day) == day
                for place in day_group.places
                if int(place.place_id) == place_id
            ),
            None,
        )
        contract = structured_evidence_payload.action_contract(
            plan_index=plan_index,
            day=day,
            place_id=place_id,
        )
        if place is None or contract is None or contract.place_name != place.name:
            continue
        public_actions = tuple(
            str(action)
            for action in contract.authorized_actions
            if action and not str(action).startswith("[INTERNAL")
        )
        writing_hint = next(
            (
                str(action).replace("[INTERNAL 写作指引，勿输出] ", "", 1)
                for action in contract.authorized_actions
                if str(action).startswith("[INTERNAL")
            ),
            "",
        )
        # A locked route stop can legitimately have only the deterministic
        # internal writing hint.  That is also the source used by the local
        # fallback, so it is sufficient authority for a bounded rewrite.
        if not public_actions and not writing_hint:
            continue
        materials = [item for item in structured_evidence_payload.places
                     if item.place_id == place_id]
        if len(materials) > 1 or (materials and materials[0].name != place.name):
            continue  # Ambiguous or stale identity must not grant fact authority.
        material = materials[0] if materials else None
        authorization = {
            "version": structured_evidence_payload.version,
            "place_id": place_id, "name": place.name, "place_type": place.place_type,
            "direct_facts": list(material.direct_facts) if material else [],
            "weak_experience": list(material.weak_experience) if material else [],
            "risk_only_warnings": list(material.risk_only_warnings) if material else [],
            "not_authorized_reasons": dict(material.not_authorized_reasons) if material else {},
            "omitted_count": material.omitted_count if material else 0,
        }
        plan_context = {}
        current_text = ""
        if plans is not None and zero < len(plans):
            plan_context = {"summary": plans[zero].summary, "plan_text": plans[zero].plan_text}
            fragment = plans[zero].poi_fragment(
                plan_index=plan_index,
                day=day,
                place_id=place_id,
            )
            if fragment is not None:
                current_text = fragment.text
        targets.append(KeyedFragmentRepairTarget(
            plan_index=plan_index,
            day=day,
            place_id=place_id,
            place_name=place.name,
            authorized_actions=public_actions,
            writing_hint=writing_hint,
            current_text=current_text,
            authorization=authorization,
            plan_context=plan_context,
            issue_codes=tuple(sorted((issue_codes_by_key or {}).get(key, set()))),
        ))
    return targets


def _parse_key(value: Any) -> FragmentKey | None:
    if not isinstance(value, dict):
        return None
    try:
        key = (
            int(value.get("plan_index", 0)),
            int(value.get("day", 0)),
            int(value.get("place_id", 0)),
        )
    except (TypeError, ValueError):
        return None
    return key if all(part > 0 for part in key) else None


def parse_keyed_fragment_repair_response(
    raw: str,
    *,
    requested_keys: Iterable[FragmentKey],
) -> KeyedFragmentRepairCallResult:
    try:
        data = decode_model_json(raw)
    except (TypeError, json.JSONDecodeError):
        return KeyedFragmentRepairCallResult(failure_reason="invalid_json")
    items = data.get("poi_fragments") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return KeyedFragmentRepairCallResult(failure_reason="output_contract_invalid")

    requested = set(_unique_keys(requested_keys))
    replacements: dict[FragmentKey, str] = {}
    invalid_details: list[dict[str, Any]] = []
    duplicate_keys: set[FragmentKey] = set()
    for index, item in enumerate(items):
        key = _parse_key(item)
        text = item.get("text") if isinstance(item, dict) else None
        if key is None or key not in requested:
            invalid_details.append({"index": index, "reason": "unknown_key"})
            continue
        if key in replacements or key in duplicate_keys:
            replacements.pop(key, None)
            duplicate_keys.add(key)
            invalid_details.append({"index": index, "reason": "duplicate_key"})
            continue
        if not isinstance(text, str) or not text.strip():
            invalid_details.append({"index": index, "reason": "empty_text"})
            continue
        replacements[key] = text.strip()
    return KeyedFragmentRepairCallResult(
        replacements=replacements,
        invalid_details=invalid_details,
        failure_reason="" if replacements else "no_valid_replacements",
    )


def _plan_contexts(targets: list[KeyedFragmentRepairTarget]) -> dict[str, Any]:
    """Send one current draft per plan, not one duplicate per repair target."""
    contexts = {}
    for target in targets:
        if not target.plan_context:
            continue
        key = str(target.plan_index)
        if key in contexts and contexts[key] != target.plan_context:
            raise ValueError("inconsistent repair draft context")
        contexts[key] = target.plan_context
    # Food attachments retain their separate ownership and repair path. They
    # must not become writable material through the whole-draft context.
    cleaned = {}
    for key, context in contexts.items():
        cleaned[key] = {}
        for field in ("summary", "plan_text"):
            text = str(context.get(field) or "")
            for span in reversed(parse_food_spans(text)):
                text = text[:span.start] + text[span.end:]
            if re.search(r"<!--\s*/?\s*food", text):
                raise ValueError("invalid food boundary in repair context")
            cleaned[key][field] = text
    return cleaned


async def call_keyed_fragment_repair(
    *,
    generator: str,
    targets: list[KeyedFragmentRepairTarget],
    timeout_seconds: float = KEYED_FRAGMENT_REPAIR_TIMEOUT_SECONDS,
) -> KeyedFragmentRepairCallResult:
    if not targets:
        return KeyedFragmentRepairCallResult(failure_reason="no_targets")
    try:
        prompt_targets = [target.to_prompt_dict() for target in targets]
        plan_contexts = _plan_contexts(targets)
    except ValueError:
        return KeyedFragmentRepairCallResult(
            failure_reason="target_attachment_contract_invalid"
        )
    user = json.dumps(
        {
            "targets": prompt_targets,
            "plan_contexts": plan_contexts,
            "required_keys": [
                {
                    "plan_index": target.plan_index,
                    "day": target.day,
                    "place_id": target.place_id,
                }
                for target in targets
            ],
        },
        ensure_ascii=False,
    )
    started = time.monotonic()
    try:
        with llm_call_context(
            call_reason="keyed_fragment_repair",
            role="writer",
            stage="KEYED_FRAGMENT_REPAIR",
            key_agent="repair",
        ):
            if generator == "ds_flash":
                response = await call_speculative_ds_fragment_repair(
                    user,
                    system=KEYED_FRAGMENT_REPAIR_SYSTEM_PROMPT,
                    timeout_seconds=timeout_seconds,
                )
                raw = response.text
            elif generator == "opus":
                raw = await asyncio.wait_for(
                    chat(
                        system=KEYED_FRAGMENT_REPAIR_SYSTEM_PROMPT,
                        user=user,
                        role="writer",
                        temperature=0.1,
                        json_mode=True,
                    ),
                    timeout=timeout_seconds,
                )
            else:
                return KeyedFragmentRepairCallResult(
                    failure_reason="unsupported_generator"
                )
    except Exception as exc:
        return KeyedFragmentRepairCallResult(
            latency_ms=int((time.monotonic() - started) * 1000),
            failure_reason=f"transport:{exc.__class__.__name__}",
        )
    result = parse_keyed_fragment_repair_response(
        raw,
        requested_keys=[target.key for target in targets],
    )
    # Both early Writer and post-Review repair consume this boundary. Do this
    # before their prose/name cleaners can damage or silently erase a food span.
    target_by_key = {target.key: target for target in targets}
    for key, text in list(result.replacements.items()):
        body = normalize_repaired_body(
            target_by_key[key].current_text, replacement=text, key=key,
        )
        if body is None:
            result.replacements.pop(key)
            result.invalid_details.append({
                "key": list(key), "reason": "food_attachment_contract_invalid",
            })
        else:
            result.replacements[key] = body
    if not result.replacements and not result.failure_reason:
        result.failure_reason = "no_valid_replacements"
    result.latency_ms = int((time.monotonic() - started) * 1000)
    return result


async def review_keyed_fragment_replacements(
    *,
    targets: list[KeyedFragmentRepairTarget],
    replacements: dict[FragmentKey, str],
    timeout_seconds: float = KEYED_FRAGMENT_REVIEW_TIMEOUT_SECONDS,
) -> KeyedFragmentReviewResult:
    if not replacements:
        return KeyedFragmentReviewResult(failure_reason="no_replacements")
    target_by_key = {target.key: target for target in targets}
    try:
        contexts = _plan_contexts(targets)
    except ValueError:
        return KeyedFragmentReviewResult(rejected_keys=set(replacements), failure_reason="inconsistent_repair_context")
    payload = {
        "plan_contexts": contexts,
        "fragments": [
            {
                **target_by_key[key].to_review_prompt_dict(),
                "replacement_text": text,
            }
            for key, text in replacements.items()
            if key in target_by_key
        ]
    }
    started = time.monotonic()
    try:
        with llm_call_context(
            call_reason="keyed_fragment_repair_review",
            role="review",
            stage="KEYED_FRAGMENT_REVIEW",
        ):
            raw = await asyncio.wait_for(
                chat(
                    system=KEYED_FRAGMENT_REVIEW_SYSTEM_PROMPT,
                    user=json.dumps(payload, ensure_ascii=False),
                    role="review",
                    temperature=0.1,
                    json_mode=True,
                ),
                timeout=timeout_seconds,
            )
    except Exception as exc:
        return KeyedFragmentReviewResult(
            latency_ms=int((time.monotonic() - started) * 1000),
            failure_reason=f"transport:{exc.__class__.__name__}",
        )
    try:
        data = decode_model_json(raw)
        items = data.get("rejected_keys") if isinstance(data, dict) else None
    except (TypeError, json.JSONDecodeError):
        items = None
    if not isinstance(items, list):
        return KeyedFragmentReviewResult(
            latency_ms=int((time.monotonic() - started) * 1000),
            failure_reason="invalid_json",
        )
    requested = set(replacements)
    rejected: set[FragmentKey] = set()
    for item in items:
        key = _parse_key(item)
        if key is None or key not in requested:
            return KeyedFragmentReviewResult(
                latency_ms=int((time.monotonic() - started) * 1000),
                failure_reason="output_contract_invalid",
            )
        rejected.add(key)
    return KeyedFragmentReviewResult(
        rejected_keys=rejected,
        latency_ms=int((time.monotonic() - started) * 1000),
    )
