"""Link summaries only through an unambiguous provider identity, never a name."""

from sqlalchemy import text

SUMMARY_LINK_CANDIDATES_SQL = """
SELECT s.place_id AS summary_place_id, c.place_id AS canonical_place_id,
       c.city, c.amap_poi_id
FROM travel_place_summary s
JOIN travel_canonical_place c ON c.city = s.city AND c.amap_poi_id = s.amap_poi_id
JOIN travel_place source ON source.id = s.place_id AND source.merged_into_place_id IS NULL
WHERE s.canonical_place_id IS NULL AND c.is_active
  AND NULLIF(c.amap_poi_id, '') IS NOT NULL
  AND (CAST(:city AS text) IS NULL OR c.city = :city)
  AND (SELECT COUNT(*) FROM travel_canonical_place p
       WHERE p.city = c.city AND p.amap_poi_id = c.amap_poi_id) = 1
  AND (SELECT COUNT(*) FROM travel_place_summary other
       WHERE other.city = s.city AND other.amap_poi_id = s.amap_poi_id) = 1
  AND NOT EXISTS (SELECT 1 FROM travel_place_summary linked
                  WHERE linked.canonical_place_id = c.place_id)
"""


async def link_summary_identities(session, city: str | None = None) -> int:
    """Run inside the caller's ingestion transaction; ambiguous rows stay unlinked."""
    result = await session.execute(text(f"""
        WITH candidates AS ({SUMMARY_LINK_CANDIDATES_SQL})
        UPDATE travel_place_summary s
        SET canonical_place_id = c.canonical_place_id
        FROM candidates c
        WHERE s.place_id = c.summary_place_id AND s.canonical_place_id IS NULL
    """), {"city": city})
    from src.pipeline.place_merge import materialize_alias_intents
    await materialize_alias_intents(session)
    return max(result.rowcount or 0, 0)
