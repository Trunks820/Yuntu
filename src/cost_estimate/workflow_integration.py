"""Fail-open P3 orchestration from locked workflow facts to snapshots."""

from __future__ import annotations

import asyncio
import hashlib

from src.agents.pace import generation_base_mode
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from src.agents.schema import RoutePlan, TripRequest, WorkflowResult
from src.agents.schema import TransportSuggestion
from src.agents.food_resolver import FoodAttachment
from src.cost_reference.catalog import load_reference_catalog
from src.cost_reference.catalog import lookup_admission_by_label
from src.cost_reference.models import ReferenceCatalog
from src.cost_sources.accommodation import resolve_accommodation_source
from src.cost_sources.intercity import IntercitySourceBundle, observe_intercity_sources
from src.cost_sources.local_transport import resolve_local_transport_source
from src.cost_sources.meals import resolve_meal_source
from src.cost_sources.admission import resolve_admission_source
from src.cost_sources.common import cny_range
from src.cost_sources.models import CostSourceResult, SourceObservation
from src.config import get_settings
from src.agents.lodging_section import parse_price_observed_at

from .models import CostEstimateSnapshotV1
from .resolver import (
    AdmissionCostInput,
    LocalTransportCostInput,
    MealCostInput,
    PlanCostInputs,
    resolve_cost_snapshot,
    unavailable_cost_snapshot,
)


COST_SOURCE_TIMEOUT_SECONDS = 5.0
INTERCITY_TIMEOUT_GRACE_SECONDS = 1.0
COST_DEADLINE_SETTLE_GRACE_SECONDS = 0.1
ADMISSION_PLACE_TYPES = frozenset({
    "attraction",
    "garden",
    "museum",
    "park",
    "photo_spot",
})
IntercityObserver = Callable[..., Awaitable[IntercitySourceBundle]]
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IntercityObservationOutcome:
    bundle: IntercitySourceBundle
    failure_code: str | None = None


@dataclass(frozen=True)
class IntercityCostPrefetch:
    task: asyncio.Task[IntercityObservationOutcome]
    started_monotonic: float
    deadline_monotonic: float
    timeout_seconds: float
    outbound_task: asyncio.Task[TransportSuggestion | None] | None = None


def _lodging_section_v2_enabled() -> bool:
    return bool(getattr(get_settings(), "lodging_section_v2_enabled", False))


def _rollinggo_accommodation_source(result, *, captured_at: datetime) -> CostSourceResult | None:
    """Nightly mid band, then economy, then high. Empty means the caller stays missing."""

    if result is None:
        return None
    checkin = getattr(result, "checkin_date", None)
    checkin_text = checkin.isoformat() if hasattr(checkin, "isoformat") else "unknown"
    for tier_name in ("mid", "economy", "high"):
        tier = getattr(result, tier_name, None)
        if tier is None:
            continue
        hotels = getattr(tier, "hotels", ()) or ()
        low = getattr(tier, "nightly_low", None)
        high = getattr(tier, "nightly_high", None)
        if not hotels or low is None or high is None:
            continue
        range_fen = cny_range(int(low), int(high))
        if range_fen is None:
            continue
        return CostSourceResult(
            adapter_status="success",
            resolution="sourced",
            selected=SourceObservation(
                source_id="rollinggo",
                source_type="rollinggo",
                price_basis="sourced",
                item_identity=f"rollinggo:lodging:checkin:{checkin_text}",
                captured_at=captured_at,
                observed_at=parse_price_observed_at(
                    str(getattr(tier, "queried_at", "") or ""),
                    captured_at,
                ),
                range_fen=range_fen,
            ),
        )
    return None


def _missing_source(reason: str) -> CostSourceResult:
    return CostSourceResult(
        adapter_status="missing",
        resolution="missing",
        reason=reason,
    )


def _provider_source(observation) -> CostSourceResult:
    if observation is None:
        return _missing_source("route-specific fare observation is unavailable")
    return CostSourceResult(
        adapter_status="success",
        resolution=observation.price_basis,
        selected=observation,
    )


def _travel_date(request: TripRequest) -> date | None:
    try:
        return date.fromisoformat(str(request.start_date or "").strip())
    except ValueError:
        return None


def _intercity_timeout_seconds() -> float:
    """Allow the transport resolver to consume its own configured budget."""

    resolver_budget = float(
        get_settings().transport_resolver_total_budget_seconds
    )
    return max(resolver_budget, 0.01) + INTERCITY_TIMEOUT_GRACE_SECONDS


def _unavailable_intercity(request: TripRequest) -> IntercitySourceBundle:
    outbound_date = _travel_date(request)
    return IntercitySourceBundle(
        from_city=str(request.from_city or "").strip() or None,
        to_city=str(request.to_city or "").strip(),
        outbound_date=outbound_date,
        return_date=(
            outbound_date + timedelta(days=max(request.days, 1) - 1)
            if outbound_date is not None
            else None
        ),
        status=(
            "unavailable"
            if str(request.from_city or "").strip()
            else "missing_from_city"
        ),
    )


async def _observe_intercity_fail_open(
    request: TripRequest,
    *,
    observer: IntercityObserver,
    captured_at: datetime,
    timeout_seconds: float,
    outbound_task: asyncio.Task[TransportSuggestion | None] | None = None,
    return_resolver: Callable[
        [TripRequest], Awaitable[TransportSuggestion | None]
    ] | None = None,
    canonicalizer: Callable[[str | None], Awaitable[str]] | None = None,
) -> IntercityObservationOutcome:
    try:
        observer_kwargs = {"captured_at": captured_at}
        if outbound_task is not None and observer is observe_intercity_sources:
            observer_kwargs["outbound_suggestion_awaitable"] = outbound_task
            if return_resolver is not None:
                observer_kwargs["resolver"] = return_resolver
            if canonicalizer is not None:
                observer_kwargs["canonicalizer"] = canonicalizer
        bundle = await asyncio.wait_for(
            observer(request, **observer_kwargs),
            timeout=timeout_seconds,
        )
        return IntercityObservationOutcome(bundle=bundle)
    except asyncio.TimeoutError:
        logger.warning(
            "cost intercity observation failed failure_code=intercity_timeout"
        )
        return IntercityObservationOutcome(
            bundle=_unavailable_intercity(request),
            failure_code="intercity_timeout",
        )
    except Exception as exc:
        failure_code = f"intercity_error:{type(exc).__name__}"
        logger.warning(
            "cost intercity observation failed failure_code=%s exception_type=%s",
            failure_code,
            type(exc).__name__,
        )
        return IntercityObservationOutcome(
            bundle=_unavailable_intercity(request),
            failure_code=failure_code,
        )


def start_intercity_cost_prefetch(
    request: TripRequest,
    *,
    observer: IntercityObserver = observe_intercity_sources,
    captured_at: datetime | None = None,
    timeout_seconds: float | None = None,
    outbound_resolver: Callable[
        [TripRequest], Awaitable[TransportSuggestion | None]
    ] | None = None,
    canonicalizer: Callable[[str | None], Awaitable[str]] | None = None,
) -> IntercityCostPrefetch:
    timeout = (
        timeout_seconds
        if timeout_seconds is not None
        else _intercity_timeout_seconds()
    )
    started = time.monotonic()
    outbound_task = (
        asyncio.create_task(
            outbound_resolver(request),
            name="shared-outbound-transport",
        )
        if outbound_resolver is not None
        else None
    )
    task = asyncio.create_task(
        _observe_intercity_fail_open(
            request,
            observer=observer,
            captured_at=captured_at or datetime.now(timezone.utc),
            timeout_seconds=timeout,
            outbound_task=outbound_task,
            return_resolver=outbound_resolver,
            canonicalizer=canonicalizer,
        ),
        name="cost-intercity-prefetch",
    )
    return IntercityCostPrefetch(
        task=task,
        outbound_task=outbound_task,
        started_monotonic=started,
        deadline_monotonic=started + timeout,
        timeout_seconds=timeout,
    )


async def cancel_intercity_cost_prefetch(
    prefetch: IntercityCostPrefetch | None,
) -> None:
    if prefetch is None:
        return
    if not prefetch.task.done():
        prefetch.task.cancel()
    await asyncio.gather(prefetch.task, return_exceptions=True)
    if prefetch.outbound_task is not None:
        if not prefetch.outbound_task.done():
            prefetch.outbound_task.cancel()
        await asyncio.gather(prefetch.outbound_task, return_exceptions=True)


def _area_identity_for_plan(
    catalog: ReferenceCatalog,
    *,
    city: str,
    accommodation_name: str,
    route_plan: RoutePlan,
) -> str | None:
    """Match only explicit catalog area labels; never use a citywide default."""

    candidates = {
        accommodation_name.strip(),
        *(day.area.strip() for day in route_plan.day_groups if day.area.strip()),
    }
    matches = {
        item.area_identity
        for item in catalog.accommodation
        if item.review_status == "reviewed"
        and item.city == city
        and item.area_label in candidates
    }
    return next(iter(matches)) if len(matches) == 1 else None


async def _plan_inputs(
    result: WorkflowResult,
    *,
    plan_index: int,
    intercity: IntercitySourceBundle,
    catalog: ReferenceCatalog,
    estimated_at: datetime,
    meal_attachments: Mapping[tuple[int, int, str], FoodAttachment],
) -> PlanCostInputs:
    request = result.trip_request
    plan = result.plans[plan_index]
    route_plan = result.route_plans[plan_index]
    city = request.to_city.strip()

    accommodation_source = _missing_source(
        "stable accommodation area identity is unavailable"
    )
    use_rollinggo_lodging = (
        _lodging_section_v2_enabled()
        and plan.accommodation is not None
        and plan.accommodation.source != "user_specified"
    )
    if use_rollinggo_lodging:
        priced = _rollinggo_accommodation_source(
            getattr(result, "lodging_price_result", None),
            captured_at=estimated_at,
        )
        accommodation_source = priced or _missing_source(
            "rollinggo lodging price is unavailable"
        )
    elif plan.accommodation is not None:
        area_identity = _area_identity_for_plan(
            catalog,
            city=city,
            accommodation_name=plan.accommodation.name,
            route_plan=route_plan,
        )
        if area_identity is not None:
            accommodation_source = await resolve_accommodation_source(
                catalog,
                city=city,
                area_identity=area_identity,
                travel_date=_travel_date(request),
                captured_at=estimated_at,
            )

    local_transport: list[LocalTransportCostInput] = []
    for day_group in route_plan.day_groups:
        for leg_index, leg in enumerate(day_group.commute_legs):
            identity = (
                f"route:plan:{plan_index}:day:{day_group.day}:leg:{leg_index}:"
                f"{leg.from_place_id}->{leg.to_place_id}:{leg.mode}"
            )
            provider_result = _provider_source(leg.fare_observation)
            selected = (
                resolve_local_transport_source(
                    catalog,
                    city=city,
                    effective_mode=leg.mode,
                    requested_commute_mode=request.commute_mode,
                    provider_result=provider_result,
                    captured_at=estimated_at,
                )
                if leg.mode in {"driving", "transit"}
                else CostSourceResult(
                    adapter_status="not_applicable",
                    resolution="not_applicable",
                    reason="walking/cycling uses deterministic P3 policy",
                )
            )
            local_transport.append(LocalTransportCostInput(
                route_identity=identity,
                effective_mode=leg.mode,
                source=selected,
            ))

        if route_plan.route_policy_version == "selector-route-v2":
            for access in day_group.access_legs:
                anchor_key = hashlib.sha256(f"{access.anchor_latitude},{access.anchor_longitude}".encode()).hexdigest()
                identity = (f"route:plan:{plan_index}:day:{day_group.day}:access:{access.direction}:"
                            f"{access.place_id}:{access.mode}:{anchor_key}")
                source = resolve_local_transport_source(
                    catalog, city=city, effective_mode=access.mode,
                    requested_commute_mode=request.commute_mode,
                    provider_result=_missing_source("accommodation access has no verified fare observation"),
                    captured_at=estimated_at,
                )
                local_transport.append(LocalTransportCostInput(route_identity=identity, effective_mode=access.mode, source=source))
            if not day_group.access_legs:
                # The omitted journey is missing coverage, not two free or priced rides.
                mode, _ = generation_base_mode(request, get_settings())
                local_transport.append(LocalTransportCostInput(
                    route_identity=f"route:plan:{plan_index}:day:{day_group.day}:access:unknown",
                    effective_mode=mode,
                    source=_missing_source("accommodation access location and fare are unknown"),
                ))

    unique_places = {
        place.place_id: place
        for day_group in route_plan.day_groups
        for place in day_group.places
    }
    admissions: list[AdmissionCostInput] = []
    for place_id in sorted(unique_places):
        place = unique_places[place_id]
        if place.place_type.lower() not in ADMISSION_PLACE_TYPES:
            continue
        identity = f"canonical-place:{place.place_id}"
        # Try catalog lookup by place name + city
        ref = lookup_admission_by_label(
            catalog, place_label=place.name, city=city,
        )
        if ref is not None:
            source = resolve_admission_source(
                catalog,
                place_identity=ref.place_identity,
                captured_at=estimated_at,
            )
        else:
            source = _missing_source(
                f"no catalog entry for {place.name} in {city}"
            )
        admissions.append(AdmissionCostInput(
            place_identity=identity,
            charging_basis="person_entry",
            source=source,
        ))

    meals = tuple(
        MealCostInput(
            day=day,
            meal_slot=slot,
            source=resolve_meal_source(
                catalog,
                city=city,
                meal_slot=slot,
                attachment=meal_attachments.get((plan_index, day, slot)),
                assigned_meal_slot=slot,
                captured_at=estimated_at,
            ),
        )
        for day in range(1, request.days + 1)
        for slot in ("lunch", "dinner")
    )
    return PlanCostInputs(
        plan_index=plan_index,
        plan_key=plan.plan_name,
        people_count=max(request.people_count, 1),
        days=max(request.days, 1),
        requested_commute_mode=request.commute_mode,
        from_city_present=bool(request.from_city.strip()),
        intercity=intercity,
        accommodation=accommodation_source,
        local_transport=tuple(local_transport),
        admissions=tuple(admissions),
        meals=meals,
    )


def unavailable_snapshots_for_result(
    result: WorkflowResult,
    *,
    estimated_at: datetime,
    failure_code: str,
) -> tuple[CostEstimateSnapshotV1, ...]:
    return tuple(
        unavailable_cost_snapshot(
            plan_index=index,
            plan_key=plan.plan_name,
            people_count=result.trip_request.people_count,
            days=result.trip_request.days,
            requested_commute_mode=result.trip_request.commute_mode,
            from_city_present=bool(result.trip_request.from_city.strip()),
            estimated_at=estimated_at,
            failure_code=failure_code,
        )
        for index, plan in enumerate(result.plans)
    )


async def resolve_workflow_cost_snapshots(
    result: WorkflowResult,
    *,
    intercity_observer: IntercityObserver = observe_intercity_sources,
    catalog: ReferenceCatalog | None = None,
    estimated_at: datetime | None = None,
    timeout_seconds: float = COST_SOURCE_TIMEOUT_SECONDS,
    intercity_timeout_seconds: float | None = None,
    intercity_prefetch: IntercityCostPrefetch | None = None,
    meal_attachments: Mapping[tuple[int, int, str], FoodAttachment] | None = None,
) -> tuple[CostEstimateSnapshotV1, ...]:
    """Resolve all plan snapshots while isolating intercity source failures.

    Storage is intentionally outside this fail-open boundary.  A later database
    failure must propagate as a persistence error rather than be relabelled as a
    missing price.
    """

    captured_at = estimated_at or datetime.now(timezone.utc)
    if intercity_prefetch is None:
        intercity_prefetch = start_intercity_cost_prefetch(
            result.trip_request,
            observer=intercity_observer,
            captured_at=captured_at,
            timeout_seconds=intercity_timeout_seconds,
        )
    intercity_timeout = intercity_prefetch.timeout_seconds
    cost_deadline_monotonic = time.monotonic() + (
        max(timeout_seconds, intercity_timeout)
        + COST_DEADLINE_SETTLE_GRACE_SECONDS
    )

    async def resolve_inputs() -> list[PlanCostInputs]:
        reference_catalog = catalog or load_reference_catalog()
        placeholder = _unavailable_intercity(result.trip_request)
        plan_inputs = []
        for index in range(len(result.plans)):
            inputs = await _plan_inputs(
                result,
                plan_index=index,
                intercity=placeholder,
                catalog=reference_catalog,
                estimated_at=captured_at,
                meal_attachments=meal_attachments or {},
            )
            plan_inputs.append(inputs)
        return plan_inputs

    async def resolve_all() -> tuple[CostEstimateSnapshotV1, ...]:
        outcome, plan_inputs = await asyncio.gather(
            intercity_prefetch.task,
            asyncio.wait_for(resolve_inputs(), timeout=timeout_seconds),
        )
        completed_at = estimated_at or datetime.now(timezone.utc)
        return tuple(
            resolve_cost_snapshot(
                inputs.model_copy(update={
                    "intercity": outcome.bundle,
                    "intercity_failure_code": outcome.failure_code,
                }),
                estimated_at=completed_at,
            )
            for inputs in plan_inputs
        )

    try:
        return await asyncio.wait_for(
            resolve_all(),
            timeout=max(cost_deadline_monotonic - time.monotonic(), 0.0),
        )
    except asyncio.TimeoutError:
        return unavailable_snapshots_for_result(
            result,
            estimated_at=estimated_at or datetime.now(timezone.utc),
            failure_code="cost_resolver_timeout",
        )
    except Exception as exc:
        return unavailable_snapshots_for_result(
            result,
            estimated_at=estimated_at or datetime.now(timezone.utc),
            failure_code=f"cost_resolver_error:{type(exc).__name__}",
        )
    finally:
        await cancel_intercity_cost_prefetch(intercity_prefetch)


__all__ = [
    "COST_SOURCE_TIMEOUT_SECONDS",
    "COST_DEADLINE_SETTLE_GRACE_SECONDS",
    "IntercityCostPrefetch",
    "INTERCITY_TIMEOUT_GRACE_SECONDS",
    "cancel_intercity_cost_prefetch",
    "resolve_workflow_cost_snapshots",
    "start_intercity_cost_prefetch",
    "unavailable_snapshots_for_result",
]
