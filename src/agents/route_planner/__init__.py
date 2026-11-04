"""Standalone v0.14 route planner.

The model picks places and order from the candidates this module prepares.
Code owns travel time, arrival times, hard rules and one repair round.
``run_trip_workflow`` calls ``plan_route`` only when ``route_planner_enabled``
is on, and keeps the deterministic route when this result is not usable.
"""

from __future__ import annotations

import asyncio
import copy
import json
import math
import re
import time
from dataclasses import dataclass, field
from datetime import date

from src.agents import llm
from src.agents.activity_arrangement import resolve_profiles
from src.agents.area_candidates import (
    fetch_kid_places as _fetch_kid_places,
    fetch_nearby_restaurants as _fetch_nearby_restaurants,
    fetch_tea_houses as _fetch_tea_houses,
)
from src.agents.composition_blueprint import is_light_food_place
from src.agents.accommodation_policy import day_policy
from src.agents.pace import daily_commute_budget_minutes, detect_pace_mode, generation_base_mode
from src.agents.preferences import (
    CITYWALK,
    NIGHT_VIEW,
    candidate_is_family_place,
    candidate_is_night_place,
    candidate_is_tea_place,
    family_arrangement_active,
    food_preference_active,
    normalize_preferences,
)
from src.agents.route_feasibility import build_visit_profile
from src.agents.preference_notices import PROGRAM_PRE_DINNER_REST_HINT, PROGRAM_REST_HINT
from src.agents.route_planner.prompt import candidate_block, request_text, system_for
from src.agents.route_planning import (
    AmapRouteProvider,
    PreciseRoute,
    RoutePlanningMetrics,
    RouteProviderError,
    _route_planning_candidate_pool,
    _estimated_leg,
    _v2_access_key,
    _v2_finalize_day,
    _v2_make_plan,
    _v2_verify_traffic,
    valid_must_include_ids,
)
from src.agents.schema import (
    AccessLeg,
    AccommodationAnchor,
    AccommodationPlanningContext,
    AccommodationSuggestion,
    CandidatePlace,
    CommuteLeg,
    RetrievalResult,
    RoutePlan,
    TripRequest,
    parse_hhmm,
)
from src.config import Settings, get_settings

from src.agents.route_planner.rules import (
    LUNCH,
    LATE_LUNCH,
    DINNER,
    LATE_DINNER,
    PACE_START,
    DEFAULT_START,
    DEFAULT_END,
    EXCURSION_START,
    NIGHT_DINNER_FLOOR,
    NIGHT_DINNER_CEILING,
    PACE_SIGHT_HOURS,
    WALK_CAP_MINUTES,
    SHORT_WALK_MINUTES,
    SHORT_DRIVE_WALK_MINUTES,
    EMIT_ERROR_TEXT_LIMIT,
    MEAL_NEAR_KM,
    IDLE_LIMIT_MINUTES,
    PRE_DINNER_REST_MIN_WAIT,
    PRE_DINNER_REST_LEG_CAP,
    SIGHT_EXTEND_CAP,
    CLUSTER_RADIUS_KM,
    SCENIC_NEAR_KM,
    SAME_PLACE_KM,
    FAR_WALK_KM,
    KINDS,
    PACE_LIMIT,
    PACE_LABEL,
    OTHER_PREFS,
    TEA_MARKERS,
    AREA_TYPES,
    CLOSED_NAME,
    JSON_RETRY_HINT,
    REPAIR_TAIL,
    PlannerRules,
    sunset_minutes,
    _fmt,
    _clock,
    _start_day,
    _tea_requested,
    _family_requested,
    dinner_latest_minute,
    dinner_earliest_minute,
    day_has_night,
    schedule_start_minute,
    build_rules,
)

from src.agents.route_planner.places import (
    AreaCluster,
    hav_km,
    _coords,
    _place_id,
    _point_key,
    coerce_place_id,
    _names_contain,
    _closed,
    _scenic_eligible,
    _rating,
    _money,
    _dict_items,
    _as_candidate,
    _BLANK_NEARBY_EVIDENCE,
    _center,
    cluster_places,
    scenic_groups,
    _protected_ids,
)

from src.agents.route_planner.legs import (
    _quoted_route,
    _HOP_MODE,
    _HOP_QUOTE_MODE,
    _HOP_WORD,
    _ESTIMATE_MODE,
    _ESTIMATE_WORD,
    LegChoice,
    _hop_note,
    _estimate_note,
    _estimated_commute,
    _fallback_minutes,
    _chosen_hop,
    _synthetic_one_minute_walk,
    _meters,
    _commute_from_hop,
    _access_from_hop,
    _pair_hops,
    _hop_note_text,
    _commute_from_via_hops,
    _apply_scheduled_legs,
    CachingEstimator,
    AmapLegEstimator,
    EstimatorBackedProvider,
    _route_place,
    _choose_mode,
    resolve_legs,
    quoted_known_legs,
    quoted_known_access,
)

from src.agents.route_planner.schedule import (
    _stay,
    _movement_pairs,
    _lookup,
    _immediate_rest_before,
    _timeline_prev_place,
    _extend_prev_sight,
    _try_insert_dinner_rest,
    _fill_meal_gaps,
    _day_is_excursion,
    schedule_day,
    _meal_anchor,
    _next_anchor,
    repick_meals,
    _visitor_meal,
    _proper_meal,
    _idle_fill_counts,
)

from src.agents.route_planner.checks import (
    _day_stop_places,
    classify_day,
    _commute_class,
    urban_commute_notes,
    _soften_urban_commute_issues,
    _estimated_leg_count,
    _stop_label,
    _lodge_label,
    _move_ends,
    _first_stop_label,
    _leg_text,
    _longest_leg_clause,
    _is_real_sight,
    collect_issues,
)

from src.agents.route_planner.lodging import (
    _itinerary_places,
    _lodging_conflicts,
    _resolve_lodging,
    _lodging_anchor,
)

from src.agents.route_planner.model_io import (
    parse_model_plan,
    merge_days,
    _public_plan,
    _split_model_result,
    _invoke,
    _default_caller,
    _repair_user,
    _read_plan,
)

from src.agents.route_planner.emit import (
    _attach_places,
    arrange,
    _day_places,
    _assigned_stop_kinds,
    emit_route_plan,
    _seal_model_route,
    _emit_failure_issue,
)

from src.agents.route_planner.planner import (
    PreparedRoute,
    RoutePlannerCall,
    RoutePlannerResult,
    _default_restaurants,
    _default_kids,
    _default_teas,
    prepare_route,
    _planner_amap_budget_ms,
    _restart_provider_budget,
    _provider_amap_calls,
    _close_owned_provider,
    _fail,
    plan_route,
)

import importlib as _importlib
_importlib.import_module("src.agents.route_planner_prompt")
del _importlib


