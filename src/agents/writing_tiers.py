"""v0.15.1 tier table: what the Writer is told and what the checks do, in one list.

block  - the prompt forbids it; a check finds it and the sentence is deleted
         (or the opening, wrap or summary is dropped), never the whole plan.
record - the prompt asks for care; checks only record it.
allow  - the prompt allows it; no check treats it as a hard fact.

Only WRITER_PROMPT_VERSION=timeline reads this table. The v6 prompt and its
checks keep their own wording and markers.
"""
from __future__ import annotations

from dataclasses import dataclass

_HISTORY_CULTURE_MARKERS = (
    "老字号", "百年", "民国", "宋代", "唐代", "明清", "历史底蕴", "文化底蕴", "古典园林",
)
_MONTH_MARKERS = (
    "12月", "12月份", "一月", "二月", "三月", "四月", "五月", "六月",
    "七月", "八月", "九月", "十月", "十一月", "十二月",
)


@dataclass(frozen=True)
class Tier:
    rule: str
    tier: str  # "block" | "record" | "allow"
    prompt: str
    # unsupported-fact pattern reasons (fact_expression_taxonomy) this rule owns
    fact_reasons: tuple[str, ...] = ()
    # hard-fact marker words this rule takes out of the hard-fact list
    released_markers: tuple[str, ...] = ()
    # marker words a block rule adds to the hard-fact list
    added_markers: tuple[str, ...] = ()


TIERS: tuple[Tier, ...] = (
    Tier("route_places", "block", "只写时间表里的地点，不提路线外的地点名"),
    Tier("verifiable_facts", "block", "价格、门票金额、开放时间、放票时间、提前几天预约，只写材料里有的；不写怎么买票",
         fact_reasons=("price_claim", "ticket_claim", "opening_hours_claim", "booking_detail_claim",
                       "ticket_decision_advice"),
         added_markers=("放票", "开票", "开抢", "抢票")),
    Tier("reminders", "allow",
         '不带时间、天数和金额的一般提醒可以写，例如"一般要提前实名预约""门票单次有效，出园不能再进"；'
         '"常规周一闭馆，去前确认"只在时间表上这天正好是闭馆日时写',
         fact_reasons=("booking_claim",)),
    Tier("source_voice", "block",
         '不写"据说、听说、网传、亲测、博主推荐、作者推荐、本地人推荐、当地人常去"这类来源口吻',
         fact_reasons=("source_voice_claim",)),
    Tier("clock_and_transport", "block", "正文不写几点、多少分钟和交通方式，这些时间表里已经有",
         fact_reasons=("transport_decision_advice", "onsite_transport_claim")),
    Tier("weather", "block", "不写天气、气温、防晒、补水、带伞、加衣这类提醒，天气单独显示"),
    Tier("season", "record",
         '季节和条件不能丢：材料写明月份、季节或条件的（例如"12 月初到中旬看红枫"），按本次出行日期判断还能不能看到；'
         '看不到就不写，或写成"要等到某月"的提醒',
         released_markers=_MONTH_MARKERS),
    Tier("crowd_and_ranking", "record", '"人多、排队、必打卡、最佳、很出片"这类说法少用，材料里有才写成提醒',
         fact_reasons=("popularity_best_claim",)),
    Tier("common_knowledge", "allow",
         "名气大的地方，公认的来历、传说、标志性景物、镇馆展品和园内走法可以写；不出名的按地点类型写；具体数字、具体菜名、具体年份只写材料里有的",
         fact_reasons=("history_culture_claim",), released_markers=_HISTORY_CULTURE_MARKERS),
)


def prompt_rules() -> str:
    """The table's lines for the timeline system prompt."""
    return "\n".join(f"- {tier.prompt}" for tier in TIERS)


def allowed_fact_reasons() -> frozenset[str]:
    return frozenset(reason for tier in TIERS if tier.tier == "allow" for reason in tier.fact_reasons)


def blocking_fact_reasons() -> frozenset[str]:
    return frozenset(reason for tier in TIERS if tier.tier == "block" for reason in tier.fact_reasons)


def released_hard_markers() -> frozenset[str]:
    return frozenset(marker for tier in TIERS if tier.tier != "block" for marker in tier.released_markers)


def added_hard_markers() -> tuple[str, ...]:
    return tuple(marker for tier in TIERS if tier.tier == "block" for marker in tier.added_markers)
