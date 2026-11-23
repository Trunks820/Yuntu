"""Strict Schema 2.0 public projection for persisted cost snapshots.

The internal snapshot remains the sole monetary fact.  These models deliberately
exclude observations, provider/reference internals, fen arithmetic, snapshot
identity and diagnostics.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import Field, ValidationError, model_serializer, model_validator

from src.cost_estimate.models import (
    CATEGORY_ORDER,
    AggregatePriceBasis,
    AssumptionCode,
    Completeness,
    CostCategory,
    CostEstimateSnapshotV1,
    Coverage,
    ExclusionCode,
    IntercityMode,
    ScenarioId,
    TotalScope,
)
from src.cost_reference.models import FrozenModel, MoneyRangeFen


PUBLIC_COST_NOTICE = "费用为规划参考，实际支付金额请以预订或现场结算为准"
MoneyCny = Annotated[int, Field(strict=True, ge=0, multiple_of=10)]


class CostEstimateProjectionError(ValueError):
    """A persisted snapshot cannot satisfy the public Schema 2.0 contract."""


class PublicMoneyRangeCny(FrozenModel):
    min_cny: MoneyCny
    max_cny: MoneyCny

    @model_validator(mode="after")
    def validate_order(self) -> "PublicMoneyRangeCny":
        if self.min_cny > self.max_cny:
            raise ValueError("min_cny must be <= max_cny")
        return self


class CostCategorySummary(FrozenModel):
    category: CostCategory
    coverage: Coverage
    range: PublicMoneyRangeCny | None = None
    price_basis: AggregatePriceBasis | None = None
    basis_label: str = Field(min_length=1)
    known_range: PublicMoneyRangeCny | None = None
    priced_item_count: int | None = Field(default=None, strict=True, ge=0)
    unpriced_item_count: int | None = Field(default=None, strict=True, ge=0)

    @model_serializer(mode="wrap")
    def _omit_empty_admission_fields(self, handler):
        payload = handler(self)
        for field in ("known_range", "priced_item_count", "unpriced_item_count"):
            if payload.get(field) is None:
                payload.pop(field, None)
        return payload

    @model_validator(mode="after")
    def validate_coverage(self) -> "CostCategorySummary":
        if self.coverage == "priced":
            if self.range is None or self.price_basis is None:
                raise ValueError("priced category requires range and price_basis")
        elif self.range is not None or self.price_basis is not None:
            raise ValueError("missing category cannot carry monetary fields")
        return self


class CostScenarioSummary(FrozenModel):
    scenario_id: ScenarioId
    intercity_mode: IntercityMode | None = None
    label: str = Field(min_length=1)
    total_scope: TotalScope
    total_range: PublicMoneyRangeCny | None = None
    categories: tuple[CostCategorySummary, ...]
    missing_categories: tuple[CostCategory, ...] = ()
    total_range_with_known_admission: PublicMoneyRangeCny | None = None
    unpriced_admission_count: int | None = Field(default=None, strict=True, ge=0)

    @model_serializer(mode="wrap")
    def _omit_empty_admission_fields(self, handler):
        payload = handler(self)
        for field in ("total_range_with_known_admission", "unpriced_admission_count"):
            if payload.get(field) is None:
                payload.pop(field, None)
        return payload

    @model_validator(mode="after")
    def validate_scenario(self) -> "CostScenarioSummary":
        expected_mode = {
            "train_round_trip": "train",
            "flight_round_trip": "flight",
            "without_intercity": None,
        }[self.scenario_id]
        if self.intercity_mode != expected_mode:
            raise ValueError("scenario id and intercity mode disagree")
        if tuple(item.category for item in self.categories) != CATEGORY_ORDER:
            raise ValueError("public categories must use the frozen order")
        missing = tuple(
            item.category for item in self.categories if item.coverage == "missing"
        )
        if missing != self.missing_categories:
            raise ValueError("missing_categories must match category coverage")
        priced = [item for item in self.categories if item.coverage == "priced"]
        expected_scope: TotalScope = (
            "full_trip" if not missing else "estimated_subset" if priced else "unavailable"
        )
        if self.total_scope != expected_scope:
            raise ValueError("total_scope disagrees with category coverage")
        if expected_scope == "unavailable":
            if self.total_range is not None:
                raise ValueError("unavailable scenario cannot carry total_range")
            return self
        if self.total_range is None:
            raise ValueError("priced scenario requires total_range")
        visible_min = sum(
            item.range.min_cny for item in priced if item.range is not None
        )
        visible_max = sum(
            item.range.max_cny for item in priced if item.range is not None
        )
        if self.total_range != PublicMoneyRangeCny(
            min_cny=visible_min,
            max_cny=visible_max,
        ):
            raise ValueError("public total must reconcile to category ranges")
        if self.scenario_id == "without_intercity" and (
            not self.categories
            or self.categories[0].category != "intercity_transport"
            or self.categories[0].coverage != "missing"
        ):
            raise ValueError("without_intercity must report missing intercity")
        return self


class CostAssumptionSummary(FrozenModel):
    code: AssumptionCode
    label: str = Field(min_length=1)


class CostExclusionSummary(FrozenModel):
    code: ExclusionCode
    label: str = Field(min_length=1)


class CostEstimateSummary(FrozenModel):
    snapshot_version: Literal["1"]
    completeness: Completeness
    currency: Literal["CNY"]
    estimated_at: datetime
    scenarios: tuple[CostScenarioSummary, ...]
    assumptions: tuple[CostAssumptionSummary, ...] = ()
    exclusions: tuple[CostExclusionSummary, ...] = ()
    notice: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_summary(self) -> "CostEstimateSummary":
        if self.estimated_at.tzinfo is None:
            raise ValueError("estimated_at must be timezone-aware")
        if self.estimated_at.utcoffset() != timedelta(0):
            raise ValueError("estimated_at must use UTC")
        if not self.scenarios:
            raise ValueError("cost estimate requires at least one scenario")
        ids = tuple(item.scenario_id for item in self.scenarios)
        if len(set(ids)) != len(ids):
            raise ValueError("scenario ids must be unique")
        if "without_intercity" in ids and ids != ("without_intercity",):
            raise ValueError("without_intercity must be the sole scenario")
        rank = {"complete": 0, "partial": 1, "unavailable": 2}
        scenario_states = tuple(
            {
                "full_trip": "complete",
                "estimated_subset": "partial",
                "unavailable": "unavailable",
            }[item.total_scope]
            for item in self.scenarios
        )
        worst = max(scenario_states, key=rank.__getitem__)
        if self.completeness != worst:
            raise ValueError("plan completeness must equal the worst scenario")
        return self


def _public_range(value: MoneyRangeFen) -> PublicMoneyRangeCny:
    if value.min_fen % 1000 or value.max_fen % 1000:
        raise CostEstimateProjectionError(
            "public snapshot range must be rounded outward to CNY 10"
        )
    return PublicMoneyRangeCny(
        min_cny=value.min_fen // 100,
        max_cny=value.max_fen // 100,
    )


def project_cost_estimate_summary(
    raw_snapshot: object,
    *,
    plan_index: int,
    plan_key: str,
    people_count: int,
    days: int,
    from_city_present: bool,
    requested_commute_mode: Literal["driving", "transit", "cycling"],
) -> CostEstimateSummary:
    try:
        snapshot = CostEstimateSnapshotV1.model_validate(raw_snapshot)
    except (TypeError, ValidationError, ValueError) as exc:
        raise CostEstimateProjectionError("cost snapshot is missing or invalid") from exc
    if (
        snapshot.plan_identity.plan_index != plan_index
        or snapshot.plan_identity.plan_key != plan_key
    ):
        raise CostEstimateProjectionError("cost snapshot plan identity mismatch")
    normalized = snapshot.normalized_inputs
    if (
        normalized.people_count != people_count
        or normalized.days != days
        or normalized.from_city_present != from_city_present
        or normalized.requested_commute_mode != requested_commute_mode
    ):
        raise CostEstimateProjectionError(
            "cost snapshot normalized inputs mismatch persisted result"
        )

    scenarios: list[CostScenarioSummary] = []
    for scenario in snapshot.scenarios:
        categories = tuple(
            CostCategorySummary(
                category=category.category,
                coverage=category.coverage,
                range=(
                    _public_range(category.rounded_party_range_fen)
                    if category.rounded_party_range_fen is not None
                    else None
                ),
                price_basis=category.price_basis,
                basis_label=category.basis_label,
                known_range=(
                    _public_range(category.known_range_fen)
                    if category.known_range_fen is not None
                    else None
                ),
                priced_item_count=category.priced_item_count,
                unpriced_item_count=category.unpriced_item_count,
            )
            for category in scenario.categories
        )
        scenarios.append(CostScenarioSummary(
            scenario_id=scenario.scenario_id,
            intercity_mode=scenario.intercity_mode,
            label=scenario.label,
            total_scope=scenario.total_scope,
            total_range=(
                _public_range(scenario.public_reconciled_total_fen)
                if scenario.public_reconciled_total_fen is not None
                else None
            ),
            categories=categories,
            missing_categories=scenario.missing_categories,
            total_range_with_known_admission=(
                _public_range(scenario.total_range_with_known_admission_fen)
                if scenario.total_range_with_known_admission_fen is not None
                else None
            ),
            unpriced_admission_count=scenario.unpriced_admission_count,
        ))

    try:
        return CostEstimateSummary(
            snapshot_version="1",
            completeness=snapshot.completeness,
            currency="CNY",
            estimated_at=snapshot.estimated_at,
            scenarios=tuple(scenarios),
            assumptions=tuple(
                CostAssumptionSummary(code=item.code, label=item.label)
                for item in snapshot.assumptions
            ),
            exclusions=tuple(
                CostExclusionSummary(code=item.code, label=item.label)
                for item in snapshot.exclusions
            ),
            notice=PUBLIC_COST_NOTICE,
        )
    except ValidationError as exc:
        raise CostEstimateProjectionError(
            "cost snapshot cannot satisfy the public contract"
        ) from exc


__all__ = [
    "CostCategorySummary",
    "CostAssumptionSummary",
    "CostEstimateProjectionError",
    "CostEstimateSummary",
    "CostExclusionSummary",
    "CostScenarioSummary",
    "PUBLIC_COST_NOTICE",
    "PublicMoneyRangeCny",
    "project_cost_estimate_summary",
]
