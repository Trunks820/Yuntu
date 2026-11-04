"""Scenic nesting is optional. Public routes still cluster by distance."""

from __future__ import annotations


def resolve_profiles(*_args, **_kwargs):
    """No nested scenic profiles are published on the guest monolith."""
    return [], []
