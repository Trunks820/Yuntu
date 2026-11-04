"""Guest port checks: planner stays off, lodging has no invented price."""

from src.agents.lodging_section import build_lodging_display, render_lodging_section
from src.agents.preferences import FOOD, normalize_preferences
from src.agents.schema import AccommodationSuggestion
from src.config import get_settings


def test_route_planner_and_lodging_flags_default_off():
    settings = get_settings()
    assert settings.route_planner_enabled is False
    assert settings.route_planner_hotel_lodging_enabled is False
    assert settings.route_planner_lodging_first_enabled is False
    assert settings.lodging_section_v2_enabled is False


def test_lodging_without_sourced_price_says_pending():
    accommodation = AccommodationSuggestion(
        name="解放碑",
        latitude=29.55,
        longitude=106.57,
        source="auto_recommended",
        reason="靠近当天的点",
    )
    display = build_lodging_display(accommodation=accommodation, prices=None)
    assert display.price_status == "待核实"
    assert all(
        group.nightly_low_cny is None and group.nightly_high_cny is None
        for group in display.groups
    )
    text = render_lodging_section(accommodation=accommodation, prices=None)
    assert "待核实" in text
    assert "元" not in text


def test_preferences_normalize_known_labels():
    normalized = normalize_preferences(["美食", "夜景", "随便看看"])
    assert FOOD in normalized.scoring_dimensions
    assert "night_view" in normalized.scoring_dimensions
    assert "随便看看" in normalized.unknown_labels
