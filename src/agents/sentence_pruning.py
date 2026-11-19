"""Exact, local removal of R6 prose defects; delivery remains with the full gates.

Offsets and issue IDs in the private ledger refer to the immutable input draft.
There are no model calls and no text/ownership inference in this module.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import re

from src.agents.activity_completion import completion_sentence_for_contract
from src.agents.evidence_strength import StructuredEvidencePayload
from src.agents.food_review import parse_food_spans
from src.agents.generation_issues import GenerationIssue, review_issue_id
from src.agents.poi_fragments import fragment_registry_is_intact
from src.agents.route_planning import authorized_commute_mask_strings
from src.agents.schema import PlanOutput, RoutePlan


PRUNABLE_REASONS = frozenset({
    "unsupported_fact_expansion", "accommodation_fabrication",
    "accommodation_price_claim", "route_outside_poi", "cross_day_poi",
    # Sixth R6 category (2026-11-08): an unauthorized weather remark in prose.
    # Other weather reasons (route drift, POI binding, format) keep their path.
    "weather_unauthorized_claim",
})
PRUNABLE_ACTIONS = frozenset({"REPAIR_PLAN", "FAIL_CLOSED"})
_STRUCTURE = re.compile(
    r"plan_name|day_place_names|used_place_names|day.heading|day_heading|"
    r"route_membership|route_structure|方案名|日标题|地点清单|每天的地点归属", re.I)
_ENDS = re.compile(r"(?:[。！？!?；;\n]+|(?<!\d)\.|\.(?!\d))[”’\"']*")
_ABBREVIATION = re.compile(r"\b(?:[A-Za-z]{1,3}|approx|prof|dept|misc|assoc|univ|incl|excl)\.", re.I)
_DANGLING_CONNECTORS = frozenset({"所以", "因此", "然后", "接着", "此外", "于是", "但是", "不过", "同时", "而且", "或者", "并且", "也就是说"})


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_pruning_candidate(issue: GenerationIssue) -> bool:
    """Only actionable R6 findings enter pruning; record-only notes stay notes."""
    return issue.reason in PRUNABLE_REASONS and issue.publish_action in PRUNABLE_ACTIONS


@dataclass
class PruningResult:
    plans: list[PlanOutput]
    actions: list[dict] = field(default_factory=list)
    resolved_issue_ids: list[str] = field(default_factory=list)
    unresolved_issue_ids: list[str] = field(default_factory=list)
    # Why each unresolved finding had no deletable sentence (observability).
    unresolved_details: list[dict] = field(default_factory=list)
    changed: bool = False

    @property
    def deleted_sentence_count(self) -> int:
        return len(self.actions)

    @property
    def fallback_count(self) -> int:
        return len({tuple(sorted(a["fragment_key"].items())) for a in self.actions
                    if a["replacement_text"] and a["fragment_key"]})


@dataclass(frozen=True)
class _Owner:
    plan_index: int
    surface: str
    start: int
    end: int
    day: int | None = None
    place_id: int | None = None

    @property
    def key(self) -> dict | None:
        if self.place_id is None:
            return None
        return {"plan_index": self.plan_index, "day": self.day, "place_id": self.place_id}


def _surface(plan: PlanOutput, surface: str) -> str:
    return plan.summary if surface == "summary" else plan.plan_text


def _owners(plan: PlanOutput, index: int, route: RoutePlan | None) -> list[_Owner]:
    owners = [_Owner(index, "summary", 0, len(plan.summary))] if plan.summary else []
    if not fragment_registry_is_intact(plan) or route is None:
        return owners
    for fragment in plan.poi_fragments:
        # Registered ownership must also belong to this locked route and plan.
        places = [p for d in route.day_groups if d.day == fragment.day
                  for p in d.places if p.place_id == fragment.place_id]
        if fragment.plan_index != index or len(places) != 1:
            continue
        prefix = re.match(rf"\s*{re.escape(places[0].name)}\s*[：:]\s*", fragment.text)
        start = fragment.start + (prefix.end() if prefix else 0)
        owners.append(_Owner(index, "plan_text", start, fragment.end,
                             fragment.day, fragment.place_id))
    return owners


def _sentences(text: str, start: int, end: int) -> list[tuple[int, int]]:
    part = text[start:end]
    # A dot in a decimal is not a sentence end. Abbreviations, initials,
    # URLs and dots embedded in words do not prove a sentence boundary.
    if _ABBREVIATION.search(part) or re.search(r"[A-Za-z]\.[A-Za-z]", part):
        return []
    if re.search(r"(?<!\d)\.(?!\d|\s|[”’\"']|$)", part):
        return []
    spans = []
    cursor = start
    for match in _ENDS.finditer(text, start, end):
        # Whitespace is not part of an offending sentence and remains exact.
        left = cursor
        while left < match.end() and text[left].isspace():
            left += 1
        if left < match.end():
            spans.append((left, match.end()))
        cursor = match.end()
    left = cursor
    while left < end and text[left].isspace():
        left += 1
    if left < end:
        spans.append((left, end))
    return spans


def _blocked(plan: PlanOutput, route: RoutePlan | None) -> list[tuple[int, int]]:
    text = plan.plan_text
    blocked = [(s.start, s.end) for s in parse_food_spans(text)]
    # Broken markers have no trustworthy attachment boundary.
    if text.count("<!--") != len(blocked) * 2 or text.count("-->") != len(blocked) * 2:
        return [(0, len(text))]
    for m in re.finditer(r"(?im)^\s*(?:#{1,6}\s*)?(?:\*\*)?\s*(?:Day\s*\d+|第\s*\d+\s*天)[^\n]*", text):
        blocked.append((m.start(), m.end()))
    values = list(authorized_commute_mask_strings(route)) if route is not None else []
    if plan.code_lodging_section:
        values.append(plan.code_lodging_section)
    for value in values:
        if value:
            blocked.extend((m.start(), m.end()) for m in re.finditer(re.escape(value), text))
    return blocked


def _timeline_prompt() -> bool:
    from src.config import get_settings
    return str(getattr(get_settings(), "writer_prompt_version", "") or "").strip() in ("timeline", "article")


def _locate(issue: GenerationIssue, plans: list[PlanOutput], routes: list[RoutePlan]) -> list[tuple[_Owner, int, int]]:
    """Sentences to delete for one finding; empty when it has no safe anchor."""
    if not is_pruning_candidate(issue):
        return []
    meta = issue.metadata or {}
    # A structural finding must not be hidden by deleting a coincidental prose echo.
    scope = " ".join(str(meta.get(k, "")) for k in ("surface", "field", "field_path", "location"))
    if _STRUCTURE.search(scope) or (issue.source == "publish_gate" and _STRUCTURE.search(issue.evidence)):
        return []
    needle = issue.snippet
    gate_named = False
    # These exact messages are generated by Publish Gate, not by a model.
    # Its display snippet can include adjacent sentences or be clipped. The
    # explicit offending name is an exact anchor; it still must identify one
    # editable occurrence and cannot target a heading/route line.
    if issue.source == "publish_gate":
        prefixes = {
            "route_outside_poi": "route-outside POI found in plan text: ",
            "cross_day_poi": "POI appears in wrong day section: ",
        }
        prefix = prefixes.get(issue.reason)
        if prefix and issue.evidence.startswith(prefix):
            named = issue.evidence[len(prefix):]
            if named and named == named.strip() and "\n" not in named:
                needle, gate_named = named, True
    if not needle or not needle.strip() or any(t in needle for t in ("...", "…", "<!--", "-->")):
        return []
    constraints = {"plan_index": issue.plan_index, "day": issue.day, "place_id": issue.place_id}
    key = meta.get("action_contract_key")
    if key is not None:
        if not isinstance(key, dict) or any(type(key.get(k)) is not int or key[k] <= 0 for k in constraints):
            return []
        if any(v is not None and v != key[k] for k, v in constraints.items()):
            return []
        constraints.update(key)
    hits = []
    for index, plan in enumerate(plans, 1):
        if constraints["plan_index"] is not None and constraints["plan_index"] != index:
            continue
        route = routes[index-1] if index <= len(routes) else None
        protected = _blocked(plan, route)
        for owner in _owners(plan, index, route):
            if constraints["place_id"] is not None and constraints["place_id"] != owner.place_id:
                continue
            if constraints["day"] is not None and constraints["day"] != owner.day:
                continue
            if meta.get("surface") in {"summary", "plan_text"} and meta["surface"] != owner.surface:
                continue
            text = _surface(plan, owner.surface)
            for match in re.finditer(re.escape(needle), text[owner.start:owner.end]):
                start, end = owner.start+match.start(), owner.start+match.end()
                spans = [(a,b) for a,b in _sentences(text,owner.start,owner.end) if a <= start < end <= b]
                if len(spans) != 1:
                    continue
                a,b = spans[0]
                if owner.surface == "plan_text" and any(a < y and b > x for x,y in protected):
                    continue
                if meta.get("fragment_sha256"):
                    fragment = next((f for f in plan.poi_fragments if f.day==owner.day and f.place_id==owner.place_id),None)
                    # Existing qualifiers bind either the exact sentence or whole fragment.
                    valid_hashes = {_sha(text[a:b]), _sha(fragment.text) if fragment else ""}
                    if meta["fragment_sha256"] not in valid_hashes:
                        continue
                hits.append((owner,a,b))
    # Repeated snippets require a stronger anchor; never choose the first match.
    # v0.15.1 timeline: a gate-named place is forbidden anywhere in the plan,
    # so every editable sentence naming it goes; the full gates rerun after.
    if gate_named and _timeline_prompt():
        return list(dict.fromkeys(hits))
    return hits if len(hits) == 1 else []


def _prose_body(text: str) -> str:
    for span in reversed(parse_food_spans(text)):
        text = text[:span.start] + text[span.end:]
    return text


def _dangling_only(text: str) -> bool:
    from src.agents.explicit_preference_fulfillment import has_executed_activity

    prose = _prose_body(text)
    pieces = [p for p in re.split(r"[\W_]+", prose) if p]
    return bool(pieces) and not has_executed_activity(prose) and all(p in _DANGLING_CONNECTORS for p in pieces)


def prune_problem_sentences(
    plans: list[PlanOutput], *, issues: list[GenerationIssue],
    route_plans: list[RoutePlan],
    structured_evidence_payload: StructuredEvidencePayload | None = None,
    fill_empty: bool = True,
) -> PruningResult:
    """Delete uniquely anchored sentences, preserving every other input byte.

    Only registered POI narrative and Writer summary are editable. Unregistered
    openings and legacy prose lack ownership evidence and remain unresolved.
    Empty POI narrative needs its existing F1 action contract for W11 completion.
    A changed draft is never declared publishable here: callers rerun full gates.
    """
    result = PruningResult(plans=[p.model_copy(deep=True) for p in plans])
    by_span: dict[tuple[_Owner,int,int],dict] = {}
    candidates = [issue for issue in issues if is_pruning_candidate(issue)]
    all_ids = list(dict.fromkeys(review_issue_id(i) for i in candidates))
    for issue in candidates:
        for location in _locate(issue, plans, route_plans):
            action = by_span.setdefault(location, {"issue_ids":[],"reasons":[]})
            ident = review_issue_id(issue)
            if ident not in action["issue_ids"]:
                action["issue_ids"].append(ident)
            if issue.reason not in action["reasons"]:
                action["reasons"].append(issue.reason)
    by_owner: dict[_Owner,list[tuple[int,int,dict]]] = {}
    for (owner,start,end), info in by_span.items():
        by_owner.setdefault(owner,[]).append((start,end,info))
    edits: dict[tuple[int,str],list[tuple[int,int,str,_Owner,dict]]] = {}
    for owner, spans in by_owner.items():
        original = _surface(plans[owner.plan_index-1],owner.surface)
        residual = original[owner.start:owner.end]
        for start,end,_ in sorted(spans,reverse=True):
            residual = residual[:start-owner.start] + residual[end-owner.start:]
        fallback = ""
        dangling = _dangling_only(residual)
        if owner.place_id is not None and (not re.search(r"[\w\u3400-\u9fff]", _prose_body(residual)) or dangling):
            if not fill_empty or structured_evidence_payload is None:
                continue
            contract = structured_evidence_payload.action_contract(**owner.key)
            route = route_plans[owner.plan_index-1]
            places = [p for d in route.day_groups if d.day==owner.day for p in d.places if p.place_id==owner.place_id]
            if contract is None or len(places)!=1 or contract.place_name!=places[0].name or contract.blueprint_role=="transfer_context":
                continue
            fallback = completion_sentence_for_contract(contract)
            if dangling:
                # Only connector-only residuals are discarded, never intact
                # normal sentences. Every removed orphan gets its own ledger
                # entry so reconstruction still uses original offsets.
                issue_ids = list(dict.fromkeys(i for _,_,row in spans for i in row["issue_ids"]))
                for a,b in _sentences(original,owner.start,owner.end):
                    if any(a < end and b > start for start,end,_ in spans):
                        continue
                    if _dangling_only(original[a:b]):
                        spans.append((a,b,{"issue_ids":issue_ids,"reasons":["unreadable_residual"],"kind":"unreadable_residual"}))
        for position,(start,end,info) in enumerate(sorted(spans)):
            replacement = fallback if position == 0 else ""
            edits.setdefault((owner.plan_index,owner.surface),[]).append((start,end,replacement,owner,info))
    rolled_back_plans: set[int] = set()
    for (index,surface), surface_edits in edits.items():
        if index in rolled_back_plans:
            continue
        plan = result.plans[index-1]
        original = _surface(plans[index-1],surface)
        updated = original
        for start,end,replacement,_,_ in sorted(surface_edits, key=lambda row:row[0],reverse=True):
            updated = updated[:start]+replacement+updated[end:]
        if surface == "summary":
            plan.summary = updated
        else:
            fragments = []
            for fragment in plan.poi_fragments:
                shift = sum(len(repl)-(end-start) for start,end,repl,_,_ in surface_edits if end <= fragment.start)
                owned = [(a,b,repl) for a,b,repl,owner,_ in surface_edits if owner.place_id==fragment.place_id and owner.day==fragment.day]
                fragment_text = fragment.text
                for a,b,repl in sorted(owned,reverse=True):
                    fragment_text = fragment_text[:a-fragment.start]+repl+fragment_text[b-fragment.start:]
                fragments.append(fragment.model_copy(update={"start":fragment.start+shift,"end":fragment.start+shift+len(fragment_text),"text":fragment_text,
                    **({"source":"deterministic_completion" if any(x[2] for x in owned) else "fragment_repair"} if owned else {})}))
            plan.plan_text, plan.poi_fragments = updated, fragments
            if not fragment_registry_is_intact(plan):
                result.plans[index-1] = plans[index-1].model_copy(deep=True)
                # All surfaces and their ledger entries are one plan-level
                # transaction. Keep independent plans, but never retain an
                # earlier summary edit or apply a later one after rollback.
                result.actions = [action for action in result.actions
                                  if action["plan_index"] != index]
                rolled_back_plans.add(index)
                continue
        for start,end,replacement,owner,info in sorted(surface_edits,key=lambda row:row[0]):
            result.actions.append({"kind":"problem_sentence",**info,"reason":info["reasons"][0],"plan_index":index,"surface":surface,
                "start":start,"end":end,"deleted_text":original[start:end],"replacement_text":replacement,
                "fragment_key":owner.key,"day":owner.day,"before_sha256":_sha(original),"after_sha256":_sha(updated),
                "replacement_source":"deterministic_action_contract" if replacement else ""})
    result.resolved_issue_ids = list(dict.fromkeys(
        issue_id for action in result.actions for issue_id in action["issue_ids"]
    ))
    result.unresolved_issue_ids = [i for i in all_ids if i not in result.resolved_issue_ids]
    unresolved = set(result.unresolved_issue_ids)
    result.unresolved_details = [
        {"issue_id": review_issue_id(issue), "reason": issue.reason, "source": issue.source,
         "place_id": issue.place_id, "snippet": str(issue.snippet or "")[:80],
         "evidence": str(issue.evidence or "")[:80]}
        for issue in candidates if review_issue_id(issue) in unresolved
    ][:12]
    result.changed = bool(result.actions)
    return result
