"""Provider constants and model routing shared by the API and account pool."""
from __future__ import annotations

import os

PROVIDER_DEEPSEEK = "deepseek"
PROVIDER_ZAI = "zai"
SUPPORTED_PROVIDERS = (PROVIDER_DEEPSEEK, PROVIDER_ZAI)

PROVIDER_LABELS = {
    PROVIDER_DEEPSEEK: "DeepSeek",
    PROVIDER_ZAI: "z.ai",
}

_PROVIDER_ALIASES = {
    "deepseek": PROVIDER_DEEPSEEK,
    "deepseek-chat": PROVIDER_DEEPSEEK,
    "zai": PROVIDER_ZAI,
    "z.ai": PROVIDER_ZAI,
    "z-ai": PROVIDER_ZAI,
    "zchat": PROVIDER_ZAI,
}

_ZAI_MODEL_ALIASES = {
    "zai",
    "z.ai",
    "z-ai",
    "zchat",
}


def normalize_provider(value: str | None, default: str = PROVIDER_DEEPSEEK) -> str:
    normalized = _PROVIDER_ALIASES.get(str(value or "").strip().lower())
    if normalized:
        return normalized
    if value:
        raise ValueError(f"不支持的账号类型：{value}")
    return default


def provider_label(provider: str) -> str:
    return PROVIDER_LABELS.get(normalize_provider(provider), provider)


def zai_model_name() -> str:
    return os.environ.get("ZAI_MODEL_NAME", "glm-5.3-flash").strip() or "glm-5.3-flash"


def zai_upstream_model_name() -> str:
    return (
        os.environ.get("ZAI_UPSTREAM_MODEL", "x-preview-l").strip()
        or "x-preview-l"
    )


def provider_for_model(model: str | None) -> str:
    name = str(model or "").strip().lower()
    if (
        name in _ZAI_MODEL_ALIASES
        or name.startswith("glm-")
        or name == zai_model_name().lower()
        or name == zai_upstream_model_name().lower()
    ):
        return PROVIDER_ZAI
    return PROVIDER_DEEPSEEK


def zai_public_model(model: str | None) -> str:
    """Return the public z.ai model name used in API responses."""
    requested = str(model or "").strip()
    if not requested or requested.lower() in _ZAI_MODEL_ALIASES:
        return zai_model_name()
    return requested


def zai_upstream_model(model: str | None) -> str:
    """Map the public z.ai model name to the web API's concrete model id."""
    configured_public = zai_model_name().lower()
    configured_upstream = zai_upstream_model_name()
    requested = zai_public_model(model)
    if (
        requested.lower() == configured_public
        or requested.lower() in _ZAI_MODEL_ALIASES
    ):
        return configured_upstream
    return requested


def available_models(deepseek_model: str) -> list[str]:
    models = [deepseek_model]
    zai_model = zai_model_name()
    if zai_model not in models:
        models.append(zai_model)
    return models
