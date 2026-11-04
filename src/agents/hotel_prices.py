"""Placeholder hotel lookup.

The public guest monolith does not call a priced hotel API. A lookup returns
an empty result so lodging copy can say 待核实 instead of inventing a rate.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class HotelOffer:
    name: str = ""
    nightly_price: float | None = None
    distance_meters: int = 0


@dataclass
class HotelTier:
    hotels: tuple = ()
    nightly_low: int | None = None
    nightly_high: int | None = None
    failure: str = "no_priced_source"


@dataclass
class HotelTierResult:
    economy: HotelTier = field(default_factory=HotelTier)
    mid: HotelTier = field(default_factory=HotelTier)
    high: HotelTier = field(default_factory=HotelTier)
    failure_reason: str = "no_priced_source"


async def fetch_tier_hotels(**_kwargs) -> HotelTierResult:
    """Never returns a nightly price. Callers must label the gap 待核实."""
    return HotelTierResult()
