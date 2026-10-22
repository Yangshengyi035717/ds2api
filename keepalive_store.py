"""SQLite-backed scheduling state for multi-account keepalive."""
from __future__ import annotations

import os
import random
import sqlite3
import time
from pathlib import Path
from typing import Iterable

from provider_registry import (
    PROVIDER_DEEPSEEK,
    PROVIDER_DOUBAO,
    PROVIDER_HUNYUAN,
    PROVIDER_KIMI,
    PROVIDER_MIMO,
    PROVIDER_QWEN,
    PROVIDER_WENXIN,
    PROVIDER_ZAI,
    normalize_provider,
)

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_STORE_PATH = PROJECT_ROOT / "data" / "keepalive.sqlite3"

STATE_ACTIVE = "active"
STATE_COOLDOWN = "cooldown"
STATE_RELOGIN_REQUIRED = "relogin_required"
STATE_DISABLED = "disabled"
STATE_RUNNING = "running"
RUNNABLE_STATES = (STATE_ACTIVE, STATE_COOLDOWN)

MIN_INTERVAL_SECS = 60
MAX_INTERVAL_SECS = 24 * 60 * 60


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _clamp_interval(value: int) -> int:
    return max(MIN_INTERVAL_SECS, min(MAX_INTERVAL_SECS, int(value)))


class KeepaliveJobStore:
    def __init__(self, path: str | Path | None = None):
        configured = path or os.environ.get(
            "KEEPALIVE_STORE_PATH",
            str(DEFAULT_STORE_PATH),
        )
        self.path = Path(configured)
        if not self.path.is_absolute():
            self.path = PROJECT_ROOT / self.path
        self.deepseek_interval = _clamp_interval(
            _env_int("KEEPALIVE_DEEPSEEK_INTERVAL", 15 * 60)
        )
        self.zai_interval = _clamp_interval(
            _env_int("KEEPALIVE_ZAI_INTERVAL", 15 * 60)
        )
        self.kimi_interval = _clamp_interval(
            _env_int("KEEPALIVE_KIMI_INTERVAL", 15 * 60)
        )
        self.qwen_interval = _clamp_interval(
            _env_int("KEEPALIVE_QWEN_INTERVAL", 15 * 60)
        )
        self.hunyuan_interval = _clamp_interval(
            _env_int("KEEPALIVE_HUNYUAN_INTERVAL", 15 * 60)
        )
        self.wenxin_interval = _clamp_interval(
            _env_int("KEEPALIVE_WENXIN_INTERVAL", 15 * 60)
        )
        self.doubao_interval = _clamp_interval(
            _env_int("KEEPALIVE_DOUBAO_INTERVAL", 15 * 60)
        )
        self.mimo_interval = _clamp_interval(
            _env_int("KEEPALIVE_MIMO_INTERVAL", 15 * 60)
        )
        self.startup_jitter = max(0, _env_int("KEEPALIVE_STARTUP_JITTER", 60))
        self.lease_secs = max(30, _env_int("KEEPALIVE_LEASE_SECS", 120))
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=15000")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS keepalive_jobs (
                    account_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    interval_secs INTEGER NOT NULL,
                    next_run_at INTEGER NOT NULL,
                    lease_owner TEXT,
                    lease_until INTEGER,
                    state TEXT NOT NULL DEFAULT 'active',
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    last_run_at INTEGER,
                    last_success_at INTEGER,
                    last_status INTEGER,
                    last_error TEXT NOT NULL DEFAULT '',
                    updated_at INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_keepalive_due
                ON keepalive_jobs(enabled, state, next_run_at)
                """
            )
            os.chmod(self.path, 0o600)

    def default_interval(self, provider: str) -> int:
        normalized = normalize_provider(provider)
        if normalized == PROVIDER_ZAI:
            return self.zai_interval
        if normalized == PROVIDER_KIMI:
            return self.kimi_interval
        if normalized == PROVIDER_QWEN:
            return self.qwen_interval
        if normalized == PROVIDER_HUNYUAN:
            return self.hunyuan_interval
        if normalized == PROVIDER_WENXIN:
            return self.wenxin_interval
        if normalized == PROVIDER_DOUBAO:
            return self.doubao_interval
        if normalized == PROVIDER_MIMO:
            return self.mimo_interval
        return self.deepseek_interval

    def sync_accounts(
        self,
        accounts: Iterable[tuple[str, str]],
        *,
        now: int | None = None,
    ) -> None:
        current = int(time.time() if now is None else now)
        pairs = [
            (str(account_id), normalize_provider(provider))
            for account_id, provider in accounts
            if account_id
        ]
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE keepalive_jobs
                SET state='active', lease_owner=NULL, lease_until=NULL,
                    updated_at=?
                WHERE state='running' AND lease_until IS NOT NULL
                  AND lease_until<?
                """,
                (current, current),
            )
            for account_id, provider in pairs:
                interval = self.default_interval(provider)
                first_run = current + random.randint(0, self.startup_jitter)
                connection.execute(
                    """
                    INSERT INTO keepalive_jobs (
                        account_id, provider, enabled, interval_secs,
                        next_run_at, state, updated_at
                    ) VALUES (?, ?, 1, ?, ?, 'active', ?)
                    ON CONFLICT(account_id) DO UPDATE SET
                        provider=excluded.provider,
                        updated_at=excluded.updated_at
                    """,
                    (account_id, provider, interval, first_run, current),
                )
            if pairs:
                placeholders = ",".join("?" for _ in pairs)
                connection.execute(
                    f"DELETE FROM keepalive_jobs WHERE account_id NOT IN ({placeholders})",
                    [account_id for account_id, _ in pairs],
                )
            else:
                connection.execute("DELETE FROM keepalive_jobs")

    def list_jobs(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM keepalive_jobs ORDER BY provider, account_id"
            ).fetchall()
        return [self._public_row(row) for row in rows]

    def get_job(self, account_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM keepalive_jobs WHERE account_id=?",
                (account_id,),
            ).fetchone()
        return self._public_row(row) if row else None

    def claim_due(
        self,
        worker_id: str,
        limit: int,
        *,
        now: int | None = None,
    ) -> list[dict]:
        current = int(time.time() if now is None else now)
        lease_until = current + self.lease_secs
        claimed: list[dict] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT * FROM keepalive_jobs
                WHERE enabled=1
                  AND state IN ('active','cooldown')
                  AND next_run_at<=?
                  AND (lease_until IS NULL OR lease_until<?)
                ORDER BY next_run_at, account_id
                LIMIT ?
                """,
                (current, current, max(1, int(limit))),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    UPDATE keepalive_jobs
                    SET state='running', lease_owner=?, lease_until=?,
                        last_run_at=?, updated_at=?
                    WHERE account_id=?
                    """,
                    (worker_id, lease_until, current, current, row["account_id"]),
                )
                item = self._public_row(row)
                item.update(
                    {
                        "state": STATE_RUNNING,
                        "lease_owner": worker_id,
                        "lease_until": lease_until,
                        "last_run_at": current,
                    }
                )
                claimed.append(item)
            connection.commit()
        return claimed

    def finish_job(
        self,
        account_id: str,
        worker_id: str,
        *,
        state: str,
        next_run_at: int | None,
        status: int | None,
        error: str = "",
        success: bool = False,
        now: int | None = None,
    ) -> None:
        current = int(time.time() if now is None else now)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT consecutive_failures FROM keepalive_jobs WHERE account_id=?",
                (account_id,),
            ).fetchone()
            if row is None:
                return
            failures = 0 if success else int(row["consecutive_failures"]) + 1
            last_success = current if success else None
            connection.execute(
                """
                UPDATE keepalive_jobs
                SET state=?, next_run_at=?, lease_owner=NULL, lease_until=NULL,
                    consecutive_failures=?, last_status=?, last_error=?,
                    last_success_at=COALESCE(?, last_success_at),
                    updated_at=?
                WHERE account_id=? AND lease_owner=?
                """,
                (
                    state,
                    next_run_at if next_run_at is not None else 0,
                    failures,
                    status,
                    str(error or "")[:1000],
                    last_success,
                    current,
                    account_id,
                    worker_id,
                ),
            )

    def update_job(
        self,
        account_id: str,
        *,
        enabled: bool | None = None,
        interval_secs: int | None = None,
        force: bool = False,
        now: int | None = None,
    ) -> dict | None:
        current = int(time.time() if now is None else now)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM keepalive_jobs WHERE account_id=?",
                (account_id,),
            ).fetchone()
            if row is None:
                return None
            new_enabled = int(row["enabled"]) if enabled is None else int(enabled)
            new_interval = (
                int(row["interval_secs"])
                if interval_secs is None
                else _clamp_interval(interval_secs)
            )
            state = str(row["state"])
            next_run = int(row["next_run_at"])
            if not new_enabled:
                state = STATE_DISABLED
            elif state in {STATE_DISABLED, STATE_RELOGIN_REQUIRED}:
                state = STATE_ACTIVE
                next_run = current
            if force:
                new_enabled = 1
                state = STATE_ACTIVE
                next_run = current
            connection.execute(
                """
                UPDATE keepalive_jobs
                SET enabled=?, interval_secs=?, state=?, next_run_at=?,
                    consecutive_failures=CASE WHEN ? THEN 0 ELSE consecutive_failures END,
                    last_error=CASE WHEN ? THEN '' ELSE last_error END,
                    updated_at=?
                WHERE account_id=?
                """,
                (
                    new_enabled,
                    new_interval,
                    state,
                    next_run,
                    int(force),
                    int(force),
                    current,
                    account_id,
                ),
            )
        return self.get_job(account_id)

    def bulk_update(
        self,
        *,
        action: str,
        account_ids: Iterable[str] | None = None,
        provider: str | None = None,
        interval_secs: int | None = None,
    ) -> int:
        ids = [str(value) for value in (account_ids or []) if value]
        clauses: list[str] = []
        params: list[object] = []
        if ids:
            clauses.append(f"account_id IN ({','.join('?' for _ in ids)})")
            params.extend(ids)
        if provider:
            clauses.append("provider=?")
            params.append(normalize_provider(provider))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT account_id FROM keepalive_jobs {where}",
                params,
            ).fetchall()
            selected = [str(row["account_id"]) for row in rows]
        updated = 0
        for account_id in selected:
            if action == "enable":
                result = self.update_job(
                    account_id,
                    enabled=True,
                    interval_secs=interval_secs,
                )
            elif action == "disable":
                result = self.update_job(account_id, enabled=False)
            elif action == "probe":
                result = self.update_job(account_id, force=True)
            elif action == "clear":
                result = self.update_job(
                    account_id,
                    enabled=True,
                    force=True,
                    interval_secs=interval_secs,
                )
            else:
                raise ValueError(f"unknown keepalive action: {action}")
            updated += int(result is not None)
        return updated

    def stats(self) -> dict:
        jobs = self.list_jobs()
        return {
            "total": len(jobs),
            "enabled_count": sum(1 for job in jobs if job["enabled"]),
            "active": sum(1 for job in jobs if job["state"] == STATE_ACTIVE),
            "cooldown": sum(1 for job in jobs if job["state"] == STATE_COOLDOWN),
            "relogin_required": sum(
                1 for job in jobs if job["state"] == STATE_RELOGIN_REQUIRED
            ),
            "running_count": sum(
                1 for job in jobs if job["state"] == STATE_RUNNING
            ),
        }

    @staticmethod
    def _public_row(row: sqlite3.Row) -> dict:
        item = dict(row)
        item["enabled"] = bool(item["enabled"])
        for key in (
            "interval_secs",
            "next_run_at",
            "lease_until",
            "consecutive_failures",
            "last_run_at",
            "last_success_at",
            "last_status",
            "updated_at",
        ):
            if item.get(key) is not None:
                item[key] = int(item[key])
        return item
