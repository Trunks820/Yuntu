"""Durable Review/Grouping/Selector circuit state and recover operations.

Writer capacity, leases and the dispatch ledger stay in
``writer_relay_store``. This module never exposes or logs URLs, keys,
configuration fingerprints, prompts, user content or raw provider errors.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from src.pipeline.db import get_session_factory
from src.agents.model_failure_policy import LONG_FAILURE_CLASSES, cooldown_seconds

logger = logging.getLogger(__name__)

CONTROLLED_ROLES = ("writer", "review", "grouping", "selector")
POOLED_ROLES = ("review", "grouping", "selector")
ROLES_WITH_CAPACITY = frozenset({"writer"})
COOLDOWN_SECONDS = (120, 120, 120)
TRANSIENT_FAILURE_THRESHOLD = 2
TRANSIENT_FAILURE_CLASSES = frozenset(
    {"TRANSPORT", "TIMEOUT", "HTTP_5XX", "WIRE_INVALID", "UNKNOWN", "POLICY_REJECTION", "MODEL_PERMISSION"}
)
IMMEDIATE_OPEN_FAILURE_CLASSES = frozenset({"RATE_LIMIT", "TRANSPORT", "TIMEOUT", "HTTP_5XX", "UNKNOWN", "POLICY_REJECTION", "MODEL_PERMISSION"}) | LONG_FAILURE_CLASSES
SINGLE_PATH_ORDINARY_FAILURES = (TRANSIENT_FAILURE_CLASSES | IMMEDIATE_OPEN_FAILURE_CLASSES) - LONG_FAILURE_CLASSES
QUARANTINE_FAILURE_CLASSES = frozenset()
DURABLE_ROLES = (*CONTROLLED_ROLES, "intent", "extract", "route_planner")
PROBE_LEASE_SECONDS = 60
ENDPOINT_NAME_MAX = 64


class RecoverError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        http_status: int,
        message: str,
        retry_after: datetime | None = None,
        replayed: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.http_status = http_status
        self.message = message
        self.retry_after = retry_after
        self.replayed = replayed


@dataclass(frozen=True)
class SafeEndpoint:
    role: str
    endpoint_id: str
    display_name: str
    model: str
    participation: str
    qualification: str
    circuit_state: str
    recoverable: bool
    cooldown_until: datetime | None
    inflight: int | None
    max_inflight: int | None
    updated_at: datetime
    provider: str = "unknown"
    failure_domain: str = ""
    availability: str = "UNVERIFIED"
    availability_reason: str = "NO_RUNTIME_EVIDENCE"
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_failure_code: str | None = None
    last_probe_at: datetime | None = None
    last_probe_status: str | None = None
    incident_id: uuid.UUID | None = None
    incident_started_at: datetime | None = None
    incident_resolved_at: datetime | None = None

    def to_api(self) -> dict[str, Any]:
        return {
            "role": "POI_SELECTOR" if self.role == "selector" else self.role.upper(),
            "endpoint_id": self.endpoint_id,
            "display_name": self.display_name,
            "model": self.model,
            "participation": self.participation,
            "qualification": self.qualification,
            "circuit_state": self.circuit_state,
            "recoverable": self.recoverable,
            "cooldown_until": _iso(self.cooldown_until),
            "inflight": self.inflight if self.role in ROLES_WITH_CAPACITY else None,
            "max_inflight": (
                self.max_inflight if self.role in ROLES_WITH_CAPACITY else None
            ),
            "updated_at": _iso(self.updated_at),
            "provider": self.provider,
            "availability": self.availability,
            "availability_reason": self.availability_reason,
            "last_success_at": _iso(self.last_success_at),
            "last_failure_at": _iso(self.last_failure_at),
            "last_failure_code": self.last_failure_code,
            "last_probe_at": _iso(self.last_probe_at),
            "last_probe_status": self.last_probe_status,
            "incident_id": str(self.incident_id) if self.incident_id else None,
            "incident_started_at": _iso(self.incident_started_at),
            "incident_resolved_at": _iso(self.incident_resolved_at),
        }


@dataclass(frozen=True)
class RecoverSuccess:
    endpoint: SafeEndpoint
    replayed: bool = False


@dataclass
class _PooledEndpoint:
    role: str
    name: str
    configured: bool = True
    participation: str = "ACTIVE"
    qualification: str = "QUALIFIED"
    circuit: str = "CLOSED"
    fingerprint: str = ""
    provider: str = "unknown"
    failure_domain: str = ""
    last_failure_code: str | None = None
    last_failure_at: datetime | None = None
    transient_failures: int = 0
    cooldown_level: int = 0
    cooldown_until: datetime | None = None
    manual_retry_level: int = 0
    manual_retry_not_before: datetime | None = None
    probe_kind: str | None = None
    probe_owner: str | None = None
    probe_until: datetime | None = None
    recovery_origin: str | None = None
    active_operation_id: uuid.UUID | None = None
    generation: int = 0
    updated_at: datetime | None = None


@dataclass
class _Operation:
    operation_id: uuid.UUID
    role: str
    endpoint_name: str
    lifecycle: str
    http_status: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    retry_after: datetime | None = None
    replay_json: dict[str, Any] | None = None
    finished_at: datetime | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_role(value: str) -> str:
    role = str(value or "").strip().lower()
    return "selector" if role == "poi_selector" else role


def _safe_error(code: str) -> tuple[int, str]:
    mapping = {
        "LLM_ENDPOINT_NOT_FOUND": (404, "endpoint was not found"),
        "LLM_ENDPOINT_DISABLED": (409, "endpoint is disabled"),
        "LLM_ENDPOINT_NOT_RECOVERABLE": (409, "endpoint is not recoverable"),
        "LLM_ENDPOINT_RECOVERY_IN_PROGRESS": (409, "endpoint recovery is in progress"),
        "LLM_ENDPOINT_BUSY": (409, "endpoint is busy"),
        "LLM_ENDPOINT_OPERATION_CONFLICT": (409, "operation id conflict"),
        "LLM_ENDPOINT_PROBE_FAILED": (502, "endpoint probe failed"),
        "LLM_ENDPOINT_PROBE_TIMEOUT": (504, "endpoint probe timed out"),
        "INTERNAL_ADMIN_UNAVAILABLE": (503, "internal admin is unavailable"),
    }
    return mapping.get(code, (409, "endpoint is not recoverable"))


def _visible_cooldown(row: _PooledEndpoint, now: datetime) -> datetime | None:
    candidates = [
        instant
        for instant in (row.manual_retry_not_before, row.cooldown_until)
        if instant is not None and instant > now
    ]
    if not candidates:
        return None
    return max(candidates)


def _pooled_recoverable(row: _PooledEndpoint, now: datetime, half_open: bool) -> bool:
    if not row.configured or row.participation == "DISABLED":
        return False
    if row.circuit not in {"OPEN", "QUARANTINED"}:
        return False
    if half_open or row.circuit == "HALF_OPEN":
        return False
    if row.manual_retry_not_before and row.manual_retry_not_before > now:
        return False
    return True


def _to_safe_pooled(
    row: _PooledEndpoint,
    *,
    model: str,
    now: datetime,
    half_open: bool,
) -> SafeEndpoint:
    updated = row.updated_at or now
    return SafeEndpoint(
        role=row.role,
        endpoint_id=row.name,
        display_name=row.name,
        model=model,
        participation=row.participation,
        qualification=row.qualification,
        circuit_state=row.circuit,
        recoverable=_pooled_recoverable(row, now, half_open),
        cooldown_until=_visible_cooldown(row, now),
        inflight=None,
        max_inflight=None,
        updated_at=updated,
    )


def _runtime_availability(row: Any) -> tuple[str, str]:
    if not bool(row.get("configured", True)) or str(row.get("participation")) == "DISABLED":
        return "UNAVAILABLE", "DISABLED"
    circuit = str(row.get("circuit_state") or "CLOSED")
    if circuit in {"OPEN", "QUARANTINED", "HALF_OPEN"}:
        return "UNAVAILABLE", "CIRCUIT_OPEN"
    if str(row.get("qualification_state") or "UNQUALIFIED") != "QUALIFIED":
        return "UNVERIFIED", "QUALIFICATION_REQUIRED"
    success = row.get("last_success_at")
    failure = row.get("last_failure_at")
    if success is not None and (failure is None or success >= failure):
        return "AVAILABLE", "LAST_CALL_SUCCEEDED"
    if failure is not None:
        return "UNAVAILABLE", "LAST_CALL_FAILED"
    return "UNVERIFIED", "NO_RUNTIME_EVIDENCE"


def _safe_from_db(row: Any, *, model: str, now: datetime, half_open: bool) -> SafeEndpoint:
    mapped = _PooledEndpoint(
        role=str(row["role"]), name=str(row["endpoint_name"]),
        configured=bool(row["configured"]), participation=str(row["participation"]),
        qualification=str(row["qualification_state"]), circuit=str(row["circuit_state"]),
        cooldown_until=row["cooldown_until"],
        manual_retry_not_before=row["manual_retry_not_before"], updated_at=row["updated_at"],
    )
    base = _to_safe_pooled(mapped, model=model, now=now, half_open=half_open)
    availability, reason = _runtime_availability(row)
    return replace(
        base, provider=str(row.get("provider") or "unknown"),
        failure_domain=str(row.get("failure_domain") or ""),
        availability=availability, availability_reason=reason,
        last_success_at=row.get("last_success_at"), last_failure_at=row.get("last_failure_at"),
        last_failure_code=row.get("last_failure_code"), last_probe_at=row.get("last_probe_at"),
        last_probe_status=row.get("last_probe_status"), incident_id=row.get("incident_id"),
        incident_started_at=row.get("incident_started_at"),
        incident_resolved_at=row.get("incident_resolved_at"),
    )


def _replay_error(code: str, retry_after: datetime | None = None) -> dict[str, Any]:
    status, message = _safe_error(code)
    payload: dict[str, Any] = {
        "ok": False,
        "error_code": code,
        "message": message,
        "http_status": status,
    }
    if retry_after is not None:
        payload["retry_after"] = _iso(retry_after)
    return payload


def _replay_success(endpoint: SafeEndpoint) -> dict[str, Any]:
    return {
        "ok": True,
        "http_status": 200,
        "endpoint": endpoint.to_api(),
    }


def _raise_from_replay(payload: dict[str, Any]) -> None:
    if payload.get("ok"):
        return
    code = str(payload.get("error_code") or "LLM_ENDPOINT_NOT_RECOVERABLE")
    status, message = _safe_error(code)
    retry_raw = payload.get("retry_after")
    retry_after = None
    if isinstance(retry_raw, str) and retry_raw:
        retry_after = datetime.fromisoformat(retry_raw.replace("Z", "+00:00"))
    raise RecoverError(
        code,
        http_status=int(payload.get("http_status") or status),
        message=message,
        retry_after=retry_after,
        replayed=True,
    )


def _endpoint_from_api(payload: dict[str, Any]) -> SafeEndpoint:
    cooldown = payload.get("cooldown_until")
    updated = payload.get("updated_at")
    return SafeEndpoint(
        role=str(payload["role"]).lower(),
        endpoint_id=str(payload["endpoint_id"]),
        display_name=str(payload["display_name"]),
        model=str(payload["model"]),
        participation=str(payload["participation"]),
        qualification=str(payload["qualification"]),
        circuit_state=str(payload["circuit_state"]),
        recoverable=bool(payload["recoverable"]),
        cooldown_until=(
            datetime.fromisoformat(str(cooldown).replace("Z", "+00:00"))
            if cooldown
            else None
        ),
        inflight=payload.get("inflight"),
        max_inflight=payload.get("max_inflight"),
        updated_at=datetime.fromisoformat(str(updated).replace("Z", "+00:00")),
        provider=str(payload.get("provider") or "unknown"),
        availability=str(payload.get("availability") or "UNVERIFIED"),
        availability_reason=str(payload.get("availability_reason") or "NO_RUNTIME_EVIDENCE"),
        last_success_at=(datetime.fromisoformat(str(payload["last_success_at"]).replace("Z", "+00:00")) if payload.get("last_success_at") else None),
        last_failure_at=(datetime.fromisoformat(str(payload["last_failure_at"]).replace("Z", "+00:00")) if payload.get("last_failure_at") else None),
        last_failure_code=payload.get("last_failure_code"),
        last_probe_at=(datetime.fromisoformat(str(payload["last_probe_at"]).replace("Z", "+00:00")) if payload.get("last_probe_at") else None),
        last_probe_status=payload.get("last_probe_status"),
        incident_id=uuid.UUID(str(payload["incident_id"])) if payload.get("incident_id") else None,
        incident_started_at=(datetime.fromisoformat(str(payload["incident_started_at"]).replace("Z", "+00:00")) if payload.get("incident_started_at") else None),
        incident_resolved_at=(datetime.fromisoformat(str(payload["incident_resolved_at"]).replace("Z", "+00:00")) if payload.get("incident_resolved_at") else None),
    )


def classify_closed_failure(failure_class: str) -> str:
    if failure_class == "TIMEOUT":
        return "LLM_ENDPOINT_PROBE_TIMEOUT"
    return "LLM_ENDPOINT_PROBE_FAILED"


class PostgresRoleRelayStore:
    def __init__(self, session_factory=None) -> None:
        self._session_factory = session_factory or get_session_factory()

    async def _lock_role(self, session, role: str) -> None:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"role-relay-runtime-{role}"},
        )

    async def _lock_operation(self, session, operation_id: uuid.UUID) -> None:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"llm-recover-{operation_id}"},
        )

    async def synchronize(
        self, role: str, fingerprints: dict[str, str]
    ) -> None:
        role = normalize_role(role)
        if role not in DURABLE_ROLES:
            return
        ordered = sorted(fingerprints.items())
        async with self._session_factory() as session, session.begin():
            await self._lock_role(session, role)
            await self._reclaim_expired(session, role)
            existing = list(
                (
                    await session.execute(
                        text(
                            """
                            SELECT endpoint_name, configured,
                                   configuration_fingerprint, circuit_state,
                                   active_operation_id, state_generation
                            FROM travel_role_relay_endpoint_state
                            WHERE role = :role AND provider = 'unknown'
                            FOR UPDATE
                            """
                        ),
                        {"role": role},
                    )
                ).mappings()
            )
            known = {str(row["endpoint_name"]): row for row in existing}
            for name, fingerprint in ordered:
                row = known.get(name)
                if row is None:
                    await session.execute(
                        text(
                            """
                            INSERT INTO travel_role_relay_endpoint_state (
                                role, endpoint_name, configured, participation,
                                qualification_state, circuit_state,
                                configuration_fingerprint, state_generation
                            ) VALUES (
                                :role, :name, TRUE, 'ACTIVE', 'QUALIFIED',
                                'CLOSED', :fingerprint, 0
                            )
                            """
                        ),
                        {"role": role, "name": name, "fingerprint": fingerprint},
                    )
                    continue
                prior = str(row["configuration_fingerprint"] or "")
                identity_changed = prior not in {"", fingerprint}
                reintroduced = not bool(row["configured"])
                if identity_changed or reintroduced:
                    if str(row["circuit_state"]) == "HALF_OPEN":
                        await self._terminalize_probe(
                            session,
                            role=role,
                            name=name,
                            lifecycle="CONFIGURATION_CHANGED",
                            error_code="LLM_ENDPOINT_NOT_RECOVERABLE",
                        )
                    await session.execute(
                        text(
                            """
                            UPDATE travel_role_relay_endpoint_state
                            SET configured = TRUE,
                                configuration_fingerprint = :fingerprint,
                                circuit_state = 'QUARANTINED',
                                qualification_state = 'UNQUALIFIED',
                                state_generation = state_generation + 1,
                                updated_at = NOW()
                            WHERE role = :role AND endpoint_name = :name
                            """
                        ),
                        {
                            "role": role,
                            "name": name,
                            "fingerprint": fingerprint,
                        },
                    )
                else:
                    await session.execute(
                        text(
                            """
                            UPDATE travel_role_relay_endpoint_state
                            SET configured = TRUE,
                                configuration_fingerprint = :fingerprint,
                                updated_at = NOW()
                            WHERE role = :role AND endpoint_name = :name
                            """
                        ),
                        {"role": role, "name": name, "fingerprint": fingerprint},
                    )
            live = set(fingerprints)
            for name, row in known.items():
                if name in live:
                    continue
                if str(row["circuit_state"]) == "HALF_OPEN":
                    await self._terminalize_probe(
                        session,
                        role=role,
                        name=name,
                        lifecycle="CONFIGURATION_CHANGED",
                        error_code="LLM_ENDPOINT_NOT_RECOVERABLE",
                    )
                await session.execute(
                    text(
                        """
                        UPDATE travel_role_relay_endpoint_state
                        SET configured = FALSE,
                            updated_at = NOW()
                        WHERE role = :role AND endpoint_name = :name
                        """
                    ),
                    {"role": role, "name": name},
                )

    async def synchronize_direct(
        self, role: str, name: str, *, provider: str, failure_domain: str,
        fingerprint: str, qualified_initial: bool = False, has_alternatives: bool = False,
    ) -> None:
        """Register a v0.12 direct endpoint without treating config as qualification."""
        role = normalize_role(role)
        if role not in DURABLE_ROLES:
            return
        async with self._session_factory() as session, session.begin():
            await self._lock_role(session, role)
            await session.execute(text("""
                INSERT INTO travel_role_relay_endpoint_state (
                    role, endpoint_name, provider, failure_domain, configured,
                    participation, qualification_state, circuit_state,
                    configuration_fingerprint
                ) VALUES (
                    :role, :name, :provider, :domain, TRUE, 'ACTIVE',
                    :initial_qualification, 'CLOSED', :fingerprint
                )
                ON CONFLICT (role, endpoint_name) DO UPDATE SET
                    provider = EXCLUDED.provider,
                    failure_domain = EXCLUDED.failure_domain,
                    configured = TRUE,
                    qualification_state = CASE
                        WHEN travel_role_relay_endpoint_state.configuration_fingerprint
                             <> EXCLUDED.configuration_fingerprint
                        THEN 'UNQUALIFIED' ELSE travel_role_relay_endpoint_state.qualification_state END,
                    circuit_state = CASE
                        WHEN travel_role_relay_endpoint_state.configuration_fingerprint
                             <> EXCLUDED.configuration_fingerprint
                        THEN 'CLOSED' ELSE travel_role_relay_endpoint_state.circuit_state END,
                    last_success_at = CASE
                        WHEN travel_role_relay_endpoint_state.configuration_fingerprint
                             <> EXCLUDED.configuration_fingerprint
                        THEN NULL ELSE travel_role_relay_endpoint_state.last_success_at END,
                    last_failure_at = CASE
                        WHEN travel_role_relay_endpoint_state.configuration_fingerprint
                             <> EXCLUDED.configuration_fingerprint
                        THEN NULL ELSE travel_role_relay_endpoint_state.last_failure_at END,
                    last_failure_code = CASE
                        WHEN travel_role_relay_endpoint_state.configuration_fingerprint
                             <> EXCLUDED.configuration_fingerprint
                        THEN NULL ELSE travel_role_relay_endpoint_state.last_failure_code END,
                    probe_kind = CASE WHEN travel_role_relay_endpoint_state.configuration_fingerprint
                             <> EXCLUDED.configuration_fingerprint THEN NULL ELSE travel_role_relay_endpoint_state.probe_kind END,
                    probe_owner = CASE WHEN travel_role_relay_endpoint_state.configuration_fingerprint
                             <> EXCLUDED.configuration_fingerprint THEN NULL ELSE travel_role_relay_endpoint_state.probe_owner END,
                    probe_lease_expires_at = CASE WHEN travel_role_relay_endpoint_state.configuration_fingerprint
                             <> EXCLUDED.configuration_fingerprint THEN NULL ELSE travel_role_relay_endpoint_state.probe_lease_expires_at END,
                    recovery_origin_circuit_state = CASE WHEN travel_role_relay_endpoint_state.configuration_fingerprint
                             <> EXCLUDED.configuration_fingerprint THEN NULL ELSE travel_role_relay_endpoint_state.recovery_origin_circuit_state END,
                    active_operation_id = CASE WHEN travel_role_relay_endpoint_state.configuration_fingerprint
                             <> EXCLUDED.configuration_fingerprint THEN NULL ELSE travel_role_relay_endpoint_state.active_operation_id END,
                    state_generation = CASE WHEN travel_role_relay_endpoint_state.configuration_fingerprint
                             <> EXCLUDED.configuration_fingerprint
                        THEN travel_role_relay_endpoint_state.state_generation + 1
                        ELSE travel_role_relay_endpoint_state.state_generation END,
                    configuration_fingerprint = EXCLUDED.configuration_fingerprint,
                    updated_at = NOW()
            """), {"role": role, "name": name, "provider": provider[:64],
                    "domain": failure_domain[:128], "fingerprint": fingerprint[:64],
                    "initial_qualification": "QUALIFIED" if qualified_initial else "UNQUALIFIED"})
            if not has_alternatives:
                # Undo only the former single-path runtime short cooldown. Old
                # configuration/manual isolation and confirmed account faults
                # remain authoritative; no pool is inferred from its provider.
                await session.execute(text("""
                    UPDATE travel_role_relay_endpoint_state
                    SET circuit_state='CLOSED', cooldown_until=NULL, cooldown_level=0,
                        state_generation=state_generation + 1, updated_at=NOW()
                    WHERE role=:role AND endpoint_name=:name
                      AND configuration_fingerprint=:fingerprint AND configured
                      AND participation IN ('ACTIVE', 'CANARY') AND circuit_state='OPEN'
                      AND last_failure_at IS NOT NULL AND last_failure_code=ANY(:ordinary)
                      AND manual_retry_not_before IS NULL AND manual_retry_level=0
                      AND probe_kind IS NULL AND probe_owner IS NULL
                      AND active_operation_id IS NULL
                    """), {"role": role, "name": name, "fingerprint": fingerprint,
                             "ordinary": list(SINGLE_PATH_ORDINARY_FAILURES)})

    async def record_direct_attempt(
        self, role: str, name: str, *, success: bool,
        failure_class: str | None = None, failure_scope: str | None = None,
        trial: bool = False, generation: int | None = None,
        retry_after_seconds: int | None = None,
        has_alternatives: bool = False,
    ) -> None:
        await self.record_attempt(
            role, name, success=success, failure_class=failure_class,
            failure_scope=failure_scope, trial=trial, generation=generation,
            retry_after_seconds=retry_after_seconds,
            has_alternatives=has_alternatives,
        )

    async def release_direct_trial(self, role: str, name: str, *, generation: int) -> None:
        """Release a lease when no provider request was completed or cancelled."""
        role = normalize_role(role)
        async with self._session_factory() as session, session.begin():
            await self._lock_role(session, role)
            await session.execute(text("""
                UPDATE travel_role_relay_endpoint_state SET
                    circuit_state=CASE WHEN qualification_state='UNQUALIFIED'
                                       THEN 'CLOSED'
                                       ELSE COALESCE(recovery_origin_circuit_state, 'OPEN') END,
                    probe_kind=NULL, probe_owner=NULL, probe_lease_expires_at=NULL,
                    recovery_origin_circuit_state=NULL, active_operation_id=NULL,
                    state_generation=state_generation + 1, updated_at=NOW()
                WHERE role=:role AND endpoint_name=:name
                  AND circuit_state='HALF_OPEN' AND state_generation=:generation
            """), {"role": role, "name": name, "generation": generation})

    async def _reclaim_expired(self, session, role: str | None = None) -> None:
        params: dict[str, Any] = {}
        role_filter = ""
        if role is not None:
            role_filter = "AND role = :role"
            params["role"] = role
        expired = list(
            (
                await session.execute(
                    text(
                        f"""
                        SELECT role, endpoint_name, recovery_origin_circuit_state,
                               active_operation_id, cooldown_level,
                               manual_retry_level, probe_kind
                        FROM travel_role_relay_endpoint_state
                        WHERE circuit_state = 'HALF_OPEN'
                          AND probe_lease_expires_at <= NOW()
                          {role_filter}
                        FOR UPDATE
                        """
                    ),
                    params,
                )
            ).mappings()
        )
        for row in expired:
            await self._finish_expired_row(session, row)

    async def _finish_expired_row(self, session, row: Any) -> None:
        origin = str(row["recovery_origin_circuit_state"] or "OPEN")
        if origin not in {"OPEN", "QUARANTINED"}:
            origin = "OPEN"
        cooldown_level = min(2, int(row["cooldown_level"]))
        manual_level = min(2, int(row["manual_retry_level"]))
        is_manual = str(row["probe_kind"] or "") == "manual"
        next_manual = manual_level + 1 if is_manual else manual_level
        await session.execute(
            text(
                """
                UPDATE travel_role_relay_endpoint_state
                SET circuit_state = CAST(:origin AS VARCHAR(16)),
                    qualification_state = CASE
                        WHEN CAST(:origin AS VARCHAR(16)) = 'QUARANTINED'
                            THEN 'UNQUALIFIED'
                        ELSE qualification_state
                    END,
                    consecutive_transient_failures = GREATEST(
                        consecutive_transient_failures,
                        :threshold
                    ),
                    cooldown_until = NOW() + make_interval(
                        secs => :cooldown_seconds
                    ),
                    cooldown_level = LEAST(2, cooldown_level + 1),
                    manual_retry_not_before = CASE
                        WHEN :is_manual
                            THEN NOW() + make_interval(secs => :manual_seconds)
                        ELSE manual_retry_not_before
                    END,
                    manual_retry_level = CASE
                        WHEN :is_manual THEN LEAST(2, :next_manual)
                        ELSE manual_retry_level
                    END,
                    probe_kind = NULL,
                    probe_owner = NULL,
                    probe_lease_expires_at = NULL,
                    recovery_origin_circuit_state = NULL,
                    active_operation_id = NULL,
                    state_generation = state_generation + 1,
                    updated_at = NOW()
                WHERE role = :role AND endpoint_name = :name
                  AND circuit_state = 'HALF_OPEN'
                """
            ),
            {
                "role": row["role"],
                "name": row["endpoint_name"],
                "origin": origin,
                "threshold": TRANSIENT_FAILURE_THRESHOLD,
                "cooldown_seconds": COOLDOWN_SECONDS[min(cooldown_level, 2)],
                "manual_seconds": COOLDOWN_SECONDS[min(manual_level, 2)],
                "is_manual": is_manual,
                "next_manual": next_manual,
            },
        )
        operation_id = row["active_operation_id"]
        if operation_id is not None:
            await self._complete_operation(
                session,
                uuid.UUID(str(operation_id)),
                lifecycle="EXPIRED",
                error_code="LLM_ENDPOINT_PROBE_TIMEOUT",
            )
        logger.warning(
            "role_relay_probe_expired role=%s endpoint=%s",
            row["role"],
            row["endpoint_name"],
        )

    async def _terminalize_probe(
        self,
        session,
        *,
        role: str,
        name: str,
        lifecycle: str,
        error_code: str,
    ) -> None:
        row = (
            (
                await session.execute(
                    text(
                        """
                        SELECT active_operation_id, manual_retry_level, probe_kind
                        FROM travel_role_relay_endpoint_state
                        WHERE role = :role AND endpoint_name = :name
                        FOR UPDATE
                        """
                    ),
                    {"role": role, "name": name},
                )
            )
            .mappings()
            .first()
        )
        operation_id = None if row is None else row["active_operation_id"]
        is_manual = bool(row and str(row["probe_kind"] or "") == "manual")
        manual_level = int(row["manual_retry_level"]) if row else 0
        await session.execute(
            text(
                """
                UPDATE travel_role_relay_endpoint_state
                SET configured = CASE
                        WHEN :lifecycle = 'CONFIGURATION_CHANGED'
                            THEN configured
                        ELSE configured
                    END,
                    circuit_state = 'QUARANTINED',
                    qualification_state = 'UNQUALIFIED',
                    probe_kind = NULL,
                    probe_owner = NULL,
                    probe_lease_expires_at = NULL,
                    recovery_origin_circuit_state = NULL,
                    active_operation_id = NULL,
                    state_generation = state_generation + 1,
                    manual_retry_not_before = CASE
                        WHEN :is_manual
                            THEN NOW() + make_interval(secs => :manual_seconds)
                        ELSE manual_retry_not_before
                    END,
                    updated_at = NOW()
                WHERE role = :role AND endpoint_name = :name
                """
            ),
            {
                "role": role,
                "name": name,
                "lifecycle": lifecycle,
                "is_manual": is_manual,
                "manual_seconds": COOLDOWN_SECONDS[min(manual_level, 2)],
            },
        )
        if operation_id is not None:
            await self._complete_operation(
                session,
                uuid.UUID(str(operation_id)),
                lifecycle=lifecycle,
                error_code=error_code,
            )

    async def _load_row(self, session, role: str, name: str) -> Any | None:
        return (
            (
                await session.execute(
                    text(
                        """
                        SELECT *
                        FROM travel_role_relay_endpoint_state
                        WHERE role = :role AND endpoint_name = :name
                        FOR UPDATE
                        """
                    ),
                    {"role": role, "name": name},
                )
            )
            .mappings()
            .first()
        )

    async def list_projections(
        self, role: str, models: dict[str, str]
    ) -> list[SafeEndpoint]:
        role = normalize_role(role)
        now = _utcnow()
        async with self._session_factory() as session, session.begin():
            await self._lock_role(session, role)
            await self._reclaim_expired(session, role)
            half_open = bool(
                await session.scalar(
                    text(
                        """
                        SELECT EXISTS (
                            SELECT 1 FROM travel_role_relay_endpoint_state
                            WHERE role = :role AND circuit_state = 'HALF_OPEN'
                        )
                        """
                    ),
                    {"role": role},
                )
            )
            rows = list(
                (
                    await session.execute(
                        text(
                            """
                            SELECT *
                            FROM travel_role_relay_endpoint_state
                            WHERE role = :role AND configured = TRUE
                            ORDER BY endpoint_name
                            """
                        ),
                        {"role": role},
                    )
                ).mappings()
            )
        items: list[SafeEndpoint] = []
        for row in rows:
            name = str(row["endpoint_name"])
            model = models.get(name)
            if not model:
                continue
            items.append(_safe_from_db(row, model=model, now=now, half_open=half_open))
        return items

    async def admit(
        self,
        role: str,
        names: tuple[str, ...],
        *,
        allow_recovery: bool = True,
    ) -> tuple[tuple[str, ...], str | None, int | dict[str, int] | None]:
        """Return closed names plus optional automatic HALF_OPEN trial."""
        role = normalize_role(role)
        if role not in DURABLE_ROLES or not names:
            return names, None, None
        async with self._session_factory() as session, session.begin():
            await self._lock_role(session, role)
            await self._reclaim_expired(session, role)
            rows = list(
                (
                    await session.execute(
                        text(
                            """
                            SELECT endpoint_name, configured, participation,
                                   qualification_state, circuit_state,
                                   cooldown_until, state_generation, provider
                            FROM travel_role_relay_endpoint_state
                            WHERE role = :role AND endpoint_name = ANY(:names)
                            FOR UPDATE
                            """
                        ),
                        {"role": role, "names": list(names)},
                    )
                ).mappings()
            )
            by_name = {str(row["endpoint_name"]): row for row in rows}
            half_open_exists = any(
                str(row["circuit_state"]) == "HALF_OPEN" for row in rows
            ) or bool(
                await session.scalar(
                    text(
                        """
                        SELECT EXISTS (
                            SELECT 1 FROM travel_role_relay_endpoint_state
                            WHERE role = :role AND circuit_state = 'HALF_OPEN'
                        )
                        """
                    ),
                    {"role": role},
                )
            )
            closed: list[str] = []
            closed_generations: dict[str, int] = {}
            recovery: list[str] = []
            qualification_trials: list[str] = []
            for name in names:
                row = by_name.get(name)
                if row is None or not bool(row["configured"]):
                    continue
                if str(row["participation"]) == "DISABLED":
                    continue
                if (
                    str(row["qualification_state"]) != "QUALIFIED"
                    and str(row.get("provider") or "unknown") != "unknown"
                    and str(row["circuit_state"]) == "CLOSED"
                    and not half_open_exists
                ):
                    qualification_trials.append(name)
                    continue
                if str(row["qualification_state"]) != "QUALIFIED" and str(row["circuit_state"]) != "OPEN":
                    continue
                circuit = str(row["circuit_state"])
                cooldown = row["cooldown_until"]
                cooldown_ready = cooldown is None or cooldown <= _utcnow()
                if circuit == "CLOSED":
                    closed.append(name)
                    closed_generations[name] = int(row["state_generation"])
                elif (
                    circuit == "OPEN"
                    and cooldown_ready
                    and not half_open_exists
                ):
                    recovery.append(name)
            candidates = qualification_trials or recovery
            if allow_recovery and candidates:
                selected = candidates[0]
                owner = f"automatic:{uuid.uuid4()}"
                updated = (
                    (
                        await session.execute(
                            text(
                                """
                                UPDATE travel_role_relay_endpoint_state
                                SET circuit_state = 'HALF_OPEN',
                                    probe_kind = 'automatic',
                                    probe_owner = :owner,
                                    probe_lease_expires_at = NOW()
                                        + make_interval(secs => :lease),
                                    recovery_origin_circuit_state = 'OPEN',
                                    active_operation_id = NULL,
                                    state_generation = state_generation + 1,
                                    updated_at = NOW()
                                WHERE role = :role AND endpoint_name = :name
                                  AND circuit_state IN ('OPEN', 'CLOSED')
                                RETURNING state_generation
                                """
                            ),
                            {
                                "role": role,
                                "name": selected,
                                "owner": owner,
                                "lease": PROBE_LEASE_SECONDS,
                            },
                        )
                    )
                    .mappings()
                    .first()
                )
                if updated is not None:
                    logger.warning(
                        "role_relay_half_open_started role=%s endpoint=%s",
                        role,
                        selected,
                    )
                    return (selected,), selected, int(updated["state_generation"])
            generation = (
                closed_generations.get(closed[0]) if len(closed) == 1 else closed_generations
            )
            return tuple(closed), None, generation

    async def record_attempt(
        self,
        role: str,
        name: str,
        *,
        success: bool,
        failure_class: str | None = None,
        failure_scope: str | None = None,
        retry_after_seconds: int | None = None,
        trial: bool = False,
        generation: int | None = None,
        has_alternatives: bool = True,
    ) -> None:
        role = normalize_role(role)
        async with self._session_factory() as session, session.begin():
            await self._lock_role(session, role)
            if has_alternatives:
                await self._reclaim_expired(session, role)
            row = await self._load_row(session, role, name)
            if row is None:
                return
            if generation is not None and int(row["state_generation"]) != generation:
                return
            if not trial and str(row["circuit_state"]) != "CLOSED":
                # An already dispatched request cannot undo a newer stop or trial.
                return
            if success:
                await session.execute(
                    text(
                        """
                        UPDATE travel_role_relay_endpoint_state
                        SET consecutive_transient_failures = 0,
                            circuit_state = CASE
                                WHEN :trial AND circuit_state = 'HALF_OPEN'
                                    THEN 'CLOSED'
                                ELSE circuit_state
                            END,
                            qualification_state = CASE
                                WHEN :trial AND circuit_state = 'HALF_OPEN'
                                    THEN 'QUALIFIED'
                                ELSE qualification_state
                            END,
                            cooldown_level = CASE
                                WHEN :trial THEN 0 ELSE cooldown_level
                            END,
                            cooldown_until = CASE
                                WHEN :trial THEN NULL ELSE cooldown_until
                            END,
                            manual_retry_level = CASE
                                WHEN :trial THEN 0 ELSE manual_retry_level
                            END,
                            manual_retry_not_before = CASE
                                WHEN :trial THEN NULL
                                ELSE manual_retry_not_before
                            END,
                            probe_kind = CASE
                                WHEN :trial THEN NULL ELSE probe_kind
                            END,
                            probe_owner = CASE
                                WHEN :trial THEN NULL ELSE probe_owner
                            END,
                            probe_lease_expires_at = CASE
                                WHEN :trial THEN NULL ELSE probe_lease_expires_at
                            END,
                            recovery_origin_circuit_state = CASE
                                WHEN :trial THEN NULL
                                ELSE recovery_origin_circuit_state
                            END,
                            active_operation_id = CASE
                                WHEN :trial THEN NULL ELSE active_operation_id
                            END,
                            last_success_at = NOW(),
                            last_failure_code = NULL,
                            incident_resolved_at = CASE
                                WHEN incident_id IS NOT NULL THEN NOW()
                                ELSE incident_resolved_at END,
                            state_generation = CASE
                                WHEN :trial THEN state_generation + 1
                                ELSE state_generation
                            END,
                            updated_at = NOW()
                        WHERE role = :role AND endpoint_name = :name
                        """
                    ),
                    {"role": role, "name": name, "trial": trial},
                )
                return
            failure_class = str(failure_class or "TRANSPORT")
            if not has_alternatives and failure_class not in LONG_FAILURE_CLASSES:
                if trial and str(row["probe_kind"] or "") != "automatic":
                    return
                await session.execute(text("""
                    UPDATE travel_role_relay_endpoint_state
                    SET circuit_state='CLOSED', cooldown_until=NULL, cooldown_level=0,
                        last_failure_at=NOW(), last_failure_code=:failure,
                        consecutive_transient_failures=consecutive_transient_failures + 1,
                        probe_kind=CASE WHEN :trial THEN NULL ELSE probe_kind END,
                        probe_owner=CASE WHEN :trial THEN NULL ELSE probe_owner END,
                        probe_lease_expires_at=CASE WHEN :trial THEN NULL ELSE probe_lease_expires_at END,
                        recovery_origin_circuit_state=CASE WHEN :trial THEN NULL ELSE recovery_origin_circuit_state END,
                        state_generation=CASE WHEN :trial THEN state_generation + 1 ELSE state_generation END,
                        updated_at=NOW()
                    WHERE role=:role AND endpoint_name=:name
                    """), {"role": role, "name": name, "failure": failure_class[:80], "trial": trial})
                return
            failure_scope = str(failure_scope or "UNKNOWN").upper()
            configured_domain = str(row.get("failure_domain") or "")
            incident_scope = (
                failure_scope if failure_scope in {"PROVIDER", "ACCOUNT"}
                else "MODEL" if failure_scope == "MODEL" else "ENDPOINT"
            )
            incident_domain = (
                configured_domain
                if failure_scope in {"PROVIDER", "ACCOUNT"} and configured_domain
                else f"endpoint:{role}:{name}"
            )
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"role-relay-incident-{incident_domain}"},
            )
            incident_id = await session.scalar(text("""
                SELECT incident_id FROM travel_role_relay_endpoint_state
                WHERE incident_id IS NOT NULL AND incident_resolved_at IS NULL
                  AND incident_scope=:incident_scope
                  AND incident_domain=:incident_domain
                ORDER BY incident_started_at LIMIT 1
            """), {"incident_scope": incident_scope,
                    "incident_domain": incident_domain})
            incident_id = incident_id or uuid.uuid4()
            quarantine = False
            counted = failure_class in TRANSIENT_FAILURE_CLASSES
            immediate = failure_class in IMMEDIATE_OPEN_FAILURE_CLASSES
            transient = int(row["consecutive_transient_failures"])
            if counted or immediate:
                transient += 1
            else:
                transient = 0
            origin = str(row["recovery_origin_circuit_state"] or "OPEN")
            if origin not in {"OPEN", "QUARANTINED"}:
                origin = "OPEN"
            open_circuit = bool(
                trial
                or immediate
                or (counted and transient >= TRANSIENT_FAILURE_THRESHOLD)
            )
            target = (
                "QUARANTINED"
                if quarantine or origin == "QUARANTINED"
                else "OPEN"
                if open_circuit
                else str(row["circuit_state"])
            )
            cooldown_level = int(row["cooldown_level"])
            is_manual = str(row["probe_kind"] or "") == "manual"
            await session.execute(
                text(
                    """
                    UPDATE travel_role_relay_endpoint_state
                    SET consecutive_transient_failures = :transient,
                        circuit_state = CAST(:target AS VARCHAR(16)),
                        qualification_state = CASE
                            WHEN :quarantine
                                 OR CAST(:target AS VARCHAR(16)) = 'QUARANTINED'
                                THEN 'UNQUALIFIED'
                            ELSE qualification_state
                        END,
                        cooldown_until = CASE
                            WHEN :open_circuit
                                THEN NOW() + make_interval(
                                    secs => :cooldown_seconds
                                )
                            ELSE cooldown_until
                        END,
                        cooldown_level = CASE
                            WHEN :open_circuit THEN LEAST(2, cooldown_level + 1)
                            ELSE cooldown_level
                        END,
                        manual_retry_not_before = CASE
                            WHEN :trial AND :is_manual
                                THEN NOW() + make_interval(
                                    secs => :manual_seconds
                                )
                            ELSE manual_retry_not_before
                        END,
                        manual_retry_level = CASE
                            WHEN :trial AND :is_manual
                                THEN LEAST(2, manual_retry_level + 1)
                            ELSE manual_retry_level
                        END,
                        probe_kind = CASE WHEN :trial THEN NULL ELSE probe_kind END,
                        probe_owner = CASE WHEN :trial THEN NULL ELSE probe_owner END,
                        probe_lease_expires_at = CASE
                            WHEN :trial THEN NULL ELSE probe_lease_expires_at
                        END,
                        recovery_origin_circuit_state = CASE
                            WHEN :trial THEN NULL
                            ELSE recovery_origin_circuit_state
                        END,
                        active_operation_id = CASE
                            WHEN :trial THEN NULL ELSE active_operation_id
                        END,
                        last_failure_at = NOW(),
                        last_failure_code = :failure_class,
                        incident_id = :incident_id,
                        incident_scope = :incident_scope,
                        incident_domain = :incident_domain,
                        incident_started_at = CASE
                            WHEN incident_id = :incident_id AND incident_started_at IS NOT NULL
                            THEN incident_started_at ELSE NOW() END,
                        incident_resolved_at = NULL,
                        state_generation = CASE
                            WHEN :trial OR :open_circuit THEN state_generation + 1
                            ELSE state_generation
                        END,
                        updated_at = NOW()
                    WHERE role = :role AND endpoint_name = :name
                    """
                ),
                {
                    "role": role,
                    "name": name,
                    "transient": transient,
                    "target": target,
                    "quarantine": quarantine,
                    "open_circuit": open_circuit or trial,
                    "cooldown_seconds": (
                        cooldown_seconds(failure_class, retry_after_seconds)
                    ),
                    "manual_seconds": COOLDOWN_SECONDS[
                        min(int(row["manual_retry_level"]), 2)
                    ],
                    "trial": trial,
                    "is_manual": is_manual,
                    "failure_class": failure_class[:80],
                    "incident_id": incident_id,
                    "incident_scope": incident_scope,
                    "incident_domain": incident_domain,
                },
            )

    async def claim_manual_probe(
        self,
        role: str,
        name: str,
        *,
        operation_id: uuid.UUID,
        owner: str,
    ) -> tuple[str, int | None]:
        role = normalize_role(role)
        async with self._session_factory() as session, session.begin():
            await self._lock_operation(session, operation_id)
            await self._lock_role(session, role)
            await self._reclaim_expired(session, role)
            replay = await self._claim_operation(
                session, operation_id, role, name
            )
            if replay is not None:
                return "REPLAY", None
            row = await self._load_row(session, role, name)
            status = self._manual_eligibility(row)
            if status == "CLAIMED":
                half_open = bool(
                    await session.scalar(
                        text(
                            """
                            SELECT EXISTS (
                                SELECT 1 FROM travel_role_relay_endpoint_state
                                WHERE role = :role AND circuit_state = 'HALF_OPEN'
                            )
                            """
                        ),
                        {"role": role},
                    )
                )
                if half_open:
                    status = "BUSY_PROBE"
            if status != "CLAIMED":
                code, http_status = _claim_error(status)
                retry_after = None
                if (
                    status == "NOT_RECOVERABLE"
                    and row is not None
                    and row["manual_retry_not_before"] is not None
                    and row["manual_retry_not_before"] > _utcnow()
                ):
                    retry_after = row["manual_retry_not_before"]
                await self._complete_operation(
                    session,
                    operation_id,
                    lifecycle="FAILED",
                    error_code=code,
                    http_status=http_status,
                    retry_after=retry_after,
                )
                return status, None
            origin = str(row["circuit_state"])
            updated = (
                (
                    await session.execute(
                        text(
                            """
                            UPDATE travel_role_relay_endpoint_state
                            SET circuit_state = 'HALF_OPEN',
                                probe_kind = 'manual',
                                probe_owner = :owner,
                                probe_lease_expires_at = NOW()
                                    + make_interval(secs => :lease),
                                recovery_origin_circuit_state = :origin,
                                active_operation_id = :operation_id,
                                last_probe_at = NOW(),
                                last_probe_status = 'RUNNING',
                                state_generation = state_generation + 1,
                                updated_at = NOW()
                            WHERE role = :role AND endpoint_name = :name
                              AND circuit_state IN ('OPEN', 'QUARANTINED')
                            RETURNING state_generation
                            """
                        ),
                        {
                            "role": role,
                            "name": name,
                            "owner": owner[:128],
                            "lease": PROBE_LEASE_SECONDS,
                            "origin": origin,
                            "operation_id": operation_id,
                        },
                    )
                )
                .mappings()
                .first()
            )
            if updated is None:
                await self._complete_operation(
                    session,
                    operation_id,
                    lifecycle="FAILED",
                    error_code="LLM_ENDPOINT_RECOVERY_IN_PROGRESS",
                )
                return "BUSY_PROBE", None
            logger.warning(
                "role_relay_manual_probe_started role=%s endpoint=%s",
                role,
                name,
            )
            return "CLAIMED", int(updated["state_generation"])

    def _manual_eligibility(self, row: Any | None) -> str:
        if row is None or not bool(row["configured"]):
            return "NOT_FOUND"
        if str(row["participation"]) == "DISABLED":
            return "DISABLED"
        now = _utcnow()
        if (
            row["manual_retry_not_before"] is not None
            and row["manual_retry_not_before"] > now
        ):
            return "NOT_RECOVERABLE"
        circuit = str(row["circuit_state"])
        if circuit == "CLOSED":
            return "NOT_RECOVERABLE"
        if circuit == "HALF_OPEN":
            return "BUSY_PROBE"
        if circuit not in {"OPEN", "QUARANTINED"}:
            return "NOT_RECOVERABLE"
        return "CLAIMED"

    async def finish_manual_probe(
        self,
        role: str,
        name: str,
        *,
        operation_id: uuid.UUID,
        owner: str,
        generation: int,
        success: bool,
        failure_class: str | None,
        endpoint: SafeEndpoint | None,
    ) -> RecoverSuccess:
        role = normalize_role(role)
        error: RecoverError | None = None
        result: RecoverSuccess | None = None
        async with self._session_factory() as session, session.begin():
            await self._lock_operation(session, operation_id)
            await self._lock_role(session, role)
            await self._reclaim_expired(session, role)
            row = await self._load_row(session, role, name)
            if (
                row is None
                or str(row["probe_owner"] or "") != owner
                or int(row["state_generation"]) != generation
            ):
                existing = await self._load_operation(session, operation_id)
                if existing and existing["lifecycle"] != "PENDING":
                    payload = existing["replay_json"] or {}
                    if payload.get("ok"):
                        result = RecoverSuccess(
                            _endpoint_from_api(payload["endpoint"]),
                            replayed=True,
                        )
                    else:
                        try:
                            _raise_from_replay(payload)
                        except RecoverError as exc:
                            error = exc
                else:
                    error = RecoverError(
                        "LLM_ENDPOINT_RECOVERY_IN_PROGRESS",
                        http_status=409,
                        message="endpoint recovery is in progress",
                    )
            else:
                await self._apply_probe_result(
                    session,
                    role=role,
                    name=name,
                    success=success,
                    failure_class=failure_class,
                    generation=generation,
                )
                row = await self._load_row(session, role, name)
                if success:
                    assert endpoint is not None
                    finished = _safe_from_db(
                        row, model=endpoint.model, now=_utcnow(), half_open=False
                    )
                    await self._complete_operation(
                        session,
                        operation_id,
                        lifecycle="SUCCEEDED",
                        endpoint=finished,
                    )
                    result = RecoverSuccess(finished)
                else:
                    error_code = classify_closed_failure(
                        str(failure_class or "TRANSPORT")
                    )
                    retry_after = (
                        row["manual_retry_not_before"] or row["cooldown_until"]
                    )
                    await self._complete_operation(
                        session,
                        operation_id,
                        lifecycle="FAILED",
                        error_code=error_code,
                        retry_after=retry_after,
                    )
                    status, message = _safe_error(error_code)
                    error = RecoverError(
                        error_code,
                        http_status=status,
                        message=message,
                        retry_after=retry_after,
                    )
        if error is not None:
            raise error
        assert result is not None
        return result

    async def _apply_probe_result(
        self,
        session,
        *,
        role: str,
        name: str,
        success: bool,
        failure_class: str | None,
        generation: int,
    ) -> None:
        row = await self._load_row(session, role, name)
        if row is None or int(row["state_generation"]) != generation:
            return
        if success:
            await session.execute(
                text(
                    """
                    UPDATE travel_role_relay_endpoint_state
                    SET circuit_state = 'CLOSED',
                        qualification_state = 'QUALIFIED',
                        consecutive_transient_failures = 0,
                        cooldown_level = 0,
                        cooldown_until = NULL,
                        manual_retry_level = 0,
                        manual_retry_not_before = NULL,
                        probe_kind = NULL,
                        probe_owner = NULL,
                        probe_lease_expires_at = NULL,
                        recovery_origin_circuit_state = NULL,
                        active_operation_id = NULL,
                        last_probe_at = NOW(),
                        last_probe_status = 'SUCCESS',
                        last_success_at = NOW(),
                        last_failure_code = NULL,
                        incident_resolved_at = CASE
                            WHEN incident_id IS NOT NULL THEN NOW()
                            ELSE incident_resolved_at END,
                        state_generation = state_generation + 1,
                        updated_at = NOW()
                    WHERE role = :role AND endpoint_name = :name
                      AND state_generation = :generation
                    """
                ),
                {"role": role, "name": name, "generation": generation},
            )
            return
        failure_class = str(failure_class or "TRANSPORT")
        quarantine = failure_class in QUARANTINE_FAILURE_CLASSES
        origin = str(row["recovery_origin_circuit_state"] or "OPEN")
        target = "QUARANTINED" if origin == "QUARANTINED" and failure_class not in LONG_FAILURE_CLASSES else "OPEN"
        cooldown_level = int(row["cooldown_level"])
        manual_level = int(row["manual_retry_level"])
        await session.execute(
            text(
                """
                UPDATE travel_role_relay_endpoint_state
                SET circuit_state = CAST(:target AS VARCHAR(16)),
                    qualification_state = CASE
                        WHEN CAST(:target AS VARCHAR(16)) = 'QUARANTINED'
                            THEN 'UNQUALIFIED'
                        ELSE qualification_state
                    END,
                    consecutive_transient_failures = GREATEST(
                        consecutive_transient_failures, :threshold
                    ),
                    cooldown_until = NOW() + make_interval(
                        secs => :cooldown_seconds
                    ),
                    cooldown_level = LEAST(2, cooldown_level + 1),
                    manual_retry_not_before = NOW() + make_interval(
                        secs => :manual_seconds
                    ),
                    manual_retry_level = LEAST(2, manual_retry_level + 1),
                    probe_kind = NULL,
                    probe_owner = NULL,
                    probe_lease_expires_at = NULL,
                    recovery_origin_circuit_state = NULL,
                    active_operation_id = NULL,
                    last_probe_at = NOW(),
                    last_probe_status = 'FAILED',
                    last_failure_at = NOW(),
                    last_failure_code = :failure_class,
                    state_generation = state_generation + 1,
                    updated_at = NOW()
                WHERE role = :role AND endpoint_name = :name
                  AND state_generation = :generation
                """
            ),
            {
                "role": role,
                "name": name,
                "target": target,
                "threshold": TRANSIENT_FAILURE_THRESHOLD,
                "cooldown_seconds": cooldown_seconds(failure_class),
                "manual_seconds": cooldown_seconds(failure_class),
                "generation": generation,
                "failure_class": failure_class[:80],
            },
        )

    async def _claim_operation(
        self,
        session,
        operation_id: uuid.UUID,
        role: str,
        name: str,
    ) -> dict[str, Any] | None:
        inserted = (
            await session.execute(
                text(
                    """
                    INSERT INTO travel_llm_endpoint_recovery_operation (
                        operation_id, role, endpoint_name, lifecycle
                    ) VALUES (
                        :operation_id, :role, :name, 'PENDING'
                    )
                    ON CONFLICT (operation_id) DO NOTHING
                    RETURNING operation_id
                    """
                ),
                {
                    "operation_id": operation_id,
                    "role": role,
                    "name": name,
                },
            )
        ).first()
        if inserted is not None:
            return None
        existing = await self._load_operation(session, operation_id)
        if existing is None:
            raise RecoverError(
                "INTERNAL_ADMIN_UNAVAILABLE",
                http_status=503,
                message="internal admin is unavailable",
            )
        if (
            str(existing["role"]) != role
            or str(existing["endpoint_name"]) != name
        ):
            raise RecoverError(
                "LLM_ENDPOINT_OPERATION_CONFLICT",
                http_status=409,
                message="operation id conflict",
                replayed=True,
            )
        if str(existing["lifecycle"]) == "PENDING":
            raise RecoverError(
                "LLM_ENDPOINT_RECOVERY_IN_PROGRESS",
                http_status=409,
                message="endpoint recovery is in progress",
                replayed=True,
            )
        payload = existing["replay_json"] or {}
        if payload.get("ok"):
            return payload
        _raise_from_replay(payload)
        return None

    async def remember_closed_rejection(
        self,
        operation_id: uuid.UUID,
        role: str,
        name: str,
        code: str,
    ) -> RecoverSuccess | None:
        role = normalize_role(role)
        name = str(name or "").strip().lower()
        async with self._session_factory() as session, session.begin():
            await self._lock_operation(session, operation_id)
            replay = await self._claim_operation(
                session, operation_id, role, name
            )
            if replay is not None:
                if replay.get("ok"):
                    return RecoverSuccess(
                        _endpoint_from_api(replay["endpoint"]),
                        replayed=True,
                    )
                _raise_from_replay(replay)
            http_status, _message = _safe_error(code)
            await self._complete_operation(
                session,
                operation_id,
                lifecycle="FAILED",
                error_code=code,
                http_status=http_status,
            )
        return None

    async def _load_operation(self, session, operation_id: uuid.UUID) -> Any | None:
        return (
            (
                await session.execute(
                    text(
                        """
                        SELECT * FROM travel_llm_endpoint_recovery_operation
                        WHERE operation_id = :operation_id
                        FOR UPDATE
                        """
                    ),
                    {"operation_id": operation_id},
                )
            )
            .mappings()
            .first()
        )

    async def load_operation_replay(
        self, operation_id: uuid.UUID
    ) -> dict[str, Any] | None:
        async with self._session_factory() as session:
            row = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT lifecycle, replay_json
                            FROM travel_llm_endpoint_recovery_operation
                            WHERE operation_id = :operation_id
                            """
                        ),
                        {"operation_id": operation_id},
                    )
                )
                .mappings()
                .first()
            )
        if row is None or str(row["lifecycle"]) == "PENDING":
            return None
        return row["replay_json"]

    async def _complete_operation(
        self,
        session,
        operation_id: uuid.UUID,
        *,
        lifecycle: str,
        error_code: str | None = None,
        http_status: int | None = None,
        retry_after: datetime | None = None,
        endpoint: SafeEndpoint | None = None,
    ) -> None:
        if lifecycle == "SUCCEEDED":
            assert endpoint is not None
            payload = _replay_success(endpoint)
            status = 200
            code = None
            message = None
        else:
            code = error_code or "LLM_ENDPOINT_PROBE_FAILED"
            status, message = _safe_error(code)
            if http_status is not None:
                status = http_status
            payload = _replay_error(code, retry_after)
            payload["http_status"] = status
        await session.execute(
            text(
                """
                UPDATE travel_llm_endpoint_recovery_operation
                SET lifecycle = :lifecycle,
                    http_status = :http_status,
                    error_code = :error_code,
                    error_message = :error_message,
                    retry_after = :retry_after,
                    replay_json = CAST(:replay_json AS jsonb),
                    finished_at = NOW(),
                    updated_at = NOW()
                WHERE operation_id = :operation_id
                  AND lifecycle = 'PENDING'
                """
            ),
            {
                "operation_id": operation_id,
                "lifecycle": lifecycle,
                "http_status": status,
                "error_code": code,
                "error_message": message,
                "retry_after": retry_after,
                "replay_json": _json_dumps(payload),
            },
        )


def _json_dumps(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, separators=(",", ":"), default=str)


def _claim_error(status: str) -> tuple[str, int]:
    mapping = {
        "NOT_FOUND": ("LLM_ENDPOINT_NOT_FOUND", 404),
        "DISABLED": ("LLM_ENDPOINT_DISABLED", 409),
        "NOT_RECOVERABLE": ("LLM_ENDPOINT_NOT_RECOVERABLE", 409),
        "BUSY_PROBE": ("LLM_ENDPOINT_RECOVERY_IN_PROGRESS", 409),
        "BUSY": ("LLM_ENDPOINT_BUSY", 409),
    }
    return mapping.get(status, ("LLM_ENDPOINT_NOT_RECOVERABLE", 409))


class InMemoryRoleRelayStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.endpoints: dict[tuple[str, str], _PooledEndpoint] = {}
        self.operations: dict[uuid.UUID, _Operation] = {}

    async def synchronize_direct(self, role: str, name: str, *, provider: str, failure_domain: str, fingerprint: str, qualified_initial: bool = False, has_alternatives: bool = False) -> None:
        role = normalize_role(role)
        async with self._lock:
            previous = self.endpoints.get((role, name))
            if previous is None or previous.fingerprint != fingerprint:
                self.endpoints[(role, name)] = _PooledEndpoint(
                    role=role, name=name, provider=provider, failure_domain=failure_domain,
                    fingerprint=fingerprint, qualification="QUALIFIED" if previous is None and qualified_initial else "UNQUALIFIED",
                    generation=(previous.generation + 1) if previous else 0,
                )
            row = self.endpoints[(role, name)]
            if (not has_alternatives and row.configured
                    and row.participation in {"ACTIVE", "CANARY"} and row.circuit == "OPEN"
                    and row.last_failure_at is not None
                    and row.last_failure_code in SINGLE_PATH_ORDINARY_FAILURES
                    and row.manual_retry_not_before is None and row.manual_retry_level == 0
                    and row.probe_kind is None and row.probe_owner is None
                    and row.active_operation_id is None):
                row.circuit = "CLOSED"
                row.cooldown_until = None
                row.cooldown_level = 0
                row.generation += 1
                row.updated_at = _utcnow()

    async def record_direct_attempt(self, role: str, name: str, **kwargs) -> None:
        kwargs.setdefault("has_alternatives", False)
        await self.record_attempt(role, name, **kwargs)

    async def release_direct_trial(self, role: str, name: str, *, generation: int) -> None:
        async with self._lock:
            row = self.endpoints.get((normalize_role(role), name))
            if row is not None and row.circuit == "HALF_OPEN" and row.generation == generation:
                row.circuit = row.recovery_origin or "OPEN"
                if row.qualification == "UNQUALIFIED" and row.cooldown_until is None:
                    row.circuit = "CLOSED"
                self._clear_probe(row)
                row.generation += 1

    async def synchronize(self, role: str, fingerprints: dict[str, str]) -> None:
        role = normalize_role(role)
        now = _utcnow()
        async with self._lock:
            self._reclaim(role, now)
            live = set(fingerprints)
            for name, fingerprint in sorted(fingerprints.items()):
                key = (role, name)
                row = self.endpoints.get(key)
                if row is None:
                    self.endpoints[key] = _PooledEndpoint(
                        role=role,
                        name=name,
                        fingerprint=fingerprint,
                        updated_at=now,
                    )
                    continue
                changed = row.fingerprint not in {"", fingerprint}
                reintroduced = not row.configured
                row.configured = True
                if changed or reintroduced:
                    if row.circuit == "HALF_OPEN":
                        self._expire_row(row, now, config_changed=True)
                    row.circuit = "QUARANTINED"
                    row.qualification = "UNQUALIFIED"
                    row.fingerprint = fingerprint
                    row.generation += 1
                    row.updated_at = now
                else:
                    row.fingerprint = fingerprint
                    row.updated_at = now
            for key, row in list(self.endpoints.items()):
                if key[0] != role or key[1] in live:
                    continue
                row.configured = False
                if row.circuit == "HALF_OPEN":
                    self._expire_row(row, now, config_changed=True)
                row.updated_at = now

    def _reclaim(self, role: str | None, now: datetime) -> None:
        for row in self.endpoints.values():
            if role is not None and row.role != role:
                continue
            if (
                row.circuit == "HALF_OPEN"
                and row.probe_until is not None
                and row.probe_until <= now
            ):
                self._expire_row(row, now)

    def _expire_row(
        self,
        row: _PooledEndpoint,
        now: datetime,
        *,
        config_changed: bool = False,
    ) -> None:
        origin = row.recovery_origin or "OPEN"
        is_manual = row.probe_kind == "manual"
        operation_id = row.active_operation_id
        row.circuit = "QUARANTINED" if config_changed else origin
        if row.circuit == "QUARANTINED":
            row.qualification = "UNQUALIFIED"
        row.transient_failures = max(
            row.transient_failures, TRANSIENT_FAILURE_THRESHOLD
        )
        row.cooldown_until = now + timedelta(
            seconds=COOLDOWN_SECONDS[min(row.cooldown_level, 2)]
        )
        row.cooldown_level = min(2, row.cooldown_level + 1)
        if is_manual:
            row.manual_retry_not_before = now + timedelta(
                seconds=COOLDOWN_SECONDS[min(row.manual_retry_level, 2)]
            )
            row.manual_retry_level = min(2, row.manual_retry_level + 1)
        self._clear_probe(row)
        row.generation += 1
        row.updated_at = now
        if operation_id is not None:
            lifecycle = "CONFIGURATION_CHANGED" if config_changed else "EXPIRED"
            code = (
                "LLM_ENDPOINT_NOT_RECOVERABLE"
                if config_changed
                else "LLM_ENDPOINT_PROBE_TIMEOUT"
            )
            self._complete_memory_operation(operation_id, lifecycle, code, now)

    def _clear_probe(self, row: _PooledEndpoint) -> None:
        row.probe_kind = None
        row.probe_owner = None
        row.probe_until = None
        row.recovery_origin = None
        row.active_operation_id = None

    async def list_projections(
        self, role: str, models: dict[str, str]
    ) -> list[SafeEndpoint]:
        role = normalize_role(role)
        now = _utcnow()
        async with self._lock:
            self._reclaim(role, now)
            half_open = any(
                row.role == role and row.circuit == "HALF_OPEN"
                for row in self.endpoints.values()
            )
            items = []
            for row in sorted(
                self.endpoints.values(), key=lambda item: item.name
            ):
                if row.role != role or not row.configured:
                    continue
                model = models.get(row.name)
                if not model:
                    continue
                items.append(
                    _to_safe_pooled(
                        row, model=model, now=now, half_open=half_open
                    )
                )
            return items

    async def admit(
        self,
        role: str,
        names: tuple[str, ...],
        *,
        allow_recovery: bool = True,
    ) -> tuple[tuple[str, ...], str | None, int | dict[str, int] | None]:
        role = normalize_role(role)
        now = _utcnow()
        async with self._lock:
            self._reclaim(role, now)
            half_open = any(
                row.role == role and row.circuit == "HALF_OPEN"
                for row in self.endpoints.values()
            )
            closed: list[str] = []
            recovery: list[str] = []
            for name in names:
                row = self.endpoints.get((role, name))
                if (
                    row is None
                    or not row.configured
                    or row.participation == "DISABLED"
                ):
                    continue
                cooldown_ready = (
                    row.cooldown_until is None or row.cooldown_until <= now
                )
                if row.circuit == "CLOSED" and row.qualification != "QUALIFIED" and row.provider != "unknown" and not half_open:
                    recovery.append(name)
                elif row.circuit == "CLOSED" and row.qualification == "QUALIFIED":
                    closed.append(name)
                elif row.circuit == "OPEN" and cooldown_ready and not half_open:
                    recovery.append(name)
            if allow_recovery and recovery:
                selected = recovery[0]
                row = self.endpoints[(role, selected)]
                row.circuit = "HALF_OPEN"
                row.probe_kind = "automatic"
                row.probe_owner = f"automatic:{uuid.uuid4()}"
                row.probe_until = now + timedelta(seconds=PROBE_LEASE_SECONDS)
                row.recovery_origin = "OPEN"
                row.active_operation_id = None
                row.generation += 1
                row.updated_at = now
                return (selected,), selected, row.generation
            generation = (
                self.endpoints[(role, closed[0])].generation if len(closed) == 1
                else {name: self.endpoints[(role, name)].generation for name in closed}
            )
            return tuple(closed), None, generation

    async def record_attempt(
        self,
        role: str,
        name: str,
        *,
        success: bool,
        failure_class: str | None = None,
        failure_scope: str | None = None,
        retry_after_seconds: int | None = None,
        trial: bool = False,
        generation: int | None = None,
        has_alternatives: bool = True,
    ) -> None:
        role = normalize_role(role)
        now = _utcnow()
        async with self._lock:
            if has_alternatives:
                self._reclaim(role, now)
            row = self.endpoints.get((role, name))
            if row is None:
                return
            if generation is not None and row.generation != generation:
                return
            if not trial and row.circuit != "CLOSED":
                return
            if success:
                row.transient_failures = 0
                if trial and row.circuit == "HALF_OPEN":
                    row.circuit = "CLOSED"
                    row.qualification = "QUALIFIED"
                    row.cooldown_level = 0
                    row.cooldown_until = None
                    row.manual_retry_level = 0
                    row.manual_retry_not_before = None
                    self._clear_probe(row)
                    row.generation += 1
                row.updated_at = now
                row.last_failure_code = None
                return
            failure_class = str(failure_class or "TRANSPORT")
            if not has_alternatives and failure_class not in LONG_FAILURE_CLASSES:
                if trial and row.probe_kind != "automatic":
                    return
                row.circuit = "CLOSED"
                row.cooldown_until = None
                row.cooldown_level = 0
                row.last_failure_at = now
                row.last_failure_code = failure_class
                row.transient_failures += 1
                if trial:
                    self._clear_probe(row)
                    row.generation += 1
                row.updated_at = now
                return
            quarantine = failure_class in QUARANTINE_FAILURE_CLASSES
            counted = failure_class in TRANSIENT_FAILURE_CLASSES
            immediate = failure_class in IMMEDIATE_OPEN_FAILURE_CLASSES
            if counted or immediate:
                row.transient_failures += 1
            else:
                row.transient_failures = 0
            origin = row.recovery_origin or "OPEN"
            open_circuit = bool(
                trial
                or immediate
                or (
                    counted
                    and row.transient_failures >= TRANSIENT_FAILURE_THRESHOLD
                )
            )
            if quarantine or origin == "QUARANTINED":
                row.circuit = "QUARANTINED"
                row.qualification = "UNQUALIFIED"
            elif open_circuit:
                row.circuit = "OPEN"
            if open_circuit or trial:
                row.cooldown_until = now + timedelta(
                    seconds=cooldown_seconds(failure_class, retry_after_seconds)
                )
                row.cooldown_level = min(2, row.cooldown_level + 1)
            if trial and row.probe_kind == "manual":
                row.manual_retry_not_before = now + timedelta(
                    seconds=COOLDOWN_SECONDS[min(row.manual_retry_level, 2)]
                )
                row.manual_retry_level = min(2, row.manual_retry_level + 1)
            row.last_failure_code = failure_class
            row.last_failure_at = now
            if trial:
                self._clear_probe(row)
            if trial or open_circuit:
                row.generation += 1
            row.updated_at = now

    async def claim_manual_probe(
        self,
        role: str,
        name: str,
        *,
        operation_id: uuid.UUID,
        owner: str,
    ) -> tuple[str, int | None]:
        role = normalize_role(role)
        now = _utcnow()
        async with self._lock:
            self._reclaim(role, now)
            existing = self.operations.get(operation_id)
            if existing is not None:
                if existing.role != role or existing.endpoint_name != name:
                    raise RecoverError(
                        "LLM_ENDPOINT_OPERATION_CONFLICT",
                        http_status=409,
                        message="operation id conflict",
                        replayed=True,
                    )
                if existing.lifecycle == "PENDING":
                    raise RecoverError(
                        "LLM_ENDPOINT_RECOVERY_IN_PROGRESS",
                        http_status=409,
                        message="endpoint recovery is in progress",
                        replayed=True,
                    )
                if existing.replay_json and existing.replay_json.get("ok"):
                    return "REPLAY", None
                _raise_from_replay(existing.replay_json or {})
            self.operations[operation_id] = _Operation(
                operation_id, role, name, "PENDING"
            )
            row = self.endpoints.get((role, name))
            status = self._eligibility(row, now)
            if status != "CLAIMED":
                code, http_status = _claim_error(status)
                retry_after = None
                if (
                    status == "NOT_RECOVERABLE"
                    and row is not None
                    and row.manual_retry_not_before
                    and row.manual_retry_not_before > now
                ):
                    retry_after = row.manual_retry_not_before
                self._complete_memory_operation(
                    operation_id,
                    "FAILED",
                    code,
                    now,
                    http_status=http_status,
                    retry_after=retry_after,
                )
                return status, None
            assert row is not None
            half_open = any(
                item.role == role and item.circuit == "HALF_OPEN"
                for item in self.endpoints.values()
            )
            if half_open:
                self._complete_memory_operation(
                    operation_id,
                    "FAILED",
                    "LLM_ENDPOINT_RECOVERY_IN_PROGRESS",
                    now,
                )
                return "BUSY_PROBE", None
            row.recovery_origin = row.circuit
            row.circuit = "HALF_OPEN"
            row.probe_kind = "manual"
            row.probe_owner = owner
            row.probe_until = now + timedelta(seconds=PROBE_LEASE_SECONDS)
            row.active_operation_id = operation_id
            row.generation += 1
            row.updated_at = now
            return "CLAIMED", row.generation

    def _eligibility(self, row: _PooledEndpoint | None, now: datetime) -> str:
        if row is None or not row.configured:
            return "NOT_FOUND"
        if row.participation == "DISABLED":
            return "DISABLED"
        if row.manual_retry_not_before and row.manual_retry_not_before > now:
            return "NOT_RECOVERABLE"
        if row.circuit == "CLOSED":
            return "NOT_RECOVERABLE"
        if row.circuit == "HALF_OPEN":
            return "BUSY_PROBE"
        if row.circuit not in {"OPEN", "QUARANTINED"}:
            return "NOT_RECOVERABLE"
        return "CLAIMED"

    async def finish_manual_probe(
        self,
        role: str,
        name: str,
        *,
        operation_id: uuid.UUID,
        owner: str,
        generation: int,
        success: bool,
        failure_class: str | None,
        endpoint: SafeEndpoint | None,
    ) -> RecoverSuccess:
        role = normalize_role(role)
        now = _utcnow()
        async with self._lock:
            self._reclaim(role, now)
            row = self.endpoints.get((role, name))
            if (
                row is None
                or row.probe_owner != owner
                or row.generation != generation
            ):
                existing = self.operations.get(operation_id)
                if existing and existing.lifecycle != "PENDING" and existing.replay_json:
                    if existing.replay_json.get("ok"):
                        return RecoverSuccess(
                            _endpoint_from_api(existing.replay_json["endpoint"]),
                            replayed=True,
                        )
                    _raise_from_replay(existing.replay_json)
                raise RecoverError(
                    "LLM_ENDPOINT_RECOVERY_IN_PROGRESS",
                    http_status=409,
                    message="endpoint recovery is in progress",
                )
            if success:
                row.circuit = "CLOSED"
                row.qualification = "QUALIFIED"
                row.transient_failures = 0
                row.cooldown_level = 0
                row.cooldown_until = None
                row.manual_retry_level = 0
                row.manual_retry_not_before = None
                self._clear_probe(row)
                row.generation += 1
                row.updated_at = now
                assert endpoint is not None
                finished = SafeEndpoint(
                    role=role,
                    endpoint_id=name,
                    display_name=name,
                    model=endpoint.model,
                    participation=row.participation,
                    qualification=row.qualification,
                    circuit_state=row.circuit,
                    recoverable=False,
                    cooldown_until=None,
                    inflight=None,
                    max_inflight=None,
                    updated_at=now,
                )
                self.operations[operation_id] = _Operation(
                    operation_id,
                    role,
                    name,
                    "SUCCEEDED",
                    http_status=200,
                    replay_json=_replay_success(finished),
                    finished_at=now,
                )
                return RecoverSuccess(finished)
            failure_class = str(failure_class or "TRANSPORT")
            origin = row.recovery_origin or "OPEN"
            if origin == "QUARANTINED" and failure_class not in LONG_FAILURE_CLASSES:
                row.circuit = "QUARANTINED"
                row.qualification = "UNQUALIFIED"
            else:
                row.circuit = "OPEN"
            row.transient_failures = max(
                row.transient_failures, TRANSIENT_FAILURE_THRESHOLD
            )
            row.cooldown_until = now + timedelta(
                seconds=cooldown_seconds(failure_class)
            )
            row.cooldown_level = min(2, row.cooldown_level + 1)
            row.manual_retry_not_before = now + timedelta(
                seconds=cooldown_seconds(failure_class)
            )
            row.manual_retry_level = min(2, row.manual_retry_level + 1)
            self._clear_probe(row)
            row.generation += 1
            row.updated_at = now
            error_code = classify_closed_failure(failure_class)
            self._complete_memory_operation(
                operation_id, "FAILED", error_code, now, retry_after=row.manual_retry_not_before
            )
            status, message = _safe_error(error_code)
            raise RecoverError(
                error_code,
                http_status=status,
                message=message,
                retry_after=row.manual_retry_not_before,
            )

    async def remember_closed_rejection(
        self,
        operation_id: uuid.UUID,
        role: str,
        name: str,
        code: str,
    ) -> RecoverSuccess | None:
        role = normalize_role(role)
        name = str(name or "").strip().lower()
        now = _utcnow()
        async with self._lock:
            existing = self.operations.get(operation_id)
            if existing is not None:
                if existing.role != role or existing.endpoint_name != name:
                    raise RecoverError(
                        "LLM_ENDPOINT_OPERATION_CONFLICT",
                        http_status=409,
                        message="operation id conflict",
                        replayed=True,
                    )
                if existing.lifecycle == "PENDING":
                    raise RecoverError(
                        "LLM_ENDPOINT_RECOVERY_IN_PROGRESS",
                        http_status=409,
                        message="endpoint recovery is in progress",
                        replayed=True,
                    )
                if existing.replay_json and existing.replay_json.get("ok"):
                    return RecoverSuccess(
                        _endpoint_from_api(existing.replay_json["endpoint"]),
                        replayed=True,
                    )
                _raise_from_replay(existing.replay_json or {})
            self.operations[operation_id] = _Operation(
                operation_id, role, name, "PENDING"
            )
            self._complete_memory_operation(operation_id, "FAILED", code, now)
        return None

    async def load_operation_replay(
        self, operation_id: uuid.UUID
    ) -> dict[str, Any] | None:
        current = self.operations.get(operation_id)
        if current is None or current.lifecycle == "PENDING":
            return None
        return current.replay_json

    def _complete_memory_operation(
        self,
        operation_id: uuid.UUID,
        lifecycle: str,
        error_code: str,
        now: datetime,
        *,
        http_status: int | None = None,
        retry_after: datetime | None = None,
    ) -> None:
        status, message = _safe_error(error_code)
        if http_status is not None:
            status = http_status
        payload = _replay_error(error_code, retry_after)
        payload["http_status"] = status
        current = self.operations.get(operation_id)
        if current is None:
            return
        current.lifecycle = lifecycle
        current.http_status = status
        current.error_code = error_code
        current.error_message = message
        current.retry_after = retry_after
        current.replay_json = payload
        current.finished_at = now
