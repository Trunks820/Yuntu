"""Validation shared by backend artifact renderers."""

from __future__ import annotations

from typing import Any

from src.jobs.generation_delivery import GenerationDelivery, validate_delivery_for_job


SUPPORTED_RESULT_SCHEMA_VERSIONS = frozenset({"2.0", "2.1", "2.2", "2.3"})


def validate_export_result_contract(result: dict[str, Any]) -> None:
    """Reject unsupported schemas and contradictory v0.12 delivery metadata."""
    schema_version = result.get("schema_version")
    if schema_version not in SUPPORTED_RESULT_SCHEMA_VERSIONS:
        raise ValueError("unsupported result schema")
    if schema_version != "2.3":
        return

    result_id = result.get("result_id")
    if (
        result.get("generation_policy_version") != "v0.12"
        or isinstance(result_id, bool)
        or not isinstance(result_id, int)
        or result_id <= 0
    ):
        raise ValueError("invalid schema 2.3 result identity")

    delivery = GenerationDelivery.model_validate(result.get("delivery"))
    validate_delivery_for_job(
        delivery,
        status="SUCCESS",
        result_type="PLAN_READY",
        result_record_id=result_id,
        published_variant=result.get("published_variant"),
        delivery_status=result.get("delivery_status"),
    )
