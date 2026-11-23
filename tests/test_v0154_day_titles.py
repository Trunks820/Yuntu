"""Check how Result 2.3 names a day from its projected stops."""

import pytest

from src.api.trip_results import _project_days


def _plan(stops):
    ids = range(1, len(stops) + 1)
    return {
        "route_plan": {
            "day_groups": [{
                "day": 1,
                "places": [
                    {"place_id": place_id, "name": name, "place_type": category}
                    for place_id, (name, category, _) in zip(ids, stops)
                ],
                "commute_legs": [
                    {"from_place_id": place_id, "to_place_id": place_id + 1}
                    for place_id in ids[:-1]
                ],
            }],
        },
        "composition_blueprint": {
            "days": [{
                "day": 1,
                "theme_label": "美食慢逛",
                "stops": [
                    {"place_id": place_id, "role": role}
                    for place_id, (_, _, role) in zip(ids, stops)
                ],
            }],
        },
    }


@pytest.mark.parametrize(("stops", "expected"), [
    (
        [
            ("山海关路", "market", "secondary_activity"),
            ("古德寺", "attraction", "secondary_activity"),
            ("汉口江滩", "park", "anchor_activity"),
            ("汉口江滩", "park", "anchor_activity"),
            ("武汉科技馆", "museum", "secondary_activity"),
            ("江汉关博物馆", "museum", "secondary_activity"),
        ],
        "古德寺 · 汉口江滩 · 武汉科技馆",
    ),
    (
        [
            ("真茶屋（江汉路店）", "other", "coffee_stop"),
            ("湖北美术馆（东湖馆）", "museum", "anchor_activity"),
            ("天天渔港(汉街店)", "restaurant", "meal_stop"),
        ],
        "湖北美术馆（东湖馆）",
    ),
    (
        [
            ("天天渔港(汉街店)", "restaurant", "meal_stop"),
            ("天天渔港（东湖店）", "restaurant", "meal_stop"),
            ("真茶屋（江汉路店）", "other", "coffee_stop"),
            ("真茶屋外卖档", "food", "filler"),
        ],
        "天天渔港 · 真茶屋 · 真茶屋外卖档",
    ),
    ([("（总店）", "restaurant", "meal_stop")], "美食慢逛"),
])
def test_day_title_uses_unique_place_names_in_route_order(stops, expected):
    plan = _plan(stops)
    [current] = _project_days(plan)
    [legacy] = _project_days(plan, cards=False)

    assert current.title == expected
    assert [place.name for place in current.places] == [name for name, _, _ in stops]
    assert legacy.title == plan["composition_blueprint"]["days"][0]["theme_label"] == "美食慢逛"
