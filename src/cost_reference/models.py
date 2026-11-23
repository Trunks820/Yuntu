"""Typed, immutable v0.9.4 reference-estimate data contracts.

This package owns reviewed static reference data only.  It is intentionally
independent from the Writer/Review LLM path and from runtime source adapters.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, model_validator


MAX_MONEY_FEN = 2**63 - 1
MoneyFen = Annotated[int, Field(strict=True, ge=0, le=MAX_MONEY_FEN)]
PositiveIntStrict = Annotated[int, Field(strict=True, gt=0)]

ReviewStatus = Literal["reviewed", "pending_review", "rejected"]
IntercityMode = Literal["train", "flight"]


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MoneyRangeFen(FrozenModel):
    min_fen: MoneyFen
    max_fen: MoneyFen

    @model_validator(mode="after")
    def validate_order(self) -> "MoneyRangeFen":
        if self.min_fen > self.max_fen:
            raise ValueError("min_fen must be <= max_fen")
        return self


class SourceRecord(FrozenModel):
    source_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    publisher: str = Field(min_length=1)
    source_uri: str = Field(min_length=1)
    observed_at: datetime
    facts: tuple[str, ...] = Field(min_length=1)


class ReferenceEntry(FrozenModel):
    provenance: str = Field(min_length=1)
    version: str = Field(min_length=1)
    effective_date: date
    review_status: ReviewStatus
    source_ids: tuple[str, ...] = Field(min_length=1)


class AccommodationReference(ReferenceEntry):
    city: str = Field(min_length=1)
    area_identity: str = Field(pattern=r"^area:[a-z0-9_:-]+$")
    area_label: str = Field(min_length=1)
    comfort_tier: Literal["comfort"] = "comfort"
    period_class: Literal["ordinary", "date_period"]
    date_start: date | None = None
    date_end: date | None = None
    source_values_fen: tuple[MoneyFen, ...] = Field(min_length=1)
    range_fen: MoneyRangeFen
    charging_unit: Literal["room_night"] = "room_night"

    @model_validator(mode="after")
    def validate_period_and_derivation(self) -> "AccommodationReference":
        has_dates = self.date_start is not None or self.date_end is not None
        if self.period_class == "ordinary" and has_dates:
            raise ValueError("ordinary accommodation rows cannot carry dates")
        if self.period_class == "date_period":
            if self.date_start is None or self.date_end is None:
                raise ValueError("date_period requires both dates")
            if self.date_start > self.date_end:
                raise ValueError("date_start must be <= date_end")
        if self.range_fen != MoneyRangeFen(
            min_fen=min(self.source_values_fen),
            max_fen=max(self.source_values_fen),
        ):
            raise ValueError("accommodation range must be source-value min/max")
        return self


class AdmissionReference(ReferenceEntry):
    city: str = Field(min_length=1)
    place_identity: str = Field(pattern=r"^official:[a-z0-9_.:-]+$")
    place_label: str = Field(min_length=1)
    price_kind: Literal["paid", "verified_free"]
    source_range_fen: MoneyRangeFen
    range_fen: MoneyRangeFen
    charging_unit: Literal["person_entry"] = "person_entry"

    @model_validator(mode="after")
    def validate_price_kind(self) -> "AdmissionReference":
        if self.range_fen != self.source_range_fen:
            raise ValueError("admission range must equal the published range")
        is_zero = self.range_fen.min_fen == self.range_fen.max_fen == 0
        if self.price_kind == "verified_free" and not is_zero:
            raise ValueError("verified_free must be exactly zero")
        if self.price_kind == "paid" and self.range_fen.max_fen == 0:
            raise ValueError("paid admission must be positive")
        return self


class MealReference(ReferenceEntry):
    city: str = Field(min_length=1)
    meal: Literal["lunch", "dinner"]
    source_range_fen: MoneyRangeFen
    source_party_size: PositiveIntStrict
    range_fen: MoneyRangeFen
    charging_unit: Literal["person_meal"] = "person_meal"

    @model_validator(mode="after")
    def validate_party_derivation(self) -> "MealReference":
        if (
            self.source_range_fen.min_fen % self.source_party_size
            or self.source_range_fen.max_fen % self.source_party_size
        ):
            raise ValueError("meal source range must divide exactly by party size")
        expected = MoneyRangeFen(
            min_fen=self.source_range_fen.min_fen // self.source_party_size,
            max_fen=self.source_range_fen.max_fen // self.source_party_size,
        )
        if self.range_fen != expected:
            raise ValueError("meal range must be the per-person source range")
        return self


class VerifiedMealReference(MealReference):
    observed_date: date


class VerifiedFactModel(FrozenModel):
    # The file also records booking, extra fees and sampling details.
    model_config = ConfigDict(extra="ignore", frozen=True)


class FactPriceCny(FrozenModel):
    min: Decimal = Field(ge=0, le=Decimal(MAX_MONEY_FEN) / 100, decimal_places=2)
    max: Decimal = Field(ge=0, le=Decimal(MAX_MONEY_FEN) / 100, decimal_places=2)

    @model_validator(mode="after")
    def validate_order(self) -> "FactPriceCny":
        if self.min > self.max:
            raise ValueError("min must be <= max")
        return self

    def as_fen(self) -> MoneyRangeFen:
        return MoneyRangeFen(min_fen=int(self.min * 100), max_fen=int(self.max * 100))


class VerifiedTicketSource(VerifiedFactModel):
    url: AnyHttpUrl
    quote: str = Field(min_length=1)


class VerifiedAdmissionFact(VerifiedFactModel):
    city: str = Field(min_length=1)
    name: str = Field(min_length=1)
    aliases: tuple[str, ...]
    price_kind: Literal["free", "paid"]
    adult_price_cny: FactPriceCny
    ticket_key: str | None = Field(min_length=1)
    sources: tuple[VerifiedTicketSource, ...] = Field(min_length=1)
    confidence: str = Field(min_length=1)
    checked: date
    expires: date

    @model_validator(mode="after")
    def validate_price_and_dates(self) -> "VerifiedAdmissionFact":
        if (self.price_kind == "free") != (self.adult_price_cny.max == 0):
            raise ValueError("only free tickets must have zero prices")
        if self.expires <= self.checked:
            raise ValueError("expires must be after checked")
        return self


class VerifiedMealCity(VerifiedFactModel):
    city: str = Field(min_length=1)
    per_person_cny: FactPriceCny


class VerifiedMealTable(VerifiedFactModel):
    method: str = Field(min_length=1)
    amap_captured: date
    queried: date
    cities: tuple[VerifiedMealCity, ...]


class VerifiedFacts(VerifiedFactModel):
    version: str = Field(min_length=1)
    admission: tuple[VerifiedAdmissionFact, ...]
    meal_city_reference: VerifiedMealTable

    @model_validator(mode="after")
    def validate_unique_keys(self) -> "VerifiedFacts":
        admission_keys = [
            (row.city, label) for row in self.admission for label in {row.name, *row.aliases}
        ]
        meal_keys = [row.city for row in self.meal_city_reference.cities]
        if len(set(admission_keys)) != len(admission_keys) or len(set(meal_keys)) != len(meal_keys):
            raise ValueError("duplicate verified fact lookup key")
        return self


class TaxiFareInputs(FrozenModel):
    base_fare_range_fen: MoneyRangeFen
    included_distance_meters: PositiveIntStrict
    upper_distance_meters: PositiveIntStrict
    per_km_fen: MoneyFen

    @model_validator(mode="after")
    def validate_distance(self) -> "TaxiFareInputs":
        if self.upper_distance_meters <= self.included_distance_meters:
            raise ValueError("upper distance must exceed included distance")
        if (self.upper_distance_meters - self.included_distance_meters) % 1000:
            raise ValueError("taxi reference distance delta must be whole kilometres")
        return self

    def derived_range(self) -> MoneyRangeFen:
        extra_km = (self.upper_distance_meters - self.included_distance_meters) // 1000
        return MoneyRangeFen(
            min_fen=self.base_fare_range_fen.min_fen,
            max_fen=self.base_fare_range_fen.max_fen + extra_km * self.per_km_fen,
        )


class LocalTransportReference(ReferenceEntry):
    city: str = Field(min_length=1)
    mode: Literal["taxi", "public_transit"]
    unit: str = Field(min_length=1)
    range_fen: MoneyRangeFen
    published_range_fen: MoneyRangeFen | None = None
    taxi_fare_inputs: TaxiFareInputs | None = None

    @model_validator(mode="after")
    def validate_mode_derivation(self) -> "LocalTransportReference":
        if self.mode == "taxi":
            if self.taxi_fare_inputs is None or self.published_range_fen is not None:
                raise ValueError("taxi requires fare inputs only")
            if self.range_fen != self.taxi_fare_inputs.derived_range():
                raise ValueError("taxi range must follow the published fare formula")
        else:
            if self.published_range_fen is None or self.taxi_fare_inputs is not None:
                raise ValueError("public_transit requires a published range only")
            if self.range_fen != self.published_range_fen:
                raise ValueError("public_transit range must equal the published range")
        return self


class IntercityReference(ReferenceEntry):
    from_city: str = Field(min_length=1)
    to_city: str = Field(min_length=1)
    mode: IntercityMode
    range_fen: MoneyRangeFen
    price_basis: Literal["reference"] = "reference"


class ReverseIntercityRule(ReferenceEntry):
    rule_id: Literal["explicit_reverse_direction_only"]
    lookup_direction: Literal["destination_to_origin"]
    requires_explicit_reverse_key: Literal[True]
    allow_outbound_amount_reuse: Literal[False]
    output_price_basis: Literal["reference"]
    missing_behavior: Literal["missing"]


class ReferenceCatalog(FrozenModel):
    schema_version: Literal["1"]
    catalog_version: str = Field(min_length=1)
    effective_date: date
    review_status: ReviewStatus
    sources: tuple[SourceRecord, ...]
    accommodation: tuple[AccommodationReference, ...]
    admission: tuple[AdmissionReference, ...]
    meals: tuple[MealReference, ...]
    local_transport: tuple[LocalTransportReference, ...]
    intercity: tuple[IntercityReference, ...] = ()
    reverse_intercity_rules: tuple[ReverseIntercityRule, ...]

    @model_validator(mode="after")
    def validate_catalog(self) -> "ReferenceCatalog":
        source_ids = [source.source_id for source in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("duplicate source_id")
        available_sources = set(source_ids)
        entry_groups = (
            self.accommodation,
            self.admission,
            self.meals,
            self.local_transport,
            self.intercity,
            self.reverse_intercity_rules,
        )
        for entry in (item for group in entry_groups for item in group):
            missing = set(entry.source_ids) - available_sources
            if missing:
                raise ValueError(f"unknown source_ids: {sorted(missing)}")

        unique_keys = (
            [
                (item.city, item.area_identity, item.period_class, item.date_start, item.date_end)
                for item in self.accommodation
            ],
            [item.place_identity for item in self.admission],
            [(item.city, item.meal) for item in self.meals],
            [(item.city, item.mode) for item in self.local_transport],
            [(item.from_city, item.to_city, item.mode) for item in self.intercity],
            [item.rule_id for item in self.reverse_intercity_rules],
        )
        for keys in unique_keys:
            if len(keys) != len(set(keys)):
                raise ValueError("duplicate reference lookup key")

        date_rows: dict[tuple[str, str], list[AccommodationReference]] = {}
        for item in self.accommodation:
            if item.period_class == "date_period":
                date_rows.setdefault((item.city, item.area_identity), []).append(item)
        for rows in date_rows.values():
            ordered = sorted(rows, key=lambda item: item.date_start or date.min)
            for previous, current in zip(ordered, ordered[1:]):
                if previous.date_end is not None and current.date_start is not None:
                    if current.date_start <= previous.date_end:
                        raise ValueError("overlapping accommodation date periods")
        return self
