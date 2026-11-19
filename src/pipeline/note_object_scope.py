"""Arrangement-only source receipts. Does not change D2 numeric admission."""
import re
from src.pipeline.visit_duration import applicable_visit_evidence, normalize_visit_duration

_NON_VISIT = re.compile(r"不用|不需要|无需|不要|不建议|没必要|营业|开放|排队|交通|车程|接驳|路程|往返|到达|只玩|局部")
_FULL_DAY_EXCLUSION = re.compile(
    r"不必|并非|不是|没有|未能|没逛完|没玩完|不够|不能|不看|只看|只在|不推荐|"
    r"等候|等了|船程|通勤|地铁|高铁|大巴|公交|开车|自驾|坐船|乘船|导航|"
    r"返程|单程|上山路|下山路|抵达|每个|每次|一趟|班次|吃饭|午饭|晚饭|"
    r"休息|停运|检票|拍照|打卡|路过|联票|其中|一部分|登顶|山顶|上山|下山|门口"
)
_DAY_GRAIN = r"(?:一整天|全天|一天)"
_FULL_DAY_VISIT = r"(?:完整游览|完整游玩|游玩|游览|逛完|玩完|畅玩)"
_TICKET_SUFFIX = r"(?:[（(]门票(?:价格)?[:：]?\d+(?:\.\d+)?元[）)])?"
_FULL_DAY_ADVICE = re.compile(
    r"(?:(?:建议)?避开(?:周末|节假日|高峰期?)|"
    r"提前(?:备好|准备)(?:雨衣|雨具|饮水)|(?:热门|重点)?项目(?:优先体验|提前预约))"
)


def _single_target_day_heading(source_text, pos, name):
    lines = [line.strip() for line in source_text[:pos].splitlines() if line.strip()]
    return bool(lines and re.fullmatch(
        r"[-•*#\s]*DAY\s*\d+\s*[:：]\s*" + re.escape(name), lines[-1], re.I,
    ))


def _full_day_target(text, name, *, day_heading=False):
    """Accept a bound predicate, with narrowly non-scope-changing advice.

    The complete clause is retained for exclusion checks. A suffix cannot hide
    a second object, partial visit, denial, or another duration. Exact target
    names (including confirmed aliases supplied by the loader) stay required.
    """
    if _FULL_DAY_EXCLUSION.search(text):
        return False
    parts = re.split(r"[,，;；]", text)
    if any(not _FULL_DAY_ADVICE.fullmatch(part) for part in parts[1:]):
        return False
    target = re.escape(name)
    bound = re.fullmatch(
        r"(?:" + target + r"(?:[:：])?" + _FULL_DAY_VISIT
        + r"(?:需|需要|建议|预留|安排)?" + _DAY_GRAIN
        + r"|" + _DAY_GRAIN + _FULL_DAY_VISIT + target + r")" + _TICKET_SUFFIX,
        parts[0],
    )
    if bound:
        return name
    # A short reference is local to this exact single-target DAY heading and
    # its immediately following predicate. Never register it as an alias or
    # remove arbitrary suffixes when no original heading names the target.
    reference = re.fullmatch(
        _DAY_GRAIN + _FULL_DAY_VISIT + r"(?P<name>[\u4e00-\u9fffA-Za-z]{3,})" + _TICKET_SUFFIX,
        parts[0],
    )
    if day_heading and reference and re.fullmatch(
        re.escape(reference['name']) + r"(?:主题)?(?:乐园|公园|景区|博物馆|古镇)", name,
    ):
        return reference['name']
    return None



def _variant_range(text):
    from src.pipeline.visit_duration import (_RANGE_HOUR_PATTERN, _RANGE_MINUTE_PATTERN,
        _HOUR_PATTERN, _MINUTE_PATTERN, _CHINESE_HOUR_PATTERN, _CHINESE_MINUTE_PATTERN, _to_number)
    for pattern, factor in [(_RANGE_HOUR_PATTERN,60), (_RANGE_MINUTE_PATTERN,1)]:
        match=pattern.search(text)
        if match:
            bounds=[_to_number(value) for value in match.groups()]
            if all(value is not None for value in bounds):
                return int(min(bounds)*factor),int(max(bounds)*factor)
    for pattern, factor in [(_HOUR_PATTERN,60),(_MINUTE_PATTERN,1),(_CHINESE_HOUR_PATTERN,60),(_CHINESE_MINUTE_PATTERN,1)]:
        match=pattern.search(text)
        if match:
            value=_to_number(match.group(1))
            if value is not None:
                minutes=int(value*factor)
                return minutes,minutes
    normalized=normalize_visit_duration(text)
    return (normalized.minutes,normalized.minutes) if normalized else (None,None)

def arrangement_receipts(value, *, place_name, source_text, source_title="", source_id=None):
    if not isinstance(value, dict) or not source_id or not source_text or not place_name:
        return []
    rows = value.get("play_styles") or value.get("variants") or [value]
    if not isinstance(rows, list):
        return []
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        evidence = str(row.get("evidence") or row.get("evidence_text") or "")
        if not evidence or source_text.count(evidence) != 1:
            continue
        pos = source_text.index(evidence)
        left = max(source_text.rfind(mark, 0, pos) for mark in ("。", "！", "？", "\n")) + 1
        stops = [source_text.find(mark, pos + len(evidence)) for mark in ("。", "！", "？", "\n")]
        clause = source_text[left:min([v for v in stops if v >= 0], default=len(source_text))]
        if _NON_VISIT.search(clause):
            continue
        text = re.sub(r"\s+", "", clause)
        name = re.sub(r"\s+", "", place_name)
        receipt = {"source_id": str(source_id), "evidence": evidence, "raw": row.get("raw", evidence),
                   "source_title": source_title, "note_line": source_text[:pos].count("\n") + 1,
                   "whole_target": place_name, "covered_names": []}
        # Complete original target, never a region/combined route copied to members.
        full = _full_day_target(text, name,
            day_heading=_single_target_day_heading(source_text, pos, name))
        contained = re.fullmatch(re.escape(name) + r"[（(](.+?)(?:全程|完整)(?:游玩|游览|逛完|玩完)(?:全天|一整天|一天)[）)]", text)
        if full or contained:
            if full and full != name:
                receipt['source_reference'] = full
                receipt['scope_binding'] = 'single_target_day_heading'
            if contained:
                receipt["covered_names"] = [n.strip() for n in re.split(r"→|->|、|/|[+＋]", contained.group(1)) if n.strip()]
            result.append({**receipt, "kind": "full_day"})
            continue
        style = row.get("style_name") or row.get("play_style")
        if isinstance(style, str) and style.strip():
            style = style.strip()
            # Source text itself must name this style for this complete target.
            match = re.fullmatch(re.escape(name) + r"(?:[:：])?" + re.escape(style) +
                                 r"(?:玩法)?(?:完整游览|完整游玩|逛完|玩完)(.+)", text)
            if match:
                minimum, maximum = _variant_range(match.group(1))
                result.append({**receipt, "kind": "play_style", "style_name": style,
                    "minutes": maximum, "min_minutes": minimum, "max_minutes": maximum})
            continue
        if applicable_visit_evidence(evidence, place_name=place_name, source_text=source_text, source_title=source_title):
            normalized = normalize_visit_duration(evidence)
            if normalized:
                chain = re.fullmatch(re.escape(name) + r"[（(](.+?)(?:全程|完整|全部)(?:游玩|游览|逛完|玩完).+[）)]", text)
                if chain:
                    receipt["covered_names"] = [n.strip() for n in re.split(r"→|->|、|/|[+＋]", chain.group(1)) if n.strip()]
                result.append({**receipt, "kind": "whole_visit", "minutes": normalized.minutes})
    return result
