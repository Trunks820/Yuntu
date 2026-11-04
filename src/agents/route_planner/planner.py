"""Prepare candidates and run one route-planner pass."""

from __future__ import annotations

import asyncio
import json
import logging
import time

from dataclasses import dataclass

from datetime import date

from src.agents.area_candidates import (
    fetch_kid_places as _fetch_kid_places,
    fetch_nearby_restaurants as _fetch_nearby_restaurants,
    fetch_tea_houses as _fetch_tea_houses,
)

from src.agents.composition_blueprint import is_light_food_place

from src.agents.pace import generation_base_mode

from src.agents.route_planner.prompt import (
    candidate_block,
    request_text,
    system_for,
)

from src.agents.route_planning import (
    AmapRouteProvider,
    _route_planning_candidate_pool,
    valid_must_include_ids,
)

from src.agents.llm import _http_status_code
from src.agents.schema import (
    AccommodationAnchor,
    CandidatePlace,
    RetrievalResult,
    RoutePlan,
    TripRequest,
)

from src.config import Settings

from src.agents.route_planner.rules import (
    PlannerRules,
    build_rules,
)

from src.agents.route_planner.places import (
    AreaCluster,
    hav_km,
    _coords,
    _names_contain,
    _closed,
    _as_candidate,
    _BLANK_NEARBY_EVIDENCE,
    _center,
    cluster_places,
    _protected_ids,
)

from src.agents.route_planner.legs import (
    CachingEstimator,
    AmapLegEstimator,
    EstimatorBackedProvider,
    quoted_known_legs,
    quoted_known_access,
)

from src.agents.route_planner.schedule import _idle_fill_counts

from src.agents.route_planner.checks import (
    urban_commute_notes,
    _soften_urban_commute_issues,
    _estimated_leg_count,
)

from src.agents.route_planner.model_io import (
    merge_days,
    _public_plan,
    _default_caller,
    _repair_user,
    _read_plan,
)

from src.agents.route_planner.emit import (
    arrange,
    _emit_failure_issue,
)
from src.agents.route_planner.lodging import (
    LODGING_FIRST_DRIFT_NOTE,
    LODGING_FIRST_KEPT,
    LODGING_FIRST_OFF,
    LODGING_FIRST_PRESELECTED,
    LODGING_FIRST_RESELECTED,
    LODGING_FIRST_UNAVAILABLE,
    _preselect_lodging,
    _preset_for_plan,
    _take_user_lodging,
    _usable_user_lodging,
    _user_lodging_name,
    hotel_lodging_enabled,
    lodging_first_enabled,
)

# Call sites below look up the package so a patch of route_planner.<name> is visible.
import src.agents.route_planner as _route_planner_pkg

logger = logging.getLogger(__name__)

_FAILURE_LABELS = {
    "quota": "额度用完",
    "rate_limited": "请求过于频繁",
    "server_error": "服务端错误",
    "timeout": "超时",
    "bad_output": "输出不是合法 JSON",
    "other": "其他",
}
_QUOTA_MARKERS = (
    "quota",
    "billing",
    "insufficient_quota",
    "payment required",
    "credit balance",
    "credits",
    "prepaid",
    "prepay",
    "额度",
    "计费",
    "余额不足",
    "欠费",
    "预付",
)



@dataclass
class PreparedRoute:
    rules: PlannerRules
    pool: list[CandidatePlace]
    clusters: list[AreaCluster]
    restaurants: dict[int, list[CandidatePlace]]
    by_id: dict[int, CandidatePlace]
    amap_ids: set[int]
    protected_ids: set[int]
    system: str
    user: str
    lodging_first: str = LODGING_FIRST_OFF
    preselected_lodging: CandidatePlace | None = None


@dataclass
class RoutePlannerCall:
    index: int
    repaired: bool
    json_retry: bool
    latency_ms: int
    prompt_tokens: int
    completion_tokens: int
    provider: str
    model: str


@dataclass
class RoutePlannerResult:
    plan: RoutePlan | None
    qualified: bool
    issues: list[str]
    adjustments: list[str]
    calls: list[RoutePlannerCall]
    elapsed_ms: int
    schedule_ms: int
    lodging_source: str
    lodging_name: str
    amap_call_count: int = 0
    estimated_leg_count: int = 0
    lodging_rest_count: int = 0
    sight_extend_count: int = 0
    failure_kind: str = ""
    failure_status: int | None = None
    kept_first_draft: bool = False
    lodging_first: str = LODGING_FIRST_OFF


async def _default_restaurants(session, **kwargs):
    if session is None:
        return []
    return await _fetch_nearby_restaurants(session, **kwargs)


async def _default_kids(session, **kwargs):
    if session is None:
        return []
    return await _fetch_kid_places(session, **kwargs)


async def _default_teas(session, **kwargs):
    if session is None:
        return []
    return await _fetch_tea_houses(session, **kwargs)


async def prepare_route(
    request: TripRequest,
    retrieval: RetrievalResult,
    *,
    session=None,
    fetch_restaurants=None,
    fetch_kid_places=None,
    fetch_tea_houses=None,
    settings: Settings | None = None,
    today: date | None = None,
    lodging_anchor: AccommodationAnchor | None = None,
    hotel_query=None,
    hotel_cache=None,
) -> PreparedRoute:
    settings = settings or _route_planner_pkg.get_settings()
    fetch_restaurants = fetch_restaurants or _default_restaurants
    fetch_kid_places = fetch_kid_places or _default_kids
    fetch_tea_houses = fetch_tea_houses or _default_teas
    pool = list(
        _route_planning_candidate_pool(
            retrieval,
            must_include_ids=valid_must_include_ids(request, retrieval),
        )
    )
    rules = build_rules(request, pool, settings=settings, today=today)
    center = _center(pool)
    known_ids = {place.place_id for place in pool}
    known_names = [place.name for place in pool]

    async def _take(rows, limit_km: float):
        kept = []
        for item in rows or []:
            place = _as_candidate(item)
            if place.place_id in known_ids or _closed(place) or _coords(place)[0] is None:
                continue
            if any(_names_contain(place.name, name) for name in known_names):
                continue
            if center is not None:
                distance = hav_km(({"latitude": center[0], "longitude": center[1]}), place)
                if distance is None or distance > limit_km:
                    continue
            kept.append(place)
            known_ids.add(place.place_id)
            known_names.append(place.name)
        return kept

    if rules.family:
        try:
            kids = await fetch_kid_places(session, city=rules.city)
        except Exception:
            kids = []
        pool.extend(await _take(kids, 30))
    if rules.tea:
        try:
            teas = await fetch_tea_houses(session, city=rules.city)
        except Exception:
            teas = []
        pool.extend(await _take(teas, 15))
    rules = build_rules(request, pool, settings=settings, today=today)
    clusters = cluster_places(pool)
    restaurants: dict[int, list[CandidatePlace]] = {}
    amap_ids: set[int] = set()
    for cluster in clusters:
        lat, lng = _coords(cluster.seed)
        if lat is None:
            continue
        try:
            found = await fetch_restaurants(
                session,
                city=rules.city,
                latitude=lat,
                longitude=lng,
                radius_meters=1000,
                limit=5,
                exclude_place_ids=sorted(known_ids),
            )
        except Exception:
            found = []
        def _proper_rows(found_rows) -> list[CandidatePlace]:
            picked = []
            seen: set[int] = set()
            for item in (found_rows or [])[:5]:
                place = _as_candidate(item)
                if place.place_id in known_ids or place.place_id in seen or _closed(place):
                    continue
                if is_light_food_place(place) or place.place_type not in {"restaurant", "food"}:
                    continue
                seen.add(place.place_id)
                picked.append(place)
            return picked

        rows = _proper_rows(found)
        if len(rows) < 3:
            try:
                wider = await fetch_restaurants(
                    session,
                    city=rules.city,
                    latitude=lat,
                    longitude=lng,
                    radius_meters=2000,
                    limit=5,
                    exclude_place_ids=sorted(known_ids),
                )
            except Exception:
                wider = []
            have = {place.place_id for place in rows}
            for place in _proper_rows(wider):
                if place.place_id in have:
                    continue
                rows.append(place)
                have.add(place.place_id)
                if len(rows) >= 5:
                    break
        rows = rows[:5]
        stored = []
        for place in rows:
            # Amap-only rows used to wipe mention_count and top_reasons, so the
            # route stop carried no evidence. Visitor-mentioned rows keep the
            # summary fields. canonical_place_id is c.place_id, the summary join key.
            if place.top_reasons:
                if place.canonical_place_id is None:
                    place = place.model_copy(update={"canonical_place_id": place.place_id})
            else:
                place = place.model_copy(update=_BLANK_NEARBY_EVIDENCE)
                amap_ids.add(place.place_id)
            stored.append(place)
            known_ids.add(place.place_id)
        if stored:
            restaurants[cluster.index] = stored
    by_id = {place.place_id: place for place in pool}
    for rows in restaurants.values():
        for place in rows:
            by_id[place.place_id] = place
    usable_lodging = _usable_user_lodging(lodging_anchor)
    lodging_name = _user_lodging_name(usable_lodging) if usable_lodging is not None else None
    lodging_point = usable_lodging.suggestion if usable_lodging is not None else None
    preselected = None
    lodging_first = LODGING_FIRST_OFF
    # A committed user lodging, including one carried only on the request,
    # skips this trial. The prompt then stays on the package S / U wording.
    if lodging_first_enabled(settings) and _take_user_lodging(
        request, pool, by_id, lodging_anchor
    ) is None:
        preselected = await _preselect_lodging(
            pool,
            must_ids=set(rules.must_ids),
            settings=settings,
            hotel_query=hotel_query,
            hotel_cache=hotel_cache,
        )
        lodging_first = (
            LODGING_FIRST_PRESELECTED if preselected is not None else LODGING_FIRST_UNAVAILABLE
        )
        if preselected is not None:
            lodging_point = preselected
    system = system_for(
        rules,
        lodging_name=lodging_name,
        hotel_lodging=hotel_lodging_enabled(settings),
        lodging_first_name=None if preselected is None else preselected.name,
    )
    user = request_text(request, rules) + "\n\n" + candidate_block(
        clusters, restaurants, rules, lodging=lodging_point
    )
    return PreparedRoute(
        rules=rules,
        pool=pool,
        clusters=clusters,
        restaurants=restaurants,
        by_id=by_id,
        amap_ids=amap_ids,
        protected_ids=_protected_ids(pool, rules),
        system=system,
        user=user,
        lodging_first=lodging_first,
        preselected_lodging=preselected,
    )


def _planner_amap_budget_ms(settings: Settings) -> int:
    return max(0, int(round(float(settings.route_planner_amap_time_budget_seconds) * 1000)))


def _restart_provider_budget(provider, budget_ms: int) -> None:
    """Start this provider's wall clock now. A caller-owned clock is not ours to move."""
    if provider is None or not hasattr(provider, "_wall_start_monotonic"):
        return
    budget_ms = max(0, int(budget_ms))
    start = time.monotonic()
    provider.time_budget_ms = budget_ms
    provider._wall_start_monotonic = start
    provider._deadline_monotonic = start + (budget_ms / 1000.0) if budget_ms > 0 else None


def _provider_amap_calls(provider) -> int:
    if provider is None:
        return 0
    try:
        return max(0, int(getattr(provider, "_call_count", 0) or 0))
    except (TypeError, ValueError):
        return 0


async def _close_owned_provider(provider) -> None:
    if provider is None:
        return
    close = getattr(provider, "close", None)
    if close is None:
        return
    try:
        result = close()
        if asyncio.iscoroutine(result) or asyncio.isfuture(result):
            await result
    except Exception:
        return


def _status_code(raw: object) -> int | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        return None
    try:
        code = int(raw)
    except (TypeError, ValueError):
        return None
    if 100 <= code <= 599:
        return code
    return None


def _planner_http_status(exc: BaseException) -> int | None:
    """Prefer the shared reader, then a status carried on the exception itself."""
    status = _http_status_code(exc)
    if status is not None:
        return status
    return _status_code(getattr(exc, "status_code", None))


def _exception_blob(exc: BaseException) -> str:
    """Short text used only to tell a billing 403 from a permission 403."""
    parts = [type(exc).__name__, str(exc)]
    for attr in ("message", "body"):
        value = getattr(exc, attr, None)
        if isinstance(value, str):
            parts.append(value[:500])
        elif isinstance(value, dict):
            for key in ("message", "code", "type"):
                item = value.get(key)
                if isinstance(item, str):
                    parts.append(item[:500])
                elif isinstance(item, dict):
                    nested = item.get("message")
                    if isinstance(nested, str):
                        parts.append(nested[:500])
    response = getattr(exc, "response", None)
    text = getattr(response, "text", None) if response is not None else None
    if isinstance(text, str):
        parts.append(text[:500])
    return " ".join(parts).lower()


def _is_timeout_failure(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    if "timeout" in type(exc).__name__.lower():
        return True
    text = str(exc).lower()
    return "timed out" in text or "timeout" in text


def _is_bad_output_failure(exc: BaseException) -> bool:
    if isinstance(exc, json.JSONDecodeError):
        return True
    name = type(exc).__name__.lower()
    return "json" in name and "decode" in name


def classify_planner_call_failure(exc: BaseException) -> tuple[str, int | None]:
    """Map one model-call exception to a failure kind and HTTP status."""
    status = _planner_http_status(exc)
    if status == 402 or (
        status == 403 and any(marker in _exception_blob(exc) for marker in _QUOTA_MARKERS)
    ):
        return "quota", status
    if status == 429:
        return "rate_limited", status
    if status is not None and 500 <= status <= 599:
        return "server_error", status
    if status == 408 or _is_timeout_failure(exc):
        return "timeout", status
    if _is_bad_output_failure(exc):
        return "bad_output", status
    return "other", status


def _model_failure_issue(exc: BaseException, kind: str, status: int | None) -> str:
    """Class name, status and a fixed label. The raw message can carry a key."""
    label = _FAILURE_LABELS.get(kind, "其他")
    name = type(exc).__name__
    if status is None:
        return f"排程模型调用失败：{name}（{label}）"
    return f"排程模型调用失败：{name}（HTTP {status}，{label}）"


def _log_model_call_failure(
    *,
    kind: str,
    status: int | None,
    repaired: bool,
    calls: list[RoutePlannerCall],
) -> None:
    index = 1
    if calls:
        try:
            index = int(getattr(calls[-1], "index", 1) or 1)
        except (TypeError, ValueError):
            index = 1
    logger.warning(
        "route_planner model call failed kind=%s http_status=%s phase=%s call_index=%s",
        kind,
        "none" if status is None else int(status),
        "改稿" if repaired else "首稿",
        index,
    )


def _fail(
    calls,
    issues,
    adjustments,
    started,
    schedule_ms,
    source="",
    name="",
    *,
    amap_call_count: int = 0,
    estimated_leg_count: int = 0,
    lodging_rest_count: int = 0,
    sight_extend_count: int = 0,
    failure_kind: str = "",
    failure_status: int | None = None,
    lodging_first: str = LODGING_FIRST_OFF,
) -> RoutePlannerResult:
    return RoutePlannerResult(
        plan=None,
        qualified=False,
        issues=issues,
        adjustments=adjustments,
        calls=calls,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        schedule_ms=schedule_ms,
        lodging_source=source,
        lodging_name=name,
        amap_call_count=amap_call_count,
        estimated_leg_count=estimated_leg_count,
        lodging_rest_count=lodging_rest_count,
        sight_extend_count=sight_extend_count,
        failure_kind=failure_kind,
        failure_status=failure_status,
        lodging_first=lodging_first,
    )


def _abandon_model_call(
    calls,
    issues,
    adjustments,
    started,
    schedule_ms,
    *,
    repaired: bool,
    kind: str,
    status: int | None,
    source: str = "",
    name: str = "",
    amap_call_count: int = 0,
    estimated_leg_count: int = 0,
    lodging_rest_count: int = 0,
    sight_extend_count: int = 0,
    lodging_first: str = LODGING_FIRST_OFF,
    exc: BaseException | None = None,
    issue: str | None = None,
) -> RoutePlannerResult:
    _log_model_call_failure(kind=kind, status=status, repaired=repaired, calls=calls)
    text = issue if issue is not None else (
        _model_failure_issue(exc, kind, status) if exc is not None else ""
    )
    merged = list(issues)
    if text:
        merged.append(text)
    return _fail(
        calls,
        merged,
        adjustments,
        started,
        schedule_ms,
        source,
        name,
        amap_call_count=amap_call_count,
        estimated_leg_count=estimated_leg_count,
        lodging_rest_count=lodging_rest_count,
        sight_extend_count=sight_extend_count,
        failure_kind=kind,
        failure_status=status,
        lodging_first=lodging_first,
    )


async def plan_route(
    request: TripRequest,
    retrieval: RetrievalResult,
    *,
    session=None,
    provider=None,
    provider_factory=None,
    estimator=None,
    model_caller=None,
    fetch_restaurants=None,
    fetch_kid_places=None,
    fetch_tea_houses=None,
    settings: Settings | None = None,
    today: date | None = None,
    citycode: str | None = None,
    lodging_anchor: AccommodationAnchor | None = None,
    hotel_query=None,
    hotel_cache=None,
) -> RoutePlannerResult:
    """Plan one trip. A qualified result carries a ``RoutePlan``; a failed one does not.

    ``provider_factory`` runs after the model returns and before the first
    schedule, so the Amap wall clock does not include the model wait. A
    provider this function creates is closed before return. A caller-supplied
    provider is left open.

    ``lodging_anchor`` is the resolver's user lodging. A user_specified anchor
    with a valid coordinate is kept. Any other value leaves lodging selection
    on the request, the model, and the area fallback.

    When ``route_planner_hotel_lodging_enabled`` is on, code picks one hotel
    cluster after the model returns and ignores the model lodging field.
    The flag defaults off, which keeps the model lodging and the area fallback.

    When ``route_planner_lodging_first_enabled`` is also on, and the user has
    not already fixed a lodging, that cluster is picked before the prompt.
    The route keeps it unless its center is farther than
    ``route_planner_lodging_first_drift_km``. The repair call uses the same
    preset and the same prompt.
    """
    started = time.perf_counter()
    settings = settings or _route_planner_pkg.get_settings()
    hotel_on = hotel_lodging_enabled(settings)
    usable_lodging = _usable_user_lodging(lodging_anchor)
    lodging_precision = usable_lodging.location_precision if usable_lodging is not None else None
    schedule_ms = 0
    calls: list[RoutePlannerCall] = []
    prepared = await prepare_route(
        request,
        retrieval,
        session=session,
        fetch_restaurants=fetch_restaurants,
        fetch_kid_places=fetch_kid_places,
        fetch_tea_houses=fetch_tea_houses,
        settings=settings,
        today=today,
        lodging_anchor=lodging_anchor,
        hotel_query=hotel_query,
        hotel_cache=hotel_cache,
    )
    marker = (
        LODGING_FIRST_KEPT
        if prepared.lodging_first == LODGING_FIRST_PRESELECTED
        else (prepared.lodging_first or LODGING_FIRST_OFF)
    )

    def _choice(plan: dict, by_id: dict[int, CandidatePlace]):
        if prepared.lodging_first != LODGING_FIRST_PRESELECTED or prepared.preselected_lodging is None:
            status = prepared.lodging_first or LODGING_FIRST_OFF
            if status == LODGING_FIRST_PRESELECTED:
                status = LODGING_FIRST_UNAVAILABLE
            return None, status
        drift_km = float(getattr(settings, "route_planner_lodging_first_drift_km", 3.0))
        return _preset_for_plan(
            plan,
            by_id,
            prepared.preselected_lodging,
            drift_km=drift_km,
        )

    def _noted(notes: list[str], status: str) -> list[str]:
        cleaned = [item for item in notes if item != LODGING_FIRST_DRIFT_NOTE]
        if status == LODGING_FIRST_RESELECTED:
            cleaned.insert(0, LODGING_FIRST_DRIFT_NOTE)
        return cleaned
    owned_provider = None
    handoff_provider = None
    provider_bound = False

    def bind(*, restart: bool) -> None:
        nonlocal provider, estimator, handoff_provider, owned_provider, provider_bound
        if not provider_bound:
            if estimator is None:
                if provider is None:
                    if provider_factory is not None:
                        provider = provider_factory()
                    else:
                        provider = AmapRouteProvider(
                            time_budget_ms=_planner_amap_budget_ms(settings)
                        )
                    owned_provider = provider
                estimator = AmapLegEstimator(
                    provider,
                    city=request.to_city or "",
                    citycode=citycode,
                )
            if not isinstance(estimator, CachingEstimator):
                estimator = CachingEstimator(estimator)
            handoff_provider = (
                provider if provider is not None else EstimatorBackedProvider(estimator)
            )
            provider_bound = True
            return
        if restart and owned_provider is not None:
            _route_planner_pkg._restart_provider_budget(
                owned_provider,
                _planner_amap_budget_ms(settings),
            )

    async def caller(system: str, user: str):
        if model_caller is None:
            return await _default_caller(system, user, settings)
        return await model_caller(system, user)

    try:
        try:
            draft, error = await _read_plan(
                caller, prepared.system, prepared.user, settings=settings, calls=calls, repaired=False
            )
        except Exception as exc:
            kind, status = classify_planner_call_failure(exc)
            return _abandon_model_call(
                calls, [], [], started, 0, repaired=False, kind=kind, status=status, exc=exc,
                lodging_first=marker,
            )
        if error is not None or draft is None:
            return _abandon_model_call(
                calls, [], [], started, 0,
                repaired=False, kind="bad_output", status=None,
                issue="排程模型没有返回合法 JSON",
                lodging_first=marker,
            )
        schedule_started = time.perf_counter()
        bind(restart=False)
        if hotel_on:
            draft.pop("lodging", None)
        preset, marker = _choice(draft, prepared.by_id)
        arranged, adjustments, lodge, source, lodging_name = await arrange(
            draft,
            rules=prepared.rules,
            pool=prepared.pool,
            by_id=dict(prepared.by_id),
            request=request,
            estimator=estimator,
            fetch_restaurants=fetch_restaurants or _default_restaurants,
            session=session,
            settings=settings,
            lodging_anchor=lodging_anchor,
            hotel_query=hotel_query,
            hotel_cache=hotel_cache,
            preset_lodging=preset,
        )
        by_id = dict(prepared.by_id)
        for rows in prepared.restaurants.values():
            for place in rows:
                by_id[place.place_id] = place
        for day in arranged.get("days") or []:
            for stop in day.get("stops") or []:
                place = stop.get("_place")
                if isinstance(place, CandidatePlace):
                    by_id[place.place_id] = place
        check_kw = {"lodge": lodge, "request": request, "settings": settings}
        issues = _route_planner_pkg.collect_issues(arranged, prepared.rules, by_id, prepared.amap_ids, **check_kw)
        estimated_leg_count = _estimated_leg_count(arranged)
        lodging_rest_count, sight_extend_count = _idle_fill_counts(arranged)
        public = _public_plan(arranged, include_lodging=not hotel_on)
        kept_first_draft = False
        if issues:
            first_draft = {
                "arranged": arranged,
                "adjustments": list(adjustments),
                "lodge": lodge,
                "source": source,
                "lodging_name": lodging_name,
                "issues": list(issues),
                "check_kw": check_kw,
                "estimated_leg_count": estimated_leg_count,
                "lodging_rest_count": lodging_rest_count,
                "sight_extend_count": sight_extend_count,
                "lodging_first": marker,
            }
            try:
                revised, repair_error = await _read_plan(
                    caller,
                    prepared.system,
                    _repair_user(prepared, public, adjustments, issues),
                    settings=settings,
                    calls=calls,
                    repaired=True,
                )
            except Exception as exc:
                schedule_ms = int((time.perf_counter() - schedule_started) * 1000)
                kind, status = classify_planner_call_failure(exc)
                return _abandon_model_call(
                    calls, issues, _noted(adjustments, marker), started, schedule_ms,
                    repaired=True, kind=kind, status=status, exc=exc,
                    source=source, name=lodging_name,
                    amap_call_count=_provider_amap_calls(owned_provider),
                    estimated_leg_count=estimated_leg_count,
                    lodging_rest_count=lodging_rest_count,
                    sight_extend_count=sight_extend_count,
                    lodging_first=marker,
                )
            if repair_error is not None or revised is None:
                schedule_ms = int((time.perf_counter() - schedule_started) * 1000)
                return _abandon_model_call(
                    calls, issues, _noted(adjustments, marker), started, schedule_ms,
                    repaired=True, kind="bad_output", status=None,
                    issue="改稿不是合法 JSON",
                    source=source, name=lodging_name,
                    amap_call_count=_provider_amap_calls(owned_provider),
                    estimated_leg_count=estimated_leg_count,
                    lodging_rest_count=lodging_rest_count,
                    sight_extend_count=sight_extend_count,
                    lodging_first=marker,
                )
            if hotel_on:
                revised.pop("lodging", None)
            merged, note = merge_days(
                prepared.rules,
                public,
                _public_plan(revised, include_lodging=not hotel_on),
            )
            if note:
                adjustments.append(note)
            bind(restart=True)
            preset, marker = _choice(merged, by_id)
            arranged, more, lodge, source, lodging_name = await arrange(
                merged,
                rules=prepared.rules,
                pool=prepared.pool,
                by_id=by_id,
                request=request,
                estimator=estimator,
                fetch_restaurants=fetch_restaurants or _default_restaurants,
                session=session,
                settings=settings,
                lodging_anchor=lodging_anchor,
                hotel_query=hotel_query,
                hotel_cache=hotel_cache,
                preset_lodging=preset,
            )
            adjustments.extend(more)
            for day in arranged.get("days") or []:
                for stop in day.get("stops") or []:
                    place = stop.get("_place")
                    if isinstance(place, CandidatePlace):
                        by_id[place.place_id] = place
            check_kw = {"lodge": lodge, "request": request, "settings": settings}
            revised_issues = _soften_urban_commute_issues(
                _route_planner_pkg.collect_issues(arranged, prepared.rules, by_id, prepared.amap_ids, **check_kw)
            )
            # The first draft's only problems were city-day commute over the cap,
            # and the repair introduced a real failure. Deliver the first draft.
            if revised_issues and not _soften_urban_commute_issues(first_draft["issues"]):
                arranged = first_draft["arranged"]
                adjustments = list(first_draft["adjustments"])
                adjustments.append("改稿后出现新问题，沿用第一稿")
                lodge = first_draft["lodge"]
                source = first_draft["source"]
                lodging_name = first_draft["lodging_name"]
                check_kw = first_draft["check_kw"]
                estimated_leg_count = first_draft["estimated_leg_count"]
                lodging_rest_count = first_draft["lodging_rest_count"]
                sight_extend_count = first_draft["sight_extend_count"]
                marker = first_draft["lodging_first"]
                issues = []
                kept_first_draft = True
            else:
                issues = revised_issues
                estimated_leg_count = _estimated_leg_count(arranged)
                lodging_rest_count, sight_extend_count = _idle_fill_counts(arranged)
        schedule_ms = int((time.perf_counter() - schedule_started) * 1000)
        for note in urban_commute_notes(arranged, prepared.rules, **check_kw):
            if note not in adjustments:
                adjustments.append(note)
        adjustments = _noted(adjustments, marker)
        if issues:
            return _fail(
                calls, issues, adjustments, started, schedule_ms, source, lodging_name,
                amap_call_count=_provider_amap_calls(owned_provider),
                estimated_leg_count=estimated_leg_count,
                lodging_rest_count=lodging_rest_count,
                sight_extend_count=sight_extend_count,
                lodging_first=marker,
            )
        generation_mode, _generation_reason = generation_base_mode(request, _route_planner_pkg.get_settings())
        try:
            route = await _route_planner_pkg.emit_route_plan(
                arranged,
                request=request,
                lodge=lodge,
                lodging_source=source,
                provider=handoff_provider,
                citycode=citycode,
                by_id=by_id,
                known=quoted_known_legs(estimator),
                known_access=quoted_known_access(estimator, lodge, source, generation_mode),
                location_precision=lodging_precision if source == "user" else None,
                write_lodging_reason=hotel_on and source != "user",
            )
        except Exception as exc:
            return _fail(
                calls,
                [_emit_failure_issue(exc)],
                adjustments,
                started,
                schedule_ms,
                source,
                lodging_name,
                amap_call_count=_provider_amap_calls(owned_provider),
                estimated_leg_count=estimated_leg_count,
                lodging_rest_count=lodging_rest_count,
                sight_extend_count=sight_extend_count,
                lodging_first=marker,
            )
        return RoutePlannerResult(
            plan=route,
            qualified=True,
            issues=[],
            adjustments=adjustments,
            calls=calls,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            schedule_ms=schedule_ms,
            lodging_source=source,
            lodging_name=lodging_name,
            amap_call_count=_provider_amap_calls(owned_provider),
            estimated_leg_count=estimated_leg_count,
            lodging_rest_count=lodging_rest_count,
            sight_extend_count=sight_extend_count,
            kept_first_draft=kept_first_draft,
            lodging_first=marker,
        )
    finally:
        await _close_owned_provider(owned_provider)


