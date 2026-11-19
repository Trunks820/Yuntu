"""Decode the outer envelope of model JSON without repairing its contents.

Only a complete JSON document, optionally inside one complete Markdown fence,
is accepted. Schema, ownership and publish validation remain with the caller.
"""
from __future__ import annotations

import json
import re
from typing import Any

_FENCE = re.compile(r"\A```(?:json)?[ \t]*\r?\n(?P<body>[\s\S]*?)\r?\n```\Z", re.IGNORECASE)


def decode_model_json(raw: str) -> Any:
    if not isinstance(raw, str):
        raise TypeError("model JSON must be text")
    text = raw.strip()
    if text.startswith("```"):
        match = _FENCE.fullmatch(text)
        if match is None:
            raise json.JSONDecodeError("invalid model JSON envelope", text, 0)
        text = match.group("body")
    # No substring extraction, trailing-comma fixes, quote repair or retry.
    return json.loads(text)
