"""Browser-assisted DeepSeek login controller.

The browser runs inside a container with a virtual X display.  noVNC exposes
that display to the operator, while this small API controls the Playwright
session and imports the resulting credentials into DS2API.

The browser process is intentionally *not* exposed directly.  Nginx protects
the noVNC page and WebSocket with a short-lived view token issued by this
service.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import shutil
import time
import urllib.error
import urllib.request
from contextlib import suppress
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel
from fastapi.responses import JSONResponse

try:
    from playwright.async_api import BrowserContext, Page, async_playwright
except ImportError:  # pragma: no cover - only happens in a broken image
    BrowserContext = Page = Any  # type: ignore[assignment,misc]
    async_playwright = None  # type: ignore[assignment]


SIGN_IN_URL = os.environ.get(
    "BROWSER_LOGIN_SIGN_IN_URL",
    "https://chat.deepseek.com/sign_in",
)
CHROMIUM_PATH = os.environ.get("BROWSER_LOGIN_CHROMIUM", "")
PROFILE_DIR = Path(os.environ.get("BROWSER_LOGIN_PROFILE_DIR", "/data/browser-profile"))
SHARED_SECRET = os.environ.get("BROWSER_LOGIN_SHARED_SECRET", "")
DS2API_INGEST_URL = os.environ.get(
    "DS2API_INGEST_URL",
    "http://dsapi:8080/admin/api/browser-login/ingest",
)
VIEW_TTL = max(300, int(os.environ.get("BROWSER_LOGIN_VIEW_TTL", "7200") or 7200))
POLL_INTERVAL = max(1.0, float(os.environ.get("BROWSER_LOGIN_POLL_INTERVAL", "2")))
USER_AGENT = os.environ.get(
    "BROWSER_LOGIN_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36",
)

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || {runtime: {}};
"""


class StartRequest(BaseModel):
    reset_profile: bool = True


class LoginState:
    def __init__(self) -> None:
        self.status = "idle"
        self.message = "尚未启动浏览器"
        self.view_token = ""
        self.view_expires_at = 0.0
        self.started_at = 0.0
        self.completed_at = 0.0
        self.account_id = ""
        self.last_token = ""
        self.task: asyncio.Task | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None


state = LoginState()
lock = asyncio.Lock()
app = FastAPI(title="DS2API Browser Login", version="1.0.0")


def _secret_ok(value: str | None) -> bool:
    if not SHARED_SECRET or not value:
        return False
    return secrets.compare_digest(value, SHARED_SECRET)


def _view_ok(value: str | None) -> bool:
    if not value or not state.view_token:
        return False
    if time.time() >= state.view_expires_at:
        return False
    return secrets.compare_digest(value, state.view_token)


def issue_view_token() -> tuple[str, float]:
    """Return ``<exp>.<hmac_sha1_hex>`` plus its expiry.

    Nginx verifies the same HMAC locally, so no auth subrequest is needed
    (which matters because ngx.location.capture does not support HTTP/2).
    """
    exp = int(time.time()) + VIEW_TTL
    payload = str(exp)
    mac = hmac.new(
        SHARED_SECRET.encode("utf-8"),
        payload.encode("ascii"),
        hashlib.sha1,
    ).hexdigest()
    return f"{payload}.{mac}", float(exp)


def _public_state() -> dict[str, Any]:
    return {
        "status": state.status,
        "message": state.message,
        "started_at": int(state.started_at),
        "completed_at": int(state.completed_at),
        "account_id": state.account_id,
    }


async def _wait_for_login(context: BrowserContext, page: Page) -> None:
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        if page.is_closed():
            raise RuntimeError("浏览器窗口已关闭")
        token, cookies, device_id = await _read_browser_credentials(context, page)
        if not _valid_token(token):
            continue
        # The SPA briefly stores an empty {"value":"","__version":"0"}
        # wrapper before login; `_valid_token` rejects that and any malformed
        # value. Do not call the upstream here: a WAF challenge would make a
        # perfectly good login look invalid.
        if device_id and token != state.last_token:
            state.last_token = token
            state.message = (
                f"已检测到登录 token（设备 {device_id[:8]}…），正在导入"
            )

        state.status = "importing"
        state.message = "已检测到登录，正在导入凭据"
        account_id = await asyncio.to_thread(_post_ingest, token, cookies, device_id)
        state.account_id = account_id
        state.status = "completed"
        state.message = "凭据已导入，浏览器即将关闭"
        state.completed_at = time.time()
        await asyncio.sleep(2)
        await context.close()
        state.context = None
        state.page = None
        state.message = "凭据已导入，可以返回账号池"
        return


def _normalize_token(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    value = raw.strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    if value.startswith("{"):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            for key in ("value", "token"):
                if key in parsed:
                    inner = parsed.get(key)
                    value = inner.strip() if isinstance(inner, str) else ""
                    break
    return value


def _valid_token(value: str) -> bool:
    """Reject the empty ``{"value":"","__version":"0"}`` wrapper and junk."""
    if len(value) < 20 or len(value) > 4096:
        return False
    if any(ch.isspace() for ch in value):
        return False
    return not value.startswith("{")


def _serialize_cookies(cookies: list[dict[str, Any]]) -> str:
    pairs: list[str] = []
    seen: set[str] = set()
    for cookie in cookies:
        name = str(cookie.get("name") or "").strip()
        value = cookie.get("value")
        if not name or value is None or name in seen:
            continue
        domain = str(cookie.get("domain") or "").lstrip(".").lower()
        # Only ship DeepSeek cookies upstream. The persistent context also
        # accumulates CDN/telemetry cookies for other hosts.
        if domain and domain != "deepseek.com" and not domain.endswith(".deepseek.com"):
            continue
        seen.add(name)
        pairs.append(f"{name}={value}")
    return "; ".join(pairs)


async def _read_browser_credentials(
    context: BrowserContext,
    page: Page,
) -> tuple[str, str, str]:
    token = _normalize_token(
        await page.evaluate(
            """() => {
                const raw = localStorage.getItem('userToken');
                if (raw) return raw;
                // Some app-kit builds store the token under an app-scoped key.
                for (let i = 0; i < localStorage.length; i += 1) {
                    const key = localStorage.key(i) || '';
                    if (key === 'userToken' || key.endsWith('_userToken')) {
                        return localStorage.getItem(key);
                    }
                }
                return null;
            }"""
        )
    )
    device_id = str(
        await page.evaluate(
            """() => {
                const exact = localStorage.getItem('deepseek-device-id:chat');
                if (exact) return exact;
                for (let i = 0; i < localStorage.length; i += 1) {
                    const key = localStorage.key(i) || '';
                    if (key.startsWith('deepseek-device-id:')) {
                        return localStorage.getItem(key) || '';
                    }
                }
                return '';
            }"""
        )
        or ""
    ).strip()
    cookies = _serialize_cookies(await context.cookies())
    return token, cookies, device_id


def _post_ingest(token: str, cookies: str, device_id: str) -> str:
    payload = json.dumps(
        {"token": token, "cookies": cookies, "device_id": device_id}
    ).encode("utf-8")
    request = urllib.request.Request(
        DS2API_INGEST_URL,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Browser-Login-Secret": SHARED_SECRET,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"DS2API ingest HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"DS2API ingest failed: {exc}") from exc
    account = body.get("account") or {}
    return str(account.get("id") or "")


async def _browser_worker(reset_profile: bool) -> None:
    if async_playwright is None:
        state.status = "error"
        state.message = "Playwright 未安装，浏览器登录服务不可用"
        return
    if reset_profile and PROFILE_DIR.exists():
        # Chromium leaves Singleton* and a lock in the profile directory. A
        # fresh launch must not race those files, so move the old profile aside
        # (atomic) instead of deleting in place. Keep only the newest two old
        # profiles: the sidecar image is 2.4 GB and /data shares the root disk.
        stale = PROFILE_DIR.with_name(f"{PROFILE_DIR.name}.old-{int(time.time())}")
        try:
            PROFILE_DIR.rename(stale)
        except OSError:
            shutil.rmtree(PROFILE_DIR, ignore_errors=True)
        olds = sorted(
            PROFILE_DIR.parent.glob(f"{PROFILE_DIR.name}.old-*"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in olds[2:]:
            shutil.rmtree(old, ignore_errors=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    try:
        async with async_playwright() as playwright:
            proxy = (
                os.environ.get("BROWSER_LOGIN_PROXY")
                or os.environ.get("DEEPSEEK_PROXY")
                or ""
            ).strip()
            try:
                context = await playwright.chromium.launch_persistent_context(
                    user_data_dir=str(PROFILE_DIR),
                    headless=False,
                    **({"executable_path": CHROMIUM_PATH} if CHROMIUM_PATH else {}),
                    user_agent=USER_AGENT,
                    locale="zh-CN",
                    timezone_id="Asia/Shanghai",
                    color_scheme="light",
                    ignore_default_args=["--enable-automation"],
                    **(
                        {"proxy": {"server": proxy}}
                        if proxy
                        else {}
                    ),
                    args=[
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-blink-features=AutomationControlled",
                        "--disable-infobars",
                        "--lang=zh-CN",
                    # This is a throwaway container per login: a first-run
                    # modal ("welcome / restore pages") would cover the
                    # sign-in form and noVNC would show a blank desktop.
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-session-crashed-bubble",
                    "--disable-features=Translate,MediaRouter",
                        "--start-maximized",
                        "--window-size=1440,900",
                    ],
                    viewport={"width": 1440, "height": 900},
                )
            except Exception as exc:
                # A killed Chromium often leaves SingletonLock behind. Retry
                # once with a clean profile rather than making the operator
                # delete files manually.
                if not reset_profile:
                    raise
                shutil.rmtree(PROFILE_DIR, ignore_errors=True)
                PROFILE_DIR.mkdir(parents=True, exist_ok=True)
                context = await playwright.chromium.launch_persistent_context(
                    user_data_dir=str(PROFILE_DIR),
                    headless=False,
                    **({"executable_path": CHROMIUM_PATH} if CHROMIUM_PATH else {}),
                    user_agent=USER_AGENT,
                    locale="zh-CN",
                    timezone_id="Asia/Shanghai",
                    color_scheme="light",
                    ignore_default_args=["--enable-automation"],
                    **(
                        {"proxy": {"server": proxy}}
                        if proxy
                        else {}
                    ),
                    args=[
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-blink-features=AutomationControlled",
                        "--disable-infobars",
                        "--lang=zh-CN",
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--disable-session-crashed-bubble",
                        "--disable-features=Translate,MediaRouter",
                        "--start-maximized",
                        "--window-size=1440,900",
                    ],
                    viewport={"width": 1440, "height": 900},
                )
            state.context = context
            await context.add_init_script(STEALTH_JS)
            page = context.pages[0] if context.pages else await context.new_page()
            state.page = page
            await page.goto(SIGN_IN_URL, wait_until="domcontentloaded", timeout=60_000)
            state.status = "waiting_login"
            state.message = "请在浏览器中完成 DeepSeek 登录"
            await _wait_for_login(context, page)
    except asyncio.CancelledError:
        state.status = "stopped"
        state.message = "浏览器会话已停止"
        raise
    except Exception as exc:  # noqa: BLE001 - surface the reason to the operator
        state.status = "error"
        state.message = f"{type(exc).__name__}: {exc}"[:500]
    finally:
        context = state.context
        state.context = None
        state.page = None
        if context is not None:
            with suppress(Exception):
                await context.close()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/sessions/start")
async def start_session(
    req: StartRequest,
    x_browser_login_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    if not _secret_ok(x_browser_login_secret):
        raise HTTPException(status_code=401, detail="Unauthorized")
    if async_playwright is None:
        raise HTTPException(status_code=503, detail="Playwright unavailable")

    async with lock:
        if state.task and not state.task.done():
            state.task.cancel()
            with suppress(asyncio.CancelledError):
                await state.task
        state.status = "starting"
        state.message = "正在启动浏览器"
        state.view_token, state.view_expires_at = issue_view_token()
        state.started_at = time.time()
        state.completed_at = 0.0
        state.account_id = ""
        state.task = asyncio.create_task(_browser_worker(req.reset_profile))
        # Let launch failures surface immediately instead of returning
        # "starting" and making the UI poll a dead worker.
        await asyncio.sleep(0.15)
    return {
        **_public_state(),
        "view_token": state.view_token,
        "view_expires_at": int(state.view_expires_at),
    }


@app.get("/sessions/status")
async def session_status(
    x_browser_login_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    if not _secret_ok(x_browser_login_secret):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return _public_state()


@app.post("/sessions/stop")
async def stop_session(
    x_browser_login_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    if not _secret_ok(x_browser_login_secret):
        raise HTTPException(status_code=401, detail="Unauthorized")
    async with lock:
        if state.task and not state.task.done():
            state.task.cancel()
            with suppress(asyncio.CancelledError):
                await state.task
        state.status = "stopped"
        state.message = "浏览器会话已停止"
        state.view_token = ""
    return _public_state()


@app.get("/auth")
async def auth(
    x_view_token: str | None = Header(default=None, alias="X-View-Token"),
) -> Response:
    if not _view_ok(x_view_token):
        raise HTTPException(status_code=401, detail="Invalid or expired view token")
    return Response(status_code=204)


@app.get("/view-auth")
async def view_auth(
    token: str = "",
    cookie: str | None = Header(default=None, alias="X-Browser-Login-View"),
) -> Response:
    """Validate the view credential used by the Nginx auth_request hook.

    Nginx forwards either the HttpOnly cookie value or a one-off query token.
    """
    if not (_view_ok(token) or _view_ok(cookie)):
        raise HTTPException(status_code=401, detail="Invalid or expired view token")
    return Response(status_code=204)


@app.get("/session/view-token")
async def session_view_token(
    x_browser_login_secret: str | None = Header(default=None),
) -> JSONResponse:
    """Return the current noVNC token to the WebUI (admin-authenticated path).

    This lets the page recover the iframe after a refresh without re-launching
    Chromium.
    """
    if not _secret_ok(x_browser_login_secret):
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not state.view_token or time.time() >= state.view_expires_at:
        raise HTTPException(status_code=404, detail="No active browser view")
    return JSONResponse(
        {"view_token": state.view_token, "view_expires_at": int(state.view_expires_at)}
    )
