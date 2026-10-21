"""In-process multi-account keepalive scheduler."""
from __future__ import annotations

import os
import random
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from curl_cffi import requests as cffi_requests

from account_pool import AccountPool
from keepalive import keepalive
from keepalive_store import (
    KeepaliveJobStore,
    STATE_ACTIVE,
    STATE_COOLDOWN,
    STATE_RELOGIN_REQUIRED,
    STATE_RUNNING,
)
from logger import get_logger
from provider_registry import PROVIDER_ZAI, normalize_provider
from proxy_pool import get_proxy_pool

log = get_logger("keepalive_scheduler")


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


class KeepaliveScheduler:
    def __init__(
        self,
        pool: AccountPool,
        store: KeepaliveJobStore | None = None,
    ) -> None:
        self.pool = pool
        self.store = store or KeepaliveJobStore()
        self.enabled = _env_bool("KEEPALIVE_ENABLED", False)
        self.tick_secs = max(5, _env_int("KEEPALIVE_TICK_SECS", 30))
        self.workers = max(1, _env_int("KEEPALIVE_GLOBAL_CONCURRENCY", 4))
        self.retry_base = max(10, _env_int("KEEPALIVE_RETRY_BASE_SECS", 60))
        self.retry_max = max(
            self.retry_base,
            _env_int("KEEPALIVE_RETRY_MAX_SECS", 3600),
        )
        self.jitter_ratio = max(
            0.0,
            min(0.5, float(os.environ.get("KEEPALIVE_JITTER_RATIO", "0.1"))),
        )
        self.worker_id = (
            f"{os.getpid()}-{threading.current_thread().name}-{uuid.uuid4().hex[:8]}"
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._provider_slots = {
            "deepseek": threading.BoundedSemaphore(
                max(1, _env_int("KEEPALIVE_DEEPSEEK_CONCURRENCY", 2))
            ),
            PROVIDER_ZAI: threading.BoundedSemaphore(
                max(1, _env_int("KEEPALIVE_ZAI_CONCURRENCY", 2))
            ),
        }

    def start(self) -> None:
        if not self.enabled or (self._thread and self._thread.is_alive()):
            return
        self._executor = ThreadPoolExecutor(
            max_workers=self.workers,
            thread_name_prefix="keepalive",
        )
        self._thread = threading.Thread(
            target=self._run,
            name="keepalive-scheduler",
            daemon=True,
        )
        self._thread.start()
        log.info(
            "keepalive_scheduler_started",
            extra={"workers": self.workers, "tick_secs": self.tick_secs},
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        if self._executor:
            self._executor.shutdown(wait=True, cancel_futures=True)
        self._thread = None
        self._executor = None

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "running": bool(self._thread and self._thread.is_alive()),
            "worker_id": self.worker_id,
            "tick_secs": self.tick_secs,
            "global_concurrency": self.workers,
            **self.store.stats(),
        }

    def sync(self) -> None:
        self.store.sync_accounts(self.pool.keepalive_accounts())

    def tick_once(self) -> int:
        """Claim and execute all currently due jobs once."""
        self.sync()
        jobs = self.store.claim_due(self.worker_id, self.workers)
        if not jobs:
            return 0
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=self.workers,
                thread_name_prefix="keepalive",
            )
        futures = [self._executor.submit(self._execute_job, job) for job in jobs]
        for future in futures:
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "keepalive_job_crashed",
                    extra={"error": str(exc)},
                )
        return len(jobs)

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.time()
            try:
                self.tick_once()
            except Exception as exc:  # noqa: BLE001
                log.error("keepalive_tick_failed", extra={"error": str(exc)})
            elapsed = time.time() - started
            self._stop.wait(max(1.0, self.tick_secs - elapsed))

    def _execute_job(self, job: dict) -> None:
        account_id = str(job["account_id"])
        provider = normalize_provider(job["provider"])
        started = time.time()
        acct = self.pool.get_by_id(account_id)
        if acct is None:
            self.store.update_job(account_id, enabled=False)
            return

        outcome = "transient"
        status: int | None = None
        error = ""
        try:
            with self._provider_slots[provider]:
                result, cookies, device_id = self._probe(acct, provider)
            status = result.status
            error = result.message
            if result.valid:
                if (
                    cookies != acct.cookies
                    or (device_id and device_id != acct.device_id)
                ):
                    self.pool.update_keepalive_credentials(
                        account_id,
                        cookies=cookies if cookies != acct.cookies else None,
                        device_id=(
                            device_id
                            if device_id and device_id != acct.device_id
                            else None
                        ),
                    )
                if acct.state == "error":
                    self.pool.mark_recovered(acct)
                outcome = "success"
            elif result.code in keepalive.INVALID_TOKEN_CODES or (
                result.status in keepalive.INVALID_HTTP_STATUSES
            ):
                outcome = "invalid"
                self.pool.mark_keepalive_invalid(
                    acct,
                    error or "keepalive login is invalid",
                )
            else:
                outcome = "transient"
        except Exception as exc:  # noqa: BLE001
            status = None
            error = f"{type(exc).__name__}: {exc}"
            outcome = "transient"

        now = int(time.time())
        if outcome == "success":
            interval = int(job["interval_secs"])
            jitter = int(interval * random.uniform(-self.jitter_ratio, self.jitter_ratio))
            self.store.finish_job(
                account_id,
                self.worker_id,
                state=STATE_ACTIVE,
                next_run_at=max(now + 60, now + interval + jitter),
                status=status,
                error="",
                success=True,
                now=now,
            )
        elif outcome == "invalid":
            self.store.finish_job(
                account_id,
                self.worker_id,
                state=STATE_RELOGIN_REQUIRED,
                next_run_at=0,
                status=status,
                error=error,
                success=False,
                now=now,
            )
        else:
            failures = int(job.get("consecutive_failures") or 0) + 1
            delay = min(
                self.retry_max,
                self.retry_base * (2 ** min(failures - 1, 12)),
            )
            delay = max(10, int(delay * random.uniform(0.8, 1.2)))
            self.store.finish_job(
                account_id,
                self.worker_id,
                state=STATE_COOLDOWN,
                next_run_at=now + delay,
                status=status,
                error=error,
                success=False,
                now=now,
            )
        log.info(
            "keepalive_probe_finished",
            extra={
                "account_id": account_id,
                "provider": provider,
                "outcome": outcome,
                "status": status,
                "elapsed_ms": int((time.time() - started) * 1000),
            },
        )

    def _probe(self, acct, provider: str) -> tuple[keepalive.ProbeResult, str, str]:
        token = keepalive._normalize_token(acct.token)
        if not token:
            code = 401 if provider == PROVIDER_ZAI else 40003
            return (
                keepalive.ProbeResult(0, False, code, "no token"),
                acct.cookies,
                acct.device_id,
            )

        device_id = (acct.device_id or "").strip()
        generated_device = False
        if not device_id:
            device_id = (
                f"uid_{uuid.uuid4().hex}"
                if provider == PROVIDER_ZAI
                else str(uuid.uuid4())
            )
            generated_device = True

        proxy_url = (acct.proxy or "").strip()
        if not proxy_url and acct.proxy_id:
            proxy_url = get_proxy_pool().resolve_url(acct.proxy_id) or ""
        proxies = {"all": proxy_url} if proxy_url else None
        impersonate = (
            os.environ.get("ZAI_IMPERSONATE", "chrome150")
            if provider == PROVIDER_ZAI
            else os.environ.get("DEEPSEEK_IMPERSONATE", "chrome150")
        )
        session = cffi_requests.Session(
            impersonate=impersonate,
            timeout=30,
            proxies=proxies,
        )
        try:
            if provider == PROVIDER_ZAI:
                result = keepalive.zai_probe(
                    session,
                    token,
                    acct.cookies,
                    device_id=device_id,
                )
                refreshed = keepalive._cookie_header(session.cookies.jar)
                merged = keepalive._merge_cookie_headers(acct.cookies, refreshed)
            else:
                keepalive._seed_jar(session, acct.cookies)
                result = keepalive.probe(session, token, device_id=device_id)
                merged = keepalive._merge_cookie_headers(
                    acct.cookies,
                    keepalive._cookie_header(session.cookies.jar),
                )
            return result, merged, device_id if generated_device else acct.device_id
        finally:
            session.close()


_scheduler: KeepaliveScheduler | None = None


def configure_scheduler(pool: AccountPool) -> KeepaliveScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = KeepaliveScheduler(pool)
    return _scheduler


def start_scheduler(pool: AccountPool) -> KeepaliveScheduler:
    scheduler = configure_scheduler(pool)
    scheduler.start()
    return scheduler


def get_scheduler() -> KeepaliveScheduler | None:
    return _scheduler


def get_scheduler_or_store() -> tuple[KeepaliveScheduler | None, KeepaliveJobStore]:
    scheduler = get_scheduler()
    if scheduler is not None:
        return scheduler, scheduler.store
    return None, KeepaliveJobStore()


def notify_account_changed(account_id: str | None = None) -> None:
    """Refresh jobs after credential import/update without creating test DBs."""
    scheduler = get_scheduler()
    if scheduler is None:
        return
    scheduler.sync()
    if account_id:
        scheduler.store.update_job(account_id, enabled=True, force=True)
