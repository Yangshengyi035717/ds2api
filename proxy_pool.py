"""Persistent upstream proxy pool used by individual account bindings."""
from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dotenv import load_dotenv
from curl_cffi import requests as cffi_requests

from adapter import IMPERSONATE
from crypto import (
    decrypt_str,
    detect_store_version,
    encrypt_str,
    is_enabled as crypto_is_enabled,
    STORE_VERSION_ENCRYPTED,
    STORE_VERSION_PLAIN,
)
from logger import get_logger

load_dotenv()

log = get_logger("proxy_pool")

DEFAULT_STORE_PATH = Path(__file__).resolve().parent / "data" / "proxies.json"
SUPPORTED_SCHEMES = {"http", "https", "socks4", "socks5", "socks5h"}
PROXY_CHECK_URL = os.environ.get("PROXY_CHECK_URL", "https://chat.deepseek.com/")


def _now() -> int:
    return int(time.time())


def _proxy_id() -> str:
    return f"proxy_{secrets.token_urlsafe(9)}"


def normalize_proxy_url(url: str) -> str:
    value = str(url or "").strip()
    if not value:
        raise ValueError("代理地址不能为空")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"代理地址格式无效：{exc}") from exc
    if parsed.scheme.lower() not in SUPPORTED_SCHEMES:
        raise ValueError("代理协议必须是 http、https、socks4、socks5 或 socks5h")
    if not parsed.hostname:
        raise ValueError("代理地址缺少主机名")
    if port is not None and not (0 < port < 65536):
        raise ValueError("代理端口无效")
    return value


def proxy_preview(url: str) -> str:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    auth = "***:***@" if parsed.username or parsed.password else ""
    return f"{parsed.scheme}://{auth}{host}{port}"


@dataclass
class ProxyEntry:
    url: str
    label: str = ""
    id: str = field(default_factory=_proxy_id)
    enabled: bool = True
    created_at: int = field(default_factory=_now)
    updated_at: int = field(default_factory=_now)
    last_check_at: int = 0
    last_check_ok: bool | None = None
    last_check_status: int | None = None
    last_check_latency_ms: int | None = None
    last_check_error: str = ""

    def public_dict(self, bound_accounts: list[str] | None = None) -> dict[str, Any]:
        accounts = bound_accounts or []
        return {
            "id": self.id,
            "label": self.label,
            "url_preview": proxy_preview(self.url),
            "enabled": self.enabled,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_check_at": self.last_check_at,
            "last_check_ok": self.last_check_ok,
            "last_check_status": self.last_check_status,
            "last_check_latency_ms": self.last_check_latency_ms,
            "last_check_error": self.last_check_error,
            "bound_accounts": accounts,
            "bound_count": len(accounts),
        }


class ProxyPool:
    """Thread-safe proxy CRUD with optional Fernet encryption at rest."""

    def __init__(self, path: str | Path | None = None):
        configured = path or os.environ.get("PROXY_STORE_PATH")
        self._path = Path(configured) if configured else DEFAULT_STORE_PATH
        if not self._path.is_absolute():
            self._path = Path(__file__).resolve().parent / self._path
        self._lock = threading.RLock()
        self._entries: list[ProxyEntry] = []
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("proxy_store_load_failed", extra={"path": str(self._path), "error": str(exc)})
            return

        version = detect_store_version(payload)
        if version == STORE_VERSION_ENCRYPTED and not crypto_is_enabled():
            log.error("encrypted_proxy_store_without_key", extra={"path": str(self._path)})
            return

        for raw in payload.get("proxies", []):
            try:
                item = dict(raw)
                if version == STORE_VERSION_ENCRYPTED:
                    item["url"] = decrypt_str(str(item.get("url") or ""))
                url = normalize_proxy_url(str(item.get("url") or ""))
                self._entries.append(
                    ProxyEntry(
                        id=str(item.get("id") or _proxy_id()),
                        label=str(item.get("label") or ""),
                        url=url,
                        enabled=bool(item.get("enabled", True)),
                        created_at=int(item.get("created_at") or _now()),
                        updated_at=int(item.get("updated_at") or _now()),
                    )
                )
            except Exception as exc:
                log.warning("proxy_entry_skipped", extra={"error": str(exc), "id": raw.get("id")})

    def _save_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        encrypted = crypto_is_enabled()
        proxies = []
        for entry in self._entries:
            item = {
                "id": entry.id,
                "label": entry.label,
                "enabled": entry.enabled,
                "created_at": entry.created_at,
                "updated_at": entry.updated_at,
                "url": encrypt_str(entry.url) if encrypted else entry.url,
            }
            proxies.append(item)

        payload = {
            "version": STORE_VERSION_ENCRYPTED if encrypted else STORE_VERSION_PLAIN,
            **({"encryption": "fernet"} if encrypted else {}),
            "proxies": proxies,
        }
        content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self._path.name}.",
            suffix=".tmp",
            dir=str(self._path.parent),
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(tmp_name, 0o600)
            except OSError:
                pass
            os.replace(tmp_name, self._path)
            try:
                os.chmod(self._path, 0o600)
            except OSError:
                pass
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def list_entries(self) -> list[ProxyEntry]:
        with self._lock:
            return list(self._entries)

    def get_by_id(self, proxy_id: str) -> ProxyEntry | None:
        with self._lock:
            return next((entry for entry in self._entries if entry.id == proxy_id), None)

    def resolve_url(self, proxy_id: str) -> str | None:
        entry = self.get_by_id(proxy_id)
        if entry is None or not entry.enabled:
            return None
        return entry.url

    def add(self, label: str, url: str, enabled: bool = True) -> ProxyEntry:
        normalized = normalize_proxy_url(url)
        display = (label or "").strip() or proxy_preview(normalized)
        with self._lock:
            if any(entry.url == normalized for entry in self._entries):
                raise ValueError("代理已存在")
            now = _now()
            entry = ProxyEntry(
                id=_proxy_id(),
                label=display,
                url=normalized,
                enabled=enabled,
                created_at=now,
                updated_at=now,
            )
            self._entries.append(entry)
            self._save_locked()
            return entry

    def update(
        self,
        proxy_id: str,
        label: str | None = None,
        url: str | None = None,
        enabled: bool | None = None,
    ) -> ProxyEntry:
        with self._lock:
            entry = next((item for item in self._entries if item.id == proxy_id), None)
            if entry is None:
                raise KeyError("Proxy not found")
            if url is not None and url.strip():
                normalized = normalize_proxy_url(url)
                if any(item.id != proxy_id and item.url == normalized for item in self._entries):
                    raise ValueError("代理已存在")
                entry.url = normalized
            if label is not None:
                entry.label = label.strip() or proxy_preview(entry.url)
            if enabled is not None:
                entry.enabled = enabled
            entry.updated_at = _now()
            self._save_locked()
            return entry

    def remove(self, proxy_id: str) -> bool:
        with self._lock:
            for index, entry in enumerate(self._entries):
                if entry.id != proxy_id:
                    continue
                self._entries.pop(index)
                self._save_locked()
                return True
            return False

    def test(self, proxy_id: str) -> dict[str, Any]:
        entry = self.get_by_id(proxy_id)
        if entry is None:
            raise KeyError("Proxy not found")
        if not entry.enabled:
            raise ValueError("代理已停用")

        started = time.time()
        client = cffi_requests.Session(
            impersonate=IMPERSONATE,
            timeout=15,
            proxies={"all": entry.url},
        )
        try:
            response = client.get(PROXY_CHECK_URL, allow_redirects=True)
            latency_ms = int((time.time() - started) * 1000)
            ok = response.status_code not in {407, 502, 503, 504}
            result = {
                "ok": ok,
                "status": response.status_code,
                "latency_ms": latency_ms,
                "error": "" if ok else f"HTTP {response.status_code}",
            }
        except Exception as exc:
            result = {
                "ok": False,
                "status": None,
                "latency_ms": int((time.time() - started) * 1000),
                "error": str(exc)[:240],
            }
        finally:
            client.close()

        with self._lock:
            entry.last_check_at = _now()
            entry.last_check_ok = bool(result["ok"])
            entry.last_check_status = result["status"]
            entry.last_check_latency_ms = int(result["latency_ms"])
            entry.last_check_error = str(result["error"])
        return result


_default_pool: ProxyPool | None = None
_default_lock = threading.Lock()


def get_proxy_pool() -> ProxyPool:
    global _default_pool
    with _default_lock:
        if _default_pool is None:
            _default_pool = ProxyPool()
        return _default_pool
