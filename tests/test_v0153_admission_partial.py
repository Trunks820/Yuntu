from datetime import date, datetime, timezone

import pytest

from src.cost_estimate.public import project_cost_estimate_summary
from src.cost_estimate.resolver import (
    AdmissionCostInput,
    LocalTransportCostInput,
    PlanCostInputs,
    resolve_cost_snapshot,
)
from src.cost_reference.models import MoneyRangeFen
from src.cost_sources.intercity import IntercitySourceBundle
from src.cost_sources.models import CostSourceResult, SourceObservation
from src.export.cost_estimate import cost_estimate_lines


NOW = datetime(2026, 11, 21, tzinfo=timezone.utc)
MISSING = CostSourceResult(adapter_status="missing", resolution="missing")


def _price(identity, fen, *, basis="sourced"):
    return CostSourceResult(
        adapter_status="success",
        resolution=basis,
        selected=SourceObservation(
            source_id=f"fixture:{identity}",
            source_type="fixture",
            price_basis=basis,
            item_identity=identity,
            captured_at=NOW,
            observed_at=NOW,
            range_fen=MoneyRangeFen(min_fen=fen, max_fen=fen),
        ),
    )


PAID = AdmissionCostInput(place_identity="paid", source=_price("paid", 1230))
PARTY = AdmissionCostInput(
    place_identity="party", charging_basis="party_entry", source=_price("party", 1300),
)
FREE = AdmissionCostInput(
    place_identity="free", source=_price("free", 0, basis="policy_zero"),
)
UNPRICED = AdmissionCostInput(place_identity="unpriced", source=MISSING)


def _snapshot(admissions, *, accommodation):
    return resolve_cost_snapshot(PlanCostInputs(
        plan_index=0,
        plan_key="测试路线",
        days=2,
        people_count=3,
        requested_commute_mode="transit",
        from_city_present=False,
        intercity=IntercitySourceBundle(
            from_city=None,
            to_city="重庆",
            outbound_date=date(2026, 12, 1),
            return_date=date(2026, 12, 2),
            status="missing_from_city",
        ),
        accommodation=accommodation,
        local_transport=(LocalTransportCostInput(
            route_identity="fixture:transit", effective_mode="transit", source=MISSING,
        ),),
        admissions=admissions,
    ), estimated_at=NOW)


def _card(snapshot):
    return project_cost_estimate_summary(
        snapshot.model_dump(mode="json"),
        plan_index=0,
        plan_key="测试路线",
        people_count=3,
        days=2,
        from_city_present=False,
        requested_commute_mode="transit",
    )


@pytest.mark.parametrize(
    ("admissions", "known_fen", "counts", "category_text"),
    [
        (
            (PAID, PARTY, FREE, PAID, UNPRICED, UNPRICED),
            (4000, 5000),
            (3, 1),
            "门票：已知 ¥40–¥50，另有 1 处待确认",
        ),
        ((FREE, UNPRICED), (0, 0), (1, 1), "门票：已知 ¥0，另有 1 处待确认"),
        ((UNPRICED,), None, (0, 1), "门票：待确认（费用事实缺失）"),
        ((PAID,), (3000, 4000), (1, 0), "门票：¥30–¥40（来源价格）"),
        ((), (0, 0), (0, 0), "门票：¥0（费用政策计为零）"),
    ],
    ids=["mixed-dedup-rounded-once", "free-and-unknown", "all-unknown", "all-known", "no-stops"],
)
def test_admission_subtotal_and_counts(admissions, known_fen, counts, category_text):
    snapshot = _snapshot(admissions, accommodation=_price("room-night", 2000))
    scenario, = snapshot.scenarios
    admission = next(item for item in scenario.categories if item.category == "admission")
    priced_count, unpriced_count = counts
    expected_known = (
        MoneyRangeFen(min_fen=known_fen[0], max_fen=known_fen[1])
        if known_fen is not None else None
    )
    assert (admission.priced_item_count, admission.unpriced_item_count) == counts
    assert admission.known_range_fen == expected_known
    assert scenario.unpriced_admission_count == unpriced_count
    assert admission.coverage == ("missing" if unpriced_count else "priced")
    assert ("admission" in scenario.missing_categories) == bool(unpriced_count)
    assert scenario.total_scope == "estimated_subset"
    assert scenario.completeness == "partial"
    if unpriced_count:
        assert admission.rounded_party_range_fen is None
        assert scenario.public_reconciled_total_fen == MoneyRangeFen(min_fen=4000, max_fen=4000)
    else:
        assert admission.rounded_party_range_fen == expected_known
        assert (
            scenario.total_range_with_known_admission_fen == scenario.public_reconciled_total_fen
        )
    min_known, max_known = known_fen or (0, 0)
    assert scenario.total_range_with_known_admission_fen == MoneyRangeFen(
        min_fen=4000 + min_known, max_fen=4000 + max_known,
    )

    card = _card(snapshot)
    projected = card.model_dump(mode="json")["scenarios"][0]
    projected_admission = next(
        item for item in projected["categories"] if item["category"] == "admission"
    )
    assert projected_admission["priced_item_count"] == priced_count
    assert projected_admission["unpriced_item_count"] == unpriced_count
    assert ("known_range" in projected_admission) == (known_fen is not None)
    assert "known_range" not in projected["categories"][0]
    lines = cost_estimate_lines(card)
    assert category_text in lines
    assert ("另有 1 处门票未计入" in lines[2]) == bool(unpriced_count)


def test_known_admission_can_be_displayed_when_legacy_total_is_unavailable():
    snapshot = _snapshot((PAID, UNPRICED), accommodation=MISSING)
    card = _card(snapshot)
    scenario, = card.scenarios
    assert card.completeness == scenario.total_scope == "unavailable"
    assert scenario.total_range is None
    assert scenario.total_range_with_known_admission.model_dump() == {
        "min_cny": 30, "max_cny": 40,
    }
    assert "已估 ¥30–¥40" in cost_estimate_lines(card)[2]
