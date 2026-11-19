"""Stable-key helpers for backend-owned POI narrative fragments."""

from __future__ import annotations

from difflib import SequenceMatcher
import re
import unicodedata

from src.agents.food_review import FoodProseSpan, parse_food_spans
from src.agents.schema import PlanOutput, PoiNarrativeFragment


def fragment_registry_is_intact(plan: PlanOutput) -> bool:
    """Return whether every stable key still owns its exact registered span."""
    text = plan.plan_text or ""
    seen_keys: set[tuple[int, int, int]] = set()
    previous_end = 0
    for fragment in sorted(plan.poi_fragments, key=lambda item: item.start):
        key = (fragment.plan_index, fragment.day, fragment.place_id)
        if (
            key in seen_keys
            or fragment.start < previous_end
            or fragment.end <= fragment.start
            or fragment.end > len(text)
            or text[fragment.start:fragment.end] != fragment.text
        ):
            return False
        seen_keys.add(key)
        previous_end = fragment.end
    return True


def remap_fragment_registry(
    plan: PlanOutput,
    *,
    updated_text: str,
) -> PlanOutput | None:
    """Apply a whole-text deterministic edit without losing stable ownership.

    The mapping is derived from exact old/new offsets, never from place-name or
    finding-text ownership guesses. Edits that cross a fragment boundary are
    rejected because their owner is ambiguous.
    """
    old_text = plan.plan_text or ""
    if not plan.poi_fragments:
        return plan.model_copy(update={"plan_text": updated_text})
    if not fragment_registry_is_intact(plan):
        return None
    if updated_text == old_text:
        return plan

    opcodes = SequenceMatcher(
        None,
        old_text,
        updated_text,
        autojunk=False,
    ).get_opcodes()

    def mapped_boundary(position: int, *, start_boundary: bool) -> int | None:
        # Resolve a zero-width insert before any equal span that also owns
        # this offset. Otherwise an insert sitting on fragment.start is
        # swallowed into that fragment.
        for tag, old_start, _old_end, new_start, new_end in opcodes:
            if tag == "insert" and position == old_start:
                # Text inserted at a fragment start belongs before it;
                # text inserted at a fragment end belongs after it.
                return new_end if start_boundary else new_start
        for tag, old_start, old_end, new_start, new_end in opcodes:
            if tag == "insert":
                continue
            if old_start <= position <= old_end:
                if tag == "equal":
                    return new_start + (position - old_start)
                if position == old_start:
                    return new_start
                if position == old_end:
                    return new_end
                return None
        if position == len(old_text):
            return len(updated_text)
        return None

    updated_fragments: list[PoiNarrativeFragment] = []
    for fragment in sorted(plan.poi_fragments, key=lambda item: item.start):
        for tag, old_start, old_end, _new_start, _new_end in opcodes:
            if tag == "equal" or old_start == old_end:
                continue
            if (
                old_start < fragment.start < old_end
                or old_start < fragment.end < old_end
            ):
                return None
        new_start = mapped_boundary(fragment.start, start_boundary=True)
        new_end = mapped_boundary(fragment.end, start_boundary=False)
        if (
            new_start is None
            or new_end is None
            or new_end <= new_start
            or new_end > len(updated_text)
        ):
            return None
        updated_fragments.append(fragment.model_copy(update={
            "text": updated_text[new_start:new_end],
            "start": new_start,
            "end": new_end,
        }))

    updated_plan = plan.model_copy(update={
        "plan_text": updated_text,
        "poi_fragments": updated_fragments,
    })
    return updated_plan if fragment_registry_is_intact(updated_plan) else None


def fragment_for_span(
    plan: PlanOutput,
    *,
    start: int,
    end: int,
) -> PoiNarrativeFragment | None:
    """Resolve a text span only through the backend fragment registry."""
    if start < 0 or end <= start:
        return None
    matches = [
        fragment
        for fragment in plan.poi_fragments
        if fragment.start <= start and end <= fragment.end
    ]
    return matches[0] if len(matches) == 1 else None


def replace_fragment_text(
    plan: PlanOutput,
    *,
    plan_index: int,
    day: int,
    place_id: int,
    replacement: str,
    source: str = "deterministic_completion",
) -> PlanOutput | None:
    """Replace one keyed fragment and shift later immutable spans.

    Ownership is selected solely by ``(plan_index, day, place_id)``.  Text is
    used only as an integrity check after the key has selected the fragment.
    """
    target = plan.poi_fragment(
        plan_index=plan_index,
        day=day,
        place_id=place_id,
    )
    text = plan.plan_text or ""
    replacement = (replacement or "").strip()
    if (
        target is None
        or not replacement
        or target.start < 0
        or target.end <= target.start
        or target.end > len(text)
        or text[target.start:target.end] != target.text
    ):
        return None

    updated_text = text[:target.start] + replacement + text[target.end:]
    delta = len(replacement) - (target.end - target.start)
    updated_fragments: list[PoiNarrativeFragment] = []
    for fragment in plan.poi_fragments:
        if fragment is target:
            updated_fragments.append(fragment.model_copy(update={
                "text": replacement,
                "end": target.start + len(replacement),
                "source": source,
            }))
            continue
        if fragment.start >= target.end:
            updated_fragments.append(fragment.model_copy(update={
                "start": fragment.start + delta,
                "end": fragment.end + delta,
            }))
            continue
        updated_fragments.append(fragment)

    return plan.model_copy(update={
        "plan_text": updated_text,
        "poi_fragments": updated_fragments,
    })


def _bound_food_spans(
    text: str,
    *,
    key: tuple[int, int, int],
    food_hint: dict | None = None,
) -> list[FoodProseSpan] | None:
    """Check attachment shape/ownership without granting content authority."""
    spans = parse_food_spans(text)
    if text.count("<!--") != 2 * len(spans) or text.count("-->") != 2 * len(spans):
        return None
    seen = set()
    for span in spans:
        if span.key[:3] != (key[0] - 1, key[1], key[2]) or span.key in seen:
            return None
        seen.add(span.key)
        if food_hint is not None and span.key[3:] != (
            food_hint.get("meal_slot"), food_hint.get("food_place_id"),
        ):
            return None
    return spans


def _fragment_food_spans(
    plan: PlanOutput,
    fragment: PoiNarrativeFragment,
) -> list[FoodProseSpan] | None:
    hints = []
    if plan.composition_blueprint is not None:
        hints = [
            stop.writing_hint
            for day in plan.composition_blueprint.days if day.day == fragment.day
            for stop in day.stops if stop.place_id == fragment.place_id
            if isinstance(stop.writing_hint, dict) and stop.writing_hint.get("span_marker")
        ]
    if len(hints) > 1:
        return None
    return _bound_food_spans(fragment.text,
        key=(fragment.plan_index, fragment.day, fragment.place_id),
        food_hint=hints[0] if hints else None)


def repair_prompt_body(original_text: str, *, key: tuple[int, int, int]) -> str | None:
    """Expose only prose; attachment ownership stays in the local original."""
    spans = _bound_food_spans(original_text, key=key)
    if spans is None:
        return None
    body = original_text
    for span in reversed(spans):
        body = body[:span.start] + body[span.end:]
    return body.strip()


def normalize_repaired_body(
    original_text: str,
    *,
    replacement: str,
    key: tuple[int, int, int],
) -> str | None:
    """Separate exact attachment echoes before any prose sanitizer runs.

    A body-only response is valid. An echoed attachment must match the complete
    original span list byte for byte, including markers and order. New, changed,
    partial, duplicate or malformed attachments are rejected, never cleaned up.
    The caller still merges against its original and runs all content gates.
    """
    original_spans = _bound_food_spans(original_text, key=key)
    replacement_spans = _bound_food_spans(replacement, key=key)
    if original_spans is None or replacement_spans is None:
        return None
    if replacement_spans and [
        replacement[span.start:span.end] for span in replacement_spans
    ] != [original_text[span.start:span.end] for span in original_spans]:
        return None
    body = repair_prompt_body(replacement, key=key)
    return body if body else None


def fragment_food_hint(plan: PlanOutput, fragment: PoiNarrativeFragment) -> dict | None:
    """Read one existing slot; ambiguous/missing hints grant no deduplication."""
    hints = [
        stop.writing_hint
        for day in (plan.composition_blueprint.days if plan.composition_blueprint else [])
        if day.day == fragment.day
        for stop in day.stops if stop.place_id == fragment.place_id
        if isinstance(stop.writing_hint, dict) and stop.writing_hint.get("span_marker")
    ]
    return hints[0] if len(hints) == 1 else None


def _pure_meal_signature(text: str, food_hint: dict) -> str | None:
    # Only the confirmed lunch synonym and formatting differences normalize.
    # Closed structural grammar excludes POI actions, dishes and new claims.
    labels = {"lunch": ("午餐", "中午"), "dinner": ("晚餐",), "breakfast": ("早餐",)}
    aliases = labels.get(food_hint.get("meal_slot"), ())
    if not aliases:
        return None
    def compact(value: object) -> str:
        return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value)))
    text = compact(text).rstrip("。.;；")
    food_name = compact(food_hint.get("food_name") or "")
    if not food_name:
        return None
    pattern = (
        rf"(?:{'|'.join(aliases)})可以去{re.escape(food_name)}"
        r"(?:,步行约(?P<walk>\d+(?:\.\d+)?)分钟)?"
        r"(?:,评分(?P<rating>\d+(?:\.\d+)?))?"
        r"(?:,人均约(?P<price>\d+(?:\.\d+)?)元)?"
    )
    match = re.fullmatch(pattern, text)
    if match is None:
        return None
    for group, field in (("walk", "walk_minutes"), ("rating", "amap_rating"),
                         ("price", "amap_avg_price")):
        value = match.group(group)
        if value is not None and (
            food_hint.get(field) is None or value != compact(food_hint[field])
        ):
            return None
    return re.sub(rf"^(?:{'|'.join(aliases)})", aliases[0], text, count=1)


def normalize_body_food_boundary(
    body: str,
    *,
    key: tuple[int, int, int],
    attachment_text: str,
    food_hint: dict | None,
    place_name: str = "",
    route_place_names: tuple[str, ...] = (),
) -> str | None:
    """Keep immutable bound food separate before every prose/name cleaner.

    Exact marked echoes retain the existing contract. An unmarked clause is
    removed only when it is pure structural dining copy equivalent to a clause
    of this exact slot, with every stated value present in the existing hint.
    Unknown/mixed prose remains visible to the unchanged business gates.
    """
    spans = _bound_food_spans(attachment_text, key=key, food_hint=food_hint)
    if spans is None:
        return None
    body = normalize_repaired_body(attachment_text, replacement=body, key=key)
    if body is None or food_hint is None or food_hint.get("evidence_tier") not in {"full", "structural"}:
        return body
    if food_hint.get("food_name") in {place_name, *route_place_names}:
        # A real route food POI keeps its own authorized meal/activity prose.
        return body
    signatures = {
        signature
        for span in spans
        if attachment_text[span.start:span.content_start] == food_hint.get("span_marker")
        for clause in re.split(r"[。；;\n\r]+", span.content)
        if (signature := _pure_meal_signature(clause, food_hint)) is not None
    }
    if not signatures:
        return body
    prefix = ""
    if place_name:
        label = re.match(rf"^\s*{re.escape(place_name)}\s*[：:]\s*", body)
        if label:
            prefix, body = body[:label.end()], body[label.end():]
    pieces = re.split(r"([。；;\n\r]+)", body)
    retained = []
    for index in range(0, len(pieces), 2):
        clause = pieces[index]
        ending = pieces[index + 1] if index + 1 < len(pieces) else ""
        if _pure_meal_signature(clause, food_hint) not in signatures:
            retained.append(clause + ending)
    return prefix + "".join(retained).strip()


def replace_body_preserving_food(
    original_text: str,
    *,
    replacement: str,
    key: tuple[int, int, int],
) -> str | None:
    """Body repair cannot supply, rewrite or erase the existing food spans."""
    replacement = (replacement or "").strip()
    if not replacement or any(token in replacement for token in ("<!--", "-->")):
        return None
    spans = _bound_food_spans(original_text, key=key)
    if spans is None:
        return None
    return replacement + "".join(original_text[span.start:span.end] for span in spans)


def fragment_body_text(plan: PlanOutput, fragment: PoiNarrativeFragment) -> str | None:
    """Read body-only text when sending a repaired fragment back to assembly."""
    spans = _fragment_food_spans(plan, fragment)
    if spans is None:
        return None
    body = fragment.text
    for span in reversed(spans):
        body = body[:span.start] + body[span.end:]
    return body


def replace_fragment_body(
    plan: PlanOutput,
    *,
    plan_index: int,
    day: int,
    place_id: int,
    replacement: str,
    source: str = "deterministic_completion",
) -> PlanOutput | None:
    """Replace POI prose while retaining bound food spans verbatim.

    Malformed, duplicate or foreign spans leave the original untouched. Content
    authority (including canonical none text) remains with the existing gates.
    """
    target = plan.poi_fragment(plan_index=plan_index, day=day, place_id=place_id)
    if target is None or _fragment_food_spans(plan, target) is None:
        return None
    updated = replace_body_preserving_food(target.text, replacement=replacement,
        key=(plan_index, day, place_id))
    if updated is None:
        return None
    return replace_fragment_text(plan, plan_index=plan_index, day=day,
        place_id=place_id, replacement=updated, source=source)


def replace_fragment_range(
    plan: PlanOutput,
    *,
    start: int,
    end: int,
    replacement: str,
) -> PlanOutput | None:
    """Replace a verified subrange while preserving its existing stable key."""
    text = plan.plan_text or ""
    replacement = (replacement or "").strip()
    owner = fragment_for_span(plan, start=start, end=end)
    if (
        owner is None
        or not replacement
        or end > len(text)
        or text[owner.start:owner.end] != owner.text
    ):
        return None

    spans = _fragment_food_spans(plan, owner)
    if spans is None or any(t in replacement for t in ("<!--", "-->")):
        return None
    if any(start < owner.start + span.end and end > owner.start + span.start for span in spans):
        return None

    updated_text = text[:start] + replacement + text[end:]
    delta = len(replacement) - (end - start)
    local_start = start - owner.start
    local_end = end - owner.start
    owner_text = owner.text[:local_start] + replacement + owner.text[local_end:]
    updated_fragments: list[PoiNarrativeFragment] = []
    for fragment in plan.poi_fragments:
        if fragment is owner:
            updated_fragments.append(fragment.model_copy(update={
                "text": owner_text,
                "end": fragment.end + delta,
                "source": "fragment_repair",
            }))
            continue
        if fragment.start >= end:
            updated_fragments.append(fragment.model_copy(update={
                "start": fragment.start + delta,
                "end": fragment.end + delta,
            }))
            continue
        updated_fragments.append(fragment)
    return plan.model_copy(update={
        "plan_text": updated_text,
        "poi_fragments": updated_fragments,
    })


def fragment_registry_prompt(plan: PlanOutput) -> str:
    """Render Review-visible keys without exposing them in the public API."""
    return "\n".join(
        (
            f"- key=({fragment.plan_index},{fragment.day},{fragment.place_id}); "
            f"text={fragment.text}"
        )
        for fragment in plan.poi_fragments
    )
