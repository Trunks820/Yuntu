"""Lunch/dinner-only Amap restaurant-price adapter with city fallback."""

from __future__ import annotations

from datetime import datetime, time, timezone
from decimal import DecimalException

from src.agents.food_resolver import FoodAttachment
from src.agents.schema import CandidatePlace
from src.cost_reference.catalog import lookup_meal
from src.cost_reference.models import MAX_MONEY_FEN, MoneyRangeFen, ReferenceCatalog, VerifiedMealReference

from .common import aware_datetime, cny_to_fen, reference_observation, utc_now
from .models import CostSourceResult, SourceObservation


COUNTED_MEAL_SLOTS = frozenset({"lunch", "dinner"})
ELIGIBLE_RESTAURANT_TYPES = frozenset({"restaurant", "food"})


def _amap_meal_source(
    place: CandidatePlace | FoodAttachment,
    *,
    meal_slot: str,
    source_type: str,
    range_fen: MoneyRangeFen,
    captured_at: datetime,
    observed_at: datetime,
) -> CostSourceResult:
    identity = (
        f"amap-poi:{place.amap_poi_id}"
        if place.amap_poi_id
        else f"canonical-place:{place.place_id}"
    )
    return CostSourceResult(
        adapter_status="success",
        resolution="sourced",
        selected=SourceObservation(
            source_id="amap_place:biz_ext.cost",
            source_type=source_type,
            price_basis="sourced",
            item_identity=f"meal:{meal_slot}:{identity}",
            captured_at=captured_at,
            observed_at=observed_at,
            range_fen=range_fen,
        ),
    )


def _route_restaurant_range(place: CandidatePlace) -> MoneyRangeFen | None:
    if place.place_type not in ELIGIBLE_RESTAURANT_TYPES:
        return None
    try:
        amount_fen = cny_to_fen(place.amap_avg_price, allow_zero=False)
    except DecimalException:
        return None
    if amount_fen is None or amount_fen * 6 > MAX_MONEY_FEN * 5:
        return None
    # Preserve fractional-fen endpoints outward in the integer-fen range.
    return MoneyRangeFen(min_fen=amount_fen * 4 // 5, max_fen=(amount_fen * 6 + 4) // 5)


def resolve_meal_source(
    catalog: ReferenceCatalog,
    *,
    city: str,
    meal_slot: str,
    attachment: FoodAttachment | None,
    route_restaurant: CandidatePlace | None = None,
    assigned_meal_slot: str | None = None,
    captured_at: datetime | None = None,
) -> CostSourceResult:
    captured = captured_at or utc_now()
    if meal_slot not in COUNTED_MEAL_SLOTS:
        return CostSourceResult(
            adapter_status="not_applicable",
            resolution="not_applicable",
            reason="only assigned lunch/dinner slots contribute",
        )

    if route_restaurant is not None:
        range_fen = _route_restaurant_range(route_restaurant)
        if range_fen is not None:
            # CandidatePlace records the price without its original fetch time.
            return _amap_meal_source(
                route_restaurant,
                meal_slot=meal_slot,
                source_type="amap_route_restaurant",
                range_fen=range_fen,
                captured_at=captured,
                observed_at=captured,
            )

    provider_status = "unavailable"
    if attachment is not None:
        observed_at = aware_datetime(attachment.source_observed_at)
        amount_fen = cny_to_fen(attachment.amap_avg_price, allow_zero=False)
        if (
            assigned_meal_slot == meal_slot
            and attachment.place_type in ELIGIBLE_RESTAURANT_TYPES
            and observed_at is not None
            and amount_fen is not None
        ):
            return _amap_meal_source(
                attachment,
                meal_slot=meal_slot,
                source_type="amap_place_cached",
                captured_at=captured,
                observed_at=observed_at,
                range_fen=MoneyRangeFen(min_fen=amount_fen, max_fen=amount_fen),
            )
        provider_status = "malformed"

    reference = lookup_meal(catalog, city=city, meal=meal_slot)
    if reference is not None:
        observation = reference_observation(
            reference,
            range_fen=reference.range_fen,
            item_identity=f"meal:{city}:{meal_slot}",
            captured_at=captured,
        )
        if isinstance(reference, VerifiedMealReference):
            observation = observation.model_copy(update={
                "source_type": "verified_city_meal_facts",
                "observed_at": datetime.combine(reference.observed_date, time.min, timezone.utc),
            })
        return CostSourceResult(
            adapter_status=provider_status,
            resolution="reference",
            selected=observation,
            reason="eligible restaurant price unavailable; city main-meal reference used",
        )
    return CostSourceResult(
        adapter_status=provider_status,
        resolution="missing",
        reason="eligible restaurant price and city main-meal reference are unavailable",
    )


__all__ = ["resolve_meal_source"]
