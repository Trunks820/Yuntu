"""Safe image-call facts, including calls whose later artifact rendering fails."""
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
import time
import uuid
import logging

from src.agents.model_call_observation import enrich_record, observed_usage, provider_error_code

image_call_sink: ContextVar = ContextVar("image_call_sink", default=None)
image_channel: ContextVar = ContextVar("image_channel", default="legacy")
_image_facts: ContextVar = ContextVar("image_facts", default=None)
logger = logging.getLogger(__name__)


def _emit(sink, facts):
    try:
        sink(dict(facts))
    except Exception:
        logger.warning("image model call observation unavailable")


def capture_image_response(response) -> None:
    facts = _image_facts.get()
    if facts is None:
        return
    facts["response_received"] = True
    facts["http_status_code"] = response.status_code
    try:
        payload = response.json()
        if isinstance(payload, dict):
            facts["observed_usage"] = observed_usage(payload.get("usage"))
            facts["error_code"] = provider_error_code(payload)
    except (ValueError, TypeError):
        pass


def observe_image_call(function):
    @wraps(function)
    def wrapped(self, *args, **kwargs):
        sink = image_call_sink.get()
        if sink is None:
            return function(self, *args, **kwargs)
        started = time.monotonic_ns()
        facts = {"observation_id": uuid.uuid4().hex, "role": "export_image", "stage": "EXPORT_IMAGE",
                 "provider": "image", "model": self.model, "endpoint_label": image_channel.get(),
                 "started_at": datetime.now(timezone.utc).isoformat(), "finished_at": None,
                 "status": "in_flight", "observed_usage": observed_usage(None), "response_received": False}
        token = _image_facts.set(facts)
        _emit(sink, facts)
        try:
            result = function(self, *args, **kwargs)
            facts["status"] = "success"
            return result
        except BaseException as exc:
            facts.update(status="error", error_type=type(exc).__name__)
            raise
        finally:
            facts.update(finished_at=datetime.now(timezone.utc).isoformat(),
                         latency_ms=(time.monotonic_ns() - started) // 1_000_000)
            _emit(sink, enrich_record(facts))
            _image_facts.reset(token)
    return wrapped
