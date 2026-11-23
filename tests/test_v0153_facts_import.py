import asyncio
from datetime import datetime, timezone

import pytest

from src.agents.schema import CandidatePlace, PlanOutput, RoutePlan, TripRequest, WorkflowResult
from src.cost_estimate.public import project_cost_estimate_summary
from src.cost_estimate.workflow_integration import _route_admissions, resolve_workflow_cost_snapshots
from src.cost_reference import catalog as reference_catalog
from src.cost_reference.models import MoneyRangeFen
from src.cost_sources.meals import resolve_meal_source
from src.export.cost_estimate import cost_estimate_lines


NOW = datetime(2026, 11, 21, tzinfo=timezone.utc)
CATALOG = reference_catalog.load_reference_catalog()


def _route(*names):
    return RoutePlan.model_validate({
        "label": "测试路线",
        "day_groups": [{
            "day": 1,
            "places": [
                CandidatePlace(place_id=index, name=name, place_type="attraction")
                for index, name in enumerate(names, start=1)
            ],
        }],
    })


def _admissions(city, *names, estimated_at=NOW):
    return _route_admissions(_route(*names), city=city, catalog=CATALOG, estimated_at=estimated_at)


def test_ticket_lookup_is_exact_and_expires_before_legacy_fallback():
    alias = reference_catalog.lookup_verified_admission(city="北京", place_label="颐和园")
    assert alias.name == "颐和园（公园大门）"
    assert reference_catalog.lookup_verified_admission(city="西安", place_label="颐和园") is None
    assert reference_catalog.lookup_verified_admission(city="北京", place_label="正阳钟楼") is None

    ticket, = _admissions(
        "北京", "颐和园", estimated_at=datetime(2027, 2, 20, 23, 59, tzinfo=timezone.utc),
    )
    assert ticket.source.resolution == "sourced"
    assert ticket.source.selected.source_type == "verified_ticket_facts"
    assert ticket.source.selected.observed_at == NOW
    assert ticket.source.selected.source_id == str(alias.sources[0].url)
    assert ticket.source.selected.range_fen == MoneyRangeFen(min_fen=2000, max_fen=3000)

    expired, = _admissions(
        "北京", "颐和园", estimated_at=datetime(2027, 2, 21, tzinfo=timezone.utc),
    )
    assert expired.source.resolution == "missing"


def test_shared_ticket_and_free_place_reach_partial_cost_card():
    route = _route("石象路", "明孝陵", "测试未知景点")
    shared, unknown = _route_admissions(route, city="南京", catalog=CATALOG, estimated_at=NOW)
    assert shared.place_identity == "canonical-place:2"
    assert shared.source.reason == "石象路已含在明孝陵门票中"
    assert unknown.source.resolution == "missing"
    solo, = _admissions("南京", "石象路")
    assert solo.source.selected.range_fen == MoneyRangeFen(min_fen=7000, max_fen=7000)
    expired, = _admissions(
        "南京", "石象路", "明孝陵", estimated_at=datetime(2027, 2, 21, tzinfo=timezone.utc),
    )
    assert expired.source.resolution == "missing"

    free, = _admissions("重庆", "山城巷")
    assert free.source.resolution == "policy_zero"
    assert free.source.selected.range_fen == MoneyRangeFen(min_fen=0, max_fen=0)
    assert free.source.selected.observed_at == NOW

    request = TripRequest(to_city="南京", days=1, people_count=2)
    result = WorkflowResult(
        trip_request=request,
        route_plans=[route, route],
        plans=[PlanOutput(plan_name=name, plan_text="路线", summary="路线") for name in ("A", "B")],
    )
    snapshots = asyncio.run(resolve_workflow_cost_snapshots(result, catalog=CATALOG, estimated_at=NOW))
    for index, snapshot in enumerate(snapshots):
        summary = project_cost_estimate_summary(
            snapshot.model_dump(mode="json"),
            plan_index=index,
            plan_key=result.plans[index].plan_name,
            people_count=2,
            days=1,
            from_city_present=False,
            requested_commute_mode=request.commute_mode,
        )
        scenario, = summary.scenarios
        admission = next(category for category in scenario.categories if category.category == "admission")
        assert (admission.priced_item_count, admission.unpriced_item_count) == (1, 1)
        lines = cost_estimate_lines(summary)
        assert "门票：已知 ¥140，另有 1 处待确认" in lines
        assert lines[2].endswith("另有 1 处门票未计入")


def test_children_with_same_ticket_key_need_no_parent_stop(monkeypatch):
    facts = reference_catalog.load_verified_facts()
    child = reference_catalog.lookup_verified_admission(city="南京", place_label="石象路")
    sibling = child.model_copy(update={"name": "另一个园内景点", "aliases": ()})
    monkeypatch.setattr(
        reference_catalog, "load_verified_facts",
        lambda: facts.model_copy(update={"admission": (*facts.admission, sibling)}),
    )
    shared, = _admissions("南京", "石象路", "另一个园内景点")
    assert shared.source.selected.item_identity == "admission:ticket:南京:明孝陵"
    assert shared.source.reason == "另一个园内景点已含在明孝陵门票中"


@pytest.mark.parametrize("slot", ["lunch", "dinner"])
def test_meal_percentiles_apply_only_as_city_fallback(slot):
    meal = resolve_meal_source(CATALOG, city="北京", meal_slot=slot, attachment=None, captured_at=NOW)
    assert meal.resolution == "reference"
    assert meal.selected.source_type == "verified_city_meal_facts"
    assert meal.selected.range_fen == MoneyRangeFen(min_fen=6400, max_fen=12700)


@pytest.mark.parametrize("payload", [None, b"{", b'{"admission": []}'])
def test_unavailable_facts_use_legacy_catalog(tmp_path, monkeypatch, payload):
    path = tmp_path / "facts.json"
    if payload is not None:
        path.write_bytes(payload)
    monkeypatch.setattr(reference_catalog, "VERIFIED_FACTS_PATH", path)
    reference_catalog.load_verified_facts.cache_clear()
    try:
        assert reference_catalog.load_verified_facts() is None
        ticket, = _admissions("北京", "颐和园")
        assert ticket.source.selected.source_type == "versioned_reference_catalog"
        meal = resolve_meal_source(
            CATALOG, city="北京", meal_slot="lunch", attachment=None, captured_at=NOW,
        )
        assert meal.selected.source_type == "versioned_reference_catalog"
        assert meal.selected.range_fen == MoneyRangeFen(min_fen=2400, max_fen=5000)
    finally:
        reference_catalog.load_verified_facts.cache_clear()
