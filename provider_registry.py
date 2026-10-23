"""Provider constants and model routing shared by the API and account pool."""
from __future__ import annotations

import os

PROVIDER_DEEPSEEK = "deepseek"
PROVIDER_ZAI = "zai"
PROVIDER_KIMI = "kimi"
PROVIDER_QWEN = "qianwen"
PROVIDER_HUNYUAN = "hunyuan"
PROVIDER_WENXIN = "wenxin"
PROVIDER_DOUBAO = "doubao"
PROVIDER_MIMO = "mimo"
SUPPORTED_PROVIDERS = (
    PROVIDER_DEEPSEEK,
    PROVIDER_ZAI,
    PROVIDER_KIMI,
    PROVIDER_QWEN,
    PROVIDER_HUNYUAN,
    PROVIDER_WENXIN,
    PROVIDER_DOUBAO,
    PROVIDER_MIMO,
)

PROVIDER_LABELS = {
    PROVIDER_DEEPSEEK: "DeepSeek",
    PROVIDER_ZAI: "z.ai",
    PROVIDER_KIMI: "Kimi",
    PROVIDER_QWEN: "千问",
    PROVIDER_HUNYUAN: "混元",
    PROVIDER_WENXIN: "文心",
    PROVIDER_DOUBAO: "豆包",
    PROVIDER_MIMO: "小米 MiMo",
}

_PROVIDER_ALIASES = {
    "deepseek": PROVIDER_DEEPSEEK,
    "deepseek-chat": PROVIDER_DEEPSEEK,
    "zai": PROVIDER_ZAI,
    "z.ai": PROVIDER_ZAI,
    "z-ai": PROVIDER_ZAI,
    "zchat": PROVIDER_ZAI,
    "kimi": PROVIDER_KIMI,
    "kimi.com": PROVIDER_KIMI,
    "moonshot": PROVIDER_KIMI,
    "qianwen": PROVIDER_QWEN,
    "qwen": PROVIDER_QWEN,
    "tongyi": PROVIDER_QWEN,
    "hunyuan": PROVIDER_HUNYUAN,
    "tencent": PROVIDER_HUNYUAN,
    "tencent-hunyuan": PROVIDER_HUNYUAN,
    "aistudio": PROVIDER_HUNYUAN,
    "wenxin": PROVIDER_WENXIN,
    "ernie": PROVIDER_WENXIN,
    "baidu": PROVIDER_WENXIN,
    "baidu-wenxin": PROVIDER_WENXIN,
    "doubao": PROVIDER_DOUBAO,
    "byte-dance": PROVIDER_DOUBAO,
    "bytedance": PROVIDER_DOUBAO,
    "豆包": PROVIDER_DOUBAO,
    "xiaomi": PROVIDER_MIMO,
    "xiaomimimo": PROVIDER_MIMO,
    "mimo": PROVIDER_MIMO,
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


def kimi_model_name() -> str:
    return os.environ.get("KIMI_MODEL_NAME", "kimi-k2.6").strip() or "kimi-k2.6"


def kimi_upstream_model_name() -> str:
    return (
        os.environ.get("KIMI_UPSTREAM_MODEL", "k2d6-chat").strip()
        or "k2d6-chat"
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
    if (
        name.startswith("kimi-")
        or name in {"k2d6-chat", "k3-agent", "k28-agent-preview"}
        or name == kimi_model_name().lower()
        or name == kimi_upstream_model_name().lower()
    ):
        return PROVIDER_KIMI
    if (
        name.startswith("qwen")
        or name in {"qianwen", "tongyi", "qwen3.7-max", "qwen3.7-max"}
        or name == qwen_upstream_model_name().lower()
    ):
        return PROVIDER_QWEN
    if (
        name.startswith("hunyuan")
        or name in {"hy4-preview", "tencent", "tencent-hunyuan", "aistudio"}
        or name == hunyuan_model_name().lower()
        or name == hunyuan_upstream_model_name().lower()
    ):
        return PROVIDER_HUNYUAN
    if (
        name.startswith("wenxin")
        or name.startswith("ernie")
        or name in {"baidu", "baidu-wenxin"}
        or name == wenxin_model_name().lower()
    ):
        return PROVIDER_WENXIN
    if (
        name.startswith("doubao")
        or name in {"bytedance", "byte-dance"}
        or name == doubao_model_name().lower()
    ):
        return PROVIDER_DOUBAO
    if (
        name.startswith("mimo-")
        or name in {"xiaomi", "xiaomimimo", "mimo"}
        or name == mimo_model_name().lower()
        or name == mimo_upstream_model_name().lower()
        or name in {item.lower() for item in available_mimo_models()}
    ):
        return PROVIDER_MIMO
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


def kimi_public_model(model: str | None) -> str:
    requested = str(model or "").strip()
    if not requested or requested.lower() in {"kimi", "kimi.com", "moonshot"}:
        return kimi_model_name()
    return requested


def kimi_upstream_model(model: str | None) -> str:
    requested = kimi_public_model(model)
    aliases = {
        kimi_model_name().lower(): kimi_upstream_model_name(),
        "kimi-k2.6": "k2d6-chat",
        "kimi-k2d6": "k2d6-chat",
        "kimi-k3": "k3-agent",
        "kimi-k2.8": "k28-agent-preview",
    }
    return aliases.get(requested.lower(), requested)


def available_kimi_models() -> list[str]:
    configured = kimi_model_name()
    models = [configured]
    for item in (
        os.environ.get("KIMI_K3_MODEL_NAME", "kimi-k3").strip(),
        os.environ.get("KIMI_K28_MODEL_NAME", "kimi-k2.8").strip(),
    ):
        if item and item not in models:
            models.append(item)
    return models


def qwen_model_name() -> str:
    return os.environ.get("QWEN_MODEL_NAME", "qwen3.7-max").strip() or "qwen3.7-max"


def qwen_upstream_model_name() -> str:
    return os.environ.get("QWEN_UPSTREAM_MODEL", "Qwen3.7-Max").strip() or "Qwen3.7-Max"


def qwen_upstream_model(model: str | None) -> str:
    requested = str(model or "").strip()
    if (
        not requested
        or requested.lower() in {"qwen", "qianwen", "tongyi"}
        or requested.lower() == qwen_model_name().lower()
    ):
        return qwen_upstream_model_name()
    return requested


def qwen_public_model(model: str | None) -> str:
    requested = str(model or "").strip()
    if not requested or requested.lower() in {"qwen", "qianwen", "tongyi"}:
        return qwen_model_name()
    return requested


def hunyuan_model_name() -> str:
    return (
        os.environ.get("HUNYUAN_MODEL_NAME", "hunyuan-hy4-preview").strip()
        or "hunyuan-hy4-preview"
    )


def hunyuan_upstream_model_name() -> str:
    return (
        os.environ.get("HUNYUAN_UPSTREAM_MODEL", "hy4-preview").strip()
        or "hy4-preview"
    )


def hunyuan_public_model(model: str | None) -> str:
    requested = str(model or "").strip()
    if not requested or requested.lower() in {
        "hunyuan",
        "tencent",
        "tencent-hunyuan",
        "aistudio",
    }:
        return hunyuan_model_name()
    return requested


def hunyuan_upstream_model(model: str | None) -> str:
    requested = hunyuan_public_model(model)
    aliases = {
        hunyuan_model_name().lower(): hunyuan_upstream_model_name(),
        "hunyuan-hy4-preview": "hy4-preview",
        "hy4-preview": "hy4-preview",
    }
    return aliases.get(requested.lower(), requested)


def available_hunyuan_models() -> list[str]:
    return [hunyuan_model_name()]


def wenxin_model_name() -> str:
    return os.environ.get("WENXIN_MODEL_NAME", "wenxin-smart-mode").strip() or "wenxin-smart-mode"


def wenxin_upstream_model_name() -> str:
    return os.environ.get("WENXIN_UPSTREAM_MODEL", "smartMode").strip() or "smartMode"


def wenxin_public_model(model: str | None) -> str:
    requested = str(model or "").strip()
    if not requested or requested.lower() in {
        "wenxin",
        "ernie",
        "baidu",
        "baidu-wenxin",
    }:
        return wenxin_model_name()
    return requested


def wenxin_upstream_model(model: str | None) -> str:
    requested = wenxin_public_model(model)
    aliases = {
        wenxin_model_name().lower(): wenxin_upstream_model_name(),
        "wenxin-smart-mode": "smartMode",
    }
    return aliases.get(requested.lower(), requested)


def available_wenxin_models() -> list[str]:
    return [wenxin_model_name()]


def doubao_model_name() -> str:
    return os.environ.get("DOUBAO_MODEL_NAME", "doubao-seed").strip() or "doubao-seed"


def doubao_upstream_model_name() -> str:
    return os.environ.get("DOUBAO_UPSTREAM_MODEL", "0").strip() or "0"


def doubao_public_model(model: str | None) -> str:
    requested = str(model or "").strip()
    if not requested or requested.lower() in {
        "doubao",
        "bytedance",
        "byte-dance",
        "豆包",
    }:
        return doubao_model_name()
    return requested


def doubao_upstream_model(model: str | None) -> str:
    requested = doubao_public_model(model)
    aliases = {
        doubao_model_name().lower(): doubao_upstream_model_name(),
        "doubao-seed": "0",
    }
    return aliases.get(requested.lower(), requested)


def available_doubao_models() -> list[str]:
    return [doubao_model_name()]


def mimo_model_name() -> str:
    return os.environ.get("MIMO_MODEL_NAME", "mimo-v2.6-pro").strip() or "mimo-v2.6-pro"


def mimo_upstream_model_name() -> str:
    return os.environ.get("MIMO_UPSTREAM_MODEL", "mimo-v2.6-pro").strip() or "mimo-v2.6-pro"


def mimo_public_model(model: str | None) -> str:
    requested = str(model or "").strip()
    if not requested or requested.lower() in {"xiaomi", "xiaomimimo", "mimo"}:
        return mimo_model_name()
    return requested


def mimo_upstream_model(model: str | None) -> str:
    requested = mimo_public_model(model)
    aliases = {
        "mimo-v2.6-pro": "mimo-v2.6-pro",
        "mimo-v2.6-flash": "mimo-v2.6-flash",
        "mimo-v2.6-pro-ultraspeed-studio": "mimo-v2.6-pro-ultraspeed-studio",
        mimo_model_name().lower(): mimo_upstream_model_name(),
    }
    return aliases.get(requested.lower(), requested)


def available_mimo_models() -> list[str]:
    return list(dict.fromkeys([
        mimo_model_name(),
        "mimo-v2.6-flash",
        "mimo-v2.6-pro-ultraspeed-studio",
    ]))


def available_models(deepseek_model: str) -> list[str]:
    models = [deepseek_model]
    zai_model = zai_model_name()
    if zai_model not in models:
        models.append(zai_model)
    return models
