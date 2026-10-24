"""
OpenAI-compatible API proxy for DeepSeek Chat
Supports streaming, tool calling (via DSML prompt injection), content parts, and
the thinking / search capabilities (DeepSeek merged its quick and expert modes,
so ``thinking_enabled`` is the only model-behaviour switch left).
"""
import asyncio
import json
import codecs
import atexit
import os
import sys
import threading
import time
import uuid
import hashlib
from typing import Optional, Union, Any

import uvicorn
from dotenv import load_dotenv
import os as _os
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from curl_cffi.requests.exceptions import RequestException
from streaming import HeartbeatStream, ReliableStreamingResponse, run_owned_setup

from adapter import (
    DeepSeekAdapter,
    UpstreamEmptyError,
    UpstreamContextLimitError,
    UpstreamHintError,
    UpstreamTransientError,
    RateLimitError,
    UserMutedError,
)
from admin import (
    router as admin_router,
    get_pool,
    get_stats,
    is_admin_password_weak,
    verify_admin_token,
)
import browser_login_client
from stats_history import start_sampler
from model_router import DEFAULT_MODEL_TYPE, canonical_model_name
from tool_dsml import (
    parse_dsml_tool_calls,
    format_tool_calls_for_prompt,
    build_dsml_tool_prompt,
)
from tool_sieve import IncompleteToolCallError, StreamSieve, SieveEvent
from anthropic_format import (
    AnthropicRequest,
    build_anthropic_prompt,
    build_nonstream_response,
    stream_response,
    _msg_id,
)
from responses_format import (
    ResponsesStreamTranslator,
    ResponsesToolAdapter,
    build_compaction_response as build_responses_compaction,
    build_nonstream_response as build_responses_nonstream,
    compaction_fallback_summary,
    compaction_prompt,
    has_compaction_trigger,
    input_to_chat_messages,
    parse_chat_sse_event,
    requested_search,
    requested_thinking,
)
from logger import get_logger, configure_from_env
from crypto import is_enabled as crypto_is_enabled
from token_counter import count_text
from rate_limiter import RateLimiter
from ip_utils import get_real_client_ip, is_trusted_proxy
from model_router import ModelRouter
from session_cache import SessionCache, ChatSession, delta_prompt
from api_key_store import get_api_key_store
from runtime_config import get_runtime_config
from provider_registry import (
    PROVIDER_DEEPSEEK,
    PROVIDER_DOUBAO,
    PROVIDER_HUNYUAN,
    PROVIDER_KIMI,
    PROVIDER_MIMO,
    PROVIDER_QWEN,
    PROVIDER_WENXIN,
    PROVIDER_ZAI,
    available_hunyuan_models,
    available_kimi_models,
    available_doubao_models,
    available_wenxin_models,
    available_mimo_models,
    mimo_model_name,
    mimo_public_model,
    mimo_upstream_model,
    hunyuan_public_model,
    hunyuan_upstream_model,
    kimi_model_name,
    kimi_public_model,
    kimi_upstream_model,
    qwen_model_name,
    qwen_public_model,
    qwen_upstream_model,
    doubao_public_model,
    doubao_upstream_model,
    wenxin_public_model,
    wenxin_upstream_model,
    normalize_provider,
    provider_for_model,
    provider_label,
    zai_public_model,
    zai_upstream_model,
    zai_model_name,
)
from keepalive_scheduler import start_scheduler
from zai_adapter import ZaiAuthError
from kimi_adapter import KimiAuthError
from qwen_adapter import QwenAuthError
from hunyuan_adapter import HunyuanAuthError
from wenxin_adapter import WenxinAuthError
from doubao_adapter import DoubaoAuthError
from mimo_adapter import (
    FASTCHAT_MODELS,
    ULTRASPEED_FALLBACK_MODEL,
    MimoAuthError,
)

load_dotenv()
configure_from_env()
log = get_logger("server")
API_KEY_STORE = get_api_key_store()
MODEL_CREATED_AT = int(os.environ.get("MODEL_CREATED_AT", str(int(time.time()))))

#: Seconds a parent gateway should wait before retrying after an upstream
#: transient outage ('服务器暂时不可用' / generation_err).
TRANSIENT_RETRY_AFTER_SECS = os.environ.get("DEEPSEEK_TRANSIENT_RETRY_AFTER", "5")
try:
    ACCOUNT_WAIT_SECS = float(os.environ.get("UPSTREAM_ACCOUNT_WAIT_TIMEOUT", "15"))
except ValueError:
    ACCOUNT_WAIT_SECS = 15.0
ACCOUNT_WAIT_SECS = min(120.0, max(0.0, ACCOUNT_WAIT_SECS))
MIMO_AUTH_MESSAGE = "MiMo 登录 Cookie 已失效，请重新导入"
MIMO_CODEX_CONTEXT_WINDOW = 32_768
MIMO_SAFE_PROMPT_TOKENS = 12_000
MIMO_COMPACTION_PROMPT_CHARS = 24_000
MIMO_COMPACTION_FALLBACK_CHARS = 8_000
CODEX_LOCAL_TOOL_GUARD = (
    "You are operating on the user's local Codex machine, not a cloud sandbox "
    "or download service. exec_command and apply_patch execute locally in the "
    "current workspace. For requests that create or edit files, you MUST call "
    "the appropriate tool before answering; do not paste the complete file in "
    "chat and do not ask the user to download or manually save it unless a real "
    "tool call failed. For large files, write them in multiple chunks of about "
    "8K characters or less so one tool-call argument never becomes excessively "
    "large."
)
PROVIDER_SAFE_PROMPT_TOKENS = {
    PROVIDER_MIMO: MIMO_SAFE_PROMPT_TOKENS,
    "wenxin": 32_000,
    "deepseek": 64_000,
    "zai": 64_000,
    "kimi": 64_000,
    "qianwen": 64_000,
    "hunyuan": 64_000,
    "doubao": 64_000,
}


def _model_name() -> str:
    """Return the current public model name (WebUI runtime config first)."""
    return get_runtime_config().model_name


def _public_model_for_request(model: str | None, provider: str) -> str:
    requested = str(model or "").strip()
    if provider == PROVIDER_ZAI:
        return zai_public_model(requested)
    if provider == PROVIDER_KIMI:
        return kimi_public_model(requested)
    if provider == PROVIDER_QWEN:
        return qwen_public_model(requested)
    if provider == PROVIDER_HUNYUAN:
        return hunyuan_public_model(requested)
    if provider == PROVIDER_WENXIN:
        return wenxin_public_model(requested)
    if provider == PROVIDER_DOUBAO:
        return doubao_public_model(requested)
    if provider == PROVIDER_MIMO:
        return mimo_public_model(requested)
    return canonical_model_name(requested) if requested else _model_name()


def _upstream_model_for_request(model: str | None, provider: str) -> str | None:
    if provider == PROVIDER_ZAI:
        return zai_upstream_model(model)
    if provider == PROVIDER_KIMI:
        return kimi_upstream_model(model)
    if provider == PROVIDER_QWEN:
        return qwen_upstream_model(model)
    if provider == PROVIDER_HUNYUAN:
        return hunyuan_upstream_model(model)
    if provider == PROVIDER_WENXIN:
        return wenxin_upstream_model(model)
    if provider == PROVIDER_DOUBAO:
        return doubao_upstream_model(model)
    if provider == PROVIDER_MIMO:
        return mimo_upstream_model(model)
    return str(model or "").strip() or None


#: Legacy mode knob.  DeepSeek merged quick/expert into a single model (the only
#: remaining difference is ``thinking_enabled``), so ``MODE`` no longer selects a
#: model: ``MODE=expert`` is honoured as "default thinking ON" for backward
#: compatibility with existing .env files, and ``MODE=quick`` as "thinking OFF".
#: Prefer THINKING=enabled/disabled, which says what it does.
MODE = os.environ.get("MODE", "auto").strip().lower()
THINKING = os.environ.get("THINKING", "auto").strip().lower()
SEARCH = os.environ.get("SEARCH", "auto").strip().lower()
HOST = os.environ.get("HOST", "127.0.0.1").strip()
PORT = int(os.environ.get("PORT", "8080"))
ALLOW_UNAUTHENTICATED_API = os.environ.get("ALLOW_UNAUTHENTICATED_API", "false").strip().lower() in {"1", "true", "yes", "on"}
ALLOW_INSECURE_PUBLIC_DEFAULTS = os.environ.get("ALLOW_INSECURE_PUBLIC_DEFAULTS", "false").strip().lower() in {"1", "true", "yes", "on"}

# CORS allow-list. Empty = same-origin only.
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
ALLOW_CREDENTIALS = bool(ALLOWED_ORIGINS) and os.environ.get("ALLOW_CORS_CREDENTIALS", "false").strip().lower() in {"1", "true", "yes", "on"}


RATE_LIMITER = RateLimiter()
MODEL_ROUTER = ModelRouter(models=[])
SESSION_CACHE = SessionCache()


def _legacy_mode_thinking() -> bool | None:
    """Map the deprecated ``MODE`` knob onto the thinking capability.

    Returns ``None`` when MODE carries no information (``auto`` or unset).
    """
    if MODE in {"expert", "thinking", "reasoner"}:
        return True
    if MODE in {"quick", "default"}:
        return False
    return None


def resolve_model_controls(decision, req_thinking: bool = False,
                           req_search: bool = False) -> tuple[str, bool, bool]:
    """Resolve ``(model_type, thinking_enabled, search_enabled)`` for a request.

    Precedence, highest first:

    1. ``MODEL_ROUTES`` entry matched by the client's ``model`` name
       (thinking / search presets).
    2. ``THINKING`` / ``SEARCH`` env vars when set to enabled/disabled.
    3. The client's own fields (``thinking_mode`` / ``search_enabled``,
       or Anthropic's ``thinking`` block mapped by the caller).
    4. Legacy ``MODE=expert`` / ``MODE=quick`` → thinking on/off.

    ``model_type`` is a protocol field now: the upstream normalises every value
    to ``"default"`` (verified 2026-10-18), so it is whatever the matching route
    declares and defaults to ``"default"``.
    """
    model_type = decision.model_type or DEFAULT_MODEL_TYPE

    if decision.thinking is not None:
        thinking = decision.thinking
    elif THINKING == "enabled":
        thinking = True
    elif THINKING == "disabled":
        thinking = False
    else:
        legacy = _legacy_mode_thinking()
        if legacy is not None:
            # Deprecated MODE= still wins over the client field so old .env files
            # keep behaving as they did, but THINKING= is the supported knob.
            thinking = legacy
        else:
            thinking = bool(req_thinking)

    if decision.search is not None:
        search = decision.search
    elif SEARCH == "enabled":
        search = True
    elif SEARCH == "disabled":
        search = False
    else:
        search = bool(req_search)

    return model_type, thinking, search


def _extract_api_key(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-api-key", "").strip()


def _client_ip(request: Request) -> str:
    """Resolve the real client IP, honouring XFF only when the peer is trusted."""
    peer = request.client.host if request.client else None
    return get_real_client_ip(request.headers, peer)


def _check_api_auth(request: Request):
    if ALLOW_UNAUTHENTICATED_API:
        return
    # A valid admin-session token is accepted too: the webui signs its
    # /v1 calls (model list, Playground) with the same bearer token that
    # authenticated the admin session, and anyone holding it already has
    # full control of the account pool.
    supplied = _extract_api_key(request)
    if supplied and verify_admin_token(supplied):
        return
    if not API_KEY_STORE.has_keys():
        raise HTTPException(status_code=503, detail="API key authentication is not configured")
    if not supplied:
        raise HTTPException(status_code=401, detail="Missing API key")
    if not API_KEY_STORE.verify(supplied):
        raise HTTPException(status_code=401, detail="Invalid API key")


def _check_rate_limit(request: Request) -> dict:
    """Return a dict of rate-limit headers to merge into the response.

    Raises HTTPException(429) when the limit is exceeded.
    """
    api_key = _extract_api_key(request) or None
    ip = _client_ip(request)
    allowed, headers = RATE_LIMITER.check(api_key, ip)
    if not allowed:
        # Don't leak whether the key or the IP was the limit, to limit
        # username-enumeration. Same response regardless of dimension.
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Slow down and retry after the indicated time.",
            headers=headers,
        )
    return headers


app = FastAPI(title="DeepSeek Chat API", version="3.3.8")


def _openai_error_payload(
    message: str,
    error_type: str = "api_error",
    code: str | None = None,
) -> dict:
    error = {"message": message, "type": error_type}
    if code:
        error["code"] = code
    return {"error": error}


def _openai_error_type(status_code: int) -> tuple[str, str]:
    if status_code in {400, 422}:
        return "invalid_request_error", "invalid_request"
    if status_code == 401:
        return "authentication_error", "invalid_api_key"
    if status_code == 403:
        return "permission_error", "permission_denied"
    if status_code == 404:
        return "not_found_error", "not_found"
    if status_code == 409:
        return "conflict_error", "conflict"
    if status_code == 429:
        return "rate_limit_error", "rate_limit_exceeded"
    if status_code >= 500:
        return "api_error", "upstream_error"
    return "api_error", "api_error"


@app.exception_handler(StarletteHTTPException)
async def _v1_http_exception_handler(request: Request, exc: StarletteHTTPException):
    if not (request.url.path.startswith("/v1") or request.url.path == "/responses"):
        return await http_exception_handler(request, exc)
    error_type, code = _openai_error_type(exc.status_code)
    message = (
        exc.detail
        if isinstance(exc.detail, str)
        else json.dumps(exc.detail, ensure_ascii=False)
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=_openai_error_payload(message, error_type, code),
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def _v1_validation_exception_handler(request: Request, exc: RequestValidationError):
    if not (request.url.path.startswith("/v1") or request.url.path == "/responses"):
        return await request_validation_exception_handler(request, exc)
    return JSONResponse(
        status_code=422,
        content=_openai_error_payload(
            "Invalid request body",
            "invalid_request_error",
            "invalid_request",
        ),
    )

# ── CORS ─────────────────────────────────────────────────────────────
# Empty ALLOWED_ORIGINS ⇒ same-origin only. We deliberately do NOT set
# `allow_origins=["*"]` with `allow_credentials=True` (spec violation; most
# browsers silently strip credentials).
if ALLOWED_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_credentials=ALLOW_CREDENTIALS,
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    # Default: strict same-origin. The middleware only does anything when
    # `allow_origins` is non-empty, so omitting it is safe.
    pass


# ── Security response headers ────────────────────────────────────────
@app.middleware("http")
async def _add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    # The browser-login view is the only page that must be frameable, and
    # only by the same-origin WebUI. Everything else keeps DENY.
    if request.url.path.startswith("/browser-login"):
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    else:
        response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    # WebUI gets an extra CSP; JSON APIs do not (SSE is not worth breaking).
    path = request.url.path
    if path.startswith("/webui") or path == "/webui":
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "frame-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'",
        )
    elif path.startswith("/browser-login"):
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self' ws: wss:; "
            "frame-ancestors 'self'; "
            "base-uri 'self'",
        )
    return response


pool = get_pool()

# Start the rolling history sampler (v3 webui dashboard charts).
start_sampler(get_stats())

# The multi-account keepalive scheduler is in-process so it shares the same
# AccountPool lock and cannot lose cookie updates to a second writer.
_keepalive_scheduler = start_scheduler(pool)
atexit.register(_keepalive_scheduler.stop)

if pool.count() == 0:
    log.warning(
        "no_accounts_in_pool",
        extra={"hint": "Add an account in the WebUI or data/accounts.json"},
    )


# ── Startup safety checks ────────────────────────────────────────────
def _validate_startup() -> None:
    """Run fatal pre-flight checks; raise SystemExit on hard failures."""
    binding_public = HOST in {"0.0.0.0", "::"}  # noqa: S104 — by design
    weak_password = is_admin_password_weak()

    if binding_public and weak_password and not ALLOW_INSECURE_PUBLIC_DEFAULTS:
        msg = (
            "\n"
            "=" * 72 + "\n"
            "FATAL: Refusing to start with default admin password on a public bind.\n"
            f"  HOST={HOST} (public), DEEPSEEK_ADMIN_PASSWORD is unset or weak.\n"
            "  Fix one of:\n"
            "    1. Bind to loopback:  HOST=127.0.0.1 (recommended for dev).\n"
            "    2. Set a strong password:  DEEPSEEK_ADMIN_PASSWORD=<random>.\n"
            "    3. Acknowledge risk explicitly:\n"
            "         ALLOW_INSECURE_PUBLIC_DEFAULTS=true\n"
            "       (only do this behind a reverse proxy that already enforces auth.)\n"
            "=" * 72 + "\n"
        )
        sys.stderr.write(msg)
        sys.stderr.flush()
        raise SystemExit(2)

    if binding_public and not ALLOWED_ORIGINS:
        log.warning(
            "cors_strict_default_in_effect",
            extra={
                "host": HOST,
                "hint": "Set ALLOWED_ORIGINS=https://app.example.com for browser access.",
            },
        )

    if not crypto_is_enabled() and pool.count() > 0:
        log.warning(
            "credentials_stored_in_plaintext",
            extra={
                "hint": (
                    "Set DEEPSEEK_ENCRYPTION_KEY to encrypt data/accounts.json. "
                    "Generate one with: python -c \"from cryptography.fernet "
                    "import Fernet; print(Fernet.generate_key().decode())\""
                ),
            },
        )

    if weak_password:
        log.warning("admin_password_is_weak", extra={"host": HOST})

    log.info(
        "startup_ok",
        extra={
            "host": HOST,
            "port": PORT,
            "accounts": pool.count(),
            "crypto_enabled": crypto_is_enabled(),
            "cors_origins": len(ALLOWED_ORIGINS),
            "allow_unauthenticated_api": ALLOW_UNAUTHENTICATED_API,
        },
    )


# ---- Pydantic models ----

class ContentPart(BaseModel):
    type: str
    text: Optional[str] = None


class FunctionCall(BaseModel):
    name: str
    arguments: str  # JSON string


class ToolCall(BaseModel):
    id: str
    type: str = "function"
    function: FunctionCall


class ChatMessage(BaseModel):
    role: str
    content: Optional[Union[str, list[ContentPart]]] = None
    tool_calls: Optional[list[ToolCall]] = None
    tool_call_id: Optional[str] = None


class FunctionDef(BaseModel):
    name: str
    description: Optional[str] = ""
    parameters: Optional[dict] = None


class ToolDef(BaseModel):
    type: str = "function"
    function: FunctionDef


class StreamOptions(BaseModel):
    include_usage: Optional[bool] = False


class ChatCompletionRequest(BaseModel):
    model: Optional[str] = None
    messages: list[ChatMessage]
    stream: Optional[bool] = False
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    tools: Optional[list[ToolDef]] = None
    tool_choice: Optional[Union[str, dict]] = None
    # OpenAI's top-level end-user identifier.  It is used only as one part of
    # an opaque, caller-scoped upstream-session cache key.
    user: Optional[str] = None
    # Capability switches (NOT a model selector): thinking_mode → upstream
    # thinking_enabled, search_enabled → upstream search_enabled.
    thinking_mode: Optional[bool] = False
    search_enabled: Optional[bool] = False
    stream_options: Optional[StreamOptions] = None


class ResponsesRequest(BaseModel):
    model: Optional[str] = None
    input: Optional[Union[str, list[dict[str, Any]]]] = None
    instructions: Optional[Union[str, list[dict[str, Any]]]] = None
    stream: Optional[bool] = False
    tools: Optional[list[dict[str, Any]]] = None
    tool_choice: Optional[Union[str, dict]] = None
    max_output_tokens: Optional[int] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    user: Optional[str] = None
    previous_response_id: Optional[str] = None
    reasoning: Optional[dict] = None
    text: Optional[dict] = None
    metadata: Optional[dict] = None
    store: Optional[bool] = False
    parallel_tool_calls: Optional[bool] = True
    truncation: Optional[str] = "disabled"
    include: Optional[list[str]] = None
    prompt_cache_key: Optional[str] = None
    client_metadata: Optional[dict[str, Any]] = None
    service_tier: Optional[str] = None


class ModelInfo(BaseModel):
    id: str
    object: str = "model"
    created: int
    owned_by: str = "ds2api"
    display_name: Optional[str] = None
    reasoning: Optional[bool] = None
    provider: Optional[str] = None
    input_modalities: Optional[list[str]] = None
    supported_endpoints: Optional[list[str]] = None


class ModelList(BaseModel):
    object: str = "list"
    data: list[ModelInfo]


def _model_display_name(name: str) -> str:
    snapshot = get_runtime_config().snapshot()
    if name == zai_model_name():
        return os.environ.get("ZAI_MODEL_DISPLAY_NAME", "GLM-5.3 Flash").strip() or "GLM-5.3 Flash"
    if name in available_kimi_models():
        if name == kimi_model_name():
            return (
                os.environ.get("KIMI_MODEL_DISPLAY_NAME", "Kimi K2.6")
                .strip()
                or "Kimi K2.6"
            )
        if name == "kimi-k3":
            return "Kimi K3"
        if name == "kimi-k2.8":
            return "Kimi K2.8"
        return name
    if name == qwen_model_name():
        return (
            os.environ.get("QWEN_MODEL_DISPLAY_NAME", "Qwen3.7 Max")
            .strip()
            or "Qwen3.7 Max"
        )
    if name in available_hunyuan_models():
        return (
            os.environ.get("HUNYUAN_MODEL_DISPLAY_NAME", "Hunyuan HY4 Preview")
            .strip()
            or "Hunyuan HY4 Preview"
        )
    if name in available_wenxin_models():
        return (
            os.environ.get("WENXIN_MODEL_DISPLAY_NAME", "Wenxin Smart Mode")
            .strip()
            or "Wenxin Smart Mode"
        )
    if name in available_doubao_models():
        return (
            os.environ.get("DOUBAO_MODEL_DISPLAY_NAME", "Doubao Seed")
            .strip()
            or "Doubao Seed"
        )
    if name in available_mimo_models():
        if name == mimo_model_name():
            return os.environ.get("MIMO_MODEL_DISPLAY_NAME", "MiMo V2.6 Pro").strip() or "MiMo V2.6 Pro"
        if name == "mimo-v2.6-flash":
            return "MiMo V2.6 Flash"
        if name == "mimo-v2.6-pro-ultraspeed-studio":
            return "MiMo V2.6 Pro UltraSpeed Studio"
        return name
    if name == snapshot["model_name"]:
        return str(snapshot["display_name"])
    configured = os.environ.get("MODEL_DISPLAY_NAME", "").strip()
    if configured:
        return configured
    if name == "deepseek-chat":
        return "DeepSeek Chat"
    return name


def _available_model_names() -> list[str]:
    configured = MODEL_ROUTER.models or [_model_name()]
    names = list(configured)
    zai_name = zai_model_name()
    if zai_name not in names:
        names.append(zai_name)
    for kimi_name in available_kimi_models():
        if kimi_name not in names:
            names.append(kimi_name)
    if qwen_model_name() not in names:
        names.append(qwen_model_name())
    for hunyuan_name in available_hunyuan_models():
        if hunyuan_name not in names:
            names.append(hunyuan_name)
    for wenxin_name in available_wenxin_models():
        if wenxin_name not in names:
            names.append(wenxin_name)
    for doubao_name in available_doubao_models():
        if doubao_name not in names:
            names.append(doubao_name)
    for mimo_name in available_mimo_models():
        if mimo_name not in names:
            names.append(mimo_name)
    return names


def _build_model_info(name: str) -> ModelInfo:
    provider = provider_for_model(name)
    return ModelInfo(
        id=name,
        created=MODEL_CREATED_AT,
        owned_by=provider,
        display_name=_model_display_name(name),
        reasoning=True,
        provider=provider,
        input_modalities=["text"],
        supported_endpoints=["/chat/completions", "/responses", "/messages"],
    )


# ---- Account pool helpers ----

class AcquiredAccount:
    """Context manager wrapping an acquired pool account."""
    def __init__(self, acct, cache_key: str | None = None):
        self.acct = acct
        self.adapter = acct.adapter
        self._provider = normalize_provider(getattr(acct, "provider", PROVIDER_DEEPSEEK))
        self._session_id: str | None = None
        self._parent_message_id: int | None = None
        self._cache_key = None if self._provider == PROVIDER_MIMO else cache_key
        self._cached_session: ChatSession | None = None
        self._released = False
        # If we have a cache key, try to reuse the existing session first.
        if self._cache_key:
            cached = SESSION_CACHE.get(self._cache_key)
            # A DeepSeek session belongs to the credentials that created it.
            # Reject mismatches (including legacy entries without a binding)
            # instead of submitting account A's session with account B.
            if (
                cached is not None
                and cached.account_id == self.acct.id
                and cached.provider == self._provider
            ):
                self._cached_session = cached

    def create_session(
        self,
        prompt: str = "",
        *,
        upstream_model: str | None = None,
        thinking_enabled: bool = False,
        search_enabled: bool = False,
    ) -> str:
        if self._provider == PROVIDER_MIMO:
            self._session_id = self.adapter.create_session()
            return self._session_id
        if self._provider == PROVIDER_ZAI:
            # z.ai stores chats server-side. Create one before completion and
            # reuse its generated user/assistant message ids in the signed body.
            session_id = self.adapter.create_session(
                prompt,
                model=upstream_model or "",
                thinking_enabled=thinking_enabled,
                search_enabled=search_enabled,
            )
            self._session_id = session_id
            if self._cache_key:
                sess = ChatSession(
                    chat_session_id=session_id,
                    account_id=self.acct.id,
                    provider=self._provider,
                )
                SESSION_CACHE.put(self._cache_key, sess)
                self._cached_session = sess
            return session_id
        if self._provider == PROVIDER_KIMI:
            session_id = self.adapter.create_session()
            self._session_id = session_id
            if self._cache_key:
                sess = ChatSession(
                    chat_session_id=session_id,
                    account_id=self.acct.id,
                    provider=self._provider,
                )
                SESSION_CACHE.put(self._cache_key, sess)
                self._cached_session = sess
            return session_id
        if self._provider == PROVIDER_QWEN:
            session_id = self.adapter.create_session()
            self._session_id = session_id
            if self._cache_key:
                sess = ChatSession(
                    chat_session_id=session_id,
                    account_id=self.acct.id,
                    provider=self._provider,
                )
                SESSION_CACHE.put(self._cache_key, sess)
                self._cached_session = sess
            return session_id
        if self._provider == PROVIDER_HUNYUAN:
            session_id = self.adapter.create_session()
            self._session_id = session_id
            if self._cache_key:
                sess = ChatSession(
                    chat_session_id=session_id,
                    account_id=self.acct.id,
                    provider=self._provider,
                )
                SESSION_CACHE.put(self._cache_key, sess)
                self._cached_session = sess
            return session_id
        if self._provider == PROVIDER_WENXIN:
            session_id = self.adapter.create_session()
            self._session_id = session_id
            if self._cache_key:
                sess = ChatSession(
                    chat_session_id=session_id,
                    account_id=self.acct.id,
                    provider=self._provider,
                )
                SESSION_CACHE.put(self._cache_key, sess)
                self._cached_session = sess
            return session_id
        if self._provider == PROVIDER_DOUBAO:
            session_id = self.adapter.create_session()
            self._session_id = session_id
            if self._cache_key:
                sess = ChatSession(
                    chat_session_id=session_id,
                    account_id=self.acct.id,
                    provider=self._provider,
                )
                SESSION_CACHE.put(self._cache_key, sess)
                self._cached_session = sess
            return session_id
        # Reuse the cached session if we have one — multi-turn support.
        if self._cached_session is not None:
            self._session_id = self._cached_session.chat_session_id
            self._parent_message_id = self._cached_session.parent_message_id
            return self._session_id
        ds_id = self.adapter.create_session()
        self._session_id = ds_id
        if self._cache_key:
            sess = ChatSession(chat_session_id=ds_id, account_id=self.acct.id)
            sess.provider = self._provider
            SESSION_CACHE.put(self._cache_key, sess)
            # Keep the live reference so record_message_id() can update the
            # parent id in place (the cache stores the same object).
            self._cached_session = sess
        return ds_id

    def prepare_prompt(self, full_prompt: str) -> str:
        """Return the prompt to send upstream for this turn.

        Three cases, decided by ``session_cache.delta_prompt``:

        * **Exact replay** — the client retried the same turn: send it again.
        * **Continuation** — the client resends the whole history and it grew:
          send only the new tail. The parent chain carries the earlier context
          server-side, which keeps the DeepSeek conversation from embedding every
          previous turn again (quadratic growth / bot-spam signature).
        * **Divergent history** — the client only sent the newest message, or
          edited/truncated the history: send the *full* prompt **on the existing
          session and parent chain**.

        That last case used to invalidate the cached session, which silently threw
        away the upstream conversation: a client that sends only the new user
        message per turn ("我叫什么名字？") got a brand-new session with no
        context, so the model answered as if the conversation had never happened.
        Reusing the chain and resending the whole prompt costs a few extra tokens
        once but keeps the conversation intact — verified against the live
        upstream.

        Call *before* ``create_session()``.
        """
        if self._provider in {
            PROVIDER_ZAI,
            PROVIDER_KIMI,
            PROVIDER_QWEN,
            PROVIDER_HUNYUAN,
            PROVIDER_WENXIN,
            PROVIDER_DOUBAO,
            PROVIDER_MIMO,
        }:
            return full_prompt
        cached = self._cached_session
        if cached is None or not (self._cache_key and cached.sent_prompt):
            return full_prompt

        delta = delta_prompt(cached.sent_prompt, full_prompt)
        if delta is None:
            # Divergent history: keep the session, resend everything. The reply
            # to *this* turn is still appended to the same upstream thread, so
            # the next turn gets its context back.
            cached.diverged_turns += 1
            cached.sent_prompt = full_prompt
            log.info(
                "session_prompt_diverged",
                extra={
                    "session_id": cached.chat_session_id,
                    "account_id": self.acct.id,
                    "diverged_turns": cached.diverged_turns,
                    "prompt_chars": len(full_prompt),
                },
            )
            return full_prompt
        if delta != full_prompt:
            log.debug(
                "session_prompt_delta",
                extra={"session_id": cached.chat_session_id, "delta_chars": len(delta)},
            )
        return delta

    @property
    def session_id(self) -> str:
        if self._session_id is None:
            raise RuntimeError("No session created")
        return self._session_id

    @property
    def parent_message_id(self) -> int | None:
        """Return the current parent_message_id for the cached session, or None
        for a fresh session (caller should treat as a new conversation).
        """
        return self._parent_message_id

    def record_message_id(self, mid: int, session_id: str | None = None,
                          full_prompt: str = "") -> None:
        """After a successful turn, record the upstream response message id
        (and the session that actually answered) into the cache so the next
        turn sends the correct ``parent_message_id``.

        Fixes the v3.2.2 leftover: previously never called, so a reused
        cached session sent ``parent_message_id=0`` on the second turn and
        the upstream replied with an empty body.
        """
        if not self._cache_key:
            return
        sess = self._cached_session
        if sess is None:
            candidate = SESSION_CACHE.get(self._cache_key)
            # Do not overwrite another account's live cache entry in a
            # concurrent fallback path; this turn must establish its own.
            sess = candidate if candidate and candidate.account_id == self.acct.id else None
        if sess is None:
            sess = ChatSession(
                chat_session_id=session_id or "",
                account_id=self.acct.id,
                provider=self._provider,
            )
            SESSION_CACHE.put(self._cache_key, sess)
        # Defensive repair for entries constructed by older versions.
        sess.account_id = self.acct.id
        sess.provider = self._provider
        if session_id:
            sess.chat_session_id = session_id
        sess.parent_message_id = mid
        if full_prompt:
            sess.sent_prompt = full_prompt
        self._cached_session = sess

    def release(self):
        if self._released:
            return
        self._released = True
        if self._provider == PROVIDER_MIMO:
            try:
                try:
                    pool.persist_adapter_cookies(self.acct, self.adapter)
                except Exception:
                    log.warning("mimo_cookie_persist_failed", extra={"account_id": self.acct.id})
                if self.acct._adapter is not self.adapter:
                    try:
                        self.adapter.close()
                    except Exception:
                        log.warning("mimo_adapter_close_failed", extra={"account_id": self.acct.id})
            finally:
                try:
                    pool.release(self.acct)
                finally:
                    _UPSTREAM_LIMITER.release()
            return
        try:
            if self._provider == PROVIDER_KIMI:
                try:
                    pool.persist_adapter_tokens(self.acct)
                except Exception:
                    log.warning("kimi_token_persist_failed", extra={"account_id": self.acct.id})
        finally:
            try:
                pool.release(self.acct)
            finally:
                _UPSTREAM_LIMITER.release()


def _acquire(
    cache_key: str | None = None,
    provider: str = PROVIDER_DEEPSEEK,
) -> AcquiredAccount:
    # Global upstream concurrency cap (e.g. coding agents spawning parallel
    # sub-agents). Wait for a slot before grabbing a pool account so queued
    # requests don't hold accounts busy.
    _UPSTREAM_LIMITER.acquire()
    try:
        wanted = normalize_provider(provider)
        if wanted == PROVIDER_MIMO:
            cache_key = None
        cached = SESSION_CACHE.get(cache_key) if cache_key else None
        # Keep a cached conversation on its creating account whenever it is
        # available.  If it is busy/error, safely start a fresh conversation
        # on the next idle account rather than crossing credentials.
        deadline = time.monotonic() + ACCOUNT_WAIT_SECS
        waited = False
        while True:
            acct = (
                pool.acquire_by_id(cached.account_id, provider=wanted)
                if cached and cached.account_id
                else None
            )
            if acct is None:
                acct = pool.acquire(provider=wanted)
            if acct is not None:
                break
            if pool.count(wanted) == 0 or time.monotonic() >= deadline:
                break
            waited = True
            time.sleep(0.1)
    except Exception:
        _UPSTREAM_LIMITER.release()
        raise
    if acct is None:
        _UPSTREAM_LIMITER.release()
        if waited:
            log.warning(
                "upstream_account_wait_timeout",
                extra={"provider": wanted, "timeout_secs": ACCOUNT_WAIT_SECS},
            )
        raise HTTPException(
            status_code=503,
            detail=f"No {provider_label(provider)} accounts available; add one or wait for a busy account",
            headers={"Retry-After": TRANSIENT_RETRY_AFTER_SECS},
        )
    return AcquiredAccount(acct, cache_key=cache_key)


def _upstream_limit_from_env() -> int:
    try:
        return max(1, int(os.environ.get("DEEPSEEK_MAX_CONCURRENCY", "2")))
    except ValueError:
        return 2


_UPSTREAM_LIMITER = threading.BoundedSemaphore(_upstream_limit_from_env())


def _cache_scope(request: Request) -> str:
    """Return an opaque caller scope for session-cache isolation."""
    supplied = _extract_api_key(request)
    if supplied:
        digest = hashlib.sha256(supplied.encode("utf-8")).hexdigest()
        return f"key:{digest}"
    return f"ip:{_client_ip(request)}"


def _cache_key(request: Request, conversation_id: str | None,
               mode_conversation_id: str | None = None) -> str | None:
    """Return the session-cache key for one conversation.

    One namespace per conversation: stream and non-stream turns of the same
    conversation share a cache entry, so flipping ``stream`` mid-thread no longer
    starts a fresh upstream session.  (The old ``stream:``/``nonstream:`` split
    existed to dodge an empty response when a streamed session was reused
    non-streamed; keeping the parent chain plus the existing empty-response retry
    covers that case without losing the conversation.)

    Legacy 3-argument calls ``_cache_key(request, mode, conversation_id)`` are
    still accepted — ``mode`` is then ignored — so out-of-tree callers and older
    tests keep working.
    """
    if mode_conversation_id is not None:
        conversation_id = mode_conversation_id
    derived = SessionCache.derive_cache_key(_cache_scope(request), conversation_id)
    return f"conv:{derived}" if derived else None


def _extract_openai_user(req: ChatCompletionRequest, request: Request) -> str | None:
    """Derive a per-conversation cache key for the OpenAI endpoint.

    Only explicit identifiers are accepted.  Message-content hashing was
    unsafe because unrelated callers commonly share the same opener.
    """
    cid = request.headers.get("X-Conversation-Id", "").strip()
    if cid:
        return f"header:{cid}"
    return SessionCache.derive_conversation_id(req.user, None)


def _extract_anthropic_user(req: AnthropicRequest, request: Request) -> str | None:
    cid = request.headers.get("X-Conversation-Id", "").strip()
    if cid:
        return f"header:{cid}"
    metadata = req.metadata or {}
    return SessionCache.derive_conversation_id(None, metadata)


# ---- Message / prompt building ----

def _extract_text(content: Union[str, list[ContentPart], None]) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return " ".join(
        p.text for p in content if p.type == "text" and p.text
    )


def _build_prompt(messages: list[ChatMessage], tools: list[ToolDef] | None = None,
                  tool_choice: str | dict | None = None) -> str:
    """Build a prompt string from messages, injecting tool definitions if present."""
    parts = []

    tool_prompt_text = None
    if tools:
        tool_prompt_text = build_dsml_tool_prompt([t.model_dump() for t in tools], tool_choice)

    has_system_message = any(m.role == "system" for m in messages)
    tool_injected = False

    for m in messages:
        if m.role == "system":
            text = _extract_text(m.content)
            if tool_prompt_text and not tool_injected:
                text = text + "\n\n" + tool_prompt_text if text else tool_prompt_text
                tool_injected = True
            if text:
                parts.append(f"System: {text}")
        elif m.role == "user":
            parts.append(f"User: {_extract_text(m.content)}")
        elif m.role == "assistant":
            segs = []
            text = _extract_text(m.content)
            if text:
                segs.append(text)
            if m.tool_calls:
                dsml = format_tool_calls_for_prompt([tc.model_dump() for tc in m.tool_calls])
                if dsml:
                    segs.append(dsml)
            if segs:
                parts.append(f"Assistant: {' '.join(segs)}")
        elif m.role == "tool":
            result = _extract_text(m.content)
            prefix = f"Tool result (call_id={m.tool_call_id}):" if m.tool_call_id else "Tool result:"
            parts.append(f"{prefix} {result[:1000]}")

    if tool_prompt_text and not has_system_message:
        parts.insert(0, f"System: {tool_prompt_text}")

    return "\n".join(parts)


def _truncate_middle(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head_size = max(1, limit * 2 // 3)
    tail_size = max(1, limit - head_size)
    return (
        text[:head_size]
        + "\n[... truncated to fit the upstream context ...]\n"
        + text[-tail_size:]
    )


def _compact_codex_messages(
    messages: list[ChatMessage],
    tools: list[ToolDef] | None,
    tool_choice: str | dict | None,
    *,
    provider: str,
    codex_mode: bool = False,
) -> tuple[list[ChatMessage], str, dict[str, int]]:
    """Fit Codex tool-loop history into a provider's usable prompt budget.

    Codex repeats complete tool-call arguments, which can contain an entire
    generated file. Those bytes are useful client-side, not as model context on
    the next turn. Web providers also inject different hidden prompts, so each
    provider gets a conservative local budget rather than trusting the client
    catalog exclusively.
    """
    safe_tokens = PROVIDER_SAFE_PROMPT_TOKENS.get(provider, 64_000)
    compacted = [message.model_copy(deep=True) for message in messages]
    if codex_mode and tools:
        compacted.insert(0, ChatMessage(
            role="system",
            content=CODEX_LOCAL_TOOL_GUARD,
        ))
    for message in compacted:
        if message.role == "assistant":
            if isinstance(message.content, str):
                message.content = _truncate_middle(message.content, 4_000)
            for tool_call in message.tool_calls or []:
                tool_call.function.arguments = _truncate_middle(
                    tool_call.function.arguments,
                    1_200,
                )
        elif message.role == "tool":
            message.content = _truncate_middle(_extract_text(message.content), 800)

    prompt = _build_prompt(compacted, tools, tool_choice)
    original_tokens = count_text(_build_prompt(messages, tools, tool_choice))
    compacted_tokens = count_text(prompt)
    if compacted_tokens <= safe_tokens:
        return compacted, prompt, {
            "original_tokens": original_tokens,
            "compacted_tokens": compacted_tokens,
            "dropped_messages": 0,
        }

    system_messages = [message for message in compacted if message.role == "system"]
    dialogue = [message for message in compacted if message.role != "system"]
    keep_tail = min(4, len(dialogue))
    older = dialogue[:-keep_tail] if keep_tail else dialogue
    recent = dialogue[-keep_tail:] if keep_tail else []
    summary = ""
    if older:
        summary = compaction_fallback_summary(
            [message.model_dump() for message in older],
            max_chars=MIMO_COMPACTION_FALLBACK_CHARS,
        )
    summarized = list(system_messages)
    if summary:
        summarized.append(ChatMessage(
            role="system",
            content=f"Compacted prior conversation:\n{summary}",
        ))
    summarized.extend(recent)
    prompt = _build_prompt(summarized, tools, tool_choice)
    compacted_tokens = count_text(prompt)
    if compacted_tokens > safe_tokens:
        for message in summarized:
            if message.role == "user":
                message.content = _truncate_middle(
                    _extract_text(message.content),
                    12_000,
                )
        prompt = _build_prompt(summarized, tools, tool_choice)
        compacted_tokens = count_text(prompt)
    if compacted_tokens > safe_tokens:
        for message in summarized:
            if message.role == "assistant":
                if isinstance(message.content, str):
                    message.content = _truncate_middle(message.content, 2_000)
                for tool_call in message.tool_calls or []:
                    tool_call.function.arguments = _truncate_middle(
                        tool_call.function.arguments,
                        600,
                    )
            elif message.role == "tool":
                message.content = _truncate_middle(
                    _extract_text(message.content),
                    400,
                )
            elif message.role == "user":
                message.content = _truncate_middle(
                    _extract_text(message.content),
                    6_000,
                )
        prompt = _build_prompt(summarized, tools, tool_choice)
        compacted_tokens = count_text(prompt)
    return summarized, prompt, {
        "original_tokens": original_tokens,
        "compacted_tokens": compacted_tokens,
        "dropped_messages": max(0, len(dialogue) - len(recent)),
    }


# ---- OpenAI SSE helpers ----

def _openai_chunk(proxy_id: str, content: str = "", finish: bool = False,
                  reasoning_content: str = None, role: str = None,
                  usage: dict | None = None,
                  model_name: str | None = None) -> str:
    delta = {}
    if not finish:
        if role:
            delta["role"] = role
        if reasoning_content is not None:
            delta["reasoning_content"] = reasoning_content
        elif content:
            delta["content"] = content
    choice = {
        "index": 0,
        "delta": delta,
        "finish_reason": "stop" if finish else None,
    }
    chunk = {
        "id": proxy_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model_name or _model_name(),
        "choices": [choice],
    }
    if usage is not None:
        chunk["usage"] = usage
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


def _openai_usage_chunk(
    proxy_id: str,
    usage: dict,
    model_name: str | None = None,
) -> str:
    chunk = {
        "id": proxy_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model_name or _model_name(),
        "choices": [],
        "usage": usage,
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


def _emit_tool_calls_chunks(
    tool_calls: list[dict],
    chat_id: str,
    usage: dict | None = None,
    model_name: str | None = None,
) -> list[str]:
    """生成 OpenAI 流式 tool_calls SSE 事件。"""
    chunks = []
    created = int(time.time())
    for i, tc in enumerate(tool_calls):
        # 首帧：id + name
        delta = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "index": i,
                "id": tc["id"],
                "type": "function",
                "function": {"name": tc["function"]["name"], "arguments": ""},
            }],
        }
        chunks.append(f"data: {json.dumps({'id': chat_id, 'object': 'chat.completion.chunk', 'created': created, 'model': model_name or _model_name(), 'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]}, ensure_ascii=False)}\n\n")
        # 次帧：arguments
        delta2 = {
            "tool_calls": [{
                "index": i,
                "function": {"arguments": tc["function"]["arguments"]},
            }],
        }
        chunks.append(f"data: {json.dumps({'id': chat_id, 'object': 'chat.completion.chunk', 'created': created, 'model': model_name or _model_name(), 'choices': [{'index': 0, 'delta': delta2, 'finish_reason': None}]}, ensure_ascii=False)}\n\n")
    # finish
    finish_chunk = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model_name or _model_name(),
        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
    }
    if usage is not None:
        finish_chunk["usage"] = usage
    chunks.append(f"data: {json.dumps(finish_chunk, ensure_ascii=False)}\n\n")
    return chunks


# ---- Extract tool names ----

def _get_tool_names(tools: list[ToolDef] | None) -> list[str]:
    names = []
    for t in tools or []:
        if t.function and t.function.name:
            names.append(t.function.name)
    return names


# ---- Response parsing for non-streaming ----

def _parse_response_for_tools(text: str, tool_names: list[str]) -> tuple[list[dict] | None, str]:
    """Parse response text for DSML tool calls. Returns (tool_calls, cleaned_text)."""
    return parse_dsml_tool_calls(text, tool_names)


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _is_codex_request(request: Request, payload: ResponsesRequest) -> bool:
    indicators = [
        request.headers.get("user-agent", ""),
        request.headers.get("originator", ""),
        json.dumps(payload.client_metadata or {}, ensure_ascii=False),
    ]
    return any("codex" in str(value).lower() for value in indicators)


def _codex_tool_mode() -> str:
    mode = os.environ.get("CODEX_TOOL_MODE", "core").strip().lower()
    return mode if mode in {"core", "all", "none"} else "core"


def _codex_allowed_tools() -> set[str]:
    raw = os.environ.get("CODEX_ALLOWED_TOOLS", "")
    return {item.strip() for item in raw.split(",") if item.strip()}


def _is_context_limit_error(error: Exception) -> bool:
    if isinstance(error, UpstreamContextLimitError):
        return True
    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "query is too long",
            "input is too long",
            "context length",
            "maximum context",
            "prompt too long",
            "请求过长",
            "上下文过长",
        )
    )


def _codex_context_window(provider: str) -> int:
    return {
        PROVIDER_MIMO: MIMO_CODEX_CONTEXT_WINDOW,
        "wenxin": 64_000,
        "deepseek": 128_000,
        "zai": 128_000,
        "kimi": 128_000,
        "qianwen": 128_000,
        "hunyuan": 128_000,
        "doubao": 128_000,
    }.get(provider, 128_000)


def _codex_auto_compact_token_limit(provider: str, context_window: int) -> int:
    # Codex needs an explicit threshold for custom models. Leave enough room
    # for the next response and tool output while compacting well before the
    # provider's hard context limit.
    if provider == PROVIDER_MIMO:
        return min(12_000, max(8_000, int(context_window * 3 / 8)))
    return max(16_000, min(64_000, int(context_window / 2)))


def _codex_model_manifest_entry(name: str) -> dict[str, Any]:
    info = _build_model_info(name)
    provider = info.provider or PROVIDER_DEEPSEEK
    context_window = _codex_context_window(provider)
    return {
        "slug": info.id,
        "display_name": info.display_name or info.id,
        "description": f"{provider_label(provider)} model routed through DS2API.",
        "default_reasoning_level": "none",
        "supported_reasoning_levels": [{
            "effort": "none",
            "description": "Direct responses without an explicit reasoning budget.",
        }],
        "shell_type": "unified_exec",
        "visibility": "list",
        "supported_in_api": True,
        "priority": 50,
        "additional_speed_tiers": [],
        "service_tiers": [],
        "default_service_tier": None,
        "availability_nux": None,
        "upgrade": None,
        "model_messages": {
            "instructions_template": "",
            "instructions_variables": None,
            "approvals": None,
            "collaboration_modes": None,
            "auto_review": None,
            "permissions": None,
            "multi_agent": None,
            "token_budget": None,
            "guardian_v2": None,
        },
        "include_skills_usage_instructions": False,
        "include_plugin_usage_instructions": False,
        "include_apps_usage_instructions": False,
        "supports_reasoning_summary_parameter": True,
        "default_reasoning_summary": "auto",
        "support_verbosity": False,
        "default_verbosity": None,
        "apply_patch_tool_type": None,
        "web_search_tool_type": "text",
        "truncation_policy": {"mode": "tokens", "limit": 10_000},
        "supports_image_detail_original": False,
        "supports_parallel_tool_calls": False,
        "context_window": context_window,
        "max_context_window": context_window,
        "auto_compact_token_limit": _codex_auto_compact_token_limit(
            provider,
            context_window,
        ),
        "comp_hash": None,
        "effective_context_window_percent": 85,
        "experimental_supported_tools": [],
        "input_modalities": ["text"],
        "supports_search_tool": False,
        "use_responses_lite": False,
        "node_repl_auto_review_required": False,
        "node_repl_disabled": False,
        "auto_review_model_override": None,
        "model_specialty": None,
        "tool_mode": None,
        "multi_agent_version": None,
    }


def _build_codex_models_manifest() -> dict[str, Any]:
    return {
        "models": [
            _codex_model_manifest_entry(name)
            for name in _available_model_names()
        ],
    }


# ---- Endpoints ----

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/v1/models")
async def list_models(request: Request):
    _check_api_auth(request)
    rate_headers = _check_rate_limit(request)
    codex_catalog = (
        "codex" in request.headers.get("user-agent", "").lower()
        or "codex" in request.headers.get("originator", "").lower()
        or request.query_params.get("format") == "codex"
    )
    if codex_catalog:
        return JSONResponse(
            _build_codex_models_manifest(),
            headers={
                **rate_headers,
                "Cache-Control": "no-cache",
            },
        )
    names = _available_model_names()
    return JSONResponse(
        ModelList(data=[_build_model_info(name) for name in names]).model_dump(),
        headers=rate_headers,
    )


@app.get("/v1/models/{model_id:path}")
async def retrieve_model(model_id: str, request: Request):
    _check_api_auth(request)
    rate_headers = _check_rate_limit(request)
    if model_id not in _available_model_names():
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")
    return JSONResponse(
        _build_model_info(model_id).model_dump(),
        headers=rate_headers,
    )


@app.get("/v1/provider-manifest")
async def provider_manifest(request: Request):
    """Expose a stable capability manifest for external gateway synchronizers."""
    _check_api_auth(request)
    rate_headers = _check_rate_limit(request)
    pricing = get_runtime_config().snapshot()["pricing"]
    models = []
    for name in _available_model_names():
        info = _build_model_info(name)
        models.append({
            **info.model_dump(),
            "endpoints": info.supported_endpoints,
            "capabilities": {
                "stream": True,
                "thinking": True,
                "search": provider_for_model(name) != PROVIDER_MIMO and SEARCH != "disabled",
                "tools": True,
                "responses": True,
                "vision": False,
            },
            "provider": provider_for_model(name),
            "provider_label": provider_label(provider_for_model(name)),
            "pricing": pricing,
        })
    return JSONResponse(
        {
            "schema_version": 1,
            "provider": {
                "id": "ds2api",
                "display_name": "DS2API",
                "protocols": [
                    "openai.chat_completions",
                    "openai.responses",
                    "anthropic.messages",
                ],
            },
            "models": models,
            "pricing": {
                "automatic": False,
                "managed_by": "sub2api",
                "source": "ds2api_runtime_config",
            },
        },
        headers=rate_headers,
    )


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest, request: Request):
    _check_api_auth(request)
    rate_headers = _check_rate_limit(request)
    proxy_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    prompt = _build_prompt(req.messages, req.tools, req.tool_choice)

    # Resolve model_type / thinking / search.  There is no quick-vs-expert model
    # any more: the upstream serves one model and normalises model_type to
    # "default"; the capabilities live in thinking_enabled / search_enabled.
    decision = MODEL_ROUTER.route_for(req.model)
    provider = provider_for_model(req.model)
    public_model = _public_model_for_request(req.model, provider)
    upstream_model = _upstream_model_for_request(public_model, provider)
    model_type, thinking, search = resolve_model_controls(
        decision, req_thinking=bool(req.thinking_mode), req_search=bool(req.search_enabled)
    )

    # One cache namespace per conversation: switching stream mode mid-thread must
    # not silently start a brand-new upstream session (it used to, because the
    # key was prefixed with stream:/nonstream:).
    openai_user = _extract_openai_user(req, request)
    cache_key = _cache_key(request, openai_user) if provider != PROVIDER_MIMO else None
    if req.stream:
        include_usage = bool(req.stream_options and req.stream_options.include_usage)
        return await _handle_stream(proxy_id, prompt, req.tools, model_type=model_type, thinking_mode=thinking, search_enabled=search, rate_headers=rate_headers,
                                    cache_key=cache_key, include_usage=include_usage,
                                    provider=provider, model_name=public_model,
                                    upstream_model=upstream_model)

    return await run_in_threadpool(_handle_nonstream, proxy_id, prompt, req.tools, model_type=model_type, thinking_mode=thinking, search_enabled=search,
                             cache_key=cache_key, provider=provider,
                             model_name=public_model,
                             upstream_model=upstream_model)


# ---- Anthropic /v1/messages endpoint ----


@app.post("/v1/messages")
async def messages(req: AnthropicRequest, request: Request):
    _check_api_auth(request)
    rate_headers = _check_rate_limit(request)
    proxy_id = _msg_id()
    system_str = req.system
    if isinstance(system_str, list):
        system_str = " ".join(
            b.text for b in system_str if b.type == "text" and b.text
        )

    tools_dict = [t.model_dump() for t in req.tools] if req.tools else None
    prompt = build_anthropic_prompt(
        [m.model_dump() for m in req.messages],
        tools=tools_dict,
        system_str=system_str,
    )

    # Resolve model_type / thinking / search — same rules as the OpenAI endpoint
    # (Anthropic's thinking block is the request-level thinking signal).
    decision = MODEL_ROUTER.route_for(req.model)
    provider = provider_for_model(req.model)
    public_model = _public_model_for_request(req.model, provider)
    upstream_model = _upstream_model_for_request(public_model, provider)
    req_thinking = bool(req.thinking is not None and req.thinking.type == "enabled")
    model_type, thinking, search = resolve_model_controls(
        decision, req_thinking=req_thinking, req_search=False
    )

    tool_names = []
    if req.tools:
        for t in req.tools:
            if t.name:
                tool_names.append(t.name)

    anth_user = _extract_anthropic_user(req, request)
    cache_key = _cache_key(request, anth_user) if provider != PROVIDER_MIMO else None
    if req.stream:
        return await _anthropic_stream(proxy_id, prompt, tool_names,
                                       model_type=model_type, thinking_mode=thinking,
                                       search_enabled=search,
                                       rate_headers=rate_headers,
                                       cache_key=cache_key,
                                       provider=provider,
                                       model_name=public_model,
                                       upstream_model=upstream_model)

    return await run_in_threadpool(_anthropic_nonstream, proxy_id, prompt, tool_names,
                                model_type=model_type, thinking_mode=thinking,
                                search_enabled=search,
                                cache_key=cache_key,
                                provider=provider,
                                model_name=public_model,
                                upstream_model=upstream_model)


def _anthropic_nonstream(msg_id: str, prompt: str, tool_names: list[str],
                         model_type: str | None = None,
                         thinking_mode: bool = False, search_enabled: bool = False,
                         cache_key: str | None = None,
                         provider: str = PROVIDER_DEEPSEEK,
                         model_name: str | None = None,
                         upstream_model: str | None = None):
    response_model = model_name or _model_name()
    acq = _acquire(cache_key=cache_key, provider=provider)
    try:
        eff = acq.prepare_prompt(prompt)
        ds_id = acq.create_session(
            eff,
            upstream_model=upstream_model,
            thinking_enabled=thinking_mode,
            search_enabled=search_enabled,
        )
        t0 = time.time()
        ready_out: dict = {}
        content, thinking = acq.adapter.chat(ds_id, eff, model_type=model_type,
                                              thinking_enabled=thinking_mode, search_enabled=search_enabled,
                                              parent_message_id=acq.parent_message_id,
                                              ready_out=ready_out,
                                              upstream_model=upstream_model)
        if ready_out.get("response_message_id") is not None:
            acq.record_message_id(ready_out["response_message_id"],
                                  ready_out.get("session_id"),
                                  full_prompt=prompt)
        get_stats().record(response_model, (time.time() - t0) * 1000)
    except UpstreamContextLimitError as e:
        get_stats().record(response_model, 0, success=False)
        if cache_key:
            SESSION_CACHE.invalidate(cache_key)
        raise HTTPException(status_code=413, detail=str(e)) from None
    except RateLimitError as e:
        # Match the OpenAI endpoint: upstream throttling is a client-visible
        # 429, not an opaque Anthropic 500. This includes account mute errors
        # because UserMutedError derives from RateLimitError.
        get_stats().record(response_model, 0, success=False)
        if cache_key:
            SESSION_CACHE.invalidate(cache_key)
        raise HTTPException(
            status_code=429,
            detail=f"上游限流：{e.args[0] if e.args else '请求过于频繁'}，请稍后重试",
        )
    except (UpstreamTransientError, RequestException) as e:
        # Upstream outage, not a credential problem: keep the account
        # schedulable and let the caller retry after the advertised delay.
        get_stats().record(response_model, 0, success=False)
        if isinstance(e, RequestException):
            e = UpstreamTransientError("Upstream network connection failed")
        if cache_key:
            SESSION_CACHE.invalidate(cache_key)
        raise HTTPException(
            status_code=503,
            detail=f"上游暂时不可用，请稍后重试（{str(e)[:200]}）",
            headers={"Retry-After": TRANSIENT_RETRY_AFTER_SECS},
        )
    except UpstreamHintError as e:
        get_stats().record(response_model, 0, success=False)
        if _is_context_limit_error(e):
            if cache_key:
                SESSION_CACHE.invalidate(cache_key)
            raise HTTPException(status_code=413, detail=str(e)) from None
        pool.mark_error(acq.acct, str(e))
        raise HTTPException(status_code=502, detail=str(e))
    except UpstreamEmptyError:
        # Empty upstream bodies are transient and should not poison the
        # account pool, but the client still needs a meaningful 502.
        get_stats().record(response_model, 0, success=False)
        if cache_key:
            SESSION_CACHE.invalidate(cache_key)
        raise HTTPException(status_code=502, detail="上游返回空响应（可能触发限流），请稍后重试")
    except (
        ZaiAuthError,
        KimiAuthError,
        QwenAuthError,
        HunyuanAuthError,
        WenxinAuthError,
        DoubaoAuthError,
        MimoAuthError,
    ) as e:
        get_stats().record(response_model, 0, success=False)
        message = MIMO_AUTH_MESSAGE if isinstance(e, MimoAuthError) else str(e)
        pool.mark_error(acq.acct, message)
        raise HTTPException(status_code=401, detail=message)
    except Exception as e:
        get_stats().record(response_model, 0, success=False)
        pool.mark_error(acq.acct, str(e))
        raise
    finally:
        acq.release()

    tool_calls, cleaned = parse_dsml_tool_calls(content, tool_names)
    return build_nonstream_response(
        msg_id, response_model,
        content_text=cleaned or content,
        tool_calls=tool_calls,
        thinking_text=thinking,
    )


async def _anthropic_stream(msg_id: str, prompt: str, tool_names: list[str],
                            model_type: str | None = None,
                            thinking_mode: bool = False, search_enabled: bool = False,
                            rate_headers: dict | None = None,
                            cache_key: str | None = None,
                            provider: str = PROVIDER_DEEPSEEK,
                            model_name: str | None = None,
                            upstream_model: str | None = None):
    response_model = model_name or _model_name()
    acq, eff, ds_id = await _prepare_stream_account(
        prompt, cache_key, provider, response_model, upstream_model,
        thinking_mode, search_enabled,
    )
    t0 = time.time()

    def event_stream():
        nonlocal t0
        ready_out: dict = {}
        upstream_tokens = None
        try:
            upstream_tokens = acq.adapter.chat_stream(
                ds_id, eff, model_type=model_type,
                thinking_enabled=thinking_mode, search_enabled=search_enabled,
                parent_message_id=acq.parent_message_id, ready_out=ready_out,
                upstream_model=upstream_model,
            )
            for event in stream_response(
                msg_id, response_model,
                upstream_tokens,
                tool_names,
                thinking_mode=thinking_mode,
            ):
                yield event
            if ready_out.get("response_message_id") is not None:
                acq.record_message_id(ready_out["response_message_id"],
                                      ready_out.get("session_id"),
                                      full_prompt=prompt)
            get_stats().record(response_model, (time.time() - t0) * 1000)
        except MimoAuthError:
            get_stats().record(response_model, (time.time() - t0) * 1000, success=False)
            pool.mark_error(acq.acct, MIMO_AUTH_MESSAGE)
            error = {"type": "error", "error": {
                "type": "authentication_error", "message": MIMO_AUTH_MESSAGE,
            }}
            yield f"event: error\ndata: {json.dumps(error, ensure_ascii=False)}\n\n"
        except Exception as e:
            if isinstance(e, RequestException):
                e = UpstreamTransientError("Upstream network connection failed")
            get_stats().record(response_model, (time.time() - t0) * 1000, success=False)
            if isinstance(e, (RateLimitError, UpstreamEmptyError, UpstreamTransientError)):
                if cache_key:
                    SESSION_CACHE.invalidate(cache_key)
            else:
                pool.mark_error(acq.acct, str(e))
            error = {"type": "error", "error": {
                "type": "overloaded_error" if isinstance(e, UpstreamTransientError) else "api_error",
                "message": str(e)[:500],
            }}
            yield f"event: error\ndata: {json.dumps(error, ensure_ascii=False)}\n\n"
        finally:
            if upstream_tokens is not None:
                try:
                    upstream_tokens.close()
                except Exception:
                    log.warning("upstream_stream_close_failed", extra={"account_id": acq.acct.id})
            acq.release()

    stream = HeartbeatStream(event_stream(), on_close=acq.release,
                             heartbeat='event: ping\ndata: {"type":"ping"}\n\n')
    return ReliableStreamingResponse(
        stream,
        media_type="text/event-stream",
        background=BackgroundTask(stream.aclose),
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
            **(rate_headers or {}),
        },
    )


# ---- OpenAI handlers ----


def _handle_nonstream(proxy_id: str, prompt: str, tools: list[ToolDef] | None = None,
                      model_type: str | None = None,
                      thinking_mode: bool = False, search_enabled: bool = False,
                      cache_key: str | None = None,
                      provider: str = PROVIDER_DEEPSEEK,
                      model_name: str | None = None,
                      upstream_model: str | None = None):
    """Non-streaming completion with tool call detection."""
    response_model = model_name or _model_name()
    prompt_tokens = count_text(prompt)
    acq = _acquire(cache_key=cache_key, provider=provider)
    try:
        eff = acq.prepare_prompt(prompt)
        ds_id = acq.create_session(
            eff,
            upstream_model=upstream_model,
            thinking_enabled=thinking_mode,
            search_enabled=search_enabled,
        )
        t0 = time.time()
        ready_out: dict = {}
        content, thinking = acq.adapter.chat(ds_id, eff, model_type=model_type,
                                              thinking_enabled=thinking_mode, search_enabled=search_enabled,
                                              parent_message_id=acq.parent_message_id,
                                              ready_out=ready_out,
                                              upstream_model=upstream_model)
        if ready_out.get("response_message_id") is not None:
            acq.record_message_id(ready_out["response_message_id"],
                                  ready_out.get("session_id"),
                                  full_prompt=prompt)
        completion_tokens = count_text(content) + count_text(thinking)
        get_stats().record(response_model, (time.time() - t0) * 1000,
                           prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    except UpstreamContextLimitError as e:
        get_stats().record(response_model, 0, success=False)
        if cache_key:
            SESSION_CACHE.invalidate(cache_key)
        raise HTTPException(status_code=413, detail=str(e)) from None
    except RateLimitError as e:
        # Rate limiting is a transient upstream state, not a credential
        # problem — do NOT mark the account as errored, or the pool would
        # drain to 503 once the limiter cools down. Invalidate the cached
        # session so the next call starts fresh.
        get_stats().record(response_model, 0, success=False)
        if cache_key:
            SESSION_CACHE.invalidate(cache_key)
        raise HTTPException(status_code=429, detail=f"上游限流：{e.args[0] if e.args else '请求过于频繁'}，请稍后重试")
    except (UpstreamTransientError, RequestException) as e:
        # Same reasoning as rate limiting: a transient upstream outage must
        # not drain the pool. 503 + Retry-After lets a parent gateway fail
        # over or retry instead of treating it as a hard error.
        get_stats().record(response_model, 0, success=False)
        if isinstance(e, RequestException):
            e = UpstreamTransientError("Upstream network connection failed")
        if cache_key:
            SESSION_CACHE.invalidate(cache_key)
        raise HTTPException(
            status_code=503,
            detail=f"上游暂时不可用，请稍后重试（{str(e)[:200]}）",
            headers={"Retry-After": TRANSIENT_RETRY_AFTER_SECS},
        )
    except UpstreamHintError as e:
        get_stats().record(response_model, 0, success=False)
        if _is_context_limit_error(e):
            if cache_key:
                SESSION_CACHE.invalidate(cache_key)
            raise HTTPException(status_code=413, detail=str(e)) from None
        pool.mark_error(acq.acct, str(e))
        raise HTTPException(status_code=502, detail=str(e))
    except UpstreamEmptyError as e:
        # Transient empty responses (stale pooled connection / upstream
        # hiccup) — retried in the adapter already; do not poison the pool.
        get_stats().record(response_model, 0, success=False)
        if cache_key:
            SESSION_CACHE.invalidate(cache_key)
        raise HTTPException(status_code=502, detail="上游返回空响应（可能触发限流），请稍后重试")
    except (
        ZaiAuthError,
        KimiAuthError,
        QwenAuthError,
        HunyuanAuthError,
        WenxinAuthError,
        DoubaoAuthError,
        MimoAuthError,
    ) as e:
        get_stats().record(response_model, 0, success=False)
        message = MIMO_AUTH_MESSAGE if isinstance(e, MimoAuthError) else str(e)
        pool.mark_error(acq.acct, message)
        raise HTTPException(status_code=401, detail=message)
    except Exception as e:
        get_stats().record(response_model, 0, success=False)
        pool.mark_error(acq.acct, str(e))
        raise
    finally:
        acq.release()

    tool_names = _get_tool_names(tools)
    tool_calls, cleaned = _parse_response_for_tools(content, tool_names)
    total = prompt_tokens + count_text(cleaned or content)

    if tool_calls:
        return {
            "id": proxy_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": response_model,
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": tool_calls,
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": count_text(cleaned),
                "total_tokens": total,
            },
        }

    # Mirror the streaming path: when thinking was on and the model produced a
    # reasoning chain, expose it as `reasoning_content` (the same non-standard
    # field DeepSeek's own API and the streaming branch use). It used to be
    # collected and then dropped here, so `thinking_mode=true` + `stream=false`
    # silently lost the chain while `stream=true` emitted it.
    message: dict = {
        "role": "assistant",
        "content": cleaned or content,
    }
    if thinking:
        message["reasoning_content"] = thinking

    return {
        "id": proxy_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": response_model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": count_text(cleaned or content),
            "total_tokens": total,
        },
    }


@app.post("/v1/responses")
@app.post("/responses")
@app.post("/v1/responses/compact")
@app.post("/responses/compact")
@app.post("/v1/responses/compact/detail")
@app.post("/responses/compact/detail")
async def responses(req: ResponsesRequest, request: Request):
    """Minimal OpenAI Responses API compatibility over the Chat Completions core."""
    _check_api_auth(request)
    rate_headers = _check_rate_limit(request)

    codex_request = _is_codex_request(request, req)
    tool_adapter = ResponsesToolAdapter(
        req.tools,
        codex_mode=codex_request,
        tool_mode=_codex_tool_mode() if codex_request else "all",
        allowed_tools=_codex_allowed_tools() if codex_request else None,
    )
    raw_messages = input_to_chat_messages(
        req.input,
        req.instructions,
        tool_adapter=tool_adapter,
    )
    force_compaction = "/responses/compact" in request.url.path
    compaction_request = force_compaction or has_compaction_trigger(req.input)
    if not raw_messages and not compaction_request:
        raise HTTPException(status_code=400, detail="Responses input must contain at least one message")
    try:
        messages = [ChatMessage(**message) for message in raw_messages]
        tools = [ToolDef(**tool) for tool in tool_adapter.chat_tools]
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid Responses payload: {exc}")

    decision = MODEL_ROUTER.route_for(req.model)
    provider = provider_for_model(req.model)
    public_model = _public_model_for_request(req.model, provider)
    upstream_model = _upstream_model_for_request(public_model, provider)
    if (
        provider == PROVIDER_MIMO
        and codex_request
        and tools
        and upstream_model.lower() in FASTCHAT_MODELS
        and ULTRASPEED_FALLBACK_MODEL
    ):
        log.info(
            "mimo_codex_tool_fallback",
            extra={
                "requested_model": upstream_model,
                "upstream_model": ULTRASPEED_FALLBACK_MODEL,
            },
        )
        upstream_model = ULTRASPEED_FALLBACK_MODEL
    model_type, thinking, search = resolve_model_controls(
        decision,
        req_thinking=requested_thinking(req.reasoning),
        req_search=(requested_search(req.tools) and (
            not codex_request or _env_truthy("CODEX_WEB_SEARCH_ENABLED", False)
        )),
    )
    adapted_tool_choice = tool_adapter.adapt_tool_choice(req.tool_choice)
    if codex_request or tools:
        messages, prompt, prompt_metrics = _compact_codex_messages(
            messages,
            tools or None,
            adapted_tool_choice,
            provider=provider,
            codex_mode=codex_request,
        )
        if prompt_metrics["dropped_messages"] or (
            prompt_metrics["compacted_tokens"]
            != prompt_metrics["original_tokens"]
        ):
            log.info(
                "codex_prompt_compacted",
                extra={
                    "provider": provider,
                    "original_tokens": prompt_metrics["original_tokens"],
                    "compacted_tokens": prompt_metrics["compacted_tokens"],
                    "dropped_messages": prompt_metrics["dropped_messages"],
                },
            )
    else:
        prompt = _build_prompt(
            messages,
            tools or None,
            adapted_tool_choice,
        )

    conversation_id = request.headers.get("X-Conversation-Id", "").strip()
    if conversation_id:
        conversation_id = f"header:{conversation_id}"
    else:
        conversation_id = SessionCache.derive_conversation_id(req.user, None)
    cache_key = _cache_key(request, conversation_id) if provider != PROVIDER_MIMO else None
    response_id = f"resp_{uuid.uuid4().hex}"
    request_data = req.model_dump()

    if compaction_request:
        async def summarize_compaction() -> tuple[str, dict[str, Any]]:
            summary = compaction_fallback_summary(raw_messages)
            chat_payload: dict[str, Any] = {
                "model": public_model,
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
            }
            try:
                candidate = await run_in_threadpool(
                    _handle_nonstream,
                    response_id,
                    compaction_prompt(
                        raw_messages,
                        max_chars=(
                            MIMO_COMPACTION_PROMPT_CHARS
                            if provider == PROVIDER_MIMO
                            else 48_000
                        ),
                    ),
                    None,
                    model_type=model_type,
                    thinking_mode=False,
                    search_enabled=False,
                    cache_key=None,
                    provider=provider,
                    model_name=public_model,
                    upstream_model=upstream_model,
                )
                choice = (candidate.get("choices") or [{}])[0]
                generated = str(
                    (choice.get("message") or {}).get("content") or ""
                ).strip()
                if generated:
                    summary = generated
                    chat_payload = candidate
            except Exception as exc:
                log.warning(
                    "compaction_summary_fallback",
                    extra={"provider": provider, "error_type": type(exc).__name__},
                )
            return summary, chat_payload

        if not req.stream:
            summary, chat_payload = await summarize_compaction()
            return JSONResponse(
                build_responses_compaction(
                    chat_payload,
                    response_id,
                    request_data,
                    summary,
                ),
                headers=rate_headers,
            )

        translator = ResponsesStreamTranslator(
            response_id,
            public_model,
            request_data,
            tool_adapter=tool_adapter,
        )

        async def compact_stream():
            for event in translator.initial_events():
                yield event
            summary_task = asyncio.create_task(summarize_compaction())
            try:
                while not summary_task.done():
                    done, _ = await asyncio.wait(
                        {summary_task},
                        timeout=15,
                    )
                    if not done:
                        yield ": keep-alive\n\n"
                summary, chat_payload = await summary_task
            finally:
                if not summary_task.done():
                    summary_task.cancel()
            for event in translator.compaction_events(
                summary,
                chat_payload.get("usage"),
            ):
                yield event

        return ReliableStreamingResponse(
            compact_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
                **rate_headers,
            },
        )

    if not req.stream:
        chat_payload = await run_in_threadpool(_handle_nonstream,
            response_id,
            prompt,
            tools or None,
            model_type=model_type,
            thinking_mode=thinking,
            search_enabled=search,
            cache_key=cache_key,
            provider=provider,
            model_name=public_model,
            upstream_model=upstream_model,
        )
        return JSONResponse(
            build_responses_nonstream(
                chat_payload,
                response_id,
                request_data,
                tool_adapter=tool_adapter,
            ),
            headers=rate_headers,
        )

    chat_response = await _handle_stream(
        response_id,
        prompt,
        tools or None,
        model_type=model_type,
        thinking_mode=thinking,
        search_enabled=search,
        cache_key=cache_key,
        include_usage=True,
        provider=provider,
        model_name=public_model,
        upstream_model=upstream_model,
    )
    translator = ResponsesStreamTranslator(
        response_id,
        public_model,
        request_data,
        tool_adapter=tool_adapter,
    )

    def upstream_error_event(error: dict) -> str:
        message = str(error.get("message") or "Upstream error")
        code = str(error.get("code") or "upstream_error")
        # Codex handles response.failed, while other consumers handle error.
        # Neither is a successful terminal response.
        return translator.error_event(message, code) + translator.failed_event(message, code)

    async def translated_events():
        for event in translator.initial_events():
            yield event

        buffer = ""
        decoder = codecs.getincrementaldecoder("utf-8")()
        finished = False
        try:
            async for raw_chunk in chat_response.body_iterator:
                text = decoder.decode(raw_chunk) if isinstance(raw_chunk, bytes) else str(raw_chunk)
                buffer = (buffer + text).replace("\r\n", "\n")
                while "\n\n" in buffer:
                    event_block, buffer = buffer.split("\n\n", 1)
                    # Heartbeats carry no model text or usage. Do not drop them
                    # while the upstream is silent or the tool sieve buffers.
                    if any(line.startswith(":") for line in event_block.splitlines()):
                        yield translator.progress_event()
                    for payload in parse_chat_sse_event(event_block):
                        if payload == "[DONE]":
                            if not finished:
                                raise UpstreamTransientError("Upstream stream ended without a finish reason")
                            for event in translator.final_events():
                                yield event
                            return
                        chunk = json.loads(payload)
                        if not isinstance(chunk, dict):
                            raise UpstreamTransientError("Upstream returned an invalid stream event")
                        if isinstance(chunk.get("error"), dict):
                            yield upstream_error_event(chunk["error"])
                            return
                        choices = chunk.get("choices") or []
                        reason = choices[0].get("finish_reason") if choices else None
                        if reason == "error":
                            raise UpstreamTransientError("Upstream stream failed before completion")
                        finished = finished or reason is not None
                        for event in translator.feed_chat_chunk(chunk):
                            yield event
            raise UpstreamTransientError("Upstream stream ended before its [DONE] event")
        except Exception as exc:
            log.warning("responses_stream_failed", extra={"provider": provider, "error_type": type(exc).__name__})
            message = str(exc) if isinstance(exc, UpstreamTransientError) else "Upstream stream was interrupted before completion"
            yield upstream_error_event({"message": message, "code": "upstream_temporarily_unavailable"})

    async def event_stream():
        events = translated_events()
        try:
            async for event in events:
                yield event
        finally:
            await events.aclose()
            try:
                await chat_response.body_iterator.aclose()
            finally:
                # Also release accounts if cancelled during initial events,
                # before the underlying chat generator has ever started.
                if chat_response.background is not None:
                    await chat_response.background()

    return ReliableStreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        background=chat_response.background,
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
            **rate_headers,
        },
    )


async def _prepare_stream_account(prompt, cache_key, provider, response_model,
                                  upstream_model, thinking_mode, search_enabled,
                                  prompt_tokens=0):
    def prepare():
        acq = _acquire(cache_key=cache_key, provider=provider)
        try:
            eff = acq.prepare_prompt(prompt)
            ds_id = acq.create_session(
                eff, upstream_model=upstream_model,
                thinking_enabled=thinking_mode, search_enabled=search_enabled,
            )
            return acq, eff, ds_id
        except Exception as exc:
            try:
                get_stats().record(response_model, 0, success=False, prompt_tokens=prompt_tokens)
                if _is_context_limit_error(exc):
                    if cache_key:
                        SESSION_CACHE.invalidate(cache_key)
                    raise HTTPException(status_code=413, detail=str(exc)) from None
                if isinstance(exc, (RequestException, UpstreamTransientError, UpstreamEmptyError, RateLimitError)):
                    if cache_key:
                        SESSION_CACHE.invalidate(cache_key)
                    status = 429 if isinstance(exc, RateLimitError) else 503
                    raise HTTPException(status_code=status, detail="Upstream temporarily unavailable",
                                        headers={"Retry-After": TRANSIENT_RETRY_AFTER_SECS}) from None
                message = MIMO_AUTH_MESSAGE if isinstance(exc, MimoAuthError) else str(exc)
                pool.mark_error(acq.acct, message)
                if isinstance(exc, (ZaiAuthError, KimiAuthError, QwenAuthError,
                                    HunyuanAuthError, WenxinAuthError, DoubaoAuthError, MimoAuthError)):
                    raise HTTPException(status_code=401, detail=message) from None
                raise
            finally:
                acq.release()
    return await run_owned_setup(prepare, lambda result: result[0].release())


async def _handle_stream(proxy_id: str, prompt: str, tools: list[ToolDef] | None = None,
                         model_type: str | None = None,
                         thinking_mode: bool = False, search_enabled: bool = False,
                         rate_headers: dict | None = None,
                         cache_key: str | None = None,
                         include_usage: bool = False,
                         provider: str = PROVIDER_DEEPSEEK,
                         model_name: str | None = None,
                         upstream_model: str | None = None):
    """Streaming completion with StreamSieve tool call detection and thinking support."""
    response_model = model_name or _model_name()
    prompt_tokens = count_text(prompt)
    acq, eff, ds_id = await _prepare_stream_account(
        prompt, cache_key, provider, response_model, upstream_model,
        thinking_mode, search_enabled, prompt_tokens,
    )
    tool_names = _get_tool_names(tools)
    t0 = time.time()

    def _chunk(*args, **kwargs):
        return _openai_chunk(*args, model_name=response_model, **kwargs)

    def _usage_chunk(*args, **kwargs):
        return _openai_usage_chunk(*args, model_name=response_model, **kwargs)

    def _tool_chunks(*args, **kwargs):
        return _emit_tool_calls_chunks(*args, model_name=response_model, **kwargs)

    def event_stream():
        nonlocal t0
        completion_parts: list[str] = []
        upstream_tokens = None
        # Multi-turn: filled by adapter with the upstream response message id
        # (and the actual session id after a retry-swap). Recorded into the
        # session cache on success so the next turn has a valid parent.
        ready_out: dict = {}

        def _usage_snapshot(completion_text: str) -> dict:
            completion_tokens = count_text(completion_text)
            return {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            }

        try:
            yield _chunk(proxy_id, finish=False)
            role_sent = False

            def _parse_fn(text):
                return parse_dsml_tool_calls(text, tool_names)

            sieve = StreamSieve(parse_fn=_parse_fn)
            full_buf = ""

            def _record_turn():
                # Persist the upstream response message id (and the session
                # actually used, e.g. after an internal retry swap) so the
                # next turn's parent_message_id is valid.
                if ready_out.get("response_message_id") is not None:
                    acq.record_message_id(ready_out["response_message_id"],
                                          ready_out.get("session_id"),
                                          full_prompt=prompt)

            upstream_tokens = acq.adapter.chat_stream(
                ds_id, eff, model_type=model_type,
                thinking_enabled=thinking_mode, search_enabled=search_enabled,
                parent_message_id=acq.parent_message_id, ready_out=ready_out,
                upstream_model=upstream_model,
            )
            for token in upstream_tokens:
                if isinstance(token, dict):
                    tt = token.get("__type")
                    if tt == "status":
                        if token["status"] == "FINISHED":
                            break
                        continue
                    elif tt == "thinking":
                        content = token.get("content", "")
                        if content:
                            completion_parts.append(content)
                            if not role_sent:
                                yield _chunk(proxy_id, reasoning_content="", role="assistant")
                                role_sent = True
                            yield _chunk(proxy_id, reasoning_content=content)
                        continue

                # Normal text token (str) — feed to sieve
                full_buf += token
                for evt in sieve.feed(token):
                    if evt.type == "text":
                        if evt.data:
                            completion_parts.append(evt.data)
                            if not role_sent:
                                if thinking_mode:
                                    yield _chunk(proxy_id, reasoning_content="")
                                yield _chunk(proxy_id, content=evt.data, role="assistant")
                                role_sent = True
                            else:
                                yield _chunk(proxy_id, content=evt.data)
                    elif evt.type == "tool_calls":
                        usage = _usage_snapshot(full_buf)
                        for chunk in _tool_chunks(evt.data, proxy_id, usage=usage):
                            yield chunk
                        if include_usage:
                            yield _usage_chunk(proxy_id, usage)
                        _record_turn()
                        get_stats().record(
                            response_model,
                            (time.time() - t0) * 1000,
                            prompt_tokens=prompt_tokens,
                            completion_tokens=usage["completion_tokens"],
                        )
                        yield "data: [DONE]\n\n"
                        return

            # Flush sieve
            had_tool = False
            try:
                flushed_events = sieve.flush(
                    reject_incomplete_tool_call=bool(tool_names)
                )
            except IncompleteToolCallError:
                raise UpstreamTransientError(
                    "模型返回了不完整的工具调用，请重试"
                ) from None
            for evt in flushed_events:
                if evt.type == "text" and evt.data:
                    completion_parts.append(evt.data)
                    if not role_sent:
                        if thinking_mode:
                            yield _chunk(proxy_id, reasoning_content="")
                        yield _chunk(proxy_id, content=evt.data, role="assistant")
                        role_sent = True
                    else:
                        yield _chunk(proxy_id, content=evt.data)
                elif evt.type == "tool_calls":
                    had_tool = True
                    for chunk in _tool_chunks(evt.data, proxy_id):
                        yield chunk

            if had_tool:
                usage = _usage_snapshot(full_buf)
                if include_usage:
                    yield _usage_chunk(proxy_id, usage)
                _record_turn()
                get_stats().record(
                    response_model,
                    (time.time() - t0) * 1000,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=usage["completion_tokens"],
                )
                yield "data: [DONE]\n\n"
                return

            # Fallback: parse full buffer
            if not had_tool and full_buf:
                tc_result, _ = parse_dsml_tool_calls(full_buf, tool_names)
                if tc_result:
                    if not role_sent:
                        if thinking_mode:
                            yield _chunk(proxy_id, reasoning_content="")
                        role_sent = True
                    for chunk in _tool_chunks(tc_result, proxy_id):
                        yield chunk
                    usage = _usage_snapshot(full_buf)
                    if include_usage:
                        yield _usage_chunk(proxy_id, usage)
                    _record_turn()
                    get_stats().record(
                        response_model,
                        (time.time() - t0) * 1000,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=usage["completion_tokens"],
                    )
                    yield "data: [DONE]\n\n"
                    return

            _record_turn()
            usage = _usage_snapshot("".join(completion_parts))
            yield _chunk(proxy_id, finish=True, usage=usage)
            if include_usage:
                yield _usage_chunk(proxy_id, usage)
            get_stats().record(response_model, (time.time() - t0) * 1000,
                               prompt_tokens=prompt_tokens,
                               completion_tokens=usage["completion_tokens"])
            yield "data: [DONE]\n\n"
        except Exception as e:
            if isinstance(e, RequestException):
                e = UpstreamTransientError("Upstream network connection failed")
            get_stats().record(response_model, (time.time() - t0) * 1000, success=False,
                               prompt_tokens=prompt_tokens)
            message = MIMO_AUTH_MESSAGE if isinstance(e, MimoAuthError) else str(e)
            # Rate limit / transient empty responses are not credential
            # failures — keep the account usable instead of draining the pool.
            context_limit = _is_context_limit_error(e)
            if isinstance(e, (
                UpstreamContextLimitError,
                RateLimitError,
                UpstreamEmptyError,
                UpstreamTransientError,
            )) or context_limit:
                if cache_key:
                    SESSION_CACHE.invalidate(cache_key)
            else:
                pool.mark_error(acq.acct, message)
            if isinstance(e, MimoAuthError):
                log.warning("upstream_auth_failed", extra={"provider": provider})
            elif isinstance(e, (UpstreamTransientError, RateLimitError, UpstreamEmptyError)):
                log.warning("openai_stream_interrupted", extra={
                    "provider": provider, "account_id": acq.acct.id,
                    "error_type": type(e).__name__, "response_id": proxy_id,
                })
            else:
                log.exception("openai_stream_failed")
            # Emit a uniform error frame to the client, then [DONE] so the
            # client SDK doesn't see a truncated stream.
            if isinstance(e, RateLimitError):
                err_type, err_code, retry_after = "rate_limit_error", "rate_limit_exceeded", TRANSIENT_RETRY_AFTER_SECS
            elif isinstance(
                e,
                (
                    ZaiAuthError,
                    KimiAuthError,
                    QwenAuthError,
                    HunyuanAuthError,
                    WenxinAuthError,
                    DoubaoAuthError,
                    MimoAuthError,
                ),
            ):
                err_type, err_code, retry_after = "authentication_error", "invalid_api_key", None
            elif context_limit:
                err_type, err_code, retry_after = "invalid_request_error", "context_length_exceeded", None
            elif isinstance(e, UpstreamTransientError):
                err_type, err_code, retry_after = "api_error", "upstream_temporarily_unavailable", TRANSIENT_RETRY_AFTER_SECS
            elif isinstance(e, UpstreamEmptyError):
                err_type, err_code, retry_after = "api_error", "upstream_empty_response", TRANSIENT_RETRY_AFTER_SECS
            else:
                err_type, err_code, retry_after = "upstream_error", "upstream_error", None
            err = {
                "id": proxy_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": response_model,
                "choices": [{
                    "index": 0,
                    "delta": {},
                    "finish_reason": "error",
                }],
                "error": {
                    "type": err_type,
                    "code": err_code,
                    "message": message[:500],
                },
            }
            if retry_after:
                err["error"]["retry_after"] = int(retry_after)
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        finally:
            if upstream_tokens is not None:
                try:
                    upstream_tokens.close()
                except Exception:
                    log.warning("upstream_stream_close_failed", extra={"account_id": acq.acct.id})
            acq.release()

    stream = HeartbeatStream(event_stream(), on_close=acq.release)
    return ReliableStreamingResponse(
        stream,
        media_type="text/event-stream",
        background=BackgroundTask(stream.aclose),
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
            **(rate_headers or {}),
        },
    )


# ── Admin & static file serving ──────────────────────────────

# v3 webui lives under webui-new/dist/ (built by `npm run build` /
# `scripts/build_webui.sh`). The old v2.2.0 webui/ directory is removed
# in v3.0.0; for development convenience we still fall back to it if
# the new dist/ hasn't been built yet.
_WEBUI_DIST = _os.path.realpath(
    _os.path.join(_os.path.dirname(__file__), "webui-new", "dist")
)
_WEBUI_LEGACY = _os.path.realpath(
    _os.path.join(_os.path.dirname(__file__), "webui")
)
# Always register the /webui routes even if the build hasn't run yet —
# the handlers will return a friendly JSON error pointing the operator
# at `npm run build`. This keeps the routes in the OpenAPI schema so
# the smoke test (and downstream tooling) can find them.
_WEBUI_DIR = (
    _WEBUI_DIST if _os.path.isdir(_WEBUI_DIST)
    else _WEBUI_LEGACY if _os.path.isdir(_WEBUI_LEGACY)
    else _WEBUI_DIST  # fall through to the canonical v3 path
)

app.include_router(admin_router)


@app.get("/browser-login/view")
async def browser_login_view(request: Request, token: str = ""):
    """Set the short-lived view cookie, then redirect into the noVNC proxy.

    The token is never placed in an iframe URL that stays in browser history;
    it is exchanged once for an HttpOnly, SameSite=Strict cookie.
    """
    if not await browser_login_client.verify_view_token(token):
        raise HTTPException(status_code=401, detail="Invalid or expired view token")
    response = JSONResponse({"ok": True})
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    secure_cookie = request.url.scheme == "https" or forwarded_proto == "https"
    response.set_cookie(
        "browser_login_view",
        token,
        max_age=7200,
        httponly=True,
        secure=secure_cookie,
        samesite="strict",
        path="/browser-login",
    )
    return response


@app.get("/browser-login/auth")
async def browser_login_view_auth(request: Request):
    """Validate a one-time noVNC view token before Nginx upgrades WebSocket."""
    supplied = (
        request.headers.get("X-View-Token", "")
        or request.cookies.get("browser_login_view", "")
    )
    if not await browser_login_client.verify_view_token(supplied):
        raise HTTPException(status_code=401, detail="Invalid or expired view token")
    return Response(status_code=204)


def _safe_webui_path(rest_of_path: str) -> str | None:
    """Resolve a path inside the webui dir, refusing traversal.

    Returns the absolute file path if it points to a file under _WEBUI_DIR,
    otherwise None. Defends against `..\\..\\.env` style requests that would
    otherwise let unauthenticated callers read project secrets.
    """
    if not rest_of_path:
        return None
    # Reject obviously hostile inputs early.
    if "\x00" in rest_of_path:
        return None
    candidate = _os.path.realpath(_os.path.join(_WEBUI_DIR, rest_of_path))
    try:
        common = _os.path.commonpath([candidate, _WEBUI_DIR])
    except ValueError:
        # Different drives on Windows raise ValueError.
        return None
    if common != _WEBUI_DIR:
        return None
    if not _os.path.isfile(candidate):
        return None
    return candidate


def _webui_index_path() -> str | None:
    index = _os.path.join(_WEBUI_DIR, "index.html")
    return index if _os.path.isfile(index) else None


if _os.path.isdir(_WEBUI_DIR) or True:  # always register so /webui is in the OpenAPI schema
    @app.get("/webui/{rest_of_path:path}")
    async def webui_spa(rest_of_path: str):
        safe = _safe_webui_path(rest_of_path)
        if safe is not None:
            return FileResponse(safe)
        # SPA fallback: serve index.html so react-router-dom can take over.
        index = _webui_index_path()
        if index is not None:
            return FileResponse(index)
        return {"error": "webui not built — run `npm run build` in webui-new/"}

    @app.get("/webui")
    async def webui_root():
        index = _webui_index_path()
        if index is not None:
            return FileResponse(index)
        return {"error": "webui not built — run `npm run build` in webui-new/"}

if __name__ == "__main__":
    _validate_startup()
    uvicorn.run("server:app", host=HOST, port=PORT, reload=False)
