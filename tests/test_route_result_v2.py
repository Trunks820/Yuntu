from datetime import datetime, timezone
from copy import deepcopy
import pytest
from src.agents import route_planning as rp
from src.agents.schema import CandidatePlace, TripRequest
from src.agents.evidence_strength import DeterministicActionContract
from src.agents.safe_plan_renderer import lock_safe_input, render_safe_plans
from src.api.trip_results import _project_plan, ResultContractUnsupported
from src.cost_estimate.resolver import unavailable_cost_snapshot

NOW=datetime(2026,9,5,tzinfo=timezone.utc)


def route_fixture(anchor=True):
    request=TripRequest(to_city="上海",days=2)
    places=[CandidatePlace(place_id=i,name=name,place_type="museum",latitude=31.23,
            longitude=121.47+i*.002,typical_visit_minutes=minutes,typical_visit_source="manual")
            for i,name,minutes in [(1,"长时活动甲",300),(2,"城市活动乙",60),(3,"城市活动丙",60)]]
    groups=[[places[0]],places[1:]] if anchor else [places[:2],places[2:]]
    if not anchor: groups=[places[1:], [places[0],places[1].model_copy(update={"place_id":4,"name":"城市活动丁"})]]
    route=rp._v2_make_plan(groups,request,(31.23,121.47) if anchor else None,"auto_recommended",{})
    return request,route


def raw_plan(route):
    actions=[DeterministicActionContract(plan_index=1,day=day.day,place_id=p.place_id,
             place_name=p.name,authorized_actions=("按既定范围参观游览",)) for day in route.day_groups for p in day.places]
    safe=render_safe_plans(locked_input=lock_safe_input(route_plans=[route],action_contracts=actions))
    plan=safe.plans[0]
    snap=unavailable_cost_snapshot(plan_index=0,plan_key=plan.plan_name,people_count=1,days=2,
          requested_commute_mode="driving",from_city_present=False,estimated_at=NOW,failure_code="offline_fixture")
    raw=plan.model_dump(mode="json")
    raw.update(route_plan=route.model_dump(mode="json"),cost_estimate_snapshot=snap.model_dump(mode="json"))
    return raw


def project(raw, enabled=False):
    return _project_plan(index=0,plan=raw,preferences=[],expected_days=2,people_count=1,
                         from_city_present=False,requested_commute_mode="driving",schema_22_enabled=enabled)


@pytest.mark.parametrize("enabled",[False,True])
def test_mixed_days_project_all_access_without_fake_poi_or_current_flag(enabled):
    _,route=route_fixture()
    raw=raw_plan(route);before=deepcopy(raw)
    result=project(raw,enabled)
    assert result.pace.total_commute_minutes==sum(day.commute_minutes for day in route.day_groups)
    assert len(result.days[0].commute_legs)==0 and len(result.days[1].commute_legs)==1
    assert all("按建议住宿区域估算" in day.commute_summary for day in result.days)
    assert "住宿往返" in raw["plan_text"]
    assert raw==before


def test_old_record_does_not_acquire_access_or_recalculate_cost():
    _,route=route_fixture();raw=raw_plan(route)
    raw["route_plan"].pop("route_policy_version")
    for day in raw["route_plan"]["day_groups"]:
        day.pop("access_legs");day.pop("day_feasibility")
        day["commute_minutes"]=sum(leg["duration_minutes"] for leg in day["commute_legs"])
        day["commute_notes"]=[]
    result=project(raw)
    assert result.pace.total_commute_minutes==sum(leg.duration_minutes for d in result.days for leg in d.commute_legs)
    assert all("住宿往返" not in d.commute_summary for d in result.days)


def test_unknown_anchor_keeps_missing_facts_without_repeated_notice():
    _,route=route_fixture(False)
    raw = raw_plan(route)
    result=project(raw)
    assert "未计入住宿往返" not in raw["plan_text"]
    assert "\n。\n" not in raw["plan_text"]
    assert all("未计入住宿往返" not in d.commute_summary for d in result.days)
    assert all(d.day_feasibility.access_minutes is None for d in route.day_groups)
    assert all(d.day_feasibility.access_status == "unknown" for d in route.day_groups)


@pytest.mark.parametrize("anchor", [False, True])
def test_normal_writer_omits_missing_anchor_notice_and_keeps_known_access(anchor):
    from src.agents.evidence_strength import build_structured_evidence_payload
    from src.agents.final_writer import _assemble_keyed_writer_plan
    from src.agents.schema import RetrievalResult

    _, route = route_fixture(anchor)
    places = [p for day in route.day_groups for p in day.places]
    payload = build_structured_evidence_payload(
        RetrievalResult(city="上海", candidates=places), route_plans=[route])
    assembly = _assemble_keyed_writer_plan(
        plan_index=1,
        raw_fragments=[{"plan_index": 1, "day": day.day, "place_id": p.place_id,
                        "text": "按自己的节奏慢慢参观。"}
                       for day in route.day_groups for p in day.places],
        route_plan=route, structured_evidence_payload=payload,
        composition_blueprint=None, validation_candidate_names=[p.name for p in places],
        weather_advisory_payload=None, accommodation=None)
    assert not assembly.failure_reason
    assert "未计入住宿往返" not in assembly.plan_text
    assert "\n。\n" not in assembly.plan_text
    assert ("住宿往返：去程约" in assembly.plan_text) is anchor


@pytest.mark.parametrize("corruption",["total","poi_endpoint","access_endpoint"])
def test_corrupt_locked_result_fails_closed(corruption):
    _,route=route_fixture();raw=raw_plan(route)
    if corruption=="total":raw["route_plan"]["day_groups"][0]["commute_minutes"]+=1
    elif corruption=="poi_endpoint":raw["route_plan"]["day_groups"][1]["commute_legs"][0]["from_place_id"]=999
    else:raw["route_plan"]["day_groups"][0]["access_legs"][0]["place_id"]=999
    with pytest.raises(ResultContractUnsupported):project(raw)


