"""W7 regression coverage for the existing Amap taxi fare."""

from datetime import datetime, timezone

import pytest

from src.cost_estimate.models import SnapshotAssumptionV1
from src.cost_estimate.resolver import (
    LocalTransportCostInput,
    PlanCostInputs,
    resolve_cost_snapshot,
)
from src.cost_reference.models import MoneyRangeFen
from src.cost_sources.intercity import IntercitySourceBundle
from src.cost_sources.local_transport import adapt_amap_route_fare
from src.cost_sources.models import CostSourceResult


NOW = datetime(2026, 11, 21, 8, 0, tzinfo=timezone.utc)
TAXI_ASSUMPTION_CODE = "four_travellers_per_taxi"
STANDARD_TAXI_LABEL = "打车默认每辆最多四人，车辆数向上取整"


@pytest.fixture
def taxi_cost_inputs() -> PlanCostInputs:
    route = "route:taxi:1"
    fare = adapt_amap_route_fare(
        mode="driving",
        path={"cost": {"taxi_cost": "12.50"}},
        distance_meters=3_000,
        route_identity=route,
        captured_at=NOW,
    )
    return PlanCostInputs(
        plan_index=0,
        plan_key="taxi-fare-fixture",
        people_count=5,
        days=1,
        requested_commute_mode="driving",
        from_city_present=False,
        intercity=IntercitySourceBundle(
            from_city=None,
            to_city="上海",
            outbound_date=NOW.date(),
            return_date=NOW.date(),
            status="missing_from_city",
        ),
        accommodation=CostSourceResult(
            adapter_status="not_applicable",
            resolution="not_applicable",
        ),
        local_transport=(LocalTransportCostInput(
            route_identity=route,
            effective_mode="driving",
            source=fare,
        ),),
    )


def test_requested_driving_preserves_amap_taxi_fare_and_vehicle_count(
    taxi_cost_inputs: PlanCostInputs,
) -> None:
    snapshot = resolve_cost_snapshot(taxi_cost_inputs, estimated_at=NOW)
    local = next(
        category for category in snapshot.scenarios[0].categories
        if category.category == "local_transport"
    )
    (leg,) = local.line_items
    assert leg.unit_range_fen == MoneyRangeFen(min_fen=1_250, max_fen=1_250)
    assert leg.quantity == 2
    assert leg.party_range_fen == MoneyRangeFen(min_fen=2_500, max_fen=2_500)
    assert local.rounded_party_range_fen == MoneyRangeFen(min_fen=2_000, max_fen=3_000)
    assert [item for item in snapshot.assumptions if item.code == TAXI_ASSUMPTION_CODE] == [
        SnapshotAssumptionV1(
            code=TAXI_ASSUMPTION_CODE,
            label="市内交通按打车估算，每辆最多四人，车辆数向上取整",
            value="vehicles=2",
        ),
    ]


@pytest.mark.parametrize("mode", ["transit", "cycling"])
def test_taxi_note_requires_requested_driving(taxi_cost_inputs: PlanCostInputs, mode: str) -> None:
    inputs = taxi_cost_inputs.model_copy(update={"requested_commute_mode": mode})
    snapshot = resolve_cost_snapshot(inputs, estimated_at=NOW)
    assert [item.label for item in snapshot.assumptions if item.code == TAXI_ASSUMPTION_CODE] == [
        STANDARD_TAXI_LABEL,
    ]


def test_requested_driving_without_a_taxi_fare_has_no_taxi_note(
    taxi_cost_inputs: PlanCostInputs,
) -> None:
    leg = taxi_cost_inputs.local_transport[0].model_copy(update={
        "source": CostSourceResult(adapter_status="missing", resolution="missing"),
    })
    inputs = taxi_cost_inputs.model_copy(update={"local_transport": (leg,)})
    snapshot = resolve_cost_snapshot(inputs, estimated_at=NOW)
    assert [item.label for item in snapshot.assumptions if item.code == TAXI_ASSUMPTION_CODE] == [
        STANDARD_TAXI_LABEL,
    ]
