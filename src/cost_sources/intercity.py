"""Independent outbound/return intercity source adapter for v0.9.4 P2."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from typing import Literal

from pydantic import Field, model_validator

from src.agents.intercity_transport_resolver import (
    canonicalize_transport_city,
    resolve_transport,
)
from src.agents.intercity_transport_static import STATIC_TRANSPORT_VERSION
from src.agents.schema import (
    FLIGHT_FARE_PENDING,
    TrainClass,
    TransportModeSummary,
    TransportSuggestion,
    TripRequest,
)
from src.cost_reference.models import FrozenModel, MoneyRangeFen

from .common import aware_datetime, cny_range, utc_now
from .models import AdapterStatus, SourceObservation


IntercityMode = Literal["train", "flight"]
Direction = Literal["outbound", "return"]
TransportResolver = Callable[[TripRequest], Awaitable[TransportSuggestion | None]]
CityCanonicalizer = Callable[[str | None], Awaitable[str]]
_PRICE_RANGE_RE = re.compile(
    r"^\s*[¥￥]?\s*(?P<min>\d+(?:\.\d{1,2})?)\s*[-–—~至]\s*"
    r"[¥￥]?\s*(?P<max>\d+(?:\.\d{1,2})?)"
)


class IntercityLegInput(FrozenModel):
    direction: Direction
    from_city: str = Field(min_length=1)
    to_city: str = Field(min_length=1)
    query_date: date | None
    mode: IntercityMode
    train_class: TrainClass | None = None
    adapter_status: AdapterStatus
    observation: SourceObservation | None = None
    reason: str = ""


class IntercityRoundTripInput(FrozenModel):
    mode: IntercityMode
    outbound: IntercityLegInput
    return_leg: IntercityLegInput

    @model_validator(mode="after")
    def validate_same_mode_and_direction(self) -> "IntercityRoundTripInput":
        if self.outbound.mode != self.mode or self.return_leg.mode != self.mode:
            raise ValueError("round-trip legs must use the same mode")
        if (
            self.outbound.direction != "outbound"
            or self.return_leg.direction != "return"
        ):
            raise ValueError("round-trip directions are invalid")
        if (
            self.outbound.from_city != self.return_leg.to_city
            or self.outbound.to_city != self.return_leg.from_city
        ):
            raise ValueError("return leg must reverse the outbound route")
        return self


class IntercitySourceBundle(FrozenModel):
    from_city: str | None
    to_city: str
    outbound_date: date | None
    return_date: date | None
    status: Literal[
        "available",
        "missing_from_city",
        "no_direct_service",
        "malformed",
        "unavailable",
    ]
    round_trips: tuple[IntercityRoundTripInput, ...] = ()
    itinerary_publishable: Literal[True] = True


def _request_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(str(value or "").strip())
    except ValueError:
        return None


def _return_date(request: TripRequest) -> date | None:
    start = _request_date(request.start_date)
    if start is None:
        return None
    return start + timedelta(days=max(int(request.days), 1) - 1)


def _price_range(value: str) -> MoneyRangeFen | None:
    match = _PRICE_RANGE_RE.match(value or "")
    if match is None:
        return None
    return cny_range(match.group("min"), match.group("max"), allow_zero=False)


def _leg_from_mode(
    *,
    direction: Direction,
    suggestion: TransportSuggestion,
    mode: TransportModeSummary,
    query_date: date | None,
    captured_at: datetime,
) -> IntercityLegInput:
    source_query_date = _request_date(suggestion.query_date)
    if source_query_date != query_date:
        return IntercityLegInput(
            direction=direction,
            from_city=suggestion.from_city,
            to_city=suggestion.to_city,
            query_date=source_query_date,
            mode=mode.mode,
            train_class=mode.train_class,
            adapter_status="malformed",
            reason="intercity source query date does not match requested date",
        )
    fare_pending = mode.mode == "flight" and mode.price_range == FLIGHT_FARE_PENDING
    if fare_pending:
        return _missing_leg(
            direction=direction,
            from_city=suggestion.from_city,
            to_city=suggestion.to_city,
            query_date=query_date,
            mode="flight",
            status="unavailable",
            reason=FLIGHT_FARE_PENDING,
        )
    price_range = _price_range(mode.price_range)
    if price_range is None:
        return IntercityLegInput(
            direction=direction,
            from_city=suggestion.from_city,
            to_city=suggestion.to_city,
            query_date=query_date,
            mode=mode.mode,
            train_class=mode.train_class,
            adapter_status="malformed",
            reason="intercity price range is missing or malformed",
        )
    fare_date = source_query_date
    if mode.mode == "train" and mode.price_source == "realtime" and mode.reference_date:
        fare_date = mode.reference_date
    identity = (
        f"route:{suggestion.from_city}->{suggestion.to_city}:"
        f"{fare_date.isoformat() if fare_date else 'no-date'}:"
        f"{mode.mode}"
    )
    if mode.price_source == "realtime":
        observed_at = (
            aware_datetime(mode.availability_checked_at)
            or aware_datetime(suggestion.cached_at)
            or captured_at
        )
        observation = SourceObservation(
            source_id=(
                "12306_ticket_price"
                if mode.mode == "train"
                else "intercity_realtime"
            ),
            source_type="intercity_provider",
            price_basis="sourced",
            item_identity=identity,
            captured_at=captured_at,
            observed_at=observed_at,
            range_fen=price_range,
        )
    else:
        observation = SourceObservation(
            source_id=f"intercity_static:{STATIC_TRANSPORT_VERSION}",
            source_type="versioned_intercity_reference",
            price_basis="reference",
            item_identity=identity,
            captured_at=captured_at,
            reference_version=STATIC_TRANSPORT_VERSION,
            effective_date=date.fromisoformat(STATIC_TRANSPORT_VERSION),
            provenance="src.agents.intercity_transport_static",
            review_status="reviewed",
            range_fen=price_range,
        )
    return IntercityLegInput(
        direction=direction,
        from_city=suggestion.from_city,
        to_city=suggestion.to_city,
        query_date=query_date,
        mode=mode.mode,
        train_class=mode.train_class,
        adapter_status="success",
        observation=observation,
    )


async def _safe_resolve(
    request: TripRequest,
    *,
    resolver: TransportResolver,
) -> tuple[AdapterStatus, TransportSuggestion | None, str]:
    try:
        suggestion = await resolver(request)
    except asyncio.TimeoutError:
        return "timeout", None, "intercity source lookup timed out"
    except Exception as exc:
        return "unavailable", None, f"intercity source unavailable: {type(exc).__name__}"
    return _validate_suggestion(request, suggestion)


def _validate_suggestion(
    request: TripRequest,
    suggestion: TransportSuggestion | None,
) -> tuple[AdapterStatus, TransportSuggestion | None, str]:
    if suggestion is None:
        return "unavailable", None, "intercity source returned no suggestion"
    expected_from = str(request.from_city or "").strip()
    expected_to = str(request.to_city or "").strip()
    if (
        suggestion.from_city != expected_from
        or suggestion.to_city != expected_to
    ):
        return (
            "malformed",
            None,
            "intercity source direction does not match requested route",
        )
    expected_date = str(request.start_date or "").strip() or None
    source_date = str(suggestion.query_date or "").strip() or None
    if source_date != expected_date:
        return (
            "malformed",
            None,
            "intercity source query date does not match requested date",
        )
    return "success", suggestion, ""


async def _safe_shared_outbound(
    request: TripRequest,
    *,
    suggestion_awaitable: Awaitable[TransportSuggestion | None],
) -> tuple[AdapterStatus, TransportSuggestion | None, str]:
    """Consume the workflow-owned outbound single-flight without cancelling it."""

    try:
        suggestion = await asyncio.shield(suggestion_awaitable)
    except asyncio.TimeoutError:
        return "timeout", None, "intercity source lookup timed out"
    except Exception as exc:
        return "unavailable", None, f"intercity source unavailable: {type(exc).__name__}"
    return _validate_suggestion(request, suggestion)


async def _safe_canonical_city(
    value: str | None,
    *,
    canonicalizer: CityCanonicalizer,
) -> str:
    """Canonicalize once at the adapter boundary without blocking publication."""

    trimmed = str(value or "").strip()
    if not trimmed:
        return ""
    try:
        canonical = await canonicalizer(trimmed)
    except Exception:
        return trimmed
    return str(canonical or "").strip() or trimmed


def _missing_leg(
    *,
    direction: Direction,
    from_city: str,
    to_city: str,
    query_date: date | None,
    mode: IntercityMode,
    status: AdapterStatus,
    reason: str,
) -> IntercityLegInput:
    return IntercityLegInput(
        direction=direction,
        from_city=from_city,
        to_city=to_city,
        query_date=query_date,
        mode=mode,
        adapter_status=status,
        reason=reason,
    )


async def observe_intercity_sources(
    request: TripRequest,
    *,
    resolver: TransportResolver = resolve_transport,
    canonicalizer: CityCanonicalizer = canonicalize_transport_city,
    captured_at: datetime | None = None,
    outbound_suggestion_awaitable: Awaitable[TransportSuggestion | None] | None = None,
) -> IntercitySourceBundle:
    """Query opposite directions and expose outbound-mode-only round trips.

    Return fares must match the outbound mode and rail class.
    """

    captured = captured_at or utc_now()
    raw_from_city = str(request.from_city or "").strip()
    raw_to_city = str(request.to_city or "").strip()
    outbound_date = _request_date(request.start_date)
    return_date = _return_date(request)
    if not raw_from_city:
        return IntercitySourceBundle(
            from_city=None,
            to_city=raw_to_city,
            outbound_date=outbound_date,
            return_date=return_date,
            status="missing_from_city",
        )

    from_city, to_city = await asyncio.gather(
        _safe_canonical_city(raw_from_city, canonicalizer=canonicalizer),
        _safe_canonical_city(raw_to_city, canonicalizer=canonicalizer),
    )
    if not from_city:
        return IntercitySourceBundle(
            from_city=None,
            to_city=to_city,
            outbound_date=outbound_date,
            return_date=return_date,
            status="missing_from_city",
        )
    if not to_city or from_city == to_city:
        return IntercitySourceBundle(
            from_city=from_city,
            to_city=to_city,
            outbound_date=outbound_date,
            return_date=return_date,
            status="unavailable",
        )

    outbound_request = request.model_copy(
        deep=True,
        update={"from_city": from_city, "to_city": to_city},
    )
    return_request = request.model_copy(
        deep=True,
        update={
            "from_city": to_city,
            "to_city": from_city,
            "start_date": return_date.isoformat() if return_date else None,
            "end_date": return_date.isoformat() if return_date else None,
            "days": 1,
        },
    )
    outbound_resolution = (
        _safe_shared_outbound(
            outbound_request,
            suggestion_awaitable=outbound_suggestion_awaitable,
        )
        if outbound_suggestion_awaitable is not None
        else _safe_resolve(outbound_request, resolver=resolver)
    )
    outbound_result, return_result = await asyncio.gather(
        outbound_resolution,
        _safe_resolve(return_request, resolver=resolver),
    )
    outbound_status, outbound_suggestion, _ = outbound_result
    return_status, return_suggestion, return_reason = return_result
    if outbound_suggestion is None or outbound_suggestion.direct_service_absent:
        status = "unavailable"
        if outbound_suggestion is not None:
            status = "no_direct_service"
        elif outbound_status == "malformed":
            status = "malformed"
        return IntercitySourceBundle(
            from_city=from_city,
            to_city=to_city,
            outbound_date=outbound_date,
            return_date=return_date,
            status=status,
        )

    return_modes = {
        (mode.mode, mode.train_class == "ordinary"): mode
        for mode in (return_suggestion.modes if return_suggestion is not None else [])
    }
    return_direction_matches = (
        return_suggestion is not None
        and return_suggestion.from_city == outbound_suggestion.to_city
        and return_suggestion.to_city == outbound_suggestion.from_city
    )
    outbound_legs = [
        _leg_from_mode(
            direction="outbound",
            suggestion=outbound_suggestion,
            mode=outbound_mode,
            query_date=outbound_date,
            captured_at=captured,
        ) for outbound_mode in outbound_suggestion.modes
    ]
    if outbound_suggestion.flight_fare_pending:
        outbound_legs.append(_missing_leg(
            direction="outbound",
            from_city=from_city,
            to_city=to_city,
            query_date=outbound_date,
            mode="flight",
            status="unavailable",
            reason=FLIGHT_FARE_PENDING,
        ))

    round_trips: list[IntercityRoundTripInput] = []
    for outbound_leg in outbound_legs:
        return_mode = return_modes.get((
            outbound_leg.mode, outbound_leg.train_class == "ordinary",
        ))
        if return_direction_matches and return_mode is not None:
            return_leg = _leg_from_mode(
                direction="return",
                suggestion=return_suggestion,
                mode=return_mode,
                query_date=return_date,
                captured_at=captured,
            )
        else:
            return_leg = _missing_leg(
                direction="return",
                from_city=to_city,
                to_city=from_city,
                query_date=return_date,
                mode=outbound_leg.mode,
                status=(
                    "unavailable" if return_direction_matches else return_status
                ),
                reason=(
                    (FLIGHT_FARE_PENDING if outbound_leg.mode == "flight" else return_reason)
                    or (
                        "same-mode return source is unavailable"
                        if return_direction_matches or return_suggestion is None
                        else "return source direction does not reverse outbound"
                    )
                ),
            )
        round_trips.append(IntercityRoundTripInput(
            mode=outbound_leg.mode,
            outbound=outbound_leg,
            return_leg=return_leg,
        ))
    return IntercitySourceBundle(
        from_city=from_city,
        to_city=to_city,
        outbound_date=outbound_date,
        return_date=return_date,
        status="available" if round_trips else "unavailable",
        round_trips=tuple(round_trips),
    )


__all__ = [
    "IntercityLegInput",
    "IntercityRoundTripInput",
    "IntercitySourceBundle",
    "observe_intercity_sources",
]
