"""Typed, non-blocking source observations for v0.9.4 P2.

These contracts preserve provider/reference identity and time at the adapter
boundary.  They deliberately stop before P3 quantity math, aggregation and
snapshot persistence.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import Field, model_validator

from src.cost_reference.models import FrozenModel, MoneyRangeFen, ReviewStatus


AdapterStatus = Literal[
    "success",
    "malformed",
    "timeout",
    "unavailable",
    "missing",
    "gated",
    "not_applicable",
]
PriceBasis = Literal["sourced", "reference", "policy_zero"]
ResolutionKind = Literal["sourced", "reference", "policy_zero", "missing", "not_applicable"]


class SourceObservation(FrozenModel):
    """One validated monetary fact or reviewed reference.

    ``captured_at`` is the adapter/query boundary time.  Sourced facts also
    require their provider observation time.  Reference facts instead retain
    the reviewed table/rule version and effective date.
    """

    source_id: str = Field(min_length=1)
    source_type: str = Field(min_length=1)
    price_basis: PriceBasis
    item_identity: str = Field(min_length=1)
    captured_at: datetime
    observed_at: datetime | None = None
    reference_version: str | None = None
    effective_date: date | None = None
    provenance: str | None = None
    review_status: ReviewStatus | None = None
    range_fen: MoneyRangeFen

    @model_validator(mode="after")
    def validate_provenance(self) -> "SourceObservation":
        if self.captured_at.tzinfo is None:
            raise ValueError("captured_at must be timezone-aware")
        if self.price_basis == "sourced":
            if self.observed_at is None or self.observed_at.tzinfo is None:
                raise ValueError("sourced observations require timezone-aware observed_at")
            if any((
                self.reference_version is not None,
                self.effective_date is not None,
                self.provenance is not None,
                self.review_status is not None,
            )):
                raise ValueError("sourced observations cannot claim reference metadata")
        elif self.price_basis == "reference":
            if (
                not self.reference_version
                or self.effective_date is None
                or not self.provenance
                or not self.review_status
            ):
                raise ValueError(
                    "reference observations require provenance, version, "
                    "effective_date and review_status"
                )
        elif self.range_fen.min_fen != 0 or self.range_fen.max_fen != 0:
            raise ValueError("policy_zero observations must be exactly zero")
        elif self.reference_version is not None and (
            self.effective_date is None
            or not self.provenance
            or not self.review_status
        ):
            raise ValueError("reference policy-zero observations require full metadata")
        return self


class CostSourceResult(FrozenModel):
    """Fail-open adapter result; itinerary publishability is invariant."""

    adapter_status: AdapterStatus
    resolution: ResolutionKind
    selected: SourceObservation | None = None
    reason: str = ""
    itinerary_publishable: Literal[True] = True
    basis_label: str | None = Field(default=None, min_length=1, exclude=True)

    @model_validator(mode="after")
    def validate_selection(self) -> "CostSourceResult":
        needs_observation = self.resolution in {"sourced", "reference", "policy_zero"}
        if needs_observation != (self.selected is not None):
            raise ValueError("resolution and selected observation disagree")
        if self.selected is not None and self.selected.price_basis != self.resolution:
            raise ValueError("selected observation basis disagrees with resolution")
        return self
