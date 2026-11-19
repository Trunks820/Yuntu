"""v0.15.1 article mode: the Writer writes one guide, one heading per stop.

The prompt states the occasion and the facts; how to write is the model's
(11-17 replay: rules in the prompt flattened the text). The rules that must
hold run here, after writing, stop by stop:

- a price, ticket price, booking lead time, traffic restriction, open
  weekday or transit line number the material does not carry is cut, one
  sentence at a time; a clock time is kept only when the material, the
  stop's own clock or the day's sunset has it;
- the rest is the model's: the card shows the backend's clock and legs, so
  minutes and sunset remarks that disagree with it are left (11-18: fixing
  each new wording of them cut right sentences);
- each stop's section is found by its "### 站名" heading, in route order;
  a missing or out-of-order stop fails the draft so the other one is used.

The result is the keyed JSON the timeline assembly already reads.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from src.agents.route_planner.places import _center
from src.agents.route_planner.rules import sunset_minutes
from src.agents.route_timeline import day_timeline
from src.agents.schema import parse_hhmm
from src.agents.weather_advisory import is_weather_line

ARTICLE_SYSTEM_PROMPT = "你是一个去过很多地方、很会写攻略的旅行达人。"

# 11-17 round 3 wording; only the occasion and the heading were added to round 1.
ARTICLE_ASK = (
    "下面是一位用户的需求，和已经排好的行程：每站的顺序、到达时间、停留时长、两站之间怎么走、走多久，"
    "都是用地图实测排出来的，不要改。请你据此给这位用户写一份攻略。"
    "怎么写、写多长、用什么格式，你自己定，写到你觉得最好为止。"
    "每站后面附了我们从游记和高德整理的材料，供你参考。"
    "这份攻略会直接显示在用户的行程页上，用户看得到但没法回复你。"
    "每一站用一个小标题“### 站名”开头（站名照抄行程里的写法，按行程顺序，每站一个），小标题下面写什么、怎么写随你。"
)

_WEEKDAYS = "一二三四五六日"
_KIND = {"sight": "景点", "lunch": "午饭", "dinner": "晚饭", "night": "夜景", "snack": "小吃", "cafe": "咖啡/下午茶"}


@dataclass
class ArticleStop:
    day: int
    place_id: int
    name: str
    material: str
    arrive: int | None = None
    leave: int | None = None
    sunset: int | None = None


def _hm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _minutes(value: Any) -> int | None:
    try:
        parsed = parse_hhmm(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, int) else None


def _leg(move: Any) -> str:
    if move is None or move.minutes is None:
        return ""
    return "就在旁边" if move.minutes == 0 else f"{move.mode}约 {move.minutes} 分钟"


def _day_date(trip_request: Any, day: int) -> date | None:
    try:
        return date.fromisoformat(str(trip_request.start_date or "")[:10]) + timedelta(days=int(day) - 1)
    except ValueError:
        return None


def _names_food(text: str, hint: dict[str, Any]) -> bool:
    name = re.sub(r"[（(][^）)]*[）)]\s*$", "", unicodedata.normalize("NFKC", str(hint.get("food_name") or ""))).strip()
    return len(name) >= 2 and _flat(name) in _flat(unicodedata.normalize("NFKC", text))


def meal_hint_text(hint: dict[str, Any]) -> str:
    """The food line the backend owns (same wording as food_review's structural text)."""
    meal = "晚餐可以去" if hint.get("meal_slot") == "dinner" else "午餐可以去"
    parts = [f"{meal}附近{hint.get('food_name') or hint.get('meal_type') or '餐馆'}"]
    if hint.get("walk_minutes") is not None:
        parts.append(f"步行约{hint['walk_minutes']}分钟")
    if hint.get("amap_rating"):
        parts.append(f"评分{hint['amap_rating']}")
    if hint.get("amap_avg_price"):
        parts.append(f"人均约{hint['amap_avg_price']}元")
    return "，".join(parts)


def food_hints(composition_blueprint: Any, zero_index: int) -> dict[tuple[int, int], dict[str, Any]]:
    """(day, anchor place_id) -> the stop's food writing hint."""
    hints: dict[tuple[int, int], dict[str, Any]] = {}
    for day in getattr(composition_blueprint, "days", None) or []:
        for stop in day.stops:
            hint = stop.writing_hint
            if isinstance(hint, dict) and hint.get("evidence_tier") and hint.get("food_name"):
                hints[(int(day.day), int(stop.place_id))] = {**hint, "_zero_index": zero_index}
    return hints


def _notes(place: Any, evidence: Any, hint: dict[str, Any] | None) -> str:
    bits = []
    if evidence is not None:
        said = [*evidence.direct_facts, *evidence.weak_experience]
        if said:
            bits.append("游记里的说法：" + "；".join(said))
        if evidence.risk_only_warnings:
            bits.append("也有人吐槽：" + "；".join(evidence.risk_only_warnings))
    if place.amap_rating:
        bits.append(f"高德评分 {place.amap_rating}")
    try:
        if place.amap_avg_price:
            bits.append(f"人均约 {float(place.amap_avg_price):.0f} 元")
    except (TypeError, ValueError):
        pass
    if place.amap_open_time:
        bits.append(f"营业时间 {place.amap_open_time}")
    if hint:
        bits.append("附近吃饭：" + meal_hint_text(hint))
    return "｜".join(bits)


def article_stops(*, trip_request: Any, route_plan: Any, structured_evidence_payload: Any,
                  hints: dict[tuple[int, int], dict[str, Any]]) -> list[ArticleStop]:
    evidence = {item.place_id: item for item in structured_evidence_payload.places}
    stops: list[ArticleStop] = []
    for group in route_plan.day_groups:
        clock = day_timeline(group, trip_request)
        times: dict[int, list[tuple[int | None, int]]] = {}
        for item in clock.items:
            if item.kind != "rest" and item.place_id is not None:
                times.setdefault(item.place_id, []).append((_minutes(item.arrive), int(item.stay_minutes or 0)))
        center = _center(list(group.places))
        day = _day_date(trip_request, group.day)
        sunset = sunset_minutes(center[0], center[1], day) if center and day else None
        for place in group.places:
            slot = times.get(place.place_id)
            arrive, stay = slot.pop(0) if slot else (None, 0)
            stops.append(ArticleStop(
                day=int(group.day), place_id=int(place.place_id), name=place.name,
                material=_notes(place, evidence.get(place.place_id), hints.get((int(group.day), int(place.place_id)))),
                arrive=arrive, leave=arrive + stay if arrive is not None else None, sunset=sunset,
            ))
    return stops


def build_article_prompt(*, trip_request: Any, route_plan: Any, structured_evidence_payload: Any,
                         hints: dict[tuple[int, int], dict[str, Any]], accommodation: Any = None) -> str:
    """The round-3 input: the need, the clock with each day's sunset, each stop's material."""
    lines = [ARTICLE_ASK, "", "【用户需求】",
             f"去{trip_request.to_city}玩 {trip_request.days} 天，{trip_request.start_date or '日期未定'} 出发，"
             f"从{trip_request.from_city or '外地'}来，{trip_request.people_count} 个人。"]
    if trip_request.preferences:
        lines.append("偏好：" + "、".join(trip_request.preferences) + "。")
    if (trip_request.notes or "").strip():
        lines.append(f"原话：{trip_request.notes.strip()}")
    if trip_request.avoid:
        lines.append("不想要：" + "、".join(trip_request.avoid))
    if accommodation is not None:
        display = getattr(accommodation, "display", None)
        lines.append(f"住处：{getattr(display, 'area_label', '') or accommodation.name}")
    lines += ["", "【已排好的行程】"]
    names = {place.place_id: place.name for group in route_plan.day_groups for place in group.places}
    stops = article_stops(trip_request=trip_request, route_plan=route_plan,
                          structured_evidence_payload=structured_evidence_payload, hints=hints)
    sunset_by_day = {stop.day: stop.sunset for stop in stops}
    for group in route_plan.day_groups:
        day = _day_date(trip_request, group.day)
        head = f"\n第 {group.day} 天"
        if day is not None:
            head += f" · {day.month}月{day.day}日 周{_WEEKDAYS[day.weekday()]}"
        if sunset_by_day.get(int(group.day)) is not None:
            head += f"（当天日落约 {_hm(sunset_by_day[int(group.day)])}）"
        lines.append(head)
        clock = day_timeline(group, trip_request)
        depart = _leg(clock.depart_move)
        lines.append(f"{clock.start} 从住处出发" + (f"，{depart}" if depart else ""))
        for item in clock.items:
            if item.kind == "rest":
                what = (f"回住处歇约 {item.stay_minutes} 分钟" if item.rest_via_lodge
                        else f"休息约 {item.stay_minutes} 分钟")
            else:
                kind = _KIND.get(item.kind, "")
                what = f"{names.get(item.place_id, '')}" + (f"（{kind}）" if kind else "") + f"，停留约 {item.stay_minutes} 分钟"
            lines.append(f"{item.arrive} {what}")
            move = _leg(item.move_to_next)
            if move and item is not clock.items[-1]:
                lines.append(f"    ↓ {move}")
        lines.append(f"{clock.return_at} 回到住处")
    lines += ["", "【各站材料】"]
    day = None
    for stop in stops:
        if stop.day != day:
            day = stop.day
            lines.append(f"\n第 {day} 天")
        lines.append(f"- {stop.name}：{stop.material or '（没有材料）'}")
    return "\n".join(lines)


# --- checks, per stop ---------------------------------------------------------------------

_MONEY = re.compile(r"(\d+(?:\.\d+)?)\s*(?:元|块)")
_BARE_TICKET = re.compile(r"(\d+(?:\.\d+)?)\s*的?(?:门票|票)")  # material often drops the unit: "买30的票"
# "人均二三十", "二三十块"; a bare "一元" is usually a shop name (张一元)
_CN_MONEY = re.compile(r"人均[一二三四五六七八九十百两几]+|[一二三四五六七八九两]*[十百][一二三四五六七八九两几]*(?:块|元)")
_CLOCK = re.compile(r"(\d{1,2})\s*[:：]\s*(\d{2})|(\d{1,2})\s*点\s*(半)?")
_LEAD = re.compile(r"提前\s*(\d+|[一二三四五六七八九十两]+)\s*(?:[-–~～至到]\s*\d+\s*)?(天|周|个月)")
_TICKET = re.compile(r"票|券")
_FIXED_TIME = re.compile(r"放票|放号|号源|预约|开放|营业|闭馆|停止入馆|入馆|开门|关门|亮灯|熄灯|演出|开演|开场")
_SUN = re.compile(r"日落|日出|夕阳|晚霞|落日")
_RESTRICTION = re.compile(r"限行")
# A line number the card does not show is made up (publish_gate's transit check reads the
# backend skeleton in article mode); minutes and stop names are left to the card.
_TRANSIT_LINE = re.compile(r"[\d一二三四五六七八九十]+\s*号线|(?:坐|乘|搭|换乘?)\s*\d+\s*路|\d+\s*路(?:公交|车|大巴)")
# "三峡博物馆周一正常开放" sends people to a closed door; a "周一闭馆" reminder errs safe and stays.
_WEEKDAY_OPEN = re.compile(
    r"(?:周|星期|礼拜)([一二三四五六日天])[^，,。；;！？不闭休]{0,8}?(?<![不未没])(?:开放|开馆|开门|营业)")


def _flat(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _clock_minutes(text: str):
    for match in _CLOCK.finditer(text):
        if match.group(1):
            yield match.group(0), int(match.group(1)) * 60 + int(match.group(2))
        else:
            yield match.group(0), int(match.group(3)) * 60 + (30 if match.group(4) else 0)


def _near(minutes: int, clocks, within: int) -> bool:
    return any(abs(minutes + shift - clock) <= within for clock in clocks for shift in (0, 720))


@dataclass
class _Material:
    money: set[float]
    ticket_money: set[float]
    avg_money: set[float]  # a restaurant's 人均: it backs a 人均 sentence, not "雷峰塔 45 元"
    clocks: set[int]
    leads: set[str]
    restriction: bool
    open_days: set[str]

    @classmethod
    def of(cls, text: str) -> "_Material":
        clauses = [_flat(part) for part in re.split(r"[；;｜\n]", text or "")]
        ticket = {float(v) for c in clauses if _TICKET.search(c) for v in [*_MONEY.findall(c), *_BARE_TICKET.findall(c)]}
        return cls(
            money=ticket | {float(v) for c in clauses if "人均" not in c for v in _MONEY.findall(c)},
            ticket_money=ticket,
            avg_money={float(v) for c in clauses if "人均" in c for v in _MONEY.findall(c)},
            clocks={minutes for c in clauses for _, minutes in _clock_minutes(c)},
            leads={m.group(0) for m in _LEAD.finditer(_flat(text))},
            restriction=bool(_RESTRICTION.search(text or "")),
            open_days=_open_days(_flat(text)),
        )


def _open_days(text: str) -> set[str]:
    return {match.group(1).replace("天", "日") for match in _WEEKDAY_OPEN.finditer(text)}


def check_sentence(sentence: str, material: _Material, *, stop: ArticleStop | None,
                   route_clocks: list[int], sunsets: list[int]) -> list[str]:
    """Why this sentence goes; empty when it stays."""
    s = _flat(sentence)
    if not s:
        return []
    pool = (material.ticket_money if _TICKET.search(s)
            else material.money | material.avg_money if "人均" in s else material.money)
    why = [f"价格 {v}元" for v in _MONEY.findall(s) if float(v) not in pool]
    why += [f"价格 {m.group(0)}" for m in _CN_MONEY.finditer(s)]
    own = [t for t in ((stop.arrive, stop.leave) if stop else ()) if t is not None]
    for token, minutes in _clock_minutes(s):
        ok = (any((minutes - c) % 720 == 0 for c in material.clocks)
              or _near(minutes, own, 30)
              or bool(_SUN.search(s) and _near(minutes, sunsets, 30)))
        if not ok and not _SUN.search(s) and not _FIXED_TIME.search(s):
            ok = _near(minutes, route_clocks, 30)  # a plain itinerary time ("8点半多就到")
        if not ok and _SUN.search(s) and not own:
            ok = _near(minutes, route_clocks, 5)
        if not ok:
            why.append(f"时刻 {token}")
    why += [m.group(0) for m in _LEAD.finditer(s) if m.group(0) not in material.leads]
    if _RESTRICTION.search(s) and not material.restriction:
        why.append("限行")
    why += [f"公交线路 {m.group(0)}" for m in _TRANSIT_LINE.finditer(s)]
    why += [f"周{day}开放" for day in sorted(_open_days(s) - material.open_days)]
    if is_weather_line(s):
        # Weather is the weather card's; the gate and the result page drop a weather line whole.
        why.append("天气")
    return why


def _sentences(text: str) -> list[str]:
    return [part for part in re.split(r"(?<=[。！？!?])", text) if part]


def checked(text: str, material: _Material, *, stop: ArticleStop | None, route_clocks: list[int],
            sunsets: list[int], dropped: list[dict[str, Any]]) -> str:
    kept = []
    for sentence in _sentences(text):
        why = check_sentence(sentence, material, stop=stop, route_clocks=route_clocks, sunsets=sunsets)
        if why:
            dropped.append({"why": why, "text": sentence.strip(), **({"place_id": stop.place_id} if stop else {})})
        else:
            kept.append(sentence)
    return "".join(kept).strip()


# --- splitting the article ----------------------------------------------------------------

_CN_NUM = {c: i for i, c in enumerate("一二三四五六七八九十", start=1)}
_DAY_HEAD = re.compile(r"^\s*(#{0,4})\s*\**\s*(?:第\s*([0-9一二三四五六七八九十]+)\s*天|Day\s*(\d+))", re.I)
# Without "#", a day line must carry its date or weekday ("第 1 天 · 8月27日 周四"), so prose
# such as "第二天上午人更少。" stays where it is.
_DAY_DATE = re.compile(r"\d{1,2}\s*月\s*\d{1,2}\s*日|周[一二三四五六日天]|星期")
_HEADING = re.compile(r"^\s*(#{2,4})\s*(.+?)\s*#*\s*$")
_RULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_LEADING_DAY_LABEL = re.compile(
    r"^\s*(?:Day\s*\d+|第\s*[一二三四五六七八九十\d]+\s*[天日])\s*[，,：:、。\s·]*", re.I)
_TIME_WORDS = re.compile(
    r"\d{1,2}\s*[:：]\s*\d{2}|约?\s*\d+(?:\.\d+)?\s*(?:分钟|小时|公里|km)|到达|抵达|停留|出发|时间|交通接驳|交通|行程参考|"
    r"从住处|住处|自驾|步行|打车|公交|地铁|骑行|驾车|前往|左右|大约|约|[｜|·•（）()，,。:：\-—→~～\s]")
_END = "。！？!?；;：:…」”）)"


def _plain(line: str) -> str:
    text = line.strip()
    text = re.sub(r"^#{1,6}\s*", "", text)
    text = re.sub(r"^>\s*", "", text)
    text = re.sub(r"^(?:[-*+•]|\d+[.)、])\s+", "", text)
    text = text.replace("**", "").replace("__", "").replace("`", "").replace("*", "")
    text = re.sub(r"<!--.*?-->", "", text)  # food span markers belong to the backend
    return text.strip()


def _day_number(match: re.Match[str], current: int) -> int:
    raw = match.group(2) or match.group(3) or ""
    if raw.isdigit():
        return int(raw)
    if raw in _CN_NUM:
        return _CN_NUM[raw]
    if raw.startswith("十") and raw[1:] in _CN_NUM:
        return 10 + _CN_NUM[raw[1:]]
    return current + 1


def _day_theme(line: str) -> str:
    """The theme left on a day heading once its label, date and sunset are gone."""
    text = _plain(line)
    text = _LEADING_DAY_LABEL.sub("", text)
    text = re.sub(r"\d{1,2}\s*月\s*\d{1,2}\s*日|周[一二三四五六日天]|星期[一二三四五六日天]", "", text)
    text = re.sub(r"[（(][^）)]*日落[^）)]*[）)]", "", text)
    return text.strip(" ·・|｜：:，,—-（）()")


# "17:41 到达｜停留约 60 分钟" leading a stop's text repeats the card's clock.
_TIME_LEAD = re.compile(
    r"^\s*(?:(?:时间|停留|到达|行程)[：:]\s*)?\d{1,2}\s*[:：]\s*\d{2}\s*(?:到达|抵达)[^。！？]*?分钟"
    r"(?:\s*[（(][^）)]{0,40}[）)])?\s*[｜|，,。]?\s*")


# A short line that only names a trip-level tips block: "行前小贴士", "💡 8月出行小贴士："
_TRIP_TIPS_HEAD = re.compile(r"^\W{0,3}[^。！？，,]{0,10}(?:行前|出行|小贴士|贴士|须知|清单)[^。！？，,]{0,6}[：:]?$")


# The label of a trip-level tips line written inline: "随身装备：…", "几点提醒：…",
# "8月出行小贴士：…". A stop's own "贴心提示：…" or "避坑提醒：…" is not one.
_TRIP_TIPS_INLINE = re.compile(
    r"(?:^|(?<=[。！？!?；;]))\s*[^\w。！？!?，,；;：:\s]{0,3}\s*"
    r"(?:几点|几个|几条|最后的?|行前|出行|随身|全程|旅行|[0-9一二三四五六七八九十]+月(?:出行)?)"
    r"[^。！？，,：:]{0,8}(?:提醒|贴士|叮嘱|装备|须知|清单)[：:]")


def _split_trip_tips(lines: list[str]) -> tuple[list[str], list[str]]:
    """The last stop's lines before the first trip-level tips label, and the tips from there on."""
    for index, line in enumerate(lines):
        match = _TRIP_TIPS_INLINE.search(line)
        if match is None:
            continue
        head = line[:match.start()].strip()
        return [*lines[:index], *([head] if head else [])], [line[match.start():].strip(), *lines[index + 1:]]
    return lines, []


def _strip_time_lead(line: str) -> str:
    return _TIME_LEAD.sub("", line, count=1).strip()


def _is_time_only(line: str, names: list[str]) -> bool:
    rest = line
    for name in names:
        rest = rest.replace(name, "")
    return len(_TIME_WORDS.sub("", rest)) < 3


def _join(lines: list[str]) -> str:
    """One paragraph per stop: the assembly keeps each stop on one line."""
    out = ""
    for line in lines:
        if out and out[-1] not in _END:
            out += "。"
        out += line
    return out


_DAY_TITLE = re.compile(r"^Day\s*\d+：")


def article_skeleton(plan_text: str, route_plan: Any) -> str:
    """Only the backend's day titles and stop labels, same length, for the route checks.

    v0.15.1 D3: the article may name any place; naming is not visiting. The
    route's membership and order are the backend's lines, which this keeps.
    """
    # "名：" opens a writer paragraph; "到名后，" opens a completion sentence (result_stops).
    labels = sorted({label for group in route_plan.day_groups for place in group.places if place.name
                     for label in (f"{place.name}：", f"到{place.name}后，")},
                    key=len, reverse=True)
    out = []
    for line in (plan_text or "").split("\n"):
        if _DAY_TITLE.match(line):
            out.append(line)
            continue
        label = next((item for item in labels if line.startswith(item)), "")
        out.append(label + " " * (len(line) - len(label)))
    return "\n".join(out)


@dataclass
class ArticleConversion:
    data: dict[str, Any] | None = None
    failure: str = ""
    actions: list[dict[str, Any]] = field(default_factory=list)


def article_to_keyed(raw: str, *, plan_index: int, trip_request: Any, route_plan: Any,
                     structured_evidence_payload: Any, hints: dict[tuple[int, int], dict[str, Any]]) -> ArticleConversion:
    stops = article_stops(trip_request=trip_request, route_plan=route_plan,
                          structured_evidence_payload=structured_evidence_payload, hints=hints)
    names = sorted({stop.name for stop in stops}, key=len, reverse=True)
    intro: list[str] = []
    day_intro: dict[int, list[str]] = {}
    extra: list[str] = []
    bodies: list[list[str]] = [[] for _ in stops]
    target: tuple[str, int] = ("intro", 0)
    cursor, current_day = 0, 0
    for line in (raw or "").splitlines():
        if not line.strip() or _RULE.match(line):
            continue
        day_head = _DAY_HEAD.match(line)
        if day_head and (day_head.group(1) or _DAY_DATE.search(line)):
            current_day = _day_number(day_head, current_day)
            target = ("day", current_day)
            theme = _day_theme(line)
            if theme:
                day_intro.setdefault(current_day, []).append(theme)
            continue
        heading = _HEADING.match(line)
        if heading:
            title = _plain(heading.group(2))
            if cursor < len(stops) and stops[cursor].name in title:
                target = ("stop", cursor)
                current_day = stops[cursor].day
                cursor += 1
                continue
            if any(stop.name in title for stop in stops[cursor + 1:]):
                return ArticleConversion(failure="stop_out_of_order",
                                         actions=[{"action": "article_split", "matched": cursor,
                                                   "stops": len(stops), "heading": title[:40]}])
            if len(heading.group(1)) <= 3:
                target = ("extra", 0)
                extra.append(f"【{title}】")
                continue
        text = _plain(line)
        if not text:
            continue
        if target == ("stop", len(stops) - 1) and _TRIP_TIPS_HEAD.match(text):
            # "**行前小贴士**" written in bold, not as a heading, after the last stop.
            target = ("extra", 0)
            extra.append(f"【{text.rstrip('：:')}】")
            continue
        if target[0] == "stop":
            text = _strip_time_lead(text)
            if text and not _is_time_only(text, names):
                bodies[target[1]].append(text)
        elif target[0] == "day":
            if not _is_time_only(text, names):
                day_intro.setdefault(target[1], []).append(_LEADING_DAY_LABEL.sub("", text))
        elif target[0] == "extra":
            extra.append(text)
        else:
            intro.append(text)
    split_action = {"action": "article_split", "matched": cursor, "stops": len(stops),
                    "extra_headings": sum(1 for line in extra if line.startswith("【"))}
    if cursor < len(stops):
        return ArticleConversion(failure="stop_missing", actions=[split_action])

    trip_material = _Material.of("\n".join(stop.material for stop in stops))
    route_clocks = [t for stop in stops for t in (stop.arrive, stop.leave) if t is not None]
    for group in route_plan.day_groups:
        clock = day_timeline(group, trip_request)
        route_clocks += [t for t in (_minutes(clock.start), _minutes(clock.return_at),
                                     *(_minutes(item.arrive) for item in clock.items)) if t is not None]
    sunsets = sorted({stop.sunset for stop in stops if stop.sunset is not None})
    dropped: list[dict[str, Any]] = []

    def check_lines(lines: list[str], material: _Material, stop: ArticleStop | None) -> list[str]:
        out = []
        for line in lines:
            kept = checked(line, material, stop=stop, route_clocks=route_clocks, sunsets=sunsets, dropped=dropped)
            if kept:
                out.append(kept)
        return out

    fragments, closing = [], []
    if bodies:
        # "几点提醒：…" written inline after the last stop is the trip's, not the card's.
        bodies[-1], tips = _split_trip_tips(bodies[-1])
        extra.extend(tips)
    # "祝你……" closes the guide; it ends the last card, not the tips or a day intro.
    tail = extra if extra else bodies[-1] if bodies else []
    while tail and tail[-1].startswith("祝"):
        closing.insert(0, tail.pop())
    if bodies:
        bodies[-1] = [*bodies[-1], *closing]
    for index, stop in enumerate(stops):
        lines = bodies[index]
        material = _Material.of(stop.material)
        text = checked(_join(lines), material, stop=stop, route_clocks=route_clocks, sunsets=sunsets, dropped=dropped)
        item: dict[str, Any] = {"plan_index": plan_index, "day": stop.day, "place_id": stop.place_id,
                                "text": text.replace("→", "，").replace("->", "，")}
        hint = hints.get((stop.day, stop.place_id))
        # the material told the model about this restaurant; when it already wrote it up,
        # the backend's sentence would say it twice
        if hint and str(hint.get("evidence_tier")) in {"full", "structural"} and not _names_food(text, hint):
            item["meal_fragment"] = {
                "attachment_key": {"plan_index": hint["_zero_index"], "day": stop.day, "anchor_place_id": stop.place_id,
                                   "meal_slot": str(hint.get("meal_slot") or ""),
                                   "food_place_id": int(hint.get("food_place_id") or -1)},
                "text": meal_hint_text(hint),
            }
        fragments.append(item)

    labels = tuple(f"{name}：" for name in names)

    def unlabeled(lines: list[str]) -> list[str]:
        # A line starting "站名：" is read as that stop's paragraph downstream.
        return [f"· {line}" if line.startswith(labels) else line for line in lines]

    intro_kept = unlabeled(check_lines(intro, trip_material, None))
    extra_kept = unlabeled(check_lines(extra, trip_material, None))
    openings = []
    days = [int(group.day) for group in route_plan.day_groups]
    for day in days:
        lines = unlabeled(check_lines(day_intro.get(day, []), trip_material, None))
        if day == days[0]:
            lines = [*intro_kept, *extra_kept, *lines]
        if lines:
            openings.append({"plan_index": plan_index, "day": day, "text": "\n".join(lines)})
    wraps: list[dict[str, Any]] = []
    summary = ""
    if intro_kept:
        for sentence in _sentences(intro_kept[0]):
            if len(summary) + len(sentence) > 160:
                break
            summary += sentence
    actions = [split_action, *({"action": "article_sentence_dropped", **item} for item in dropped)]
    return ArticleConversion(
        data={"poi_fragments": fragments, "day_openings": openings, "day_wraps": wraps, "summary": summary},
        actions=actions,
    )
