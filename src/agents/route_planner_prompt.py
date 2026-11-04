"""Prompt text for the v0.14 route planner.

The wording follows the P0.5 experiment (``rules_for``, ``system_for``,
``request_text``, ``candidate_block``). Place ids are the database numbers.
The model is asked for order and stay minutes only.
"""

from __future__ import annotations

from src.agents.route_planner.prompt import (
    _fmt,
    _md,
    request_text,
    _hard_rules,
    _soft_rules,
    rules_for,
    system_for,
    _VISITOR_QUOTE_LIMIT,
    _reason_text,
    _visitor_quote,
    _marks,
    _money,
    _visit_hint,
    candidate_block,
)


