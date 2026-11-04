import asyncio
from datetime import date
from src.agents.schema import PlanOutput, WorkflowResult
from src.cost_estimate.workflow_integration import _plan_inputs
from src.cost_estimate.resolver import resolve_cost_snapshot
from src.cost_reference.catalog import load_reference_catalog
from src.cost_sources.intercity import IntercitySourceBundle
from test_route_result_v2 import route_fixture, NOW


def inputs_for(anchor=True,city="上海",requested="driving"):
    request,route=route_fixture(anchor)
    request.to_city=city;request.commute_mode=requested
    result=WorkflowResult(trip_request=request,route_plans=[route],plans=[PlanOutput(plan_name="本地验收",plan_text="本地验收",summary="本地验收")])
    bundle=IntercitySourceBundle(from_city=None,to_city=city,outbound_date=date(2026,9,5),return_date=date(2026,9,6),status="missing_from_city")
    return asyncio.run(_plan_inputs(result,plan_index=0,intercity=bundle,catalog=load_reference_catalog(),estimated_at=NOW,meal_attachments={}))


def test_access_and_poi_cost_identities_are_unique_and_reference_is_explicit():
    inputs=inputs_for()
    assert len(inputs.local_transport)==5 # 1 POI leg + 2 access legs per day
    assert len({x.route_identity for x in inputs.local_transport})==5
    accesses=[x for x in inputs.local_transport if ':access:' in x.route_identity]
    assert len(accesses)==4 and all(x.source.resolution in {"reference","missing"} for x in accesses)
    assert all("->" not in x.route_identity for x in accesses)


def test_missing_reference_and_unknown_access_are_not_free():
    inputs=inputs_for(city="无参考测试城")
    accesses=[x for x in inputs.local_transport if ':access:' in x.route_identity]
    assert all(x.source.resolution=="missing" for x in accesses)
    snapshot=resolve_cost_snapshot(inputs,estimated_at=NOW)
    assert snapshot.scenarios[0].completeness != "complete"
    unknown=inputs_for(anchor=False)
    assert len([x for x in unknown.local_transport if ':access:unknown' in x.route_identity])==2
    assert all(x.source.resolution=="missing" for x in unknown.local_transport if ':access:unknown' in x.route_identity)


def test_effective_driving_does_not_add_unrequested_paid_taxi():
    inputs=inputs_for(requested="transit")
    accesses=[x for x in inputs.local_transport if ':access:' in x.route_identity]
    assert all(x.source.resolution=="not_applicable" for x in accesses)


