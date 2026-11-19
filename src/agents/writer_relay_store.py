"""PostgreSQL authority for v0.9.4.2+ Writer relay admission.

The ledger stores endpoint names and safe classifications only. Prompts,
consumer input, relay URLs, credentials and raw relay responses never enter it.

An ``OUTCOME_UNKNOWN`` dispatch has two distinguishable causes. The original
v0.9.4.2 form has ``safe_failure_class=PROCESS_LOSS`` because the process lost
the upstream outcome. A v0.9.6 probe-killed dispatch whose connection could not
be confirmed closed keeps ``safe_failure_class=TIMEOUT`` while its
``DISPATCHED`` lease is held pessimistically to expiry, then becomes
``OUTCOME_UNKNOWN``. Neither form is ever resent automatically.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from sqlalchemy import text

from src.pipeline.db import get_session_factory
from src.agents.model_failure_policy import LONG_FAILURE_CLASSES, cooldown_seconds

logger = logging.getLogger(__name__)

# v0.15: shuai is the only writer relay (src/model_routes.py). centos and
# gateai are retired; their rows stay in the table as history.
PARTICIPATING_ENDPOINTS = (
    "shuai",
)
# Optional entries never become required configuration or auto-qualified traffic.
OPTIONAL_ENDPOINT_POLICY: dict[str, tuple[str, int, int]] = {
    "zerocat": ("CANARY", 1, 1),
}
ALLOWED_ENDPOINTS = frozenset(PARTICIPATING_ENDPOINTS) | frozenset(OPTIONAL_ENDPOINT_POLICY)
ACTIVE_ENDPOINTS = frozenset({"shuai"})
CANARY_ENDPOINTS: frozenset[str] = frozenset()
RETIRED_ENDPOINTS = frozenset({"venlacy", "aixoras", "centos", "gateai"})
OWNER_OVERRIDE_PROMOTION_ENDPOINTS = frozenset({"shuai"})
FROZEN_ENDPOINT_POLICY: dict[str, tuple[str, int, int]] = {
    "aixoras": ("CANARY", 2, 2),
    "centos": ("DISABLED", 0, 0),
    "shuai": ("ACTIVE", 3, 3),
    "kuaipao": ("DISABLED", 0, 0),
    "gateai": ("DISABLED", 0, 0),
    "venlacy": ("DISABLED", 0, 0),
    "keungliang": ("DISABLED", 0, 0),
    "4router": ("DISABLED", 0, 0),
}
TERMINAL_DISPATCH_STATES = frozenset({"SUCCEEDED", "FAILED", "OUTCOME_UNKNOWN"})
COOLDOWN_SECONDS = (120, 120, 120)
TRANSIENT_FAILURE_THRESHOLD = 2
TRANSIENT_FAILURE_CLASSES = frozenset(
    {"TRANSPORT", "TIMEOUT", "HTTP_5XX", "WIRE_INVALID"}
)
IMMEDIATE_OPEN_FAILURE_CLASSES = frozenset({"RATE_LIMIT", "TRANSPORT", "TIMEOUT", "HTTP_5XX", "UNKNOWN", "POLICY_REJECTION"}) | LONG_FAILURE_CLASSES
PENDING_POLICY_COMPENSATION_SECONDS = 180


@dataclass(frozen=True)
class EndpointCapacity:
    name: str
    participation: str
    circuit_state: str
    inflight: int
    cap: int
    pending_policy_count: int = 0


@dataclass(frozen=True)
class DispatchClaim:
    status: Literal["CLAIMED", "BUSY", "UNAVAILABLE", "DUPLICATE"]
    endpoint_name: str | None = None
    dispatch_id: uuid.UUID | None = None
    snapshot: tuple[EndpointCapacity, ...] = ()
    half_open_trial: bool = False


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _validate_promotion(
    name: str,
    row: Any,
    *,
    active_rates: list[float],
    owner_override: bool,
) -> None:
    if str(row["participation"]) != "CANARY":
        raise ValueError("only CANARY endpoints may be promoted")
    if owner_override:
        if name not in OWNER_OVERRIDE_PROMOTION_ENDPOINTS:
            raise ValueError("owner override is authorized only for shuai")
        if str(row["qualification_state"]) != "QUALIFIED":
            raise ValueError("owner override requires a QUALIFIED endpoint")
        if str(row["circuit_state"]) != "CLOSED":
            raise ValueError("owner override requires a CLOSED circuit")
        if int(row["pending_policy_count"]):
            raise ValueError("owner override blocked by pending policy action")
        return

    observations = int(row["observation_count"])
    successes = int(row["transport_success_count"])
    timeouts = int(row["timeout_count"])
    rejected = int(row["auth_policy_rejection_count"])
    valid = int(row["contract_valid_count"])
    if observations < 20:
        raise ValueError("promotion requires at least 20 observations")
    if successes / observations < 0.95 or timeouts / observations > 0.05:
        raise ValueError("promotion transport/timeout threshold not met")
    if rejected:
        raise ValueError("promotion blocked by auth/policy rejection")
    active_rate = sum(active_rates) / len(active_rates) if active_rates else 1.0
    if valid / observations < active_rate - 0.05:
        raise ValueError("promotion contract-valid threshold not met")


class PostgresWriterRelayStore:
    """Cross-process single-winner admission backed by PostgreSQL."""

    def __init__(self, session_factory=None) -> None:
        self._session_factory = session_factory or get_session_factory()

    async def synchronize_endpoints(
        self,
        fingerprints: dict[str, str],
        *,
        now: datetime | None = None,
    ) -> None:
        del now  # PostgreSQL uses the database clock for authoritative leases.
        async with self._session_factory() as session, session.begin():
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": "writer-relay-admission-v0942"},
            )
            await self._reap_expired(session)
            policies = {
                **FROZEN_ENDPOINT_POLICY,
                **{
                    name: policy
                    for name, policy in OPTIONAL_ENDPOINT_POLICY.items()
                    if name in fingerprints
                },
            }
            for name, (participation, cap, weight) in policies.items():
                await session.execute(
                    text(
                        """
                        INSERT INTO travel_relay_endpoint_state (
                            endpoint_name, participation, previous_participation,
                            max_inflight, routing_weight, qualification_state
                        ) VALUES (
                            :name, :participation, :participation,
                            :cap, :weight, :qualification
                        )
                        ON CONFLICT (endpoint_name) DO NOTHING
                        """
                    ),
                    {
                        "name": name,
                        "participation": participation,
                        "cap": cap,
                        "weight": weight,
                        "qualification": (
                            "QUALIFIED"
                            if name in PARTICIPATING_ENDPOINTS
                            else "UNQUALIFIED"
                        ),
                    },
                )
            for name in RETIRED_ENDPOINTS:
                await session.execute(
                    text(
                        """
                        UPDATE travel_relay_endpoint_state
                        SET participation = 'DISABLED',
                            previous_participation = 'DISABLED',
                            max_inflight = 0,
                            routing_weight = 0,
                            qualification_state = 'UNQUALIFIED',
                            circuit_state = 'CLOSED',
                            consecutive_transient_failures = 0,
                            cooldown_until = NULL,
                            probe_kind = NULL,
                            probe_owner = NULL,
                            probe_lease_expires_at = NULL,
                            recovery_origin_circuit_state = NULL,
                            active_operation_id = NULL,
                            updated_at = NOW()
                        WHERE endpoint_name = :name
                        """
                    ),
                    {"name": name},
                )
            for name, fingerprint in fingerprints.items():
                row = (
                    (
                        await session.execute(
                            text(
                                """
                                SELECT configuration_fingerprint, circuit_state,
                                       active_operation_id
                                FROM travel_relay_endpoint_state
                                WHERE endpoint_name = :name
                                FOR UPDATE
                                """
                            ),
                            {"name": name},
                        )
                    )
                    .mappings()
                    .first()
                )
                if row is None:
                    continue
                prior = str(row["configuration_fingerprint"] or "")
                identity_changed = prior not in {"", fingerprint}
                if not identity_changed:
                    await session.execute(
                        text(
                            """
                            UPDATE travel_relay_endpoint_state
                            SET configuration_fingerprint = :fingerprint,
                                updated_at = NOW()
                            WHERE endpoint_name = :name
                            """
                        ),
                        {"name": name, "fingerprint": fingerprint},
                    )
                    continue
                operation_id = row["active_operation_id"]
                await session.execute(
                    text(
                        """
                        UPDATE travel_relay_endpoint_state
                        SET configuration_fingerprint = :fingerprint,
                            qualification_state = 'UNQUALIFIED',
                            circuit_state = 'QUARANTINED',
                            probe_kind = NULL,
                            probe_owner = NULL,
                            probe_lease_expires_at = NULL,
                            recovery_origin_circuit_state = NULL,
                            active_operation_id = NULL,
                            state_generation = state_generation + 1,
                            updated_at = NOW()
                        WHERE endpoint_name = :name
                        """
                    ),
                    {"name": name, "fingerprint": fingerprint},
                )
                if operation_id is not None:
                    await self._complete_recovery_operation(
                        session,
                        uuid.UUID(str(operation_id)),
                        lifecycle="CONFIGURATION_CHANGED",
                        error_code="LLM_ENDPOINT_NOT_RECOVERABLE",
                    )
            await self._compensate_stale_policy_rejections(session)

    async def _compensate_stale_policy_rejections(self, session) -> None:
        rows = (
            (
                await session.execute(
                    text(
                        """
                        WITH finalized AS (
                            UPDATE travel_writer_dispatch
                            SET safe_failure_class = 'POLICY_REJECTION_APPLIED',
                                updated_at = NOW()
                            WHERE dispatch_id IN (
                                SELECT dispatch_id
                                FROM travel_writer_dispatch
                                WHERE safe_failure_class = 'POLICY_REJECTION'
                                  AND updated_at <= NOW() - make_interval(secs => :stale_seconds)
                                FOR UPDATE SKIP LOCKED
                            )
                            RETURNING endpoint_name
                        ), counts AS (
                            SELECT endpoint_name, COUNT(*)::INT AS failure_count
                            FROM finalized
                            GROUP BY endpoint_name
                        )
                        UPDATE travel_relay_endpoint_state AS endpoint
                        SET pending_policy_count = GREATEST(
                                0, endpoint.pending_policy_count - counts.failure_count
                            ),
                            observation_count = endpoint.observation_count + counts.failure_count,
                            auth_policy_rejection_count =
                                endpoint.auth_policy_rejection_count + counts.failure_count,
                            circuit_state = CASE
                                WHEN endpoint.circuit_state IN ('CLOSED', 'OPEN') THEN 'OPEN'
                                ELSE endpoint.circuit_state
                            END,
                            cooldown_until = CASE
                                WHEN endpoint.circuit_state IN ('CLOSED', 'OPEN')
                                    THEN GREATEST(endpoint.cooldown_until, NOW() + INTERVAL '120 seconds')
                                ELSE endpoint.cooldown_until
                            END,
                            updated_at = NOW()
                        FROM counts
                        WHERE endpoint.endpoint_name = counts.endpoint_name
                        RETURNING endpoint.endpoint_name, counts.failure_count
                        """
                    ),
                    {"stale_seconds": PENDING_POLICY_COMPENSATION_SECONDS},
                )
            )
            .mappings()
            .all()
        )
        for row in rows:
            logger.warning(
                "writer_relay_pending_policy_compensated endpoint=%s count=%s",
                row["endpoint_name"],
                row["failure_count"],
            )

    async def _reap_expired(self, session) -> None:
        expired = list(
            (
                await session.execute(
                    text(
                        """
                        UPDATE travel_writer_dispatch
                        SET state = CASE
                                WHEN state = 'DISPATCHED' THEN 'OUTCOME_UNKNOWN'
                                ELSE 'FAILED'
                            END,
                            safe_failure_class = CASE
                                WHEN safe_failure_class = 'STANDBY_READY' THEN 'STANDBY_READY'
                                WHEN state = 'DISPATCHED' THEN COALESCE(
                                    safe_failure_class, 'PROCESS_LOSS'
                                )
                                ELSE 'CLAIM_EXPIRED'
                            END,
                            finished_at = NOW(), updated_at = NOW()
                        WHERE state IN ('CLAIMED', 'DISPATCHED')
                          AND lease_expires_at <= NOW()
                        RETURNING endpoint_name, half_open_trial, safe_failure_class
                        """
                    )
                )
            ).mappings()
        )
        half_open_endpoints = sorted(
            {
                str(row["endpoint_name"])
                for row in expired
                if bool(row["half_open_trial"])
                and row["safe_failure_class"] != "STANDBY_READY"
            }
        )
        if half_open_endpoints:
            await session.execute(
                text(
                    """
                    UPDATE travel_relay_endpoint_state
                    SET circuit_state = CASE
                            WHEN recovery_origin_circuit_state IN ('OPEN', 'QUARANTINED')
                                THEN recovery_origin_circuit_state
                            WHEN circuit_state = 'QUARANTINED' THEN circuit_state
                            ELSE 'OPEN'
                        END,
                        consecutive_transient_failures = GREATEST(
                            consecutive_transient_failures,
                            :transient_threshold
                        ),
                        cooldown_until = CASE
                            WHEN circuit_state = 'QUARANTINED'
                                 AND COALESCE(
                                     recovery_origin_circuit_state, 'OPEN'
                                 ) = 'QUARANTINED'
                                THEN cooldown_until
                            ELSE NOW() + make_interval(
                                secs => CASE cooldown_level
                                    WHEN 0 THEN 120
                                    WHEN 1 THEN 120
                                    ELSE 120
                                END
                            )
                        END,
                        cooldown_level = CASE
                            WHEN circuit_state = 'QUARANTINED'
                                 AND COALESCE(
                                     recovery_origin_circuit_state, 'OPEN'
                                 ) = 'QUARANTINED'
                                THEN cooldown_level
                            ELSE LEAST(2, cooldown_level + 1)
                        END,
                        probe_kind = NULL,
                        probe_owner = NULL,
                        probe_lease_expires_at = NULL,
                        recovery_origin_circuit_state = NULL,
                        active_operation_id = NULL,
                        state_generation = state_generation + 1,
                        updated_at = NOW()
                    WHERE endpoint_name = ANY(:names)
                      AND circuit_state = 'HALF_OPEN'
                    """
                ),
                {
                    "names": half_open_endpoints,
                    "transient_threshold": TRANSIENT_FAILURE_THRESHOLD,
                },
            )
            logger.warning(
                "writer_relay_half_open_lease_expired endpoints=%s",
                ",".join(half_open_endpoints),
            )
        # Older versions could finish a policy-rejected dispatch before
        # releasing its automatic probe. Recover only an expired orphan;
        # a new lease or any live dispatch remains authoritative.
        await session.execute(
            text("""
                UPDATE travel_relay_endpoint_state AS endpoint
                SET circuit_state = 'OPEN',
                    cooldown_until = GREATEST(cooldown_until, NOW() + INTERVAL '120 seconds'),
                    consecutive_transient_failures = GREATEST(consecutive_transient_failures, :threshold),
                    cooldown_level = LEAST(2, cooldown_level + 1),
                    probe_kind = NULL, probe_owner = NULL,
                    probe_lease_expires_at = NULL,
                    recovery_origin_circuit_state = NULL,
                    state_generation = state_generation + 1,
                    updated_at = NOW()
                WHERE circuit_state = 'HALF_OPEN' AND probe_kind = 'automatic'
                  AND probe_lease_expires_at <= NOW()
                  AND recovery_origin_circuit_state = 'OPEN'
                  AND participation IN ('ACTIVE', 'CANARY')
                  AND configuration_fingerprint <> ''
                  AND active_operation_id IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM travel_writer_dispatch AS dispatch
                      WHERE dispatch.endpoint_name = endpoint.endpoint_name
                        AND dispatch.state IN ('CLAIMED', 'DISPATCHED')
                        AND dispatch.lease_expires_at > NOW()
                  )
            """),
            {"threshold": TRANSIENT_FAILURE_THRESHOLD},
        )
        stale_manual_probes = list(
            (
                await session.execute(
                    text(
                        """
                        WITH stale AS (
                            SELECT endpoint_name, active_operation_id
                            FROM travel_relay_endpoint_state
                            WHERE circuit_state = 'HALF_OPEN'
                              AND probe_kind = 'manual'
                              AND probe_owner IS NOT NULL
                              AND probe_lease_expires_at <= NOW()
                            FOR UPDATE
                        ), updated AS (
                            UPDATE travel_relay_endpoint_state AS endpoint
                            SET circuit_state = CASE
                                    WHEN recovery_origin_circuit_state IN (
                                        'OPEN', 'QUARANTINED'
                                    )
                                        THEN recovery_origin_circuit_state
                                    ELSE 'OPEN'
                                END,
                                consecutive_transient_failures = GREATEST(
                                    consecutive_transient_failures,
                                    :transient_threshold
                                ),
                                cooldown_until = NOW() + make_interval(
                                    secs => CASE cooldown_level
                                        WHEN 0 THEN 120
                                        WHEN 1 THEN 120
                                        ELSE 120
                                    END
                                ),
                                cooldown_level = LEAST(2, cooldown_level + 1),
                                manual_retry_not_before = CASE
                                    WHEN probe_kind = 'manual'
                                        THEN NOW() + make_interval(
                                            secs => CASE manual_retry_level
                                                WHEN 0 THEN 120
                                                WHEN 1 THEN 120
                                                ELSE 120
                                            END
                                        )
                                    ELSE manual_retry_not_before
                                END,
                                manual_retry_level = CASE
                                    WHEN probe_kind = 'manual'
                                        THEN LEAST(2, manual_retry_level + 1)
                                    ELSE manual_retry_level
                                END,
                                probe_kind = NULL,
                                probe_owner = NULL,
                                probe_lease_expires_at = NULL,
                                recovery_origin_circuit_state = NULL,
                                active_operation_id = NULL,
                                state_generation = state_generation + 1,
                                updated_at = NOW()
                            FROM stale
                            WHERE endpoint.endpoint_name = stale.endpoint_name
                            RETURNING endpoint.endpoint_name,
                                      stale.active_operation_id
                        )
                        SELECT * FROM updated
                        """
                    ),
                    {"transient_threshold": TRANSIENT_FAILURE_THRESHOLD},
                )
            ).mappings()
        )
        if stale_manual_probes:
            logger.warning(
                "writer_relay_manual_probe_expired endpoints=%s",
                ",".join(
                    sorted(str(row["endpoint_name"]) for row in stale_manual_probes)
                ),
            )
            for row in stale_manual_probes:
                operation_id = row["active_operation_id"]
                if operation_id is None:
                    continue
                await session.execute(
                    text(
                        """
                        UPDATE travel_llm_endpoint_recovery_operation
                        SET lifecycle = 'EXPIRED',
                            http_status = 504,
                            error_code = 'LLM_ENDPOINT_PROBE_TIMEOUT',
                            error_message = 'endpoint probe timed out',
                            replay_json = CAST(:replay AS jsonb),
                            finished_at = NOW(),
                            updated_at = NOW()
                        WHERE operation_id = :operation_id
                          AND lifecycle = 'PENDING'
                        """
                    ),
                    {
                        "operation_id": operation_id,
                        "replay": (
                            '{"ok":false,"http_status":504,'
                            '"error_code":"LLM_ENDPOINT_PROBE_TIMEOUT",'
                            '"message":"endpoint probe timed out"}'
                        ),
                    },
                )

    async def claim(
        self,
        *,
        logical_call_id: uuid.UUID,
        ordinal: int,
        job_id: str | None,
        call_reason: str,
        model: str,
        release_identity: str,
        fingerprints: dict[str, str],
        initial: bool,
        exclude_endpoints: frozenset[str],
        lease_seconds: float,
        now: datetime | None = None,
    ) -> DispatchClaim:
        del now
        async with self._session_factory() as session, session.begin():
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": "writer-relay-admission-v0942"},
            )
            await self._reap_expired(session)
            await self._compensate_stale_policy_rejections(session)
            existing = await session.execute(
                text(
                    """
                    SELECT dispatch_id, endpoint_name
                    FROM travel_writer_dispatch
                    WHERE logical_call_id = :logical_call_id AND ordinal = :ordinal
                    """
                ),
                {"logical_call_id": logical_call_id, "ordinal": ordinal},
            )
            duplicate = existing.mappings().first()
            if duplicate is not None:
                return DispatchClaim(
                    "DUPLICATE",
                    endpoint_name=str(duplicate["endpoint_name"]),
                    dispatch_id=duplicate["dispatch_id"],
                )
            rows = list(
                (
                    await session.execute(
                        text(
                            """
                            SELECT endpoint_name, participation, circuit_state,
                                   qualification_state, max_inflight,
                                   routing_weight, configuration_fingerprint,
                                   selection_current, pending_policy_count,
                                   observation_count, transport_success_count,
                                   cooldown_until,
                                   (cooldown_until IS NULL OR cooldown_until <= NOW())
                                       AS cooldown_ready
                            FROM travel_relay_endpoint_state
                            WHERE endpoint_name = ANY(:names)
                            FOR UPDATE
                            """
                        ),
                        {"names": list(fingerprints)},
                    )
                ).mappings()
            )
            inflight_rows = list(
                (
                    await session.execute(
                        text(
                            """
                            SELECT endpoint_name, COUNT(*) AS inflight
                            FROM travel_writer_dispatch
                            WHERE state IN ('CLAIMED', 'DISPATCHED')
                              AND lease_expires_at > NOW()
                            GROUP BY endpoint_name
                            """
                        )
                    )
                ).mappings()
            )
            inflight = {
                str(row["endpoint_name"]): int(row["inflight"]) for row in inflight_rows
            }
            snapshot = tuple(
                EndpointCapacity(
                    name=str(row["endpoint_name"]),
                    participation=str(row["participation"]),
                    circuit_state=str(row["circuit_state"]),
                    inflight=inflight.get(str(row["endpoint_name"]), 0),
                    cap=int(row["max_inflight"]),
                    pending_policy_count=int(row["pending_policy_count"]),
                )
                for row in rows
            )
            for item in snapshot:
                if item.inflight > item.cap:
                    logger.critical(
                        "writer_relay_cap_breach endpoint=%s inflight=%s cap=%s",
                        item.name,
                        item.inflight,
                        item.cap,
                    )
            admitted_participation = (
                {"ACTIVE", "CANARY"} if initial else {"ACTIVE"}
            )

            def common_eligible(row: Any) -> bool:
                name = str(row["endpoint_name"])
                participation = str(row["participation"])
                if name in exclude_endpoints:
                    return False
                if participation not in admitted_participation:
                    return False
                if str(row["qualification_state"]) != "QUALIFIED" and str(row["circuit_state"]) != "OPEN":
                    return False
                if int(row["pending_policy_count"]) > 0:
                    return False
                if str(row["configuration_fingerprint"]) != fingerprints.get(name):
                    return False
                return True

            half_open_exists = bool(
                await session.scalar(
                    text(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM travel_relay_endpoint_state
                            WHERE circuit_state = 'HALF_OPEN'
                        )
                        """
                    )
                )
            )
            recovery_candidates = []
            if initial and not half_open_exists:
                recovery_candidates = [
                    row
                    for row in rows
                    if common_eligible(row)
                    and str(row["circuit_state"]) == "OPEN"
                    and bool(row["cooldown_ready"])
                    and inflight.get(str(row["endpoint_name"]), 0) == 0
                ]

            dispatch_id = uuid.uuid4()
            half_open_trial = bool(recovery_candidates)
            if half_open_trial:
                minimum_time = datetime.min.replace(tzinfo=timezone.utc)

                def recovery_order(row: Any) -> tuple[Any, ...]:
                    observations = max(1, int(row["observation_count"]))
                    success_rate = int(row["transport_success_count"]) / observations
                    return (
                        0 if str(row["participation"]) == "ACTIVE" else 1,
                        -success_rate,
                        row["cooldown_until"] or minimum_time,
                        str(row["endpoint_name"]),
                    )

                selected = sorted(recovery_candidates, key=recovery_order)[0]
                selected_name = str(selected["endpoint_name"])
                probe_owner = str(dispatch_id)
                await session.execute(
                    text(
                        """
                        UPDATE travel_relay_endpoint_state
                        SET circuit_state = 'HALF_OPEN',
                            selection_count = selection_count + 1,
                            peak_inflight = GREATEST(peak_inflight, :peak_inflight),
                            probe_kind = 'automatic',
                            probe_owner = :probe_owner,
                            probe_lease_expires_at = NOW()
                                + make_interval(secs => :lease_seconds),
                            recovery_origin_circuit_state = 'OPEN',
                            active_operation_id = NULL,
                            state_generation = state_generation + 1,
                            updated_at = NOW()
                        WHERE endpoint_name = :name AND circuit_state = 'OPEN'
                        """
                    ),
                    {
                        "name": selected_name,
                        "peak_inflight": 1,
                        "probe_owner": probe_owner,
                        "lease_seconds": float(lease_seconds),
                    },
                )
                logger.warning(
                    "writer_relay_half_open_started endpoint=%s",
                    selected_name,
                )
            else:
                eligible = [
                    row
                    for row in rows
                    if common_eligible(row)
                    and str(row["circuit_state"]) == "CLOSED"
                ]
                if not eligible:
                    return DispatchClaim("UNAVAILABLE", snapshot=snapshot)
                free = [
                    row
                    for row in eligible
                    if inflight.get(str(row["endpoint_name"]), 0)
                    < int(row["max_inflight"])
                ]
                if not free:
                    return DispatchClaim("BUSY", snapshot=snapshot)

                total_weight = sum(int(row["routing_weight"]) for row in free)
                scored = [
                    (
                        int(row["selection_current"])
                        + int(row["routing_weight"]),
                        str(row["endpoint_name"]),
                        row,
                    )
                    for row in free
                ]
                _score, selected_name, _selected = max(
                    scored, key=lambda item: (item[0], item[1])
                )
                for score, name, _row in scored:
                    await session.execute(
                        text(
                            """
                            UPDATE travel_relay_endpoint_state
                            SET selection_current = :score,
                                selection_count = selection_count + :selected,
                                peak_inflight = GREATEST(
                                    peak_inflight, :peak_inflight
                                ),
                                updated_at = NOW()
                            WHERE endpoint_name = :name
                            """
                        ),
                        {
                            "name": name,
                            "score": score - total_weight
                            if name == selected_name
                            else score,
                            "selected": 1 if name == selected_name else 0,
                            "peak_inflight": (
                                inflight.get(name, 0) + 1
                                if name == selected_name
                                else inflight.get(name, 0)
                            ),
                        },
                    )
            await session.execute(
                text(
                    """
                    INSERT INTO travel_writer_dispatch (
                        dispatch_id, job_id, logical_call_id, ordinal,
                        call_reason, endpoint_name, model, release_identity,
                        state, lease_expires_at, half_open_trial
                    ) VALUES (
                        :dispatch_id, :job_id, :logical_call_id, :ordinal,
                        :call_reason, :endpoint_name, :model, :release_identity,
                        'CLAIMED', NOW() + make_interval(secs => :lease_seconds),
                        :half_open_trial
                    )
                    """
                ),
                {
                    "dispatch_id": dispatch_id,
                    "job_id": job_id,
                    "logical_call_id": logical_call_id,
                    "ordinal": ordinal,
                    "call_reason": call_reason[:128],
                    "endpoint_name": selected_name,
                    "model": model,
                    "release_identity": release_identity,
                    "lease_seconds": float(lease_seconds),
                    "half_open_trial": half_open_trial,
                },
            )
            if job_id:
                dispatch_count = await session.scalar(
                    text(
                        "SELECT COUNT(*) FROM travel_writer_dispatch WHERE job_id = :job_id"
                    ),
                    {"job_id": job_id},
                )
                if int(dispatch_count or 0) > 4:
                    logger.warning(
                        "writer_relay_dispatch_count_alert job_id=%s count=%s release=%s",
                        job_id,
                        dispatch_count,
                        release_identity,
                    )
            return DispatchClaim(
                "CLAIMED",
                endpoint_name=selected_name,
                dispatch_id=dispatch_id,
                snapshot=snapshot,
                half_open_trial=half_open_trial,
            )

    async def mark_dispatched(self, dispatch_id: uuid.UUID) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                text(
                    """
                    UPDATE travel_writer_dispatch
                    SET state = 'DISPATCHED', dispatched_at = NOW(), updated_at = NOW()
                    WHERE dispatch_id = :dispatch_id AND state = 'CLAIMED'
                    """
                ),
                {"dispatch_id": dispatch_id},
            )

    async def complete_success(
        self, dispatch_id: uuid.UUID, *, latency_ms: int
    ) -> None:
        async with self._session_factory() as session, session.begin():
            row = (
                (
                    await session.execute(
                        text(
                            """
                        UPDATE travel_writer_dispatch
                        SET state = 'SUCCEEDED', finished_at = NOW(),
                            latency_ms = :latency_ms, updated_at = NOW()
                        WHERE dispatch_id = :dispatch_id AND state = 'DISPATCHED'
                          AND safe_failure_class IS DISTINCT FROM 'STANDBY_READY'
                        RETURNING endpoint_name, half_open_trial
                        """
                        ),
                        {"dispatch_id": dispatch_id, "latency_ms": max(0, latency_ms)},
                    )
                )
                .mappings()
                .first()
            )
            if row is not None:
                await session.execute(
                    text(
                        """
                        UPDATE travel_relay_endpoint_state
                        SET observation_count = observation_count + 1,
                            transport_success_count = transport_success_count + 1,
                            contract_valid_count = contract_valid_count + 1,
                            consecutive_invalid_output = 0,
                            consecutive_transient_failures = 0,
                            circuit_state = CASE
                                WHEN :half_open_trial
                                     AND circuit_state = 'HALF_OPEN'
                                    THEN 'CLOSED'
                                ELSE circuit_state
                            END,
                            qualification_state = CASE
                                WHEN :half_open_trial AND circuit_state = 'HALF_OPEN' THEN 'QUALIFIED'
                                ELSE qualification_state
                            END,
                            cooldown_level = CASE
                                WHEN :half_open_trial THEN 0
                                ELSE cooldown_level
                            END,
                            cooldown_until = CASE
                                WHEN :half_open_trial THEN NULL
                                ELSE cooldown_until
                            END,
                            probe_kind = CASE
                                WHEN :half_open_trial THEN NULL ELSE probe_kind
                            END,
                            probe_owner = CASE
                                WHEN :half_open_trial THEN NULL ELSE probe_owner
                            END,
                            probe_lease_expires_at = CASE
                                WHEN :half_open_trial THEN NULL
                                ELSE probe_lease_expires_at
                            END,
                            recovery_origin_circuit_state = CASE
                                WHEN :half_open_trial THEN NULL
                                ELSE recovery_origin_circuit_state
                            END,
                            active_operation_id = CASE
                                WHEN :half_open_trial THEN NULL
                                ELSE active_operation_id
                            END,
                            state_generation = CASE
                                WHEN :half_open_trial THEN state_generation + 1
                                ELSE state_generation
                            END,
                            updated_at = NOW()
                        WHERE endpoint_name = :name
                        """
                    ),
                    {
                        "name": row["endpoint_name"],
                        "half_open_trial": bool(row["half_open_trial"]),
                    },
                )
                if bool(row["half_open_trial"]):
                    logger.warning(
                        "writer_relay_half_open_succeeded endpoint=%s",
                        row["endpoint_name"],
                    )

    async def cancel_for_standby(
        self, dispatch_id: uuid.UUID, *, release_capacity: bool = False,
    ) -> None:
        """Record intentional cancellation before slow connection/store cleanup.

        An unconfirmed close keeps capacity leased. It is never a health failure;
        the original automatic probe is returned without extending its cooldown.
        The unique probe owner fences later generations and manual recoveries.
        """
        async with self._session_factory() as session, session.begin():
            row = (await session.execute(text("""
                UPDATE travel_writer_dispatch
                SET state = CASE WHEN :release_capacity THEN 'OUTCOME_UNKNOWN' ELSE state END,
                    safe_failure_class = 'STANDBY_READY',
                    finished_at = CASE WHEN :release_capacity THEN NOW() ELSE finished_at END,
                    updated_at = NOW()
                WHERE dispatch_id = :dispatch_id
                  AND state IN ('CLAIMED', 'DISPATCHED')
                  AND (safe_failure_class IS NULL OR safe_failure_class = 'STANDBY_READY')
                RETURNING endpoint_name, half_open_trial
                """), {"dispatch_id": dispatch_id, "release_capacity": release_capacity})).mappings().first()
            if row is not None and bool(row["half_open_trial"]):
                await session.execute(text("""
                    UPDATE travel_relay_endpoint_state
                    SET circuit_state = COALESCE(recovery_origin_circuit_state, 'OPEN'),
                        probe_kind = NULL, probe_owner = NULL, probe_lease_expires_at = NULL,
                        recovery_origin_circuit_state = NULL, active_operation_id = NULL,
                        state_generation = state_generation + 1, updated_at = NOW()
                    WHERE endpoint_name = :name AND circuit_state = 'HALF_OPEN'
                      AND probe_kind = 'automatic' AND probe_owner = :owner
                      AND active_operation_id IS NULL
                    """), {"name": row["endpoint_name"], "owner": str(dispatch_id)})

    async def complete_failure(
        self,
        dispatch_id: uuid.UUID,
        *,
        latency_ms: int,
        failure_class: str,
        http_status_class: str | None = None,
        failure_signature: str | None = None,
        retry_after_seconds: float | None = None,
        transport_succeeded: bool = False,
        release_capacity: bool = True,
    ) -> bool:
        """Record failure and detect a same-call shared request defect.

        ``release_capacity=False`` is reserved for a v0.9.6 probe timeout whose
        transport close could not be confirmed. It records TIMEOUT and applies
        endpoint health attribution immediately, but deliberately leaves the
        dispatch in ``DISPATCHED`` until lease expiry. The reaper then produces
        ``OUTCOME_UNKNOWN + TIMEOUT`` rather than the native process-loss form.
        """
        if not release_capacity and failure_class != "TIMEOUT":
            raise ValueError(
                "release_capacity=False is reserved for probe TIMEOUT"
            )
        async with self._session_factory() as session, session.begin():
            row = (
                (
                    await session.execute(
                        text(
                            """
                        UPDATE travel_writer_dispatch
                        SET state = CASE
                                WHEN :release_capacity THEN 'FAILED'
                                ELSE state
                            END,
                            finished_at = CASE
                                WHEN :release_capacity THEN NOW()
                                ELSE finished_at
                            END,
                            latency_ms = :latency_ms,
                            safe_failure_class = :failure_class,
                            http_status_class = :http_status_class,
                            failure_signature = :failure_signature,
                            updated_at = NOW()
                        WHERE dispatch_id = :dispatch_id
                          AND state IN ('CLAIMED', 'DISPATCHED')
                          AND safe_failure_class IS DISTINCT FROM 'STANDBY_READY'
                          AND (
                              :release_capacity
                              OR (
                                  state = 'DISPATCHED'
                                  AND safe_failure_class IS NULL
                              )
                          )
                        RETURNING endpoint_name, logical_call_id, half_open_trial
                        """
                        ),
                        {
                            "dispatch_id": dispatch_id,
                            "latency_ms": max(0, latency_ms),
                            "failure_class": failure_class[:64],
                            "http_status_class": http_status_class,
                            "failure_signature": failure_signature,
                            "release_capacity": release_capacity,
                        },
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return False
            name = str(row["endpoint_name"])
            if failure_class == "POLICY_REJECTION" and failure_signature:
                if bool(row["half_open_trial"]):
                    # End this automatic trial now. Deferred policy accounting
                    # must not strand a finished dispatch in HALF_OPEN.
                    await session.execute(
                        text("""
                            UPDATE travel_relay_endpoint_state
                            SET circuit_state = 'OPEN',
                                cooldown_until = GREATEST(cooldown_until, NOW() + INTERVAL '120 seconds'),
                                probe_kind = NULL, probe_owner = NULL,
                                probe_lease_expires_at = NULL,
                                recovery_origin_circuit_state = NULL,
                                active_operation_id = NULL,
                                state_generation = state_generation + 1,
                                updated_at = NOW()
                            WHERE endpoint_name = :name AND circuit_state = 'HALF_OPEN'
                              AND probe_kind = 'automatic' AND probe_owner = :owner
                        """),
                        {"name": name, "owner": str(dispatch_id)},
                    )
                await session.execute(
                    text(
                        """
                        UPDATE travel_relay_endpoint_state
                        SET pending_policy_count = pending_policy_count + 1,
                            updated_at = NOW()
                        WHERE endpoint_name = :name
                        """
                    ),
                    {"name": name},
                )
                shared_endpoints = list(
                    (
                        await session.scalars(
                            text(
                                """
                                SELECT DISTINCT endpoint_name
                                FROM travel_writer_dispatch
                                WHERE logical_call_id = :logical_call_id
                                  AND failure_signature = :signature
                                  AND safe_failure_class = 'POLICY_REJECTION'
                                """
                            ),
                            {
                                "logical_call_id": row["logical_call_id"],
                                "signature": failure_signature,
                            },
                        )
                    ).all()
                )
                if len(shared_endpoints) < 2:
                    return False
                await session.execute(
                    text(
                        """
                        UPDATE travel_writer_dispatch
                        SET safe_failure_class = 'SHARED_REQUEST_CONTRACT',
                            updated_at = NOW()
                        WHERE logical_call_id = :logical_call_id
                          AND failure_signature = :signature
                          AND safe_failure_class = 'POLICY_REJECTION'
                        """
                    ),
                    {
                        "logical_call_id": row["logical_call_id"],
                        "signature": failure_signature,
                    },
                )
                await session.execute(
                    text(
                        """
                        UPDATE travel_relay_endpoint_state
                        SET pending_policy_count = GREATEST(
                                0, pending_policy_count - 1
                            ),
                            circuit_state = CASE
                                WHEN circuit_state = 'HALF_OPEN' THEN 'OPEN'
                                ELSE circuit_state
                            END,
                            cooldown_until = CASE
                                WHEN circuit_state = 'HALF_OPEN'
                                    THEN NOW() + INTERVAL '120 seconds'
                                ELSE cooldown_until
                            END,
                            probe_kind = CASE
                                WHEN circuit_state = 'HALF_OPEN' THEN NULL
                                ELSE probe_kind
                            END,
                            probe_owner = CASE
                                WHEN circuit_state = 'HALF_OPEN' THEN NULL
                                ELSE probe_owner
                            END,
                            probe_lease_expires_at = CASE
                                WHEN circuit_state = 'HALF_OPEN' THEN NULL
                                ELSE probe_lease_expires_at
                            END,
                            recovery_origin_circuit_state = CASE
                                WHEN circuit_state = 'HALF_OPEN' THEN NULL
                                ELSE recovery_origin_circuit_state
                            END,
                            active_operation_id = CASE
                                WHEN circuit_state = 'HALF_OPEN' THEN NULL
                                ELSE active_operation_id
                            END,
                            state_generation = CASE
                                WHEN circuit_state = 'HALF_OPEN'
                                    THEN state_generation + 1
                                ELSE state_generation
                            END,
                            updated_at = NOW()
                        WHERE endpoint_name = ANY(:names)
                        """
                    ),
                    {"names": shared_endpoints},
                )
                return True
            endpoint = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT consecutive_invalid_output,
                               consecutive_transient_failures,
                               cooldown_level, circuit_state
                        FROM travel_relay_endpoint_state
                        WHERE endpoint_name = :name FOR UPDATE
                        """
                        ),
                        {"name": name},
                    )
                )
                .mappings()
                .one()
            )
            invalid_count = int(endpoint["consecutive_invalid_output"])
            transient_count = int(endpoint["consecutive_transient_failures"])
            cooldown_level = int(endpoint["cooldown_level"])
            prior_circuit = str(endpoint["circuit_state"])
            half_open_trial = bool(row["half_open_trial"])
            counted_transient = failure_class in TRANSIENT_FAILURE_CLASSES
            immediate_open = failure_class in IMMEDIATE_OPEN_FAILURE_CLASSES
            quarantine = False  # W6 runtime failures expire; config/qualification quarantines remain separate.
            if counted_transient or immediate_open:
                transient_count += 1
            else:
                transient_count = 0
            if failure_class == "OUTPUT_CONTRACT":
                invalid_count += 1
            else:
                invalid_count = 0
            open_circuit = bool(
                half_open_trial
                or immediate_open
                or (
                    counted_transient
                    and transient_count >= TRANSIENT_FAILURE_THRESHOLD
                )
                or (failure_class == "OUTPUT_CONTRACT" and invalid_count >= 3)
            )
            failure_cooldown = cooldown_seconds(failure_class, retry_after_seconds)
            await session.execute(
                text(
                    """
                    UPDATE travel_relay_endpoint_state
                    SET observation_count = observation_count + 1,
                        transport_success_count = transport_success_count + :transport_success_inc,
                        timeout_count = timeout_count + :timeout_inc,
                        auth_policy_rejection_count = auth_policy_rejection_count + :auth_inc,
                        consecutive_invalid_output = :invalid_count,
                        consecutive_transient_failures = :transient_count,
                        circuit_state = CASE
                            WHEN :quarantine THEN 'QUARANTINED'
                            WHEN circuit_state = 'QUARANTINED' THEN circuit_state
                            WHEN :open_circuit THEN 'OPEN'
                            ELSE circuit_state
                        END,
                        qualification_state = CASE
                            WHEN :quarantine THEN 'UNQUALIFIED'
                            ELSE qualification_state
                        END,
                        cooldown_until = CASE
                            WHEN :open_circuit
                                THEN GREATEST(cooldown_until, NOW() + make_interval(secs => :cooldown_seconds))
                            ELSE cooldown_until
                        END,
                        cooldown_level = CASE
                            WHEN :open_circuit THEN LEAST(2, cooldown_level + 1)
                            ELSE cooldown_level
                        END,
                        probe_kind = CASE
                            WHEN :half_open_trial THEN NULL ELSE probe_kind
                        END,
                        probe_owner = CASE
                            WHEN :half_open_trial THEN NULL ELSE probe_owner
                        END,
                        probe_lease_expires_at = CASE
                            WHEN :half_open_trial THEN NULL
                            ELSE probe_lease_expires_at
                        END,
                        recovery_origin_circuit_state = CASE
                            WHEN :half_open_trial THEN NULL
                            ELSE recovery_origin_circuit_state
                        END,
                        active_operation_id = CASE
                            WHEN :half_open_trial THEN NULL
                            ELSE active_operation_id
                        END,
                        state_generation = CASE
                            WHEN :half_open_trial THEN state_generation + 1
                            ELSE state_generation
                        END,
                        updated_at = NOW()
                    WHERE endpoint_name = :name
                    """
                ),
                {
                    "name": name,
                    "transport_success_inc": 1 if transport_succeeded else 0,
                    "timeout_inc": 1 if failure_class == "TIMEOUT" else 0,
                    "auth_inc": 1 if failure_class in LONG_FAILURE_CLASSES else 0,
                    "invalid_count": invalid_count,
                    "transient_count": transient_count,
                    "quarantine": quarantine,
                    "open_circuit": open_circuit,
                    "cooldown_seconds": failure_cooldown,
                    "half_open_trial": half_open_trial,
                },
            )
            next_circuit = (
                "QUARANTINED"
                if quarantine or prior_circuit == "QUARANTINED"
                else "OPEN"
                if open_circuit
                else prior_circuit
            )
            if next_circuit != prior_circuit:
                logger.warning(
                    "writer_relay_circuit_transition endpoint=%s from=%s to=%s class=%s cooldown_seconds=%s",
                    name,
                    prior_circuit,
                    next_circuit,
                    failure_class,
                    failure_cooldown if open_circuit else 0,
                )
            if half_open_trial:
                logger.warning(
                    "writer_relay_half_open_failed endpoint=%s class=%s cooldown_seconds=%s",
                    name,
                    failure_class,
                    failure_cooldown,
                )
            return False

    async def finalize_policy_rejection(self, dispatch_id: uuid.UUID) -> None:
        """Apply one deferred endpoint-policy failure exactly once."""
        async with self._session_factory() as session, session.begin():
            row = (
                (
                    await session.execute(
                        text(
                            """
                            UPDATE travel_writer_dispatch
                            SET safe_failure_class = 'POLICY_REJECTION_APPLIED',
                                updated_at = NOW()
                            WHERE dispatch_id = :dispatch_id
                              AND safe_failure_class = 'POLICY_REJECTION'
                            RETURNING endpoint_name
                            """
                        ),
                        {"dispatch_id": dispatch_id},
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return
            name = str(row["endpoint_name"])
            await session.execute(
                text(
                    """
                    UPDATE travel_relay_endpoint_state
                    SET observation_count = observation_count + 1,
                        auth_policy_rejection_count = auth_policy_rejection_count + 1,
                        pending_policy_count = GREATEST(0, pending_policy_count - 1),
                        circuit_state = CASE WHEN circuit_state IN ('CLOSED', 'OPEN') THEN 'OPEN' ELSE circuit_state END,
                        cooldown_until = CASE WHEN circuit_state IN ('CLOSED', 'OPEN')
                            THEN GREATEST(cooldown_until, NOW() + INTERVAL '120 seconds') ELSE cooldown_until END,
                        updated_at = NOW()
                    WHERE endpoint_name = :name
                    """
                ),
                {"name": name},
            )
            logger.warning(
                "writer_relay_circuit_transition endpoint=%s to=OPEN class=POLICY_REJECTION",
                name,
            )

    async def list_endpoints(self) -> list[dict[str, Any]]:
        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    """
                    SELECT s.endpoint_name, s.participation, s.circuit_state,
                           s.qualification_state, s.max_inflight, s.routing_weight,
                           s.selection_count, s.peak_inflight, s.observation_count,
                           s.transport_success_count, s.timeout_count,
                           s.auth_policy_rejection_count, s.contract_valid_count,
                           s.pending_policy_count,
                           s.consecutive_transient_failures, s.cooldown_until,
                           s.operator_actor, s.operator_reason,
                           s.operator_updated_at,
                           COUNT(d.dispatch_id) FILTER (
                               WHERE d.state IN ('CLAIMED', 'DISPATCHED')
                                 AND d.lease_expires_at > NOW()
                           ) AS inflight
                    FROM travel_relay_endpoint_state s
                    LEFT JOIN travel_writer_dispatch d
                      ON d.endpoint_name = s.endpoint_name
                    GROUP BY s.endpoint_name
                    ORDER BY s.endpoint_name
                    """
                )
            )
            rows = [dict(row) for row in result.mappings()]
            participating = [
                row
                for row in rows
                if row["endpoint_name"] in PARTICIPATING_ENDPOINTS
                or (
                    row["endpoint_name"] in OPTIONAL_ENDPOINT_POLICY
                    and row["qualification_state"] == "QUALIFIED"
                )
            ]
            total_weight = sum(int(row["routing_weight"]) for row in participating)
            total_selected = sum(int(row["selection_count"]) for row in participating)
            for row in participating:
                target = (
                    int(row["routing_weight"]) / total_weight if total_weight else 0.0
                )
                actual = (
                    int(row["selection_count"]) / total_selected
                    if total_selected
                    else 0.0
                )
                row["target_share"] = target
                row["actual_share"] = actual
                row["share_drift"] = actual - target
            return rows

    async def mutate_endpoint(
        self,
        name: str,
        action: str,
        *,
        actor: str,
        reason: str,
        owner_override: bool = False,
    ) -> None:
        if not actor.strip() or not reason.strip():
            raise ValueError("actor and reason are required")
        name = name.strip().lower()
        if owner_override and action != "promote":
            raise ValueError("owner override is valid only for promote")
        async with self._session_factory() as session, session.begin():
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT * FROM travel_relay_endpoint_state WHERE endpoint_name = :name FOR UPDATE"
                        ),
                        {"name": name},
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise ValueError(f"unknown endpoint: {name}")
            updates: dict[str, Any]
            if action == "disable":
                updates = {"participation": "DISABLED"}
            elif action == "qualify":
                updates = {
                    "qualification_state": "QUALIFIED",
                    "circuit_state": "CLOSED",
                    "consecutive_transient_failures": 0,
                }
            elif action == "promote":
                active_rates = []
                if not owner_override:
                    for active in await self.list_endpoints():
                        if (
                            active["participation"] == "ACTIVE"
                            and active["observation_count"]
                        ):
                            active_rates.append(
                                active["contract_valid_count"]
                                / active["observation_count"]
                            )
                _validate_promotion(
                    name,
                    row,
                    active_rates=active_rates,
                    owner_override=owner_override,
                )
                updates = {
                    "participation": "ACTIVE",
                    "previous_participation": "ACTIVE",
                }
            else:
                raise ValueError(f"unsupported endpoint action: {action}")
            assignments = ", ".join(f"{key} = :{key}" for key in updates)
            await session.execute(
                text(
                    f"""
                    UPDATE travel_relay_endpoint_state
                    SET {assignments}, operator_actor = :actor,
                        operator_reason = :reason, operator_updated_at = NOW(),
                        updated_at = NOW()
                    WHERE endpoint_name = :name
                    """
                ),
                {
                    "name": name,
                    "actor": actor[:128],
                    "reason": (
                        f"[OWNER_OVERRIDE] {reason}" if owner_override else reason
                    )[:500],
                    **updates,
                },
            )
            if owner_override:
                logger.warning(
                    "writer_relay_owner_override endpoint=%s actor=%s reason=%s",
                    name,
                    actor[:128],
                    reason[:500],
                )

    async def claim_probe(
        self,
        name: str,
        owner: str,
        *,
        actor: str = "",
        reason: str = "",
        lease_seconds: int = 60,
    ) -> bool:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": "writer-relay-admission-v0942"},
            )
            await self._reap_expired(session)
            await self._compensate_stale_policy_rejections(session)
            result = await session.execute(
                text(
                    """
                    UPDATE travel_relay_endpoint_state
                    SET circuit_state = 'HALF_OPEN',
                        probe_kind = 'automatic',
                        probe_owner = :owner,
                        probe_lease_expires_at = NOW() + make_interval(secs => :lease_seconds),
                        recovery_origin_circuit_state = CASE
                            WHEN circuit_state IN ('OPEN', 'QUARANTINED')
                                THEN circuit_state
                            ELSE 'OPEN'
                        END,
                        active_operation_id = NULL,
                        state_generation = state_generation + 1,
                        operator_actor = NULLIF(:actor, ''),
                        operator_reason = NULLIF(:reason, ''),
                        operator_updated_at = NOW(),
                        updated_at = NOW()
                    WHERE endpoint_name = :name
                      AND circuit_state <> 'HALF_OPEN'
                      AND (probe_lease_expires_at IS NULL OR probe_lease_expires_at <= NOW())
                      AND (cooldown_until IS NULL OR cooldown_until <= NOW())
                      AND NOT EXISTS (
                          SELECT 1
                          FROM travel_relay_endpoint_state AS active_probe
                          WHERE active_probe.circuit_state = 'HALF_OPEN'
                      )
                    RETURNING endpoint_name
                    """
                ),
                {
                    "name": name.lower(),
                    "owner": owner[:128],
                    "actor": actor[:128],
                    "reason": reason[:500],
                    "lease_seconds": lease_seconds,
                },
            )
            return result.first() is not None

    async def finish_probe(self, name: str, owner: str, *, success: bool) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                text(
                    """
                    UPDATE travel_relay_endpoint_state
                    SET circuit_state = CASE
                            WHEN :success THEN 'CLOSED'
                            WHEN recovery_origin_circuit_state IN (
                                'OPEN', 'QUARANTINED'
                            )
                                THEN recovery_origin_circuit_state
                            ELSE 'OPEN'
                        END,
                        participation = CASE WHEN :success THEN previous_participation ELSE participation END,
                        cooldown_until = CASE
                            WHEN :success THEN NULL
                            ELSE NOW() + make_interval(
                                secs => CASE cooldown_level
                                    WHEN 0 THEN 120
                                    WHEN 1 THEN 120
                                    ELSE 120
                                END
                            )
                        END,
                        cooldown_level = CASE
                            WHEN :success THEN 0
                            ELSE LEAST(2, cooldown_level + 1)
                        END,
                        consecutive_transient_failures = CASE
                            WHEN :success THEN 0
                            ELSE GREATEST(
                                consecutive_transient_failures,
                                :transient_threshold
                            )
                        END,
                        probe_kind = NULL,
                        probe_owner = NULL,
                        probe_lease_expires_at = NULL,
                        recovery_origin_circuit_state = NULL,
                        active_operation_id = NULL,
                        state_generation = state_generation + 1,
                        updated_at = NOW()
                    WHERE endpoint_name = :name AND probe_owner = :owner
                    """
                ),
                {
                    "name": name.lower(),
                    "owner": owner[:128],
                    "success": success,
                    "transient_threshold": TRANSIENT_FAILURE_THRESHOLD,
                },
            )

    async def _complete_recovery_operation(
        self,
        session,
        operation_id: uuid.UUID,
        *,
        lifecycle: str,
        error_code: str,
        http_status: int | None = None,
        retry_after: datetime | None = None,
    ) -> None:
        status, message = {
            "LLM_ENDPOINT_NOT_FOUND": (404, "endpoint was not found"),
            "LLM_ENDPOINT_DISABLED": (409, "endpoint is disabled"),
            "LLM_ENDPOINT_NOT_RECOVERABLE": (
                409,
                "endpoint is not recoverable",
            ),
            "LLM_ENDPOINT_RECOVERY_IN_PROGRESS": (
                409,
                "endpoint recovery is in progress",
            ),
            "LLM_ENDPOINT_BUSY": (409, "endpoint is busy"),
            "INTERNAL_ADMIN_UNAVAILABLE": (
                503,
                "internal admin is unavailable",
            ),
            "LLM_ENDPOINT_PROBE_TIMEOUT": (504, "endpoint probe timed out"),
            "LLM_ENDPOINT_PROBE_FAILED": (502, "endpoint probe failed"),
        }.get(error_code, (409, "endpoint is not recoverable"))
        if http_status is not None:
            status = http_status
        replay: dict[str, Any] = {
            "ok": False,
            "http_status": status,
            "error_code": error_code,
            "message": message,
        }
        if retry_after is not None:
            instant = retry_after
            if instant.tzinfo is None:
                instant = instant.replace(tzinfo=timezone.utc)
            replay["retry_after"] = instant.astimezone(timezone.utc).isoformat().replace(
                "+00:00", "Z"
            )
        await session.execute(
            text(
                """
                UPDATE travel_llm_endpoint_recovery_operation
                SET lifecycle = :lifecycle,
                    http_status = :http_status,
                    error_code = :error_code,
                    error_message = :message,
                    retry_after = :retry_after,
                    replay_json = CAST(:replay AS jsonb),
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
                "error_code": error_code,
                "message": message,
                "retry_after": retry_after,
                "replay": json.dumps(replay, separators=(",", ":")),
            },
        )

    async def list_safe_projections(
        self, models: dict[str, str]
    ) -> list[Any]:
        from src.agents.role_relay_store import SafeEndpoint

        now = _utcnow()
        async with self._session_factory() as session, session.begin():
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": "writer-relay-admission-v0942"},
            )
            await self._reap_expired(session)
            half_open = bool(
                await session.scalar(
                    text(
                        """
                        SELECT EXISTS (
                            SELECT 1 FROM travel_relay_endpoint_state
                            WHERE circuit_state = 'HALF_OPEN'
                        )
                        """
                    )
                )
            )
            rows = list(
                (
                    await session.execute(
                        text(
                            """
                            SELECT s.endpoint_name, s.participation, s.circuit_state,
                                   s.qualification_state, s.max_inflight,
                                   s.cooldown_until, s.manual_retry_not_before,
                                   s.updated_at,
                                   COUNT(d.dispatch_id) FILTER (
                                       WHERE d.state IN ('CLAIMED', 'DISPATCHED')
                                         AND d.lease_expires_at > NOW()
                                   ) AS inflight
                            FROM travel_relay_endpoint_state s
                            LEFT JOIN travel_writer_dispatch d
                              ON d.endpoint_name = s.endpoint_name
                            WHERE s.endpoint_name = ANY(:names)
                            GROUP BY s.endpoint_name
                            ORDER BY s.endpoint_name
                            """
                        ),
                        {"names": list(models)},
                    )
                ).mappings()
            )
        items = []
        for row in rows:
            name = str(row["endpoint_name"])
            model = models.get(name)
            if not model:
                continue
            inflight = int(row["inflight"] or 0)
            cooldown = row["manual_retry_not_before"] or row["cooldown_until"]
            if cooldown is not None and cooldown <= now:
                cooldown = None
            circuit = str(row["circuit_state"])
            recoverable = (
                str(row["participation"]) != "DISABLED"
                and circuit in {"OPEN", "QUARANTINED"}
                and not half_open
                and inflight == 0
                and (
                    row["manual_retry_not_before"] is None
                    or row["manual_retry_not_before"] <= now
                )
            )
            items.append(
                SafeEndpoint(
                    role="writer",
                    endpoint_id=name,
                    display_name=name,
                    model=model,
                    participation=str(row["participation"]),
                    qualification=str(row["qualification_state"]),
                    circuit_state=circuit,
                    recoverable=recoverable,
                    cooldown_until=cooldown,
                    inflight=inflight,
                    max_inflight=int(row["max_inflight"]),
                    updated_at=row["updated_at"] or now,
                )
            )
        return items

    async def claim_manual_recover(
        self,
        name: str,
        *,
        operation_id: uuid.UUID,
        owner: str,
    ) -> tuple[str, int | None]:
        from src.agents.role_relay_store import RecoverError

        name = name.strip().lower()
        async with self._session_factory() as session, session.begin():
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"llm-recover-{operation_id}"},
            )
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": "writer-relay-admission-v0942"},
            )
            await self._reap_expired(session)
            inserted = (
                await session.execute(
                    text(
                        """
                        INSERT INTO travel_llm_endpoint_recovery_operation (
                            operation_id, role, endpoint_name, lifecycle
                        ) VALUES (
                            :operation_id, 'writer', :name, 'PENDING'
                        )
                        ON CONFLICT (operation_id) DO NOTHING
                        RETURNING operation_id
                        """
                    ),
                    {"operation_id": operation_id, "name": name},
                )
            ).first()
            if inserted is None:
                existing = (
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
                if existing is None:
                    return "UNAVAILABLE", None
                if (
                    str(existing["role"]) != "writer"
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
                return "REPLAY", None
            row = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT * FROM travel_relay_endpoint_state
                            WHERE endpoint_name = :name
                            FOR UPDATE
                            """
                        ),
                        {"name": name},
                    )
                )
                .mappings()
                .first()
            )
            status = self._manual_recover_status(session_row=row)
            if status == "CLAIMED":
                half_open = bool(
                    await session.scalar(
                        text(
                            """
                            SELECT EXISTS (
                                SELECT 1 FROM travel_relay_endpoint_state
                                WHERE circuit_state = 'HALF_OPEN'
                            )
                            """
                        )
                    )
                )
                if half_open:
                    status = "BUSY_PROBE"
            if status == "CLAIMED":
                inflight = int(
                    await session.scalar(
                        text(
                            """
                            SELECT COUNT(*) FROM travel_writer_dispatch
                            WHERE endpoint_name = :name
                              AND state IN ('CLAIMED', 'DISPATCHED')
                              AND lease_expires_at > NOW()
                            """
                        ),
                        {"name": name},
                    )
                    or 0
                )
                if inflight > 0:
                    status = "BUSY"
            if status != "CLAIMED":
                code, http_status, _message = _writer_claim_error(status)
                retry_after = None
                if (
                    status == "NOT_RECOVERABLE"
                    and row is not None
                    and row.get("manual_retry_not_before") is not None
                    and row["manual_retry_not_before"] > _utcnow()
                ):
                    retry_after = row["manual_retry_not_before"]
                await self._complete_recovery_operation(
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
                            UPDATE travel_relay_endpoint_state
                            SET circuit_state = 'HALF_OPEN',
                                probe_kind = 'manual',
                                probe_owner = :owner,
                                probe_lease_expires_at = NOW()
                                    + make_interval(secs => 60),
                                recovery_origin_circuit_state = :origin,
                                active_operation_id = :operation_id,
                                state_generation = state_generation + 1,
                                updated_at = NOW()
                            WHERE endpoint_name = :name
                              AND circuit_state IN ('OPEN', 'QUARANTINED')
                            RETURNING state_generation
                            """
                        ),
                        {
                            "name": name,
                            "owner": owner[:128],
                            "origin": origin,
                            "operation_id": operation_id,
                        },
                    )
                )
                .mappings()
                .first()
            )
            if updated is None:
                await self._complete_recovery_operation(
                    session,
                    operation_id,
                    lifecycle="FAILED",
                    error_code="LLM_ENDPOINT_RECOVERY_IN_PROGRESS",
                )
                return "BUSY_PROBE", None
            logger.warning(
                "writer_relay_manual_probe_started endpoint=%s",
                name,
            )
            return "CLAIMED", int(updated["state_generation"])

    def _manual_recover_status(self, session_row: Any) -> str:
        if session_row is None:
            return "NOT_FOUND"
        if str(session_row["participation"]) == "DISABLED":
            return "DISABLED"
        now = _utcnow()
        retry_at = session_row.get("manual_retry_not_before")
        if retry_at is not None and retry_at > now:
            return "NOT_RECOVERABLE"
        circuit = str(session_row["circuit_state"])
        if circuit == "CLOSED":
            return "NOT_RECOVERABLE"
        if circuit == "HALF_OPEN":
            return "BUSY_PROBE"
        if circuit not in {"OPEN", "QUARANTINED"}:
            return "NOT_RECOVERABLE"
        return "CLAIMED"

    async def finish_manual_recover(
        self,
        name: str,
        *,
        operation_id: uuid.UUID,
        owner: str,
        generation: int,
        success: bool,
        failure_class: str | None,
        model: str,
    ):
        from src.agents.role_relay_store import RecoverError, RecoverSuccess, SafeEndpoint

        name = name.strip().lower()
        error: RecoverError | None = None
        result: RecoverSuccess | None = None
        async with self._session_factory() as session, session.begin():
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"llm-recover-{operation_id}"},
            )
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": "writer-relay-admission-v0942"},
            )
            await self._reap_expired(session)
            row = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT * FROM travel_relay_endpoint_state
                            WHERE endpoint_name = :name
                            FOR UPDATE
                            """
                        ),
                        {"name": name},
                    )
                )
                .mappings()
                .first()
            )
            if (
                row is None
                or str(row["probe_owner"] or "") != owner
                or int(row["state_generation"]) != generation
            ):
                existing = (
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
                if existing and str(existing["lifecycle"]) != "PENDING":
                    payload = existing["replay_json"] or {}
                    if payload.get("ok"):
                        endpoint = payload["endpoint"]
                        result = RecoverSuccess(
                            SafeEndpoint(
                                role="writer",
                                endpoint_id=endpoint["endpoint_id"],
                                display_name=endpoint["display_name"],
                                model=endpoint["model"],
                                participation=endpoint["participation"],
                                qualification=endpoint["qualification"],
                                circuit_state=endpoint["circuit_state"],
                                recoverable=bool(endpoint["recoverable"]),
                                cooldown_until=None,
                                inflight=endpoint.get("inflight"),
                                max_inflight=endpoint.get("max_inflight"),
                                updated_at=_utcnow(),
                            ),
                            replayed=True,
                        )
                    else:
                        error = RecoverError(
                            str(
                                payload.get("error_code")
                                or "LLM_ENDPOINT_PROBE_FAILED"
                            ),
                            http_status=int(payload.get("http_status") or 502),
                            message=str(
                                payload.get("message") or "endpoint probe failed"
                            ),
                            replayed=True,
                        )
                else:
                    error = RecoverError(
                        "LLM_ENDPOINT_RECOVERY_IN_PROGRESS",
                        http_status=409,
                        message="endpoint recovery is in progress",
                    )
            elif success:
                await session.execute(
                    text(
                        """
                        UPDATE travel_relay_endpoint_state
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
                            state_generation = state_generation + 1,
                            updated_at = NOW()
                        WHERE endpoint_name = :name
                          AND state_generation = :generation
                        """
                    ),
                    {"name": name, "generation": generation},
                )
                inflight = int(
                    await session.scalar(
                        text(
                            """
                            SELECT COUNT(*) FROM travel_writer_dispatch
                            WHERE endpoint_name = :name
                              AND state IN ('CLAIMED', 'DISPATCHED')
                              AND lease_expires_at > NOW()
                            """
                        ),
                        {"name": name},
                    )
                    or 0
                )
                row = (
                    (
                        await session.execute(
                            text(
                                """
                                SELECT * FROM travel_relay_endpoint_state
                                WHERE endpoint_name = :name
                                """
                            ),
                            {"name": name},
                        )
                    )
                    .mappings()
                    .one()
                )
                finished = SafeEndpoint(
                    role="writer",
                    endpoint_id=name,
                    display_name=name,
                    model=model,
                    participation=str(row["participation"]),
                    qualification=str(row["qualification_state"]),
                    circuit_state=str(row["circuit_state"]),
                    recoverable=False,
                    cooldown_until=None,
                    inflight=inflight,
                    max_inflight=int(row["max_inflight"]),
                    updated_at=row["updated_at"],
                )
                await session.execute(
                    text(
                        """
                        UPDATE travel_llm_endpoint_recovery_operation
                        SET lifecycle = 'SUCCEEDED',
                            http_status = 200,
                            replay_json = CAST(:replay AS jsonb),
                            finished_at = NOW(),
                            updated_at = NOW()
                        WHERE operation_id = :operation_id
                          AND lifecycle = 'PENDING'
                        """
                    ),
                    {
                        "operation_id": operation_id,
                        "replay": json.dumps(
                            {"ok": True, "http_status": 200, "endpoint": finished.to_api()},
                            separators=(",", ":"),
                            default=str,
                        ),
                    },
                )
                result = RecoverSuccess(finished)
            else:
                quarantine = False
                timeout = str(failure_class or "") == "TIMEOUT"
                origin = str(row["recovery_origin_circuit_state"] or "OPEN")
                target = (
                    "QUARANTINED"
                    if (quarantine or origin == "QUARANTINED") and failure_class not in LONG_FAILURE_CLASSES
                    else "OPEN"
                )
                await session.execute(
                    text(
                        """
                        UPDATE travel_relay_endpoint_state
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
                                secs => :failure_cooldown
                            ),
                            cooldown_level = LEAST(2, cooldown_level + 1),
                            manual_retry_not_before = NOW() + make_interval(
                                secs => :failure_cooldown
                            ),
                            manual_retry_level = LEAST(2, manual_retry_level + 1),
                            probe_kind = NULL,
                            probe_owner = NULL,
                            probe_lease_expires_at = NULL,
                            recovery_origin_circuit_state = NULL,
                            active_operation_id = NULL,
                            state_generation = state_generation + 1,
                            updated_at = NOW()
                        WHERE endpoint_name = :name
                          AND state_generation = :generation
                        """
                    ),
                    {
                        "name": name,
                        "target": target,
                        "threshold": TRANSIENT_FAILURE_THRESHOLD,
                        "failure_cooldown": cooldown_seconds(failure_class),
                        "generation": generation,
                    },
                )
                row = (
                    (
                        await session.execute(
                            text(
                                """
                                SELECT manual_retry_not_before
                                FROM travel_relay_endpoint_state
                                WHERE endpoint_name = :name
                                """
                            ),
                            {"name": name},
                        )
                    )
                    .mappings()
                    .one()
                )
                error_code = (
                    "LLM_ENDPOINT_PROBE_TIMEOUT"
                    if timeout
                    else "LLM_ENDPOINT_PROBE_FAILED"
                )
                http_status = 504 if timeout else 502
                message = (
                    "endpoint probe timed out"
                    if timeout
                    else "endpoint probe failed"
                )
                retry_after = row["manual_retry_not_before"]
                await self._complete_recovery_operation(
                    session,
                    operation_id,
                    lifecycle="FAILED",
                    error_code=error_code,
                    http_status=http_status,
                    retry_after=retry_after,
                )
                error = RecoverError(
                    error_code,
                    http_status=http_status,
                    message=message,
                    retry_after=retry_after,
                )
        if error is not None:
            raise error
        assert result is not None
        return result


def _writer_claim_error(status: str) -> tuple[str, int, str]:
    mapping = {
        "NOT_FOUND": ("LLM_ENDPOINT_NOT_FOUND", 404, "endpoint was not found"),
        "DISABLED": ("LLM_ENDPOINT_DISABLED", 409, "endpoint is disabled"),
        "NOT_RECOVERABLE": (
            "LLM_ENDPOINT_NOT_RECOVERABLE",
            409,
            "endpoint is not recoverable",
        ),
        "BUSY_PROBE": (
            "LLM_ENDPOINT_RECOVERY_IN_PROGRESS",
            409,
            "endpoint recovery is in progress",
        ),
        "BUSY": ("LLM_ENDPOINT_BUSY", 409, "endpoint is busy"),
        "UNAVAILABLE": (
            "INTERNAL_ADMIN_UNAVAILABLE",
            503,
            "internal admin is unavailable",
        ),
    }
    return mapping.get(
        status,
        ("LLM_ENDPOINT_NOT_RECOVERABLE", 409, "endpoint is not recoverable"),
    )


@dataclass
class _MemoryEndpoint:
    participation: str
    cap: int
    weight: int
    fingerprint: str = ""
    qualification: str = "QUALIFIED"
    circuit: str = "CLOSED"
    current: int = 0
    selected: int = 0
    invalid: int = 0
    transient_failures: int = 0
    observations: int = 0
    transport_success: int = 0
    timeouts: int = 0
    auth_policy: int = 0
    contract_valid: int = 0
    pending_policy: int = 0
    probe_owner: str | None = None
    probe_until: datetime | None = None
    probe_kind: str | None = None
    recovery_origin: str | None = None
    active_operation_id: uuid.UUID | None = None
    generation: int = 0
    manual_retry_level: int = 0
    manual_retry_not_before: datetime | None = None
    cooldown_level: int = 0
    cooldown_until: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class _MemoryDispatch:
    dispatch_id: uuid.UUID
    logical_call_id: uuid.UUID
    ordinal: int
    endpoint: str
    state: str
    lease_expires_at: datetime
    failure_signature: str | None = None
    failure_class: str | None = None
    half_open_trial: bool = False
    pending_policy_at: datetime | None = None


class InMemoryWriterRelayStore:
    """Deterministic non-network store for local fake-traffic acceptance only."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.endpoints = {
            name: _MemoryEndpoint(participation, cap, weight)
            for name, (participation, cap, weight) in FROZEN_ENDPOINT_POLICY.items()
        }
        self.dispatches: dict[uuid.UUID, _MemoryDispatch] = {}
        self.by_ordinal: dict[tuple[uuid.UUID, int], uuid.UUID] = {}
        self.peak_inflight = {name: 0 for name in self.endpoints}
        self.force_initial: str | None = None
        self.operations: dict[uuid.UUID, dict[str, Any]] = {}

    async def synchronize_endpoints(
        self,
        fingerprints: dict[str, str],
        *,
        now: datetime | None = None,
    ) -> None:
        async with self._lock:
            now = now or _utcnow()
            self._reap(now)
            for name, policy in OPTIONAL_ENDPOINT_POLICY.items():
                if name in fingerprints and name not in self.endpoints:
                    self.endpoints[name] = _MemoryEndpoint(
                        *policy, qualification="UNQUALIFIED"
                    )
                    self.peak_inflight[name] = 0
            for name, fingerprint in fingerprints.items():
                endpoint = self.endpoints[name]
                if endpoint.fingerprint and endpoint.fingerprint != fingerprint:
                    if endpoint.circuit == "HALF_OPEN":
                        operation_id = endpoint.active_operation_id
                        endpoint.probe_owner = None
                        endpoint.probe_until = None
                        endpoint.probe_kind = None
                        endpoint.recovery_origin = None
                        endpoint.active_operation_id = None
                        endpoint.generation += 1
                        if operation_id is not None:
                            self._complete_memory_operation(
                                operation_id,
                                "CONFIGURATION_CHANGED",
                                "LLM_ENDPOINT_NOT_RECOVERABLE",
                                now,
                            )
                    endpoint.qualification = "UNQUALIFIED"
                    endpoint.circuit = "QUARANTINED"
                    endpoint.generation += 1
                    endpoint.updated_at = now
                endpoint.fingerprint = fingerprint

    def _reap(self, now: datetime) -> None:
        for dispatch in self.dispatches.values():
            if (
                dispatch.failure_class == 'POLICY_REJECTION'
                and dispatch.pending_policy_at is not None
                and dispatch.pending_policy_at <= now - timedelta(seconds=PENDING_POLICY_COMPENSATION_SECONDS)
            ):
                dispatch.failure_class = 'POLICY_REJECTION_APPLIED'
                endpoint = self.endpoints[dispatch.endpoint]
                endpoint.pending_policy = max(0, endpoint.pending_policy - 1)
                endpoint.observations += 1
                endpoint.auth_policy += 1
                if endpoint.circuit in {'CLOSED', 'OPEN'}:
                    endpoint.circuit = 'OPEN'
                    endpoint.cooldown_until = max(endpoint.cooldown_until or now, now + timedelta(seconds=120))
            if (
                dispatch.state in {"CLAIMED", "DISPATCHED"}
                and dispatch.lease_expires_at <= now
            ):
                if dispatch.state == "DISPATCHED":
                    dispatch.state = "OUTCOME_UNKNOWN"
                    if dispatch.failure_class is None:
                        dispatch.failure_class = "PROCESS_LOSS"
                else:
                    dispatch.state = "FAILED"
                    if dispatch.failure_class != "STANDBY_READY":
                        dispatch.failure_class = "CLAIM_EXPIRED"
                if dispatch.half_open_trial and dispatch.failure_class != "STANDBY_READY":
                    endpoint = self.endpoints[dispatch.endpoint]
                    if endpoint.circuit == "HALF_OPEN":
                        origin = endpoint.recovery_origin or "OPEN"
                        endpoint.circuit = origin
                        endpoint.transient_failures = max(
                            endpoint.transient_failures,
                            TRANSIENT_FAILURE_THRESHOLD,
                        )
                        endpoint.cooldown_until = now + timedelta(
                            seconds=COOLDOWN_SECONDS[
                                min(endpoint.cooldown_level, 2)
                            ]
                        )
                        endpoint.cooldown_level = min(
                            2, endpoint.cooldown_level + 1
                        )
                        endpoint.probe_owner = None
                        endpoint.probe_until = None
                        endpoint.probe_kind = None
                        endpoint.recovery_origin = None
                        endpoint.active_operation_id = None
                        endpoint.generation += 1
        for name, endpoint in self.endpoints.items():
            if (
                endpoint.circuit == "HALF_OPEN"
                and endpoint.probe_until is not None
                and endpoint.probe_until <= now
            ):
                if endpoint.probe_kind == "automatic" and (
                    endpoint.participation not in {"ACTIVE", "CANARY"}
                    or not endpoint.fingerprint
                    or endpoint.recovery_origin != "OPEN"
                    or endpoint.active_operation_id is not None
                    or self._inflight(name, now) > 0
                ):
                    continue
                origin = endpoint.recovery_origin or "OPEN"
                is_manual = endpoint.probe_kind == "manual"
                operation_id = endpoint.active_operation_id
                endpoint.circuit = origin if origin in {"OPEN", "QUARANTINED"} else "OPEN"
                endpoint.transient_failures = max(
                    endpoint.transient_failures, TRANSIENT_FAILURE_THRESHOLD
                )
                endpoint.cooldown_until = now + timedelta(
                    seconds=COOLDOWN_SECONDS[min(endpoint.cooldown_level, 2)]
                )
                endpoint.cooldown_level = min(2, endpoint.cooldown_level + 1)
                if is_manual:
                    endpoint.manual_retry_not_before = now + timedelta(
                        seconds=COOLDOWN_SECONDS[min(endpoint.manual_retry_level, 2)]
                    )
                    endpoint.manual_retry_level = min(
                        2, endpoint.manual_retry_level + 1
                    )
                endpoint.probe_owner = None
                endpoint.probe_until = None
                endpoint.probe_kind = None
                endpoint.recovery_origin = None
                endpoint.active_operation_id = None
                endpoint.generation += 1
                endpoint.updated_at = now
                if operation_id is not None:
                    self._complete_memory_operation(
                        operation_id,
                        "EXPIRED",
                        "LLM_ENDPOINT_PROBE_TIMEOUT",
                        now,
                    )

    def _inflight(self, name: str, now: datetime) -> int:
        return sum(
            1
            for dispatch in self.dispatches.values()
            if dispatch.endpoint == name
            and dispatch.state in {"CLAIMED", "DISPATCHED"}
            and dispatch.lease_expires_at > now
        )

    async def claim(self, **kwargs) -> DispatchClaim:
        now = kwargs.get("now") or _utcnow()
        logical_call_id = kwargs["logical_call_id"]
        ordinal = int(kwargs["ordinal"])
        fingerprints = kwargs["fingerprints"]
        initial = bool(kwargs["initial"])
        excluded = kwargs["exclude_endpoints"]
        async with self._lock:
            self._reap(now)
            key = (logical_call_id, ordinal)
            if key in self.by_ordinal:
                existing = self.dispatches[self.by_ordinal[key]]
                return DispatchClaim(
                    "DUPLICATE", existing.endpoint, existing.dispatch_id
                )
            snapshot = tuple(
                EndpointCapacity(
                    name,
                    endpoint.participation,
                    endpoint.circuit,
                    self._inflight(name, now),
                    endpoint.cap,
                    endpoint.pending_policy,
                )
                for name, endpoint in self.endpoints.items()
                if name in fingerprints
            )
            admitted_participation = (
                {"ACTIVE", "CANARY"} if initial else {"ACTIVE"}
            )

            def common_eligible(name: str, endpoint: _MemoryEndpoint) -> bool:
                if name not in fingerprints or name in excluded:
                    return False
                if endpoint.participation not in admitted_participation:
                    return False
                if (
                    (endpoint.qualification != "QUALIFIED" and endpoint.circuit != "OPEN")
                    or endpoint.pending_policy > 0
                ):
                    return False
                if endpoint.fingerprint != fingerprints[name]:
                    return False
                return True

            half_open_exists = any(
                endpoint.circuit == "HALF_OPEN"
                for endpoint in self.endpoints.values()
            )
            recovery_candidates = []
            if initial and not half_open_exists:
                recovery_candidates = [
                    (name, endpoint)
                    for name, endpoint in self.endpoints.items()
                    if common_eligible(name, endpoint)
                    and endpoint.circuit == "OPEN"
                    and (
                        endpoint.cooldown_until is None
                        or endpoint.cooldown_until <= now
                    )
                    and self._inflight(name, now) == 0
                ]
            dispatch_id = uuid.uuid4()
            half_open_trial = bool(recovery_candidates)
            if half_open_trial:
                name, endpoint = sorted(
                    recovery_candidates,
                    key=lambda pair: (
                        0 if pair[1].participation == "ACTIVE" else 1,
                        -(
                            pair[1].transport_success
                            / max(1, pair[1].observations)
                        ),
                        pair[1].cooldown_until
                        or datetime.min.replace(tzinfo=timezone.utc),
                        pair[0],
                    ),
                )[0]
                endpoint.circuit = "HALF_OPEN"
                endpoint.probe_kind = "automatic"
                endpoint.probe_owner = str(dispatch_id)
                endpoint.probe_until = now + timedelta(
                    seconds=float(kwargs["lease_seconds"])
                )
                endpoint.recovery_origin = "OPEN"
                endpoint.active_operation_id = None
                endpoint.generation += 1
                endpoint.updated_at = now
            else:
                eligible = [
                    (name, endpoint)
                    for name, endpoint in self.endpoints.items()
                    if common_eligible(name, endpoint)
                    and endpoint.circuit == "CLOSED"
                ]
                if not eligible:
                    return DispatchClaim("UNAVAILABLE", snapshot=snapshot)
                free = [
                    pair
                    for pair in eligible
                    if self._inflight(pair[0], now) < pair[1].cap
                ]
                if not free:
                    return DispatchClaim("BUSY", snapshot=snapshot)
                forced = self.force_initial if initial else None
                if forced and any(name == forced for name, _endpoint in free):
                    name, endpoint = next(
                        pair for pair in free if pair[0] == forced
                    )
                    self.force_initial = None
                else:
                    total = sum(endpoint.weight for _name, endpoint in free)
                    for _name, candidate in free:
                        candidate.current += candidate.weight
                    name, endpoint = max(
                        free, key=lambda pair: (pair[1].current, pair[0])
                    )
                    endpoint.current -= total
            endpoint.selected += 1
            dispatch = _MemoryDispatch(
                dispatch_id,
                logical_call_id,
                ordinal,
                name,
                "CLAIMED",
                now + timedelta(seconds=float(kwargs["lease_seconds"])),
                half_open_trial=half_open_trial,
            )
            self.dispatches[dispatch_id] = dispatch
            self.by_ordinal[key] = dispatch_id
            inflight = self._inflight(name, now)
            self.peak_inflight[name] = max(self.peak_inflight[name], inflight)
            return DispatchClaim(
                "CLAIMED",
                name,
                dispatch_id,
                snapshot,
                half_open_trial=half_open_trial,
            )

    async def mark_dispatched(self, dispatch_id: uuid.UUID) -> None:
        async with self._lock:
            dispatch = self.dispatches[dispatch_id]
            if dispatch.state == "CLAIMED":
                dispatch.state = "DISPATCHED"

    async def complete_success(
        self, dispatch_id: uuid.UUID, *, latency_ms: int
    ) -> None:
        del latency_ms
        async with self._lock:
            dispatch = self.dispatches[dispatch_id]
            if dispatch.state == "DISPATCHED" and dispatch.failure_class != "STANDBY_READY":
                dispatch.state = "SUCCEEDED"
                endpoint = self.endpoints[dispatch.endpoint]
                endpoint.observations += 1
                endpoint.transport_success += 1
                endpoint.contract_valid += 1
                endpoint.invalid = 0
                endpoint.transient_failures = 0
                if dispatch.half_open_trial and endpoint.circuit == "HALF_OPEN":
                    endpoint.circuit = "CLOSED"
                    endpoint.qualification = "QUALIFIED"
                    endpoint.cooldown_level = 0
                    endpoint.cooldown_until = None
                    endpoint.probe_owner = None
                    endpoint.probe_until = None
                    endpoint.probe_kind = None
                    endpoint.recovery_origin = None
                    endpoint.active_operation_id = None
                    endpoint.generation += 1

    async def cancel_for_standby(
        self, dispatch_id: uuid.UUID, *, release_capacity: bool = False,
    ) -> None:
        async with self._lock:
            dispatch = self.dispatches[dispatch_id]
            if dispatch.state not in {"CLAIMED", "DISPATCHED"}:
                return
            if dispatch.failure_class not in {None, "STANDBY_READY"}:
                return
            dispatch.failure_class = "STANDBY_READY"
            if release_capacity:
                dispatch.state = "OUTCOME_UNKNOWN"
            endpoint = self.endpoints[dispatch.endpoint]
            if (dispatch.half_open_trial and endpoint.circuit == "HALF_OPEN"
                    and endpoint.probe_kind == "automatic"
                    and endpoint.probe_owner == str(dispatch_id)
                    and endpoint.active_operation_id is None):
                endpoint.circuit = endpoint.recovery_origin or "OPEN"
                endpoint.probe_owner = None
                endpoint.probe_until = None
                endpoint.probe_kind = None
                endpoint.recovery_origin = None
                endpoint.generation += 1
                endpoint.updated_at = _utcnow()

    async def complete_failure(self, dispatch_id: uuid.UUID, **kwargs) -> bool:
        async with self._lock:
            dispatch = self.dispatches[dispatch_id]
            if (dispatch.state not in {"CLAIMED", "DISPATCHED"}
                    or dispatch.failure_class == "STANDBY_READY"):
                return False
            release_capacity = bool(kwargs.get("release_capacity", True))
            failure_class = kwargs["failure_class"]
            if not release_capacity and failure_class != "TIMEOUT":
                raise ValueError(
                    "release_capacity=False is reserved for probe TIMEOUT"
                )
            if not release_capacity:
                if (
                    dispatch.state != "DISPATCHED"
                    or dispatch.failure_class is not None
                ):
                    return False
            else:
                dispatch.state = "FAILED"
            dispatch.failure_signature = kwargs.get("failure_signature")
            dispatch.failure_class = failure_class
            if failure_class == "POLICY_REJECTION":
                dispatch.pending_policy_at = _utcnow()
            signature = dispatch.failure_signature
            if failure_class == "POLICY_REJECTION" and signature:
                endpoint = self.endpoints[dispatch.endpoint]
                if dispatch.half_open_trial and endpoint.circuit == "HALF_OPEN" and endpoint.probe_kind == "automatic" and endpoint.probe_owner == str(dispatch_id):
                    endpoint.circuit = "OPEN"
                    endpoint.cooldown_until = max(endpoint.cooldown_until or _utcnow(), _utcnow() + timedelta(seconds=120))
                    endpoint.probe_kind = None
                    endpoint.probe_owner = None
                    endpoint.probe_until = None
                    endpoint.recovery_origin = None
                    endpoint.active_operation_id = None
                    endpoint.generation += 1
                endpoint.pending_policy += 1
                shared = {
                    item.endpoint
                    for item in self.dispatches.values()
                    if item.logical_call_id == dispatch.logical_call_id
                    and item.failure_signature == signature
                    and item.failure_class == "POLICY_REJECTION"
                }
                if len(shared) < 2:
                    return False
                for item in self.dispatches.values():
                    if (
                        item.logical_call_id == dispatch.logical_call_id
                        and item.failure_signature == signature
                        and item.failure_class == "POLICY_REJECTION"
                    ):
                        item.failure_class = "SHARED_REQUEST_CONTRACT"
                for name in shared:
                    shared_endpoint = self.endpoints[name]
                    shared_endpoint.pending_policy = max(
                        0, shared_endpoint.pending_policy - 1
                    )
                    if shared_endpoint.circuit == "HALF_OPEN":
                        shared_endpoint.circuit = "OPEN"
                        shared_endpoint.cooldown_until = _utcnow() + timedelta(
                            seconds=COOLDOWN_SECONDS[0]
                        )
                return True
            endpoint = self.endpoints[dispatch.endpoint]
            endpoint.observations += 1
            if kwargs.get("transport_succeeded"):
                endpoint.transport_success += 1
            if failure_class == "OUTPUT_CONTRACT":
                endpoint.invalid += 1
            else:
                endpoint.invalid = 0
            if failure_class in LONG_FAILURE_CLASSES:
                endpoint.auth_policy += 1
            counted_transient = failure_class in TRANSIENT_FAILURE_CLASSES
            immediate_open = failure_class in IMMEDIATE_OPEN_FAILURE_CLASSES
            if counted_transient or immediate_open:
                endpoint.transient_failures += 1
            else:
                endpoint.transient_failures = 0
            open_circuit = bool(
                dispatch.half_open_trial
                or immediate_open
                or (
                    counted_transient
                    and endpoint.transient_failures
                    >= TRANSIENT_FAILURE_THRESHOLD
                )
                or (
                    failure_class == "OUTPUT_CONTRACT"
                    and endpoint.invalid >= 3
                )
            )
            if open_circuit and endpoint.circuit != "QUARANTINED":
                origin = endpoint.recovery_origin or "OPEN"
                endpoint.circuit = origin if origin == "QUARANTINED" else "OPEN"
                endpoint.cooldown_until = max(endpoint.cooldown_until or _utcnow(), _utcnow() + timedelta(
                    seconds=cooldown_seconds(failure_class, kwargs.get("retry_after_seconds"))
                ))
                endpoint.cooldown_level = min(2, endpoint.cooldown_level + 1)
            if dispatch.half_open_trial:
                endpoint.probe_owner = None
                endpoint.probe_until = None
                endpoint.probe_kind = None
                endpoint.recovery_origin = None
                endpoint.active_operation_id = None
                endpoint.generation += 1
            if failure_class == "TIMEOUT":
                endpoint.timeouts += 1
            return False

    async def finalize_policy_rejection(self, dispatch_id: uuid.UUID) -> None:
        async with self._lock:
            dispatch = self.dispatches[dispatch_id]
            if dispatch.failure_class != "POLICY_REJECTION":
                return
            dispatch.failure_class = "POLICY_REJECTION_APPLIED"
            endpoint = self.endpoints[dispatch.endpoint]
            endpoint.pending_policy = max(0, endpoint.pending_policy - 1)
            endpoint.observations += 1
            endpoint.auth_policy += 1
            if endpoint.circuit in {"CLOSED", "OPEN"}:
                endpoint.circuit = "OPEN"
                endpoint.cooldown_until = max(endpoint.cooldown_until or _utcnow(), _utcnow() + timedelta(seconds=120))

    async def claim_probe(
        self,
        name: str,
        owner: str,
        *,
        actor: str = "",
        reason: str = "",
        lease_seconds: int = 60,
        now=None,
    ) -> bool:
        del actor, reason
        now = now or _utcnow()
        async with self._lock:
            endpoint = self.endpoints[name]
            for candidate in self.endpoints.values():
                if (
                    candidate.circuit == "HALF_OPEN"
                    and candidate.probe_until
                    and candidate.probe_until <= now
                ):
                    candidate.circuit = candidate.recovery_origin or "OPEN"
                    candidate.probe_kind = None
                    candidate.probe_owner = None
                    candidate.probe_until = None
                    candidate.recovery_origin = None
                    candidate.active_operation_id = None
                    candidate.generation += 1
            if any(
                candidate.circuit == "HALF_OPEN"
                for candidate in self.endpoints.values()
            ):
                return False
            if endpoint.cooldown_until and endpoint.cooldown_until > now:
                return False
            origin = endpoint.circuit if endpoint.circuit in {"OPEN", "QUARANTINED"} else "OPEN"
            endpoint.recovery_origin = origin
            endpoint.circuit = "HALF_OPEN"
            endpoint.probe_kind = "automatic"
            endpoint.probe_owner = owner
            endpoint.probe_until = now + timedelta(seconds=lease_seconds)
            endpoint.active_operation_id = None
            endpoint.generation += 1
            return True

    async def finish_probe(self, name: str, owner: str, *, success: bool) -> None:
        async with self._lock:
            endpoint = self.endpoints[name]
            if endpoint.probe_owner != owner:
                return
            if success:
                endpoint.circuit = "CLOSED"
                endpoint.transient_failures = 0
                endpoint.cooldown_level = 0
                endpoint.cooldown_until = None
            else:
                endpoint.circuit = endpoint.recovery_origin or "OPEN"
                endpoint.transient_failures = max(
                    endpoint.transient_failures,
                    TRANSIENT_FAILURE_THRESHOLD,
                )
                endpoint.cooldown_until = _utcnow() + timedelta(
                    seconds=COOLDOWN_SECONDS[min(endpoint.cooldown_level, 2)]
                )
                endpoint.cooldown_level = min(2, endpoint.cooldown_level + 1)
            endpoint.probe_owner = None
            endpoint.probe_until = None
            endpoint.probe_kind = None
            endpoint.recovery_origin = None
            endpoint.active_operation_id = None
            endpoint.generation += 1

    async def list_safe_projections(self, models: dict[str, str]):
        from src.agents.role_relay_store import SafeEndpoint

        now = _utcnow()
        async with self._lock:
            self._reap(now)
            half_open = any(
                endpoint.circuit == "HALF_OPEN" for endpoint in self.endpoints.values()
            )
            items = []
            for name, endpoint in sorted(self.endpoints.items()):
                model = models.get(name)
                if not model:
                    continue
                inflight = self._inflight(name, now)
                cooldown = endpoint.manual_retry_not_before or endpoint.cooldown_until
                if cooldown is not None and cooldown <= now:
                    cooldown = None
                recoverable = (
                    endpoint.participation != "DISABLED"
                    and endpoint.circuit in {"OPEN", "QUARANTINED"}
                    and not half_open
                    and inflight == 0
                    and (
                        endpoint.manual_retry_not_before is None
                        or endpoint.manual_retry_not_before <= now
                    )
                )
                items.append(
                    SafeEndpoint(
                        role="writer",
                        endpoint_id=name,
                        display_name=name,
                        model=model,
                        participation=endpoint.participation,
                        qualification=endpoint.qualification,
                        circuit_state=endpoint.circuit,
                        recoverable=recoverable,
                        cooldown_until=cooldown,
                        inflight=inflight,
                        max_inflight=endpoint.cap,
                        updated_at=endpoint.updated_at or now,
                    )
                )
            return items

    async def claim_manual_recover(
        self,
        name: str,
        *,
        operation_id: uuid.UUID,
        owner: str,
    ) -> tuple[str, int | None]:
        from src.agents.role_relay_store import RecoverError

        name = name.strip().lower()
        now = _utcnow()
        async with self._lock:
            self._reap(now)
            existing = self.operations.get(operation_id)
            if existing is not None:
                if existing["role"] != "writer" or existing["endpoint_name"] != name:
                    raise RecoverError(
                        "LLM_ENDPOINT_OPERATION_CONFLICT",
                        http_status=409,
                        message="operation id conflict",
                        replayed=True,
                    )
                if existing["lifecycle"] == "PENDING":
                    raise RecoverError(
                        "LLM_ENDPOINT_RECOVERY_IN_PROGRESS",
                        http_status=409,
                        message="endpoint recovery is in progress",
                        replayed=True,
                    )
                return "REPLAY", None
            self.operations[operation_id] = {
                "role": "writer",
                "endpoint_name": name,
                "lifecycle": "PENDING",
                "replay_json": None,
            }
            endpoint = self.endpoints.get(name)
            status = self._manual_recover_status_memory(endpoint, now)
            if status == "CLAIMED" and any(
                item.circuit == "HALF_OPEN" for item in self.endpoints.values()
            ):
                status = "BUSY_PROBE"
            if status == "CLAIMED" and self._inflight(name, now) > 0:
                status = "BUSY"
            if status != "CLAIMED" or endpoint is None:
                code, http_status, _message = _writer_claim_error(status)
                retry_after = None
                if (
                    status == "NOT_RECOVERABLE"
                    and endpoint is not None
                    and endpoint.manual_retry_not_before
                    and endpoint.manual_retry_not_before > now
                ):
                    retry_after = endpoint.manual_retry_not_before
                self._complete_memory_operation(
                    operation_id,
                    "FAILED",
                    code,
                    now,
                    http_status=http_status,
                    retry_after=retry_after,
                )
                return status, None
            endpoint.recovery_origin = endpoint.circuit
            endpoint.circuit = "HALF_OPEN"
            endpoint.probe_kind = "manual"
            endpoint.probe_owner = owner
            endpoint.probe_until = now + timedelta(seconds=60)
            endpoint.active_operation_id = operation_id
            endpoint.generation += 1
            endpoint.updated_at = now
            return "CLAIMED", endpoint.generation

    def _manual_recover_status_memory(
        self, endpoint: _MemoryEndpoint | None, now: datetime
    ) -> str:
        if endpoint is None:
            return "NOT_FOUND"
        if endpoint.participation == "DISABLED":
            return "DISABLED"
        if (
            endpoint.manual_retry_not_before is not None
            and endpoint.manual_retry_not_before > now
        ):
            return "NOT_RECOVERABLE"
        if endpoint.circuit == "CLOSED":
            return "NOT_RECOVERABLE"
        if endpoint.circuit == "HALF_OPEN":
            return "BUSY_PROBE"
        if endpoint.circuit not in {"OPEN", "QUARANTINED"}:
            return "NOT_RECOVERABLE"
        return "CLAIMED"

    async def finish_manual_recover(
        self,
        name: str,
        *,
        operation_id: uuid.UUID,
        owner: str,
        generation: int,
        success: bool,
        failure_class: str | None,
        model: str,
    ):
        from src.agents.role_relay_store import (
            RecoverError,
            RecoverSuccess,
            SafeEndpoint,
            classify_closed_failure,
        )

        name = name.strip().lower()
        now = _utcnow()
        async with self._lock:
            self._reap(now)
            endpoint = self.endpoints.get(name)
            if (
                endpoint is None
                or endpoint.probe_owner != owner
                or endpoint.generation != generation
            ):
                existing = self.operations.get(operation_id)
                if existing and existing["lifecycle"] != "PENDING":
                    payload = existing.get("replay_json") or {}
                    if payload.get("ok"):
                        item = payload["endpoint"]
                        return RecoverSuccess(
                            SafeEndpoint(
                                role="writer",
                                endpoint_id=item["endpoint_id"],
                                display_name=item["display_name"],
                                model=item["model"],
                                participation=item["participation"],
                                qualification=item["qualification"],
                                circuit_state=item["circuit_state"],
                                recoverable=bool(item["recoverable"]),
                                cooldown_until=None,
                                inflight=item.get("inflight"),
                                max_inflight=item.get("max_inflight"),
                                updated_at=now,
                            ),
                            replayed=True,
                        )
                    raise RecoverError(
                        str(payload.get("error_code") or "LLM_ENDPOINT_PROBE_FAILED"),
                        http_status=int(payload.get("http_status") or 502),
                        message=str(payload.get("message") or "endpoint probe failed"),
                        replayed=True,
                    )
                raise RecoverError(
                    "LLM_ENDPOINT_RECOVERY_IN_PROGRESS",
                    http_status=409,
                    message="endpoint recovery is in progress",
                )
            if success:
                endpoint.circuit = "CLOSED"
                endpoint.qualification = "QUALIFIED"
                endpoint.transient_failures = 0
                endpoint.cooldown_level = 0
                endpoint.cooldown_until = None
                endpoint.manual_retry_level = 0
                endpoint.manual_retry_not_before = None
                endpoint.probe_kind = None
                endpoint.probe_owner = None
                endpoint.probe_until = None
                endpoint.recovery_origin = None
                endpoint.active_operation_id = None
                endpoint.generation += 1
                endpoint.updated_at = now
                finished = SafeEndpoint(
                    role="writer",
                    endpoint_id=name,
                    display_name=name,
                    model=model,
                    participation=endpoint.participation,
                    qualification=endpoint.qualification,
                    circuit_state=endpoint.circuit,
                    recoverable=False,
                    cooldown_until=None,
                    inflight=self._inflight(name, now),
                    max_inflight=endpoint.cap,
                    updated_at=now,
                )
                self.operations[operation_id] = {
                    "role": "writer",
                    "endpoint_name": name,
                    "lifecycle": "SUCCEEDED",
                    "replay_json": {
                        "ok": True,
                        "http_status": 200,
                        "endpoint": finished.to_api(),
                    },
                }
                return RecoverSuccess(finished)
            origin = endpoint.recovery_origin or "OPEN"
            quarantine = False
            if (quarantine or origin == "QUARANTINED") and failure_class not in LONG_FAILURE_CLASSES:
                endpoint.circuit = "QUARANTINED"
                endpoint.qualification = "UNQUALIFIED"
            else:
                endpoint.circuit = "OPEN"
            endpoint.transient_failures = max(
                endpoint.transient_failures, TRANSIENT_FAILURE_THRESHOLD
            )
            endpoint.cooldown_until = now + timedelta(
                seconds=cooldown_seconds(failure_class)
            )
            endpoint.cooldown_level = min(2, endpoint.cooldown_level + 1)
            endpoint.manual_retry_not_before = now + timedelta(
                seconds=cooldown_seconds(failure_class)
            )
            endpoint.manual_retry_level = min(2, endpoint.manual_retry_level + 1)
            endpoint.probe_kind = None
            endpoint.probe_owner = None
            endpoint.probe_until = None
            endpoint.recovery_origin = None
            endpoint.active_operation_id = None
            endpoint.generation += 1
            endpoint.updated_at = now
            error_code = classify_closed_failure(str(failure_class or "TRANSPORT"))
            http_status = 504 if error_code.endswith("TIMEOUT") else 502
            message = (
                "endpoint probe timed out"
                if http_status == 504
                else "endpoint probe failed"
            )
            self._complete_memory_operation(
                operation_id,
                "FAILED",
                error_code,
                now,
                http_status=http_status,
                retry_after=endpoint.manual_retry_not_before,
            )
            raise RecoverError(
                error_code,
                http_status=http_status,
                message=message,
                retry_after=endpoint.manual_retry_not_before,
            )

    async def load_operation_replay(
        self, operation_id: uuid.UUID
    ) -> dict[str, Any] | None:
        current = self.operations.get(operation_id)
        if current is None or current["lifecycle"] == "PENDING":
            return None
        return current.get("replay_json")

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
        del now
        status, message = {
            "LLM_ENDPOINT_NOT_FOUND": (404, "endpoint was not found"),
            "LLM_ENDPOINT_DISABLED": (409, "endpoint is disabled"),
            "LLM_ENDPOINT_NOT_RECOVERABLE": (
                409,
                "endpoint is not recoverable",
            ),
            "LLM_ENDPOINT_RECOVERY_IN_PROGRESS": (
                409,
                "endpoint recovery is in progress",
            ),
            "LLM_ENDPOINT_BUSY": (409, "endpoint is busy"),
            "INTERNAL_ADMIN_UNAVAILABLE": (
                503,
                "internal admin is unavailable",
            ),
            "LLM_ENDPOINT_PROBE_TIMEOUT": (504, "endpoint probe timed out"),
            "LLM_ENDPOINT_PROBE_FAILED": (502, "endpoint probe failed"),
        }.get(error_code, (409, "endpoint is not recoverable"))
        if http_status is not None:
            status = http_status
        payload: dict[str, Any] = {
            "ok": False,
            "http_status": status,
            "error_code": error_code,
            "message": message,
        }
        if retry_after is not None:
            instant = retry_after
            if instant.tzinfo is None:
                instant = instant.replace(tzinfo=timezone.utc)
            payload["retry_after"] = instant.astimezone(timezone.utc).isoformat().replace(
                "+00:00", "Z"
            )
        current = self.operations.get(operation_id)
        if current is None:
            return
        current["lifecycle"] = lifecycle
        current["replay_json"] = payload
