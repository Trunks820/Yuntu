"""v0.15.1 article mode: one guide, a heading per stop, the rules checked per stop after writing."""
from __future__ import annotations

import json

import pytest

from src.agents import final_writer, llm
from src.agents import route_planning as rp
from src.agents.article_writer import (
    ARTICLE_SYSTEM_PROMPT,
    article_skeleton,
    article_to_keyed,
    build_article_prompt,
)
from src.agents.evidence_strength import build_structured_evidence_payload
from src.agents.fact_expression_taxonomy import unsupported_fact_pattern_matches
from src.agents.schema import CandidatePlace, RetrievalResult, TripRequest
from src.config import get_settings

NAMES = ["湖边公园", "老街面馆", "古塔景区"]


@pytest.fixture(autouse=True)
def article(monkeypatch):
    monkeypatch.setattr(get_settings(), "writer_prompt_version", "article")


def _route():
    request = TripRequest(to_city="杭州", days=1, start_date="2026-11-18", people_count=2, preferences=["citywalk"])
    places = [
        CandidatePlace(place_id=i + 1, name=name, place_type=kind, latitude=30.25, longitude=120.15 + i * 0.002,
                       typical_visit_minutes=60, typical_visit_source="manual",
                       top_reasons=[{"reason": reason}])
        for i, (name, kind, reason) in enumerate(zip(
            NAMES, ["park", "restaurant", "attraction"],
            ["环湖步道可以绕湖一圈，门票10元", "老街面馆的片儿川很有名", "登塔可以俯瞰全城"]))
    ]
    route = rp._v2_make_plan([places], request, (30.25, 120.15), "auto_recommended", {})
    payload = build_structured_evidence_payload(RetrievalResult(city="杭州", candidates=places), route_plans=[route])
    return request, route, payload


def _convert(raw):
    request, route, payload = _route()
    return article_to_keyed(raw, plan_index=1, trip_request=request, route_plan=route,
                            structured_evidence_payload=payload, hints={})


ARTICLE = """欢迎来到杭州！这一天都在湖边和老城。

## 第 1 天 · 11月18日 周三
湖光与老街

### 湖边公园
**09:00 到达｜停留约 60 分钟**
- **怎么逛**：沿着环湖步道走，门票10元，湖心的石桥最好看。
- 门票要30元，记得带好零钱。

### 老街面馆
来一碗片儿川，人均二三十就能吃饱。坐公交约10分钟就到下一站。

### 古塔景区
登塔俯瞰全城。

### 行前小贴士
湖边公园：早点去人少。
祝你玩得开心！
"""


def test_article_prompt_and_switches():
    request, route, payload = _route()
    prompt = build_article_prompt(trip_request=request, route_plan=route, structured_evidence_payload=payload, hints={})
    assert "每一站用一个小标题“### 站名”开头" in prompt
    assert "当天日落约" in prompt and "【各站材料】" in prompt and "- 湖边公园：游记里的说法：" in prompt
    assert final_writer.single_plan_system_prompt() == ARTICLE_SYSTEM_PROMPT
    assert llm._ds_system_prompt() == ARTICLE_SYSTEM_PROMPT  # no JSON suffix for the standby
    assert final_writer.opus_prompt_label() == "opus_article"
    assert final_writer._writer_temperature("original") == pytest.approx(get_settings().writer_article_temperature)
    assert unsupported_fact_pattern_matches("据说门票 45 元，需要提前预约") == []


def test_article_splits_into_stops_openings_and_checks_per_stop():
    result = _convert(ARTICLE)
    assert result.failure == "" and result.data is not None
    fragments = {item["place_id"]: item["text"] for item in result.data["poi_fragments"]}
    # the stop's own price stays; one the material does not have goes
    assert "门票10元" in fragments[1] and "30元" not in fragments[1]
    # the clock line and markdown are gone; it is one paragraph
    assert "09:00" not in fragments[1] and "**" not in fragments[1] and "\n" not in fragments[1]
    # "人均二三十" goes; transit minutes are the card's, the sentence stays
    assert fragments[2] == "坐公交约10分钟就到下一站。"
    # the closing line ends the last card
    assert fragments[3] == "登塔俯瞰全城。祝你玩得开心！"
    opening = result.data["day_openings"][0]["text"]
    assert opening.startswith("欢迎来到杭州！") and "湖光与老街" in opening
    # an intro line starting "站名：" would be read as that stop's paragraph
    assert "· 湖边公园：早点去人少。" in opening
    assert result.data["day_wraps"] == []
    dropped = [a for a in result.actions if a["action"] == "article_sentence_dropped"]
    assert {tuple(a["why"]) for a in dropped} == {("价格 30元",), ("价格 人均二三十",)}
    split = next(a for a in result.actions if a["action"] == "article_split")
    assert split["matched"] == split["stops"] == 3


def test_missing_or_reordered_stop_fails_the_draft():
    # the last heading missing leaves a stop unmatched; a middle one makes a later heading out of order
    assert _convert(ARTICLE.replace("### 古塔景区\n", "")).failure == "stop_missing"
    assert _convert(ARTICLE.replace("### 老街面馆\n", "")).failure == "stop_out_of_order"
    reordered = "### 老街面馆\n吃面。\n### 湖边公园\n散步。\n### 古塔景区\n登塔。\n"
    assert _convert(reordered).failure == "stop_out_of_order"


def test_weather_restriction_and_open_day_sentences_go():
    text = ("### 湖边公园\n傍晚在湖边看日落最美。距离日落还有段时间。下午3点半来看日落。湖心石桥值得一看。注意防晒补水。"
            "如果遇到阴天，湖面会灰一些。当天会下雨，记得带伞。\n"
            "### 老街面馆\n周末单双号限行。片儿川很好吃。\n"
            "### 古塔景区\n古塔周一正常开放。不少场馆周一闭馆，去前确认。登塔。\n")
    result = _convert(text)
    fragments = {item["place_id"]: item["text"] for item in result.data["poi_fragments"]}
    # sunset remarks are the model's; the card shows the clock. A made-up sunset clock still goes.
    assert "看日落最美。距离日落还有段时间。湖心石桥" in fragments[1]
    # seasonal advice and conditions stay; a claim about the trip day's weather goes
    assert "注意防晒补水。" in fragments[1] and "如果遇到阴天" in fragments[1]
    assert "当天会下雨" not in fragments[1]
    assert fragments[2] == "片儿川很好吃。"
    # an open weekday the material does not carry goes; a closed-day reminder stays
    assert fragments[3] == "不少场馆周一闭馆，去前确认。登塔。"


def test_transit_line_numbers_go_minutes_and_stops_stay():
    """11-18 H3: "停留约 75 分钟", "溜达 10 分钟", "坐两站公交" were cut as transit claims."""
    from src.agents.article_writer import _Material, check_sentence

    def why(sentence, material=""):
        return check_sentence(sentence, _Material.of(material), stop=None, route_clocks=[510, 546], sunsets=[])

    assert why("行程参考：08:30 从王府井出发（公交约 36 分钟）→ 09:06 到达，停留约 75 分钟。") == []
    assert why("早上不用赶公交，从王府井溜达 10 分钟就能到。") == []
    assert why("坐两站公交就到了北新桥。") == []
    assert why("坐地铁2号线到西溪湿地北站。") == ["公交线路 2号线"]
    assert why("换乘18路公交过去。") == ["公交线路 换乘18路"]
    assert why("周一照常开放。", "营业时间 周一照常开放") == []
    assert why("星期二开馆。") == ["周二开放"]
    # a restaurant's 人均 backs a 人均 sentence only (11-18 DS replay: "雷峰塔 45 元")
    assert why("雷峰塔 45 元，外面看就够。", "人均约 45 元") == ["价格 45元"]
    assert why("晚饭人均约 45 元。", "人均约 45 元") == []


def test_backend_meal_sentence_stays_out_when_the_stop_names_the_restaurant():
    request, route, payload = _route()
    hint = {"evidence_tier": "structural", "food_name": "爱骅裤带面（翠华路店）", "meal_slot": "lunch",
            "food_place_id": 99, "walk_minutes": 4, "_zero_index": 0}
    text = "### 湖边公园\n散步。\n### 老街面馆\n{}\n### 古塔景区\n登塔。\n"

    def meal(body):
        result = article_to_keyed(text.format(body), plan_index=1, trip_request=request, route_plan=route,
                                  structured_evidence_payload=payload, hints={(1, 2): hint})
        return next(item for item in result.data["poi_fragments"] if item["place_id"] == 2).get("meal_fragment")

    assert meal("步行约4分钟就是爱骅裤带面，吃一碗筋道的面。") is None
    assert meal("吃完再走。")["text"] == "午餐可以去附近爱骅裤带面（翠华路店），步行约4分钟"


def test_a_restaurant_named_like_a_review_is_no_review():
    """11-18 H3 P05: "雀食府好吃街" in the backend's own meal sentence read as a taste claim."""
    from src.agents.food_resolver import FoodAttachmentAuthorization
    from src.agents.food_review import find_food_rule_violations

    auth = FoodAttachmentAuthorization(
        plan_index=0, day=1, anchor_place_id=10, meal_slot="lunch", food_place_id=20, evidence_tier="structural",
        food_name="雀食府好吃街", amap_rating=None, amap_avg_price=None, walk_minutes=5, meal_type="餐馆",
        none_tier_text=None)
    span = "<!-- food:plan0_day1_anchor10_slotlunch_food20 -->{}<!-- /food -->"
    check = lambda body: [v.reason for v in find_food_rule_violations(span.format(body), {auth.key: auth}, plan_index=0)]
    assert check("午餐可以去附近雀食府好吃街，步行约5分钟") == []
    assert check("午餐可以去附近雀食府好吃街，菜很好吃") == ["food_tier_exceeded"]


def test_summary_drops_a_bracket_left_holding_only_its_clock():
    from src.api.trip_results import _clean_summary_fragment

    assert _clean_summary_fragment("把最温柔的傍晚（18:44 左右日落）留给西湖") == "把最温柔的傍晚 留给西湖"


def test_route_checks_read_the_backend_skeleton():
    _, route, _ = _route()
    text = ("Day 1：湖边公园 -> 老街面馆 -> 古塔景区\n今天先去古塔景区附近转转。\n"
            "湖边公园：散步，远处能看到古塔景区。\n老街面馆：吃面。\n古塔景区：登塔。")
    skeleton = article_skeleton(text, route)
    assert len(skeleton) == len(text)
    assert "古塔景区附近" not in skeleton and "远处" not in skeleton
    assert skeleton.splitlines()[0] == text.splitlines()[0]
    assert [line.split("：")[0] for line in skeleton.splitlines()[2:]] == NAMES


def test_bold_trip_tips_after_the_last_stop_leave_its_card():
    text = ("### 湖边公园\n散步。\n### 老街面馆\n吃面。\n### 古塔景区\n登塔看全城。\n"
            "**行前小贴士**\n- 穿双好走的鞋。\n祝你玩得开心！\n")
    result = _convert(text)
    fragments = {item["place_id"]: item["text"] for item in result.data["poi_fragments"]}
    assert fragments[3] == "登塔看全城。祝你玩得开心！"
    assert "【行前小贴士】" in result.data["day_openings"][0]["text"]
    assert "穿双好走的鞋" in result.data["day_openings"][0]["text"]


def test_inline_trip_tips_after_the_last_stop_leave_its_card():
    text = ("### 湖边公园\n散步。\n### 老街面馆\n吃面。贴心提示：可以线上取号。\n### 古塔景区\n"
            "登塔看全城，给这趟画个句号。几个小提醒：穿双好走的鞋，带件外套。\n随身装备：充电宝。\n")
    result = _convert(text)
    fragments = {item["place_id"]: item["text"] for item in result.data["poi_fragments"]}
    assert fragments[2] == "吃面。贴心提示：可以线上取号。"  # a stop's own tip stays
    assert fragments[3] == "登塔看全城，给这趟画个句号。"
    opening = result.data["day_openings"][0]["text"]
    assert "几个小提醒：穿双好走的鞋" in opening and "随身装备：充电宝" in opening


def test_predispatch_transit_cleanup_keeps_article_stop_lines():
    """11-18 staging: cutting from "步行"/"公交" also cut the "站名：" label and emptied the card."""
    from src.agents.publish_gate import _sanitize_transit_line

    line = "老街面馆：从湖边公园坐公交约10分钟过来，片儿川很好吃。"
    assert _sanitize_transit_line(line, []) == line
    assert _sanitize_transit_line("通勤参考：湖边公园→老街面馆，公共交通约 10 分钟。", []) == ""


def test_every_route_check_ignores_places_the_article_only_names(monkeypatch):
    """11-18 staging: content validation counted "什刹海" named in day 1's prose as a day-1 stop."""
    from src.agents.route_planning import route_plan_violations
    from src.agents.schema import PlanOutput

    request, route, _ = _route()
    text = ("Day 1：湖边公园 -> 老街面馆 -> 古塔景区\n今天先去古塔景区附近转转，晚上可以去别处景点。\n"
            "湖边公园：散步，远处能看到古塔景区。\n老街面馆：吃面。\n古塔景区：登塔。")
    plan = PlanOutput(plan_name="x", plan_text=text, day_place_names=[NAMES])
    assert route_plan_violations([plan], [route]) == []
    monkeypatch.setattr(get_settings(), "writer_prompt_version", "timeline")
    assert route_plan_violations([plan], [route])  # the prose order still counts outside article mode


@pytest.mark.asyncio
async def test_writer_validation_takes_an_article(monkeypatch):
    request, route, _ = _route()
    places = [p for g in route.day_groups for p in g.places]
    result = await final_writer._generate_one_locked_plan(
        zero_index=0, trip_request=request, retrieval=RetrievalResult(city="杭州", candidates=places),
        route_plan=route, sibling_route_plans=[], _raw_override=ARTICLE, speculative_initial_generation=False,
    )
    assert not result.failure_reason, result.failure_reason
    assert "湖心的石桥" in result.plan.plan_text
    # "古塔景区是…" keeps its subject; the leading name goes only before a separator
    raw = ARTICLE.replace("登塔俯瞰全城。", "古塔景区是全城最高点。")
    result = await final_writer._generate_one_locked_plan(
        zero_index=0, trip_request=request, retrieval=RetrievalResult(city="杭州", candidates=places),
        route_plan=route, sibling_route_plans=[], _raw_override=raw, speculative_initial_generation=False,
    )
    assert "古塔景区是全城最高点" in result.plan.plan_text
    # 11-18 DS replay: "第一天的第一口" lost its subject to the day-label strip
    for lead, kept in (("第一天的第一口，值得留给这碗面。", "今天的第一口"), ("第二天，先去吃面。", "：先去吃面")):
        raw = ARTICLE.replace("来一碗片儿川，人均二三十就能吃饱。", lead)
        result = await final_writer._generate_one_locked_plan(
            zero_index=0, trip_request=request, retrieval=RetrievalResult(city="杭州", candidates=places),
            route_plan=route, sibling_route_plans=[], _raw_override=raw, speculative_initial_generation=False,
        )
        assert kept in result.plan.plan_text
    assert any(a.get("action") == "article_split" for a in result.sanitizer_actions if isinstance(a, dict))


def test_other_prompt_versions_are_unchanged(monkeypatch):
    monkeypatch.setattr(get_settings(), "writer_prompt_version", "timeline")
    assert final_writer.single_plan_system_prompt() == final_writer.TIMELINE_SYSTEM_PROMPT
    assert unsupported_fact_pattern_matches("据说门票 45 元")
    monkeypatch.setattr(get_settings(), "writer_prompt_version", "v6")
    assert final_writer._writer_temperature("original") == pytest.approx(get_settings().writer_temperature)
    assert json.dumps({"ok": True})
