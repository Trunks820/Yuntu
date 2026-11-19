"""Ordered Outbox publisher for the Admin Trip projection stream."""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from src.config import get_settings
from src.jobs.projection_outbox import iso_utc
from src.pipeline.db import get_engine, get_session_factory


logger = logging.getLogger(__name__)
PUBLISHER_LOCK_KEY = "yuntu:admin-control-plane:v0.2:trip-projection-publisher"
HEARTBEAT_EVENT_TYPE = "PROJECTION_HEARTBEAT"
SCHEMA_VERSION = "1.0"

BrokerPublish = Callable[[dict[str, Any]], Awaitable[None]]


class ProjectionOutboxPublisher:
    """One active advisory-lock leader that never skips the Outbox head."""

    def __init__(self, broker_publish: BrokerPublish | None = None) -> None:
        self._stop = asyncio.Event()
        self._broker_publish = broker_publish
        self._connection: Any = None
        self._channel: Any = None
        self._exchange: Any = None
        self._leader = False
        self._published_total = 0
        self._heartbeat_total = 0
        self._publish_failure_total = 0
        self._confirm_failure_total = 0
        self._last_confirmed_at: datetime | None = None

    async def stop(self) -> None:
        self._stop.set()
        await self._close_broker()

    async def run(self) -> None:
        settings = get_settings()
        logger.info(
            "projection publisher started exchange=%s routing_key=%s",
            settings.projection_exchange_name,
            settings.projection_routing_key,
        )
        try:
            while not self._stop.is_set():
                try:
                    async with get_engine().connect() as connection:
                        acquired = bool(
                            (
                                await connection.execute(
                                    text(
                                        "SELECT pg_try_advisory_lock("
                                        "hashtextextended(:lock_key, 0))"
                                    ),
                                    {"lock_key": PUBLISHER_LOCK_KEY},
                                )
                            ).scalar_one()
                        )
                        await connection.commit()
                        if not acquired:
                            self._leader = False
                            await self._wait(settings.projection_publisher_poll_seconds)
                            continue
                        self._leader = True
                        try:
                            await self._run_as_leader(connection)
                        finally:
                            self._leader = False
                            try:
                                await connection.execute(
                                    text(
                                        "SELECT pg_advisory_unlock("
                                        "hashtextextended(:lock_key, 0))"
                                    ),
                                    {"lock_key": PUBLISHER_LOCK_KEY},
                                )
                                await connection.commit()
                            except Exception:
                                logger.warning(
                                    "projection publisher lock release failed",
                                    exc_info=True,
                                )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._leader = False
                    await self._close_broker()
                    logger.warning(
                        "projection publisher leader loop failed; retrying",
                        exc_info=True,
                    )
                    await self._wait(settings.projection_publisher_retry_seconds)
        finally:
            self._leader = False
            await self._close_broker()
            logger.info("projection publisher stopped")

    async def _run_as_leader(self, connection: AsyncConnection) -> None:
        settings = get_settings()
        next_heartbeat = 0.0
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT outbox_sequence, event_id, event_type,
                               schema_version, aggregate_type, aggregate_id,
                               aggregate_version, occurred_at, payload
                        FROM trip_projection_outbox
                        WHERE published_at IS NULL
                        ORDER BY outbox_sequence ASC
                        LIMIT 1
                        """
                    )
                )
            ).mappings().one_or_none()
            await connection.commit()
            if row is not None:
                await self._publish_outbox_head(connection, row)
                continue

            now = loop.time()
            if now >= next_heartbeat:
                heartbeat_row = (
                    await connection.execute(
                        text(
                            """
                            SELECT head_sequence AS outbox_high_watermark,
                                   NOW() AS observed_at
                            FROM projection_stream_head
                            WHERE stream_id = 1
                            """
                        )
                    )
                ).mappings().one()
                await connection.commit()
                heartbeat = {
                    "event_type": HEARTBEAT_EVENT_TYPE,
                    "schema_version": SCHEMA_VERSION,
                    "observed_at": iso_utc(heartbeat_row["observed_at"]),
                    "outbox_high_watermark": int(
                        heartbeat_row["outbox_high_watermark"]
                    ),
                }
                try:
                    await self._publish(heartbeat)
                except Exception:
                    self._publish_failure_total += 1
                    await self._close_broker()
                    raise
                self._heartbeat_total += 1
                self._last_confirmed_at = datetime.now(timezone.utc)
                next_heartbeat = now + settings.projection_heartbeat_interval_seconds
            await self._wait(settings.projection_publisher_poll_seconds)

    async def _publish_outbox_head(
        self,
        connection: AsyncConnection,
        row: Any,
    ) -> None:
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        event = {
            "event_id": str(row["event_id"]),
            "event_type": str(row["event_type"]),
            "schema_version": str(row["schema_version"]),
            "outbox_sequence": int(row["outbox_sequence"]),
            "aggregate_type": str(row["aggregate_type"]),
            "aggregate_id": str(row["aggregate_id"]),
            "aggregate_version": int(row["aggregate_version"]),
            "occurred_at": iso_utc(row["occurred_at"]),
            "payload": payload,
        }
        try:
            await self._publish(event)
        except Exception as exc:
            self._publish_failure_total += 1
            self._confirm_failure_total += 1
            await connection.execute(
                text(
                    """
                    UPDATE trip_projection_outbox
                    SET publish_attempts = publish_attempts + 1,
                        last_publish_error = :error
                    WHERE outbox_sequence = :outbox_sequence
                      AND published_at IS NULL
                    """
                ),
                {
                    "outbox_sequence": event["outbox_sequence"],
                    "error": type(exc).__name__[:500],
                },
            )
            await connection.commit()
            await self._close_broker()
            raise
        updated = await connection.execute(
            text(
                """
                UPDATE trip_projection_outbox
                SET published_at = NOW(),
                    publish_attempts = publish_attempts + 1,
                    last_publish_error = NULL
                WHERE outbox_sequence = :outbox_sequence
                  AND event_id = CAST(:event_id AS uuid)
                  AND published_at IS NULL
                """
            ),
            {
                "outbox_sequence": event["outbox_sequence"],
                "event_id": event["event_id"],
            },
        )
        if updated.rowcount != 1:
            await connection.rollback()
            raise RuntimeError("confirmed Outbox head changed before marking published")
        await connection.commit()
        self._published_total += 1
        self._last_confirmed_at = datetime.now(timezone.utc)

    async def _publish(self, event: dict[str, Any]) -> None:
        if self._broker_publish is not None:
            await self._broker_publish(event)
            return
        await self._ensure_broker()
        aio_pika = importlib.import_module("aio_pika")
        body = json.dumps(
            event,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        message = aio_pika.Message(
            body=body,
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            type=str(event["event_type"]),
            # The outbox row owns its schema version.  Replays must preserve it
            # instead of being relabelled with the publisher's current version.
            headers={"schema_version": str(event["schema_version"])},
        )
        confirmed = await self._exchange.publish(
            message,
            routing_key=get_settings().projection_routing_key,
            timeout=get_settings().projection_publish_confirm_timeout_seconds,
        )
        if confirmed is False:
            raise RuntimeError("RabbitMQ publisher confirm was negative")

    async def _ensure_broker(self) -> None:
        if self._exchange is not None:
            return
        settings = get_settings()
        aio_pika = importlib.import_module("aio_pika")
        self._connection = await aio_pika.connect_robust(
            settings.projection_rabbitmq_url,
            timeout=settings.projection_broker_connect_timeout_seconds,
        )
        self._channel = await self._connection.channel(
            publisher_confirms=True,
            on_return_raises=True,
        )
        self._exchange = await self._channel.get_exchange(
            settings.projection_exchange_name,
            ensure=True,
        )

    async def _close_broker(self) -> None:
        connection = self._connection
        self._connection = None
        self._channel = None
        self._exchange = None
        if connection is not None:
            try:
                await connection.close()
            except Exception:
                logger.warning("projection broker close failed", exc_info=True)

    async def _wait(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=max(0.01, seconds))
        except asyncio.TimeoutError:
            pass

    async def metrics(self) -> dict[str, Any]:
        factory = get_session_factory()
        async with factory() as session:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT
                            h.head_sequence,
                            COUNT(o.outbox_sequence) FILTER (
                                WHERE o.published_at IS NULL
                            )::bigint
                                AS unpublished_count,
                            MIN(o.outbox_sequence) FILTER (
                                WHERE o.published_at IS NULL
                            ) AS oldest_unpublished_sequence,
                            MAX(o.outbox_sequence) FILTER (
                                WHERE o.published_at IS NOT NULL
                            ) AS last_published_sequence
                        FROM projection_stream_head h
                        LEFT JOIN trip_projection_outbox o ON TRUE
                        WHERE h.stream_id = 1
                        GROUP BY h.head_sequence
                        """
                    )
                )
            ).mappings().one()
        return {
            "leader": self._leader,
            "outbox_high_watermark": int(row["head_sequence"]),
            "unpublished_count": int(row["unpublished_count"] or 0),
            "oldest_unpublished_sequence": row["oldest_unpublished_sequence"],
            "last_published_sequence": row["last_published_sequence"],
            "published_total": self._published_total,
            "heartbeat_total": self._heartbeat_total,
            "publish_failure_total": self._publish_failure_total,
            "confirm_failure_total": self._confirm_failure_total,
            "last_confirmed_at": (
                iso_utc(self._last_confirmed_at)
                if self._last_confirmed_at is not None
                else None
            ),
        }
