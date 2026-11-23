"""Export source builder for v0.8.11.1 backend artifacts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from src.api import trip_results

SHARE_IMAGE_EXPORT_VERSION = "v0.15.4-share-long-1"
PDF_EXPORT_VERSION = "v0.15.4-pdf-book-1"
# Keep the legacy PDF version inside the canonical payload for Schemas 2.1/2.2.
EXPORT_VERSION = "v0.9.9.9-pdf-light-routebook-layout-3"


@dataclass(frozen=True)
class ExportSource:
    export_version: str
    source_hash: str
    canonical_json: str
    source: dict[str, Any]


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _source_hash(payload: dict[str, Any]) -> tuple[str, str]:
    canonical = _canonical_json(payload)
    return canonical, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _day_source(day: dict[str, Any]) -> dict[str, Any]:
    return {
        "day": day["day"],
        "title": day["title"],
        "narrative": day.get("narrative", ""),
        "places": day.get("places", []),
        "commute_summary": day.get("commute_summary", ""),
        "commute_legs": day.get("commute_legs", []),
        "pace_status": day.get("pace_status"),
    }


def _plan_source(
    plan: dict[str, Any], *, include_advice: bool, include_travel_details: bool
) -> dict[str, Any]:
    source = {
        "plan_id": plan["plan_id"],
        "title": plan["title"],
        "summary": plan["summary"],
        "tags": plan.get("tags", []),
        "pace": plan.get("pace"),
        "days": [_day_source(day) for day in plan.get("days", [])],
        "cost_estimate": plan["cost_estimate"],
    }
    if include_travel_details:
        for field in ("accommodation", "transport"):
            if field in plan:
                source[field] = plan[field]
    if include_advice:
        if plan.get("opening"):
            source["opening"] = plan["opening"]
        packing = []
        for item in plan.get("packing_checklist") or []:
            if not isinstance(item, dict):
                continue
            category = item.get("category")
            values = [value for value in item.get("items") or [] if isinstance(value, str) and value.strip()]
            if isinstance(category, str) and category.strip() and values:
                packing.append({"category": category, "items": values})
        tips = []
        for item in plan.get("travel_tips") or []:
            if not isinstance(item, dict):
                continue
            title, content = item.get("title"), item.get("content")
            if all(isinstance(value, str) and value.strip() for value in (title, content)):
                tips.append({"title": title, "content": content})
        if packing:
            source["packing_checklist"] = packing
        if tips:
            source["travel_tips"] = tips
    return source


def build_export_source_from_trip_result(
    result: trip_results.TripResultResponse,
    artifact_type: str = "pdf",
) -> ExportSource:
    if artifact_type not in {"pdf", "share_image"}:
        raise ValueError("unsupported artifact type")
    include_advice = artifact_type == "pdf"
    schema_23 = result.schema_version == "2.3"
    pdf_version = PDF_EXPORT_VERSION if schema_23 else EXPORT_VERSION
    export_version = pdf_version if include_advice else SHARE_IMAGE_EXPORT_VERSION
    projected = result.model_dump(mode="json", exclude_none=True)
    artifact_request = trip_results.artifact_request_fields(result)
    request = dict(projected["request"])
    request.update(artifact_request)
    source = {
        "export_version": export_version,
        "result": {
            "schema_version": projected.get("schema_version"),
            "published_variant": projected["published_variant"],
            "delivery_status": projected["delivery_status"],
            "result_id": projected["result_id"],
            "city": projected["city"],
            "request": request,
            "weather": projected.get("weather"),
            "time_preferences": projected.get("time_preferences"),
            "must_include": projected.get("must_include", []),
            "plans": [
                _plan_source(
                    plan,
                    include_advice=include_advice,
                    include_travel_details=include_advice and schema_23,
                )
                for plan in projected.get("plans", [])
            ],
        },
    }
    if schema_23:
        source["result"]["generation_policy_version"] = projected[
            "generation_policy_version"
        ]
        if result.delivery is None:
            raise ValueError("schema 2.3 export requires delivery metadata")
        source["result"]["delivery"] = result.delivery.model_dump(mode="json")
    canonical, digest = _source_hash(source)
    return ExportSource(
        export_version=export_version,
        source_hash=digest,
        canonical_json=canonical,
        source=source,
    )


async def build_export_source(
    result_record_id: int,
    artifact_type: str = "pdf",
) -> ExportSource | None:
    result = await trip_results.get_trip_result(result_record_id)
    if result is None:
        return None
    return build_export_source_from_trip_result(result, artifact_type)
