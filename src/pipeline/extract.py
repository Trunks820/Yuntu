"""LLM extraction: read raw items, call Gemini (via OpenAI compat), write to core tables."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import unicodedata

from sqlalchemy import text

from src.agents.llm import chat_with_usage, resolve_role_config
from src.pipeline.db import get_session_factory
from src.pipeline.place_merge import SaveTransactionRestart, record_redirected_children, upsert_source_place
from src.pipeline.visit_duration import fact_from_value, original_source_title

logger = logging.getLogger(__name__)

EXTRACT_PROMPT_VERSION = "xhs_full_v2_city_filter"

SYSTEM_PROMPT_TEMPLATE = """你是一个旅游数据分析助手。从小红书笔记中提取结构化旅游信息。

请严格按以下 JSON 格式输出（不要输出其他内容）：
{{
  "title": "笔记标题",
  "content_summary": "内容摘要，1-2句话",
  "tags": ["标签1", "标签2"],
  "content_type_tags": ["攻略", "路线"],
  "places": [
    {{
      "name": "地点名称",
      "place_type": "attraction|restaurant|business_area|market|park|museum|photo_spot|hotel|other",
      "mention_type": "recommend|avoid|pass_by|food|photo|stay|transport",
      "sentiment": "positive|neutral|negative",
      "reason": "推荐或避坑理由",
      "evidence_text": "原文摘录",
      "visit_duration": {{"raw": "建议游玩2小时", "evidence_text": "原文摘录", "confidence": 0.9}},
      "route_order": null,
      "confidence": 0.9
    }}
  ]
}}

规则：
- place_type 必须是给定枚举值之一
- mention_type 必须是给定枚举值之一
- sentiment 必须是 positive/neutral/negative 之一
- route_order 有路线顺序时填数字，否则 null
- confidence 范围 0-1
- 只提取明确提到的地点，不要推测
- 如果原文明确写了某地点建议游玩/停留/打卡/路过拍照时长，visit_duration 填结构化对象；否则填 null
- visit_duration.raw 保留原始表达（如"30分钟"、"半小时"、"1-2小时"、"半天"、"路过拍拍"），不要换算成裸字符串
- visit_duration.evidence_text 必须逐字摘录原文并保留对象、否定、交通、排队及局部活动语境；不得用改写后的建议替代原文
- 只有整点完整游览的明确分钟/小时才可采用；半天/全天仅是原文粒度，区域/组合/路线串时长不能复制给成员

## 城市过滤规则（必须严格遵守）

你正在处理 **{city}** 的旅游攻略。

✅ **只提取位于 {city} 的地点**：
- 示例：洪崖洞 ✅（重庆渝中区）、解放碑 ✅（重庆渝中区）

❌ **不要提取其他城市的地点**，即使笔记中提到：
- "重庆和成都都很好玩" → 只提取重庆地点，跳过成都
- "去重庆前先在上海中转" → 跳过上海
- 地点名称包含其他城市名（如"大理洱海"、"杭州西湖"）→ 跳过

❌ **不要提取模糊的通用场景**（无具体名称的描述性场景）：
- "小巷子"、"江边礁石"、"某咖啡店"、"夜景街道"、"观景台"（没有具体名称）
- "那家奶茶店"、"附近的公园"（指代不明）

⚠️ **如果不确定地点是否在 {city}，宁可跳过也不要提取。**"""


_VALID_PLACE_TYPES = {
    "attraction", "restaurant", "business_area", "market",
    "park", "museum", "photo_spot", "hotel", "other",
}
_VALID_MENTION_TYPES = {
    "recommend", "avoid", "pass_by", "food", "photo", "stay", "transport",
}
_VALID_SENTIMENTS = {"positive", "neutral", "negative"}


def _safe_smallint_or_none(val) -> int | None:
    """Accept only true integers (or integer-valued floats like 2.0); reject 1.5 etc."""
    if val is None:
        return None
    if isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val if -32768 <= val <= 32767 else None
    if isinstance(val, float):
        if val != int(val):
            return None
        v = int(val)
        return v if -32768 <= v <= 32767 else None
    if isinstance(val, str):
        try:
            v = int(val)
            return v if -32768 <= v <= 32767 else None
        except ValueError:
            return None
    return None


def _safe_str(val, default: str = "") -> str:
    """Coerce value to str for TEXT columns; None → default, list/dict → JSON."""
    if val is None:
        return default
    if isinstance(val, str):
        return val
    if isinstance(val, (dict, list)):
        return json.dumps(val, ensure_ascii=False)
    return str(val)


def _sanitize_place(p: dict) -> dict:
    """Clamp and whitelist LLM output fields to pass DB CHECK constraints."""
    p["place_type"] = p.get("place_type", "other") if p.get("place_type") in _VALID_PLACE_TYPES else "other"
    p["mention_type"] = p.get("mention_type", "recommend") if p.get("mention_type") in _VALID_MENTION_TYPES else "recommend"
    p["sentiment"] = p.get("sentiment", "neutral") if p.get("sentiment") in _VALID_SENTIMENTS else "neutral"
    p["route_order"] = _safe_smallint_or_none(p.get("route_order"))
    p["evidence_text"] = _safe_str(p.get("evidence_text"))
    p["reason"] = _safe_str(p.get("reason"))
    try:
        conf = float(p.get("confidence", 1.0))
    except (TypeError, ValueError):
        conf = 1.0
    p["confidence"] = max(0.0, min(1.0, conf))
    return p


def _visit_duration_fact_from_place(p: dict, *, source_text: str | None = None,
                                    source_title: str = "") -> tuple[dict, float] | None:
    """Build a structured visit_duration fact from sanitized LLM place output."""
    confidence = p.get("confidence", 1.0)
    candidates: list[tuple[object, str, float, bool]] = []
    explicit_value = (
        p.get("visit_duration")
        or p.get("visit_duration_text")
        or p.get("recommended_visit_duration")
        or p.get("duration")
    )
    if explicit_value:
        if isinstance(explicit_value, dict):
            raw_confidence = explicit_value.get("confidence", confidence)
            try:
                duration_confidence = float(raw_confidence)
            except (TypeError, ValueError):
                duration_confidence = confidence
            evidence = _safe_str(
                explicit_value.get("evidence_text")
                or explicit_value.get("evidence")
                or p.get("evidence_text")
            )
        else:
            duration_confidence = confidence
            evidence = p.get("evidence_text", "")
        candidates.append((explicit_value, evidence, duration_confidence, False))

    for value, evidence, duration_confidence, require_hint in candidates:
        fact = fact_from_value(
            value,
            confidence=duration_confidence,
            place_name=p.get("name", ""),
            place_type=p.get("place_type"),
            evidence=evidence,
            require_duration_hint=require_hint,
            source_text=source_text,
            source_title=source_title,
        )
        if fact is None:
            continue
        return (
            {
                "normalized_minutes": fact.minutes,
                "minutes": fact.minutes,
                "raw": fact.raw,
                "evidence": fact.evidence,
            },
            fact.confidence,
        )
    return None


def _normalize_place_name(name: str) -> str:
    """Normalize: strip whitespace, remove parentheses content, NFKC normalize."""
    name = unicodedata.normalize("NFKC", name)
    name = re.sub(r"[（(][^）)]*[）)]", "", name)
    return name.strip()


def _input_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


async def fetch_pending_raw_items(
    limit: int = 10,
    crawl_run_ids: list[int] | None = None,
) -> list[dict]:
    """Fetch raw items with parse_status=PENDING."""
    if crawl_run_ids is not None and not crawl_run_ids:
        return []

    factory = get_session_factory()
    async with factory() as session:
        await session.execute(text("""
            UPDATE travel_raw_item
            SET parse_status = 'IGNORED',
                parse_error = 'outside two-year discovery window'
            WHERE parse_status = 'PENDING'
              AND source_platform = 'xhs'
              AND publish_time < NOW() - INTERVAL '2 years'
        """))
        await session.commit()
        crawl_run_filter = ""
        params: dict = {"limit": limit}
        if crawl_run_ids is not None:
            crawl_run_filter = "AND crawl_run_id = ANY(CAST(:crawl_run_ids AS bigint[]))"
            params["crawl_run_ids"] = crawl_run_ids
        result = await session.execute(
            text(f"""
                SELECT id, source_id, source_url, city, keyword, raw_json, raw_text,
                       crawl_run_id, author_id, author_name, author_url, publish_time,
                       liked_count, collected_count, comment_count, shared_count
                FROM travel_raw_item
                WHERE parse_status = 'PENDING'
                  AND raw_status = 'NORMAL'
                  {crawl_run_filter}
                ORDER BY created_time
                LIMIT :limit
            """),
            params,
        )
        rows = result.fetchall()
        return [
            {
                "id": r[0], "source_id": r[1], "source_url": r[2],
                "city": r[3], "keyword": r[4],
                "raw_json": r[5], "raw_text": r[6], "crawl_run_id": r[7],
                "author_id": r[8], "author_name": r[9], "author_url": r[10],
                "publish_time": r[11], "liked_count": r[12],
                "collected_count": r[13], "comment_count": r[14],
                "shared_count": r[15],
            }
            for r in rows
        ]


ERROR_RECEIPT_VERSION = "extract-error.v1"


def classify_extract_error(exc: BaseException) -> dict:
    """Only typed HTTP status attributes prove an upstream billing failure.

    Persist a safe receipt, never provider response bodies or exception text.
    A wrapper may preserve the actual transport exception as its cause/context.
    """
    seen = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        status = getattr(current, "status_code", None)
        if status is None:
            status = getattr(getattr(current, "response", None), "status_code", None)
        if type(status) is int and 100 <= status <= 599:
            return {"version": ERROR_RECEIPT_VERSION, "category": "upstream_http",
                    "http_status": status}
        current = current.__cause__ or current.__context__
    return {"version": ERROR_RECEIPT_VERSION, "category": "call_failure"}


def extract_error_receipt(value: object) -> dict | None:
    """Read the exact structured receipt, not arbitrary occurrences of 402."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return None
    if not isinstance(value, dict) or value.get("version") != ERROR_RECEIPT_VERSION:
        return None
    if value.get("category") == "upstream_http" and type(value.get("http_status")) is int:
        return value if 100 <= value["http_status"] <= 599 else None
    return None


def is_upstream_402(value: object) -> bool:
    receipt = extract_error_receipt(value)
    return bool(receipt and receipt["http_status"] == 402)


async def call_llm_extract(raw_text: str, city: str, title: str = "") -> tuple[dict | None, dict]:
    """Call LLM (role=extract) via unified client. Returns (parsed_json, usage_meta).

    Config errors (bad provider/model/key) are NOT caught — they propagate and
    abort the whole extract run so they are never silently written as FAILED rows.
    Only transient LLM call failures and JSON parse errors return (None, meta).
    """
    input_text = f"标题: {title}\n\n正文:\n{raw_text}" if title else raw_text
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(city=city)

    # Intentionally not caught: ValueError from bad config must propagate.
    _provider, _model, _ = resolve_role_config("extract")

    t0 = time.monotonic_ns()
    try:
        raw_output, usage_meta = await chat_with_usage(
            system_prompt,
            input_text,
            role="extract",
            temperature=0.1,
            json_mode=True,
        )
    except Exception as exc:
        latency_ms = (time.monotonic_ns() - t0) // 1_000_000
        return None, {
            "error": "extraction call failed",
            "error_info": classify_extract_error(exc),
            "provider": _provider,
            "model": _model,
            "latency_ms": latency_ms,
        }

    meta = {**usage_meta, "raw_output": raw_output}

    try:
        parsed = json.loads(raw_output)
        return parsed, meta
    except json.JSONDecodeError as exc:
        meta["error"] = f"JSON parse error: {exc}"
        return None, meta


async def save_extract_result(
    raw_item: dict, parsed: dict | None, meta: dict
) -> int:
    """Save parsed output; restart its owned transaction once on identity drift.

    The failed save's session context closes/rolls back before this catch,
    releasing every stale lock and write, including log/content/raw status.
    Reuse parsed/meta; this boundary never calls the extraction model.
    """
    for attempt in range(2):
        try:
            return await _save_extract_result_once(raw_item, parsed, meta)
        except SaveTransactionRestart:
            if attempt == 1:
                raise
            logger.warning("D1 parsed save transaction restart raw_item=%s retry=1", raw_item["id"])


async def _save_extract_result_once(
    raw_item: dict, parsed: dict | None, meta: dict
) -> int:
    """One complete caller-owned save transaction; errors roll back on close."""
    factory = get_session_factory()
    raw_text = raw_item.get("raw_text") or ""
    ih = _input_hash(raw_text)
    raw_places = parsed.get("places") if isinstance(parsed, dict) else None
    if not isinstance(raw_places, list):
        raw_places = []
    places = [
        p for p in raw_places
        if isinstance(p, dict) and isinstance(p.get("name"), str) and p.get("name").strip()
    ]
    if parsed and not places:
        status = "PARTIAL"
        meta.setdefault("error", "no valid places extracted")
    elif parsed:
        status = "SUCCESS"
    else:
        status = "FAILED"

    persisted_error = (json.dumps(meta["error_info"], sort_keys=True)
                       if extract_error_receipt(meta.get("error_info")) else meta.get("error"))

    async with factory() as session:
        # 1. travel_extract_log
        log_result = await session.execute(
            text("""
                INSERT INTO travel_extract_log
                    (raw_item_id, extract_type, extract_model,
                     extract_prompt_version, input_hash,
                     extract_input_text, extract_output_raw, extract_output_json,
                     extract_status, extract_error,
                     token_input, token_output, latency_ms)
                VALUES
                    (:raw_id, 'full_extract', :model,
                     :prompt_ver, :input_hash,
                     :input_text, :output_raw, CAST(:output_json AS jsonb),
                     :status, :error,
                     :tok_in, :tok_out, :latency)
                RETURNING id
            """),
            {
                "raw_id": raw_item["id"],
                "model": meta.get("model", ""),
                "prompt_ver": EXTRACT_PROMPT_VERSION,
                "input_hash": ih,
                "input_text": raw_text,
                "output_raw": meta.get("raw_output", ""),
                "output_json": json.dumps(parsed, ensure_ascii=False) if parsed else None,
                "status": status,
                "error": persisted_error,
                "tok_in": meta.get("token_input", 0),
                "tok_out": meta.get("token_output", 0),
                "latency": meta.get("latency_ms", 0),
            },
        )
        extract_log_id = log_result.fetchone()[0]

        # Mark raw_item parse_status
        if status == "SUCCESS":
            parse_status = "PARSED"
        elif status == "PARTIAL":
            parse_status = "IGNORED"
        else:
            parse_status = "FAILED"

        await session.execute(
            text("""
                UPDATE travel_raw_item
                SET parse_status = :status, parse_error = :error
                WHERE id = :id
            """),
            {
                "status": parse_status,
                "error": persisted_error,
                "id": raw_item["id"],
            },
        )

        if not places:
            await session.commit()
            return extract_log_id

        city = raw_item.get("city", "重庆")
        hot_score = (
            int(raw_item.get("collected_count") or 0) * 2
            + int(raw_item.get("liked_count") or 0)
            + int(raw_item.get("shared_count") or 0) * 1.5
            + int(raw_item.get("comment_count") or 0) * 0.25
        )

        # 2. travel_content — coerce LLM fields to expected types
        _title = parsed.get("title")
        _summary = parsed.get("content_summary")
        _tags = parsed.get("tags")
        _type_tags = parsed.get("content_type_tags")
        safe_title = _title if isinstance(_title, str) else str(_title) if _title is not None else ""
        safe_summary = _summary if isinstance(_summary, str) else str(_summary) if _summary is not None else ""
        safe_tags = _tags if isinstance(_tags, list) else []
        safe_type_tags = _type_tags if isinstance(_type_tags, list) else []

        content_result = await session.execute(
            text("""
                INSERT INTO travel_content
                    (raw_item_id, extract_log_id, city,
                     source_platform, source_type, source_id, source_url,
                     title, author, author_id, author_url,
                     content_text, content_summary,
                     tags, content_type_tags, hot_score, publish_time, captured_time)
                VALUES
                    (:raw_id, :log_id, :city,
                     'xhs', 'note', :source_id, :source_url,
                     :title, :author, :author_id, :author_url,
                     :content_text, :summary,
                     CAST(:tags AS jsonb), CAST(:type_tags AS jsonb), :hot_score,
                     :publish_time, NOW())
                RETURNING id
            """),
            {
                "raw_id": raw_item["id"],
                "log_id": extract_log_id,
                "city": city,
                "source_id": raw_item.get("source_id"),
                "source_url": raw_item.get("source_url", ""),
                "title": safe_title,
                "author": raw_item.get("author_name"),
                "author_id": raw_item.get("author_id"),
                "author_url": raw_item.get("author_url"),
                "content_text": raw_text,
                "summary": safe_summary,
                "tags": json.dumps(safe_tags, ensure_ascii=False),
                "type_tags": json.dumps(safe_type_tags, ensure_ascii=False),
                "hot_score": hot_score,
                "publish_time": raw_item.get("publish_time"),
            },
        )
        content_id = content_result.fetchone()[0]

        # 3-5. For each place: sanitize, upsert place, insert mention, insert facts
        for p in places:
            p = _sanitize_place(p)
            place_name = p.get("name", "").strip()
            if not place_name:
                continue

            normalized = _normalize_place_name(place_name)
            place_type = p["place_type"]
            confidence = p["confidence"]

            # Resolve before any upsert row lock; hold identity through child writes.
            place_id = await upsert_source_place(
                session, city=city, name=place_name,
                normalized_name=normalized, place_type=place_type,
            )
            children = {"travel_content_place_mention": [], "travel_place_fact": []}

            # 4. travel_content_place_mention
            mention_result = await session.execute(
                text("""
                    INSERT INTO travel_content_place_mention
                        (content_id, place_id, place_name_text,
                         mention_context, mention_type, sentiment,
                         route_order, confidence)
                    VALUES
                        (:cid, :pid, :name_text,
                         :context, :mtype, :sentiment,
                         :route_order, :confidence)
                    RETURNING id
                """),
                {
                    "cid": content_id,
                    "pid": place_id,
                    "name_text": place_name,
                    "context": p["evidence_text"],
                    "mtype": p["mention_type"],
                    "sentiment": p["sentiment"],
                    "route_order": p["route_order"],
                    "confidence": confidence,
                },
            )
            children["travel_content_place_mention"].append(mention_result.scalar_one())

            # 5. travel_place_fact — reason as recommendation or warning
            reason = p["reason"]
            if reason:
                mention_type = p.get("mention_type", "recommend")
                fact_type = "warning" if mention_type == "avoid" else "recommendation"
                fact_result = await session.execute(
                    text("""
                        INSERT INTO travel_place_fact
                            (place_id, source_platform, source_id, source_url,
                             fact_type, fact_value, confidence)
                        VALUES
                            (:pid, 'xhs', :source_id, :source_url,
                             :fact_type, CAST(:fact_value AS jsonb), :confidence)
                        RETURNING id
                    """),
                    {
                        "pid": place_id,
                        "source_id": raw_item.get("source_id"),
                        "source_url": raw_item.get("source_url", ""),
                        "fact_type": fact_type,
                        "fact_value": json.dumps(
                            {"reason": reason, "evidence": p["evidence_text"]},
                            ensure_ascii=False,
                        ),
                        "confidence": confidence,
                    },
                )
                children["travel_place_fact"].append(fact_result.scalar_one())

            visit_duration_fact = _visit_duration_fact_from_place(
                p, source_text=raw_text, source_title=original_source_title(raw_item.get("raw_json")))
            if visit_duration_fact:
                fact_value, fact_confidence = visit_duration_fact
                fact_result = await session.execute(
                    text("""
                        INSERT INTO travel_place_fact
                            (place_id, source_platform, source_id, source_url,
                             fact_type, fact_value, confidence)
                        VALUES
                            (:pid, 'xhs', :source_id, :source_url,
                             'visit_duration', CAST(:fact_value AS jsonb), :confidence)
                        RETURNING id
                    """),
                    {
                        "pid": place_id,
                        "source_id": raw_item.get("source_id"),
                        "source_url": raw_item.get("source_url", ""),
                        "fact_value": json.dumps(fact_value, ensure_ascii=False),
                        "confidence": fact_confidence,
                    },
                )
                children["travel_place_fact"].append(fact_result.scalar_one())
            await record_redirected_children(session, place_id=place_id, children=children)

        await session.commit()
        logger.info(
            "Extracted raw_item=%s -> content=%s, places=%d",
            raw_item["id"], content_id, len(places),
        )
        return extract_log_id
