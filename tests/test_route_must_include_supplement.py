from __future__ import annotations

import pytest

from src.agents import route_planning as rp
from src.agents.schema import CandidatePlace, PoiSelectionItem, PoiSelectionResult, TripRequest


def case():
    must = CandidatePlace(place_id=1, name="远郊必选长城", place_type="ATTRACTION", must_include=True, latitude=40.35, longitude=116.02, adcode="110119", typical_visit_minutes=120)
    urban = [CandidatePlace(place_id=i, name=f"市区景点{i}", place_type="ATTRACTION", latitude=39.9, longitude=116.4 + i * 0.001, adcode="110101", typical_visit_minutes=60) for i in range(2, 7)]
    nearby = CandidatePlace(place_id=7, name="附近博物馆", place_type="MUSEUM", latitude=40.345, longitude=116.025, adcode="110119", typical_visit_minutes=60)
    request = TripRequest(to_city="北京", days=2, must_include=[{"place_id": 1, "name": must.name}], preferences=["适中"])
    selection = PoiSelectionResult(selected=[PoiSelectionItem(place_id=i) for i in range(1, 7)])
    return request, selection, must, urban, nearby


def test_required_remote_day_gets_qualified_partner_before_conflict():
    request, selection, must, urban, nearby = case()
    plan = rp.build_selected_route_plan(request=request, selection=selection, qualified_pool=[must, *urban, nearby])
    assert len(plan.day_groups) == 2
    day = next(d for d in plan.day_groups if any(p.place_id == 1 for p in d.places))
    assert {p.place_id for p in day.places} == {1, 7}
    assert [(p.place_id, p.reason) for p in plan.membership_ledger.supplemented] == [(7, "FILL_MUST_INCLUDE_DAY")]
    assert {p.place_id for p in plan.membership_ledger.selected if p.status == "USED"}.issubset(set(range(1, 7)))
    metrics = rp.RoutePlanningMetrics()
    metrics.record_membership_ledger(plan.membership_ledger)
    assert metrics.supplement_reason_counts == {"FILL_MUST_INCLUDE_DAY": 1}


@pytest.mark.parametrize("unusable", ["missing", "far", "blocked", "visit_budget", "avoid"])
def test_missing_or_unusable_partner_cannot_silently_drop_required_place(unusable):
    request, selection, must, urban, nearby = case()
    kwargs = {}
    if unusable == "far":
        nearby.latitude, nearby.longitude = 39.9, 116.4
    elif unusable == "blocked":
        kwargs["blocked_supplement_ids"] = {7}
    elif unusable == "visit_budget":
        nearby.typical_visit_minutes = 1000
    elif unusable == "avoid":
        request.avoid = [nearby.name]
    pool = [must, *urban] + ([] if unusable == "missing" else [nearby])
    with pytest.raises(rp.RouteMustIncludeConflictError):
        rp.build_selected_route_plan(request=request, selection=selection, qualified_pool=pool, **kwargs)


def test_existing_selected_partner_needs_no_supplement():
    request, selection, must, urban, nearby = case()
    selection.selected.append(PoiSelectionItem(place_id=7))
    plan = rp.build_selected_route_plan(request=request, selection=selection, qualified_pool=[must, *urban, nearby])
    assert plan.membership_ledger.supplemented == []
    assert {1, 7}.issubset(rp.route_plan_place_ids(plan))


