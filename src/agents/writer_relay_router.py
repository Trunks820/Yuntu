"""v0.9.4.2 same-model Writer routing with no hedge or fan-out."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from openai import APIConnectionError, APITimeoutError

from src.agents.model_failure_policy import confirmed_long_failure
from src.model_routes import WRITER_OPUS_MODEL

from src.agents.writer_relay_store import (
    ALLOWED_ENDPOINTS,
    PARTICIPATING_ENDPOINTS,
    DispatchClaim,
    EndpointCapacity,
)

logger = logging.getLogger(__name__)

WRITER_MODEL = WRITER_OPUS_MODEL
CAPACITY_WAIT_SECONDS = 15.0
ATTEMPT_TIMEOUT_SECONDS = 45.0
STREAM_ATTEMPT_TIMEOUT_SECONDS = 60.0
FAILOVER_MIN_REMAINING_SECONDS = 75.0
WORKFLOW_WALL_SECONDS = 180.0
LEASE_SECONDS = 50.0
STREAM_LEASE_SECONDS = 70.0
_invalid_output_endpoint: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "writer_relay_invalid_output_endpoint", default=None
)


def _notify_observer(observer: object | None, method_name: str, **kwargs: object) -> None:
    callback = getattr(observer, method_name, None)
    if not callable(callback):
        return
    try:
        callback(**kwargs)
    except Exception as exc:
        logger.warning(
            "writer_probe_observer_failed method=%s error_type=%s",
            method_name,
            exc.__class__.__name__,
        )


class WriterRelayError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        snapshot: tuple[EndpointCapacity, ...] = (),
        waited_ms: int = 0,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.snapshot = snapshot
        self.waited_ms = waited_ms


@dataclass(frozen=True)
class WriterStreamEvent:
    """One parsed stream frame; only non-empty content is liveness."""

    content_delta: str = ""
    token_in: int | None = None
    token_out: int | None = None


class WriterStreamTimeout(httpx.ReadTimeout):
    """A bounded stream kill with close-confirmation settlement evidence."""

    def __init__(
        self,
        *,
        probe_kind: str | None,
        partial_output_before_kill: bool,
        connection_closed_confirmed: bool,
    ) -> None:
        label = probe_kind or "attempt_total"
        super().__init__(f"writer stream timeout ({label})")
        self.probe_kind = probe_kind
        self.partial_output_before_kill = partial_output_before_kill
        self.connection_closed_confirmed = connection_closed_confirmed


class WriterBudgetCutoff(httpx.ReadTimeout):
    """Adjudication budget cancellation with lease-settlement evidence."""

    def __init__(self, *, connection_closed_confirmed: bool) -> None:
        super().__init__("writer cancelled by adjudication budget cutoff")
        self.connection_closed_confirmed = connection_closed_confirmed


@dataclass(frozen=True)
class FailureClassification:
    failure_class: str
    retryable: bool
    http_status_class: str | None = None
    signature: str | None = None
    retry_after_seconds: float | None = None


def normalize_endpoint_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.strip().lower())


def endpoint_fingerprint(endpoint: Any, model: str) -> str:
    # The API key contributes only through SHA-256; the fingerprint is not a secret.
    key_digest = hashlib.sha256(str(endpoint.api_key).encode("utf-8")).hexdigest()
    payload = json.dumps(
        {
            "name": normalize_endpoint_name(endpoint.name),
            "base_url": str(endpoint.base_url).rstrip("/"),
            "wire_api": str(endpoint.wire_api),
            "model": str(endpoint.model or model),
            "key_digest": key_digest,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def frozen_writer_pool(config: Any) -> tuple[dict[str, Any], dict[str, str]]:
    endpoints: dict[str, Any] = {}
    for endpoint in config.relay_pool:
        name = normalize_endpoint_name(endpoint.name)
        if name in ALLOWED_ENDPOINTS:
            resolved_model = str(endpoint.model or config.model)
            if resolved_model != WRITER_MODEL:
                raise ValueError(
                    f"Writer relay endpoint {name!r} must declare {WRITER_MODEL!r}"
                )
            endpoints[name] = endpoint
    missing = sorted(set(PARTICIPATING_ENDPOINTS) - set(endpoints))
    if missing:
        raise ValueError(
            "Writer relay pool is missing frozen participating endpoints: "
            + ", ".join(missing)
        )
    fingerprints = {
        name: endpoint_fingerprint(endpoint, WRITER_MODEL)
        for name, endpoint in endpoints.items()
    }
    return endpoints, fingerprints


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(raw)
            return max(0.0, (parsed - datetime.now(parsed.tzinfo)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def classify_failure(exc: BaseException) -> FailureClassification:
    response = getattr(exc, "response", None)
    if isinstance(response, httpx.Response):
        status = response.status_code
        status_class = f"{status // 100}xx"
        long_failure = confirmed_long_failure(response)
        if long_failure is not None:
            return FailureClassification(long_failure, True, status_class)
        signature = None
        if 400 <= status < 500 and status != 429:
            # Hash only; provider bodies can contain sensitive request excerpts.
            signature = hashlib.sha256(
                str(status).encode("ascii") + b":" + response.content
            ).hexdigest()
        if status == 429:
            return FailureClassification(
                "RATE_LIMIT",
                True,
                status_class,
                signature,
                _retry_after_seconds(response),
            )
        if status >= 500:
            return FailureClassification("HTTP_5XX", True, status_class, signature)
        if 400 <= status < 500:
            return FailureClassification(
                "POLICY_REJECTION", True, status_class, signature
            )
    if isinstance(exc, (asyncio.TimeoutError, httpx.TimeoutException, APITimeoutError)):
        return FailureClassification("TIMEOUT", True)
    if isinstance(
        exc,
        (
            httpx.ConnectError,
            httpx.ReadError,
            httpx.RemoteProtocolError,
            httpx.WriteError,
            APIConnectionError,
        ),
    ):
        return FailureClassification("TRANSPORT", True)
    if isinstance(exc, (KeyError, json.JSONDecodeError, UnicodeDecodeError)):
        return FailureClassification("WIRE_INVALID", True)
    return FailureClassification("NON_RETRYABLE", False)


def parseable_json_object(raw: str) -> bool:
    text_value = str(raw or "").strip()
    if text_value.startswith("```"):
        lines = text_value.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text_value = "\n".join(lines).strip()
    try:
        value = json.loads(text_value)
    except json.JSONDecodeError:
        start, end = text_value.find("{"), text_value.rfind("}")
        if start < 0 or end <= start:
            return False
        try:
            value = json.loads(text_value[start : end + 1])
        except json.JSONDecodeError:
            return False
    return isinstance(value, dict)


class WriterRelayRouter:
    def __init__(
        self,
        store: Any,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        admission_poll_seconds: float = 0.05,
    ) -> None:
        self.store = store
        self._monotonic = monotonic
        self._utcnow = utcnow
        self._sleep = sleep
        self._admission_poll_seconds = admission_poll_seconds
        self._sync_lock = asyncio.Lock()
        self._synchronized: tuple[tuple[str, str], ...] | None = None

    async def _synchronize(self, fingerprints: dict[str, str]) -> None:
        frozen = tuple(sorted(fingerprints.items()))
        if self._synchronized == frozen:
            return
        async with self._sync_lock:
            if self._synchronized != frozen:
                await self.store.synchronize_endpoints(
                    fingerprints,
                    now=self._utcnow(),
                )
                self._synchronized = frozen

    async def _admit(
        self,
        *,
        logical_call_id: uuid.UUID,
        ordinal: int,
        job_id: str | None,
        call_reason: str,
        release_identity: str,
        fingerprints: dict[str, str],
        initial: bool,
        exclude_endpoints: frozenset[str],
        wait_seconds: float,
        lease_seconds: float,
    ) -> DispatchClaim:
        started = self._monotonic()
        last_snapshot: tuple[EndpointCapacity, ...] = ()
        while True:
            claim = await self.store.claim(
                logical_call_id=logical_call_id,
                ordinal=ordinal,
                job_id=job_id,
                call_reason=call_reason,
                model=WRITER_MODEL,
                release_identity=release_identity,
                fingerprints=fingerprints,
                initial=initial,
                exclude_endpoints=exclude_endpoints,
                lease_seconds=lease_seconds,
                now=self._utcnow(),
            )
            last_snapshot = claim.snapshot
            waited_ms = int((self._monotonic() - started) * 1000)
            if claim.status == "CLAIMED":
                logger.info(
                    "writer_relay_admitted endpoint=%s ordinal=%s reason=%s waited_ms=%s release=%s",
                    claim.endpoint_name,
                    ordinal,
                    call_reason,
                    waited_ms,
                    release_identity,
                )
                return claim
            if claim.status == "UNAVAILABLE":
                logger.warning(
                    "writer_relay_capacity_failure code=%s waited_ms=%s snapshot=%s",
                    "WRITER_ENDPOINTS_UNAVAILABLE",
                    waited_ms,
                    json.dumps(
                        [item.__dict__ for item in last_snapshot],
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
                raise WriterRelayError(
                    "WRITER_ENDPOINTS_UNAVAILABLE",
                    snapshot=last_snapshot,
                    waited_ms=waited_ms,
                )
            if claim.status == "DUPLICATE":
                # A previously admitted ordinal is never resent after uncertainty.
                logger.error(
                    "writer_relay_duplicate_claim logical_call_id=%s ordinal=%s",
                    logical_call_id,
                    ordinal,
                )
                raise WriterRelayError(
                    "WRITER_DISPATCH_OUTCOME_UNKNOWN",
                    snapshot=last_snapshot,
                    waited_ms=waited_ms,
                )
            if self._monotonic() - started >= wait_seconds:
                logger.warning(
                    "writer_relay_capacity_failure code=%s waited_ms=%s snapshot=%s",
                    "WRITER_CAPACITY_BUSY",
                    waited_ms,
                    json.dumps(
                        [item.__dict__ for item in last_snapshot],
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
                raise WriterRelayError(
                    "WRITER_CAPACITY_BUSY",
                    snapshot=last_snapshot,
                    waited_ms=waited_ms,
                )
            await self._sleep(min(self._admission_poll_seconds, wait_seconds))

    async def _finalize_policy_rejections(self, dispatch_ids: list[uuid.UUID]) -> None:
        while dispatch_ids:
            await self.store.finalize_policy_rejection(dispatch_ids.pop(0))

    async def _close_stream(self, stream: Any) -> bool:
        try:
            return bool(await stream.aclose())
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            logger.warning(
                "writer_stream_close_unconfirmed error_type=%s",
                exc.__class__.__name__,
            )
            return False

    async def _consume_stream_with_probes(
        self,
        stream: AsyncIterator[WriterStreamEvent],
        *,
        attempt_timeout_seconds: float,
        first_token_deadline_seconds: float,
        stall_deadline_seconds: float,
        budget_cancel_event: asyncio.Event | None = None,
        standby_cancel_event: asyncio.Event | None = None,
        record_standby_cancel: Callable[[bool], Awaitable[None]] | None = None,
        speculative_observer: object | None = None,
    ) -> tuple[str, int, int]:
        started = self._monotonic()
        attempt_deadline = started + attempt_timeout_seconds
        probe_deadline = started + first_token_deadline_seconds
        saw_content = False
        chunks: list[str] = []
        token_in = 0
        token_out = 0
        close_attempted = False
        try:
            while True:
                now = self._monotonic()
                attempt_remaining = attempt_deadline - now
                probe_remaining = probe_deadline - now
                if attempt_remaining <= probe_remaining:
                    active_timeout = attempt_remaining
                    timeout_probe_kind: str | None = None
                else:
                    active_timeout = probe_remaining
                    timeout_probe_kind = "stall" if saw_content else "first_token"
                if active_timeout <= 0:
                    connection_closed = await self._close_stream(stream)
                    close_attempted = True
                    raise WriterStreamTimeout(
                        probe_kind=timeout_probe_kind,
                        partial_output_before_kill=bool(chunks),
                        connection_closed_confirmed=connection_closed,
                    )
                try:
                    event = await asyncio.wait_for(
                        anext(stream),
                        timeout=active_timeout,
                    )
                except StopAsyncIteration:
                    await self._close_stream(stream)
                    close_attempted = True
                    return "".join(chunks), token_in, token_out
                except asyncio.TimeoutError:
                    connection_closed = await self._close_stream(stream)
                    close_attempted = True
                    raise WriterStreamTimeout(
                        probe_kind=timeout_probe_kind,
                        partial_output_before_kill=bool(chunks),
                        connection_closed_confirmed=connection_closed,
                    ) from None

                if event.token_in is not None:
                    token_in = max(0, int(event.token_in))
                if event.token_out is not None:
                    token_out = max(0, int(event.token_out))
                content_delta = str(event.content_delta or "")
                if not content_delta:
                    continue
                if not saw_content:
                    _notify_observer(
                        speculative_observer,
                        "record_opus_first_token",
                        observed_at_monotonic=self._monotonic(),
                    )
                chunks.append(content_delta)
                saw_content = True
                probe_deadline = self._monotonic() + stall_deadline_seconds
        except WriterStreamTimeout:
            raise
        except asyncio.CancelledError:
            standby_cancelled = standby_cancel_event is not None and standby_cancel_event.is_set()
            if standby_cancelled and record_standby_cancel is not None:
                # Record intent before a possibly slow socket close. A restart
                # must not reinterpret this deliberate cancellation as a fault.
                await record_standby_cancel(False)
            connection_closed = False
            if not close_attempted:
                connection_closed = await self._close_stream(stream)
                close_attempted = True
            if standby_cancelled and record_standby_cancel is not None:
                await record_standby_cancel(connection_closed)
            if budget_cancel_event is not None and budget_cancel_event.is_set():
                raise WriterBudgetCutoff(
                    connection_closed_confirmed=connection_closed,
                ) from None
            raise
        except BaseException:
            if not close_attempted:
                await self._close_stream(stream)
                close_attempted = True
            raise
        finally:
            if not close_attempted:
                await self._close_stream(stream)

    async def execute(
        self,
        *,
        config: Any,
        system: str,
        user: str,
        temperature: float,
        json_mode: bool,
        call_context: dict[str, object],
        release_identity: str,
        request: Callable[
            [Any, str, str, float, bool, float], Awaitable[tuple[str, int, int]]
        ],
        stream_probe_enabled: bool = False,
        stream_request: Callable[
            [Any, str, str, float, bool], AsyncIterator[WriterStreamEvent]
        ]
        | None = None,
        first_token_deadline_seconds: float = 15.0,
        stall_deadline_seconds: float = 10.0,
        on_wire_invalid: Callable[[Any, int], None] | None = None,
    ) -> tuple[str, int, int, Any, int, str | None]:
        if stream_probe_enabled and stream_request is None:
            raise ValueError("stream_request is required when Writer probes are enabled")
        attempt_timeout_seconds = (
            STREAM_ATTEMPT_TIMEOUT_SECONDS
            if stream_probe_enabled
            else ATTEMPT_TIMEOUT_SECONDS
        )
        lease_seconds = STREAM_LEASE_SECONDS if stream_probe_enabled else LEASE_SECONDS
        endpoints, fingerprints = frozen_writer_pool(config)
        await self._synchronize(fingerprints)
        logical_call_id = uuid.uuid4()
        call_reason = str(call_context.get("call_reason") or "writer")[:128]
        job_id = str(call_context.get("job_id") or "").strip() or None
        workflow_deadline = call_context.get("workflow_deadline_monotonic")
        budget_cancel_event_value = call_context.get("writer_budget_cancel_event")
        budget_cancel_event = (
            budget_cancel_event_value
            if isinstance(budget_cancel_event_value, asyncio.Event)
            else None
        )
        standby_cancel_event_value = call_context.get("writer_standby_ready_cancel_event")
        standby_cancel_event = (
            standby_cancel_event_value
            if isinstance(standby_cancel_event_value, asyncio.Event)
            else None
        )
        speculative_observer = call_context.get("writer_speculative_observer")
        try:
            deadline = float(workflow_deadline)
        except (TypeError, ValueError):
            deadline = self._monotonic() + WORKFLOW_WALL_SECONDS
        prior_invalid = (
            _invalid_output_endpoint.get() if "json_retry" in call_reason else None
        )
        initial_exclude = frozenset({prior_invalid}) if prior_invalid else frozenset()
        claim = await self._admit(
            logical_call_id=logical_call_id,
            ordinal=0,
            job_id=job_id,
            call_reason=call_reason,
            release_identity=release_identity,
            fingerprints=fingerprints,
            initial=True,
            exclude_endpoints=initial_exclude,
            wait_seconds=CAPACITY_WAIT_SECONDS,
            lease_seconds=lease_seconds,
        )
        previous_endpoint: str | None = None
        last_exc: BaseException | None = None
        deferred_policy_dispatches: list[uuid.UUID] = []

        async def watch_standby_cancellation() -> None:
            assert standby_cancel_event is not None
            await standby_cancel_event.wait()
            try:
                # This also covers non-streaming clients whose request-finally
                # stalls before CancelledError can reach the router.
                await self.store.cancel_for_standby(claim.dispatch_id)
            except Exception as cancel_error:
                logger.warning("writer_standby_cancel_intent_failed error_type=%s",
                               type(cancel_error).__name__)

        standby_cancel_watcher = (
            asyncio.create_task(watch_standby_cancellation())
            if standby_cancel_event is not None else None
        )
        try:
            for ordinal in (0, 1):
                if ordinal == 1:
                    remaining = deadline - self._monotonic()
                    if remaining < FAILOVER_MIN_REMAINING_SECONDS:
                        await self._finalize_policy_rejections(
                            deferred_policy_dispatches
                        )
                        assert last_exc is not None
                        raise last_exc
                    try:
                        claim = await self._admit(
                            logical_call_id=logical_call_id,
                            ordinal=1,
                            job_id=job_id,
                            call_reason=call_reason,
                            release_identity=release_identity,
                            fingerprints=fingerprints,
                            initial=False,
                            exclude_endpoints=frozenset(
                                {previous_endpoint} if previous_endpoint else set()
                            ),
                            wait_seconds=min(
                                CAPACITY_WAIT_SECONDS,
                                max(0.0, remaining - FAILOVER_MIN_REMAINING_SECONDS),
                            ),
                            lease_seconds=lease_seconds,
                        )
                    except BaseException:
                        await self._finalize_policy_rejections(
                            deferred_policy_dispatches
                        )
                        raise
                assert claim.dispatch_id is not None and claim.endpoint_name is not None
                endpoint_name = claim.endpoint_name
                endpoint = endpoints[endpoint_name]
                selected_config = config.__class__(
                    provider=config.provider,
                    model=endpoint.model or config.model,
                    api_key=endpoint.api_key,
                    base_url=endpoint.base_url,
                    relay_profile=config.relay_profile,
                    wire_api=endpoint.wire_api,
                    relay_endpoint=endpoint.name,
                    relay_pool=config.relay_pool,
                )
                await self.store.mark_dispatched(claim.dispatch_id)
                attempt_started = self._monotonic()

                async def record_standby_cancel(release_capacity: bool) -> None:
                    try:
                        await self.store.cancel_for_standby(
                            claim.dispatch_id, release_capacity=release_capacity,
                        )
                    except Exception as cancel_error:
                        logger.warning("writer_standby_cancel_settlement_failed error_type=%s",
                                       type(cancel_error).__name__)

                try:
                    if stream_probe_enabled:
                        assert stream_request is not None
                        stream = stream_request(
                            selected_config,
                            system,
                            user,
                            temperature,
                            json_mode,
                        )
                        raw, token_in, token_out = (
                            await self._consume_stream_with_probes(
                                stream,
                                attempt_timeout_seconds=attempt_timeout_seconds,
                                first_token_deadline_seconds=(
                                    first_token_deadline_seconds
                                ),
                                stall_deadline_seconds=stall_deadline_seconds,
                                budget_cancel_event=budget_cancel_event,
                                standby_cancel_event=standby_cancel_event,
                                record_standby_cancel=record_standby_cancel,
                                speculative_observer=speculative_observer,
                            )
                        )
                    else:
                        raw, token_in, token_out = await asyncio.wait_for(
                            request(
                                selected_config,
                                system,
                                user,
                                temperature,
                                json_mode,
                                attempt_timeout_seconds,
                            ),
                            timeout=attempt_timeout_seconds,
                        )
                except BaseException as exc:
                    if isinstance(exc, asyncio.CancelledError):
                        if standby_cancel_event is not None and standby_cancel_event.is_set():
                            if not stream_probe_enabled:
                                await record_standby_cancel(True)
                            await self._finalize_policy_rejections(deferred_policy_dispatches)
                            raise
                        elif budget_cancel_event is not None and budget_cancel_event.is_set():
                            exc = WriterBudgetCutoff(
                                connection_closed_confirmed=False,
                            )
                        else:
                            await self._finalize_policy_rejections(
                                deferred_policy_dispatches
                            )
                            raise
                    classification = classify_failure(exc)
                    latency_ms = int((self._monotonic() - attempt_started) * 1000)
                    release_capacity = True
                    if isinstance(exc, WriterStreamTimeout):
                        release_capacity = exc.connection_closed_confirmed
                        _notify_observer(
                            speculative_observer,
                            "record_probe_kill",
                            endpoint=endpoint_name,
                            kind=exc.probe_kind or "attempt_total",
                            partial_output_before_kill=(
                                exc.partial_output_before_kill
                            ),
                            observed_at_monotonic=self._monotonic(),
                        )
                        logger.warning(
                            "writer_stream_killed endpoint=%s ordinal=%s "
                            "probe_kind=%s partial_output=%s close_confirmed=%s",
                            endpoint_name,
                            ordinal,
                            exc.probe_kind or "attempt_total",
                            exc.partial_output_before_kill,
                            exc.connection_closed_confirmed,
                        )
                    elif isinstance(exc, WriterBudgetCutoff):
                        release_capacity = exc.connection_closed_confirmed
                        logger.warning(
                            "writer_budget_cutoff endpoint=%s ordinal=%s close_confirmed=%s",
                            endpoint_name,
                            ordinal,
                            exc.connection_closed_confirmed,
                        )
                    shared = await self.store.complete_failure(
                        claim.dispatch_id,
                        latency_ms=latency_ms,
                        failure_class=classification.failure_class,
                        http_status_class=classification.http_status_class,
                        failure_signature=classification.signature,
                        retry_after_seconds=classification.retry_after_seconds,
                        release_capacity=release_capacity,
                    )
                    if (
                        classification.failure_class == "POLICY_REJECTION"
                        and classification.signature
                    ):
                        deferred_policy_dispatches.append(claim.dispatch_id)
                    logger.warning(
                        "writer_relay_failed endpoint=%s ordinal=%s class=%s status=%s release=%s",
                        endpoint_name,
                        ordinal,
                        classification.failure_class,
                        classification.http_status_class,
                        release_identity,
                    )
                    if shared:
                        raise WriterRelayError(
                            "WRITER_REQUEST_CONTRACT_FAILED"
                        ) from exc
                    if isinstance(exc, WriterBudgetCutoff):
                        await self._finalize_policy_rejections(
                            deferred_policy_dispatches
                        )
                        raise exc
                    if not classification.retryable or ordinal == 1:
                        await self._finalize_policy_rejections(
                            deferred_policy_dispatches
                        )
                        raise
                    previous_endpoint = endpoint_name
                    last_exc = exc
                    continue
                latency_ms = int((self._monotonic() - attempt_started) * 1000)
                if json_mode and not parseable_json_object(raw):
                    stripped = str(raw or "").strip()
                    if not stripped or stripped.startswith(("{", "[")):
                        await self.store.complete_failure(
                            claim.dispatch_id,
                            latency_ms=latency_ms,
                            failure_class="WIRE_INVALID",
                            transport_succeeded=True,
                        )
                        # The transport finished cleanly, so no stream/transport
                        # observer saw this attempt; report it without affecting dispatch.
                        if on_wire_invalid is not None:
                            try:
                                on_wire_invalid(selected_config, latency_ms)
                            except Exception:
                                logger.warning("writer wire-invalid observation unavailable")
                        wire_error = RuntimeError(
                            "writer relay returned wire-invalid JSON"
                        )
                        if ordinal == 0:
                            previous_endpoint = endpoint_name
                            last_exc = wire_error
                            continue
                        await self._finalize_policy_rejections(
                            deferred_policy_dispatches
                        )
                        raise wire_error
                    await self.store.complete_failure(
                        claim.dispatch_id,
                        latency_ms=latency_ms,
                        failure_class="OUTPUT_CONTRACT",
                        transport_succeeded=True,
                    )
                    _invalid_output_endpoint.set(endpoint_name)
                else:
                    await self.store.complete_success(
                        claim.dispatch_id, latency_ms=latency_ms
                    )
                    _invalid_output_endpoint.set(None)
                await self._finalize_policy_rejections(deferred_policy_dispatches)
                return (
                    raw,
                    token_in,
                    token_out,
                    selected_config,
                    ordinal,
                    previous_endpoint,
                )
            assert last_exc is not None
            raise last_exc
        finally:
            if standby_cancel_watcher is not None:
                if standby_cancel_event is not None and not standby_cancel_event.is_set():
                    standby_cancel_watcher.cancel()
                await asyncio.gather(standby_cancel_watcher, return_exceptions=True)
            await self._finalize_policy_rejections(deferred_policy_dispatches)
