"""Pydantic schemas for the trip workflow."""

from __future__ import annotations

import re
from datetime import date
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    field_validator,
    model_serializer,
    model_validator,
)

from src.cost_estimate.models import CostEstimateSnapshotV1
from src.cost_sources.models import SourceObservation

WorkflowResultType = Literal[
    "PLAN_READY",
    "NO_CANDIDATES",
    "NO_USABLE_ROUTE",
]

RequestedCommuteMode = Literal["driving", "transit", "cycling"]
EffectiveCommuteMode = Literal["driving", "transit", "walking", "cycling"]
TransitStepKind = Literal["walking", "bus", "rail", "other"]
TransitDetailQuality = Literal["complete", "partial", "missing"]
PoiPreferenceTag = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=40),
]

_TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _optional_must_include_place_id(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("must_include.place_id must be a positive integer")
    return value


def parse_hhmm(value: str | None) -> int | None:
    if value is None:
        return None
    match = _TIME_PATTERN.fullmatch(value.strip())
    if match is None:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


class RestWindow(BaseModel):
    """User preferred rest window. v0.8.9 supports only all-day matching."""
    days: str = "all"
    start: str
    end: str

    @field_validator("days")
    @classmethod
    def _validate_days(cls, value: str) -> str:
        days = value.strip()
        if days != "all":
            raise ValueError('rest_windows.days must be "all"')
        return days

    @field_validator("start", "end")
    @classmethod
    def _validate_time(cls, value: str) -> str:
        time_value = value.strip()
        if parse_hhmm(time_value) is None:
            raise ValueError("time must use HH:MM format")
        return time_value

    @model_validator(mode="after")
    def _validate_window(self) -> "RestWindow":
        start = parse_hhmm(self.start)
        end = parse_hhmm(self.end)
        if start is None or end is None:
            return self
        if start >= end:
            raise ValueError("rest_windows.start must be before end")
        duration = end - start
        if duration < 30 or duration > 240:
            raise ValueError("rest window length must be 30 minutes to 4 hours")
        return self


class MustIncludeItem(BaseModel):
    """A user-requested place anchor from structured clients."""
    place_id: int | None = None
    name: str

    @field_validator("place_id", mode="before")
    @classmethod
    def _validate_place_id(cls, value: Any) -> int | None:
        return _optional_must_include_place_id(value)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("must_include.name must not be empty")
        if len(name) > 100:
            raise ValueError("must_include.name must be at most 100 characters")
        return name


class AccommodationRequest(BaseModel):
    """User-provided accommodation anchor input."""

    name: str | None = None
    place_id: int | None = None
    latitude: float | None = None
    longitude: float | None = None


class LodgingDisplayGroup(BaseModel):
    kind: Literal["economy", "mid", "high", "homestay"]
    label: str
    hotels: list[str]
    nightly_low_cny: int | None = None
    nightly_high_cny: int | None = None

    @model_validator(mode="after")
    def _nightly_prices_together(self) -> "LodgingDisplayGroup":
        if (self.nightly_low_cny is None) != (self.nightly_high_cny is None):
            raise ValueError(
                "nightly_low_cny and nightly_high_cny must both be set or both be None"
            )
        return self


class LodgingDisplayNotice(BaseModel):
    kind: Literal["user_lodging_unmatched", "holiday"]
    text: str


class LodgingDisplay(BaseModel):
    layout: Literal["tiers", "homestay", "brands", "user_lodging", "area_only"]
    area_label: str
    caption: str = ""
    groups: list[LodgingDisplayGroup] = Field(default_factory=list)
    round_trip_minutes_low: int | None = None
    round_trip_minutes_high: int | None = None
    notices: list[LodgingDisplayNotice] = Field(default_factory=list)

    @model_validator(mode="after")
    def _round_trip_together(self) -> "LodgingDisplay":
        if (self.round_trip_minutes_low is None) != (
            self.round_trip_minutes_high is None
        ):
            raise ValueError(
                "round_trip_minutes_low and round_trip_minutes_high must both be set or both be None"
            )
        return self


class AccommodationSuggestion(BaseModel):
    """Resolved accommodation anchor.

    ``name`` may be the placeholder ``用户指定住宿位置`` for coordinate-only
    input. P3 deterministic copy must render that placeholder specially.
    """

    name: str
    latitude: float
    longitude: float
    source: Literal["user_specified", "auto_recommended"]
    reason: str = ""
    user_input_unmatched: str = ""
    display: LodgingDisplay | None = None

    @model_serializer(mode="wrap")
    def _omit_empty_display(self, handler):
        payload = handler(self)
        if payload.get("display") is None:
            payload.pop("display", None)
        return payload


class AccommodationAnchor(BaseModel):
    suggestion: AccommodationSuggestion
    place_id: int | None = None
    location_precision: Literal["point", "area", "unknown"] = "unknown"


class AccommodationPlanningContext(BaseModel):
    policy_version: Literal["accommodation-route-v1"] = "accommodation-route-v1"
    state: Literal["fixed", "auto_candidates", "user_unresolved", "auto_unavailable"]
    anchors: list[AccommodationAnchor] = Field(default_factory=list)
    urban_core: tuple[float, float] | None = None
    reason_code: str = ""


class DayTrafficPolicy(BaseModel):
    urban_core: tuple[float, float] | None = None
    companion_route_sources: dict[int, str] = Field(default_factory=dict)
    version: Literal["accommodation-route-v1"] = "accommodation-route-v1"
    day_kind: Literal["urban", "excursion", "unknown"]
    classification_basis: str
    daily_limit_minutes: int = Field(gt=0)
    poi_leg_limits: dict[str, int]
    access_leg_limits: dict[str, int]
    resolution_state: Literal["fixed", "auto_candidates", "user_unresolved", "auto_unavailable"]
    location_precision: Literal["point", "area", "unknown"] = "unknown"


class TrainOption(BaseModel):
    """One recommended direct train returned by the runtime provider."""

    train_no: str
    departure_time: str
    arrival_time: str
    duration_minutes: int
    departure_station: str
    arrival_station: str
    second_class_price: float | None = None
    first_class_price: float | None = None


class FlightOption(BaseModel):
    """One recommended scheduled flight."""

    flight_no: str
    airline: str
    departure_time: str
    arrival_time: str
    duration_minutes: int
    departure_airport: str
    arrival_airport: str


TrainClass = Literal["high_speed", "ordinary"]
FLIGHT_FARE_PENDING = "票价待确认"


class TransportModeSummary(BaseModel):
    """Mode-level intercity transport recommendation."""

    mode: Literal["train", "flight"]
    min_duration_minutes: int
    price_range: str
    price_source: Literal["realtime", "static_reference"]
    daily_count: int
    data_source: Literal["realtime", "static_fallback"]
    availability_status: Literal[
        "available_at_query",
        "sold_out_at_query",
        "unknown",
    ] = "unknown"
    availability_checked_at: str | None = None
    top_options: list[TrainOption] | list[FlightOption] = Field(default_factory=list)
    train_class: TrainClass | None = None
    reference_date: date | None = None

    @model_serializer(mode="wrap")
    def _omit_empty_train_metadata(self, handler):
        payload = handler(self)
        for field in ("train_class", "reference_date"):
            if payload.get(field) is None:
                payload.pop(field, None)
        return payload


class TransportSuggestion(BaseModel):
    """Optional deterministic intercity recommendation attached to each plan."""

    from_city: str
    to_city: str
    query_date: str | None = None
    modes: list[TransportModeSummary] = Field(default_factory=list)
    source: Literal["realtime", "mixed", "static_fallback"]
    cached_at: str | None = None
    direct_service_absent: bool = Field(default=False, exclude=True)
    # The cost adapter retains the flight scenario when no schedule duration is available.
    flight_fare_pending: bool = Field(default=False, exclude=True)


class TripRequest(BaseModel):
    """User intent parsed into structured fields."""
    from_city: str = ""
    to_city: str = "重庆"
    start_date: str | None = None
    end_date: str | None = None
    days: int = 3
    people_count: int = 1
    preferences: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)
    notes: str = ""
    commute_mode: RequestedCommuteMode = "driving"
    must_include: list[MustIncludeItem] = Field(default_factory=list)
    daily_start: str | None = None
    daily_end: str | None = None
    rest_windows: list[RestWindow] = Field(default_factory=list)
    accommodation: AccommodationRequest | None = None

    @field_validator("daily_start", "daily_end")
    @classmethod
    def _validate_daily_time(cls, value: str | None) -> str | None:
        if value is None:
            return None
        time_value = value.strip()
        if parse_hhmm(time_value) is None:
            raise ValueError("time must use HH:MM format")
        return time_value

    @field_validator("must_include", mode="before")
    @classmethod
    def _clean_must_include(cls, value):
        from src.agents.poi_alias import normalize_place_name

        if value is None:
            return []
        if not isinstance(value, list):
            return value
        cleaned = []
        seen_ids: set[int] = set()
        seen_names: set[str] = set()
        for item in value:
            if isinstance(item, MustIncludeItem):
                raw_name = item.name
                place_id = item.place_id
                raw_item = item.model_dump()
            elif isinstance(item, dict):
                raw_name = item.get("name")
                place_id = item.get("place_id")
                raw_item = dict(item)
            else:
                cleaned.append(item)
                continue
            explicit_id = _optional_must_include_place_id(place_id)
            if not isinstance(raw_name, str):
                cleaned.append(item)
                continue
            name = raw_name.strip()
            if not name:
                continue
            normalized = normalize_place_name(name)
            if not normalized:
                continue
            if explicit_id is not None:
                if explicit_id in seen_ids:
                    continue
                seen_ids.add(explicit_id)
                seen_names.add(normalized)
            elif normalized in seen_names:
                continue
            else:
                seen_names.add(normalized)
            raw_item["name"] = name
            raw_item["place_id"] = explicit_id
            cleaned.append(raw_item)
        if len(cleaned) > 5:
            raise ValueError("must_include supports at most 5 items")
        return cleaned

    @model_validator(mode="after")
    def _validate_time_preferences(self) -> "TripRequest":
        if len(self.rest_windows) > 2:
            raise ValueError("rest_windows supports at most 2 windows")
        daily_start = parse_hhmm(self.daily_start)
        daily_end = parse_hhmm(self.daily_end)
        if daily_start is not None and daily_end is not None:
            if daily_start >= daily_end:
                raise ValueError("daily_start must be before daily_end")
            for window in self.rest_windows:
                window_start = parse_hhmm(window.start)
                window_end = parse_hhmm(window.end)
                if (
                    window_start is not None
                    and window_end is not None
                    and (window_start < daily_start or window_end > daily_end)
                ):
                    raise ValueError(
                        "rest_windows must be within daily_start and daily_end"
                    )
        return self

    def has_time_preferences(self) -> bool:
        return bool(self.daily_start or self.daily_end or self.rest_windows)

    def daily_span_minutes(self) -> int | None:
        daily_start = parse_hhmm(self.daily_start)
        daily_end = parse_hhmm(self.daily_end)
        if daily_start is None or daily_end is None:
            return None
        return daily_end - daily_start

    def rest_window_minutes(self) -> int:
        total = 0
        for window in self.rest_windows:
            start = parse_hhmm(window.start)
            end = parse_hhmm(window.end)
            if start is not None and end is not None and end > start:
                total += end - start
        return total

    def time_preferences_payload(self) -> dict | None:
        if not self.has_time_preferences():
            return None
        payload: dict = {}
        if self.daily_start:
            payload["daily_start"] = self.daily_start
        if self.daily_end:
            payload["daily_end"] = self.daily_end
        if self.rest_windows:
            payload["rest_windows"] = [
                window.model_dump()
                for window in self.rest_windows
            ]
        return payload


class CandidatePlace(BaseModel):
    """A canonical place plus optional summary evidence."""
    place_id: int
    canonical_place_id: int | None = None
    name: str
    place_type: str
    district: str | None = None
    category_tags: list[str] = Field(default_factory=list)
    base_priority: int = 0
    typical_visit_minutes: int | None = None
    typical_visit_source: str | None = None
    typical_visit_confidence: float | None = None
    # Runtime-only evidence receipt; never part of public result/prompt schemas.
    typical_visit_applicable: bool = Field(default=False, exclude=True)
    typical_visit_fact_count: int = Field(default=0, exclude=True)
    typical_visit_candidate_count: int = Field(default=0, exclude=True)
    typical_visit_accepted_count: int = Field(default=0, exclude=True)
    typical_visit_reject_reasons: list[str] = Field(default_factory=list, exclude=True)
    typical_visit_observation: dict = Field(default_factory=dict, exclude=True)
    city: str | None = Field(default=None, exclude=True)
    address: str | None = Field(default=None, exclude=True)
    arrangement_evidence: list[dict] = Field(default_factory=list, exclude=True)
    mention_count: int = 0
    positive_count: int = 0
    negative_count: int = 0
    source_count: int = 0
    quality_score: float = 0.0
    recommend_score: float = 0.0
    effective_score: float = 0.0
    longitude: float | None = None
    latitude: float | None = None
    adcode: str | None = None
    amap_poi_id: str | None = None
    amap_rating: str | None = None
    amap_avg_price: str | None = None
    amap_open_time: str | None = None
    top_reasons: list[dict] = Field(default_factory=list)
    warnings: list[dict] = Field(default_factory=list)
    must_include: bool = False

    @model_validator(mode="after")
    def _reject_unhydrated_xhs_duration(self):
        # All CandidatePlace consumers, including the legacy dwell path, see
        # unknown until the source-bound retrieval batch admits evidence.
        if self.typical_visit_source == "xhs_median" and not self.typical_visit_applicable:
            self.typical_visit_minutes = None
            self.typical_visit_confidence = None
        return self


class PoiSelectionRequest(BaseModel):
    """Strict request facts exposed to POI_SELECTOR."""

    model_config = ConfigDict(extra="forbid")

    city: str = Field(min_length=1)
    days: StrictInt = Field(ge=1)
    preferences: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)


class PoiSelectionConstraints(BaseModel):
    """Deterministic membership and count constraints for POI_SELECTOR."""

    model_config = ConfigDict(extra="forbid")

    ordinary_target_count: StrictInt = Field(ge=0, le=30)
    must_include_place_ids: list[StrictInt] = Field(default_factory=list, max_length=5)
    eligible_place_ids: list[StrictInt] = Field(default_factory=list, max_length=65)

    @field_validator("must_include_place_ids", "eligible_place_ids")
    @classmethod
    def _require_unique_positive_ids(cls, value: list[int]) -> list[int]:
        if any(place_id <= 0 for place_id in value):
            raise ValueError("place IDs must be positive integers")
        if len(value) != len(set(value)):
            raise ValueError("place IDs must be unique")
        return value


class PoiSelectionCandidate(BaseModel):
    """Qualified retrieval fact exposed to POI_SELECTOR."""

    model_config = ConfigDict(extra="forbid")

    place_id: StrictInt = Field(gt=0)
    name: str = Field(min_length=1)
    place_type: str = Field(min_length=1)
    district: str = ""
    must_include: bool = False
    category_tags: list[str] = Field(default_factory=list)
    allowed_preference_tags: list[PoiPreferenceTag] = Field(default_factory=list)

    @field_validator("allowed_preference_tags")
    @classmethod
    def _require_unique_preference_tags(
        cls,
        value: list[PoiPreferenceTag],
    ) -> list[PoiPreferenceTag]:
        if len(value) != len(set(value)):
            raise ValueError("allowed_preference_tags must be unique")
        return value


class PoiSelectionInput(BaseModel):
    """Complete strict input contract for one disconnected selector call."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    request: PoiSelectionRequest
    constraints: PoiSelectionConstraints
    candidates: list[PoiSelectionCandidate] = Field(default_factory=list, max_length=65)

    @model_validator(mode="after")
    def _validate_membership_contract(self) -> "PoiSelectionInput":
        candidate_ids = [candidate.place_id for candidate in self.candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("candidates place_id values must be unique")
        if candidate_ids != self.constraints.eligible_place_ids:
            raise ValueError(
                "eligible_place_ids must exactly match candidates place_id order"
            )

        must_ids = self.constraints.must_include_place_ids
        must_id_set = set(must_ids)
        ordinary_ids = [place_id for place_id in candidate_ids if place_id not in must_id_set]
        if candidate_ids != [*ordinary_ids, *must_ids]:
            raise ValueError(
                "qualified must-include candidates must be appended in canonical input order"
            )

        for candidate in self.candidates:
            expected_must = candidate.place_id in must_id_set
            if candidate.must_include != expected_must:
                raise ValueError("candidate must_include flag conflicts with constraints")
            has_must_tag = "MUST_GO" in candidate.allowed_preference_tags
            if has_must_tag != expected_must:
                raise ValueError("MUST_GO is allowed exactly for must-include candidates")

        if self.schema_version == "1.1":
            minimum, expected_target = selector_ordinary_bounds(
                self.request.days, len(ordinary_ids), len(must_ids), self.request.day_slot_limit)
            if (self.constraints.ordinary_min_count, self.constraints.ordinary_max_count) != (minimum, expected_target):
                raise ValueError("ordinary bounds do not match policy")
        else:
            expected_target = min(len(ordinary_ids), self.request.days * 5, 30)
        if self.constraints.ordinary_target_count != expected_target:
            raise ValueError("ordinary_target_count does not match the frozen formula")
        if len(ordinary_ids) > 60:
            raise ValueError("ordinary selector input must not exceed 60 candidates")
        return self


class PoiSelectionItem(BaseModel):
    """One selected canonical ID and its evidence-bounded preference tags."""

    model_config = ConfigDict(extra="forbid")

    place_id: StrictInt = Field(gt=0)
    preference_tags: list[PoiPreferenceTag] = Field(default_factory=list)

    @field_validator("preference_tags")
    @classmethod
    def _require_unique_tags(
        cls,
        value: list[PoiPreferenceTag],
    ) -> list[PoiPreferenceTag]:
        if len(value) != len(set(value)):
            raise ValueError("preference_tags must be unique")
        return value


class PoiSelectionResult(BaseModel):
    """Narrow selector output; order is preference only, never route order."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    selected: list[PoiSelectionItem] = Field(default_factory=list, max_length=35)


class VisitProfile(BaseModel):
    """Normalized planning estimate; never written back as canonical evidence."""
    place_id: int
    visit_minutes: int = Field(ge=0)
    basis: Literal["canonical", "type_estimate", "full_day_evidence", "unresolved_play_style", "window_conflict"]
    source: str | None = None
    confidence: float | None = None
    uncertainty_minutes: int = Field(ge=0)
    singleton_eligible: bool = False
    policy_version: Literal["selector-route-v2"] = "selector-route-v2"

    arrangement: Literal["standard", "full_day"] = "standard"
    play_style: dict | None = None
    play_styles: list[dict] = Field(default_factory=list)
    evidence: list[dict] = Field(default_factory=list)
    nested_place_ids: list[int] = Field(default_factory=list)
    contained_place_ids: list[int] = Field(default_factory=list)
    arrangement_warnings: list[str] = Field(default_factory=list)


class ExcursionTimePolicy(BaseModel):
    version: Literal["excursion-time-v1", "urban-night-time-v1"] = "excursion-time-v1"
    pace: Literal["relaxed", "default", "compact"]
    start_minute: int
    end_minute: int
    start_source: Literal["system", "user"]
    end_source: Literal["system", "user"]
    reference_span_minutes: int
    reference_start_minute: int | None = None
    reference_end_minute: int | None = None
    system_break_intervals: list[tuple[int, int]] = Field(default_factory=list)
    original_daily_start: str | None = None
    original_daily_end: str | None = None
    original_rest_windows: list[dict] = Field(default_factory=list)
    break_intervals: list[tuple[int, int]] = Field(default_factory=list)
    break_minutes: int = 0
    violations: list[str] = Field(default_factory=list)


class SelectorVisitProfile(BaseModel):
    place_id: int
    visit_minutes: int = Field(ge=15, le=480)
    basis: Literal["canonical", "type_estimate"]
    source: str | None = None
    confidence: float | None = None
    uncertainty_minutes: int = Field(ge=0)
    singleton_eligible: bool = False
    policy_version: Literal["selector-route-v2"] = "selector-route-v2"


class AccessLeg(BaseModel):
    direction: Literal["outbound", "inbound"]
    anchor_kind: Literal["accommodation"] = "accommodation"
    anchor_source: Literal["user_specified", "auto_recommended"]
    anchor_latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    anchor_longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    place_id: int = Field(gt=0)
    mode: EffectiveCommuteMode
    duration_source: Literal["estimate", "amap"] = "estimate"
    duration_minutes: int = Field(ge=0)
    distance_meters: int = Field(ge=0)
    estimate_policy_version: Literal["selector-route-v2"] = "selector-route-v2"
    # INTERNAL durable walking-proof receipt. Survives model_dump/rebuild.
    # Public Result 2.3 does not project access legs; writer/prompt omit it.
    walk_proof_receipt: str | None = None
    # Runtime-only alias of walk_proof_receipt. Excluded from persistence.
    walk_proof_decision: str | None = Field(default=None, exclude=True)


class DayFeasibility(BaseModel):
    ordered_place_ids: list[int]
    policy_version: Literal["selector-route-v2"] = "selector-route-v2"
    visit_minutes: int
    uncertainty_minutes: int
    poi_commute_minutes: int
    access_minutes: int | None
    capacity_minutes: int
    load_limit_minutes: int
    load_minutes: int
    access_status: Literal["estimated", "precise", "mixed", "unknown"]
    violations: list[str] = Field(default_factory=list)
    singleton_eligible: bool = False
    precise_leg_count: int = 0
    estimated_leg_count: int = 0
    break_minutes: int = 0

    @property
    def feasible(self) -> bool:
        return not self.violations


def selector_ordinary_bounds(days: int, ordinary: int, must: int, slots: int) -> tuple[int, int]:
    maximum = min(ordinary, 30, max(0, days * slots - must) + min(days, 3))
    return min(maximum, max(0, days - must)), maximum


class PoiSelectionRequestV11(PoiSelectionRequest):
    notes: str = ""
    pace: Literal["relaxed", "default", "compact"] = "default"
    requested_commute_mode: RequestedCommuteMode = "driving"
    effective_commute_mode: EffectiveCommuteMode = "driving"
    commute_mode_basis: str = "feature_disabled"
    accommodation: AccommodationSuggestion | None = None
    time_preferences: dict[str, Any] = Field(default_factory=dict)
    time_preferences_enabled: bool = False
    day_slot_limit: int = Field(ge=2, le=5)


class PoiSelectionConstraintsV11(PoiSelectionConstraints):
    ordinary_min_count: StrictInt = Field(ge=0, le=30)
    ordinary_max_count: StrictInt = Field(ge=0, le=30)


class PoiSelectionCandidateV11(PoiSelectionCandidate):
    latitude: float | None = None
    longitude: float | None = None
    adcode: str | None = None
    visit_profile: SelectorVisitProfile
    experience: list[Annotated[str, StringConstraints(max_length=80)]] = Field(default_factory=list, max_length=2)


class PoiSelectionInputV11(PoiSelectionInput):
    schema_version: Literal["1.1"] = "1.1"
    request: PoiSelectionRequestV11
    constraints: PoiSelectionConstraintsV11
    candidates: list[PoiSelectionCandidateV11] = Field(default_factory=list, max_length=65)


class PoiSelectionResultV11(PoiSelectionResult):
    schema_version: Literal["1.1"] = "1.1"


class CandidateGroup(BaseModel):
    """Evidence-backed candidate input for one generated plan."""
    label: str
    internal_theme: str = ""
    candidates: list[CandidatePlace] = Field(default_factory=list)


class RetrievalResult(BaseModel):
    """Output of Data Retrieval agent."""
    city: str
    candidates: list[CandidatePlace]
    route_planning_candidates: list[CandidatePlace] = Field(
        default_factory=list,
        exclude=True,
    )
    evidence_summary: str = ""
    candidate_groups: list[CandidateGroup] = Field(default_factory=list)
    qualified_candidate_count: int = 0
    in_response_candidate_overlap: float | None = None
    diversity_gap: bool = False


class CompositionStop(BaseModel):
    """Writing role for one locked route stop."""
    place_id: int
    name: str
    role: str
    meal_slot: str | None = None
    emphasis: str = "normal"
    writing_hint: str | dict = ""


class CompositionCommute(BaseModel):
    """Writing guidance for one locked commute leg."""
    from_place_id: int
    to_place_id: int
    duration_minutes: int
    mode: EffectiveCommuteMode = "driving"
    style: str
    must_mention: bool = False
    # Short ready-to-copy transition; detailed line/stop truth stays structured.
    transit_transition: str | None = None


class CompositionDay(BaseModel):
    """Composition guidance for one locked travel day."""
    day: int
    theme_code: str
    theme_label: str
    stops: list[CompositionStop] = Field(default_factory=list)
    commutes: list[CompositionCommute] = Field(default_factory=list)
    writing_notes: list[str] = Field(default_factory=list)


class CompositionBlueprint(BaseModel):
    """Deterministic itinerary-composition guidance for one plan."""
    version: str = "v0.6.9"
    plan_label: str
    days: list[CompositionDay] = Field(default_factory=list)


PoiIdentityRelationType = Literal[
    "canonical_same",
    "alias_same",
    "parent_child",
    "nearby_distinct",
    "unknown",
]


class PoiIdentityRelation(BaseModel):
    """Deterministic relationship between a locked POI and another POI name."""
    source_name: str
    target_name: str
    relation: PoiIdentityRelationType
    reason: str = ""


class PoiIdentityResult(BaseModel):
    """POI identity graph used before writing and review."""
    version: str = "v0.6.10"
    plan_label: str
    relations: list[PoiIdentityRelation] = Field(default_factory=list)


BudgetStatus = Literal[
    "within_budget",
    "relaxed_exception",
    "pruned",
    "infeasible",
]


class BudgetDayResult(BaseModel):
    """Commute-budget decision for one locked travel day."""
    day: int
    status: BudgetStatus
    budget_minutes: int
    commute_minutes: int
    overage_ratio: float = 0.0
    removed_place_ids: list[int] = Field(default_factory=list)
    removed_place_names: list[str] = Field(default_factory=list)
    reason: str = ""


class BudgetResult(BaseModel):
    """Budget resolution for one route plan before prose generation."""
    version: str = "v0.6.10"
    plan_label: str
    days: list[BudgetDayResult] = Field(default_factory=list)


class PoiNarrativeFragment(BaseModel):
    """Backend-owned POI prose fragment keyed by locked route identity.

    This registry is internal workflow state.  It gives Gate/Review/Repair an
    immutable ownership key without exposing another frontend response field.
    """

    plan_index: int = Field(ge=1)
    day: int = Field(ge=1)
    place_id: int = Field(ge=1)
    text: str
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    source: Literal[
        "writer",
        "deterministic_completion",
        "deterministic_food_none",
        "fragment_repair",
        "safe_renderer",
    ] = "writer"


class PackingChecklistGroup(BaseModel):
    """One packing category on a plan. Caps are enforced by the sanitizer."""

    category: str
    items: list[str] = Field(default_factory=list)

    @field_validator("category", mode="before")
    @classmethod
    def _strip_category(cls, value):
        if not isinstance(value, str):
            return value
        return value.strip()

    @field_validator("items", mode="before")
    @classmethod
    def _strip_items(cls, value):
        if not isinstance(value, list):
            return value
        return [item.strip() if isinstance(item, str) else item for item in value]


class TravelTip(BaseModel):
    """Public travel tip. Internal Writer drafts also carry evidence_ref."""

    title: str
    content: str

    @field_validator("title", "content", mode="before")
    @classmethod
    def _strip_text(cls, value):
        if not isinstance(value, str):
            return value
        return value.strip()


class PlanOutput(BaseModel):
    """One generated travel plan."""
    plan_name: str
    plan_text: str
    # Writer-generated whole-trip summary (v0.9.0.1). Empty means "use the
    # deterministic fallback"; old plan_json rows without the field stay valid.
    summary: str = ""
    used_place_ids: list[int] = Field(default_factory=list)
    used_place_names: list[str] = Field(default_factory=list)
    day_place_names: list[list[str]] = Field(default_factory=list)
    composition_blueprint: CompositionBlueprint | None = None
    poi_identity_result: PoiIdentityResult | None = None
    budget_result: BudgetResult | None = None
    accommodation: AccommodationSuggestion | None = None
    transport: TransportSuggestion | None = None
    packing_checklist: list[PackingChecklistGroup] | None = None
    travel_tips: list[TravelTip] | None = None
    poi_fragments: list[PoiNarrativeFragment] = Field(
        default_factory=list,
        exclude=True,
    )
    # Exact code-written 【住宿】 block from this process. The publish gate
    # exempts that span only; reloaded plan JSON does not carry it.
    code_lodging_section: str = Field(default="", exclude=True)

    @field_validator("transport")
    @classmethod
    def _omit_transport_without_direct_service(
        cls, value: TransportSuggestion | None,
    ) -> TransportSuggestion | None:
        if value is not None and value.direct_service_absent:
            return None
        return value

    def poi_fragment(
        self,
        *,
        plan_index: int,
        day: int,
        place_id: int,
    ) -> PoiNarrativeFragment | None:
        key = (plan_index, day, place_id)
        return next(
            (
                fragment
                for fragment in self.poi_fragments
                if (
                    fragment.plan_index,
                    fragment.day,
                    fragment.place_id,
                )
                == key
            ),
            None,
        )


class TransitStep(BaseModel):
    """One normalized provider-ordered fact inside a public-transit leg."""
    kind: TransitStepKind
    duration_minutes: int | None = Field(default=None, ge=0)
    distance_meters: int | None = Field(default=None, ge=0)
    line_name: str | None = None
    provider_type: str | None = None
    from_stop: str | None = None
    to_stop: str | None = None
    stop_count: int | None = Field(default=None, ge=1)

    @field_validator(
        "line_name",
        "provider_type",
        "from_stop",
        "to_stop",
        mode="before",
    )
    @classmethod
    def _normalize_nullable_text(cls, value):
        if value is None or not isinstance(value, str):
            return None
        text = value.strip()
        return text or None

    @model_validator(mode="after")
    def _clear_walking_ride_fields(self) -> "TransitStep":
        if self.kind == "walking":
            self.line_name = None
            self.provider_type = None
            self.from_stop = None
            self.to_stop = None
            self.stop_count = None
        return self


class CommuteLeg(BaseModel):
    """One deterministic commute edge in a planned travel day."""
    from_place_id: int
    to_place_id: int
    from_name: str
    to_name: str
    distance_meters: int
    duration_minutes: int
    mode: EffectiveCommuteMode = "driving"
    source: str = "estimate"
    note: str = ""
    encoded_polyline: str = ""
    transit_steps: list[TransitStep] = Field(default_factory=list)
    transit_detail_quality: TransitDetailQuality = "missing"
    # Internal provenance only: true exclusively for a validated detail-v2 hit.
    transit_detail_cache_hit: bool = False
    # Internal P2 fact only. Public projection ignores this field; P3 will
    # consume it before creating the immutable Cost Estimate Snapshot.
    fare_observation: SourceObservation | None = None
    # INTERNAL durable walking-proof receipt. Survives model_dump/rebuild for
    # point and access reuse. Public Result 2.3 and writer/prompt omit it.
    # Never copy this value onto physical transit_steps.
    walk_proof_receipt: str | None = None
    # Runtime-only alias of walk_proof_receipt. Excluded from persistence.
    walk_proof_decision: str | None = Field(default=None, exclude=True)


StopKind = Literal["lunch", "dinner", "snack", "night", "sight"]


class TimelineMove(BaseModel):
    mode: str = "步行"
    minutes: int | None = None


class DayTimelineItem(BaseModel):
    """One scheduled stop, or a rest (place_id None), with clock times."""
    kind: Literal["lunch", "dinner", "snack", "night", "sight", "rest"]
    place_id: int | None = None
    arrive: str
    stay_minutes: int
    # Minutes spent waiting for a meal or dark before ``arrive``.
    wait_minutes: int = 0
    rest_via_lodge: bool = False
    # The leg after this item; on the last item it is the way back.
    move_to_next: TimelineMove | None = None


class DayTimeline(BaseModel):
    """v0.15.1: the clock the route planner already computed for one day.

    ``scheduler`` rows come from route_planner.schedule_day; ``estimated``
    rows are rebuilt by code for routes that never had one.
    """
    source: Literal["scheduler", "estimated"] = "scheduler"
    start: str
    depart_move: TimelineMove | None = None
    items: list[DayTimelineItem] = Field(default_factory=list)
    return_at: str


class RouteDayGroup(BaseModel):
    """Locked place assignment for one day of one generated plan."""
    day: int
    area: str = ""
    adcode: str | None = None
    places: list[CandidatePlace] = Field(default_factory=list)
    commute_legs: list[CommuteLeg] = Field(default_factory=list)
    commute_minutes: int = 0
    commute_notes: list[str] = Field(default_factory=list)
    time_hints: list[str] = Field(default_factory=list)
    access_legs: list[AccessLeg] = Field(default_factory=list)
    day_feasibility: DayFeasibility | None = None
    traffic_policy: DayTrafficPolicy | None = None
    time_policy: ExcursionTimePolicy | None = None
    visit_profiles: list[VisitProfile] = Field(default_factory=list)
    # Model-route stop roles by place_id, after the planner and code
    # adjustments. Included in model_dump, so plan_json keeps it. Old rows
    # omit the key and stay empty, which keeps meal slots guessed by order.
    stop_kinds: dict[int, StopKind] = Field(default_factory=dict)
    # v0.15.1: model-route clock, kept in plan_json. Old rows and selector
    # routes have none; route_timeline.day_timeline() estimates one.
    timeline: DayTimeline | None = None
    # Facts for fixed notices on the already scheduled day.
    excursion_commute_over_limit: bool = Field(default=False, exclude=True)
    return_after_pace_limit: bool = Field(default=False, exclude=True)
    empty_day_reason: Literal["all_closed", "insufficient_candidates"] | None = Field(default=None, exclude=True)

    @field_validator("stop_kinds", mode="before")
    @classmethod
    def _coerce_stop_kinds(cls, value: Any) -> Any:
        if value is None:
            return {}
        if not isinstance(value, dict):
            return value
        cleaned: dict[int, str] = {}
        for key, kind in value.items():
            if isinstance(key, bool) or not isinstance(key, (int, str)):
                continue
            try:
                place_id = int(str(key).strip())
            except (TypeError, ValueError):
                continue
            if place_id <= 0 or isinstance(kind, bool) or not isinstance(kind, str):
                continue
            text = kind.strip()
            if text in {"lunch", "dinner", "snack", "night", "sight"}:
                cleaned[place_id] = text
        return cleaned


SelectedRouteStatus = Literal["USED", "DROPPED"]
SelectedDropReason = Literal[
    "HARD_INELIGIBLE",
    "TEMPORALLY_UNSCHEDULABLE",
    "CAPACITY_LIMIT",
    "ROUTE_FEASIBILITY_LIMIT",
    "NOT_CHOSEN_FOR_FINAL_ROUTE",
]
QualifiedSupplementReason = Literal["REPLACE_DROPPED", "FILL_EMPTY_DAY", "FILL_MUST_INCLUDE_DAY", "FILL_MEAL_SLOT"]


class SelectedRouteMembership(BaseModel):
    """Closed final disposition for one selector-provided place."""

    model_config = ConfigDict(extra="forbid")

    place_id: StrictInt = Field(gt=0)
    status: SelectedRouteStatus
    reason: SelectedDropReason | None = None

    @model_validator(mode="after")
    def _validate_status_reason(self) -> "SelectedRouteMembership":
        if (self.status == "USED") != (self.reason is None):
            raise ValueError("USED has no reason; DROPPED requires a closed reason")
        return self


class QualifiedRouteSupplement(BaseModel):
    """Closed authorization for one qualified place added by Route."""

    model_config = ConfigDict(extra="forbid")

    place_id: StrictInt = Field(gt=0)
    reason: QualifiedSupplementReason


class RouteMembershipLedger(BaseModel):
    """Internal selector-to-route membership ledger; never publicly projected."""

    model_config = ConfigDict(extra="forbid")

    selected_place_ids: list[StrictInt]
    selected: list[SelectedRouteMembership]
    supplemented: list[QualifiedRouteSupplement] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_closed_membership(self) -> "RouteMembershipLedger":
        selected_ids = list(self.selected_place_ids)
        disposition_ids = [item.place_id for item in self.selected]
        supplemented_ids = [item.place_id for item in self.supplemented]
        if any(place_id <= 0 for place_id in selected_ids):
            raise ValueError("selected place IDs must be positive")
        if len(selected_ids) != len(set(selected_ids)):
            raise ValueError("selected place IDs must be unique")
        if disposition_ids != selected_ids:
            raise ValueError("every selected ID needs exactly one ordered disposition")
        if len(supplemented_ids) != len(set(supplemented_ids)):
            raise ValueError("supplemented place IDs must be unique")
        if set(selected_ids).intersection(supplemented_ids):
            raise ValueError("selected places cannot also be supplemented")
        return self


class RoutePlan(BaseModel):
    """Deterministic locked route skeleton."""
    route_policy_version: Literal["selector-route-v2"] | None = None
    # Who locked the days. Old plan_json rows omit the key and stay selector.
    # Included in route model_dump, so plan_json keeps it. Cost snapshots do
    # not embed the route body; they keep using the route object in memory.
    planner_source: Literal["selector", "model"] = "selector"
    # Internal route evidence, included in Writer/Review context and snapshots.
    preference_coverage: list[dict[str, Any]] = Field(default_factory=list)
    accommodation_policy_version: Literal["accommodation-route-v1"] | None = None
    accommodation_anchor: AccommodationAnchor | None = None
    accommodation_resolution_state: str | None = None
    label: str
    day_groups: list[RouteDayGroup] = Field(default_factory=list)
    dropped_place_ids: list[int] = Field(default_factory=list)
    optimized: bool = True
    fallback_reason: str | None = None
    # The known closure reason, or None if the scheduler has no confirmed cause.
    missed_must_visits: dict[str, Literal["closed_all_days"] | None] = Field(default_factory=dict, exclude=True)
    membership_ledger: RouteMembershipLedger | None = Field(
        default=None,
        exclude=True,
    )
    # Nearby hotel names from the lodging search. Absent means that search
    # did not run. Not part of plan_json.
    nearby_lodging_hotel_names: list[str] | None = Field(
        default=None,
        exclude=True,
    )


PublishedVariant = Literal["normal", "safe"]
DeliveryStatus = Literal["NORMAL", "DEGRADED"]
SafeTrigger = Literal[
    "review_no_anchor",
    "fragment_repair_failed",
    "writer_failure",
    "speculative_no_publishable_draft",
    "preference_unverified",
]


class DeliveryMetadata(BaseModel):
    """Authoritative delivery semantics; detailed cause remains internal."""

    published_variant: PublishedVariant = "normal"
    delivery_status: DeliveryStatus = "NORMAL"
    safe_trigger: SafeTrigger | None = None

    @model_validator(mode="after")
    def validate_combination(self) -> "DeliveryMetadata":
        if self.published_variant == "safe":
            if self.delivery_status != "DEGRADED" or self.safe_trigger is None:
                raise ValueError("safe delivery requires DEGRADED and a safe_trigger")
        elif self.safe_trigger is not None:
            raise ValueError("normal delivery cannot carry a safe_trigger")
        return self

    @classmethod
    def from_metrics(cls, metrics: dict[str, Any] | None) -> "DeliveryMetadata":
        values = metrics if isinstance(metrics, dict) else {}
        return cls(
            published_variant=values.get("published_variant", "normal"),
            delivery_status=values.get("delivery_status", "NORMAL"),
            safe_trigger=values.get("safe_trigger"),
        )


class WorkflowResult(BaseModel):
    """Final output of the full trip workflow."""
    trip_request: TripRequest
    plans: list[PlanOutput]
    result_type: WorkflowResultType = "PLAN_READY"
    review_notes: str = ""
    quality_metrics: dict = Field(default_factory=dict)
    delivery_metadata: DeliveryMetadata = Field(default_factory=DeliveryMetadata)
    route_plans: list[RoutePlan] = Field(default_factory=list)
    route_diagnostics: dict | None = Field(default=None, exclude=True)
    route_replay_data: dict | None = Field(default=None, exclude=True)
    weather_display: dict = Field(default_factory=dict)
    cost_estimate_snapshots: list[CostEstimateSnapshotV1] = Field(
        default_factory=list,
        exclude=True,
    )
    record_id: int | None = None
    # RollingGo tier lookup for this process. The budget reads it; plan_json
    # does not store it.
    lodging_price_result: Any | None = Field(default=None, exclude=True)
