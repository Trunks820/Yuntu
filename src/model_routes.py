"""Open-source model routing table and single-key / per-role resolver.

Supports both:
- Simple open-source configuration: a single ``DEEPSEEK_API_KEY``,
  ``GEMINI_API_KEY``, or ``OPENAI_API_KEY``, or per-role ``<ROLE>_PROVIDER``,
  ``<ROLE>_MODEL``, ``<ROLE>_API_KEY``, ``<ROLE>_BASE_URL``.
- Optional per-leg keys (e.g. ``SELECTOR_DS_API_KEY``,
  ``ROUTE_PLANNER_GEMINI_API_KEY``, ``WRITER_DS_API_KEY``) when deploying with
  independent primary/backup providers.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

DS_OFFICIAL = "deepseek-official-account"
OPENAI_OFFICIAL = "openai-official-account"
GEMINI_OFFICIAL = "gemini-official-account"

ACCOUNT_LABELS: Mapping[str, str] = MappingProxyType({
    DS_OFFICIAL: "DeepSeek",
    OPENAI_OFFICIAL: "OpenAI",
    GEMINI_OFFICIAL: "Gemini",
})

_DS_URL = "https://api.deepseek.com"
_OPENAI_URL = "https://api.openai.com/v1"
_GEMINI_OPENAI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

PROVIDER_DEFAULT_BASE_URLS: Mapping[str, str] = MappingProxyType({
    "deepseek": _DS_URL,
    "openai": _OPENAI_URL,
    "gemini": _GEMINI_OPENAI_URL,
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
})

PROVIDER_DEFAULT_MODELS: Mapping[str, dict[str, str]] = MappingProxyType({
    "deepseek": {
        "selector": "deepseek-chat",
        "route_planner": "deepseek-chat",
        "writer": "deepseek-chat",
        "writer_gemini": "deepseek-chat",
        "repair": "deepseek-chat",
        "extract": "deepseek-chat",
    },
    "gemini": {
        "selector": "gemini-2.5-flash",
        "route_planner": "gemini-2.5-flash",
        "writer": "gemini-2.5-pro",
        "writer_gemini": "gemini-2.5-pro",
        "repair": "gemini-2.5-flash",
        "extract": "gemini-2.5-flash",
    },
    "openai": {
        "selector": "gpt-4.1-mini",
        "route_planner": "gpt-4.1-mini",
        "writer": "gpt-4.1",
        "writer_gemini": "gpt-4.1",
        "repair": "gpt-4.1-mini",
        "extract": "gpt-4.1-mini",
    },
})

DS_FLASH_MODEL = "deepseek-chat"
WRITER_OPUS_MODEL = "gemini-2.5-pro"


@dataclass(frozen=True)
class RouteLeg:
    agent: str
    slot: str  # "primary" or "backup"
    account: str
    base_url: str
    model: str
    wire_api: str
    key_env: str
    endpoint_id: str
    provider: str
    params: Mapping[str, Any] = field(default_factory=dict)

    @property
    def key_field(self) -> str:
        return self.key_env.lower()


@dataclass(frozen=True)
class ResolvedLegConfig:
    agent: str
    slot: str
    account: str
    base_url: str
    model: str
    wire_api: str
    api_key: str
    endpoint_id: str
    provider: str
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentRoute:
    agent: str
    title: str
    primary: RouteLeg
    backup: RouteLeg | None = None
    switch_seconds: float | None = None
    switch_rule: str = ""
    backup_pending: str = ""

    @property
    def legs(self) -> tuple[RouteLeg, ...]:
        return (self.primary,) if self.backup is None else (self.primary, self.backup)


def _leg(
    agent: str,
    slot: str,
    account: str,
    base_url: str,
    model: str,
    wire_api: str,
    key_env: str,
    endpoint_id: str,
    provider: str,
    **params: Any,
) -> RouteLeg:
    return RouteLeg(
        agent=agent,
        slot=slot,
        account=account,
        base_url=base_url,
        model=model,
        wire_api=wire_api,
        key_env=key_env,
        endpoint_id=endpoint_id,
        provider=provider,
        params=MappingProxyType(dict(params)),
    )


MODEL_ROUTES: tuple[AgentRoute, ...] = (
    AgentRoute(
        agent="selector",
        title="选点",
        primary=_leg(
            "selector",
            "primary",
            DS_OFFICIAL,
            _DS_URL,
            DS_FLASH_MODEL,
            "chat_completions",
            "SELECTOR_DS_API_KEY",
            "selector-official-ds",
            "deepseek",
        ),
        backup=_leg(
            "selector",
            "backup",
            OPENAI_OFFICIAL,
            _OPENAI_URL,
            "gpt-4.1-mini",
            "responses",
            "SELECTOR_OPENAI_API_KEY",
            "selector-official-openai",
            "openai",
            reasoning_effort="none",
        ),
        switch_seconds=3.5,
        switch_rule="可选独立备用通道；单 Key 开源部署时自动走单路",
    ),
    AgentRoute(
        agent="route_planner",
        title="排路线",
        primary=_leg(
            "route_planner",
            "primary",
            GEMINI_OFFICIAL,
            _GEMINI_OPENAI_URL,
            "gemini-2.5-flash",
            "openai_chat",
            "ROUTE_PLANNER_GEMINI_API_KEY",
            "route_planner",
            "gemini",
            reasoning_effort="low",
            max_tokens=16000,
        ),
        backup=_leg(
            "route_planner",
            "backup",
            DS_OFFICIAL,
            _DS_URL,
            DS_FLASH_MODEL,
            "openai_chat",
            "ROUTE_PLANNER_BACKUP_API_KEY",
            "route-planner-backup",
            "deepseek",
            max_tokens=16000,
        ),
        switch_rule="主路明确失败且已配置独立备用 Key 时切换",
    ),
    AgentRoute(
        agent="writer",
        title="写作",
        primary=_leg(
            "writer",
            "primary",
            GEMINI_OFFICIAL,
            _GEMINI_OPENAI_URL,
            "gemini-2.5-pro",
            "openai_chat",
            "WRITER_API_KEY",
            "writer-primary",
            "gemini",
        ),
        backup=_leg(
            "writer",
            "backup",
            DS_OFFICIAL,
            _DS_URL,
            DS_FLASH_MODEL,
            "chat_completions",
            "WRITER_DS_API_KEY",
            "deepseek-official-standby",
            "deepseek",
        ),
        switch_seconds=15.0,
        switch_rule="按配置的 WRITER_* 或全局 Key 路由；可选 DeepSeek 热备",
    ),
    AgentRoute(
        agent="writer_gemini",
        title="写作（Gemini）",
        primary=_leg(
            "writer_gemini",
            "primary",
            GEMINI_OFFICIAL,
            _GEMINI_OPENAI_URL,
            "gemini-2.5-pro",
            "openai_chat",
            "WRITER_GEMINI_API_KEY",
            "writer-gemini",
            "gemini",
            reasoning_effort="low",
            timeout_seconds=60,
        ),
        switch_seconds=35.0,
        switch_rule="WRITER_PRIMARY=gemini 时作写作主路",
    ),
    AgentRoute(
        agent="repair",
        title="局部修补",
        primary=_leg(
            "repair",
            "primary",
            GEMINI_OFFICIAL,
            _GEMINI_OPENAI_URL,
            "gemini-2.5-flash",
            "openai_chat",
            "REPAIR_API_KEY",
            "repair-primary",
            "gemini",
        ),
        backup=_leg(
            "repair",
            "backup",
            DS_OFFICIAL,
            _DS_URL,
            DS_FLASH_MODEL,
            "chat_completions",
            "REPAIR_DS_API_KEY",
            "deepseek-official-standby",
            "deepseek",
        ),
        switch_rule="按写作同源模型执行局部修补",
    ),
    AgentRoute(
        agent="export_image",
        title="导出图片",
        primary=_leg(
            "export_image",
            "primary",
            OPENAI_OFFICIAL,
            _OPENAI_URL,
            "gpt-image-1",
            "images",
            "EXPORT_IMAGE_OPENAI_API_KEY",
            "openai-official",
            "image",
            size="1024x1536",
            quality="medium",
            output_format="png",
        ),
        switch_rule="未配置独立生图 Key 时自动使用内置城市背景图",
    ),
    AgentRoute(
        agent="extract",
        title="离线抽取",
        primary=_leg(
            "extract",
            "primary",
            DS_OFFICIAL,
            _DS_URL,
            DS_FLASH_MODEL,
            "openai_chat",
            "EXTRACT_DS_API_KEY",
            "extract",
            "deepseek",
        ),
    ),
)

_PAIRED_AGENTS = frozenset({"selector", "route_planner", "writer"})


class ModelRouteConfigError(ValueError):
    pass


def model_routes() -> tuple[AgentRoute, ...]:
    return MODEL_ROUTES


def agent_route(agent: str) -> AgentRoute:
    for route in model_routes():
        if route.agent == agent:
            return route
    raise KeyError(agent)


def route_leg(agent: str, slot: str = "primary") -> RouteLeg:
    route = agent_route(agent)
    leg = route.primary if slot == "primary" else route.backup
    if leg is None:
        raise KeyError(f"{agent}:{slot}")
    return leg


def _clean_str(value: Any) -> str:
    return str(value or "").strip()


def _provider_account(provider: str) -> str:
    if provider == "deepseek":
        return DS_OFFICIAL
    if provider == "gemini":
        return GEMINI_OFFICIAL
    if provider == "openai":
        return OPENAI_OFFICIAL
    return f"{provider or 'custom'}-account"


def _infer_global_provider_and_key(settings: Any) -> tuple[str, str, str, str]:
    """Return ``(provider, api_key, base_url, model)`` from global/sibling open-source settings."""
    # 1. Explicit global provider keys
    ds_key = _clean_str(getattr(settings, "deepseek_api_key", ""))
    gemini_key = _clean_str(getattr(settings, "gemini_api_key", ""))
    openai_key = _clean_str(getattr(settings, "openai_api_key", ""))

    # 2. Infer from per-role settings if user configured e.g. WRITER_* or INTENT_* only
    role_candidates = ("writer", "intent", "extract", "review", "grouping")
    for role_name in role_candidates:
        r_key = _clean_str(getattr(settings, f"{role_name}_api_key", ""))
        r_prov = _clean_str(getattr(settings, f"{role_name}_provider", "")).lower()
        r_base = _clean_str(getattr(settings, f"{role_name}_base_url", ""))
        r_model = _clean_str(getattr(settings, f"{role_name}_model", ""))
        if r_key and r_prov in {"deepseek", "gemini", "openai", "qwen"}:
            return (
                r_prov,
                r_key,
                r_base or PROVIDER_DEFAULT_BASE_URLS.get(r_prov, _OPENAI_URL),
                r_model,
            )

    if ds_key:
        return "deepseek", ds_key, _DS_URL, ""
    if gemini_key:
        return "gemini", gemini_key, _GEMINI_OPENAI_URL, ""
    if openai_key:
        return "openai", openai_key, _OPENAI_URL, ""
    return "", "", "", ""


def resolve_open_source_leg(
    settings: Any,
    agent: str,
    slot: str = "primary",
) -> ResolvedLegConfig:
    """Resolve an agent's active configuration for open-source deployments.

    Precedence for ``slot == "primary"``:
    1. Per-role settings: ``<AGENT>_API_KEY`` + optional ``_PROVIDER`` / ``_MODEL`` / ``_BASE_URL``.
    2. Explicit v0.15 leg key: ``leg.key_env`` (e.g. ``ROUTE_PLANNER_GEMINI_API_KEY``).
    3. Global or sibling-role open-source key (``DEEPSEEK_API_KEY``, ``GEMINI_API_KEY``,
       ``OPENAI_API_KEY``, or ``WRITER_API_KEY`` / ``INTENT_API_KEY``) with matching provider/model/base_url.

    For ``slot == "backup"``, only explicit backup keys are used so single-key
    deployments never emit bogus dual-leg failover attempts.
    """
    leg = route_leg(agent, slot)
    if slot == "backup":
        key = _clean_str(getattr(settings, leg.key_field, ""))
        return ResolvedLegConfig(
            agent=agent,
            slot=slot,
            account=leg.account,
            base_url=leg.base_url,
            model=leg.model,
            wire_api=leg.wire_api,
            api_key=key,
            endpoint_id=leg.endpoint_id,
            provider=leg.provider,
            params=leg.params,
        )

    role_prefix = "writer" if agent == "writer_gemini" else agent
    role_key = _clean_str(getattr(settings, f"{role_prefix}_api_key", ""))
    role_provider = _clean_str(getattr(settings, f"{role_prefix}_provider", "")).lower()
    role_model = _clean_str(getattr(settings, f"{role_prefix}_model", ""))
    role_base_url = _clean_str(getattr(settings, f"{role_prefix}_base_url", ""))

    # Special test/override knobs for gemini legs
    if agent == "route_planner":
        role_base_url = _clean_str(getattr(settings, "route_planner_gemini_base_url", "")) or role_base_url
        role_model = _clean_str(getattr(settings, "route_planner_gemini_model", "")) or role_model
    elif agent == "writer_gemini":
        role_base_url = _clean_str(getattr(settings, "writer_gemini_base_url", "")) or role_base_url
        role_model = _clean_str(getattr(settings, "writer_gemini_model", "")) or role_model

    if agent == "export_image":
        export_key = (
            _clean_str(getattr(settings, leg.key_field, ""))
            or _clean_str(getattr(settings, "export_image_api_key", ""))
            or _clean_str(getattr(settings, "openai_api_key", ""))
        )
        export_base = _clean_str(getattr(settings, "export_image_api_base_url", "")) or leg.base_url
        export_model = _clean_str(getattr(settings, "export_image_model", "")) or leg.model
        return ResolvedLegConfig(
            agent=agent,
            slot=slot,
            account=leg.account,
            base_url=export_base,
            model=export_model,
            wire_api=leg.wire_api,
            api_key=export_key,
            endpoint_id=leg.endpoint_id,
            provider=leg.provider,
            params=leg.params,
        )

    leg_key = _clean_str(getattr(settings, leg.key_field, ""))
    if role_key:
        provider = role_provider if role_provider in PROVIDER_DEFAULT_BASE_URLS else leg.provider
        if role_base_url and not role_provider:
            provider = "openai"
        base_url = role_base_url or PROVIDER_DEFAULT_BASE_URLS.get(provider, leg.base_url)
        default_model = PROVIDER_DEFAULT_MODELS.get(provider, {}).get(agent, leg.model)
        model = role_model or default_model
        return ResolvedLegConfig(
            agent=agent,
            slot=slot,
            account=_provider_account(provider),
            base_url=base_url,
            model=model,
            wire_api="openai_chat" if leg.wire_api in {"openai_chat", "chat_completions", "anthropic_messages"} else leg.wire_api,
            api_key=role_key,
            endpoint_id=f"{agent}-{provider}",
            provider=provider,
            params=leg.params,
        )

    if leg_key:
        provider = role_provider if role_provider in PROVIDER_DEFAULT_BASE_URLS else leg.provider
        base_url = role_base_url or leg.base_url
        model = role_model or leg.model
        return ResolvedLegConfig(
            agent=agent,
            slot=slot,
            account=_provider_account(provider),
            base_url=base_url,
            model=model,
            wire_api=leg.wire_api,
            api_key=leg_key,
            endpoint_id=leg.endpoint_id,
            provider=provider,
            params=leg.params,
        )

    # Graceful open-source single-key fallback
    g_provider, g_key, g_base_url, g_model = _infer_global_provider_and_key(settings)
    if g_key and g_provider:
        provider = role_provider if role_provider in PROVIDER_DEFAULT_BASE_URLS else g_provider
        base_url = role_base_url or g_base_url or PROVIDER_DEFAULT_BASE_URLS.get(provider, leg.base_url)
        default_model = PROVIDER_DEFAULT_MODELS.get(provider, {}).get(agent, leg.model)
        # Only inherit sibling role's custom model if providers match and role has no default
        model = role_model or default_model or g_model
        return ResolvedLegConfig(
            agent=agent,
            slot=slot,
            account=_provider_account(provider),
            base_url=base_url,
            model=model,
            wire_api="chat_completions" if leg.wire_api == "chat_completions" else "openai_chat",
            api_key=g_key,
            endpoint_id=f"{agent}-{provider}",
            provider=provider,
            params=leg.params,
        )

    # Unconfigured fallback (preserves default shape without crashing imports/tests)
    provider = role_provider if role_provider in PROVIDER_DEFAULT_BASE_URLS else leg.provider
    base_url = role_base_url or PROVIDER_DEFAULT_BASE_URLS.get(provider, leg.base_url)
    model = role_model or PROVIDER_DEFAULT_MODELS.get(provider, {}).get(agent, leg.model)
    return ResolvedLegConfig(
        agent=agent,
        slot=slot,
        account=_provider_account(provider),
        base_url=base_url,
        model=model,
        wire_api=leg.wire_api,
        api_key="",
        endpoint_id=leg.endpoint_id,
        provider=provider,
        params=leg.params,
    )


def leg_api_key(settings: Any, leg: RouteLeg) -> str:
    return resolve_open_source_leg(settings, leg.agent, leg.slot).api_key


def key_fingerprint(value: str) -> str | None:
    """Short digest for diagnostics. The key itself is never shown."""
    value = str(value or "").strip()
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:6]


def table_problems(routes: tuple[AgentRoute, ...] | None = None) -> list[str]:
    """Mistakes in the static table itself."""
    problems: list[str] = []
    seen: dict[str, str] = {}
    for route in routes if routes is not None else model_routes():
        for leg in route.legs:
            owner = f"{route.agent}:{leg.slot}"
            if leg.key_env in seen:
                problems.append(f"{leg.key_env} 同时给了 {seen[leg.key_env]} 和 {owner}")
            seen[leg.key_env] = owner
        if (
            route.agent in _PAIRED_AGENTS
            and route.backup is not None
            and route.backup.account == route.primary.account
        ):
            problems.append(f"{route.agent} 的主路和备用在同一个账户（{route.primary.account}）")
    return problems


def key_problems(settings: Any, routes: tuple[AgentRoute, ...] | None = None) -> list[str]:
    """Only flag explicitly duplicated leg-specific keys; shared global open-source keys are allowed."""
    problems: list[str] = []
    owners: dict[str, str] = {}
    for route in routes if routes is not None else model_routes():
        for leg in route.legs:
            key = _clean_str(getattr(settings, leg.key_field, ""))
            if not key:
                continue
            digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
            if digest in owners:
                problems.append(f"{leg.key_env} 和 {owners[digest]} 填的是同一把 key")
            else:
                owners[digest] = leg.key_env
    return problems


def validate_model_routes(settings: Any) -> list[str]:
    """Validate static route table; allow single-key open-source deployments."""
    problems = table_problems()
    if problems:
        raise ModelRouteConfigError("模型路由配置有误：" + "；".join(problems))
    return key_problems(settings)
