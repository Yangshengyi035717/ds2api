"""Persisted runtime settings configured from the WebUI."""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger("ds2api.runtime_config")

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "data" / "runtime_config.json"
DEFAULT_MODEL_NAME = "deepseek-chat"
MAX_MODEL_NAME_LENGTH = 128
DEFAULT_PRICING: dict[str, Any] = {
    "currency": "USD",
    "billing_mode": "token",
    "input_price": None,
    "output_price": None,
    "cache_read_price": None,
    "cache_write_price": None,
    "per_request_price": None,
    "rate_multiplier": 1.0,
}
BILLING_MODES = {"token", "per_request", "image"}


def _normalize_model_name(name: str) -> str:
    value = str(name or "").strip()
    if not value:
        raise ValueError("模型名称不能为空")
    if len(value) > MAX_MODEL_NAME_LENGTH:
        raise ValueError(f"模型名称不能超过 {MAX_MODEL_NAME_LENGTH} 个字符")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("模型名称不能包含控制字符")
    return value


def _normalize_display_name(name: str) -> str:
    value = str(name or "").strip()
    if not value:
        raise ValueError("显示名称不能为空")
    if len(value) > MAX_MODEL_NAME_LENGTH:
        raise ValueError(f"显示名称不能超过 {MAX_MODEL_NAME_LENGTH} 个字符")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("显示名称不能包含控制字符")
    return value


def _normalize_optional_price(value: Any, field: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        price = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是数字") from exc
    if price < 0:
        raise ValueError(f"{field} 不能小于 0")
    return price


def _normalize_pricing(raw: Any) -> dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    currency = str(source.get("currency") or "USD").strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise ValueError("币种必须是 3 位字母代码")
    billing_mode = str(source.get("billing_mode") or "token").strip().lower()
    if billing_mode not in BILLING_MODES:
        raise ValueError("计费模式必须是 token、per_request 或 image")
    rate_multiplier = _normalize_optional_price(
        source.get("rate_multiplier", 1.0),
        "rate_multiplier",
    )
    return {
        "currency": currency,
        "billing_mode": billing_mode,
        "input_price": _normalize_optional_price(source.get("input_price"), "input_price"),
        "output_price": _normalize_optional_price(source.get("output_price"), "output_price"),
        "cache_read_price": _normalize_optional_price(
            source.get("cache_read_price"),
            "cache_read_price",
        ),
        "cache_write_price": _normalize_optional_price(
            source.get("cache_write_price"),
            "cache_write_price",
        ),
        "per_request_price": _normalize_optional_price(
            source.get("per_request_price"),
            "per_request_price",
        ),
        "rate_multiplier": 1.0 if rate_multiplier is None else rate_multiplier,
    }


class RuntimeConfigStore:
    """Thread-safe, atomically persisted runtime configuration."""

    def __init__(
        self,
        path: str | Path | None = None,
        default_model_name: str | None = None,
    ) -> None:
        configured_path = path or os.environ.get("RUNTIME_CONFIG_PATH")
        self._path = Path(configured_path) if configured_path else DEFAULT_CONFIG_PATH

        env_model_name = os.environ.get("MODEL_NAME", "").strip()
        fallback = default_model_name or env_model_name or DEFAULT_MODEL_NAME
        self._default_model_name = _normalize_model_name(fallback)
        self._default_source = "env" if env_model_name else "default"
        self._model_name = self._default_model_name
        self._display_name = (
            "DeepSeek Chat"
            if self._model_name == "deepseek-chat"
            else self._model_name
        )
        self._pricing = dict(DEFAULT_PRICING)
        self._source = self._default_source
        self._persisted = False
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("配置根节点必须是对象")
            model_name = _normalize_model_name(str(raw.get("model_name", "")))
            display_name = _normalize_display_name(
                str(
                    raw.get("display_name")
                    or (
                        "DeepSeek Chat"
                        if model_name == "deepseek-chat"
                        else model_name
                    )
                )
            )
            pricing = _normalize_pricing(raw.get("pricing"))
        except Exception as exc:
            log.warning(
                "runtime_config_load_failed path=%s error=%s",
                self._path,
                exc,
            )
            return

        self._model_name = model_name
        self._display_name = display_name
        self._pricing = pricing
        self._source = "webui"
        self._persisted = True

    @property
    def model_name(self) -> str:
        with self._lock:
            return self._model_name

    def set_model_name(self, name: str) -> str:
        return str(self.update_settings(model_name=name)["model_name"])

    def update_settings(
        self,
        model_name: str | None = None,
        display_name: str | None = None,
        pricing: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            normalized_model = (
                self._model_name
                if model_name is None
                else _normalize_model_name(model_name)
            )
            normalized_display = (
                self._display_name
                if display_name is None
                else _normalize_display_name(display_name)
            )
            merged_pricing = dict(self._pricing)
            if pricing is not None:
                merged_pricing.update(pricing)
            normalized_pricing = _normalize_pricing(merged_pricing)
            self._model_name = normalized_model
            self._display_name = normalized_display
            self._pricing = normalized_pricing
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self._path.with_name(f"{self._path.name}.tmp")
            temp_path.write_text(
                json.dumps(self._payload_locked(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temp_path, self._path)
            self._source = "webui"
            self._persisted = True
            return self.snapshot()

    def _payload_locked(self) -> dict[str, Any]:
        return {
            "version": 1,
            "model_name": self._model_name,
            "display_name": self._display_name,
            "pricing": dict(self._pricing),
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "model_name": self._model_name,
                "display_name": self._display_name,
                "pricing": dict(self._pricing),
                "source": self._source,
                "persisted": self._persisted,
                "default_model_name": self._default_model_name,
            }


_default_store: RuntimeConfigStore | None = None
_default_store_lock = threading.Lock()


def get_runtime_config() -> RuntimeConfigStore:
    global _default_store
    with _default_store_lock:
        if _default_store is None:
            _default_store = RuntimeConfigStore()
        return _default_store
