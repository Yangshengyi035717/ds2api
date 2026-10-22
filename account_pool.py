"""
Account pool — multi-account management for DeepSeek Chat proxy.

Manages multiple DeepSeek accounts, tracks their states (idle/busy/error),
provides credential health checking, session lifecycle, and persistent
panel-managed accounts.
"""
import hashlib
import json
import os
import secrets
import tempfile
import time
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from adapter import DeepSeekAdapter
from zai_adapter import ZaiAdapter, zai_user_id_from_token
from kimi_adapter import KimiAdapter, kimi_device_id_from_token, kimi_user_id_from_token
from qwen_adapter import QwenAdapter
from hunyuan_adapter import (
    HunyuanAdapter,
    hunyuan_token_from_cookies,
    hunyuan_user_id_from_cookies,
)
from wenxin_adapter import WenxinAdapter, wenxin_baiduid, wenxin_cookie_value
from doubao_adapter import DoubaoAdapter, doubao_cookie_value
from provider_registry import (
    PROVIDER_DEEPSEEK,
    PROVIDER_DOUBAO,
    PROVIDER_HUNYUAN,
    PROVIDER_KIMI,
    PROVIDER_QWEN,
    PROVIDER_WENXIN,
    PROVIDER_ZAI,
    normalize_provider,
    provider_label,
)
from crypto import (
    is_enabled as crypto_is_enabled,
    encrypt_account_dict,
    decrypt_account_dict,
    detect_store_version,
    STORE_VERSION_ENCRYPTED,
    STORE_VERSION_PLAIN,
    maybe_upgrade_store_file,
)
from logger import get_logger
from proxy_pool import get_proxy_pool, proxy_preview

load_dotenv()

log = get_logger("account_pool")

STORE_VERSION = 1
DEFAULT_STORE_PATH = Path(__file__).resolve().parent / "data" / "accounts.json"

# Auto-recovery: when an account has accumulated this many errors,
# schedule a background health check to clear the error state if the
# upstream is reachable again. Set to 0 to disable (every error is
# permanent until manual relogin).
_RECOVER_ERROR_THRESHOLD = 3
_RECOVER_FLAG_DISABLED = os.environ.get("DISABLE_AUTO_RECOVER", "").lower() in {
    "1", "true", "yes", "on",
}


def _now() -> int:
    return int(time.time())


def _account_id(prefix: str = "acc") -> str:
    return f"{prefix}_{secrets.token_urlsafe(9)}"


def _safe_provider(value: str | None) -> str:
    try:
        return normalize_provider(value)
    except ValueError:
        return PROVIDER_DEEPSEEK


def _credential_fingerprint(
    token: str,
    cookies: str,
    provider: str = PROVIDER_DEEPSEEK,
    user_id: str = "",
    refresh_token: str = "",
) -> str:
    return hashlib.sha256(
        f"{provider}\0{user_id}\0{refresh_token}\0{token}\0{cookies}".encode()
    ).hexdigest()


def _mask_secret(value: str, start: int = 6, end: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= start + end:
        return "*" * len(value)
    return f"{value[:start]}...{value[-end:]}"


def _cookie_names(cookies: str, limit: int = 6) -> str:
    names = []
    for part in cookies.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        names.append(part.split("=", 1)[0].strip())
    if not names:
        return _mask_secret(cookies, 12, 4)
    suffix = "" if len(names) <= limit else f", +{len(names) - limit} more"
    return ", ".join(names[:limit]) + suffix


@dataclass
class Account:
    """A single DeepSeek account with credentials and runtime state."""
    token: str
    cookies: str
    email: str = ""
    provider: str = PROVIDER_DEEPSEEK
    user_id: str = ""          # z.ai signature user id
    refresh_token: str = ""     # Kimi rotating refresh token
    xsrf_token: str = ""
    session_id: str = ""
    topic_id: str = ""
    device_id: str = ""         # browser device that owns the token (optional)
    id: str = field(default_factory=_account_id)
    source: str = "file"       # persisted account
    proxy: str = ""             # legacy/manual direct proxy URL (optional)
    proxy_id: str = ""          # managed proxy-pool binding
    created_at: int = field(default_factory=_now)
    updated_at: int = field(default_factory=_now)
    state: str = "idle"        # idle | busy | error
    error_count: int = 0
    last_error: str = ""
    last_used: float = 0.0
    _adapter: Optional[
        DeepSeekAdapter | ZaiAdapter | KimiAdapter | QwenAdapter | HunyuanAdapter
        | WenxinAdapter
        | DoubaoAdapter
    ] = field(default=None, repr=False)

    @property
    def adapter(
        self,
    ) -> (
        DeepSeekAdapter
        | ZaiAdapter
        | KimiAdapter
        | QwenAdapter
        | HunyuanAdapter
        | WenxinAdapter
        | DoubaoAdapter
    ):
        if self._adapter is None:
            proxy_url = self.proxy.strip()
            if not proxy_url and self.proxy_id:
                proxy_url = get_proxy_pool().resolve_url(self.proxy_id) or ""
                if not proxy_url:
                    raise ValueError("绑定的代理不存在或已停用")
            if self.provider == PROVIDER_ZAI:
                self._adapter = ZaiAdapter(
                    token=self.token,
                    cookies=self.cookies,
                    proxy=proxy_url or None,
                    device_id=self.device_id,
                    user_id=self.user_id,
                )
            elif self.provider == PROVIDER_KIMI:
                self._adapter = KimiAdapter(
                    token=self.token,
                    refresh_token=self.refresh_token,
                    cookies=self.cookies,
                    proxy=proxy_url or None,
                    device_id=self.device_id,
                    user_id=self.user_id,
                )
            elif self.provider == PROVIDER_QWEN:
                self._adapter = QwenAdapter(
                    token=self.token,
                    cookies=self.cookies,
                    device_id=self.device_id,
                    user_id=self.user_id,
                    xsrf_token=self.xsrf_token,
                    session_id=self.session_id,
                    topic_id=self.topic_id,
                    proxy=proxy_url or None,
                )
            elif self.provider == PROVIDER_HUNYUAN:
                self._adapter = HunyuanAdapter(
                    token=self.token,
                    cookies=self.cookies,
                    user_id=self.user_id,
                    device_id=self.device_id,
                    session_id=self.session_id,
                    topic_id=self.topic_id,
                    proxy=proxy_url or None,
                )
            elif self.provider == PROVIDER_WENXIN:
                self._adapter = WenxinAdapter(
                    token=self.token,
                    lid=self.user_id,
                    cookies=self.cookies,
                    device_id=self.device_id,
                    user_id=self.user_id,
                    session_id=self.session_id,
                    proxy=proxy_url or None,
                )
            elif self.provider == PROVIDER_DOUBAO:
                self._adapter = DoubaoAdapter(
                    cookies=self.cookies,
                    device_id=self.device_id,
                    web_id=self.user_id,
                    tea_uuid=self.topic_id,
                    session_id=self.session_id,
                    request_url=self.xsrf_token,
                    request_body=self.refresh_token,
                    proxy=proxy_url or None,
                )
            else:
                self._adapter = DeepSeekAdapter(
                    token=self.token,
                    cookies=self.cookies,
                    proxy=proxy_url or None,
                    device_id=self.device_id,
                )
        return self._adapter

    @property
    def fingerprint(self) -> str:
        return _credential_fingerprint(
            self.token,
            self.cookies,
            self.provider,
            self.user_id,
            self.refresh_token,
        )

    def to_dict(self) -> dict:
        proxy_entry = get_proxy_pool().get_by_id(self.proxy_id) if self.proxy_id else None
        proxy_url = self.proxy.strip() or (proxy_entry.url if proxy_entry else "")
        return {
            "id": self.id,
            "email": self.email,
            "source": self.source,
            "provider": self.provider,
            "provider_label": provider_label(self.provider),
            "state": self.state,
            "error_count": self.error_count,
            "last_error": self.last_error,
            "last_used": int(self.last_used),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "token_preview": _mask_secret(self.token),
            "cookies_preview": _cookie_names(self.cookies),
            "device_id_preview": _mask_secret(self.device_id, 8, 4),
            "has_device_id": bool(self.device_id),
            "user_id_preview": _mask_secret(self.user_id, 8, 4),
            "has_user_id": bool(self.user_id),
            "refresh_token_preview": _mask_secret(self.refresh_token, 8, 4),
            "has_refresh_token": bool(self.refresh_token),
            "xsrf_token_preview": _mask_secret(self.xsrf_token, 8, 4),
            "has_xsrf_token": bool(self.xsrf_token),
            "session_id": self.session_id,
            "topic_id": self.topic_id,
            "credential_fingerprint": self.fingerprint[:12],
            "proxy_id": self.proxy_id,
            "proxy_label": proxy_entry.label if proxy_entry else "",
            "proxy_preview": proxy_preview(proxy_url) if proxy_url else "",
            "proxy_enabled": proxy_entry.enabled if proxy_entry else bool(self.proxy),
        }

    def to_store_dict(self) -> dict:
        return {
            "id": self.id,
            "email": self.email,
            "provider": self.provider,
            "user_id": self.user_id,
            "refresh_token": self.refresh_token,
            "xsrf_token": self.xsrf_token,
            "session_id": self.session_id,
            "topic_id": self.topic_id,
            "token": self.token,
            "cookies": self.cookies,
            "device_id": self.device_id,
            "proxy": self.proxy,
            "proxy_id": self.proxy_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class AccountPool:
    """Thread-safe pool of DeepSeek accounts with round-robin selection."""

    def __init__(self):
        self._lock = threading.Lock()
        self._accounts: list[Account] = []
        self._next_idx = 0
        configured_store = Path(os.environ.get("ACCOUNT_STORE_PATH", str(DEFAULT_STORE_PATH)))
        if not configured_store.is_absolute():
            configured_store = Path(__file__).resolve().parent / configured_store
        self._store_path = configured_store
        self._load_persisted_accounts()

    # ── Loading / persistence ────────────────────────────────────

    def _append_loaded(self, acct: Account):
        if any(a.fingerprint == acct.fingerprint for a in self._accounts):
            log.warning("skipping_duplicate_account", extra={"email": acct.email, "id": acct.id})
            return
        if acct.provider == PROVIDER_ZAI and not acct.user_id:
            acct.state = "error"
            acct.last_error = "z.ai account is missing user_id"
        if acct.provider == PROVIDER_KIMI and not acct.refresh_token:
            acct.state = "error"
            acct.last_error = "Kimi account is missing refresh_token"
        if acct.provider == PROVIDER_QWEN and (
            not acct.cookies or not acct.session_id
        ):
            acct.state = "error"
            acct.last_error = "Qianwen account is missing cookies/session_id"
        if acct.provider == PROVIDER_HUNYUAN and (
            not acct.cookies or not acct.token or not acct.user_id
        ):
            acct.state = "error"
            acct.last_error = "Hunyuan account is missing cookies/user"
        if acct.provider == PROVIDER_WENXIN and (
            not acct.cookies
        ):
            acct.state = "error"
            acct.last_error = "Wenxin account is missing login cookies"
        if acct.provider == PROVIDER_DOUBAO and (
            not acct.cookies or not doubao_cookie_value(acct.cookies, "sessionid")
        ):
            acct.state = "error"
            acct.last_error = "Doubao account is missing sessionid cookies"
        if acct.proxy_id:
            proxy_entry = get_proxy_pool().get_by_id(acct.proxy_id)
            if proxy_entry is None or not proxy_entry.enabled:
                acct.state = "error"
                acct.last_error = "Bound proxy not found or disabled"
            elif any(a.proxy_id == acct.proxy_id for a in self._accounts):
                log.warning(
                    "duplicate_proxy_binding_loaded",
                    extra={"proxy_id": acct.proxy_id, "account_id": acct.id},
                )
                acct.state = "error"
                acct.last_error = "Proxy already bound to another account"
        self._accounts.append(acct)

    def _load_persisted_accounts(self):
        path = self._store_path
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("failed_to_load_account_store", extra={"path": str(path), "error": str(e)})
            return

        version = detect_store_version(data)
        if version == STORE_VERSION_ENCRYPTED and not crypto_is_enabled():
            log.error("encrypted_store_without_key", extra={"path": str(path)})
            return

        # If we're encrypted, rewrite the file as v2 in place (one-shot migration).
        if version == STORE_VERSION_PLAIN and crypto_is_enabled():
            try:
                maybe_upgrade_store_file(path)
                log.info("upgraded_account_store_to_encrypted", extra={"path": str(path)})
            except Exception as e:
                log.warning("account_store_upgrade_failed", extra={"path": str(path), "error": str(e)})

        for item in data.get("accounts", []):
            # Decrypt in-memory copy if v2.
            item = decrypt_account_dict(item) if version == STORE_VERSION_ENCRYPTED else item
            token = str(item.get("token") or "").strip()
            cookies = str(item.get("cookies") or "").strip()
            loaded_provider = _safe_provider(item.get("provider"))
            if not token and loaded_provider not in {
                PROVIDER_QWEN,
                PROVIDER_HUNYUAN,
                PROVIDER_WENXIN,
                PROVIDER_DOUBAO,
            }:
                log.warning("skipping_account_missing_credentials", extra={"id": item.get("id")})
                continue
            created_at = int(item.get("created_at") or _now())
            updated_at = int(item.get("updated_at") or created_at)
            account_value = Account(
                id=str(item.get("id") or _account_id()),
                email=str(item.get("email") or ""),
                provider=loaded_provider,
                user_id=str(item.get("user_id") or "").strip(),
                refresh_token=str(item.get("refresh_token") or "").strip(),
                xsrf_token=str(item.get("xsrf_token") or "").strip(),
                session_id=str(item.get("session_id") or "").strip(),
                topic_id=str(item.get("topic_id") or "").strip(),
                token=token,
                cookies=cookies,
                device_id=str(item.get("device_id") or "").strip(),
                proxy=str(item.get("proxy") or "").strip(),
                proxy_id=str(item.get("proxy_id") or "").strip(),
                source="file",
                created_at=created_at,
                updated_at=updated_at,
            )
            if account_value.provider == PROVIDER_KIMI:
                account_value.user_id = (
                    account_value.user_id
                    or kimi_user_id_from_token(account_value.token)
                )
                account_value.device_id = (
                    account_value.device_id
                    or kimi_device_id_from_token(account_value.token)
                )
            if account_value.provider == PROVIDER_HUNYUAN:
                account_value.token = (
                    account_value.token
                    or hunyuan_token_from_cookies(account_value.cookies)
                )
                account_value.user_id = (
                    account_value.user_id
                    or hunyuan_user_id_from_cookies(account_value.cookies)
                )
            self._append_loaded(account_value)

    def _ensure_store_dir(self):
        self._store_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self._store_path.parent, 0o700)

    def _save_persisted_accounts_locked(self):
        self._ensure_store_dir()
        if crypto_is_enabled():
            accounts = [encrypt_account_dict(a.to_store_dict()) for a in self._accounts]
            data = {
                "version": STORE_VERSION_ENCRYPTED,
                "encryption": "fernet",
                "accounts": accounts,
            }
        else:
            data = {
                "version": STORE_VERSION_PLAIN,
                "accounts": [a.to_store_dict() for a in self._accounts],
            }
        payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"

        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self._store_path.name}.",
            suffix=".tmp",
            dir=str(self._store_path.parent),
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, self._store_path)
            os.chmod(self._store_path, 0o600)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    # ── CRUD ───────────────────────────────────────────────────

    def add(
        self,
        token: str,
        cookies: str,
        email: str = "",
        proxy_id: str = "",
        device_id: str = "",
        provider: str = PROVIDER_DEEPSEEK,
        user_id: str = "",
        refresh_token: str = "",
        xsrf_token: str = "",
        session_id: str = "",
        topic_id: str = "",
    ) -> Account:
        token = (token or "").strip()
        cookies = (cookies or "").strip()
        email = (email or "").strip()
        device_id = (device_id or "").strip()
        provider = normalize_provider(provider)
        user_id = (user_id or "").strip()
        if provider == PROVIDER_ZAI and not user_id:
            user_id = zai_user_id_from_token(token)
        if provider == PROVIDER_KIMI:
            user_id = user_id or kimi_user_id_from_token(token)
            device_id = device_id or kimi_device_id_from_token(token)
        if provider == PROVIDER_HUNYUAN:
            token = token or hunyuan_token_from_cookies(cookies)
            user_id = user_id or hunyuan_user_id_from_cookies(cookies)
        if provider == PROVIDER_WENXIN and not user_id:
            user_id = wenxin_baiduid(cookies)
        if provider == PROVIDER_DOUBAO and not user_id:
            user_id = (
                doubao_cookie_value(cookies, "web_id")
                or doubao_cookie_value(cookies, "ttwid")
            )
        if provider == PROVIDER_DOUBAO and not device_id:
            device_id = doubao_cookie_value(cookies, "device_id")
        if not token and provider not in {
            PROVIDER_QWEN,
            PROVIDER_HUNYUAN,
            PROVIDER_WENXIN,
            PROVIDER_DOUBAO,
        }:
            raise ValueError("Token is required")
        if provider == PROVIDER_ZAI and not user_id:
            raise ValueError("z.ai 账号需要 User ID")
        if provider == PROVIDER_KIMI and not refresh_token:
            raise ValueError("Kimi 账号需要 refresh_token")
        if provider == PROVIDER_QWEN and (not cookies or not session_id):
            raise ValueError("千问账号需要 cookies 和 session_id")
        if provider == PROVIDER_HUNYUAN and (
            not cookies or not token or not user_id
        ):
            raise ValueError(
                "Hunyuan 账号需要包含 hunyuan_token 和 hunyuan_user 的 cookies"
            )
        if provider == PROVIDER_WENXIN and (
            not cookies or not wenxin_cookie_value(cookies, "BDUSS")
        ):
            raise ValueError("Wenxin 账号需要包含 BDUSS 的登录 Cookie")
        if provider == PROVIDER_DOUBAO and (
            not cookies or not doubao_cookie_value(cookies, "sessionid")
        ):
            raise ValueError("Doubao 账号需要包含 sessionid 的登录 Cookie")

        with self._lock:
            fp = _credential_fingerprint(
                token,
                cookies,
                provider,
                user_id,
                refresh_token,
            )
            if any(a.fingerprint == fp for a in self._accounts):
                raise ValueError("Account already exists")
            proxy_id = self._validate_proxy_binding_locked(proxy_id)
            if not email:
                email = f"acc-{len(self._accounts) + 1}"
            now = _now()
            acct = Account(
                id=_account_id(),
                token=token,
                cookies=cookies,
                email=email,
                device_id=device_id,
                provider=provider,
                user_id=user_id,
                refresh_token=refresh_token,
                xsrf_token=xsrf_token,
                session_id=session_id,
                topic_id=topic_id,
                source="file",
                proxy_id=proxy_id,
                created_at=now,
                updated_at=now,
            )
            self._accounts.append(acct)
            self._save_persisted_accounts_locked()
            return acct

    def get_by_id(self, account_id: str) -> Optional[Account]:
        with self._lock:
            return next((a for a in self._accounts if a.id == account_id), None)

    def keepalive_accounts(self) -> list[tuple[str, str]]:
        """Return the stable account/provider pairs used by the scheduler."""
        with self._lock:
            return [(account.id, account.provider) for account in self._accounts]

    def update_keepalive_credentials(
        self,
        account_id: str,
        *,
        token: str | None = None,
        refresh_token: str | None = None,
        cookies: str | None = None,
        device_id: str | None = None,
    ) -> Account:
        """Persist session rotation without changing API scheduling state."""
        with self._lock:
            acct = next((a for a in self._accounts if a.id == account_id), None)
            if acct is None:
                raise KeyError("Account not found")
            new_cookies = acct.cookies if cookies is None else cookies.strip()
            new_device_id = (
                acct.device_id if device_id is None else device_id.strip()
            )
            new_token = acct.token if token is None else token.strip()
            new_refresh_token = (
                acct.refresh_token
                if refresh_token is None
                else refresh_token.strip()
            )
            if (
                new_cookies == acct.cookies
                and new_device_id == acct.device_id
                and new_token == acct.token
                and new_refresh_token == acct.refresh_token
            ):
                return acct
            acct.token = new_token
            acct.refresh_token = new_refresh_token
            acct.cookies = new_cookies
            acct.device_id = new_device_id
            acct.updated_at = _now()
            acct._adapter = None
            self._save_persisted_accounts_locked()
            return acct

    def persist_adapter_tokens(self, acct: Account) -> bool:
        """Persist Kimi access/refresh rotation from a live adapter."""
        adapter = acct._adapter
        access_token = str(getattr(adapter, "access_token", "") or "").strip()
        refresh_token = str(getattr(adapter, "refresh_token", "") or "").strip()
        if not access_token or not refresh_token:
            return False
        if access_token == acct.token and refresh_token == acct.refresh_token:
            return False
        self.update_keepalive_credentials(
            acct.id,
            token=access_token,
            refresh_token=refresh_token,
        )
        return True

    def find_by_token(
        self,
        token: str,
        provider: str | None = None,
    ) -> Optional[Account]:
        """Find an account by its bare token (used by credential re-import)."""
        value = (token or "").strip()
        if not value:
            return None
        wanted = normalize_provider(provider) if provider else None
        with self._lock:
            return next(
                (
                    account
                    for account in self._accounts
                    if account.token == value
                    and (wanted is None or account.provider == wanted)
                ),
                None,
            )

    def update(self, account_id: str, token: str | None = None,
               cookies: str | None = None, email: str | None = None,
               proxy_id: str | None = None,
               device_id: str | None = None,
               provider: str | None = None,
               user_id: str | None = None,
               refresh_token: str | None = None,
               xsrf_token: str | None = None,
               session_id: str | None = None,
               topic_id: str | None = None) -> Account:
        with self._lock:
            acct = next((a for a in self._accounts if a.id == account_id), None)
            if acct is None:
                raise KeyError("Account not found")
            new_token = acct.token if token is None or token == "" else token.strip()
            new_cookies = acct.cookies if cookies is None or cookies == "" else cookies.strip()
            new_email = acct.email if email is None else email.strip()
            new_device_id = (
                acct.device_id if device_id is None else device_id.strip()
            )
            new_provider = (
                acct.provider
                if provider is None
                else normalize_provider(provider)
            )
            new_user_id = (
                acct.user_id if user_id is None else user_id.strip()
            )
            new_refresh_token = (
                acct.refresh_token
                if refresh_token is None
                else refresh_token.strip()
            )
            new_xsrf_token = (
                acct.xsrf_token if xsrf_token is None else xsrf_token.strip()
            )
            new_session_id = (
                acct.session_id if session_id is None else session_id.strip()
            )
            new_topic_id = (
                acct.topic_id if topic_id is None else topic_id.strip()
            )
            if new_provider == PROVIDER_KIMI and not new_refresh_token:
                raise ValueError("Kimi 账号需要 refresh_token")
            if new_provider == PROVIDER_ZAI and not new_user_id:
                new_user_id = zai_user_id_from_token(new_token)
            if new_provider == PROVIDER_KIMI:
                new_user_id = new_user_id or kimi_user_id_from_token(new_token)
                new_device_id = (
                    new_device_id or kimi_device_id_from_token(new_token)
                )
            if new_provider == PROVIDER_HUNYUAN:
                new_token = (
                    new_token or hunyuan_token_from_cookies(new_cookies)
                )
                new_user_id = (
                    new_user_id or hunyuan_user_id_from_cookies(new_cookies)
                )
            if new_provider == PROVIDER_WENXIN and not new_user_id:
                new_user_id = wenxin_baiduid(new_cookies)
            if new_provider == PROVIDER_DOUBAO:
                new_user_id = new_user_id or (
                    doubao_cookie_value(new_cookies, "web_id")
                    or doubao_cookie_value(new_cookies, "ttwid")
                )
                new_device_id = new_device_id or doubao_cookie_value(
                    new_cookies, "device_id"
                )
            new_proxy_id = (
                acct.proxy_id
                if proxy_id is None
                else self._validate_proxy_binding_locked(proxy_id, ignore_account_id=account_id)
            )
            if not new_token and new_provider not in {
                PROVIDER_QWEN,
                PROVIDER_HUNYUAN,
                PROVIDER_WENXIN,
                PROVIDER_DOUBAO,
            }:
                raise ValueError("Token cannot be empty")
            if new_provider == PROVIDER_ZAI and not new_user_id:
                raise ValueError("z.ai 账号需要 User ID")
            if new_provider == PROVIDER_HUNYUAN and (
                not new_cookies or not new_token or not new_user_id
            ):
                raise ValueError(
                    "Hunyuan 账号需要包含 hunyuan_token 和 hunyuan_user 的 cookies"
                )
            if new_provider == PROVIDER_WENXIN and (
                not new_cookies
                or not wenxin_cookie_value(new_cookies, "BDUSS")
            ):
                raise ValueError("Wenxin 账号需要包含 BDUSS 的登录 Cookie")
            if new_provider == PROVIDER_DOUBAO and (
                not new_cookies
                or not doubao_cookie_value(new_cookies, "sessionid")
            ):
                raise ValueError("Doubao 账号需要包含 sessionid 的登录 Cookie")

            new_fp = _credential_fingerprint(
                new_token,
                new_cookies,
                new_provider,
                new_user_id,
                new_refresh_token,
            )
            if any(a.id != account_id and a.fingerprint == new_fp for a in self._accounts):
                raise ValueError("Account already exists")

            credentials_changed = (
                new_token != acct.token
                or new_cookies != acct.cookies
                or new_device_id != acct.device_id
                or new_provider != acct.provider
                or new_user_id != acct.user_id
                or new_refresh_token != acct.refresh_token
                or new_xsrf_token != acct.xsrf_token
                or new_session_id != acct.session_id
                or new_topic_id != acct.topic_id
            )
            proxy_changed = new_proxy_id != acct.proxy_id
            acct.token = new_token
            acct.cookies = new_cookies
            acct.email = new_email or acct.email
            acct.device_id = new_device_id
            acct.provider = new_provider
            acct.user_id = new_user_id
            acct.refresh_token = new_refresh_token
            acct.xsrf_token = new_xsrf_token
            acct.session_id = new_session_id
            acct.topic_id = new_topic_id
            acct.proxy_id = new_proxy_id
            acct.updated_at = _now()
            if credentials_changed or proxy_changed:
                acct._adapter = None
            if credentials_changed:
                acct.state = "idle"
                acct.error_count = 0
                acct.last_error = ""
            self._save_persisted_accounts_locked()
            return acct

    def _validate_proxy_binding_locked(
        self,
        proxy_id: str,
        ignore_account_id: str | None = None,
    ) -> str:
        value = (proxy_id or "").strip()
        if not value:
            return ""
        entry = get_proxy_pool().get_by_id(value)
        if entry is None:
            raise ValueError("绑定的代理不存在")
        if not entry.enabled:
            raise ValueError("绑定的代理已停用")
        if any(a.id != ignore_account_id and a.proxy_id == value for a in self._accounts):
            raise ValueError("该代理已绑定其他账号")
        return value

    def invalidate_proxy(self, proxy_id: str) -> int:
        """Reset cached adapters after a bound proxy changes."""
        changed = 0
        with self._lock:
            for account in self._accounts:
                if account.proxy_id == proxy_id:
                    account._adapter = None
                    changed += 1
        return changed

    def proxy_is_bound(self, proxy_id: str) -> bool:
        with self._lock:
            return any(account.proxy_id == proxy_id for account in self._accounts)

    def remove_by_id(self, account_id: str) -> bool:
        with self._lock:
            for idx, acct in enumerate(self._accounts):
                if acct.id != account_id:
                    continue
                self._accounts.pop(idx)
                if self._next_idx >= len(self._accounts):
                    self._next_idx = 0
                self._save_persisted_accounts_locked()
                return True
            return False

    # Backward-compatible index removal for callers that have not migrated.
    def remove(self, index: int) -> bool:
        with self._lock:
            if not (0 <= index < len(self._accounts)):
                return False
            account_id = self._accounts[index].id
        return self.remove_by_id(account_id)

    def get_all(self, provider: str | None = None) -> list[dict]:
        wanted = normalize_provider(provider) if provider else None
        with self._lock:
            return [
                account.to_dict()
                for account in self._accounts
                if wanted is None or account.provider == wanted
            ]

    def count(self, provider: str | None = None) -> int:
        wanted = normalize_provider(provider) if provider else None
        with self._lock:
            return sum(
                1
                for account in self._accounts
                if wanted is None or account.provider == wanted
            )

    # ── Selection ──────────────────────────────────────────────

    def acquire(self, provider: str | None = None) -> Optional[Account]:
        """Get the next idle account (round-robin), or None if all busy.

        Returns None when no configured account is idle.
        """
        wanted = normalize_provider(provider) if provider else None
        with self._lock:
            if not self._accounts:
                return None
            n = len(self._accounts)
            for _ in range(n):
                idx = self._next_idx % n
                self._next_idx += 1
                acct = self._accounts[idx]
                if acct.state == "idle" and (
                    wanted is None or acct.provider == wanted
                ):
                    acct.state = "busy"
                    acct.last_used = time.time()
                    return acct
            return None

    def acquire_by_id(
        self,
        account_id: str,
        provider: str | None = None,
    ) -> Optional[Account]:
        """Acquire one specific idle account.

        Used for cached upstream conversations: a DeepSeek chat session is
        owned by the credentials that created it, so round-robin selection is
        unsafe when continuing that session.
        """
        if not account_id:
            return None
        wanted = normalize_provider(provider) if provider else None
        with self._lock:
            candidates = self._accounts
            for acct in candidates:
                if (
                    acct.id == account_id
                    and acct.state == "idle"
                    and (wanted is None or acct.provider == wanted)
                ):
                    acct.state = "busy"
                    acct.last_used = time.time()
                    return acct
            return None

    def release(self, acct: Account):
        """Mark account back to idle unless it was already marked as error."""
        with self._lock:
            if acct.state == "busy":
                acct.state = "idle"

    def mark_error(self, acct: Account, error_msg: str = ""):
        """Mark an account as `error`. If it has failed too many times, schedule
        a background health check to see if the credentials have come back
        (e.g. WAF challenge window expired, IP block lifted). The threshold
        is intentionally low so that transient blips heal automatically.
        """
        with self._lock:
            acct.state = "error"
            acct.error_count += 1
            acct.last_error = error_msg
            should_recover = acct.error_count >= _RECOVER_ERROR_THRESHOLD

        if should_recover and not _RECOVER_FLAG_DISABLED:
            # Run the health check on a background thread so the request
            # thread isn't blocked. We don't wait for the result; acquire()
            # will see the new state next time.
            t = threading.Thread(
                target=self._background_recover,
                args=(acct,),
                daemon=True,
            )
            t.start()

    def mark_keepalive_invalid(self, acct: Account, error_msg: str = "") -> None:
        """Disable an account without scheduling an upstream recovery probe."""
        with self._lock:
            acct.state = "error"
            acct.error_count += 1
            acct.last_error = error_msg

    def _background_recover(self, acct: Account) -> None:
        try:
            ok = self.check_health(acct)
            log.info(
                "background_recover_finished",
                extra={"id": acct.id, "ok": ok, "error": acct.last_error},
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "background_recover_crashed",
                extra={"id": acct.id, "error": str(e)},
            )

    def mark_recovered(self, acct: Account) -> None:
        """Public hook: reset error counters and return to idle."""
        with self._lock:
            acct.state = "idle"
            acct.error_count = 0
            acct.last_error = ""
            acct._adapter = None

    # ── Health check / relogin ─────────────────────────────────

    def check_health(self, acct: Account) -> bool:
        """Test whether the account credentials still work."""
        try:
            adapter = acct.adapter
            if hasattr(adapter, "check_health"):
                adapter.check_health()
            else:
                adapter.create_session()
            self.persist_adapter_tokens(acct)
            return True
        except Exception as e:
            with self._lock:
                acct.state = "error"
                acct.error_count += 1
                acct.last_error = str(e)
            return False

    def relogin_by_id(self, account_id: str) -> tuple[bool, str]:
        """Attempt to heal an error account by testing credentials."""
        with self._lock:
            acct = next((a for a in self._accounts if a.id == account_id), None)
            if acct is None:
                return False, "Account not found"
            if acct.state != "error":
                return False, f"Account is {acct.state}, not error"

        ok = self.check_health(acct)
        if ok:
            self.mark_recovered(acct)
            return True, "ok"
        return False, acct.last_error or "unknown error"

    def relogin(self, index: int) -> tuple[bool, str]:
        with self._lock:
            if not (0 <= index < len(self._accounts)):
                return False, "Account not found"
            account_id = self._accounts[index].id
        return self.relogin_by_id(account_id)

    # ── Stats ──────────────────────────────────────────────────

    def stats(self) -> dict:
        with self._lock:
            accounts = self._accounts
            total = len(accounts)
            idle = sum(1 for a in accounts if a.state == "idle")
            busy = sum(1 for a in accounts if a.state == "busy")
            error = sum(1 for a in accounts if a.state == "error")
            return {
                "total": total,
                "idle": idle,
                "busy": busy,
                "error": error,
            }
