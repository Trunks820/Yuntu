"""Deterministic integer-fen resolver for Cost Estimate Snapshot V1."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import Field, model_validator

from src.cost_reference.models import MAX_MONEY_FEN, FrozenModel, MoneyRangeFen
from src.cost_sources.intercity import IntercityRoundTripInput, IntercitySourceBundle
from src.cost_sources.models import CostSourceResult, SourceObservation

from .models import (
    CATEGORY_ORDER,
    CostCategory,
    CostEstimateSnapshotPayloadV1,
    CostEstimateSnapshotV1,
    CostLineItemV1,
    InternalCategoryCostV1,
    InternalCostScenarioV1,
    SnapshotAssumptionV1,
    SnapshotExclusionV1,
    SnapshotNormalizedInputsV1,
    SnapshotObservationV1,
    SnapshotPlanIdentityV1,
    snapshot_id_for_payload,
)


ROUNDING_STEP_FEN = 1_000
_MODE_ORDER = {"train": 0, "flight": 1}
_COMPLETENESS_RANK = {"complete": 0, "partial": 1, "unavailable": 2}
_BASIS_LABELS = {
    "sourced": "来源价格",
    "reference": "版本化参考估算",
    "mixed": "来源价格与版本化参考估算",
    "policy_zero": "费用政策计为零",
}


class LocalTransportCostInput(FrozenModel):
    route_identity: str = Field(min_length=1)
    effective_mode: Literal["driving", "transit", "walking", "cycling"]
    source: CostSourceResult


class AdmissionCostInput(FrozenModel):
    place_identity: str = Field(min_length=1)
    charging_basis: Literal["person_entry", "party_entry"] = "person_entry"
    source: CostSourceResult


class MealCostInput(FrozenModel):
    day: int = Field(strict=True, ge=1)
    meal_slot: Literal["lunch", "dinner"]
    source: CostSourceResult


class PlanCostInputs(FrozenModel):
    plan_index: int = Field(strict=True, ge=0)
    plan_key: str = Field(min_length=1)
    people_count: int = Field(strict=True, ge=1)
    days: int = Field(strict=True, ge=1)
    requested_commute_mode: Literal["driving", "transit", "cycling"]
    from_city_present: bool
    intercity: IntercitySourceBundle
    intercity_failure_code: str | None = None
    accommodation: CostSourceResult
    local_transport: tuple[LocalTransportCostInput, ...] = ()
    admissions: tuple[AdmissionCostInput, ...] = ()
    meals: tuple[MealCostInput, ...] = ()

    @model_validator(mode="after")
    def validate_intercity_origin_state(self) -> "PlanCostInputs":
        if not self.from_city_present and self.intercity.status != "missing_from_city":
            raise ValueError("missing origin requires missing_from_city source state")
        return self


def checked_add(left: int, right: int) -> int:
    if isinstance(left, bool) or isinstance(right, bool) or left < 0 or right < 0:
        raise ValueError("money arithmetic accepts non-negative integers only")
    total = left + right
    if total > MAX_MONEY_FEN:
        raise OverflowError("money addition exceeds signed 64-bit fen")
    return total


def checked_multiply(value: int, quantity: int) -> int:
    if (
        isinstance(value, bool)
        or isinstance(quantity, bool)
        or value < 0
        or quantity < 0
    ):
        raise ValueError("money multiplication accepts non-negative integers only")
    if value and quantity > MAX_MONEY_FEN // value:
        raise OverflowError("money multiplication exceeds signed 64-bit fen")
    return value * quantity


def add_ranges(*ranges: MoneyRangeFen) -> MoneyRangeFen:
    minimum = 0
    maximum = 0
    for item in ranges:
        minimum = checked_add(minimum, item.min_fen)
        maximum = checked_add(maximum, item.max_fen)
    return MoneyRangeFen(min_fen=minimum, max_fen=maximum)


def multiply_range(value: MoneyRangeFen, quantity: int) -> MoneyRangeFen:
    return MoneyRangeFen(
        min_fen=checked_multiply(value.min_fen, quantity),
        max_fen=checked_multiply(value.max_fen, quantity),
    )


def round_outward_to_cny10(value: MoneyRangeFen) -> MoneyRangeFen:
    minimum = value.min_fen // ROUNDING_STEP_FEN * ROUNDING_STEP_FEN
    if value.max_fen > MAX_MONEY_FEN - (ROUNDING_STEP_FEN - 1):
        raise OverflowError("outward rounding exceeds signed 64-bit fen")
    maximum = (
        (value.max_fen + ROUNDING_STEP_FEN - 1)
        // ROUNDING_STEP_FEN
        * ROUNDING_STEP_FEN
    )
    if maximum > MAX_MONEY_FEN:
        raise OverflowError("outward rounding exceeds signed 64-bit fen")
    return MoneyRangeFen(min_fen=minimum, max_fen=maximum)


def _aggregate_basis(values: list[str]) -> str:
    nonzero = {value for value in values if value != "policy_zero"}
    if len(nonzero) > 1:
        return "mixed"
    if len(nonzero) == 1:
        return next(iter(nonzero))
    return "policy_zero"


def _observation_payload(source: SourceObservation) -> dict:
    return source.model_dump(mode="json")


def _snapshot_observation(source: SourceObservation) -> SnapshotObservationV1:
    payload = _observation_payload(source)
    observation_id = snapshot_id_for_payload(payload)
    return SnapshotObservationV1(
        observation_id=observation_id,
        **payload,
    )


class _ObservationRegistry:
    def __init__(self) -> None:
        self._items: dict[str, SnapshotObservationV1] = {}

    def add(self, source: SourceObservation) -> str:
        item = _snapshot_observation(source)
        self._items[item.observation_id] = item
        return item.observation_id

    def values(self) -> tuple[SnapshotObservationV1, ...]:
        return tuple(self._items[key] for key in sorted(self._items))


def _missing_line(
    identity: str,
    *,
    charging_unit: str,
    reason: str,
) -> CostLineItemV1:
    return CostLineItemV1(
        item_identity=identity,
        charging_unit=charging_unit,
        quantity=0,
        coverage="missing",
        missing_reason=reason or "cost fact is unavailable",
    )


def _priced_line(
    identity: str,
    *,
    charging_unit: str,
    quantity: int,
    unit_range: MoneyRangeFen,
    basis: str,
    observation_ids: tuple[str, ...],
) -> CostLineItemV1:
    return CostLineItemV1(
        item_identity=identity,
        charging_unit=charging_unit,
        quantity=quantity,
        coverage="priced",
        unit_range_fen=unit_range,
        party_range_fen=multiply_range(unit_range, quantity),
        price_basis=basis,
        observation_ids=observation_ids,
    )


def _zero_line(
    identity: str,
    *,
    charging_unit: str,
    quantity: int = 1,
) -> CostLineItemV1:
    zero = MoneyRangeFen(min_fen=0, max_fen=0)
    return _priced_line(
        identity,
        charging_unit=charging_unit,
        quantity=quantity,
        unit_range=zero,
        basis="policy_zero",
        observation_ids=(),
    )


def _category(
    category: CostCategory,
    line_items: list[CostLineItemV1],
    *,
    basis_label: str | None = None,
) -> InternalCategoryCostV1:
    ordered = tuple(line_items)
    missing = tuple(
        item.item_identity for item in ordered if item.coverage == "missing"
    )
    if missing:
        return InternalCategoryCostV1(
            category=category,
            coverage="missing",
            line_items=ordered,
            basis_label=basis_label or "费用事实缺失",
            missing_item_identities=missing,
        )
    ranges = [
        item.party_range_fen
        for item in ordered
        if item.party_range_fen is not None
    ]
    unrounded = add_ranges(*ranges)
    rounded = round_outward_to_cny10(unrounded)
    basis = _aggregate_basis([
        str(item.price_basis)
        for item in ordered
        if item.price_basis is not None
    ])
    return InternalCategoryCostV1(
        category=category,
        coverage="priced",
        line_items=ordered,
        unrounded_party_range_fen=unrounded,
        rounded_party_range_fen=rounded,
        price_basis=basis,
        basis_label=basis_label or _BASIS_LABELS[basis],
    )


def _source_line(
    identity: str,
    *,
    charging_unit: str,
    quantity: int,
    source: CostSourceResult,
    registry: _ObservationRegistry,
) -> CostLineItemV1:
    observation = source.selected
    if observation is None:
        return _missing_line(
            identity,
            charging_unit=charging_unit,
            reason=source.reason,
        )
    observation_id = registry.add(observation)
    return _priced_line(
        identity,
        charging_unit=charging_unit,
        quantity=quantity,
        unit_range=observation.range_fen,
        basis=observation.price_basis,
        observation_ids=(observation_id,),
    )


def _intercity_category(
    round_trip: IntercityRoundTripInput | None,
    *,
    mode: str | None,
    people_count: int,
    registry: _ObservationRegistry,
) -> InternalCategoryCostV1:
    if round_trip is None:
        return _category("intercity_transport", [
            _missing_line(
                f"intercity:{mode or 'without_intercity'}",
                charging_unit="traveller_round_trip",
                reason="round-trip intercity facts are unavailable",
            )
        ])
    outbound = round_trip.outbound.observation
    return_leg = round_trip.return_leg.observation
    if outbound is None or return_leg is None:
        return _category("intercity_transport", [
            _missing_line(
                f"intercity:{round_trip.mode}:round_trip",
                charging_unit="traveller_round_trip",
                reason=(
                    round_trip.outbound.reason
                    or round_trip.return_leg.reason
                    or "one intercity direction is unavailable"
                ),
            )
        ])
    unit_range = add_ranges(outbound.range_fen, return_leg.range_fen)
    observation_ids = (registry.add(outbound), registry.add(return_leg))
    return _category("intercity_transport", [
        _priced_line(
            f"intercity:{round_trip.mode}:round_trip",
            charging_unit="traveller_round_trip",
            quantity=people_count,
            unit_range=unit_range,
            basis=_aggregate_basis([
                outbound.price_basis,
                return_leg.price_basis,
            ]),
            observation_ids=observation_ids,
        )
    ])


def _accommodation_category(
    inputs: PlanCostInputs,
    registry: _ObservationRegistry,
) -> tuple[InternalCategoryCostV1, int, int]:
    rooms = max((inputs.people_count + 1) // 2, 1)
    nights = max(inputs.days - 1, 0)
    if nights == 0:
        return (
            _category("accommodation", [
                _zero_line(
                    "accommodation:no-room-nights",
                    charging_unit="room_night",
                    quantity=0,
                )
            ]),
            rooms,
            nights,
        )
    quantity = checked_multiply(rooms, nights)
    return (
        _category("accommodation", [
            _source_line(
                "accommodation:room_nights",
                charging_unit="room_night",
                quantity=quantity,
                source=inputs.accommodation,
                registry=registry,
            )
        ], basis_label=inputs.accommodation.basis_label),
        rooms,
        nights,
    )


def _local_transport_category(
    inputs: PlanCostInputs,
    registry: _ObservationRegistry,
) -> tuple[InternalCategoryCostV1, int, bool]:
    vehicles = max((inputs.people_count + 3) // 4, 1)
    cycling_present = False
    lines: list[CostLineItemV1] = []
    for item in sorted(inputs.local_transport, key=lambda value: value.route_identity):
        if item.effective_mode == "cycling":
            cycling_present = True
            continue
        if item.effective_mode == "walking":
            lines.append(_zero_line(
                item.route_identity,
                charging_unit="walking_leg",
            ))
            continue
        if item.source.resolution == "not_applicable":
            continue
        quantity = vehicles if item.effective_mode == "driving" else inputs.people_count
        unit = "vehicle_trip" if item.effective_mode == "driving" else "person_trip"
        lines.append(_source_line(
            item.route_identity,
            charging_unit=unit,
            quantity=quantity,
            source=item.source,
            registry=registry,
        ))
    if not lines:
        lines.append(_zero_line(
            "local-transport:no-counted-fare",
            charging_unit="policy_scope",
            quantity=0,
        ))
    return _category("local_transport", lines), vehicles, cycling_present


def _admission_category(
    inputs: PlanCostInputs,
    registry: _ObservationRegistry,
) -> InternalCategoryCostV1:
    unique: dict[str, AdmissionCostInput] = {}
    for item in inputs.admissions:
        previous = unique.get(item.place_identity)
        if previous is not None and previous != item:
            raise ValueError("conflicting duplicate admission identity")
        unique[item.place_identity] = item
    lines: list[CostLineItemV1] = []
    for identity in sorted(unique):
        item = unique[identity]
        quantity = inputs.people_count if item.charging_basis == "person_entry" else 1
        lines.append(_source_line(
            identity,
            charging_unit=item.charging_basis,
            quantity=quantity,
            source=item.source,
            registry=registry,
        ))
    if not lines:
        lines.append(_zero_line(
            "admission:no-locked-attractions",
            charging_unit="policy_scope",
            quantity=0,
        ))
    category = _category("admission", lines)
    unpriced_count = len(category.missing_item_identities)
    known_range = category.rounded_party_range_fen
    if unpriced_count:
        known_ranges = [
            item.party_range_fen for item in lines if item.party_range_fen is not None
        ]
        if known_ranges:
            known_range = round_outward_to_cny10(add_ranges(*known_ranges))
    return category.model_copy(update={
        "known_range_fen": known_range,
        "priced_item_count": len(unique) - unpriced_count,
        "unpriced_item_count": unpriced_count,
    })


def _meals_category(
    inputs: PlanCostInputs,
    registry: _ObservationRegistry,
) -> InternalCategoryCostV1:
    provided: dict[tuple[int, str], MealCostInput] = {}
    for item in inputs.meals:
        if item.day > inputs.days:
            raise ValueError("meal day exceeds itinerary length")
        key = (item.day, item.meal_slot)
        if key in provided:
            raise ValueError("duplicate meal slot")
        provided[key] = item
    lines: list[CostLineItemV1] = []
    for day in range(1, inputs.days + 1):
        for slot in ("lunch", "dinner"):
            identity = f"meal:day:{day}:{slot}"
            item = provided.get((day, slot))
            if item is None:
                lines.append(_missing_line(
                    identity,
                    charging_unit="person_meal",
                    reason="planned meal slot is missing",
                ))
                continue
            lines.append(_source_line(
                identity,
                charging_unit="person_meal",
                quantity=inputs.people_count,
                source=item.source,
                registry=registry,
            ))
    return _category("meals", lines)


def _scenario(
    *,
    scenario_id: str,
    mode: str | None,
    label: str,
    categories: tuple[InternalCategoryCostV1, ...],
) -> InternalCostScenarioV1:
    priced = [item for item in categories if item.coverage == "priced"]
    missing = tuple(
        item.category for item in categories if item.coverage == "missing"
    )
    completeness = (
        "complete"
        if not missing
        else "partial"
        if priced
        else "unavailable"
    )
    total_scope = {
        "complete": "full_trip",
        "partial": "estimated_subset",
        "unavailable": "unavailable",
    }[completeness]
    internal_total = None
    public_total = None
    if priced:
        internal_total = add_ranges(*[
            item.unrounded_party_range_fen
            for item in priced
            if item.unrounded_party_range_fen is not None
        ])
        public_total = add_ranges(*[
            item.rounded_party_range_fen
            for item in priced
            if item.rounded_party_range_fen is not None
        ])
    admission = next(item for item in categories if item.category == "admission")
    total_with_known_admission = None
    if admission.unpriced_item_count is not None:
        total_with_known_admission = public_total
        if admission.coverage == "missing" and admission.known_range_fen is not None:
            total_with_known_admission = (
                add_ranges(public_total, admission.known_range_fen)
                if public_total is not None else admission.known_range_fen
            )
    return InternalCostScenarioV1(
        scenario_id=scenario_id,
        intercity_mode=mode,
        label=label,
        completeness=completeness,
        total_scope=total_scope,
        internal_priced_total_fen=internal_total,
        public_reconciled_total_fen=public_total,
        categories=categories,
        missing_categories=missing,
        total_range_with_known_admission_fen=total_with_known_admission,
        unpriced_admission_count=admission.unpriced_item_count,
    )


def _snapshot_from_payload(payload: dict) -> CostEstimateSnapshotV1:
    normalized = CostEstimateSnapshotPayloadV1.model_validate(payload)
    normalized_payload = normalized.model_dump(mode="json")
    snapshot_id = snapshot_id_for_payload(normalized_payload)
    return CostEstimateSnapshotV1(snapshot_id=snapshot_id, **normalized_payload)


def resolve_cost_snapshot(
    inputs: PlanCostInputs,
    *,
    estimated_at: datetime | None = None,
) -> CostEstimateSnapshotV1:
    completed_at = estimated_at or datetime.now(timezone.utc)
    if completed_at.tzinfo is None:
        raise ValueError("estimated_at must be timezone-aware")
    completed_at = completed_at.astimezone(timezone.utc)
    registry = _ObservationRegistry()
    accommodation, rooms, nights = _accommodation_category(inputs, registry)
    local_transport, vehicles, cycling_present = _local_transport_category(
        inputs,
        registry,
    )
    admission = _admission_category(inputs, registry)
    meals = _meals_category(inputs, registry)
    shared = {
        "accommodation": accommodation,
        "local_transport": local_transport,
        "admission": admission,
        "meals": meals,
    }

    scenarios: list[InternalCostScenarioV1] = []
    if not inputs.from_city_present:
        categories = (
            _intercity_category(
                None,
                mode=None,
                people_count=inputs.people_count,
                registry=registry,
            ),
            *(shared[category] for category in CATEGORY_ORDER[1:]),
        )
        scenarios.append(_scenario(
            scenario_id="without_intercity",
            mode=None,
            label="未填出发城市，所以不含往返交通",
            categories=categories,
        ))
    else:
        round_trips: dict[str, IntercityRoundTripInput] = {}
        for item in inputs.intercity.round_trips:
            if item.mode in round_trips:
                raise ValueError("duplicate intercity mode")
            round_trips[item.mode] = item
        for mode in sorted(round_trips, key=lambda value: _MODE_ORDER[value]):
            round_trip = round_trips[mode]
            categories = (
                _intercity_category(
                    round_trip,
                    mode=mode,
                    people_count=inputs.people_count,
                    registry=registry,
                ),
                *(shared[category] for category in CATEGORY_ORDER[1:]),
            )
            if mode == "flight":
                label = "飞机往返"
            elif round_trip.outbound.train_class == "ordinary":
                label = "普通火车往返"
            else:
                label = "高铁往返"
            scenarios.append(_scenario(
                scenario_id=f"{mode}_round_trip",
                mode=mode,
                label=label,
                categories=categories,
            ))
        if not scenarios:
            categories = (
                _intercity_category(
                    None,
                    mode=None,
                    people_count=inputs.people_count,
                    registry=registry,
                ),
                *(shared[category] for category in CATEGORY_ORDER[1:]),
            )
            scenarios.append(_scenario(
                scenario_id="without_intercity",
                mode=None,
                label=(
                    "没查到直达高铁或直飞航班，所以不含往返交通"
                    if inputs.intercity.status == "no_direct_service"
                    and not inputs.intercity_failure_code
                    else "大交通费用待确认"
                ),
                categories=categories,
            ))

    completeness = max(
        scenarios,
        key=lambda item: _COMPLETENESS_RANK[item.completeness],
    ).completeness
    taxi_assumption_label = "打车默认每辆最多四人，车辆数向上取整"
    if inputs.requested_commute_mode == "driving" and any(
        line.charging_unit == "vehicle_trip" and line.price_basis in {"sourced", "reference"}
        for line in local_transport.line_items
    ):
        taxi_assumption_label = "市内交通按打车估算，每辆最多四人，车辆数向上取整"
    assumptions = (
        SnapshotAssumptionV1(
            code="two_travellers_per_room",
            label="默认每间房入住两人，房间数向上取整",
            value=f"rooms={rooms}",
        ),
        SnapshotAssumptionV1(
            code="itinerary_days_minus_one_nights",
            label="住宿晚数按行程天数减一计算",
            value=f"nights={nights}",
        ),
        SnapshotAssumptionV1(
            code="four_travellers_per_taxi",
            label=taxi_assumption_label,
            value=f"vehicles={vehicles}",
        ),
        SnapshotAssumptionV1(
            code="adult_full_fare",
            label="公交、门票与主餐按成人全价计算",
            value=f"travellers={inputs.people_count}",
        ),
        SnapshotAssumptionV1(
            code="two_main_meals_per_day",
            label="每个行程日计入午餐和晚餐各一次，不含早餐和小吃",
            value=f"meal_slots={inputs.days * 2}",
        ),
    )
    exclusions = (
        (
            SnapshotExclusionV1(
                code="cycling_cost_not_included",
                label="骑行费用暂未计入",
            ),
        )
        if cycling_present
        else ()
    )
    payload = {
        "snapshot_version": "1",
        "estimated_at": completed_at,
        "currency": "CNY",
        "calculation_unit": "fen",
        "completeness": completeness,
        "plan_identity": SnapshotPlanIdentityV1(
            plan_index=inputs.plan_index,
            plan_key=inputs.plan_key,
        ),
        "normalized_inputs": SnapshotNormalizedInputsV1(
            people_count=inputs.people_count,
            days=inputs.days,
            from_city_present=inputs.from_city_present,
            requested_commute_mode=inputs.requested_commute_mode,
        ),
        "observations": registry.values(),
        "scenarios": tuple(scenarios),
        "assumptions": assumptions,
        "exclusions": exclusions,
        "diagnostics": {
            "category_rounding_step_fen": ROUNDING_STEP_FEN,
            "scenario_selection": "user_choice_not_array_order",
            "intercity_source_status": inputs.intercity.status,
            "intercity_failure_code": inputs.intercity_failure_code,
        },
    }
    return _snapshot_from_payload(payload)


def unavailable_cost_snapshot(
    *,
    plan_index: int,
    plan_key: str,
    people_count: int,
    days: int,
    requested_commute_mode: Literal["driving", "transit", "cycling"],
    from_city_present: bool,
    estimated_at: datetime,
    failure_code: str,
) -> CostEstimateSnapshotV1:
    categories = tuple(
        InternalCategoryCostV1(
            category=category,
            coverage="missing",
            line_items=(_missing_line(
                f"resolver-unavailable:{category}",
                charging_unit="unknown",
                reason="cost resolver unavailable",
            ),),
            basis_label="费用事实缺失",
            missing_item_identities=(f"resolver-unavailable:{category}",),
        )
        for category in CATEGORY_ORDER
    )
    scenarios = (_scenario(
        scenario_id="without_intercity",
        mode=None,
        label="费用暂不可估算",
        categories=categories,
    ),)
    payload = {
        "snapshot_version": "1",
        "estimated_at": estimated_at,
        "currency": "CNY",
        "calculation_unit": "fen",
        "completeness": "unavailable",
        "plan_identity": SnapshotPlanIdentityV1(
            plan_index=plan_index,
            plan_key=plan_key,
        ).model_dump(mode="json"),
        "normalized_inputs": SnapshotNormalizedInputsV1(
            people_count=max(people_count, 1),
            days=max(days, 1),
            from_city_present=from_city_present,
            requested_commute_mode=requested_commute_mode,
        ).model_dump(mode="json"),
        "observations": [],
        "scenarios": [scenario.model_dump(mode="json") for scenario in scenarios],
        "assumptions": [],
        "exclusions": [],
        "diagnostics": {"failure_code": failure_code},
    }
    return _snapshot_from_payload(payload)


__all__ = [
    "AdmissionCostInput",
    "LocalTransportCostInput",
    "MealCostInput",
    "PlanCostInputs",
    "add_ranges",
    "checked_add",
    "checked_multiply",
    "multiply_range",
    "resolve_cost_snapshot",
    "round_outward_to_cny10",
    "unavailable_cost_snapshot",
]
