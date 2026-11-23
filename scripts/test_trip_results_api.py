"""Focused checks for the public trip result projection API."""

from __future__ import annotations

import asyncio
import json
import os
from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ["TRIP_WORKER_ENABLED"] = "false"
os.environ["CRAWL_WORKER_ENABLED"] = "false"
os.environ["CITY_BATCH_WORKER_ENABLED"] = "false"

import httpx
from httpx import ASGITransport

from src.agents import workflow
from src.agents.schema import (
    CandidatePlace,
    CommuteLeg,
    PlanOutput,
    RouteDayGroup,
    RoutePlan,
    TripRequest as AgentTripRequest,
    WorkflowResult,
)
from src.api import trip_results
from src.api.trip_results import (
    ResultContractUnsupported,
    project_trip_result,
)
from src.config import get_settings
from src.cost_estimate.resolver import unavailable_cost_snapshot
from src.cost_estimate.workflow_integration import unavailable_snapshots_for_result

get_settings.cache_clear()

from src.api.app import app


def _place(place_id: int, name: str, place_type: str = "landmark") -> dict:
    return {
        "place_id": place_id,
        "name": name,
        "place_type": place_type,
        "longitude": 106.5 + place_id / 1000,
        "latitude": 29.5 + place_id / 1000,
        "top_reasons": [
            {
                "reason": (
                    f"**{name}** reason source:xhs provider:raw "
                    "recommend_score=9.9 https://example.com/raw"
                )
            }
        ],
    }


def _candidate(place_id: int) -> CandidatePlace:
    return CandidatePlace(
        place_id=place_id,
        name=f"地点{place_id}",
        place_type="attraction",
    )


def _workflow_route_plan(day_count: int) -> RoutePlan:
    return RoutePlan(
        label="A",
        day_groups=[
            RouteDayGroup(day=day, places=[_candidate(100 + day)])
            for day in range(1, day_count + 1)
        ],
    )


def _workflow_plan(day_count: int) -> PlanOutput:
    return PlanOutput(
        plan_name="测试路线",
        plan_text="",
        day_place_names=[
            [f"地点{100 + day}"]
            for day in range(1, day_count + 1)
        ],
    )


def _day_group(day: int, area: str, first_id: int) -> dict:
    encoded_polyline = (
        f"{106.5 + first_id / 1000:.3f},{29.5 + first_id / 1000:.3f};"
        f"{106.5 + (first_id + 1) / 1000:.3f},{29.5 + (first_id + 1) / 1000:.3f}"
    )
    return {
        "day": day,
        "area": area,
        "commute_minutes": 12,
        "commute_notes": [f"{area} mostly walking"],
        "places": [
            _place(first_id, f"{area} Anchor", "landmark"),
            _place(first_id + 1, f"{area} Street", "culture"),
        ],
        "commute_legs": [
            {
                "from_place_id": first_id,
                "to_place_id": first_id + 1,
                "duration_minutes": 12,
                "distance_meters": 850,
                "encoded_polyline": encoded_polyline,
            }
        ],
    }


def _sample_plan() -> dict:
    return {
        "plan_name": "Classic relaxed route",
        "plan_text": (
            "**Day 1：Old city walk** 天气提醒：户外步行段注意带伞、防滑。 "
            "Start with the landmark, then keep the afternoon light.\n"
            "---\n"
            "**Day 2：River view** "
            "Use this day for a slower riverside loop.\n"
            "---\n"
            "**Day 3：Food finish** "
            "End with a simple food stop."
        ),
        "weather_display": {
            "status": "ok",
            "city": "Chongqing",
            "days": [
                {
                    "day": 1,
                    "date": "2026-06-30",
                    "weather_text": "小雨转多云",
                    "temp_min_c": 25,
                    "temp_max_c": 31,
                    "wind_text": "东北风 3级",
                    "icon_code": "rain",
                    "reminders": ["户外步行段注意带伞、防滑。"],
                },
                {
                    "day": 2,
                    "date": "2026-07-01",
                    "weather_text": "多云",
                    "temp_min_c": 24,
                    "temp_max_c": 30,
                    "wind_text": "东风 2级",
                    "icon_code": "cloudy",
                    "reminders": [],
                }
            ],
        },
        "composition_blueprint": {
            "days": [
                {
                    "day": 1,
                    "theme_label": "Old city walk",
                    "stops": [
                        {
                            "place_id": 123,
                            "role": "anchor",
                            "writing_hint": "A central landmark.",
                        }
                    ],
                },
                {"day": 2, "theme_label": "River view", "stops": []},
                {"day": 3, "theme_label": "Food finish", "stops": []},
            ]
        },
        "budget_result": {
            "days": [
                {
                    "day": 1,
                    "status": "within_budget",
                    "budget_minutes": 90,
                    "commute_minutes": 12,
                }
            ]
        },
        "route_plan": {
            "day_groups": [
                _day_group(1, "Old city", 123),
                _day_group(2, "River", 223),
                _day_group(3, "Food", 323),
            ]
        },
    }


def _sample_row(
    *,
    plan_json: list[dict] | None = None,
    days: int = 3,
    start_date=None,
    end_date=None,
    quality_metrics: dict | None = None,
) -> SimpleNamespace:
    metrics = {
        "commute_mode_request": "driving",
        **(quality_metrics or {}),
    }
    plans = deepcopy(plan_json if plan_json is not None else [_sample_plan()])
    requested_mode = metrics["commute_mode_request"]
    for plan_index, plan in enumerate(plans):
        plan["cost_estimate_snapshot"] = unavailable_cost_snapshot(
            plan_index=plan_index,
            plan_key=str(plan.get("plan_name") or f"Plan {plan_index + 1}"),
            people_count=1,
            days=days,
            requested_commute_mode=requested_mode,
            from_city_present=False,
            estimated_at=datetime(2026, 8, 4, 8, 0, tzinfo=timezone.utc),
            failure_code="offline_fixture_unavailable",
        ).model_dump(mode="json")
    return SimpleNamespace(
        id=536,
        user_query="Chongqing structured",
        from_city="",
        to_city="Chongqing",
        start_date=start_date,
        end_date=end_date,
        days=days,
        people_count=1,
        preferences=["food", "citywalk"],
        avoid=["too tired"],
        notes="budget 1000-5000 per person",
        quality_metrics=metrics,
        plan_json=plans,
    )


def _assert_no_public_legacy_fields(payload: dict) -> None:
    serialized = json.dumps(payload, ensure_ascii=False)
    for forbidden in ("reply_text", "generated_plan", "plan_text"):
        assert forbidden not in serialized


def _assert_no_place_detail_fields(payload: dict) -> None:
    forbidden = {"summary", "top_reasons", "warnings", "source_count", "mention_count", "district"}
    for plan in payload.get("plans", []):
        for day in plan.get("days", []):
            for place in day.get("places", []):
                leaked = forbidden.intersection(place)
                assert not leaked, f"/trip/results place leaked detail fields: {sorted(leaked)}"


def _test_projection_shape() -> None:
    result = project_trip_result(
        _sample_row(start_date=date(2026, 7, 1), end_date=date(2026, 7, 3))
    )
    payload = result.model_dump(mode="json")
    assert payload["schema_version"] == "2.1"
    assert payload["result_id"] == 536
    assert payload["city"] == {"name": "Chongqing"}
    assert payload["weather"]["status"] == "ok"
    assert payload["weather"]["days"][0]["weather_text"] == "小雨转多云"
    assert "dayweather" not in payload["weather"]["days"][0]
    assert payload["request"]["days"] == 3
    assert set(payload["request"]) == {
        "days", "people_count", "preferences", "avoid"
    }
    assert payload["plans"][0]["plan_id"] == "plan_a"
    assert payload["plans"][0]["title"] == "Classic relaxed route"
    assert payload["plans"][0]["cost_estimate"]["completeness"] == "unavailable"
    assert payload["plans"][0]["accommodation"] is None
    assert "Start with the landmark" not in payload["plans"][0]["summary"]
    assert "天气提醒" not in payload["plans"][0]["summary"]
    assert "**" not in payload["plans"][0]["summary"]
    assert "Day 1" not in payload["plans"][0]["summary"]
    assert payload["plans"][0]["pace"]["level"] == "RELAXED"
    day = payload["plans"][0]["days"][0]
    assert day["title"] == "Old city walk"
    assert day["pace_status"] == "WITHIN_LIMIT"
    assert day["commute_legs"][0]["mode"] == "driving"
    assert day["commute_legs"][0]["duration_source"] == "estimate"
    assert day["commute_legs"][0]["encoded_polyline"] == (
        "106.623,29.623;106.624,29.624"
    )
    assert day["places"][0]["role"] == "anchor"
    # Internal writing_hint (Writer instructions) must never surface as the
    # public brief; brief falls back to the cleaned top_reason instead.
    assert day["places"][0]["brief"] != "A central landmark."
    assert "reason" in day["places"][0]["brief"]
    assert "https://" not in day["places"][1]["brief"]
    assert "**" not in day["places"][1]["brief"]
    assert "source:" not in day["places"][1]["brief"]
    assert "provider:" not in day["places"][1]["brief"]
    assert "recommend_score" not in day["places"][1]["brief"]
    assert day["narrative"].startswith("Old city walk")
    assert "天气提醒" not in day["narrative"]
    assert "带伞" not in day["narrative"]
    assert "**Day" not in day["narrative"]
    assert "---" not in day["narrative"]
    assert payload["plans"][0]["days"][1]["narrative"].startswith("River view")
    assert "Old city walk" not in payload["plans"][0]["days"][1]["narrative"]
    assert payload["plans"][0]["days"][2]["narrative"].startswith("Food finish")
    _assert_no_public_legacy_fields(payload)
    _assert_no_place_detail_fields(payload)


def _test_all_modes_preserve_encoded_polyline() -> None:
    polyline = "116.481,39.990;116.483,39.991"
    request_mode = {
        "driving": "driving",
        "walking": "transit",
        "cycling": "cycling",
        "transit": "transit",
    }
    for mode in ("driving", "walking", "cycling", "transit"):
        plan = _sample_plan()
        for day_group in plan["route_plan"]["day_groups"]:
            for leg in day_group["commute_legs"]:
                leg.update({
                    "mode": mode,
                    "source": "amap",
                    "encoded_polyline": polyline,
                })
        payload = project_trip_result(_sample_row(
            plan_json=[plan],
            quality_metrics={"commute_mode_request": request_mode[mode]},
        )).model_dump(mode="json")
        assert payload["schema_version"] == "2.1"
        legs = [
            leg
            for day in payload["plans"][0]["days"]
            for leg in day["commute_legs"]
        ]
        assert {leg["mode"] for leg in legs} == {mode}
        assert all(leg["encoded_polyline"] == polyline for leg in legs)

    empty_plan = _sample_plan()
    empty_plan["route_plan"]["day_groups"][0]["commute_legs"][0].update({
        "mode": "cycling",
        "encoded_polyline": "",
    })
    empty_payload = project_trip_result(_sample_row(
        plan_json=[empty_plan],
        quality_metrics={"commute_mode_request": "cycling"},
    )).model_dump(mode="json")
    assert empty_payload["schema_version"] == "2.1"
    assert empty_payload["plans"][0]["days"][0]["commute_legs"][0][
        "encoded_polyline"
    ] == ""


def _test_old_record_polyline_defaults_empty() -> None:
    plan = _sample_plan()
    for day_group in plan["route_plan"]["day_groups"]:
        for leg in day_group["commute_legs"]:
            leg.pop("encoded_polyline", None)

    payload = project_trip_result(_sample_row(plan_json=[plan])).model_dump(mode="json")

    assert payload["schema_version"] == "2.1"
    for day in payload["plans"][0]["days"]:
        for leg in day["commute_legs"]:
            assert "encoded_polyline" in leg
            assert leg["encoded_polyline"] == ""


def _test_commute_mode_schema_2_projection_cases() -> None:
    flag_off_plan = _sample_plan()
    for day_group in flag_off_plan["route_plan"]["day_groups"]:
        for leg in day_group["commute_legs"]:
            leg.update({"mode": "driving", "source": "amap"})
    flag_off_metrics = {
        "commute_mode_request": "transit",
        "commute_mode_report": {
            "requested": "transit",
            "effective": "driving",
            "effective_reason": "feature_disabled",
            "degraded_leg_count": 0,
            "rationalized_leg_count": 0,
            "duration_source": "amap",
        },
    }
    flag_off = project_trip_result(_sample_row(
        plan_json=[flag_off_plan],
        quality_metrics=flag_off_metrics,
    )).model_dump(mode="json")
    assert "commute_mode" not in flag_off["request"]
    assert "commute_mode_report" not in flag_off
    assert all(
        leg["mode"] == "driving" and leg["duration_source"] == "amap"
        for day in flag_off["plans"][0]["days"]
        for leg in day["commute_legs"]
    )

    partial_plan = _sample_plan()
    partial_legs = [
        leg
        for day in partial_plan["route_plan"]["day_groups"]
        for leg in day["commute_legs"]
    ]
    for index, leg in enumerate(partial_legs):
        leg.update({
            "mode": "transit",
            "source": "estimate" if index == 0 else "amap",
            "encoded_polyline": "106.7,29.7;106.8,29.8",
        })
    partial = project_trip_result(_sample_row(
        plan_json=[partial_plan],
        quality_metrics={
            "commute_mode_request": "transit",
            "commute_mode_report": {
                "requested": "transit",
                "effective": "transit",
                "effective_reason": "requested",
                "degraded_leg_count": 1,
                "rationalized_leg_count": 0,
                "duration_source": "mixed",
            },
        },
    )).model_dump(mode="json")
    projected_legs = [
        leg
        for day in partial["plans"][0]["days"]
        for leg in day["commute_legs"]
    ]
    assert [leg["duration_source"] for leg in projected_legs] == [
        "estimate",
        "amap",
        "amap",
    ]
    assert all(
        leg["encoded_polyline"] == "106.7,29.7;106.8,29.8"
        for leg in projected_legs
    )
    assert "commute_mode_report" not in partial

    rationalized_plan = _sample_plan()
    legs = [
        leg
        for day in rationalized_plan["route_plan"]["day_groups"]
        for leg in day["commute_legs"]
    ]
    for leg in legs:
        leg.update({"mode": "transit", "source": "estimate"})
    legs[0]["mode"] = "walking"
    rationalized = project_trip_result(_sample_row(
        plan_json=[rationalized_plan],
        quality_metrics={
            "commute_mode_request": "transit",
            "commute_mode_report": {
                "requested": "transit",
                "effective": "transit",
                "effective_reason": "requested",
                "degraded_leg_count": 3,
                "rationalized_leg_count": 1,
                "duration_source": "estimated",
            },
        },
    )).model_dump(mode="json")
    assert rationalized["plans"][0]["days"][0]["commute_legs"][0][
        "mode"
    ] == "walking"
    assert "commute_mode_report" not in rationalized

    legacy_taxi = _sample_plan()
    legacy_taxi["route_plan"]["day_groups"][0]["commute_legs"][0][
        "mode"
    ] = "taxi"
    taxi_payload = project_trip_result(_sample_row(
        plan_json=[legacy_taxi],
    )).model_dump(mode="json")
    assert taxi_payload["plans"][0]["days"][0]["commute_legs"][0][
        "mode"
    ] == "driving"


def _test_heading_prefix_symbols_are_removed() -> None:
    plan = _sample_plan()
    plan["plan_text"] = (
        "Day 1｜两江小渡 → 洪崖洞 → 山城巷\n\n"
        "第一天从两江小渡出发。\n"
        "---\n"
        "Day 2 · 龙门浩 · 下浩里\n\n"
        "第二天在南岸慢慢走。\n"
        "---\n"
        "Day 3：Food finish\n\n"
        "End with a food stop."
    )
    payload = project_trip_result(_sample_row(plan_json=[plan])).model_dump(mode="json")

    assert payload["plans"][0]["days"][0]["narrative"].startswith("第一天")
    assert not payload["plans"][0]["days"][0]["narrative"].startswith("｜")
    assert "两江小渡 → 洪崖洞" not in payload["plans"][0]["days"][0]["narrative"]
    assert payload["plans"][0]["days"][1]["narrative"].startswith("第二天")
    assert not payload["plans"][0]["days"][1]["narrative"].startswith("·")
    assert "龙门浩 · 下浩里" not in payload["plans"][0]["days"][1]["narrative"]


def _test_weather_status_falls_back_to_metrics_for_legacy_rows() -> None:
    row = _sample_row()
    row.quality_metrics = {
        "commute_mode_request": "driving",
        "weather_status": "skipped_no_travel_date",
    }
    row.plan_json[0].pop("weather_display", None)

    payload = project_trip_result(row).model_dump(mode="json")

    assert payload["weather"] == {
        "status": "skipped_no_travel_date",
        "city": "Chongqing",
        "days": [],
    }


def _test_must_include_report_projection() -> None:
    row = _sample_row()
    row.quality_metrics = {
        "commute_mode_request": "driving",
        "must_include_report": [
            {
                "name": "Hongya Cave",
                "place_id": 123,
                "status": "scheduled",
                "reason": "exact canonical name match",
                "matched_city": "Chongqing",
                "avoid_conflict": False,
                "match_status": "matched",
                "matched_via": "canonical_exact",
            },
            {
                "name": "Unknown Place",
                "place_id": 999,
                "status": "recorded_unmatched",
                "reason": "not found",
            },
            {
                "name": "Candidate Place",
                "place_id": 456,
                "status": "recorded_candidate",
                "reason": "candidate only",
            },
        ]
    }

    payload = project_trip_result(row).model_dump(mode="json")

    assert payload["must_include"] == [
        {
            "name": "Hongya Cave",
            "status": "scheduled",
            "place_id": 123,
            "reason": "exact canonical name match",
            "matched_city": "Chongqing",
            "avoid_conflict": False,
        },
        {
            "name": "Unknown Place",
            "status": "recorded_unmatched",
            "place_id": None,
            "reason": "not found",
            "matched_city": None,
            "avoid_conflict": None,
        },
        {
            "name": "Candidate Place",
            "status": "recorded_candidate",
            "place_id": None,
            "reason": "candidate only",
            "matched_city": None,
            "avoid_conflict": None,
        },
    ]
    serialized = json.dumps(payload["must_include"], ensure_ascii=False)
    assert "match_status" not in serialized
    assert "matched_via" not in serialized


def _test_must_include_absent_report_projects_empty() -> None:
    payload = project_trip_result(_sample_row()).model_dump(exclude_none=True)

    assert "must_include" not in payload


def _test_short_multiday_weather_degrades_to_empty_weather() -> None:
    row = _sample_row()
    row.plan_json[0]["weather_display"]["days"] = [
        row.plan_json[0]["weather_display"]["days"][0]
    ]

    payload = project_trip_result(row).model_dump(mode="json")

    assert payload["request"]["days"] == 3
    assert payload["weather"] == {
        "status": "skipped_date_out_of_range",
        "city": "Chongqing",
        "days": [],
    }


def _test_narrative_fallback_rules() -> None:
    missing_day = _sample_plan()
    missing_day["plan_text"] = "**Day 1：Only heading** Body for day one."
    assert project_trip_result(_sample_row(plan_json=[missing_day])).plans[0].days[1].narrative == ""

    one_day = _sample_plan()
    one_day["plan_text"] = "Plain one-day itinerary without headings. 天气提醒：注意带伞。"
    one_day["route_plan"]["day_groups"] = [_day_group(1, "Plain", 123)]
    one_day["composition_blueprint"]["days"] = [{"day": 1, "theme_label": "Plain", "stops": []}]
    one_day_payload = project_trip_result(
        _sample_row(plan_json=[one_day], days=1)
    ).model_dump(mode="json")
    assert one_day_payload["plans"][0]["days"][0]["narrative"].startswith(
        "Plain one-day itinerary"
    )
    assert "天气提醒" not in one_day_payload["plans"][0]["days"][0]["narrative"]

    multi_day = _sample_plan()
    multi_day["plan_text"] = "Plain multi-day itinerary without reliable headings."
    multi_day_payload = project_trip_result(
        _sample_row(plan_json=[multi_day])
    ).model_dump(mode="json")
    assert all(day["narrative"] == "" for day in multi_day_payload["plans"][0]["days"])


def _test_contract_unsupported_is_all_or_nothing() -> None:
    broken_plan = _sample_plan()
    broken_plan["route_plan"]["day_groups"][1]["places"] = []
    valid_plan = _sample_plan()

    try:
        project_trip_result(_sample_row(plan_json=[valid_plan, broken_plan]))
    except ResultContractUnsupported:
        pass
    else:
        raise AssertionError("one broken plan must make the whole result unsupported")

    try:
        project_trip_result(_sample_row(plan_json=[]))
    except ResultContractUnsupported:
        pass
    else:
        raise AssertionError("empty plan_json must be unsupported")

    short_plan = _sample_plan()
    short_plan["route_plan"]["day_groups"] = short_plan["route_plan"]["day_groups"][:2]
    short_plan["composition_blueprint"]["days"] = short_plan["composition_blueprint"]["days"][:2]
    try:
        project_trip_result(_sample_row(plan_json=[short_plan]))
    except ResultContractUnsupported:
        pass
    else:
        raise AssertionError("plan day count must match request.days")


def _test_workflow_refuses_to_persist_shortened_result() -> None:
    assert workflow._is_complete_plan_result(
        [_workflow_plan(3)],
        route_plans=[_workflow_route_plan(3)],
        expected_days=3,
    )
    assert not workflow._is_complete_plan_result(
        [_workflow_plan(3)],
        route_plans=[_workflow_route_plan(3), _workflow_route_plan(3)],
        expected_days=3,
    )

    valid = WorkflowResult(
        trip_request=AgentTripRequest(to_city="重庆", days=3),
        plans=[_workflow_plan(3)],
        route_plans=[_workflow_route_plan(3)],
    )
    workflow._assert_persistable_result(valid)

    short_route = WorkflowResult(
        trip_request=AgentTripRequest(to_city="重庆", days=3),
        plans=[_workflow_plan(3)],
        route_plans=[_workflow_route_plan(2)],
    )
    try:
        workflow._assert_persistable_result(short_route)
    except ValueError:
        pass
    else:
        raise AssertionError("short route plan must not be persisted")

    short_writer = WorkflowResult(
        trip_request=AgentTripRequest(to_city="重庆", days=3),
        plans=[_workflow_plan(2)],
        route_plans=[_workflow_route_plan(3)],
    )
    try:
        workflow._assert_persistable_result(short_writer)
    except ValueError:
        pass
    else:
        raise AssertionError("short writer output must not be persisted")


def _test_workflow_commute_report_uses_final_route_legs() -> None:
    request = AgentTripRequest(to_city="重庆", commute_mode="transit")
    route_plan = RoutePlan(
        label="A",
        day_groups=[
            RouteDayGroup(
                day=1,
                places=[_candidate(1), _candidate(2), _candidate(3)],
                commute_legs=[
                    CommuteLeg(
                        from_place_id=1,
                        to_place_id=2,
                        from_name="地点1",
                        to_name="地点2",
                        distance_meters=700,
                        duration_minutes=10,
                        mode="walking",
                        source="amap",
                    ),
                    CommuteLeg(
                        from_place_id=2,
                        to_place_id=3,
                        from_name="地点2",
                        to_name="地点3",
                        distance_meters=3000,
                        duration_minutes=20,
                        mode="transit",
                        source="estimate",
                    ),
                ],
            )
        ],
    )
    with patch.object(
        workflow,
        "get_settings",
        return_value=SimpleNamespace(commute_mode_enabled=True),
    ):
        metrics = workflow._commute_mode_metrics(
            request,
            [route_plan],
            {"by_effective_mode": {}},
        )
    assert metrics["commute_mode_request"] == "transit"
    assert metrics["commute_mode_report"] == {
        "requested": "transit",
        "effective": "transit",
        "effective_reason": "requested",
        "degraded_leg_count": 1,
        "rationalized_leg_count": 1,
        "duration_source": "mixed",
    }


async def _test_workflow_persists_request_dates() -> None:
    captured: dict = {}

    class FakeResult:
        def scalar_one(self):
            return 987

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def execute(self, statement, params):
            captured["sql"] = str(statement)
            captured["params"] = params
            return FakeResult()

        async def commit(self):
            captured["committed"] = True

    result = WorkflowResult(
        trip_request=AgentTripRequest(
            to_city="重庆",
            days=1,
            start_date="2026-07-01",
            end_date="2026-07-01",
        ),
        plans=[
            PlanOutput(
                plan_name="测试路线",
                plan_text="Day 1 body",
                used_place_ids=[101],
                day_place_names=[["地点101"]],
            )
        ],
        route_plans=[
            RoutePlan(
                label="A",
                day_groups=[
                    RouteDayGroup(day=1, places=[_candidate(101)]),
                ],
            )
        ],
    )
    result.cost_estimate_snapshots = list(unavailable_snapshots_for_result(
        result,
        estimated_at=datetime(2026, 8, 4, 8, 0, tzinfo=timezone.utc),
        failure_code="offline_persistence_fixture_unavailable",
    ))

    with (
        patch.object(workflow, "_weather_display_for_persistence", AsyncMock(return_value={})),
        patch.object(workflow, "get_session_factory", return_value=lambda: FakeSession()),
    ):
        record_id = await workflow._save_plan_record("structured query", result)

    assert record_id == 987
    assert captured["committed"] is True
    assert "start_date, end_date" in captured["sql"]
    assert captured["params"]["start_date"] == date(2026, 7, 1)
    assert captured["params"]["end_date"] == date(2026, 7, 1)
    persisted_metrics = json.loads(captured["params"]["quality_metrics"])
    assert persisted_metrics["commute_mode_request"] == "driving"
    assert "commute_mode_report" not in persisted_metrics


async def _test_http_contract() -> None:
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        plan = _sample_plan()
        plan["accommodation"] = {
            "name": "解放碑(商圈)",
            "latitude": 29.556728,
            "longitude": 106.57688,
            "source": "auto_recommended",
            "reason": "步行可达多数主景点",
            "user_input_unmatched": "",
        }
        projected = project_trip_result(_sample_row(plan_json=[plan]))
        with patch.object(
            trip_results,
            "get_trip_result",
            new=AsyncMock(return_value=projected),
        ) as mocked:
            response = await client.get("/trip/results/536")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["result_id"] == 536
        assert body["plans"][0]["days"][0]["places"][0]["place_id"] == 123
        assert body["plans"][0]["accommodation"] == {
            "name": "解放碑(商圈)",
            "latitude": 29.556728,
            "longitude": 106.57688,
            "source": "auto_recommended",
            "reason": "步行可达多数主景点",
        }
        _assert_no_public_legacy_fields(body)
        mocked.assert_awaited_once_with(536, job_id=None)

        empty_plan = _sample_plan()
        for day_group in empty_plan["route_plan"]["day_groups"]:
            for leg in day_group["commute_legs"]:
                leg.pop("encoded_polyline", None)
                leg["mode"] = "walking"
        empty_projected = project_trip_result(_sample_row(
            plan_json=[empty_plan],
            quality_metrics={"commute_mode_request": "transit"},
        ))
        with patch.object(
            trip_results,
            "get_trip_result",
            new=AsyncMock(return_value=empty_projected),
        ):
            empty_response = await client.get("/trip/results/536")
        assert empty_response.status_code == 200, empty_response.text
        empty_body = empty_response.json()
        assert empty_body["schema_version"] == "2.1"
        assert empty_body["plans"][0]["days"][0]["commute_legs"][0][
            "encoded_polyline"
        ] == ""

        with patch.object(
            trip_results,
            "get_trip_result",
            new=AsyncMock(side_effect=ResultContractUnsupported("bad record")),
        ):
            unsupported = await client.get("/trip/results/536?job_id=job-536")
        assert unsupported.status_code == 422, unsupported.text
        assert unsupported.json()["error"]["code"] == "RESULT_CONTRACT_UNSUPPORTED"

        with patch.object(
            trip_results,
            "get_trip_result",
            new=AsyncMock(return_value=None),
        ):
            missing = await client.get("/trip/results/999999")
        assert missing.status_code == 404, missing.text
        assert missing.json()["error"]["code"] == "RESULT_NOT_FOUND"


def _write_fixture(name: str, payload: dict) -> None:
    path = Path("reports") / f"v0.8.3-result-page-contract-{name}.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _generate_fixtures() -> None:
    normal = project_trip_result(_sample_row()).model_dump(
        mode="json",
        exclude_none=True,
        exclude={"must_include", "time_preferences"},
    )
    _write_fixture("normal-success", normal)

    _write_fixture("weather-ok", normal)

    skipped_row = _sample_row()
    skipped_row.quality_metrics = {
        "commute_mode_request": "driving",
        "weather_status": "skipped_no_travel_date",
    }
    skipped_row.plan_json[0] = deepcopy(skipped_row.plan_json[0])
    skipped_row.plan_json[0].pop("weather_display", None)
    _write_fixture(
        "skipped-weather",
        project_trip_result(skipped_row).model_dump(
            mode="json",
            exclude_none=True,
            exclude={"must_include", "time_preferences"},
        ),
    )

    _write_fixture(
        "unsupported-record",
        {
            "ok": False,
            "error": {
                "code": "RESULT_CONTRACT_UNSUPPORTED",
                "message": "该攻略由旧版本生成，暂不支持打开，请重新生成",
            },
        },
    )

    empty_narrative_plan = _sample_plan()
    empty_narrative_plan["plan_text"] = "**Day 1：Only one day** Body only."
    _write_fixture(
        "empty-narrative",
        project_trip_result(_sample_row(plan_json=[empty_narrative_plan])).model_dump(
            mode="json",
            exclude_none=True,
            exclude={"must_include", "time_preferences"},
        ),
    )


async def main() -> None:
    _test_projection_shape()
    _test_all_modes_preserve_encoded_polyline()
    _test_old_record_polyline_defaults_empty()
    _test_commute_mode_schema_2_projection_cases()
    _test_heading_prefix_symbols_are_removed()
    _test_weather_status_falls_back_to_metrics_for_legacy_rows()
    _test_must_include_report_projection()
    _test_must_include_absent_report_projects_empty()
    _test_short_multiday_weather_degrades_to_empty_weather()
    _test_narrative_fallback_rules()
    _test_contract_unsupported_is_all_or_nothing()
    _test_workflow_refuses_to_persist_shortened_result()
    _test_workflow_commute_report_uses_final_route_legs()
    await _test_workflow_persists_request_dates()
    await _test_http_contract()
    _generate_fixtures()
    print("trip results API tests PASSED")


if __name__ == "__main__":
    asyncio.run(main())
