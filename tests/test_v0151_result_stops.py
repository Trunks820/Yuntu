"""v0.15.1: Result 2.3 cards get the Writer's place paragraphs, period, stay and planned time."""
from __future__ import annotations

from src.api.result_stops import practical_line, split_day_text, stop_fields
from src.api.trip_results import _project_days
from src.agents.schema import (
    CandidatePlace,
    CommuteLeg,
    DayTimeline,
    DayTimelineItem,
    RouteDayGroup,
    RoutePlan,
    TimelineMove,
)
from src.config import get_settings

PARK, NOODLE = "太子湾公园", "同兴春·杭州面馆拌川（西湖店）"


def _plan(plan_text: str, *, timeline: DayTimeline | None = None) -> dict:
    park = CandidatePlace(place_id=1, name=PARK, place_type="park", amap_rating="4.6")
    noodle = CandidatePlace(place_id=2, name=NOODLE, place_type="restaurant",
                            amap_open_time="10:00-21:00 10:00-21:00", amap_avg_price="35.00", amap_rating="4.5")
    group = RouteDayGroup(
        day=1, places=[park, noodle], timeline=timeline,
        commute_legs=[CommuteLeg(from_place_id=1, to_place_id=2, from_name=PARK, to_name=NOODLE,
                                 distance_meters=3000, duration_minutes=23)],
    )
    return {"plan_text": plan_text, "route_plan": RoutePlan(label="A", day_groups=[group]).model_dump(mode="json")}


KEYED_TEXT = (
    f"Day 1：{PARK} -> {NOODLE}\n"
    "从住处出发约 21 分钟。\n"
    "第一天沿西湖南线慢慢走，中午吃碗拌川。\n"
    f"{PARK}：十一月的草坪很开阔，适合一个人慢慢散步。\n"
    f"{NOODLE}：中午来一碗拌川，简单扎实。\n"
    f"通勤参考：{PARK}→{NOODLE}，驾车约 23 分钟。\n"
    "晚上早点回住处休息。"
)

TIMELINE = DayTimeline(
    start="08:30", return_at="13:30",
    items=[
        DayTimelineItem(kind="sight", place_id=1, arrive="08:51", stay_minutes=90,
                        move_to_next=TimelineMove(mode="打车", minutes=23)),
        DayTimelineItem(kind="lunch", place_id=2, arrive="12:02", stay_minutes=60, wait_minutes=0),
    ],
)


def test_place_paragraphs_move_to_cards_and_the_day_keeps_its_opening_and_wrap(monkeypatch):
    monkeypatch.setattr(get_settings(), "result_planned_time_enabled", False)
    [day] = _project_days(_plan(KEYED_TEXT, timeline=TIMELINE))
    park, noodle = day.places
    assert day.narrative == "从住处出发约 21 分钟。\n第一天沿西湖南线慢慢走，中午吃碗拌川。\n晚上早点回住处休息。"
    assert park.activity_note == "十一月的草坪很开阔，适合一个人慢慢散步。\n高德评分 4.6"
    assert noodle.activity_note == "中午来一碗拌川，简单扎实。\n营业 10:00–21:00 · 人均约 35 元 · 高德评分 4.5"
    assert (park.schedule.period, park.stay_minutes) == ("morning", 90)
    assert (noodle.schedule.period, noodle.stay_minutes) == ("afternoon", 60)
    assert park.schedule.exact_start is None and park.schedule.exact_time_source is None
    assert park.brief == ""  # unchanged field, still the evidence reason


def test_planned_time_is_rounded_to_the_quarter_only_behind_the_flag(monkeypatch):
    monkeypatch.setattr(get_settings(), "result_planned_time_enabled", True)
    [day] = _project_days(_plan(KEYED_TEXT, timeline=TIMELINE))
    park, noodle = day.places
    assert (park.schedule.exact_start, park.schedule.exact_time_source) == ("08:45", "planned")
    assert noodle.schedule.exact_start == "12:00" and noodle.schedule.exact_end is None


def test_a_route_without_a_stored_clock_shows_no_time_on_cards(monkeypatch):
    # The Writer may use an estimate; cards only show a clock the scheduler computed.
    monkeypatch.setattr(get_settings(), "result_planned_time_enabled", True)
    [day] = _project_days(_plan(KEYED_TEXT))
    assert all(p.schedule is None and p.stay_minutes is None for p in day.places)
    assert all(p.activity_note for p in day.places)
    assert "schedule" not in day.places[0].model_dump()


def test_legacy_results_keep_the_old_day_text_and_no_card_fields():
    [day] = _project_days(_plan(KEYED_TEXT, timeline=TIMELINE), cards=False)
    assert f"{PARK}：十一月的草坪很开阔" in day.narrative
    assert all(set(p.model_dump()) >= {"brief"} and "activity_note" not in p.model_dump() for p in day.places)


def test_basic_plan_sentences_also_become_card_notes():
    text = f"Day 1：{PARK} -> {NOODLE}\n到{PARK}后，沿草坪走一圈。\n到{NOODLE}后，坐下吃午饭。"
    [day] = _project_days(_plan(text))
    assert [p.activity_note.split("\n")[0] for p in day.places] == ["沿草坪走一圈。", "坐下吃午饭。"]
    assert day.narrative == ""


def test_basic_plan_filler_goes_and_meal_lines_stay_with_their_stop():
    text = (
        f"Day 1\n住宿往返：去程约 7 分钟。\n晚饭前回住处歇一会儿\n"
        f"到{PARK}后，沿草坪走一圈。\n随后按既定路线前往下一站。\n"
        f"到{NOODLE}后，午餐与休整。\n拌川是这家店的招牌。\n"
        "当天仅安排以上地点，依次完成对应活动；地点之间沿既定路线前往，途中可按自身节奏短暂休息，完成后结束当天行程。"
    )
    [day] = _project_days(_plan(text))
    # A bare "Day 1" heading reads the next line as its title (old behavior).
    assert day.narrative == "住宿往返：去程约 7 分钟\n晚饭前回住处歇一会儿"
    assert day.places[0].activity_note.split("\n")[0] == "沿草坪走一圈。"
    assert day.places[1].activity_note.startswith("午餐与休整。\n拌川是这家店的招牌。\n营业")


def test_text_without_place_paragraphs_keeps_the_old_narrative():
    text = f"Day 1：{PARK} -> {NOODLE}\n今天随意逛逛西湖边。"
    [day] = _project_days(_plan(text))
    assert day.narrative == "今天随意逛逛西湖边。"
    assert all(place.activity_note is None for place in day.places)


def test_split_and_practical_helpers():
    kept, notes = split_day_text(f"开场。\n{PARK}：散步。\n<!-- food -->{NOODLE}：吃面。<!-- /food -->", {1: PARK, 2: NOODLE})
    assert kept == ["开场。"] and notes == {1: "散步。", 2: "吃面。"}
    assert practical_line({"place_type": "attraction", "amap_avg_price": "60", "amap_open_time": "x" * 40}) == ""
    assert stop_fields(None, planned_time=True) == {}
