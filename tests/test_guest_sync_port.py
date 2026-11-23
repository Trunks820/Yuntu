"""Open-source guest port checks: v0.15.1 defaults, single-key routing, and lodging safety."""

from src.agents import llm
from src.agents.lodging_section import build_lodging_display, render_lodging_section
from src.agents.preferences import FOOD, normalize_preferences
from src.agents.schema import AccommodationSuggestion
from src.api.app import app
from src.config import Settings
from src.model_routes import resolve_open_source_leg, validate_model_routes


def test_v0151_open_source_defaults():
    settings = Settings(_env_file=None)
    assert settings.route_planner_enabled is True
    assert settings.writer_prompt_version == "article"
    assert settings.result_planned_time_enabled is True
    assert settings.llm_review_enabled is False
    assert settings.v012_trip_creation_enabled is True
    assert settings.v012_trip_worker_enabled is True
    assert settings.route_planner_hotel_lodging_enabled is False
    assert settings.lodging_section_v2_enabled is False
    assert settings.rollinggo_enabled is False


def test_single_key_deepseek_routes_all_roles_without_startup_error():
    settings = Settings(_env_file=None, deepseek_api_key="sk-test-deepseek")
    assert validate_model_routes(settings) == []
    for agent in ("selector", "route_planner", "writer", "repair", "extract"):
        leg = resolve_open_source_leg(settings, agent)
        assert leg.provider == "deepseek"
        assert leg.model == "deepseek-chat"
        assert leg.api_key == "sk-test-deepseek"
    backup = resolve_open_source_leg(settings, "route_planner", "backup")
    assert backup.api_key == ""


def test_single_key_gemini_routes_all_roles_and_table_config(monkeypatch):
    settings = Settings(_env_file=None, gemini_api_key="test-gemini-key")
    monkeypatch.setattr(llm, "get_settings", lambda: settings)
    for role in ("intent", "selector", "route_planner", "writer"):
        cfg = llm.resolve_role_config(role)
        assert cfg.provider == "gemini"
        assert cfg.api_key == "test-gemini-key"
    assert llm.resolve_role_config("writer").model == "gemini-3.8-flash"
    assert llm.resolve_role_config("route_planner").model == "gemini-3.8-flash"


def test_per_role_writer_key_falls_back_to_other_roles(monkeypatch):
    settings = Settings(
        _env_file=None,
        writer_provider="deepseek",
        writer_model="deepseek-chat",
        writer_api_key="sk-writer-only",
    )
    monkeypatch.setattr(llm, "get_settings", lambda: settings)
    for role in ("intent", "selector", "route_planner", "writer"):
        cfg = llm.resolve_role_config(role)
        assert cfg.provider == "deepseek"
        assert cfg.api_key == "sk-writer-only"


def test_lodging_without_sourced_price_has_no_invented_price():
    accommodation = AccommodationSuggestion(
        name="解放碑",
        latitude=29.55,
        longitude=106.57,
        source="auto_recommended",
        reason="靠近当天的点",
    )
    display = build_lodging_display(accommodation=accommodation, prices=None)
    assert display.layout == "area_only"
    assert display.groups == []
    text = render_lodging_section(accommodation=accommodation, prices=None)
    assert "解放碑一带" in text
    assert "元" not in text


def test_public_open_source_routes_registered():
    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/destinations" in paths
    assert "/trip" in paths
    assert "/trip/async" in paths
    assert "/trip/poi-catalog" in paths
    assert "/trip/poi-catalog/selection" in paths
    assert "/trip/poi-inspiration" in paths
    assert "/trip/poi-inspiration/cities" in paths


def test_preferences_normalize_known_labels():
    normalized = normalize_preferences(["美食", "夜景", "随便看看"])
    assert FOOD in normalized.scoring_dimensions
    assert "night_view" in normalized.scoring_dimensions
    assert "随便看看" in normalized.unknown_labels


def test_schema_sql_includes_plan_record_accommodation_columns():
    from pathlib import Path

    sql_dir = Path(__file__).resolve().parent.parent / "sql"
    schema_sql = (sql_dir / "schema.sql").read_text(encoding="utf-8")
    for col in (
        "accommodation_name",
        "accommodation_lat",
        "accommodation_lng",
        "accommodation_source",
        "travel_canonical_place_opening_schedule",
    ):
        assert col in schema_sql

    seed_sql = (sql_dir / "seed_chongqing.sql").read_text(encoding="utf-8")
    assert "INSERT INTO travel_place_summary" in seed_sql
    assert "INSERT INTO travel_canonical_place_image" in seed_sql


def test_legacy_model_names_and_workflow_policy_default(monkeypatch):
    import inspect
    from src.agents.workflow import run_trip_workflow

    sig = inspect.signature(run_trip_workflow)
    assert sig.parameters["generation_policy_version"].default == "v0.12"

    settings = Settings(
        _env_file=None,
        deepseek_api_key="sk-ds",
        selector_model="gpt-5.5",
        review_provider="gemini",
        review_model="gemini-flash-latest",
        review_api_key="gem-key",
    )
    monkeypatch.setattr(llm, "get_settings", lambda: settings)
    assert resolve_open_source_leg(settings, "selector").model == "deepseek-chat"
    assert llm.resolve_role_config("review").model == "gemini-3.8-flash"


def test_food_preference_notice_reconciles_scheduled_seed_restaurants():
    from src.agents.preference_notices import build_preference_notices
    from src.agents.schema import CandidatePlace, RouteDayGroup, RoutePlan, TripRequest

    request = TripRequest(to_city="重庆", days=2, preferences=["美食"])
    route = RoutePlan(
        label="A",
        day_groups=[
            RouteDayGroup(
                day=1,
                area="解放碑",
                places=[
                    CandidatePlace(
                        place_id=101,
                        name="大井巷社区火锅",
                        place_type="restaurant",
                        latitude=29.558,
                        longitude=106.578,
                        mention_count=0,
                        top_reasons=[],
                    ),
                    CandidatePlace(
                        place_id=102,
                        name="杨记隆府(解放碑总店)",
                        place_type="restaurant",
                        latitude=29.557,
                        longitude=106.577,
                        mention_count=0,
                        top_reasons=[],
                    ),
                ],
            )
        ]
    )
    notices = build_preference_notices(request, route)
    assert all("没排到网友提过的正餐店" not in notice for notice in notices.trip_lines)



