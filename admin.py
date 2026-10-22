"""
Admin API — authentication, statistics tracking, account pool management.
"""
import asyncio
import hmac
import os
import secrets
import threading
import time
from collections import deque

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from account_pool import AccountPool
from admin_password import get_admin_password_store
import browser_login_client
from credential_import import (
    CredentialImportError,
    normalize_token,
    parse_credential_payload,
)
from crypto import is_enabled as crypto_is_enabled, _resolve_key
from ip_utils import get_real_client_ip, is_trusted_proxy
from logger import get_logger
from stats_history import get_history, start_sampler
from api_key_store import get_api_key_store
from runtime_config import get_runtime_config
from proxy_pool import get_proxy_pool
from provider_registry import (
    PROVIDER_DEEPSEEK,
    PROVIDER_MIMO,
    normalize_provider,
    doubao_model_name,
    doubao_upstream_model_name,
    hunyuan_model_name,
    hunyuan_upstream_model_name,
    kimi_model_name,
    kimi_upstream_model_name,
    available_mimo_models,
    mimo_model_name,
    mimo_upstream_model_name,
    wenxin_model_name,
    wenxin_upstream_model_name,
    zai_upstream_model_name,
)
from keepalive_scheduler import get_scheduler_or_store, notify_account_changed
from mimo_adapter import mimo_user_id_from_cookies

log = get_logger("admin")

# ── Token management ──────────────────────────────────────────
_tokens: set[str] = set()


def _generate_token() -> str:
    return secrets.token_hex(32)


def is_admin_password_weak() -> bool:
    """True if the effective admin password is a known weak default.

    The caller decides what to do with this signal (warn loudly, refuse to
    start on a public bind, etc.). We never refuse on a loopback bind.
    """
    return get_admin_password_store().is_weak()


def _verify_token(token: str) -> bool:
    if not token:
        return False
    # Constant-time membership: compare against every stored token. The set is
    # small (one entry per active admin session) so the cost is negligible.
    snapshot = tuple(_tokens)
    matched = False
    for stored in snapshot:
        if secrets.compare_digest(token, stored):
            matched = True
    return matched


def verify_admin_token(token: str | None) -> bool:
    """Public check: is this a valid admin-session token?

    Used by server.py so authenticated webui sessions can also call the
    /v1/* endpoints (model list, chat) with the same bearer token.
    """
    return _verify_token(token or "")


# ── Login throttling ──────────────────────────────────────────
# Per-IP sliding window: at most _LOGIN_MAX failures per _LOGIN_WINDOW seconds.
_LOGIN_MAX = 5
_LOGIN_WINDOW = 300
_login_attempts: dict[str, list[float]] = {}
_login_lock = threading.Lock()


def _client_ip(request: Request) -> str:
    """Resolve the *real* client IP. Trusts X-Forwarded-For only when the
    immediate connection peer is in the TRUSTED_PROXIES allowlist.
    """
    peer = request.client.host if request.client else None
    return get_real_client_ip(request.headers, peer)


def _login_record_failure(ip: str) -> None:
    now = time.time()
    with _login_lock:
        attempts = _login_attempts.setdefault(ip, [])
        cutoff = now - _LOGIN_WINDOW
        attempts[:] = [t for t in attempts if t > cutoff]
        attempts.append(now)


def _login_check_over_limit(ip: str) -> None:
    """Raise 429 if this IP has too many recent failures.
    Call AFTER recording the failure so the current attempt is counted."""
    now = time.time()
    with _login_lock:
        attempts = _login_attempts.get(ip, [])
        cutoff = now - _LOGIN_WINDOW
        recent = [t for t in attempts if t > cutoff]
        if len(recent) >= _LOGIN_MAX:
            raise HTTPException(
                status_code=429,
                detail="Too many failed login attempts; try again later",
            )


def _login_clear(ip: str) -> None:
    with _login_lock:
        _login_attempts.pop(ip, None)


# ── Stats ─────────────────────────────────────────────────────
_LATENCY_WINDOW_SIZE = 1024


def _percentile(values: list[float], pct: float) -> int:
    """Return the pct-th percentile of a sorted list of numbers (0..100)."""
    if not values:
        return 0
    sorted_v = sorted(values)
    k = max(0, min(len(sorted_v) - 1, int(round((pct / 100.0) * (len(sorted_v) - 1)))))
    return int(sorted_v[k])


class StatsSnapshot:
    def __init__(self):
        self.reset()

    def reset(self):
        self.total_requests = 0
        self.success_requests = 0
        self.failed_requests = 0
        self.total_latency_ms = 0.0
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.start_time = time.time()
        self.models: dict[str, dict] = {}
        # Bounded ring buffer of recent latencies (ms) for percentile
        # computation. ``deque(maxlen=N)`` is O(1) on push and silently
        # drops the oldest entry when full.
        self._recent_latencies: deque[float] = deque(maxlen=_LATENCY_WINDOW_SIZE)

    def record(self, model: str, latency_ms: float,
               prompt_tokens: int = 0, completion_tokens: int = 0,
               success: bool = True):
        self.total_requests += 1
        if success:
            self.success_requests += 1
            if latency_ms > 0:
                self._recent_latencies.append(float(latency_ms))
        else:
            self.failed_requests += 1
        self.total_latency_ms += latency_ms
        self.total_prompt_tokens += prompt_tokens
        self.total_completion_tokens += completion_tokens
        if model not in self.models:
            self.models[model] = {"requests": 0, "prompt_tokens": 0,
                                  "completion_tokens": 0, "errors": 0}
        self.models[model]["requests"] += 1
        self.models[model]["prompt_tokens"] += prompt_tokens
        self.models[model]["completion_tokens"] += completion_tokens
        if not success:
            self.models[model]["errors"] += 1

    def percentile(self, pct: float) -> int:
        return _percentile(list(self._recent_latencies), pct)

    def success_rate(self) -> float:
        if self.total_requests == 0:
            return 0.0
        return self.success_requests / self.total_requests


_stats = StatsSnapshot()
_pool = AccountPool()


def get_pool() -> AccountPool:
    return _pool


def get_stats() -> StatsSnapshot:
    return _stats


# ── Pydantic models ───────────────────────────────────────────

class LoginRequest(BaseModel):
    password: str


class LoginResponse(BaseModel):
    token: str


class AdminPasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str


class AccountAddRequest(BaseModel):
    token: str = ""
    cookies: str
    email: str | None = ""
    proxy_id: str | None = ""
    device_id: str | None = ""
    provider: str = PROVIDER_DEEPSEEK
    user_id: str | None = ""
    refresh_token: str | None = ""
    xsrf_token: str | None = ""
    session_id: str | None = ""
    topic_id: str | None = ""


class AccountUpdateRequest(BaseModel):
    token: str | None = None
    cookies: str | None = None
    email: str | None = None
    proxy_id: str | None = None
    device_id: str | None = None
    provider: str | None = None
    user_id: str | None = None
    refresh_token: str | None = None
    xsrf_token: str | None = None
    session_id: str | None = None
    topic_id: str | None = None


class KeepaliveAccountUpdateRequest(BaseModel):
    cookies: str | None = None
    device_id: str | None = None


class KeepaliveJobUpdateRequest(BaseModel):
    enabled: bool | None = None
    interval_secs: int | None = None


class KeepaliveBulkRequest(BaseModel):
    action: str
    account_ids: list[str] | None = None
    provider: str | None = None
    interval_secs: int | None = None


class CredentialImportRequest(BaseModel):
    payload: str | dict | list
    provider: str | None = None
    user_id: str | None = ""
    refresh_token: str | None = ""
    xsrf_token: str | None = ""
    session_id: str | None = ""
    topic_id: str | None = ""


class BrowserLoginStartRequest(BaseModel):
    reset_profile: bool = True


class BrowserLoginIngestRequest(BaseModel):
    token: str
    cookies: str = ""
    device_id: str = ""


class AccountReloginResponse(BaseModel):
    ok: bool
    message: str


class ApiKeyCreateRequest(BaseModel):
    name: str


class RuntimeConfigUpdateRequest(BaseModel):
    model_name: str | None = None
    display_name: str | None = None
    pricing: dict | None = None


class ProxyAddRequest(BaseModel):
    label: str | None = ""
    url: str
    enabled: bool = True


class ProxyBulkImportRequest(BaseModel):
    payload: str
    enabled: bool = True


class ProxyUpdateRequest(BaseModel):
    label: str | None = None
    url: str | None = None
    enabled: bool | None = None


# ── Router ────────────────────────────────────────────────────

router = APIRouter(prefix="/admin/api")


def _check_keepalive_auth(request: Request) -> None:
    """Accept the narrow keepalive secret, or a normal admin session."""
    configured = os.environ.get("KEEPALIVE_SHARED_SECRET", "").strip()
    supplied = request.headers.get("X-Keepalive-Secret", "").strip()
    if configured and supplied and hmac.compare_digest(configured, supplied):
        return
    _check_auth(request)


def _check_auth(request: Request):
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    if not _verify_token(token):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _pool_error(e: Exception):
    if isinstance(e, KeyError):
        raise HTTPException(status_code=404, detail=str(e).strip("'"))
    if isinstance(e, PermissionError):
        raise HTTPException(status_code=400, detail=str(e))
    if isinstance(e, RuntimeError):
        raise HTTPException(status_code=409, detail=str(e))
    if isinstance(e, ValueError):
        raise HTTPException(status_code=400, detail=str(e))
    raise e


def _proxy_account_map() -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for account in get_pool().get_all():
        proxy_id = str(account.get("proxy_id") or "")
        if proxy_id:
            result.setdefault(proxy_id, []).append(
                str(account.get("email") or account.get("id") or "")
            )
    return result


def _proxy_error(e: Exception):
    if isinstance(e, KeyError):
        raise HTTPException(status_code=404, detail=str(e).strip("'"))
    if isinstance(e, ValueError):
        raise HTTPException(status_code=400, detail=str(e))
    if isinstance(e, OSError):
        raise HTTPException(status_code=500, detail=f"无法保存代理池：{e}")
    raise e


@router.post("/login")
async def login(req: LoginRequest, request: Request):
    ip = _client_ip(request)
    # Constant-time compare so a wrong password and a right one take the
    # same time. Throttle is checked AFTER the compare so a legitimate
    # password resets the counter even if the IP was previously cooling
    # down — typical SSH/GitHub semantics, prevents lockout-of-self.
    correct = await asyncio.to_thread(
        get_admin_password_store().verify,
        req.password or "",
    )
    if correct:
        _login_clear(ip)
        token = _generate_token()
        _tokens.add(token)
        log.info("admin_login_success", extra={"ip": ip})
        return {"token": token}
    _login_record_failure(ip)
    log.warning("admin_login_failed", extra={"ip": ip})
    _login_check_over_limit(ip)
    raise HTTPException(status_code=403, detail="Invalid password")


@router.get("/admin-password")
async def admin_password_status(request: Request, response: Response):
    _check_auth(request)
    response.headers["Cache-Control"] = "no-store"
    return get_admin_password_store().snapshot()


@router.put("/admin-password")
async def change_admin_password(
    req: AdminPasswordChangeRequest,
    request: Request,
    response: Response,
):
    """Change the WebUI password and revoke every existing admin session.

    The current password is required even though the caller already holds a
    valid session token. Revoking every token prevents a stolen session from
    surviving a password rotation.
    """
    _check_auth(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        await asyncio.to_thread(
            get_admin_password_store().change,
            req.current_password,
            req.new_password,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"无法保存管理员密码：{exc}")

    _tokens.clear()
    with _login_lock:
        _login_attempts.clear()
    log.info(
        "admin_password_changed",
        extra={"ip": _client_ip(request)},
    )
    return {
        "ok": True,
        "relogin_required": True,
        **get_admin_password_store().snapshot(),
    }


@router.post("/logout")
async def logout(request: Request):
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    if token:
        _tokens.discard(token)
    return {"ok": True}


@router.get("/api-keys")
async def list_api_keys(request: Request):
    _check_auth(request)
    return {"keys": get_api_key_store().list_keys()}


@router.post("/api-keys")
async def create_api_key(req: ApiKeyCreateRequest, request: Request, response: Response):
    _check_auth(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        return get_api_key_store().create_key(req.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/api-keys/{key_id}")
async def revoke_api_key(key_id: str, request: Request):
    _check_auth(request)
    if not get_api_key_store().revoke_key(key_id):
        raise HTTPException(status_code=404, detail="API key not found")
    return {"ok": True}


@router.get("/runtime-config")
async def runtime_config(request: Request, response: Response):
    _check_auth(request)
    response.headers["Cache-Control"] = "no-store"
    return {
        **get_runtime_config().snapshot(),
        "model_routes_configured": bool(os.environ.get("MODEL_ROUTES", "").strip()),
    }


@router.put("/runtime-config")
async def update_runtime_config(
    req: RuntimeConfigUpdateRequest,
    request: Request,
    response: Response,
):
    _check_auth(request)
    response.headers["Cache-Control"] = "no-store"
    if req.model_name is None and req.display_name is None and req.pricing is None:
        raise HTTPException(status_code=400, detail="No runtime configuration fields supplied")
    try:
        get_runtime_config().update_settings(
            model_name=req.model_name,
            display_name=req.display_name,
            pricing=req.pricing,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"无法保存运行时配置：{e}")
    log.info(
        "runtime_model_config_updated",
        extra={
            "model_name": get_runtime_config().snapshot()["model_name"],
            "display_name": get_runtime_config().snapshot()["display_name"],
        },
    )
    return {
        **get_runtime_config().snapshot(),
        "model_routes_configured": bool(os.environ.get("MODEL_ROUTES", "").strip()),
    }


@router.get("/proxies")
async def list_proxies(request: Request):
    _check_auth(request)
    bound = _proxy_account_map()
    entries = [
        entry.public_dict(bound.get(entry.id, []))
        for entry in get_proxy_pool().list_entries()
    ]
    return {
        "proxies": entries,
        "total": len(entries),
        "available": sum(1 for entry in entries if entry["enabled"]),
    }


@router.post("/proxies")
async def add_proxy(req: ProxyAddRequest, request: Request):
    _check_auth(request)
    try:
        entry = get_proxy_pool().add(
            label=req.label or "",
            url=req.url,
            enabled=req.enabled,
        )
    except Exception as e:
        _proxy_error(e)
    return {"ok": True, "proxy": entry.public_dict()}


@router.post("/proxies/import")
async def import_proxies(req: ProxyBulkImportRequest, request: Request):
    _check_auth(request)
    try:
        result = get_proxy_pool().add_many(req.payload, enabled=req.enabled)
    except Exception as e:
        _proxy_error(e)
    return {
        "ok": True,
        "added_count": result["added_count"],
        "skipped_count": result["skipped_count"],
        "error_count": result["error_count"],
        "errors": result["errors"],
        "proxies": [entry.public_dict() for entry in result["added"]],
    }


@router.put("/proxies/{proxy_id}")
async def update_proxy(proxy_id: str, req: ProxyUpdateRequest, request: Request):
    _check_auth(request)
    try:
        entry = get_proxy_pool().update(
            proxy_id,
            label=req.label,
            url=req.url,
            enabled=req.enabled,
        )
    except Exception as e:
        _proxy_error(e)
    get_pool().invalidate_proxy(proxy_id)
    bound = _proxy_account_map().get(proxy_id, [])
    return {"ok": True, "proxy": entry.public_dict(bound)}


@router.delete("/proxies/{proxy_id}")
async def remove_proxy(proxy_id: str, request: Request):
    _check_auth(request)
    if get_pool().proxy_is_bound(proxy_id):
        raise HTTPException(status_code=409, detail="代理仍绑定账号，请先解除绑定")
    try:
        removed = get_proxy_pool().remove(proxy_id)
    except Exception as e:
        _proxy_error(e)
    if not removed:
        raise HTTPException(status_code=404, detail="Proxy not found")
    return {"ok": True}


@router.post("/proxies/{proxy_id}/test")
def test_proxy(proxy_id: str, request: Request):
    _check_auth(request)
    try:
        result = get_proxy_pool().test(proxy_id)
        entry = get_proxy_pool().get_by_id(proxy_id)
    except Exception as e:
        _proxy_error(e)
    bound = _proxy_account_map().get(proxy_id, [])
    return {
        **result,
        "proxy": entry.public_dict(bound) if entry else None,
    }


@router.get("/stats")
async def stats(request: Request):
    _check_auth(request)
    s = _stats
    uptime = int(time.time() - s.start_time)
    avg_latency = int(s.total_latency_ms / s.total_requests) if s.total_requests > 0 else 0
    return {
        "total_requests": s.total_requests,
        "success_requests": s.success_requests,
        "failed_requests": s.failed_requests,
        "success_rate": round(s.success_rate(), 4),
        "avg_latency_ms": avg_latency,
        "p50_latency_ms": s.percentile(50),
        "p95_latency_ms": s.percentile(95),
        "p99_latency_ms": s.percentile(99),
        "latency_window_size": len(s._recent_latencies),
        "total_prompt_tokens": s.total_prompt_tokens,
        "total_completion_tokens": s.total_completion_tokens,
        "uptime_secs": uptime,
        "models": s.models,
    }


@router.get("/history")
async def history(request: Request):
    """Return the rolling time-series of StatsSnapshot, for the dashboard charts."""
    _check_auth(request)
    h = get_history()
    return {
        "interval_secs": h.interval(),
        "points": h.points(),
    }


@router.get("/env")
async def env_info(request: Request):
    """Return a read-only view of the effective runtime configuration.

    Powers the React "Settings" page. Intentionally hides secret values
    (only "set / default" indicators for credentials) so a logged-in
    admin cannot expose secret values through this endpoint.
    """
    _check_auth(request)
    pool = _pool

    # Enumerate the env vars we know about. ``is_default`` is True when
    # the value matches the module-level default; "set" otherwise.
    def _env(name: str) -> str:
        return os.environ.get(name, "")

    def _flag(name: str) -> str:
        return "set" if os.environ.get(name) else "default"

    password_state = get_admin_password_store().snapshot()
    crypto_on = crypto_is_enabled()
    runtime = get_runtime_config()
    runtime_snapshot = runtime.snapshot()
    origins = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
    trusted = [p.strip() for p in os.environ.get("TRUSTED_PROXIES", "").split(",") if p.strip()]

    return {
        "host": os.environ.get("HOST", "127.0.0.1"),
        "port": int(os.environ.get("PORT", "8080")),
        "insecure_public_defaults": os.environ.get("ALLOW_INSECURE_PUBLIC_DEFAULTS", "false").lower() in {"1", "true", "yes", "on"},
        "admin_password_set": password_state["set"],
        "admin_password_weak": password_state["weak"],
        "admin_password_source": password_state["source"],
        "admin_password_updated_at": password_state["updated_at"],
        "accounts_total": pool.count(),
        "crypto": {
            "enabled": crypto_on,
            "fernet_configured": bool(_resolve_key()),
        },
        "cors": {
            "origins": origins,
            "allow_credentials": os.environ.get("ALLOW_CORS_CREDENTIALS", "false").lower() in {"1", "true", "yes", "on"},
        },
        "trusted_proxies": trusted,
        "proxies_total": len(get_proxy_pool().list_entries()),
        "model_name": runtime_snapshot["model_name"],
        "zai_model_name": os.environ.get("ZAI_MODEL_NAME", "glm-5.3-flash"),
        "zai_upstream_model": zai_upstream_model_name(),
        "kimi_model_name": kimi_model_name(),
        "kimi_upstream_model": kimi_upstream_model_name(),
        "hunyuan_model_name": hunyuan_model_name(),
        "hunyuan_upstream_model": hunyuan_upstream_model_name(),
        "wenxin_model_name": wenxin_model_name(),
        "wenxin_upstream_model": wenxin_upstream_model_name(),
        "doubao_model_name": doubao_model_name(),
        "doubao_upstream_model": doubao_upstream_model_name(),
        "mimo_model_name": mimo_model_name(),
        "mimo_upstream_model": mimo_upstream_model_name(),
        "mimo_models": available_mimo_models(),
        "model_name_source": runtime_snapshot["source"],
        "model_routes_configured": bool(os.environ.get("MODEL_ROUTES", "").strip()),
        "rate_limit": {
            "enabled": os.environ.get("ENABLE_RATE_LIMIT", "true").lower() in {"1", "true", "yes", "on"},
            "per_key": int(os.environ.get("CLIENT_RPM_PER_KEY", "60") or 60),
            "per_ip": int(os.environ.get("CLIENT_RPM_PER_IP", "120") or 120),
        },
        "session_cache_ttl": int(os.environ.get("SESSION_CACHE_TTL", "1800") or 1800),
        "log_level": os.environ.get("LOG_LEVEL", "INFO").upper(),
        "log_format": os.environ.get("LOG_FORMAT", "json").lower(),
        "dsml_max_buffer_bytes": int(os.environ.get("DSML_MAX_BUFFER_BYTES", "1048576") or 1048576),
        "uptime_secs": int(time.time() - _stats.start_time),
        "server_version": "3.3.8",
        "env_overrides": [
            {"name": "HOST", "value": _env("HOST") or "127.0.0.1", "is_default": _flag("HOST") == "default"},
            {"name": "PORT", "value": _env("PORT") or "8080", "is_default": _flag("PORT") == "default"},
            {"name": "ALLOW_UNAUTHENTICATED_API", "value": _env("ALLOW_UNAUTHENTICATED_API") or "false", "is_default": _flag("ALLOW_UNAUTHENTICATED_API") == "default"},
            {"name": "ALLOW_INSECURE_PUBLIC_DEFAULTS", "value": _env("ALLOW_INSECURE_PUBLIC_DEFAULTS") or "false", "is_default": _flag("ALLOW_INSECURE_PUBLIC_DEFAULTS") == "default"},
            {"name": "MODE", "value": _env("MODE") or "auto", "is_default": _flag("MODE") == "default"},
            {"name": "THINKING", "value": _env("THINKING") or "auto", "is_default": _flag("THINKING") == "default"},
            {"name": "SEARCH", "value": _env("SEARCH") or "auto", "is_default": _flag("SEARCH") == "default"},
            {
                "name": "MODEL_NAME",
                "value": runtime_snapshot["model_name"],
                "is_default": runtime_snapshot["source"] == "default",
                "source": runtime_snapshot["source"],
            },
            {"name": "MODEL_ROUTES", "value": _env("MODEL_ROUTES") or "", "is_default": _flag("MODEL_ROUTES") == "default"},
            {"name": "ZAI_MODEL_NAME", "value": _env("ZAI_MODEL_NAME") or "glm-5.3-flash", "is_default": _flag("ZAI_MODEL_NAME") == "default"},
            {"name": "ZAI_UPSTREAM_MODEL", "value": zai_upstream_model_name(), "is_default": _flag("ZAI_UPSTREAM_MODEL") == "default"},
            {"name": "KIMI_MODEL_NAME", "value": kimi_model_name(), "is_default": _flag("KIMI_MODEL_NAME") == "default"},
            {"name": "KIMI_UPSTREAM_MODEL", "value": kimi_upstream_model_name(), "is_default": _flag("KIMI_UPSTREAM_MODEL") == "default"},
            {"name": "HUNYUAN_MODEL_NAME", "value": hunyuan_model_name(), "is_default": _flag("HUNYUAN_MODEL_NAME") == "default"},
            {"name": "HUNYUAN_UPSTREAM_MODEL", "value": hunyuan_upstream_model_name(), "is_default": _flag("HUNYUAN_UPSTREAM_MODEL") == "default"},
            {"name": "WENXIN_MODEL_NAME", "value": wenxin_model_name(), "is_default": _flag("WENXIN_MODEL_NAME") == "default"},
            {"name": "WENXIN_UPSTREAM_MODEL", "value": wenxin_upstream_model_name(), "is_default": _flag("WENXIN_UPSTREAM_MODEL") == "default"},
            {"name": "DOUBAO_MODEL_NAME", "value": doubao_model_name(), "is_default": _flag("DOUBAO_MODEL_NAME") == "default"},
            {"name": "DOUBAO_UPSTREAM_MODEL", "value": doubao_upstream_model_name(), "is_default": _flag("DOUBAO_UPSTREAM_MODEL") == "default"},
            {"name": "MIMO_MODEL_NAME", "value": mimo_model_name(), "is_default": _flag("MIMO_MODEL_NAME") == "default"},
            {"name": "MIMO_UPSTREAM_MODEL", "value": mimo_upstream_model_name(), "is_default": _flag("MIMO_UPSTREAM_MODEL") == "default"},
            {
                "name": "DEEPSEEK_ADMIN_PASSWORD",
                "value": "***" if password_state["set"] else "",
                "is_default": password_state["source"] == "env"
                and _flag("DEEPSEEK_ADMIN_PASSWORD") == "default",
                "source": password_state["source"],
            },
            {
                "name": "ADMIN_PASSWORD_STORE_PATH",
                "value": _env("ADMIN_PASSWORD_STORE_PATH") or "data/admin_auth.json",
                "is_default": _flag("ADMIN_PASSWORD_STORE_PATH") == "default",
            },
            {"name": "DEEPSEEK_ENCRYPTION_KEY", "value": "***" if crypto_on else "", "is_default": _flag("DEEPSEEK_ENCRYPTION_KEY") == "default"},
            {"name": "PROXY_STORE_PATH", "value": _env("PROXY_STORE_PATH") or "data/proxies.json", "is_default": _flag("PROXY_STORE_PATH") == "default"},
            {"name": "PROXY_CHECK_URL", "value": _env("PROXY_CHECK_URL") or "https://chat.deepseek.com/", "is_default": _flag("PROXY_CHECK_URL") == "default"},
            {"name": "ALLOWED_ORIGINS", "value": _env("ALLOWED_ORIGINS") or "", "is_default": _flag("ALLOWED_ORIGINS") == "default"},
            {"name": "TRUSTED_PROXIES", "value": _env("TRUSTED_PROXIES") or "", "is_default": _flag("TRUSTED_PROXIES") == "default"},
            {"name": "ENABLE_RATE_LIMIT", "value": _env("ENABLE_RATE_LIMIT") or "true", "is_default": _flag("ENABLE_RATE_LIMIT") == "default"},
            {"name": "CLIENT_RPM_PER_KEY", "value": _env("CLIENT_RPM_PER_KEY") or "60", "is_default": _flag("CLIENT_RPM_PER_KEY") == "default"},
            {"name": "CLIENT_RPM_PER_IP", "value": _env("CLIENT_RPM_PER_IP") or "120", "is_default": _flag("CLIENT_RPM_PER_IP") == "default"},
            {"name": "SESSION_CACHE_TTL", "value": _env("SESSION_CACHE_TTL") or "600", "is_default": _flag("SESSION_CACHE_TTL") == "default"},
            {"name": "LOG_LEVEL", "value": _env("LOG_LEVEL") or "INFO", "is_default": _flag("LOG_LEVEL") == "default"},
            {"name": "LOG_FORMAT", "value": _env("LOG_FORMAT") or "json", "is_default": _flag("LOG_FORMAT") == "default"},
            {"name": "DSML_MAX_BUFFER_BYTES", "value": _env("DSML_MAX_BUFFER_BYTES") or "1048576", "is_default": _flag("DSML_MAX_BUFFER_BYTES") == "default"},
            {"name": "STATS_HISTORY_INTERVAL_SECS", "value": _env("STATS_HISTORY_INTERVAL_SECS") or "30", "is_default": _flag("STATS_HISTORY_INTERVAL_SECS") == "default"},
            {"name": "DEEPSEEK_IMPERSONATE", "value": _env("DEEPSEEK_IMPERSONATE") or "chrome131", "is_default": _flag("DEEPSEEK_IMPERSONATE") == "default"},
            {"name": "DEEPSEEK_JITTER_SECS", "value": _env("DEEPSEEK_JITTER_SECS") or "0.0", "is_default": _flag("DEEPSEEK_JITTER_SECS") == "default"},
        ],
    }


@router.get("/accounts")
async def list_accounts(request: Request, provider: str | None = None):
    _check_auth(request)
    pool = get_pool()
    try:
        selected = normalize_provider(provider) if provider else None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "accounts": pool.get_all(provider=selected),
        **pool.stats(),
        "provider": selected or "all",
    }


def _credential_payload(acct) -> dict:
    proxy_url = acct.proxy.strip()
    if not proxy_url and acct.proxy_id:
        proxy_url = get_proxy_pool().resolve_url(acct.proxy_id) or ""
    return {
        **acct.to_dict(),
        "token": acct.token,
        "cookies": acct.cookies,
        "device_id": acct.device_id,
        "user_id": acct.user_id,
        "refresh_token": acct.refresh_token,
        "xsrf_token": acct.xsrf_token,
        "session_id": acct.session_id,
        "topic_id": acct.topic_id,
        "proxy_url": proxy_url,
    }


def _keepalive_resources():
    scheduler, store = get_scheduler_or_store()
    store.sync_accounts(get_pool().keepalive_accounts())
    return scheduler, store


@router.get("/keepalive/jobs")
async def list_keepalive_jobs(request: Request):
    _check_auth(request)
    scheduler, store = _keepalive_resources()
    status = scheduler.status() if scheduler else {
        "enabled": False,
        "running": False,
        "worker_id": "",
        "tick_secs": 0,
        "global_concurrency": 0,
        **store.stats(),
    }
    return {"status": status, "jobs": store.list_jobs()}


@router.put("/keepalive/jobs/{account_id}")
async def update_keepalive_job(
    account_id: str,
    req: KeepaliveJobUpdateRequest,
    request: Request,
):
    _check_auth(request)
    _, store = _keepalive_resources()
    job = store.update_job(
        account_id,
        enabled=req.enabled,
        interval_secs=req.interval_secs,
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Keepalive job not found")
    return {"ok": True, "job": job}


@router.post("/keepalive/jobs/{account_id}/probe")
async def probe_keepalive_job(account_id: str, request: Request):
    _check_auth(request)
    _, store = _keepalive_resources()
    job = store.update_job(account_id, force=True)
    if job is None:
        raise HTTPException(status_code=404, detail="Keepalive job not found")
    return {"ok": True, "job": job}


@router.post("/keepalive/actions/bulk")
async def bulk_keepalive_action(req: KeepaliveBulkRequest, request: Request):
    _check_auth(request)
    if req.action not in {"enable", "disable", "probe", "clear"}:
        raise HTTPException(status_code=400, detail="Unsupported keepalive action")
    _, store = _keepalive_resources()
    updated = store.bulk_update(
        action=req.action,
        account_ids=req.account_ids,
        provider=req.provider,
        interval_secs=req.interval_secs,
    )
    return {"ok": True, "updated": updated}


@router.get("/keepalive/accounts/{account_id}")
async def get_keepalive_account(
    account_id: str,
    request: Request,
    response: Response,
):
    _check_keepalive_auth(request)
    response.headers["Cache-Control"] = "no-store"
    acct = get_pool().get_by_id(account_id)
    if acct is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return {"account": _credential_payload(acct)}


@router.put("/keepalive/accounts/{account_id}")
async def update_keepalive_account(
    account_id: str,
    req: KeepaliveAccountUpdateRequest,
    request: Request,
    response: Response,
):
    _check_keepalive_auth(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        acct = get_pool().update(
            account_id,
            cookies=req.cookies,
            device_id=req.device_id,
        )
    except Exception as e:
        _pool_error(e)
    return {"ok": True, "account": _credential_payload(acct)}


@router.get("/accounts/{account_id}")
async def get_account_credentials(account_id: str, request: Request, response: Response):
    """Return one account including raw token/cookies.

    The list endpoint only exposes previews, which is not enough for an
    automated session keeper that has to re-probe the upstream with the real
    credentials. Admin authentication is required, and the response must not
    be cached.
    """
    _check_auth(request)
    response.headers["Cache-Control"] = "no-store"
    acct = get_pool().get_by_id(account_id)
    if acct is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return {"account": _credential_payload(acct)}


@router.post("/accounts")
async def add_account(req: AccountAddRequest, request: Request):
    _check_auth(request)
    pool = get_pool()
    try:
        acct = pool.add(
            token=req.token,
            cookies=req.cookies,
            email=req.email or "",
            proxy_id=req.proxy_id or "",
            device_id=req.device_id or "",
            provider=req.provider,
            user_id=req.user_id or "",
            refresh_token=req.refresh_token or "",
            xsrf_token=req.xsrf_token or "",
            session_id=req.session_id or "",
            topic_id=req.topic_id or "",
        )
    except Exception as e:
        _pool_error(e)
    notify_account_changed(acct.id)
    return {"ok": True, "account": acct.to_dict()}


@router.post("/accounts/import")
async def import_account(req: CredentialImportRequest, request: Request, response: Response):
    """Import credentials copied from the DeepSeek browser session.

    Accepts a DevTools HAR or the small JSON object produced by the WebUI's
    "copy browser export script".  The response never echoes the token or
    cookie values.
    """
    _check_auth(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        imported = parse_credential_payload(req.payload)
    except CredentialImportError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        provider = normalize_provider(
            req.provider or imported.provider or PROVIDER_DEEPSEEK
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    user_id = (
        mimo_user_id_from_cookies(imported.cookies)
        if provider == PROVIDER_MIMO
        else (req.user_id or imported.user_id or "").strip()
    )
    refresh_token = (
        req.refresh_token or imported.refresh_token or ""
    ).strip()
    xsrf_token = (req.xsrf_token or imported.xsrf_token or "").strip()
    session_id = (req.session_id or "").strip()
    topic_id = (req.topic_id or "").strip()
    pool = get_pool()
    try:
        existing = (
            pool.find_by_user_id(user_id, provider=provider)
            if provider == PROVIDER_MIMO
            else pool.find_by_token(imported.token, provider=provider)
        )
        if existing is not None:
            acct = pool.update(
                existing.id,
                token=imported.token,
                cookies=imported.cookies or None,
                device_id=imported.device_id or None,
                provider=provider,
                user_id=user_id,
                refresh_token=refresh_token or None,
                xsrf_token=xsrf_token or None,
                session_id=session_id or None,
                topic_id=topic_id or None,
            )
            action = "updated"
        else:
            acct = pool.add(
                token=imported.token,
                cookies=imported.cookies,
                device_id=imported.device_id,
                provider=provider,
                user_id=user_id,
                refresh_token=refresh_token,
                xsrf_token=xsrf_token,
                session_id=session_id,
                topic_id=topic_id,
            )
            action = "created"
    except Exception as e:
        _pool_error(e)
    notify_account_changed(acct.id)

    log.info(
        "account_credentials_imported",
        extra={
            "account_id": acct.id,
            "action": action,
            "source": imported.source,
            "provider": provider,
            "has_device_id": bool(imported.device_id),
            "cookie_count": len(imported.public_summary()["cookie_names"]),
        },
    )
    return {
        "ok": True,
        "action": action,
        "account": acct.to_dict(),
        "import": imported.public_summary(),
    }


@router.get("/browser-login/status")
async def browser_login_status(request: Request):
    _check_auth(request)
    if not browser_login_client.enabled():
        return {"enabled": False, "status": "disabled", "message": "浏览器登录服务未配置"}
    try:
        return {"enabled": True, **(await browser_login_client.status())}
    except browser_login_client.BrowserLoginUnavailable as e:
        return {"enabled": True, "status": "unavailable", "message": str(e)}


@router.post("/browser-login/start")
async def browser_login_start(req: BrowserLoginStartRequest, request: Request, response: Response):
    _check_auth(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        result = await browser_login_client.start(reset_profile=req.reset_profile)
    except browser_login_client.BrowserLoginUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"enabled": True, **result}


@router.get("/browser-login/view-token")
async def browser_login_view_token(request: Request, response: Response):
    _check_auth(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        return {"enabled": True, **(await browser_login_client.view_token())}
    except browser_login_client.BrowserLoginUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/browser-login/stop")
async def browser_login_stop(request: Request):
    _check_auth(request)
    try:
        return {"enabled": True, **(await browser_login_client.stop())}
    except browser_login_client.BrowserLoginUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/browser-login/ingest")
async def browser_login_ingest(
    req: BrowserLoginIngestRequest,
    request: Request,
    response: Response,
):
    """Internal callback from the browser sidecar.

    Protected by a dedicated shared secret instead of the admin session token,
    so the sidecar never receives the WebUI administrator credential.
    """
    expected = os.environ.get("BROWSER_LOGIN_SHARED_SECRET", "")
    supplied = request.headers.get("X-Browser-Login-Secret", "")
    if not expected or not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")
    response.headers["Cache-Control"] = "no-store"

    token = req.token.strip()
    # Older browser-login builds posted the raw localStorage wrapper
    # (``{"value":"...","__version":"0"}``); normalize it before persisting.
    token = normalize_token(token)
    if not token:
        raise HTTPException(status_code=400, detail="Token is required")
    pool = get_pool()
    try:
        existing = pool.find_by_token(token)
        if existing is not None:
            acct = pool.update(
                existing.id,
                token=token,
                cookies=req.cookies or None,
                device_id=req.device_id or None,
            )
            action = "updated"
        else:
            acct = pool.add(
                token=token,
                cookies=req.cookies,
                device_id=req.device_id,
            )
            action = "created"
    except Exception as e:
        _pool_error(e)
    notify_account_changed(acct.id)

    log.info(
        "browser_login_credentials_ingested",
        extra={
            "account_id": acct.id,
            "action": action,
            "has_device_id": bool(req.device_id),
        },
    )
    return {"ok": True, "action": action, "account": acct.to_dict()}


@router.put("/accounts/{account_id}")
async def update_account(account_id: str, req: AccountUpdateRequest, request: Request):
    _check_auth(request)
    pool = get_pool()
    try:
        acct = pool.update(
            account_id,
            token=req.token,
            cookies=req.cookies,
            email=req.email,
            proxy_id=req.proxy_id,
            device_id=req.device_id,
            provider=req.provider,
            user_id=req.user_id,
            refresh_token=req.refresh_token,
            xsrf_token=req.xsrf_token,
            session_id=req.session_id,
            topic_id=req.topic_id,
        )
    except Exception as e:
        _pool_error(e)
    notify_account_changed(acct.id)
    return {"ok": True, "account": acct.to_dict()}


@router.delete("/accounts/{account_id}")
async def remove_account(account_id: str, request: Request):
    _check_auth(request)
    pool = get_pool()
    try:
        ok = pool.remove_by_id(account_id)
    except Exception as e:
        _pool_error(e)
    if not ok:
        raise HTTPException(status_code=404, detail="Account not found")
    notify_account_changed()
    return {"ok": True}


@router.post("/accounts/{account_id}/relogin")
async def relogin_account(account_id: str, request: Request) -> AccountReloginResponse:
    _check_auth(request)
    pool = get_pool()
    ok, msg = pool.relogin_by_id(account_id)
    return AccountReloginResponse(ok=ok, message=msg)
