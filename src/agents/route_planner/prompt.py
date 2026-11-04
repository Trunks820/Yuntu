"""Prompt text for the v0.14 route planner.

The wording follows the P0.5 experiment (``rules_for``, ``system_for``,
``request_text``, ``candidate_block``). Place ids are the database numbers.
The model is asked for order and stay minutes only.
"""

from __future__ import annotations

from datetime import date

from src.agents.preferences import (
    candidate_is_family_place,
    candidate_is_night_place,
    candidate_is_tea_place,
)
from src.agents.schema import CandidatePlace, TripRequest
from src.agents.route_planner.rules import (
    PACE_SIGHT_HOURS,
    WALK_CAP_MINUTES,
    dinner_earliest_minute,
    dinner_latest_minute,
)
from src.agents.route_planner.schedule import _visitor_meal


def _fmt(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _about_hours(minutes: int) -> str:
    """Whole hours, half hours, or hours plus leftover minutes."""
    minutes = max(0, int(minutes))
    hours, rest = divmod(minutes, 60)
    if rest == 0:
        return f"{hours} 小时"
    if rest == 30:
        if hours == 0:
            return "0.5 小时"
        return f"{hours}.5 小时"
    if hours == 0:
        return f"{rest} 分钟"
    return f"{hours} 小时 {rest} 分钟"


def _dinner_window_text(latest: int) -> str:
    if latest == 20 * 60:
        return "17:00–20:00 都行"
    return f"17:00–{_fmt(latest)} 都行"


def _day_segments(rules) -> list[str]:
    """Three blocks of a day, with hours computed from ``rules``.

    Departure and return use the clocks already stored on ``rules`` (a filled
    daily start or end replaces the pace default). The 07:30 excursion
    departure is not part of this text. Night-view time is
    ``end - 30 - max(dark, night dinner + 60)``, and the sentence never says
    less than 30 minutes.
    """
    morning = max(0, 12 * 60 - rules.start_minute)
    lines = [
        f"上午：从 {_fmt(rules.start_minute)} 出门到约 12:00 吃午饭"
        "（午饭 11:30–13:30 之间开始），"
        f"可逛约 {_about_hours(morning)}（含路上）；上午排一个主要地点。"
    ]
    summer = ""
    if rules.pace_mode == "relaxed" and rules.start_month in {7, 8}:
        summer = (
            "其中 2–3 小时回住处歇（中午最热，午饭后排 kind=rest，16:00 以后再出门）。"
        )
    if not rules.dinner:
        afternoon = max(0, rules.end_minute - 13 * 60)
        lines.append(
            f"下午：午饭约 1 小时；从约 13:00 到 {_fmt(rules.end_minute)} 回住处，"
            f"可逛约 {_about_hours(afternoon)}。{summer}"
        )
        lines.append("晚上：这个时间窗不安排晚饭和夜景。")
        return lines
    latest = dinner_latest_minute(rules.end_minute)
    plain_dinner = min(18 * 60, latest)
    night_dinner = dinner_earliest_minute(rules, has_night=True)
    lines.append(
        "下午：午饭约 1 小时；从约 13:00 到晚饭。"
        f"没排夜景的天晚饭约 {_fmt(plain_dinner)}（{_dinner_window_text(latest)}），"
        f"可逛约 {_about_hours(max(0, plain_dinner - 13 * 60))}；"
        f"有夜景的天晚饭约 {_fmt(night_dinner)}，"
        f"可逛约 {_about_hours(max(0, night_dinner - 13 * 60))}。"
        f"{summer}"
    )
    raw_night = rules.end_minute - 30 - max(rules.dark_minute, night_dinner + 60)
    evening = (
        "晚上：晚饭约 1 小时。没夜景的天吃完可以直接回住处。"
        f"有夜景的天：天黑约 {_fmt(rules.dark_minute)}，"
        f"夜景最多待约 {max(raw_night, 30)} 分钟。"
    )
    if raw_night < 45:
        evening += "这几天晚上很紧，夜景只排离晚饭近的一处。"
    lines.append(evening)
    return lines


def _md(value: str) -> str:
    parsed = date.fromisoformat(value[:10])
    return f"{parsed.month}月{parsed.day}日"


def request_text(request: TripRequest, rules) -> str:
    notes = request.notes or ""
    company = ""
    marker = "【同行情况】"
    if marker in notes:
        tail = notes.split(marker, 1)[1]
        company = tail.split("。", 1)[0]
    extra = notes
    if marker in notes:
        head, tail = notes.split(marker, 1)
        rest = tail.split("。", 1)
        extra = (head + (rest[1] if len(rest) > 1 else "")).strip()
    people = company or f"{request.people_count or 1}个人"
    head = f"帮我做一份{rules.city}{rules.days}天的旅行攻略"
    if request.start_date and request.end_date:
        head += f"，{_md(request.start_date)}到{_md(request.end_date)}"
    lines = [head + "，" + people + "。"]
    if request.preferences:
        lines.append("偏好：" + "、".join(request.preferences) + "。")
    if rules.must_names:
        lines.append("一定要去：" + "、".join(rules.must_names) + "。")
    if rules.avoid:
        lines.append("想避开：" + "、".join(rules.avoid) + "。")
    if request.daily_start or request.daily_end:
        lines.append(
            f"每天只在 {_fmt(rules.start_minute)}–{_fmt(rules.end_minute)} 之间玩。"
        )
    if rules.commute_mode == "driving":
        lines.append("自驾。")
    elif rules.commute_mode == "cycling":
        lines.append("骑行。")
    else:
        lines.append("坐公共交通。")
    if extra:
        lines.append(extra)
    return "\n".join(lines)


def _rule_pairs(rules) -> list[tuple[str, str]]:
    """Hard-rule paragraph plus a short checklist line, same order and numbering."""
    pairs: list[tuple[str, str]] = []
    latest = dinner_latest_minute(rules.end_minute)
    night_dinner = dinner_earliest_minute(rules, has_night=True)
    if rules.must_names:
        pairs.append((
            "必去点（候选里标【必去】）全部安排；彼此相近的放在同一天连着去。漏了会被打回。",
            "必去点都排上了",
        ))
    if rules.family:
        pairs.append((
            "亲子出行：要有孩子能玩的地方（候选里标【亲子】的），整趟至少一处，有多处时分散到不同天。",
            "有孩子能玩的地方",
        ))
    if rules.night:
        pairs.append((
            "用户想看夜景：整趟至少安排一处标【夜景】的地点，kind 写 night。",
            "有标【夜景】的点",
        ))
    if rules.food:
        pairs.append((
            "用户喜欢美食：整趟至少一顿午饭或晚饭选网友提过的店（标【网友提过】的店），否则会被打回。",
            "有一顿网友提过的正餐",
        ))
    if rules.tea:
        pairs.append((
            "用户想去茶馆喝茶：从标【茶馆】的地点挑一处，kind 写 snack，不要占午饭或晚饭。",
            "安排了茶馆喝茶",
        ))
    pairs.append((
        f"一共 {rules.days} 天，day 从 1 到 {rules.days}，行程天数不对会被打回。",
        "行程天数对了",
    ))
    pairs.append((
        f"节奏{rules.pace_label}：每天最多 {rules.sight_limit} 个 sight/night"
        f"（吃饭、小吃、歇脚不算）。超过{rules.pace_label}节奏的 {rules.sight_limit} 个会被打回。",
        f"每天最多{rules.sight_limit}个游览点",
    ))
    pairs.append((
        "每天至少有一个游览点；没有游览点会被打回。",
        "每天都有游览点",
    ))
    pairs.append((
        "同一个点不能排两次；重复安排会被打回。",
        "没有重复的点",
    ))
    pairs.append((
        "餐饮店不当景点：类型是饭店、小吃的不要写成 sight 或 night。",
        "饭店没写成景点",
    ))
    pairs.append((
        "名字带停业字样（暂停营业、停业、歇业、已关闭）的不要选。",
        "没选停业的点",
    ))
    meals = "每天安排午饭" + ("和晚饭" if rules.dinner else "") + "，选正经吃饭的店"
    meals += "（挑店时尽量离前后两站近；优先选标【网友提过】的店（候选清单里的饭店和【高德餐厅】都算），其次选高德评分高的）"
    meals += "。咖啡馆、面包烘焙、甜品、冷饮、奶茶只能当小吃（snack），每天最多 1 个小吃。"
    meals += "午饭在 11:30–13:30 之间开始，不在饭点会被打回。"
    if rules.dinner:
        meals += (
            f"没排夜景的天，晚饭在 17:00–{_fmt(latest)} 之间开始。"
            f"有夜景的那天晚饭约 {_fmt(night_dinner)} 开始，吃完天黑再去看夜景；"
            f"晚饭最晚仍是 {_fmt(latest)}。"
        )
        meals += "缺少午饭或缺少晚饭会被打回。"
        meal_check = "每天有正餐、饭点对、小吃最多1个"
    else:
        meals += "缺少午饭会被打回。这个时间窗里不安排晚饭和夜景。"
        meal_check = "每天有午饭、饭点对、小吃最多1个"
    pairs.append((meals, meal_check))
    if rules.dinner:
        pairs.append((
            f"夜景只从标【夜景】的地点里选（网友笔记写到了夜景、亮灯或夜游），"
            f"只有标【夜景】的点能当 night。没有这个标记却写成 night，会判夜景没写到夜景。"
            f"夜景必须放在当天晚饭之后，夜景排在晚饭之前会被打回；"
            f"夜景不是最后一站会被打回；晚饭后只排夜景（可以加 kind=rest）。"
            f"{rules.city}这几天天黑约 {_fmt(rules.dark_minute)}，天还没黑就去看夜景会被打回。",
            "夜景在晚饭后天黑后、当天最后",
        ))
    pairs.append((
        f"每天 {_fmt(rules.start_minute)} 从住处出发，{_fmt(rules.end_minute)} 前回到住处。"
        "回到住处超过这个时间会被打回。空等不能超过 1 小时。",
        f"{_fmt(rules.end_minute)} 前回到住处、没有空等超过 1 小时",
    ))
    pairs.append((
        f"单段步行不能超过 {WALK_CAP_MINUTES} 分钟。",
        f"单段步行不超过 {WALK_CAP_MINUTES} 分钟",
    ))
    pairs.append((
        f"去城外的远郊日，路上来回总时间不超过约 {rules.excursion_budget} 分钟。",
        f"远郊日路上不超过约 {rules.excursion_budget} 分钟",
    ))
    pairs.append((
        "地点只能从候选里选，编号必须是候选给的。",
        "地点和编号都来自候选",
    ))
    return pairs


def _hard_rules(rules) -> list[str]:
    """Hard checks. Same set as collect_issues; violating them triggers a repair."""
    return [hard for hard, _check in _rule_pairs(rules)]


def _soft_rules(rules, *, lodging_first_name: str | None = None) -> list[str]:
    """Guidance only. City-day commute over the cap is a note after one repair.

    ``lodging_first_name`` adds one soft line. It stays out of the hard rules.
    """
    rows: list[str] = []
    hours = PACE_SIGHT_HOURS[rules.pace_mode]
    rows.append("一天尽量 1–2 个相邻片区，最多 3 个。")
    rows.append(
        "每类地方逛多久：大型博物馆、故宫这一级宫殿、皇家园林、大景区 3 小时；"
        "一般景点 2 小时；古街、步行街 1.5 小时；寺庙、公园 1–1.5 小时；"
        "夜景 1–1.5 小时；观景台、打卡点半小时。候选行带了「常逛约 N 分钟」的以它为准。"
    )
    rows.append(
        f"每天时间预算：景点、夜景停留合计尽量不超过 {hours} 小时"
        "（轻松 6、适中 7、紧凑 8），不含吃饭、歇脚、路上。"
    )
    rows.extend(_day_segments(rules))
    rows.append("下午逛完离晚饭还早可以排 kind=rest 回住处歇。")
    if rules.family and rules.pace_mode == "relaxed":
        rows.append("亲子又要轻松：午饭后安排 kind=rest 回住处歇脚。")
    rows.append(
        f"城市日路上总时间尽量短，尽量不超过 {rules.urban_commute_cap} 分钟；"
        "偏长会提示。超过这个上限时第一稿会要求改一次，改完仍超只记提示。"
    )
    if rules.food:
        rows.append("午饭晚饭尽量选当地特色店。")
    if rules.citywalk:
        rows.append("citywalk：至少有一段连着几站都能走路串起来（相邻两站步行 15 分钟以内）。")
    if rules.other_preferences:
        rows.append(
            "其他偏好（" + "、".join(rules.other_preferences) + "）：整趟每一项至少一处对应的地点，停留时间给够。"
        )
    if rules.avoid:
        rows.append("用户想避开：" + "、".join(rules.avoid) + "。网友笔记里主要是排队打卡的网红点不要选。")
    if rules.commute_mode == "driving":
        rows.append("交通：自驾，代码按开车算路程。")
    elif rules.commute_mode == "cycling":
        rows.append("交通：骑行；近处走路，远处按骑行算。")
    else:
        rows.append("交通：公共交通；代码会在近处安排步行，远处安排地铁、公交或打车。")
    cleaned = (lodging_first_name or "").strip()
    if cleaned:
        rows.append(
            "住处在 "
            + cleaned
            + "（系统按候选位置选的）。每天第一站和最后一站尽量选离住处近的点；"
            "必去点和用户偏好优先，不要为了离住处近而放弃它们。"
        )
    return rows


def rules_for(rules, *, lodging_first_name: str | None = None) -> str:
    pairs = _rule_pairs(rules)
    soft = _soft_rules(rules, lodging_first_name=lodging_first_name)
    rows = [
        "排程规则分两类：违反会被打回；尽量做到。",
        "",
        "违反会被打回：",
    ]
    rows.extend(f"{index}. {hard}" for index, (hard, _check) in enumerate(pairs, 1))
    rows.append("")
    rows.append("尽量做到：")
    rows.extend(f"{index}. {text}" for index, text in enumerate(soft, 1))
    rows.append("")
    rows.append("交稿前自查（与「违反会被打回」一一对应）：")
    rows.extend(f"{index}. {check}" for index, (_hard, check) in enumerate(pairs, 1))
    return "\n".join(rows)


def system_for(
    rules,
    *,
    lodging_name: str | None = None,
    hotel_lodging: bool = False,
    lodging_first_name: str | None = None,
) -> str:
    """System prompt. ``hotel_lodging`` false keeps the pre-change wording.

    The model then still names a ``lodging`` anchor. When the flag is on, that
    field is gone and a fixed user lodging no longer says to write null.
    ``lodging_first_name`` is a lodging this code already picked. It is a soft
    hint only, and only when hotel lodging is on. An empty name changes nothing.
    """
    fixed = ""
    cleaned = (lodging_name or "").strip()
    if hotel_lodging:
        if cleaned:
            fixed = f"住处已定：{cleaned}（用户指定）。\n"
        output_shape = (
            '{"days": [{"day": 1, "stops": [{"kind": "sight|lunch|dinner|snack|night|rest", '
            '"place_id": 2501, "stay_minutes": 90, "optional": true}]}]}'
        )
    else:
        if cleaned:
            fixed = f"住处已定：{cleaned}（用户指定），lodging 写 null，不用挑。\n"
        output_shape = (
            '{"lodging": {"area": "片区名", "anchor": <一个 business_area 候选的 place_id，没有合适的就写 null>},\n'
            ' "days": [{"day": 1, "stops": [{"kind": "sight|lunch|dinner|snack|night|rest", '
            '"place_id": 2501, "stay_minutes": 90, "optional": true}]}]}'
        )
    return f"""你是旅行规划师。根据用户需求，从候选里挑地点，排好每天的先后顺序。
- 游览点只能从候选里选，用候选给的编号（place_id，数据库里的数字）指代。
- 午饭、晚饭从当时所在片区或相邻片区挑：可以选【高德餐厅】（评分和人均来自高德），也可以选候选里网友提过的正经饭店。优先选标【网友提过】的店，其次选高德评分高的。
- 你不用写时间，也不用写路上多久：代码会按实测算每段路和到达时间，并按规则自动删掉放不下的点。你只需要给出顺序和每个点大概停留多少分钟。
- 一天里有时间就去、没时间可以不去的点，在这一站写 "optional": true。必去点、亲子点、用户要的夜景点、午饭、晚饭、回住处歇不能标。不要为了凑数多放点。不写 optional 就是 false。
{rules_for(rules, lodging_first_name=lodging_first_name if hotel_lodging else None)}
{fixed}只输出一个 JSON 对象，不要其他文字：
{output_shape}
一共 {rules.days} 天，day 从 1 到 {rules.days}。kind=rest 表示回住处歇脚，place_id 写 null。optional 只有有时间才去的点才写 true。"""


_VISITOR_QUOTE_LIMIT = 40


def _reason_text(place: CandidatePlace) -> str:
    lines: list[str] = []
    for item in place.top_reasons or []:
        if isinstance(item, str) and item.strip():
            lines.append(item.strip())
        elif isinstance(item, dict):
            for key in ("reason", "text", "cleaned_text", "evidence"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    lines.append(value.strip())
                    break
        if len(lines) == 2:
            break
    return "；".join(piece[:50] for piece in lines[:2])


def _visitor_quote(place: CandidatePlace) -> str:
    """One cleaned visitor line for a nearby restaurant, at most 40 characters."""

    text = _reason_text(place)
    if not text:
        return ""
    first = text.split("；", 1)[0].replace("|", "／").replace("\n", " ")
    return first[:_VISITOR_QUOTE_LIMIT]


def _marks(place: CandidatePlace, rules) -> str:
    mark = "【必去】" if place.must_include or place.place_id in rules.must_ids else ""
    if rules.family and candidate_is_family_place(place):
        mark += "【亲子】"
    if candidate_is_night_place(place):
        mark += "【夜景】"
    if rules.tea and candidate_is_tea_place(place):
        mark += "【茶馆】"
    return mark


def _money(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return "无"
    whole, dot, frac = text.partition(".")
    if dot and set(frac) <= {"0"}:
        return whole or "无"
    return text


def _visit_hint(place: CandidatePlace) -> str:
    minutes = getattr(place, "typical_visit_minutes", None)
    try:
        value = int(minutes)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    return f"|常逛约{value}分钟"


def _lodging_distance_suffix(place: CandidatePlace, lodging) -> str:
    """Straight-line kilometers from a fixed lodging. Empty when lodging is unset."""
    if lodging is None:
        return ""
    # Imported here: places <-> rules already cycle, and a top-level import
    # from this module starts places before rules has finished loading.
    from src.agents.route_planner.places import hav_km

    kilometers = hav_km(lodging, place)
    if kilometers is None:
        return ""
    return f"|离住处约 {kilometers:.1f} 公里"


def candidate_block(
    clusters,
    restaurants_by_index: dict[int, list[CandidatePlace]],
    rules,
    *,
    lodging=None,
) -> str:
    lines = [
        "候选按片区分组（同一片区内步行一般 15 分钟以内；片区由代码按坐标算出）。",
        "候选地点格式：place_id|名称|类型|标签|网友笔记摘录|常逛约N分钟（有常见游览时长才写）；"
        "【高德餐厅】格式：编号|店名|高德评分|人均。"
        "【网友提过】可能出现在候选清单的饭店行，名称前加这个标记；"
        "也可能出现在【高德餐厅】行，店名前加这个标记，末尾再附一句网友原话（40字内）。"
        "标【夜景】表示网友笔记写到了夜景、亮灯或夜游；只有标【夜景】的点能当 night。",
    ]
    for cluster in clusters:
        near = "、".join(f"片区{index}（{distance:.1f}km）" for index, distance in cluster.neighbors)
        lines.append(f"\n【片区{cluster.index}：{cluster.seed.name}一带】邻近：" + near)
        members = sorted(cluster.members, key=lambda place: place.place_type == "restaurant")
        for place in members:
            tags = "、".join(place.category_tags or [])[:20]
            # Candidate-list rows are not the blank 【高德餐厅】 ids, so the amap
            # set is empty. collect_issues uses this same `_visitor_meal`.
            name = f"【网友提过】{place.name}" if _visitor_meal(place, set()) else place.name
            lines.append(
                f"{place.place_id}|{_marks(place, rules)}{name}|{place.place_type}|{tags}|{_reason_text(place)}{_visit_hint(place)}{_lodging_distance_suffix(place, lodging)}"
            )
        for place in restaurants_by_index.get(cluster.index, []):
            name = f"【网友提过】{place.name}" if place.top_reasons else place.name
            line = (
                f"【高德餐厅】{place.place_id}|{name}"
                f"|评分{place.amap_rating or '无'}|人均{_money(place.amap_avg_price)}"
            )
            quote = _visitor_quote(place) if place.top_reasons else ""
            if quote:
                line += f"|{quote}"
            line += _lodging_distance_suffix(place, lodging)
            lines.append(line)
    return "\n".join(lines)


