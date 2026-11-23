"""Admission-price precedence with generic Amap consumption excluded."""

from __future__ import annotations

import re
from datetime import datetime, time, timezone
from typing import Any

from src.cost_reference.catalog import lookup_admission
from src.cost_reference.models import MoneyRangeFen, ReferenceCatalog, VerifiedAdmissionFact

from .common import aware_datetime, cny_range, reference_observation, utc_now
from .models import CostSourceResult, SourceObservation


_STABLE_PLACE_IDENTITY_RE = re.compile(r"^official:[a-z0-9_.:-]+$")


def _explicit_observation(
    payload: dict[str, Any] | None,
    *,
    place_identity: str,
    captured_at: datetime,
    verified_free: bool,
) -> SourceObservation | None:
    if not isinstance(payload, dict):
        return None
    observed_at = aware_datetime(payload.get("observed_at"))
    source_id = str(payload.get("source_id") or "").strip()
    provider = str(payload.get("provider") or "").strip()
    source_place_identity = str(payload.get("place_identity") or "").strip()
    if (
        _STABLE_PLACE_IDENTITY_RE.fullmatch(place_identity) is None
        or _STABLE_PLACE_IDENTITY_RE.fullmatch(source_place_identity) is None
        or source_place_identity != place_identity
        or observed_at is None
        or not source_id
        or not provider
    ):
        return None
    if verified_free:
        price_range = MoneyRangeFen(min_fen=0, max_fen=0)
        basis = "policy_zero"
    else:
        price_range = cny_range(
            payload.get("min_cny"),
            payload.get("max_cny"),
            allow_zero=False,
        )
        basis = "sourced"
        if price_range is None:
            return None
    return SourceObservation(
        source_id=source_id,
        source_type=provider,
        price_basis=basis,
        item_identity=f"admission:{source_place_identity}",
        captured_at=captured_at,
        observed_at=observed_at,
        range_fen=price_range,
    )


def resolve_admission_source(
    catalog: ReferenceCatalog,
    *,
    place_identity: str,
    explicit_ticket_price: dict[str, Any] | None = None,
    verified_free_fact: dict[str, Any] | None = None,
    verified_fact: VerifiedAdmissionFact | None = None,
    amap_generic_consumption: Any = None,
    captured_at: datetime | None = None,
) -> CostSourceResult:
    """Resolve explicit observations, verified ticket facts, then legacy references.

    ``amap_generic_consumption`` is accepted only to make the rejection boundary
    explicit.  It is never parsed or promoted to an admission fact.
    """

    del amap_generic_consumption
    captured = captured_at or utc_now()
    explicit = _explicit_observation(
        explicit_ticket_price,
        place_identity=place_identity,
        captured_at=captured,
        verified_free=False,
    )
    if explicit is not None:
        return CostSourceResult(
            adapter_status="success",
            resolution="sourced",
            selected=explicit,
        )
    free = _explicit_observation(
        verified_free_fact,
        place_identity=place_identity,
        captured_at=captured,
        verified_free=True,
    )
    if free is not None:
        return CostSourceResult(
            adapter_status="success",
            resolution="policy_zero",
            selected=free,
        )
    if verified_fact is not None:
        if captured.date() >= verified_fact.expires:
            return CostSourceResult(
                adapter_status="missing",
                resolution="missing",
                reason="verified admission fact expired",
            )
        basis = "policy_zero" if verified_fact.price_kind == "free" else "sourced"
        return CostSourceResult(
            adapter_status="success",
            resolution=basis,
            selected=SourceObservation(
                source_id=str(verified_fact.sources[0].url),
                source_type="verified_ticket_facts",
                price_basis=basis,
                item_identity=f"admission:{place_identity}",
                captured_at=captured,
                observed_at=datetime.combine(verified_fact.checked, time.min, timezone.utc),
                range_fen=verified_fact.adult_price_cny.as_fen(),
            ),
        )
    reference = lookup_admission(catalog, place_identity=place_identity)
    if reference is not None:
        observation = reference_observation(
            reference,
            range_fen=reference.range_fen,
            item_identity=f"admission:{place_identity}",
            captured_at=captured,
        )
        if reference.price_kind == "verified_free":
            observation = SourceObservation(
                source_id=observation.source_id,
                source_type=observation.source_type,
                price_basis="policy_zero",
                item_identity=observation.item_identity,
                captured_at=observation.captured_at,
                observed_at=None,
                reference_version=observation.reference_version,
                effective_date=observation.effective_date,
                provenance=observation.provenance,
                review_status=observation.review_status,
                range_fen=observation.range_fen,
            )
            return CostSourceResult(
                adapter_status="success",
                resolution="policy_zero",
                selected=observation,
            )
        return CostSourceResult(
            adapter_status="success",
            resolution="reference",
            selected=observation,
        )
    status = "malformed" if explicit_ticket_price is not None or verified_free_fact is not None else "missing"
    return CostSourceResult(
        adapter_status=status,
        resolution="missing",
        reason="no eligible admission price/free/reference fact",
    )


__all__ = ["resolve_admission_source"]
