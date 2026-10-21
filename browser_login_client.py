"""Client for the isolated browser-login controller.

DS2API never launches Chromium itself.  The browser lives in a separate
container with Xvfb + noVNC; this module only talks to its small HTTP API.
"""
from __future__ import annotations

import os
from typing import Any

import httpx


class BrowserLoginUnavailable(RuntimeError):
    """The browser-login sidecar is not configured or not reachable."""


def _base_url() -> str:
    return os.environ.get("BROWSER_LOGIN_URL", "").rstrip("/")


def _secret() -> str:
    return os.environ.get("BROWSER_LOGIN_SHARED_SECRET", "")


def enabled() -> bool:
    return bool(_base_url() and _secret())


def _headers() -> dict[str, str]:
    return {"X-Browser-Login-Secret": _secret()}


async def _request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    if not enabled():
        raise BrowserLoginUnavailable(
            "浏览器登录未启用：请配置 BROWSER_LOGIN_URL 和 BROWSER_LOGIN_SHARED_SECRET"
        )
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.request(
                method,
                f"{_base_url()}{path}",
                headers=_headers(),
                **kwargs,
            )
    except httpx.HTTPError as exc:
        raise BrowserLoginUnavailable(f"浏览器登录服务不可达：{exc}") from exc
    if response.status_code >= 400:
        detail = response.text[:300]
        raise BrowserLoginUnavailable(
            f"浏览器登录服务返回 HTTP {response.status_code}: {detail}"
        )
    try:
        return response.json()
    except ValueError as exc:
        raise BrowserLoginUnavailable("浏览器登录服务返回了非 JSON 响应") from exc


async def start(reset_profile: bool = True) -> dict[str, Any]:
    return await _request(
        "POST",
        "/sessions/start",
        json={"reset_profile": reset_profile},
    )


async def status() -> dict[str, Any]:
    return await _request("GET", "/sessions/status")


async def view_token() -> dict[str, Any]:
    return await _request("GET", "/session/view-token")


async def stop() -> dict[str, Any]:
    return await _request("POST", "/sessions/stop")


async def verify_view_token(token: str) -> bool:
    if not enabled() or not token:
        return False
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(
                f"{_base_url()}/auth",
                headers={"X-View-Token": token},
            )
    except httpx.HTTPError:
        return False
    return response.status_code == 204
