"""Delete-only sanitizer for Writer repair output."""

from __future__ import annotations

import re

from src.agents.generation_issues import GenerationIssue
from src.agents.poi_alias import mask_evidenced_place_references
from src.agents.schema import CandidatePlace
from src.agents.route_planning import _is_day_heading_line, _parse_day_heading_number
from src.agents.text_quality import BANNED_DATABASE_PHRASES
from src.agents.writer_repair_contract import RepairSanitizerAction

DAY_HEADING_RE = re.compile(
    r"(?im)^\s*(?:#{1,6}\s*)?(?:\*\*)?\s*"
    r"(?:Day\s*(\d+)|第\s*(\d+|[一二三四五六七八九十]+)\s*(?:天|日))"
    r"([^\n]*?)(?:\*\*)?\s*$"
)
SENTENCE_SEGMENT_RE = re.compile(r"([^。！？；;\n]*[。！？；;\n]?)")

def _remove_banned_database_phrases(
    text: str,
    *,
    issues: list[GenerationIssue],
) -> tuple[str, list[RepairSanitizerAction]]:
    actions: list[RepairSanitizerAction] = []
    if not text:
        return text, actions
    should_clean = any(
        issue.reason == "database_tone"
        or issue.reason == "unsupported_fact_expansion"
        or (
            issue.reason == "unsupported_fact_expansion"
            and isinstance(issue.metadata, dict)
            and issue.metadata.get("pattern_reason") == "source_voice_claim"
        )
        for issue in issues
    )
    if not should_clean:
        return text, actions
    sanitized = text
    for phrase in BANNED_DATABASE_PHRASES:
        if phrase not in sanitized:
            continue
        sanitized = sanitized.replace(phrase, "")
        actions.append(RepairSanitizerAction(
            action="remove_database_phrase",
            target=phrase,
            reason="database_tone",
            issue_reason="database_tone",
        ))
    return sanitized, actions


def _remove_known_snippets(
    text: str,
    *,
    issues: list[GenerationIssue],
) -> tuple[str, list[RepairSanitizerAction]]:
    actions: list[RepairSanitizerAction] = []
    sanitized = text
    for issue in issues:
        if issue.publish_action == "RECORD_ONLY" or issue.category == "WARN":
            continue
        if issue.reason == "long_transfer_missing_or_misleading":
            continue
        snippet = issue.snippet.strip()
        if not snippet or sanitized.count(snippet) != 1:
            continue
        sanitized = sanitized.replace(snippet, "")
        pattern_reason = ""
        if isinstance(issue.metadata, dict):
            pattern_reason = str(issue.metadata.get("pattern_reason") or "")
        actions.append(RepairSanitizerAction(
            action="remove_snippet",
            target=snippet[:120],
            reason="issue_snippet",
            issue_reason=issue.reason,
            pattern_reason=pattern_reason,
        ))
    return sanitized, actions


def _remove_disallowed_name_segments(
    text: str,
    *,
    disallowed_candidate_names: list[str],
    allowed_candidate_names: list[str] | None = None,
    reference_place: CandidatePlace | None = None,
    reference_locked_names: set[str] | None = None,
) -> tuple[str, list[RepairSanitizerAction]]:
    actions: list[RepairSanitizerAction] = []
    if not text or not disallowed_candidate_names:
        return text, actions
    names = sorted(set(disallowed_candidate_names), key=lambda value: (-len(value), value))
    allowed_names = sorted(
        set(allowed_candidate_names or []),
        key=lambda value: (-len(value), value),
    )
    pieces = re.split(r"([^。！？；;\n]*[。！？；;\n]?)", text)
    sanitized_parts: list[str] = []
    for piece in pieces:
        if not piece:
            continue
        searchable_piece = (mask_evidenced_place_references(
            piece, place=reference_place, candidate_names=[*names, *allowed_names],
            locked_names=reference_locked_names or set(),
        ) if reference_place is not None else piece)
        for allowed_name in allowed_names:
            if allowed_name and allowed_name in searchable_piece:
                searchable_piece = searchable_piece.replace(
                    allowed_name,
                    " " * len(allowed_name),
                )
        hits = [
            name for name in names
            if name and name in searchable_piece
        ]
        if hits and not re.search(
            r"(?im)^\s*(?:#{1,6}\s*)?(?:\*\*)?\s*"
            r"(?:Day\s*\d+|第\s*\d+\s*天)",
            piece,
        ):
            actions.append(RepairSanitizerAction(
                action="remove_route_outside_segment",
                target=piece.strip()[:160],
                reason="route_outside_candidate",
            ))
            continue
        sanitized_parts.append(piece)
    return "".join(sanitized_parts), actions


def _remove_cross_day_segments(
    text: str,
    *,
    locked_day_names: list[list[str]] | None,
) -> tuple[str, list[RepairSanitizerAction]]:
    actions: list[RepairSanitizerAction] = []
    if not text or not locked_day_names:
        return text, actions

    all_locked_names = sorted(
        {name for day_names in locked_day_names for name in day_names if name},
        key=lambda value: (-len(value), value),
    )
    if not all_locked_names:
        return text, actions

    valid_matches: list[tuple[re.Match[str], int]] = []
    for match in DAY_HEADING_RE.finditer(text):
        day_number = _parse_day_heading_number(match.group(1) or match.group(2))
        remainder = match.group(3) or ""
        if day_number is None or not _is_day_heading_line(match.group(0), remainder):
            continue
        valid_matches.append((match, day_number))
    if not valid_matches:
        return text, actions

    sanitized_parts: list[str] = [text[:valid_matches[0][0].start()]]
    for index, (match, day_number) in enumerate(valid_matches):
        section_end = (
            valid_matches[index + 1][0].start()
            if index + 1 < len(valid_matches)
            else len(text)
        )
        heading = text[match.start():match.end()]
        body = text[match.end():section_end]
        if day_number < 1 or day_number > len(locked_day_names):
            sanitized_parts.append(text[match.start():section_end])
            continue

        current_day_names = {name for name in locked_day_names[day_number - 1] if name}
        cross_day_forbidden = [
            name for name in all_locked_names if name not in current_day_names
        ]
        if not cross_day_forbidden:
            sanitized_parts.append(text[match.start():section_end])
            continue

        sanitized_body_parts: list[str] = []
        for piece in SENTENCE_SEGMENT_RE.split(body):
            if not piece:
                continue
            hits = [name for name in cross_day_forbidden if name in piece]
            if hits:
                actions.append(RepairSanitizerAction(
                    action="remove_cross_day_segment",
                    target=piece.strip()[:160],
                    reason="cross_day_locked_poi",
                ))
                continue
            sanitized_body_parts.append(piece)
        sanitized_parts.append(heading + "".join(sanitized_body_parts))

    return "".join(sanitized_parts), actions



def _cleanup_sanitized_text(text: str) -> str:
    cleaned = re.sub(r"[ \t]{2,}", " ", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r"([，、；;])\s*([。！？])", r"\2", cleaned)
    cleaned = re.sub(r"[，、；;]\s*(\n|$)", r"\1", cleaned)
    return cleaned.strip()


def sanitize_repair_text(
    text: str,
    *,
    issues: list[GenerationIssue],
    disallowed_candidate_names: list[str],
    allowed_candidate_names: list[str] | None = None,
    locked_day_names: list[list[str]] | None = None,
    reference_place: CandidatePlace | None = None,
    reference_locked_names: set[str] | None = None,
) -> tuple[str, list[RepairSanitizerAction]]:
    """Remove only known risky snippets; never invent replacement facts."""
    sanitized, database_actions = _remove_banned_database_phrases(
        text,
        issues=issues,
    )
    sanitized, snippet_actions = _remove_known_snippets(sanitized, issues=issues)
    sanitized, name_actions = _remove_disallowed_name_segments(
        sanitized,
        disallowed_candidate_names=disallowed_candidate_names,
        allowed_candidate_names=allowed_candidate_names,
        reference_place=reference_place,
        reference_locked_names=reference_locked_names,
    )
    sanitized, cross_day_actions = _remove_cross_day_segments(
        sanitized,
        locked_day_names=locked_day_names,
    )
    return (
        _cleanup_sanitized_text(sanitized),
        [
            *database_actions,
            *snippet_actions,
            *name_actions,
            *cross_day_actions,
        ],
    )
