"""Result 2.3 moves the Writer's trip-wide prose to the plan."""

import pytest

from scripts.test_trip_results_api import _sample_row
from src.api.export_source import build_export_source_from_trip_result
from src.api.trip_results import project_trip_result
from src.config import get_settings


# Text from the 2927 result snapshot, replayed as persisted keyed paragraphs.
OPENING = (
    "武汉3日 · citywalk与夜景美食之旅\n"
    "11月20日—22日｜2人｜住徐家棚｜节奏适中、不早起、少爬坡少排队\n"
    "三天日落都在17:24前后，傍晚的江滩和码头留给光线最好的时刻。"
    "这份行程按你排好的顺序和时间走，下面每站附上实用提示和周边吃法。"
)
TIPS = (
    "几点整体提醒。三天都是8:30出发，符合你“不想早起”的偏好，节奏适中不赶。"
    "行程里爬坡点很少，江滩、绿道、美术馆都是平地动线，穿舒服的鞋即可。"
    "周末江汉路、汉街人多，尽量“买完就走”，少排队靠早到（午饭都卡在11:30左右）。"
    "武汉11月下旬早晚凉、江边风大，外套和薄围巾带上；科技馆、美术馆记得提前预约。"
    "住处徐梓棚到各点自驾都在15—20分钟，停车建议提前查好目的地周边停车场。"
)
ACCESS = "住宿往返：去程约 20 分钟，回程约 15 分钟；按住宿区域参考点计算，实际耗时以出行时为准。"
DAY_OPENING = "晚饭前回住处歇一会儿"
DAY_WRAP = "今天逛完早点回去休息。"
LAST_STOP = "提示：轮渡票价便宜、班次密，坐一趟往返很值。20:36回住处，三天到此收尾。"


def _row(*, opening=OPENING, tips=TIPS, previous_tips=None, cards=True):
    names = ["古德寺", "江汉关博物馆", "中华路码头"]
    text = (
        "Day 1：古德寺 -> 江滩\n"
        f"{ACCESS}\n{DAY_OPENING}\n{opening}\n古德寺：看看老建筑。\n{DAY_WRAP}\n"
        "Day 2：江汉关博物馆 -> 江滩\n江汉关博物馆：看钟楼。\n"
        f"Day 3：中华路码头 -> 住处\n中华路码头：{LAST_STOP}{tips}\n"
        "通勤参考：码头到住处约 17 分钟。"
    )
    persisted_plan = {
        "plan_text": text,
        "route_plan": {"day_groups": [
            {"day": day, "places": [{"place_id": day, "name": name, "place_type": "attraction"}]}
            for day, name in enumerate(names, 1)
        ]},
        "travel_tips": previous_tips,
    }
    row = _sample_row(plan_json=[persisted_plan])
    row.to_city = "武汉"
    if cards:
        row.generation_policy_version = "v0.12"
        row.job_delivery_json = {"outcome": "DETAILED", "review_state": "MODEL_PASSED", "selector_source": "LLM"}
        row.quality_metrics.update(
            generation_policy_version="v0.12",
            published_variant="normal",
            delivery_status="NORMAL",
            delivery=row.job_delivery_json,
        )
    return row


def test_replays_2927_opening_and_inline_tips_into_the_projected_plan(monkeypatch):
    monkeypatch.setattr(get_settings(), "result_schema_22_enabled", False)
    result = project_trip_result(_row())
    assert result.schema_version == "2.3"
    [plan] = result.model_dump()["plans"]
    assert plan["opening"] == OPENING
    assert plan["days"][0]["narrative"] == f"{ACCESS}\n{DAY_OPENING}\n{DAY_WRAP}"
    assert plan["days"][2]["places"][-1]["activity_note"] == LAST_STOP
    assert plan["travel_tips"] == [{"title": "整体提醒", "content": TIPS}]

    pdf_plan = build_export_source_from_trip_result(result).source["result"]["plans"][0]
    assert pdf_plan["travel_tips"] == plan["travel_tips"]


def test_preserves_standalone_tip_prose_and_reserves_a_slot_with_four_existing_tips(monkeypatch):
    monkeypatch.setattr(get_settings(), "result_schema_22_enabled", True)
    previous_tips = [
        {"title": f"提醒{index}", "content": f"第 {index} 天出门前充好手机电量。"}
        for index in range(1, 5)
    ]
    tips = "**行前小贴士**\n出门前看天气。\n记得带  一件外套。"
    day_sentence = "今天先逛老街。"
    row = _row(opening=OPENING + day_sentence, tips="\n\n" + tips, previous_tips=previous_tips)
    [plan] = project_trip_result(row).model_dump()["plans"]
    assert plan["opening"] == OPENING
    assert plan["days"][0]["narrative"] == f"{ACCESS}\n{DAY_OPENING}\n{day_sentence}\n{DAY_WRAP}"
    assert plan["days"][2]["narrative"] == ""
    assert plan["travel_tips"] == [*previous_tips[:3], {"title": "整体提醒", "content": tips}]


def test_regular_day_text_and_existing_advice_keep_their_projection(monkeypatch):
    monkeypatch.setattr(get_settings(), "result_schema_22_enabled", True)
    advice = [{"title": "查看开放时段", "content": "出发前核对官方开放时间。"}]
    day_intro = "今天从老城开始慢慢逛。"
    [plan] = project_trip_result(_row(opening=day_intro, tips="", previous_tips=advice)).model_dump()["plans"]
    assert "opening" not in plan
    assert plan["travel_tips"] == advice
    assert plan["days"][0]["narrative"] == f"{ACCESS}\n{DAY_OPENING}\n{day_intro}\n{DAY_WRAP}"
    assert plan["days"][2]["places"][-1]["activity_note"] == LAST_STOP


@pytest.mark.parametrize("schema_22_enabled", [False, True])
def test_legacy_result_keeps_trip_text_in_the_original_days(monkeypatch, schema_22_enabled):
    monkeypatch.setattr(get_settings(), "result_schema_22_enabled", schema_22_enabled)
    result = project_trip_result(_row(cards=False)).model_dump()
    assert result["schema_version"] == ("2.2" if schema_22_enabled else "2.1")
    [plan] = result["plans"]
    assert "opening" not in plan
    assert plan["travel_tips"] is None
    assert " ".join(OPENING.splitlines()) in plan["days"][0]["narrative"]
    assert LAST_STOP + TIPS in plan["days"][2]["narrative"]
