"""D1 exact-provider identity operations. The caller owns the session/commit.

Locks: provider advisory lock, sorted source rows, dependent rows. No name-based
merge decision lives here. Snapshots contain full rows; rollback fails closed
on association conflicts. Historical plans and route caches are never written.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict

from sqlalchemy import text

logger = logging.getLogger(__name__)
KIND = "same_city_amap"
SOURCE_CHILDREN = {
    "travel_content_place_mention": "id",
    "travel_place_fact": "id",
    "travel_place_source_map": "id",
}
CANONICAL_CHILDREN = {
    "travel_place_alias": ("alias_id", "canonical_place_id"),
    "travel_canonical_place_source": ("source_id", "canonical_place_id"),
    "travel_place_demand": ("id", "matched_place_id"),
    "travel_place_fact_draft": ("id", "place_id"),
    "travel_canonical_place_review_event": ("event_id", "canonical_place_id"),
    "travel_canonical_place_image": ("image_id", "place_id"),
}
RESTORE_KEYS = {
    **SOURCE_CHILDREN,
    **{t: p[0] for t, p in CANONICAL_CHILDREN.items()},
    "travel_place": "id", "travel_place_summary": "place_id",
    "travel_canonical_place": "place_id",
}


class MergeConflict(RuntimeError):
    """An identity, review, or inverse constraint requires Human review."""


class SaveTransactionRestart(MergeConflict):
    """Lookup raced with identity mutation; caller must release all locks first.

    Only parsed-data save owners may retry this signal, at most once, in a new
    transaction. Never acquire a new provider/row lock from this transaction.
    Other MergeConflict failures remain non-retryable.
    """


def json_text(value):
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


def fingerprint(value) -> str:
    return hashlib.sha256(json_text(value).encode()).hexdigest()


async def confirm_database(session, target_confirm: str) -> str:
    actual = (await session.execute(text("SELECT current_database()"))).scalar_one()
    if not target_confirm or actual != target_confirm:
        raise MergeConflict(f"target-confirm mismatch: current_database={actual}")
    return actual


async def lock_identity(session, city: str, amap_poi_id: str) -> None:
    if not city or not amap_poi_id:
        raise MergeConflict("nonempty city and exact Amap id required")
    # Length-delimited encoding avoids city/id concatenation ambiguities.
    key = json_text([city, amap_poi_id])
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key})


async def lock_places(session, ids: list[int]) -> list[dict]:
    rows = (await session.execute(text("""
        SELECT * FROM travel_place WHERE id = ANY(CAST(:ids AS bigint[]))
        ORDER BY id FOR UPDATE
    """), {"ids": sorted(set(ids))})).mappings().all()
    return [dict(r) for r in rows]


async def resolve_place_id(session, *, city: str, normalized_name: str) -> int | None:
    """Resolve and hold source/survivor locks until all child writes finish.

    Initial reads do not lock. Refresh identity after the provider lock, before
    building the sorted row-lock set: a merge may commit while we wait for the
    advisory. Recheck the locked rows before writing any children.
    The resolved old id is retained in session.info for inverse provenance.
    """
    identity_query = text("""
        SELECT p.id, p.merged_into_place_id, COALESCE(p.amap_poi_id, target.amap_poi_id) AS amap
        FROM travel_place p LEFT JOIN travel_place target ON target.id = p.merged_into_place_id
        WHERE p.city = :city AND p.normalized_name = :name
    """)
    params = {"city": city, "name": normalized_name}
    row = (await session.execute(identity_query, params)).mappings().first()
    if row is None:
        return None
    if row["amap"]:
        await lock_identity(session, city, row["amap"])
        # READ COMMITTED now sees the completed merge (or inverse), including
        # its current one-hop target. Never add that target after locking the
        # stale source alone: it may have a smaller id and violate lock order.
        fresh = (await session.execute(identity_query, params)).mappings().first()
        if fresh is None or fresh["id"] != row["id"] or fresh["amap"] != row["amap"]:
            raise SaveTransactionRestart("provider identity changed during lookup; retry transaction")
        row = fresh
    ids = [row["id"]] + ([row["merged_into_place_id"]] if row["merged_into_place_id"] else [])
    locked = {r["id"]: r for r in await lock_places(session, ids)}
    source = locked[row["id"]]
    target_id = source["merged_into_place_id"] or source["id"]
    if target_id not in locked:
        # Fail closed on an identity change not serialized by our provider
        # lock, rather than acquire an out-of-order row lock.
        raise SaveTransactionRestart("source identity changed during lookup; retry transaction")
    target = locked[target_id]
    if target["city"] != city or target["merged_into_place_id"] is not None:
        raise MergeConflict("invalid cross-city/chained tombstone")
    if target["amap_poi_id"] != row["amap"]:
        # In particular NULL -> provider can occur while waiting for a row
        # lock, even without a fold. We do not own its advisory lock, and may
        # already hold other source rows from this save. Release the entire
        # caller transaction before looking up the new identity/lock set.
        raise SaveTransactionRestart("provider identity changed during row wait; retry transaction")
    session.info.setdefault("d1_source_ids", {})[target_id] = source["id"]
    return int(target_id)


async def record_redirected_children(session, *, place_id: int, children: dict[str, list[int]]) -> None:
    original = session.info.get("d1_source_ids", {}).get(place_id, place_id)
    if original == place_id:
        return
    entries = [{"table": table, "id": child, "source_place_id": original}
               for table, ids in children.items() for child in ids]
    if any(e["table"] not in SOURCE_CHILDREN for e in entries):
        raise ValueError("unsupported redirected child table")
    result = await session.execute(text("""
        UPDATE travel_place_merge_log
        SET redirected_children = redirected_children || CAST(:entries AS jsonb), updated_at = NOW()
        WHERE loser_id = :original AND status = 'applied'
    """), {"entries": json_text(entries), "original": original})
    if result.rowcount != 1:
        raise MergeConflict("redirect has no single applied provenance log")


async def upsert_source_place(session, *, city, name, normalized_name, place_type) -> int:
    resolved = await resolve_place_id(session, city=city, normalized_name=normalized_name)
    if resolved is not None:
        return resolved
    await session.execute(text("""
        INSERT INTO travel_place(city,name,normalized_name,place_type)
        VALUES (:city,:name,:normalized,:type)
        ON CONFLICT(city,normalized_name) DO NOTHING
    """), {"city": city, "name": name, "normalized": normalized_name, "type": place_type})
    resolved = await resolve_place_id(session, city=city, normalized_name=normalized_name)
    if resolved is None:
        raise MergeConflict("source upsert did not produce a visible row")
    return resolved


async def rows_json(session, table: str, where: str, params: dict) -> list[dict]:
    if table not in RESTORE_KEYS:
        raise ValueError("snapshot table not authorized")
    pk = RESTORE_KEYS[table]
    return list((await session.execute(text(
        f"SELECT to_jsonb(t) FROM {table} t WHERE {where} ORDER BY {pk} FOR UPDATE"
    ), params)).scalars())


def planner_visible(row: dict) -> bool:
    return bool(row["is_active"] and row["trust_level"] == "trusted"
                and row["review_status"] in {"reviewed", "auto_accepted"}
                and not row["contextual_only"] and row["geo_status"] in {"resolved", "coordinate_only"}
                and row["latitude"] is not None and row["longitude"] is not None
                and row["canonical_name"] != row["city"])


async def group_snapshot(session, ids: list[int]) -> dict:
    params = {"ids": ids}
    snapshot = {"travel_place": await rows_json(session, "travel_place", "id = ANY(CAST(:ids AS bigint[]))", params)}
    snapshot["travel_place_summary"] = await rows_json(session, "travel_place_summary", "place_id = ANY(CAST(:ids AS bigint[]))", params)
    for table in SOURCE_CHILDREN:
        snapshot[table] = await rows_json(session, table, "place_id = ANY(CAST(:ids AS bigint[]))", params)
    cids = sorted({r["canonical_place_id"] for r in snapshot["travel_place_summary"] if r["canonical_place_id"]})
    params = {"ids": cids}
    snapshot["travel_canonical_place"] = await rows_json(session, "travel_canonical_place", "place_id = ANY(CAST(:ids AS bigint[]))", params)
    for table, (_, fk) in CANONICAL_CHILDREN.items():
        snapshot[table] = await rows_json(session, table, f"{fk} = ANY(CAST(:ids AS bigint[]))", params)
    return snapshot


async def materialize_alias_intents(session, survivor_id: int | None = None) -> int:
    """No canonical id invention; candidate aliases appear only after valid linking."""
    params = {"survivor": survivor_id}
    logs = (await session.execute(text("""
        SELECT l.id, l.alias_intents, l.materialized_alias_ids, s.canonical_place_id
        FROM travel_place_merge_log l
        JOIN travel_place_summary s ON s.place_id = l.survivor_id
        JOIN travel_canonical_place c ON c.place_id = s.canonical_place_id
        JOIN travel_place p ON p.id = s.place_id
        WHERE l.status = 'applied' AND c.is_active AND p.merged_into_place_id IS NULL
          AND (CAST(:survivor AS bigint) IS NULL OR l.survivor_id = :survivor)
        ORDER BY l.id FOR UPDATE OF l
    """), params)).mappings().all()
    count = 0
    for log in logs:
        created = list(log["materialized_alias_ids"])
        for intent in log["alias_intents"]:
            alias = (await session.execute(text("""
                INSERT INTO travel_place_alias
                    (canonical_place_id, alias_name, normalized_alias, relation_type, status, source, note)
                VALUES (:cid, :name, :normalized, 'alias_same', 'candidate', 'xhs', :note)
                ON CONFLICT (canonical_place_id, normalized_alias) DO NOTHING RETURNING alias_id
            """), {"cid": log["canonical_place_id"], "name": intent["name"],
                   "normalized": intent["normalized_name"], "note": f"D1 merge log {log['id']}"})).scalar()
            if alias is not None:
                created.append(alias)
                count += 1
        if created != log["materialized_alias_ids"]:
            await session.execute(text("UPDATE travel_place_merge_log SET materialized_alias_ids=CAST(:ids AS jsonb) WHERE id=:id"),
                                  {"ids": json_text(created), "id": log["id"]})
    return count


async def _persist_pending(session, *, city, amap, survivor, loser, batch_id, reason):
    await session.execute(text("""
        INSERT INTO travel_place_merge_log
            (batch_id,merge_kind,city,amap_poi_id,survivor_id,loser_id,status,reason,alias_intents)
        VALUES (:batch,:kind,:city,:amap,:survivor,:loser,'pending_review',:reason,CAST(:aliases AS jsonb))
        ON CONFLICT (merge_kind,loser_id) WHERE status='pending_review'
        DO UPDATE SET survivor_id=EXCLUDED.survivor_id, city=EXCLUDED.city,
                      amap_poi_id=EXCLUDED.amap_poi_id, reason=EXCLUDED.reason,
                      alias_intents=EXCLUDED.alias_intents, updated_at=NOW()
    """), {"batch": batch_id, "kind": KIND, "city": city, "amap": amap,
           "survivor": survivor, "loser": loser["id"], "reason": reason,
           "aliases": json_text([{"source_place_id": loser["id"], "name": loser["name"], "normalized_name": loser["normalized_name"]}])})


async def _move_source_children(session, snapshot, survivor, loser):
    moved, deleted = {}, {}
    published = set((await session.execute(text("SELECT published_fact_id FROM travel_place_fact_draft WHERE published_fact_id IS NOT NULL"))).scalars())
    for table, pk in SOURCE_CHILDREN.items():
        rows = snapshot[table]
        groups = defaultdict(list)
        for row in rows:
            if table == "travel_place_fact":
                key = (row["source_platform"], row["source_id"], row["fact_type"])
                if row["source_platform"] == "manual" or row[pk] in published or row["source_id"] is None:
                    key = ("protected", row[pk])
            elif table == "travel_content_place_mention":
                # Different content copies of the same note are still one sample.
                identity = (await session.execute(text("SELECT source_platform,source_id FROM travel_content WHERE id=:id"), {"id": row["content_id"]})).first()
                key = tuple(identity) if identity.source_id else ("content", row["content_id"])
            else:
                key = (row["source_platform"], row["source_id"])
            groups[key].append(row)
        removed = []
        for group in groups.values():
            if not any(r["place_id"] == loser for r in group):
                continue
            if table == "travel_content_place_mention":
                rank = lambda r: (sum(len(str(r.get(k) or "")) for k in ("mention_context", "place_name_text", "tags")), float(r["confidence"]), -r[pk])
            else:
                rank = lambda r: (float(r["confidence"]), -r[pk])
            keep = max(group, key=rank)
            removed.extend(r for r in group if r[pk] != keep[pk])
        for row in removed:
            await session.execute(text(f"DELETE FROM {table} WHERE {pk}=:id"), {"id": row[pk]})
        ids = [r[pk] for r in rows if r["place_id"] == loser and r not in removed]
        if ids:
            await session.execute(text(f"UPDATE {table} SET place_id=:survivor WHERE {pk}=ANY(CAST(:ids AS bigint[]))"), {"survivor": survivor, "ids": ids})
        moved[table], deleted[table] = ids, removed
    return moved, deleted


async def merge_source_places(session, *, city: str, amap_poi_id: str,
                              survivor_id: int, loser_ids: list[int], batch_id: str,
                              reviewed: bool = False, expected_fingerprint: str | None = None) -> dict:
    """Shared auto/window merge. Call inside a caller-owned transaction/savepoint.

    Reviewed CLI groups require a full snapshot fingerprint. Automatic groups
    cannot fold canonical-linked losers. A reverted/do_not_merge member blocks
    the entire group, including any different choice of survivor.
    """
    ids = sorted(set([survivor_id, *loser_ids]))
    if len(ids) < 2 or survivor_id in loser_ids:
        raise MergeConflict("distinct live group required")
    await lock_identity(session, city, amap_poi_id)
    rows = await lock_places(session, ids)
    if len(rows) != len(ids) or any(r["city"] != city or r["amap_poi_id"] != amap_poi_id
                                  or r["poi_resolution_status"] != "resolved"
                                  or r["merged_into_place_id"] is not None for r in rows):
        return {"applied": 0, "skipped": "identity_drift"}
    blocked = (await session.execute(text("""
        SELECT 1 FROM travel_place_merge_log WHERE city=:city AND amap_poi_id=:amap
          AND status IN ('reverted','do_not_merge')
          AND (loser_id=ANY(CAST(:ids AS bigint[])) OR survivor_id=ANY(CAST(:ids AS bigint[]))) LIMIT 1
    """), {"city": city, "amap": amap_poi_id, "ids": ids})).scalar()
    if blocked:
        return {"applied": 0, "skipped": "do_not_merge_or_reverted"}
    snapshot = await group_snapshot(session, ids)
    if expected_fingerprint is not None and fingerprint(snapshot) != expected_fingerprint:
        return {"applied": 0, "skipped": "snapshot_drift"}
    if reviewed and expected_fingerprint is None:
        raise MergeConflict("reviewed merge requires plan snapshot fingerprint")
    if any(c["city"] != city or c["amap_poi_id"] != amap_poi_id for c in snapshot["travel_canonical_place"]):
        return {"applied": 0, "skipped": "held_out_canonical_identity"}
    linked_losers = {s["place_id"] for s in snapshot["travel_place_summary"]
                     if s["canonical_place_id"] is not None and s["place_id"] in loser_ids}
    if not reviewed and linked_losers:
        for loser in rows:
            if loser["id"] != survivor_id:
                await _persist_pending(session, city=city, amap=amap_poi_id, survivor=survivor_id,
                                       loser=loser, batch_id=batch_id, reason="canonical_linked_requires_review")
        return {"applied": 0, "pending_review": len(loser_ids)}
    # One inverse log per loser, applied sequentially inside the group's transaction.
    logs = []
    for loser_id in sorted(loser_ids):
        before = await group_snapshot(session, [survivor_id, loser_id])
        loser = next(r for r in before["travel_place"] if r["id"] == loser_id)
        compressed = await rows_json(session, "travel_place", "merged_into_place_id=:id", {"id": loser_id})
        # Retarget old applied logs alongside one-hop tombstones. L2 restores both.
        old_logs = list((await session.execute(text("SELECT to_jsonb(l) FROM travel_place_merge_log l WHERE survivor_id=:id AND status='applied' ORDER BY id FOR UPDATE"), {"id": loser_id})).scalars())
        before["compressed_tombstones"] = compressed
        before["compressed_logs"] = old_logs
        moved, deleted = await _move_source_children(session, before, survivor_id, loser_id)
        await _merge_canonical(session, before, survivor_id, loser_id, amap_poi_id, deleted)
        await session.execute(text("""
            UPDATE travel_place SET merged_into_place_id=:survivor, amap_poi_id=NULL,
                poi_resolution_status='unresolvable', poi_resolution_last_error=:error
            WHERE id=:loser AND merged_into_place_id IS NULL
        """), {"survivor": survivor_id, "loser": loser_id, "error": f"merged_into:{survivor_id}"})
        await session.execute(text("UPDATE travel_place SET merged_into_place_id=:survivor, poi_resolution_last_error=:error WHERE merged_into_place_id=:loser"),
                              {"survivor": survivor_id, "loser": loser_id, "error": f"merged_into:{survivor_id}"})
        await session.execute(text("UPDATE travel_place_merge_log SET survivor_id=:survivor WHERE survivor_id=:loser AND status='applied'"), {"survivor": survivor_id, "loser": loser_id})
        aliases = [{"source_place_id": loser_id, "name": loser["name"], "normalized_name": loser["normalized_name"]}]
        aliases += [{"name": c["canonical_name"], "normalized_name": c["canonical_name"], "canonical_place_id": c["place_id"]}
                    for c in before["travel_canonical_place"]]
        pending = (await session.execute(text("SELECT * FROM travel_place_merge_log WHERE loser_id=:id AND merge_kind=:kind AND status='pending_review' FOR UPDATE"), {"id": loser_id, "kind": KIND})).mappings().first()
        if pending:
            # The reviewed snapshot is authoritative. Preserve the stale proposal
            # for audit, then atomically bind every identity field to this merge.
            before["pending_review_intent"] = dict(pending)
        params = {"batch": batch_id, "kind": KIND, "city": city, "amap": amap_poi_id,
                  "survivor": survivor_id, "loser": loser_id, "before": json_text(before),
                  "moved": json_text(moved), "deleted": json_text(deleted), "aliases": json_text(aliases)}
        if pending:
            params["id"] = pending["id"]
            params["reason"] = ("reviewed_plan_retargeted" if
                (pending["survivor_id"], pending["city"], pending["amap_poi_id"]) !=
                (survivor_id, city, amap_poi_id) else "reviewed_plan")
            log_id = (await session.execute(text("""
                UPDATE travel_place_merge_log SET batch_id=:batch,status='applied',reason=:reason,
                    survivor_id=:survivor,city=:city,amap_poi_id=:amap,
                    before_snapshot=CAST(:before AS jsonb),moved_rows=CAST(:moved AS jsonb),
                    deleted_rows=CAST(:deleted AS jsonb),alias_intents=CAST(:aliases AS jsonb),
                    redirected_children='[]'::jsonb,materialized_alias_ids='[]'::jsonb,
                    evidence_rewash_required=TRUE,applied_at=clock_timestamp(),updated_at=NOW() WHERE id=:id RETURNING id
            """), params)).scalar_one()
        else:
            log_id = (await session.execute(text("""
                INSERT INTO travel_place_merge_log
                    (batch_id,merge_kind,city,amap_poi_id,survivor_id,loser_id,status,reason,
                     before_snapshot,moved_rows,deleted_rows,alias_intents,evidence_rewash_required,applied_at)
                VALUES (:batch,:kind,:city,:amap,:survivor,:loser,'applied','exact_identity',
                        CAST(:before AS jsonb),CAST(:moved AS jsonb),CAST(:deleted AS jsonb),
                        CAST(:aliases AS jsonb),TRUE,clock_timestamp()) RETURNING id
            """), params)).scalar_one()
        logs.append(log_id)
    await materialize_alias_intents(session, survivor_id)
    return {"applied": len(logs), "log_ids": logs}


async def _merge_canonical(session, before, survivor, loser, amap, deleted):
    canonicals = before["travel_canonical_place"]
    target = None
    if canonicals:
        counts = defaultdict(int)
        for r in before["travel_content_place_mention"] + before["travel_place_fact"]:
            counts[r["place_id"]] += 1
        source_for = {r["canonical_place_id"]: r["place_id"] for r in before["travel_place_summary"]}
        images = {r["place_id"] for r in before["travel_canonical_place_image"]}
        target = max(canonicals, key=lambda c: (planner_visible(c), c["amap_poi_id"] == amap,
                     counts[source_for.get(c["place_id"])] , c["typical_visit_source"] == "manual" or c["place_id"] in images, -c["place_id"]))
        cid = target["place_id"]
        for c in canonicals:
            if c["place_id"] == cid:
                continue
            old = c["place_id"]
            # Clear unique Amap ownership before any canonical reassociation.
            await session.execute(text("UPDATE travel_canonical_place SET is_active=FALSE,review_status='rejected',amap_poi_id=NULL WHERE place_id=:id"), {"id": old})
            for table, (pk, fk) in CANONICAL_CHILDREN.items():
                members = [r for r in before[table] if r[fk] == old]
                if table == "travel_canonical_place_image":
                    occupied = [r for r in before[table] if r[fk] == cid]
                    if any(r["active"] for r in occupied):
                        continue
                    positions = {r["position"] for r in occupied}
                    assets = {r["asset_id"] for r in occupied}
                    for r in members:
                        if r["asset_id"] in assets:
                            continue
                        free = next((p for p in range(1, 7) if p not in positions), None)
                        if free is None:
                            raise MergeConflict("image positions exhausted; group requires review")
                        await session.execute(text(f"UPDATE {table} SET {fk}=:cid,position=:position WHERE {pk}=:id"), {"cid": cid, "position": free, "id": r[pk]})
                        positions.add(free)
                        assets.add(r["asset_id"])
                    continue
                for r in members:
                    collision = None
                    if table == "travel_place_alias":
                        collision = (await session.execute(text("SELECT alias_id FROM travel_place_alias WHERE canonical_place_id=:cid AND normalized_alias=:name"), {"cid": cid, "name": r["normalized_alias"]})).scalar()
                    elif table == "travel_canonical_place_source":
                        collision = (await session.execute(text("""SELECT source_id FROM travel_canonical_place_source
                            WHERE canonical_place_id=:cid AND source_type=:type AND source_name=:name
                            AND COALESCE(source_url,'')=:url AND COALESCE(source_place_name,'')=:place
                            AND COALESCE(source_rank,-1)=:rank"""), {"cid": cid, "type": r["source_type"], "name": r["source_name"], "url": r["source_url"] or "", "place": r["source_place_name"] or "", "rank": r["source_rank"] if r["source_rank"] is not None else -1})).scalar()
                    if collision:
                        deleted.setdefault(table, []).append(r)
                        await session.execute(text(f"DELETE FROM {table} WHERE {pk}=:id"), {"id": r[pk]})
                    else:
                        await session.execute(text(f"UPDATE {table} SET {fk}=:cid WHERE {pk}=:id"), {"cid": cid, "id": r[pk]})
            if target["typical_visit_minutes"] is None and c["typical_visit_source"] == "manual":
                await session.execute(text("""UPDATE travel_canonical_place SET typical_visit_minutes=:minutes,
                    typical_visit_source='manual',typical_visit_confidence=:confidence,
                    typical_visit_updated_at=:stamp WHERE place_id=:id"""),
                    {"minutes": c["typical_visit_minutes"], "confidence": c["typical_visit_confidence"], "stamp": __import__('datetime').datetime.fromisoformat(c["typical_visit_updated_at"]) if c["typical_visit_updated_at"] else None, "id": cid})
                target = {**target, "typical_visit_minutes": c["typical_visit_minutes"]}
    # The loser summary must release uq_summary_canonical before survivor linking.
    await session.execute(text("DELETE FROM travel_place_summary WHERE place_id=:id"), {"id": loser})
    survivor_summary = next((r for r in before["travel_place_summary"] if r["place_id"] == survivor), None)
    loser_summary = next((r for r in before["travel_place_summary"] if r["place_id"] == loser), None)
    if survivor_summary is None and loser_summary:
        # Preserve evidence; change only source identity to the actual live survivor.
        source = next(r for r in before["travel_place"] if r["id"] == survivor)
        row = {**loser_summary, "place_id": survivor, "canonical_place_id": None,
               "name": source["name"], "city": source["city"]}
        await restore_row(session, "travel_place_summary", row)
    if target:
        await session.execute(text("UPDATE travel_place_summary SET canonical_place_id=:cid WHERE place_id=:id"),
                              {"cid": target["place_id"] if planner_visible(target) else None, "id": survivor})


async def restore_row(session, table: str, row: dict) -> None:
    if table not in RESTORE_KEYS:
        raise ValueError("inverse table not authorized")
    pk = RESTORE_KEYS[table]
    # Column names come from PostgreSQL snapshots, validated against actual schema.
    columns = list((await session.execute(text("""SELECT column_name FROM information_schema.columns
        WHERE table_schema=current_schema() AND table_name=:table ORDER BY ordinal_position"""), {"table": table})).scalars())
    if set(row) != set(columns):
        raise MergeConflict(f"schema drift prevents inverse: {table}")
    quoted = ','.join(f'"{c}"' for c in columns)
    updates = ','.join(f'"{c}"=EXCLUDED."{c}"' for c in columns if c != pk)
    await session.execute(text(f"""INSERT INTO {table} ({quoted})
        SELECT {quoted} FROM jsonb_populate_record(NULL::{table}, CAST(:row AS jsonb))
        ON CONFLICT ({pk}) DO UPDATE SET {updates}"""), {"row": json_text(row)})


async def rollback_merge(session, log_id: int) -> dict:
    """L2 inverse; callers wrap each complete group in a transaction/savepoint."""
    peek = (await session.execute(text("SELECT * FROM travel_place_merge_log WHERE id=:id"), {"id": log_id})).mappings().one()
    await lock_identity(session, peek["city"], peek["amap_poi_id"])
    await lock_places(session, [peek["survivor_id"], peek["loser_id"]])
    log = (await session.execute(text("SELECT * FROM travel_place_merge_log WHERE id=:id FOR UPDATE"), {"id": log_id})).mappings().one()
    if log["status"] != "applied":
        return {"reverted": 0}
    # Log ids need not reflect apply order: a pending intent can be older than
    # another applied loser. Applied time, then id, defines the inverse order.
    dependent = (await session.execute(text("""
        SELECT id FROM travel_place_merge_log
        WHERE status='applied' AND id<>:id
          AND (applied_at,id) > (:applied_at,:id)
          AND ((city=:city AND amap_poi_id=:amap)
               OR survivor_id IN (:survivor,:loser) OR loser_id IN (:survivor,:loser))
        ORDER BY applied_at DESC,id DESC LIMIT 1 FOR UPDATE
    """), {"id": log_id, "applied_at": log["applied_at"], "city": log["city"],
           "amap": log["amap_poi_id"], "survivor": log["survivor_id"], "loser": log["loser_id"]})).scalar()
    if dependent is not None:
        raise MergeConflict(f"rollback later dependent merge first: {dependent}")
    survivor, loser = log["survivor_id"], log["loser_id"]
    before = log["before_snapshot"]
    # Any later merge of either endpoint must be undone first.
    state = (await session.execute(text("SELECT merged_into_place_id FROM travel_place WHERE id=:id"), {"id": survivor})).scalar()
    original_survivor = next(r for r in before["travel_place"] if r["id"] != loser)["id"]
    if state is not None or survivor != original_survivor:
        raise MergeConflict("rollback later merge first")
    cids = [r["place_id"] for r in before["travel_canonical_place"]]
    occupied = (await session.execute(text("""SELECT place_id FROM travel_place_summary
        WHERE canonical_place_id=ANY(CAST(:ids AS bigint[])) AND place_id<>ALL(CAST(:sources AS bigint[]))
        FOR UPDATE"""), {"ids": cids, "sources": [survivor, loser]})).all()
    if occupied:
        raise MergeConflict("original canonical link occupied; inverse aborted")
    # Fixed order: unlink survivor before restoring loser/canonical unique keys.
    await session.execute(text("DELETE FROM travel_place_summary WHERE place_id=ANY(CAST(:ids AS bigint[]))"), {"ids": [survivor, loser]})
    for alias in log["materialized_alias_ids"]:
        await session.execute(text("DELETE FROM travel_place_alias WHERE alias_id=:id AND note=:note"), {"id": alias, "note": f"D1 merge log {log_id}"})
    await session.execute(text("UPDATE travel_canonical_place SET amap_poi_id=NULL WHERE place_id=ANY(CAST(:ids AS bigint[]))"), {"ids": cids})
    for table in ["travel_place", "travel_canonical_place", *SOURCE_CHILDREN, *CANONICAL_CHILDREN, "travel_place_summary"]:
        for row in before[table]:
            await restore_row(session, table, row)
    for entry in log["redirected_children"]:
        table = entry["table"]
        if table not in SOURCE_CHILDREN:
            raise MergeConflict("unrecognized inverse provenance")
        result = await session.execute(text(f"UPDATE {table} SET place_id=:loser WHERE id=:id AND place_id=:survivor"), {"id": entry["id"], "loser": loser, "survivor": survivor})
        if result.rowcount != 1:
            raise MergeConflict("redirected child missing or reassociated; inverse aborted")
    for row in before.get("compressed_tombstones", []):
        await restore_row(session, "travel_place", row)
    for old in before.get("compressed_logs", []):
        # Writes since the compression stay in their original log. Return them
        # to the restored intermediate survivor, retaining original-name provenance.
        current = (await session.execute(text("SELECT redirected_children FROM travel_place_merge_log WHERE id=:id FOR UPDATE"), {"id": old["id"]})).scalar_one()
        for entry in current:
            if entry["table"] not in SOURCE_CHILDREN:
                raise MergeConflict("invalid compressed provenance")
            await session.execute(text(f"UPDATE {entry['table']} SET place_id=:loser WHERE id=:id AND place_id=:survivor"), {"id": entry["id"], "loser": loser, "survivor": survivor})
        await session.execute(text("UPDATE travel_place_merge_log SET survivor_id=:loser WHERE id=:id"), {"loser": loser, "id": old["id"]})
    await session.execute(text("UPDATE travel_place_merge_log SET status='reverted',reverted_at=NOW(),evidence_rewash_required=FALSE WHERE id=:id"), {"id": log_id})
    return {"reverted": 1}


async def merge_health(session) -> dict:
    return dict((await session.execute(text("""
        SELECT (SELECT count(*) FROM travel_place WHERE merged_into_place_id IS NOT NULL) AS tombstones,
          (SELECT count(*) FROM travel_content_place_mention m JOIN travel_place p ON p.id=m.place_id
           WHERE p.merged_into_place_id IS NOT NULL) AS mentions_on_tombstones,
          (SELECT count(*) FROM travel_place_fact f JOIN travel_place p ON p.id=f.place_id
           WHERE p.merged_into_place_id IS NOT NULL) AS facts_on_tombstones,
          (SELECT count(*) FROM travel_place_source_map m JOIN travel_place p ON p.id=m.place_id
           WHERE p.merged_into_place_id IS NOT NULL) AS maps_on_tombstones,
          (SELECT count(*) FROM travel_place_summary s JOIN travel_place p ON p.id=s.place_id
           WHERE p.merged_into_place_id IS NOT NULL) AS summaries_on_tombstones,
          (SELECT count(*) FROM travel_place p JOIN travel_place t ON t.id=p.merged_into_place_id
           WHERE t.merged_into_place_id IS NOT NULL OR t.city<>p.city) AS invalid_chains,
          (SELECT count(*) FROM travel_place_summary s JOIN travel_canonical_place c ON c.place_id=s.canonical_place_id
           WHERE NOT c.is_active) AS inactive_canonical_summaries
    """))).mappings().one())
