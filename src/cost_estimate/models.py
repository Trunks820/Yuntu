"""Immutable internal contracts for the v0.9.4 cost snapshot.

These models are deliberately not public result models.  P4 owns the later,
compact Schema 2.0 projection.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from typing import Literal

from pydantic import Field, model_serializer, model_validator

from src.cost_reference.models import FrozenModel, MoneyRangeFen, ReviewStatus


CostCategory = Literal[
    "intercity_transport",
    "accommodation",
    "local_transport",
    "admission",
    "meals",
]
CATEGORY_ORDER: tuple[CostCategory, ...] = (
    "intercity_transport",
    "accommodation",
    "local_transport",
    "admission",
    "meals",
)
ScenarioId = Literal[
    "train_round_trip",
    "flight_round_trip",
    "without_intercity",
]
IntercityMode = Literal["train", "flight"]
Completeness = Literal["complete", "partial", "unavailable"]
Coverage = Literal["priced", "missing"]
AggregatePriceBasis = Literal["sourced", "reference", "mixed", "policy_zero"]
TotalScope = Literal["full_trip", "estimated_subset", "unavailable"]
ChargingUnit = Literal[
    "traveller_round_trip",
    "room_night",
    "vehicle_trip",
    "person_trip",
    "walking_leg",
    "person_entry",
    "party_entry",
    "person_meal",
    "policy_scope",
    "unknown",
]
AssumptionCode = Literal[
    "two_travellers_per_room",
    "itinerary_days_minus_one_nights",
    "four_travellers_per_taxi",
    "adult_full_fare",
    "two_main_meals_per_day",
]
ExclusionCode = Literal["cycling_cost_not_included"]


class SnapshotObservationV1(FrozenModel):
    observation_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_id: str = Field(min_length=1)
    source_type: str = Field(min_length=1)
    price_basis: Literal["sourced", "reference", "policy_zero"]
    item_identity: str = Field(min_length=1)
    captured_at: datetime
    observed_at: datetime | None = None
    reference_version: str | None = None
    effective_date: date | None = None
    provenance: str | None = None
    review_status: ReviewStatus | None = None
    range_fen: MoneyRangeFen

    @model_validator(mode="after")
    def validate_time_and_reference_metadata(self) -> "SnapshotObservationV1":
        if self.captured_at.tzinfo is None:
            raise ValueError("captured_at must be timezone-aware")
        if self.price_basis == "sourced":
            if self.observed_at is None or self.observed_at.tzinfo is None:
                raise ValueError("sourced observation requires observed_at")
        if self.price_basis == "reference" and (
            not self.reference_version
            or self.effective_date is None
            or not self.provenance
            or not self.review_status
        ):
            raise ValueError("reference observation metadata is incomplete")
        if self.price_basis == "policy_zero" and self.range_fen != MoneyRangeFen(
            min_fen=0,
            max_fen=0,
        ):
            raise ValueError("policy_zero must be exactly zero")
        return self


class CostLineItemV1(FrozenModel):
    item_identity: str = Field(min_length=1)
    charging_unit: ChargingUnit
    quantity: int = Field(strict=True, ge=0)
    coverage: Coverage
    unit_range_fen: MoneyRangeFen | None = None
    party_range_fen: MoneyRangeFen | None = None
    price_basis: AggregatePriceBasis | None = None
    observation_ids: tuple[str, ...] = ()
    missing_reason: str = ""

    @model_validator(mode="after")
    def validate_coverage(self) -> "CostLineItemV1":
        has_money = self.unit_range_fen is not None and self.party_range_fen is not None
        if self.coverage == "priced":
            if not has_money or self.price_basis is None:
                raise ValueError("priced line requires ranges and price basis")
            if self.missing_reason:
                raise ValueError("priced line cannot carry a missing reason")
        elif has_money or self.price_basis is not None or self.observation_ids:
            raise ValueError("missing line cannot carry monetary facts")
        return self


class InternalCategoryCostV1(FrozenModel):
    category: CostCategory
    coverage: Coverage
    line_items: tuple[CostLineItemV1, ...]
    unrounded_party_range_fen: MoneyRangeFen | None = None
    rounded_party_range_fen: MoneyRangeFen | None = None
    price_basis: AggregatePriceBasis | None = None
    basis_label: str = Field(min_length=1)
    missing_item_identities: tuple[str, ...] = ()
    known_range_fen: MoneyRangeFen | None = None
    priced_item_count: int | None = Field(default=None, strict=True, ge=0)
    unpriced_item_count: int | None = Field(default=None, strict=True, ge=0)

    @model_serializer(mode="wrap")
    def _omit_empty_admission_fields(self, handler):
        payload = handler(self)
        for field in ("known_range_fen", "priced_item_count", "unpriced_item_count"):
            if payload.get(field) is None:
                payload.pop(field, None)
        return payload

    @model_validator(mode="after")
    def validate_category_coverage(self) -> "InternalCategoryCostV1":
        has_money = (
            self.unrounded_party_range_fen is not None
            and self.rounded_party_range_fen is not None
        )
        if self.coverage == "priced":
            if not has_money or self.price_basis is None:
                raise ValueError("priced category requires ranges and basis")
            if self.missing_item_identities:
                raise ValueError("priced category cannot report missing items")
        elif has_money or self.price_basis is not None:
            raise ValueError("missing category cannot carry a range or basis")
        return self


class InternalCostScenarioV1(FrozenModel):
    scenario_id: ScenarioId
    intercity_mode: IntercityMode | None
    label: str = Field(min_length=1)
    completeness: Completeness
    total_scope: TotalScope
    internal_priced_total_fen: MoneyRangeFen | None = None
    public_reconciled_total_fen: MoneyRangeFen | None = None
    categories: tuple[InternalCategoryCostV1, ...]
    missing_categories: tuple[CostCategory, ...] = ()
    total_range_with_known_admission_fen: MoneyRangeFen | None = None
    unpriced_admission_count: int | None = Field(default=None, strict=True, ge=0)

    @model_serializer(mode="wrap")
    def _omit_empty_admission_fields(self, handler):
        payload = handler(self)
        for field in ("total_range_with_known_admission_fen", "unpriced_admission_count"):
            if payload.get(field) is None:
                payload.pop(field, None)
        return payload

    @model_validator(mode="after")
    def validate_scenario(self) -> "InternalCostScenarioV1":
        if tuple(item.category for item in self.categories) != CATEGORY_ORDER:
            raise ValueError("scenario categories must use the frozen order")
        missing = tuple(
            item.category for item in self.categories if item.coverage == "missing"
        )
        if missing != self.missing_categories:
            raise ValueError("missing_categories must match missing category records")
        priced = [item for item in self.categories if item.coverage == "priced"]
        expected = (
            "complete"
            if not missing
            else "partial"
            if priced
            else "unavailable"
        )
        if self.completeness != expected:
            raise ValueError("scenario completeness disagrees with category coverage")
        expected_scope = {
            "complete": "full_trip",
            "partial": "estimated_subset",
            "unavailable": "unavailable",
        }[expected]
        if self.total_scope != expected_scope:
            raise ValueError("total_scope disagrees with completeness")
        if expected == "unavailable":
            if (
                self.internal_priced_total_fen is not None
                or self.public_reconciled_total_fen is not None
            ):
                raise ValueError("unavailable scenario cannot carry a total")
            return self
        if (
            self.internal_priced_total_fen is None
            or self.public_reconciled_total_fen is None
        ):
            raise ValueError("priced scenario requires internal and public totals")
        visible_min = sum(
            item.rounded_party_range_fen.min_fen
            for item in priced
            if item.rounded_party_range_fen is not None
        )
        visible_max = sum(
            item.rounded_party_range_fen.max_fen
            for item in priced
            if item.rounded_party_range_fen is not None
        )
        if self.public_reconciled_total_fen != MoneyRangeFen(
            min_fen=visible_min,
            max_fen=visible_max,
        ):
            raise ValueError("scenario total must reconcile to visible category ranges")
        return self


class SnapshotAssumptionV1(FrozenModel):
    code: AssumptionCode
    label: str = Field(min_length=1)
    value: str = Field(min_length=1)


class SnapshotExclusionV1(FrozenModel):
    code: ExclusionCode
    label: str = Field(min_length=1)


class SnapshotPlanIdentityV1(FrozenModel):
    plan_index: int = Field(strict=True, ge=0)
    plan_key: str = Field(min_length=1)


class SnapshotNormalizedInputsV1(FrozenModel):
    people_count: int = Field(strict=True, ge=1)
    days: int = Field(strict=True, ge=1)
    from_city_present: bool
    requested_commute_mode: Literal["driving", "transit", "cycling"]


class CostEstimateSnapshotPayloadV1(FrozenModel):
    snapshot_version: Literal["1"] = "1"
    estimated_at: datetime
    currency: Literal["CNY"] = "CNY"
    calculation_unit: Literal["fen"] = "fen"
    completeness: Completeness
    plan_identity: SnapshotPlanIdentityV1
    normalized_inputs: SnapshotNormalizedInputsV1
    observations: tuple[SnapshotObservationV1, ...] = ()
    scenarios: tuple[InternalCostScenarioV1, ...]
    assumptions: tuple[SnapshotAssumptionV1, ...] = ()
    exclusions: tuple[SnapshotExclusionV1, ...] = ()
    diagnostics: dict[str, str | int | bool | None] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_snapshot_payload(self) -> "CostEstimateSnapshotPayloadV1":
        if self.estimated_at.tzinfo is None:
            raise ValueError("estimated_at must be timezone-aware")
        if self.estimated_at.utcoffset() != timedelta(0):
            raise ValueError("estimated_at must use UTC")
        if not self.scenarios:
            raise ValueError("snapshot requires at least one scenario")
        if len({item.scenario_id for item in self.scenarios}) != len(self.scenarios):
            raise ValueError("scenario ids must be unique")
        rank = {"complete": 0, "partial": 1, "unavailable": 2}
        worst = max(self.scenarios, key=lambda item: rank[item.completeness])
        if self.completeness != worst.completeness:
            raise ValueError("plan completeness must equal its worst scenario")
        return self


class CostEstimateSnapshotV1(CostEstimateSnapshotPayloadV1):
    snapshot_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_snapshot_identity(self) -> "CostEstimateSnapshotV1":
        if self.snapshot_id != snapshot_id_for_payload(
            self.model_dump(mode="json", exclude={"snapshot_id"})
        ):
            raise ValueError("snapshot_id does not match canonical snapshot payload")
        return self


def canonical_json_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def snapshot_id_for_payload(payload: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def canonical_snapshot_bytes(snapshot: CostEstimateSnapshotV1) -> bytes:
    return canonical_json_bytes(snapshot.model_dump(mode="json"))


__all__ = [
    "CATEGORY_ORDER",
    "CostCategory",
    "CostEstimateSnapshotPayloadV1",
    "CostEstimateSnapshotV1",
    "CostLineItemV1",
    "InternalCategoryCostV1",
    "InternalCostScenarioV1",
    "SnapshotAssumptionV1",
    "SnapshotExclusionV1",
    "SnapshotNormalizedInputsV1",
    "SnapshotObservationV1",
    "SnapshotPlanIdentityV1",
    "canonical_json_bytes",
    "canonical_snapshot_bytes",
    "snapshot_id_for_payload",
]
