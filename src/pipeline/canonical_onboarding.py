"""Evidence-driven promotion from discovered travel_place rows to canonical POIs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from src.pipeline.db import get_session_factory
from src.pipeline.place_merge import materialize_alias_intents

RULE_VERSION = "v0.6.16-onboarding-v1"
AUTO_PLACE_TYPES = frozenset({
    "attraction", "park", "museum", "photo_spot", "business_area", "market",
})


@dataclass(frozen=True)
class OnboardingStats:
    scanned: int = 0
    created: int = 0
    linked: int = 0
    auto_accepted: int = 0
    pending_review: int = 0


async def _write_event(
    session,
    *,
    place_id: int,
    action: str,
    from_trust: str | None,
    from_review: str | None,
    to_trust: str | None,
    to_review: str | None,
    reason_code: str,
    operator_id: str = "system",
    merge_target_place_id: int | None = None,
    metadata: dict | None = None,
) -> None:
    await session.execute(text("""
        INSERT INTO travel_canonical_place_review_event (
            canonical_place_id, action,
            from_trust_level, from_review_status,
            to_trust_level, to_review_status,
            reason_code, operator_id, merge_target_place_id,
            rule_version, metadata
        ) VALUES (
            :place_id, :action,
            :from_trust, :from_review,
            :to_trust, :to_review,
            :reason_code, :operator_id, :merge_target_place_id,
            :rule_version, CAST(:metadata AS jsonb)
        )
    """), {
        "place_id": place_id,
        "action": action,
        "from_trust": from_trust,
        "from_review": from_review,
        "to_trust": to_trust,
        "to_review": to_review,
        "reason_code": reason_code,
        "operator_id": operator_id,
        "merge_target_place_id": merge_target_place_id,
        "rule_version": RULE_VERSION,
        "metadata": json.dumps(metadata or {}, ensure_ascii=False),
    })


async def onboard_city(city: str, *, crawl_run_ids: list[int] | None = None) -> OnboardingStats:
    """Attach resolved discovered POIs to canonical and apply admission rules."""
    run_filter = ""
    params: dict[str, object] = {
        "city": city,
        "fresh_cutoff": datetime.now(timezone.utc) - timedelta(days=365),
    }
    if crawl_run_ids:
        run_filter = "AND raw.crawl_run_id = ANY(CAST(:crawl_run_ids AS bigint[]))"
        params["crawl_run_ids"] = crawl_run_ids

    async with get_session_factory()() as session:
        rows = (await session.execute(text(f"""
            SELECT
                p.id AS source_place_id,
                p.name,
                p.normalized_name,
                p.place_type,
                p.address,
                p.longitude,
                p.latitude,
                p.amap_poi_id,
                p.adcode,
                COALESCE(p.poi_alternatives_count, 0) AS alternatives_count,
                COUNT(DISTINCT c.author_id) FILTER (
                    WHERE c.author_id IS NOT NULL AND c.author_id <> ''
                )::int AS author_count,
                COUNT(DISTINCT c.source_id) FILTER (
                    WHERE c.publish_time >= :fresh_cutoff
                )::int AS fresh_note_count,
                ARRAY_REMOVE(ARRAY_AGG(DISTINCT c.source_id), NULL) AS note_ids,
                ARRAY_REMOVE(ARRAY_AGG(DISTINCT c.author_id), NULL) AS author_ids
            FROM travel_place AS p
            JOIN travel_content_place_mention AS mention ON mention.place_id = p.id
            JOIN travel_content AS c ON c.id = mention.content_id
            JOIN travel_raw_item AS raw ON raw.id = c.raw_item_id
            WHERE p.city = :city
              AND p.merged_into_place_id IS NULL
              AND p.poi_resolution_status = 'resolved'
              AND p.latitude IS NOT NULL
              AND p.longitude IS NOT NULL
              AND p.adcode IS NOT NULL
              {run_filter}
            GROUP BY p.id
            ORDER BY p.id
        """), params)).all()

        created = linked = auto_accepted = pending_review = 0
        for row in rows:
            alias_conflict = (await session.execute(text("""
                SELECT EXISTS (
                    SELECT 1
                    FROM travel_place_alias AS alias
                    JOIN travel_canonical_place AS owner
                      ON owner.place_id = alias.canonical_place_id
                    WHERE alias.normalized_alias = :normalized_name
                      AND alias.status = 'confirmed'
                       AND owner.city = :city
                       AND (
                           CAST(:amap_poi_id AS TEXT) IS NULL
                           OR owner.amap_poi_id IS DISTINCT FROM CAST(:amap_poi_id AS TEXT)
                       )
                )
            """), {
                "normalized_name": row.normalized_name,
                "city": city,
                "amap_poi_id": row.amap_poi_id,
            })).scalar_one()
            canonical = None
            if row.amap_poi_id:
                canonical = (await session.execute(text("""
                    SELECT *
                    FROM travel_canonical_place
                    WHERE amap_poi_id = :amap_poi_id
                      AND is_active = TRUE
                    ORDER BY place_id
                    LIMIT 1
                    FOR UPDATE
                """), {"amap_poi_id": row.amap_poi_id})).one_or_none()
            name_conflict = False
            if canonical is None:
                name_match = (await session.execute(text("""
                    SELECT *
                    FROM travel_canonical_place
                    WHERE city = :city AND canonical_name = :name
                      AND is_active = TRUE
                    FOR UPDATE
                """), {"city": city, "name": row.name})).one_or_none()
                if (
                    name_match is not None
                    and name_match.amap_poi_id
                    and row.amap_poi_id
                    and name_match.amap_poi_id != row.amap_poi_id
                ):
                    name_conflict = True
                    alias_conflict = True
                else:
                    canonical = name_match

            auto_eligible = (
                bool(row.amap_poi_id)
                and int(row.alternatives_count) <= 1
                and int(row.author_count) >= 2
                and int(row.fresh_note_count) >= 1
                and row.place_type in AUTO_PLACE_TYPES
                and not bool(alias_conflict)
            )
            target_trust = "trusted" if auto_eligible else "candidate"
            target_review = "auto_accepted" if auto_eligible else "pending_review"
            canonical_name = (
                f"{row.name}（{row.address or row.amap_poi_id}）"
                if name_conflict else row.name
            )

            if canonical is None:
                canonical = (await session.execute(text("""
                    INSERT INTO travel_canonical_place (
                        canonical_name, city, address, adcode, latitude, longitude,
                        amap_poi_id, place_type, source_type,
                        trust_level, review_status, is_active, geo_status, base_priority
                    ) VALUES (
                        :name, :city, :address, :adcode, :latitude, :longitude,
                        :amap_poi_id, :place_type, 'xhs',
                        :trust_level, :review_status, TRUE, 'resolved', 50
                    )
                    RETURNING *
                """), {
                    "name": canonical_name,
                    "city": city,
                    "address": row.address,
                    "adcode": row.adcode,
                    "latitude": row.latitude,
                    "longitude": row.longitude,
                    "amap_poi_id": row.amap_poi_id,
                    "place_type": row.place_type,
                    "trust_level": target_trust,
                    "review_status": target_review,
                })).one()
                created += 1
                await _write_event(
                    session,
                    place_id=int(canonical.place_id),
                    action="auto_approve" if auto_eligible else "candidate_create",
                    from_trust=None,
                    from_review=None,
                    to_trust=target_trust,
                    to_review=target_review,
                    reason_code=(
                        "multi_author_amap_unique_match"
                        if auto_eligible else "evidence_requires_review"
                    ),
                    metadata={
                        "source_place_id": int(row.source_place_id),
                        "note_ids": list(row.note_ids or []),
                        "author_ids": list(row.author_ids or []),
                        "amap_poi_id": row.amap_poi_id,
                        "alternatives_count": int(row.alternatives_count),
                        "alias_conflict": bool(alias_conflict),
                    },
                )
                if auto_eligible:
                    auto_accepted += 1
                else:
                    pending_review += 1
            else:
                if (
                    auto_eligible
                    and canonical.trust_level == "candidate"
                    and canonical.review_status == "pending_review"
                ):
                    await session.execute(text("""
                        UPDATE travel_canonical_place
                        SET trust_level = 'trusted',
                            review_status = 'auto_accepted'
                        WHERE place_id = :place_id
                    """), {"place_id": canonical.place_id})
                    await _write_event(
                        session,
                        place_id=int(canonical.place_id),
                        action="auto_approve",
                        from_trust=canonical.trust_level,
                        from_review=canonical.review_status,
                        to_trust="trusted",
                        to_review="auto_accepted",
                        reason_code="multi_author_amap_unique_match",
                        metadata={
                            "source_place_id": int(row.source_place_id),
                            "note_ids": list(row.note_ids or []),
                            "author_ids": list(row.author_ids or []),
                            "amap_poi_id": row.amap_poi_id,
                        },
                    )
                    auto_accepted += 1

            canonical_id = int(canonical.place_id)
            link_result = await session.execute(text("""
                UPDATE travel_place_summary
                SET canonical_place_id = :canonical_id
                WHERE place_id = :source_place_id
                  AND canonical_place_id IS NULL
                  AND EXISTS (SELECT 1 FROM travel_place p WHERE p.id=:source_place_id
                              AND p.merged_into_place_id IS NULL)
                  AND NOT EXISTS (
                      SELECT 1
                      FROM travel_place_summary AS existing
                      WHERE existing.canonical_place_id = :canonical_id
                        AND existing.place_id <> :source_place_id
                  )
            """), {
                "canonical_id": canonical_id,
                "source_place_id": row.source_place_id,
            })
            linked += max(link_result.rowcount or 0, 0)
            await materialize_alias_intents(session, int(row.source_place_id))
            if row.name != canonical.canonical_name:
                await session.execute(text("""
                    INSERT INTO travel_place_alias (
                        canonical_place_id, alias_name, normalized_alias,
                        relation_type, status, source, confidence, note
                    ) VALUES (
                        :canonical_id, :alias_name, :normalized_alias,
                        'alias_same', 'candidate', 'xhs', 0.5,
                        'Discovered during v0.6.16 onboarding'
                    )
                    ON CONFLICT (canonical_place_id, normalized_alias) DO NOTHING
                """), {
                    "canonical_id": canonical_id,
                    "alias_name": row.name,
                    "normalized_alias": row.normalized_name,
                })

            sources = (await session.execute(text("""
                SELECT DISTINCT c.source_id, c.source_url, c.title
                FROM travel_content_place_mention AS mention
                JOIN travel_content AS c ON c.id = mention.content_id
                WHERE mention.place_id = :source_place_id
                  AND c.source_platform = 'xhs'
            """), {"source_place_id": row.source_place_id})).all()
            for source in sources:
                await session.execute(text("""
                    INSERT INTO travel_canonical_place_source (
                        canonical_place_id, source_type, source_name, source_url,
                        source_place_name, evidence_summary
                    ) VALUES (
                        :canonical_id, 'xhs', :source_name, :source_url,
                        :source_place_name, :evidence_summary
                    )
                    ON CONFLICT DO NOTHING
                """), {
                    "canonical_id": canonical_id,
                    "source_name": f"XHS note {source.source_id}",
                    "source_url": source.source_url,
                    "source_place_name": row.name,
                    "evidence_summary": (source.title or "")[:200],
                })

        await session.commit()
    return OnboardingStats(
        scanned=len(rows),
        created=created,
        linked=linked,
        auto_accepted=auto_accepted,
        pending_review=pending_review,
    )
