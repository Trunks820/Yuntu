"""Refresh travel_place_summary by full recalculation (MVP: small data, no incremental)."""

from __future__ import annotations

import json
import logging

from sqlalchemy import text

from src.pipeline.db import get_session_factory
from src.pipeline.summary_identity import link_summary_identities
from src.pipeline.visit_duration import (
    aggregate_visit_duration_facts,
    load_canonical_duration_facts,
)

logger = logging.getLogger(__name__)


async def _has_column(session, table_name: str, column_name: str) -> bool:
    result = await session.execute(
        text("""
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = :table_name
                  AND column_name = :column_name
            )
        """),
        {"table_name": table_name, "column_name": column_name},
    )
    return bool(result.scalar())


def _json_value(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


async def _refresh_canonical_visit_durations(session, city: str | None = None) -> int:
    has_duration_schema = (
        await _has_column(session, "travel_canonical_place", "typical_visit_minutes")
        and await _has_column(session, "travel_canonical_place", "typical_visit_source")
        and await _has_column(session, "travel_canonical_place", "typical_visit_confidence")
        and await _has_column(session, "travel_canonical_place", "typical_visit_updated_at")
        and await _has_column(session, "travel_canonical_place", "updated_at")
        and await _has_column(session, "travel_place_summary", "canonical_place_id")
    )
    if not has_duration_schema:
        return 0

    params = {"city": city} if city else {}
    grouped = await load_canonical_duration_facts(session, city=city)

    updated = 0
    valid_ids: list[int] = []
    for canonical_id, bucket in grouped.items():
        aggregate = aggregate_visit_duration_facts(
            bucket["facts"],
            place_name=bucket["name"],
            place_type=bucket["place_type"],
        )
        if aggregate is None:
            continue
        valid_ids.append(canonical_id)
        result = await session.execute(
            text("""
                UPDATE travel_canonical_place
                SET typical_visit_minutes = :minutes,
                    typical_visit_source = :source,
                    typical_visit_confidence = :confidence,
                    typical_visit_updated_at = NOW(),
                    updated_at = NOW()
                WHERE place_id = :canonical_id
                  AND COALESCE(typical_visit_source, '') NOT IN ('manual', 'amap')
            """),
            {
                "canonical_id": canonical_id,
                "minutes": aggregate.minutes,
                "source": aggregate.source,
                "confidence": aggregate.confidence,
            },
        )
        rowcount = result.rowcount if result.rowcount is not None else 0
        updated += max(0, int(rowcount))

    clear_params = dict(params)
    clear_scope = "AND city = :city" if city else ""
    if valid_ids:
        clear_params["valid_ids"] = valid_ids
        stale_filter = "AND place_id <> ALL(CAST(:valid_ids AS bigint[]))"
    else:
        stale_filter = ""
    await session.execute(
        text(f"""
            UPDATE travel_canonical_place
            SET typical_visit_minutes = NULL,
                typical_visit_source = NULL,
                typical_visit_confidence = NULL,
                typical_visit_updated_at = NULL,
                updated_at = NOW()
            WHERE typical_visit_source = 'xhs_median'
              {clear_scope}
              {stale_filter}
        """),
        clear_params,
    )
    return updated


async def refresh_canonical_visit_durations(city: str | None = None) -> int:
    """Backfill canonical typical_visit fields from existing XHS duration facts."""
    async with get_session_factory()() as session:
        updated = await _refresh_canonical_visit_durations(session, city)
        await session.commit()
    logger.info("Refreshed %d canonical visit durations for city=%s", updated, city or "ALL")
    return updated


async def refresh_place_summary(city: str | None = None):
    """Full refresh of travel_place_summary for a given city (or all cities)."""
    factory = get_session_factory()

    city_filter_place = "AND p.city = :city" if city else ""
    city_filter_summary = "AND city = :city" if city else ""
    city_filter_summary_alias = "AND s.city = :city" if city else ""
    params = {"city": city} if city else {}

    async with factory() as session:
        has_v06_columns = (
            await _has_column(session, "travel_place", "poi_resolution_status")
            and await _has_column(session, "travel_place_summary", "amap_poi_id")
        )

        if has_v06_columns:
            insert_columns = """
                    (place_id, city, name, place_type, address,
                     longitude, latitude, tags,
                     amap_poi_id, adcode, place_type_resolved,
                     mention_count_30d, positive_count_30d, negative_count_30d,
                     source_count, latest_captured_time,
                     top_reasons, warnings,
                     hot_score, quality_score, recommend_score)
            """
            select_columns = """
                    p.id,
                    p.city,
                    p.name,
                    p.place_type,
                    p.address,
                    p.longitude,
                    p.latitude,
                    p.tags,
                    p.amap_poi_id,
                    p.adcode,
                    p.place_type_resolved,
                    COALESCE(m.mention_count, 0),
                    COALESCE(m.positive_count, 0),
                    COALESCE(m.negative_count, 0),
                    COALESCE(m.source_count, 0),
                    m.latest_captured,
                    COALESCE(f.top_reasons, '[]'::jsonb),
                    COALESCE(f.warnings, '[]'::jsonb),
                    COALESCE(m.hot_score, 0),
                    LEAST(10, COALESCE(m.mention_count, 0) * 0.5
                        + COALESCE(m.source_count, 0) * 1.0),
                    LEAST(10,
                        COALESCE(m.mention_count, 0) * 0.3
                        + CASE WHEN COALESCE(m.mention_count, 0) > 0
                            THEN COALESCE(m.positive_count, 0)::numeric
                                / m.mention_count * 5
                            ELSE 0 END
                        + COALESCE(m.source_count, 0) * 0.5
                        + CASE WHEN p.place_type = 'restaurant' THEN 1.5 ELSE 0 END
                    )
            """
            v06_filter = "AND p.poi_resolution_status = 'resolved'"
            update_extra = """
                    amap_poi_id = EXCLUDED.amap_poi_id,
                    adcode = EXCLUDED.adcode,
                    place_type_resolved = EXCLUDED.place_type_resolved,
            """
        else:
            insert_columns = """
                    (place_id, city, name, place_type, address,
                     longitude, latitude, tags,
                     mention_count_30d, positive_count_30d, negative_count_30d,
                     source_count, latest_captured_time,
                     top_reasons, warnings,
                     hot_score, quality_score, recommend_score)
            """
            select_columns = """
                    p.id,
                    p.city,
                    p.name,
                    p.place_type,
                    p.address,
                    p.longitude,
                    p.latitude,
                    p.tags,
                    COALESCE(m.mention_count, 0),
                    COALESCE(m.positive_count, 0),
                    COALESCE(m.negative_count, 0),
                    COALESCE(m.source_count, 0),
                    m.latest_captured,
                    COALESCE(f.top_reasons, '[]'::jsonb),
                    COALESCE(f.warnings, '[]'::jsonb),
                    COALESCE(m.hot_score, 0),
                    LEAST(10, COALESCE(m.mention_count, 0) * 0.5
                        + COALESCE(m.source_count, 0) * 1.0),
                    LEAST(10,
                        COALESCE(m.mention_count, 0) * 0.3
                        + CASE WHEN COALESCE(m.mention_count, 0) > 0
                            THEN COALESCE(m.positive_count, 0)::numeric
                                / m.mention_count * 5
                            ELSE 0 END
                        + COALESCE(m.source_count, 0) * 0.5
                        + CASE WHEN p.place_type = 'restaurant' THEN 1.5 ELSE 0 END
                    )
            """
            v06_filter = ""
            update_extra = ""

        await session.execute(
            text(f"""
                INSERT INTO travel_place_summary
                    {insert_columns}
                SELECT
                    {select_columns}
                FROM travel_place p
                LEFT JOIN LATERAL (
                    SELECT
                        COUNT(*) AS mention_count,
                        COUNT(*) FILTER (WHERE cpm.sentiment = 'positive') AS positive_count,
                        COUNT(*) FILTER (WHERE cpm.sentiment = 'negative') AS negative_count,
                        COUNT(DISTINCT c.source_id) AS source_count,
                        MAX(c.captured_time) AS latest_captured,
                        AVG(c.hot_score) AS hot_score
                    FROM travel_content_place_mention cpm
                    JOIN travel_content c ON c.id = cpm.content_id
                    WHERE cpm.place_id = p.id
                      AND c.created_time >= NOW() - INTERVAL '30 days'
                ) m ON true
                LEFT JOIN LATERAL (
                    SELECT
                        jsonb_agg(pf.fact_value) FILTER
                            (WHERE pf.fact_type = 'recommendation') AS top_reasons,
                        jsonb_agg(pf.fact_value) FILTER
                            (WHERE pf.fact_type = 'warning') AS warnings
                    FROM travel_place_fact pf
                    WHERE pf.place_id = p.id
                ) f ON true
                WHERE 1=1 {city_filter_place}
                  AND p.merged_into_place_id IS NULL
                  {v06_filter}
                ON CONFLICT (place_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    place_type = EXCLUDED.place_type,
                    address = EXCLUDED.address,
                    longitude = EXCLUDED.longitude,
                    latitude = EXCLUDED.latitude,
                    tags = EXCLUDED.tags,
                    {update_extra}
                    mention_count_30d = EXCLUDED.mention_count_30d,
                    positive_count_30d = EXCLUDED.positive_count_30d,
                    negative_count_30d = EXCLUDED.negative_count_30d,
                    source_count = EXCLUDED.source_count,
                    latest_captured_time = EXCLUDED.latest_captured_time,
                    top_reasons = EXCLUDED.top_reasons,
                    warnings = EXCLUDED.warnings,
                    hot_score = EXCLUDED.hot_score,
                    quality_score = EXCLUDED.quality_score,
                    recommend_score = EXCLUDED.recommend_score
            """),
            params,
        )
        if has_v06_columns:
            await session.execute(
                text(f"""
                    DELETE FROM travel_place_summary AS s
                    USING travel_place AS p
                    WHERE s.place_id = p.id
                      AND (p.poi_resolution_status IS DISTINCT FROM 'resolved'
                           OR p.merged_into_place_id IS NOT NULL)
                      {city_filter_summary_alias}
                """),
                params,
            )
        if await _has_column(session, "travel_place_summary", "canonical_place_id"):
            await link_summary_identities(session, city)
        await _refresh_canonical_visit_durations(session, city)
        await session.commit()

        count_result = await session.execute(
            text(f"""
                SELECT COUNT(*) FROM travel_place_summary
                WHERE 1=1 {city_filter_summary}
            """),
            params,
        )
        total = count_result.scalar()
        logger.info("Refreshed %d place summaries for city=%s", total, city or "ALL")
        return total
