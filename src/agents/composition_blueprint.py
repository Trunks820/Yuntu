"""Deterministic itinerary-composition guidance for locked route plans."""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace

from src.agents.food_authorization import (
    _walk_minutes,
    build_food_attachment_authorization,
)
from src.agents.food_resolver import (
    FOOD_PLACE_TYPES,
    FoodAttachment,
    FoodAttachmentAuthorization,
    FoodEnrichmentResult,
    resolve_food_attachment,
)
from src.agents.schema import (
    CandidatePlace,
    CompositionBlueprint,
    CompositionCommute,
    CompositionDay,
    CompositionStop,
    RoutePlan,
    TripRequest,
    parse_hhmm,
)

FOOD_TYPES = {"restaurant", "food", "market"}
SNACK_TYPES = {"snack", "dessert", "bakery", "milk_tea"}
COFFEE_TYPES = {"cafe", "coffee", "tea"}
PHOTO_TYPES = {"photo_spot"}
COFFEE_MARKERS = ("coffee", "cafe", "咖啡", "茶", "茶歇")
SNACK_MARKERS = ("dessert", "bakery", "甜品", "面包", "蛋糕", "奶茶")
FULL_MEAL_MARKERS = (
    "午餐",
    "晚餐",
    "正餐",
    "吃饭",
    "餐厅",
    "饭店",
    "火锅",
    "烧烤",
    "米线",
    "面馆",
)
LUNCH_WINDOW_START = 12 * 60
LUNCH_WINDOW_END = 14 * 60


def _candidate_text(place: CandidatePlace) -> str:
    parts = [place.name, place.place_type]
    for item in [*place.top_reasons, *place.warnings]:
        if isinstance(item, dict) and item.get("reason"):
            parts.append(str(item["reason"]))
    return " ".join(parts).lower()


def is_food_place(place: CandidatePlace) -> bool:
    place_type = place.place_type.lower()
    return (
        place_type in FOOD_TYPES
        or place_type in SNACK_TYPES
        or place_type in COFFEE_TYPES
        or _light_food_kind(place) is not None
    )


def _has_full_meal_evidence(place: CandidatePlace) -> bool:
    text = _candidate_text(place)
    return any(marker in text for marker in FULL_MEAL_MARKERS)


def _light_food_kind(place: CandidatePlace) -> str | None:
    text = _candidate_text(place)
    place_type = place.place_type.lower()
    if place_type in COFFEE_TYPES:
        return "coffee"
    if place_type in SNACK_TYPES:
        return "snack"
    # A service mentioned inside a museum/park/street does not own its role.
    # Keyword inference is only for untyped candidates and their identity name;
    # recommendation prose and warnings can contain negation or nearby shops.
    if place_type not in {"", "other"}:
        return None
    text = place.name.lower()
    if any(marker in text for marker in FULL_MEAL_MARKERS):
        return None
    if any(marker in text for marker in COFFEE_MARKERS):
        return "coffee"
    if any(marker in text for marker in SNACK_MARKERS):
        return "snack"
    return None


def is_photo_stop(place: CandidatePlace) -> bool:
    text = _candidate_text(place)
    return place.place_type.lower() in PHOTO_TYPES or any(
        marker in text
        for marker in ("拍照", "打卡", "机位", "观景", "出片", "夜景")
    )


def is_context_stop(place: CandidatePlace) -> bool:
    text = _candidate_text(place)
    place_type = place.place_type.lower()
    return (
        place_type in {"area", "district", "transfer", "commercial_area"}
        or place.name.endswith(("市区", "城区", "商圈", "片区"))
        or "中转" in text
    )


def commute_style(duration_minutes: int, mode: str = "driving") -> str:
    if mode == "walking" and duration_minutes <= 5:
        return "walkable"
    if duration_minutes <= 15:
        return "nearby"
    if duration_minutes <= 30:
        return "normal_transfer"
    if duration_minutes <= 45:
        return "long_transfer"
    return "remote_transfer"


def trip_is_food_focused(req: TripRequest) -> bool:
    text = " ".join([*req.preferences, req.notes])
    return any(marker in text for marker in ("美食", "吃", "小吃", "咖啡", "火锅"))


def _theme_for_day(
    places: list[CandidatePlace],
    *,
    trip_request: TripRequest,
) -> tuple[str, str]:
    text = " ".join(_candidate_text(place) for place in places)
    food_count = sum(1 for place in places if is_food_place(place))
    if any(marker in text for marker in ("平潭", "岛", "海", "郊", "环岛", "远郊")):
        return "day_trip", "远郊一日"
    if any(marker in text for marker in ("夜景", "江景", "桥", "灯光", "天际线")):
        return "night_view", "夜景打卡"
    if trip_is_food_focused(trip_request) and food_count >= 2:
        return "food_walk", "美食慢逛"
    if any(marker in text for marker in ("街", "巷", "商圈", "步行", "citywalk")):
        return "citywalk", "街区漫步"
    if any(marker in text for marker in ("公园", "茶", "咖啡", "寺", "书院", "休闲")):
        return "slow_day", "慢节奏休闲"
    if any(marker in text for marker in ("博物馆", "纪念", "历史", "文化", "古城", "老城")):
        return "culture_day", "人文老城"
    return "classic_day", "经典顺路游"


def _has_lunch_rest_overlap(trip_request: TripRequest) -> bool:
    for window in trip_request.rest_windows:
        start = parse_hhmm(window.start)
        end = parse_hhmm(window.end)
        if start is None or end is None:
            continue
        if start < LUNCH_WINDOW_END and end > LUNCH_WINDOW_START:
            return True
    return False


def _stop_hint(
    role: str,
    meal_slot: str | None,
    *,
    lunch_rest_overlap: bool = False,
) -> str:
    if role == "meal_stop":
        if meal_slot == "lunch" and lunch_rest_overlap:
            return "写成午餐+休整节点，不承诺逐点到离时间"
        return "写成午餐节点" if meal_slot == "lunch" else "写成晚餐节点"
    if role == "snack_stop":
        return "写成顺路小吃或甜品，不要当成主景点"
    if role == "coffee_stop":
        return "写成咖啡/茶歇休息点"
    if role == "optional_stop":
        return "写成可选补充，不要过度推荐"
    if role == "transfer_context":
        return "只作为区域或换乘语境轻写"
    if role == "photo_stop":
        return "写成拍照/打卡停留点"
    if role == "anchor_activity":
        return "作为当天主活动展开，但不要补充无来源事实"
    return "作为次要活动简洁说明"


def _build_day_stops(
    day_places: list[CandidatePlace],
    *,
    trip_request: TripRequest,
) -> list[CompositionStop]:
    food_seen = 0
    non_food = [place for place in day_places if not is_food_place(place)]
    anchor_id = None
    if non_food:
        anchor_id = max(
            non_food,
            key=lambda place: (
                place.effective_score,
                place.recommend_score,
                place.mention_count,
                -place.place_id,
            ),
        ).place_id

    stops = []
    for place in day_places:
        place_type = place.place_type.lower()
        light_kind = _light_food_kind(place)
        meal_slot = None
        role = "secondary_activity"
        emphasis = "normal"
        if light_kind == "coffee":
            role = "coffee_stop"
            meal_slot = "coffee"
        elif light_kind == "snack":
            role = "snack_stop"
            meal_slot = "snack"
        elif place_type in FOOD_TYPES:
            food_seen += 1
            if food_seen == 1:
                role = "meal_stop"
                meal_slot = "lunch"
            elif food_seen == 2:
                role = "meal_stop"
                meal_slot = "dinner"
            else:
                role = "optional_stop"
                meal_slot = "late_night_optional"
                emphasis = "light"
        elif is_context_stop(place):
            role = "transfer_context"
            emphasis = "light"
        elif is_photo_stop(place):
            role = "photo_stop"
        elif place.place_id == anchor_id:
            role = "anchor_activity"
            emphasis = "high"

        stops.append(CompositionStop(
            place_id=place.place_id,
            name=place.name,
            role=role,
            meal_slot=meal_slot,
            emphasis=emphasis,
            writing_hint=_stop_hint(
                role,
                meal_slot,
                lunch_rest_overlap=_has_lunch_rest_overlap(trip_request),
            ),
        ))
    return stops


def build_base_composition_blueprints(
    route_plans: list[RoutePlan],
    trip_request: TripRequest,
) -> list[CompositionBlueprint]:
    """Build presentation guidance without changing locked route structure."""
    blueprints = []
    for route_plan in route_plans:
        days = []
        for day_group in route_plan.day_groups:
            places = day_group.places
            theme_code, theme_label = _theme_for_day(
                places,
                trip_request=trip_request,
            )
            commutes = []
            for leg in day_group.commute_legs:
                style = commute_style(
                    int(leg.duration_minutes or 0),
                    leg.mode,
                )
                commutes.append(CompositionCommute(
                    from_place_id=leg.from_place_id,
                    to_place_id=leg.to_place_id,
                    duration_minutes=int(leg.duration_minutes or 0),
                    mode=leg.mode,
                    style=style,
                    must_mention=style in {"long_transfer", "remote_transfer"},
                    transit_transition=(
                        f"{leg.from_name}→{leg.to_name}，"
                        f"公共交通约 {int(leg.duration_minutes or 0)} 分钟"
                        if leg.mode == "transit"
                        else None
                    ),
                ))
            notes = [
                "每天开头先写一句主题导语",
                "不要把餐饮点写成核心景点活动",
                "短通勤合并成顺路表达，避免机械列分钟",
            ]
            if any(commute.must_mention for commute in commutes):
                notes.append("long_transfer/remote_transfer 必须在正文中提示")
            if trip_request.rest_windows:
                notes.append("按用户时间偏好写出休息/午休安排，但不要写逐点到离时间")
            days.append(CompositionDay(
                day=day_group.day,
                theme_code=theme_code,
                theme_label=theme_label,
                stops=_build_day_stops(places, trip_request=trip_request),
                commutes=commutes,
                writing_notes=notes,
            ))
        blueprints.append(CompositionBlueprint(
            version="v0.6.10",
            plan_label=route_plan.label,
            days=days,
        ))
    return blueprints


def build_composition_blueprints(
    route_plans: list[RoutePlan],
    trip_request: TripRequest,
) -> list[CompositionBlueprint]:
    """Backward-compatible name for callers that only need base blueprints."""

    return build_base_composition_blueprints(route_plans, trip_request)


def _food_evidence_tier(
    attachment: FoodAttachment | None,
    auth_payload: FoodAttachmentAuthorization | None,
) -> str:
    if attachment is None:
        return "none"
    if auth_payload and (
        auth_payload.direct_facts or auth_payload.weak_experience
    ):
        return "full"
    return "structural"


def _food_span_marker(
    *,
    plan_index: int,
    day: int,
    anchor_place_id: int,
    meal_slot: str,
    food_place_id: int,
) -> str:
    return (
        f"<!-- food:plan{plan_index}_day{day}_anchor{anchor_place_id}"
        f"_slot{meal_slot}_food{food_place_id} -->"
    )


def _food_anchor_place_ids(
    route_days: dict,
    plan_index: int,
    day: int,
) -> set[int]:
    """Return place_ids of food-type anchors in a day — these should not
    trigger the Food Resolver because the user is already eating there."""
    day_group = route_days.get((plan_index, day))
    if day_group is None:
        return set()
    return {
        place.place_id
        for place in day_group.places
        if place.place_type.lower() in FOOD_PLACE_TYPES
    }


async def enrich_blueprints_with_food(
    blueprints: list[CompositionBlueprint],
    route_plans: list[RoutePlan],
    trip_request: TripRequest,
    session_factory,
    settings,
) -> FoodEnrichmentResult:
    """Attach nearby food guidance without changing the locked route structure."""

    if not getattr(settings, "food_recommendation_enabled", False):
        return FoodEnrichmentResult(
            blueprints=blueprints,
            attachment_auth_map={},
        )

    enriched_blueprints = [
        blueprint.model_copy(deep=True)
        for blueprint in blueprints
    ]
    route_days = {
        (plan_index, day_group.day): day_group
        for plan_index, route_plan in enumerate(route_plans)
        for day_group in route_plan.day_groups
    }

    async def enrich_stop(
        plan_index: int,
        day: int,
        stop: CompositionStop,
    ) -> tuple[FoodAttachmentAuthorization, FoodAttachment | None]:
        day_group = route_days.get((plan_index, day))
        anchor = next(
            (
                place
                for place in (day_group.places if day_group else [])
                if place.place_id == stop.place_id
            ),
            None,
        )
        attachment = None
        if anchor is not None:
            async with session_factory() as resolver_session:
                attachment = await resolve_food_attachment(
                    anchor_place_id=anchor.place_id,
                    lat=anchor.latitude,
                    lng=anchor.longitude,
                    city=trip_request.to_city,
                    session=resolver_session,
                    settings=settings,
                )

        async with session_factory() as authorization_session:
            authorization = await build_food_attachment_authorization(
                attachment,
                plan_index,
                day,
                stop.place_id,
                stop.meal_slot or "lunch",
                authorization_session,
            )

        walk_minutes = (
            _walk_minutes(
                attachment.latitude,
                attachment.longitude,
                anchor.latitude,
                anchor.longitude,
            )
            if attachment is not None and anchor is not None
            else None
        )
        tier = _food_evidence_tier(attachment, authorization)
        authorization = replace(
            authorization,
            evidence_tier=tier,
            walk_minutes=walk_minutes,
        )
        stop.writing_hint = {
            "role": "meal_stop",
            "meal_slot": authorization.meal_slot,
            "evidence_tier": authorization.evidence_tier,
            "food_name": authorization.food_name,
            "amap_rating": authorization.amap_rating,
            "amap_avg_price": authorization.amap_avg_price,
            "walk_minutes": authorization.walk_minutes,
            "meal_type": authorization.meal_type,
            "food_place_id": authorization.food_place_id,
            "none_tier_text": authorization.none_tier_text,
            "span_marker": _food_span_marker(
                plan_index=authorization.plan_index,
                day=authorization.day,
                anchor_place_id=authorization.anchor_place_id,
                meal_slot=authorization.meal_slot,
                food_place_id=authorization.food_place_id,
            ),
        }
        return authorization, attachment

    pending = [
        (
            plan_index,
            day.day,
            stop,
        )
        for plan_index, blueprint in enumerate(enriched_blueprints)
        for day in blueprint.days
        for stop in day.stops
        if stop.role == "meal_stop"
        and stop.place_id not in _food_anchor_place_ids(route_days, plan_index, day.day)
    ]
    # Issue 2: when a day has no meal_stop, attach lunch recommendation to
    # the day's anchor_activity (or first non-food stop).
    for plan_index, blueprint in enumerate(enriched_blueprints):
        for day in blueprint.days:
            food_anchor_ids = _food_anchor_place_ids(
                route_days,
                plan_index,
                day.day,
            )
            # A route restaurant is already the day's meal stop. It is skipped
            # by enrichment, but must still prevent the no-meal fallback from
            # attaching a second lunch recommendation to another route stop.
            has_meal = any(stop.role == "meal_stop" for stop in day.stops)
            if not has_meal:
                fallback = next(
                    (
                        stop
                        for stop in day.stops
                        if stop.role
                        in ("anchor_activity", "secondary_activity", "photo_stop")
                        and stop.place_id not in food_anchor_ids
                    ),
                    None,
                )
                if fallback is not None:
                    pending.append((plan_index, day.day, fallback))
    enrichment_results = await asyncio.gather(
        *(
            enrich_stop(plan_index, day, stop)
            for plan_index, day, stop in pending
        )
    )
    return FoodEnrichmentResult(
        blueprints=enriched_blueprints,
        attachment_auth_map={
            authorization.key: authorization
            for authorization, _ in enrichment_results
        },
        meal_attachment_map={
            (authorization.plan_index, authorization.day, authorization.meal_slot): attachment
            for authorization, attachment in enrichment_results
            if attachment is not None
        },
    )


def meal_role_recovery_metrics(
    route_plans: list[RoutePlan],
    composition_blueprints: list[CompositionBlueprint],
) -> dict:
    places_by_id = {
        place.place_id: place
        for route_plan in route_plans
        for day_group in route_plan.day_groups
        for place in day_group.places
    }
    recovered = []
    for blueprint in composition_blueprints:
        for day in blueprint.days:
            for stop in day.stops:
                place = places_by_id.get(stop.place_id)
                if place is None:
                    continue
                if place.place_type.lower() not in FOOD_TYPES:
                    continue
                if stop.role not in {"coffee_stop", "snack_stop"}:
                    continue
                recovered.append({
                    "place_id": stop.place_id,
                    "name": stop.name,
                    "role": stop.role,
                    "meal_slot": stop.meal_slot,
                })
    return {
        "meal_role_recovered": bool(recovered),
        "meal_role_recovered_count": len(recovered),
        "meal_role_recovered_stops": recovered[:10],
    }


_LIGHT_FOOD_TYPES = {"cafe", "coffee", "dessert", "bakery", "milk_tea", "tea"}
_LIGHT_FOOD_WORDS = (
    "咖啡",
    "烘焙",
    "面包",
    "甜品",
    "蛋糕",
    "三明治",
    "奶茶",
    "茶饮",
    "cafe",
    "coffee",
    "bakery",
)
_NIGHT_HINT_CLOCK_RE = re.compile(r"\d{1,2}\s*[:：]\s*\d{2}")
_NIGHT_HINT_NEGATION_RE = re.compile(r"(?:不要|别|不宜|不适合).{0,12}(?:夜|晚)")


def is_light_food_place(place) -> bool:
    """Cafe, bakery and tea stops are light food, not a seated meal."""
    place_type = str(getattr(place, "place_type", "") or "").lower()
    if place_type in _LIGHT_FOOD_TYPES:
        return True
    blob = f"{getattr(place, 'name', '')} {place_type}".lower()
    return any(word in blob for word in _LIGHT_FOOD_WORDS)


def _night_hint_clauses(hint: str) -> list[str]:
    return [part.strip() for part in re.split(r"[。；;]", hint or "") if part.strip()]


def _night_activity_bound_to_place(clause: str, place_name: str) -> bool:
    name = (place_name or "").strip()
    return len(name) >= 2 and name in (clause or "")
