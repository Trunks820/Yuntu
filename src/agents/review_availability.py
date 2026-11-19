"""Internal evidence binding for code-gated detailed delivery."""
from __future__ import annotations

import hashlib
import json


def code_only_body_fingerprint(plans) -> str:
    bodies = [{name: getattr(plan, name) for name in (
        "plan_name", "plan_text", "summary", "used_place_ids", "used_place_names", "day_place_names"
    )} for plan in plans]
    return hashlib.sha256(json.dumps(bodies, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def has_current_unreviewed_receipt(plans, metrics) -> bool:
    receipt = metrics.get("v015_unreviewed_receipt")
    if not isinstance(receipt, dict) or not plans:
        return False
    gates = receipt.get("code_gates")
    required = {
        "final_writer_succeeded", "writer_output_not_parseable_false",
        "writer_generated_plan_count_is_one", "plan_count_is_one",
        "route_day_integrity_passed", "blueprint_integrity_passed", "publish_preflight_passed",
        "deterministic_issue_gate_passed",
        "explicit_preference_gate_passed", "writer_template_ratio_passed", "final_publish_gate_passed",
    }
    return bool(
        receipt.get("version") == 1
        # "review_disabled" is W32's closed switch; it is not an outage.
        and receipt.get("cause") in {"technical_unavailable", "review_disabled"}
        and receipt.get("stage") in {"REVIEW_TAXONOMY", "REVIEW_TAXONOMY_AFTER_REPAIR"}
        and receipt.get("prior_findings_remaining") is False
        and isinstance(gates, dict) and required.issubset(gates)
        and all(value is True for value in gates.values())
        and metrics.get("review_status") == "unreviewed"
        and receipt.get("body_sha256") == code_only_body_fingerprint(plans)
    )


def pruning_change_matches(original, updated, actions) -> bool:
    """Every changed byte must be explained by a frozen, nonoverlapping edit."""
    from src.agents.poi_fragments import fragment_registry_is_intact
    if len(original) != len(updated) or not actions:
        return False
    for index, (before, after) in enumerate(zip(original, updated), 1):
        if (before.model_dump(exclude={"plan_text", "summary"})
                != after.model_dump(exclude={"plan_text", "summary"})
                or before.code_lodging_section != after.code_lodging_section):
            return False
        old_keys = [(f.plan_index, f.day, f.place_id) for f in before.poi_fragments]
        if old_keys != [(f.plan_index, f.day, f.place_id) for f in after.poi_fragments]:
            return False
        if not fragment_registry_is_intact(before) or not fragment_registry_is_intact(after):
            return False
        for surface in ("plan_text", "summary"):
            old, new = getattr(before, surface), getattr(after, surface)
            edits = sorted([a for a in actions if a["plan_index"] == index and a["surface"] == surface],
                           key=lambda a: a["start"])
            end = 0
            for action in edits:
                if (action["start"] < end or not 0 <= action["start"] < action["end"] <= len(old)
                        or old[action["start"]:action["end"]] != action["deleted_text"]
                        or action["before_sha256"] != hashlib.sha256(old.encode()).hexdigest()
                        or action["after_sha256"] != hashlib.sha256(new.encode()).hexdigest()):
                    return False
                end = action["end"]
            replay = old
            for action in reversed(edits):
                replay = replay[:action["start"]] + action["replacement_text"] + replay[action["end"]:]
            if replay != new:
                return False
    return True


def pruning_review_closures(review_receipt, source_issues, actions, plans):
    """Close only findings whose actual quoted problem was removed by R6.

    The original model receipt is evidence, never rewritten into a new approval.
    A broad full-body quote cannot be closed by deleting an unrelated sentence.
    """
    from src.agents.generation_issues import GenerationIssue, review_issue_id
    from src.agents.sentence_pruning import is_pruning_candidate

    issues = [GenerationIssue.model_validate(item) for item in source_issues]
    indexed = {review_issue_id(issue): issue for issue in issues}
    if len(indexed) != len(issues):
        return None
    for issue_id, row in review_receipt.get("issue_resolutions", {}).items():
        if row.get("status") == "still_present" and issue_id not in indexed:
            return None
    required = {
        issue_id for issue_id, issue in indexed.items()
        if issue.publish_action in {"REPAIR_PLAN", "FAIL_CLOSED"}
    }
    covered = {issue_id for action in actions for issue_id in action["issue_ids"]}
    if not required.issubset(covered) or not covered.issubset(indexed):
        return None
    for issue_id in covered:
        if not is_pruning_candidate(indexed[issue_id]):
            return None
    # Removing a word elsewhere, or changing punctuation, is not closure.
    import unicodedata
    def substance(text):
        return "".join(c for c in unicodedata.normalize("NFKC", text).casefold()
                       if not unicodedata.category(c).startswith(("P", "Z", "C")))
    current = substance("\n".join(p.plan_text + "\n" + p.summary for p in plans))
    for issue_id in required:
        issue = indexed[issue_id]
        snippet = substance(issue.snippet)
        if not snippet:
            return None
        for action in (a for a in actions if issue_id in a["issue_ids"]):
            if issue.plan_index is not None and issue.plan_index != action["plan_index"]:
                return None
            plan = plans[action["plan_index"] - 1]
            key = action.get("fragment_key")
            if key is not None:
                if ((issue.day is not None and issue.day != key.get("day"))
                        or (issue.place_id is not None and issue.place_id != key.get("place_id"))):
                    return None
                fragment = plan.poi_fragment(**key)
                if fragment is None:
                    return None
                owned_current = fragment.text
            elif action["surface"] == "summary" and issue.place_id is None:
                owned_current = plan.summary
            else:
                return None
            # A precisely owned finding does not condemn identical, authorized
            # prose in another POI. Ambiguous original ownership was rejected
            # by the pruning operator before this closure check.
            if snippet in substance(owned_current):
                return None
    closures = []
    for assessment_id, row in review_receipt.get("assessments", {}).items():
        if not (assessment_id.startswith("consistency:") and row.get("status") == "missing"):
            continue
        matches = []
        for ref in row.get("evidence", []):
            source, quote = ref.get("source", ""), ref.get("quote", "")
            if not source.startswith(("plan:", "summary:")) or not substance(quote):
                continue
            for index, action in enumerate(actions):
                expected_source = ("summary:" if action["surface"] == "summary" else "plan:") + str(action["plan_index"])
                # The entire contradictory quote must be inside a deleted
                # sentence, not merely overlap a broad quote of the whole plan.
                if (source == expected_source
                        and substance(quote) in substance(action["deleted_text"])
                        and substance(quote) not in current):
                    matches.append({"action_index": index, "source": source,
                                    "quote_sha256": hashlib.sha256(quote.encode()).hexdigest()})
        if not matches:
            return None
        closures.append({"assessment_id": assessment_id, "deleted_evidence": matches})
    return {"issue_ids": sorted(required), "consistency": closures}


def _has_current_pruning_receipt(plans, metrics) -> bool:
    """Worker gate for P4; it cannot be substituted by a W9 outage receipt."""
    from src.agents.poi_fragments import fragment_registry_is_intact
    from src.agents.sentence_pruning import PRUNABLE_REASONS

    receipt = metrics.get("v015_pruning_receipt")
    if not isinstance(receipt, dict) or not plans:
        return False
    required_gates = {
        "final_writer_succeeded", "writer_output_not_parseable_false",
        "writer_generated_plan_count_is_one", "plan_count_is_one",
        "route_day_integrity_passed", "blueprint_integrity_passed", "publish_preflight_passed",
        "deterministic_issue_gate_passed", "explicit_preference_gate_passed",
        "writer_template_ratio_passed", "final_publish_gate_passed", "pruning_boundaries_passed",
        "fragment_registry_passed", "known_findings_closed", "workflow_deadline_remaining",
    }
    gates = receipt.get("code_gates")
    source_review = receipt.get("source_review_receipt")
    actions = receipt.get("actions")
    source_issues = receipt.get("source_issues")
    def sha(value):
        return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    if not (
        receipt.get("version") == 1 and receipt.get("cause") == "deterministic_sentence_prune"
        and metrics.get("review_status") == "code_rechecked"
        and receipt.get("prior_findings_remaining") is False
        and isinstance(gates, dict) and required_gates.issubset(gates)
        and all(value is True for value in gates.values())
        and isinstance(source_review, dict) and sha(source_review.get("snapshot"))
        and isinstance(source_review.get("assessments"), dict) and source_review["assessments"]
        and isinstance(actions, list) and actions and isinstance(source_issues, list)
        and sha(receipt.get("source_body_sha256"))
        and receipt.get("source_body_sha256") != receipt.get("body_sha256")
        and receipt.get("body_sha256") == code_only_body_fingerprint(plans)
        and all(fragment_registry_is_intact(plan) for plan in plans)
    ):
        return False
    for action in actions:
        if not isinstance(action, dict):
            return False
        if not (
            isinstance(action.get("issue_ids"), list) and action["issue_ids"]
            and isinstance(action.get("reasons"), list) and action["reasons"]
            and (set(action["reasons"]).issubset(PRUNABLE_REASONS)
                 or (action.get("kind") == "unreadable_residual"
                     and action["reasons"] == ["unreadable_residual"]
                     and action.get("fragment_key") is not None
                     and any(other.get("fragment_key") == action.get("fragment_key")
                             and other.get("replacement_text")
                             and other.get("replacement_source") == "deterministic_action_contract"
                             and set(other.get("issue_ids", ())) & set(action["issue_ids"])
                             for other in actions)))
            and type(action.get("plan_index")) is int and 1 <= action["plan_index"] <= len(plans)
            and action.get("surface") in {"plan_text", "summary"}
            and type(action.get("start")) is int and type(action.get("end")) is int
            and 0 <= action["start"] < action["end"]
            and isinstance(action.get("deleted_text"), str) and action["deleted_text"].strip()
            and isinstance(action.get("replacement_text"), str)
            and sha(action.get("before_sha256")) and sha(action.get("after_sha256"))
            and action.get("before_sha256") != action.get("after_sha256")
        ):
            return False
    try:
        closures = pruning_review_closures(source_review, source_issues, actions, plans)
    except (ValueError, TypeError, KeyError, AttributeError):
        return False
    return closures is not None and receipt.get("closures") == closures


def has_current_pruning_receipt(plans, metrics) -> bool:
    try:
        return _has_current_pruning_receipt(plans, metrics)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        return False
