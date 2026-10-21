"""Tests for the multi-account keepalive job scheduler."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from keepalive import keepalive
from keepalive_scheduler import KeepaliveScheduler
from keepalive_store import (
    KeepaliveJobStore,
    STATE_ACTIVE,
    STATE_RELOGIN_REQUIRED,
    STATE_RUNNING,
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("KEEPALIVE_STARTUP_JITTER", "0")
    monkeypatch.setenv("KEEPALIVE_LEASE_SECS", "120")
    return KeepaliveJobStore(tmp_path / "keepalive.sqlite3")


def test_store_syncs_accounts_and_claims_due_jobs_atomically(store):
    store.sync_accounts(
        [("account-a", "deepseek"), ("account-b", "zai")],
        now=1000,
    )

    first = store.claim_due("worker-1", 1, now=1000)
    second = store.claim_due("worker-2", 1, now=1000)
    third = store.claim_due("worker-3", 1, now=1000)

    assert len(first) == 1
    assert len(second) == 1
    assert third == []
    assert first[0]["state"] == STATE_RUNNING
    assert second[0]["state"] == STATE_RUNNING


def test_store_keeps_authentication_failures_out_of_due_queue(store):
    store.sync_accounts([("account-a", "deepseek")], now=1000)
    job = store.claim_due("worker", 1, now=1000)[0]

    store.finish_job(
        "account-a",
        "worker",
        state=STATE_RELOGIN_REQUIRED,
        next_run_at=0,
        status=40003,
        error="invalid token",
        success=False,
        now=1001,
    )

    assert job["account_id"] == "account-a"
    assert store.claim_due("worker", 1, now=9999) == []
    current = store.get_job("account-a")
    assert current["state"] == STATE_RELOGIN_REQUIRED
    assert current["last_status"] == 40003


def test_force_run_reenables_relogin_job(store):
    store.sync_accounts([("account-a", "deepseek")], now=1000)
    store.claim_due("worker", 1, now=1000)
    store.finish_job(
        "account-a",
        "worker",
        state=STATE_RELOGIN_REQUIRED,
        next_run_at=0,
        status=40003,
        error="invalid",
        success=False,
        now=1001,
    )

    job = store.update_job("account-a", force=True, now=1002)

    assert job["enabled"] is True
    assert job["state"] == STATE_ACTIVE
    assert job["next_run_at"] == 1002


class _FakePool:
    def __init__(self):
        self.accounts = {
            "deepseek-1": SimpleNamespace(
                id="deepseek-1",
                provider="deepseek",
                token="deepseek-token",
                refresh_token="",
                cookies="ds_session_id=old",
                device_id="device-1",
                proxy="",
                proxy_id="",
                state="idle",
                error_count=0,
                last_error="",
            ),
            "zai-1": SimpleNamespace(
                id="zai-1",
                provider="zai",
                token="zai-token",
                refresh_token="",
                cookies="acw_tc=old",
                device_id="uid_device",
                proxy="",
                proxy_id="",
                state="idle",
                error_count=0,
                last_error="",
            ),
        }
        self.invalid = []
        self.recovered = []

    def keepalive_accounts(self):
        return [(account.id, account.provider) for account in self.accounts.values()]

    def get_by_id(self, account_id):
        return self.accounts.get(account_id)

    def update_keepalive_credentials(
        self,
        account_id,
        *,
        token=None,
        refresh_token=None,
        cookies=None,
        device_id=None,
    ):
        account = self.accounts[account_id]
        if token is not None:
            account.token = token
        if refresh_token is not None:
            account.refresh_token = refresh_token
        if cookies is not None:
            account.cookies = cookies
        if device_id is not None:
            account.device_id = device_id
        return account

    def mark_keepalive_invalid(self, account, error):
        account.state = "error"
        account.last_error = error
        self.invalid.append(account.id)

    def mark_recovered(self, account):
        account.state = "idle"
        self.recovered.append(account.id)


def test_scheduler_runs_providers_independently(store, monkeypatch):
    monkeypatch.setenv("KEEPALIVE_GLOBAL_CONCURRENCY", "2")
    pool = _FakePool()
    scheduler = KeepaliveScheduler(pool, store=store)

    def fake_probe(account, provider):
        if account.id == "deepseek-1":
            return (
                keepalive.ProbeResult(200, True, 0, ""),
                "ds_session_id=new",
                account.device_id,
                account.token,
                account.refresh_token,
            )
        return (
            keepalive.ProbeResult(401, False, 401, "invalid z.ai token"),
            account.cookies,
            account.device_id,
            account.token,
            account.refresh_token,
        )

    monkeypatch.setattr(scheduler, "_probe", fake_probe)
    try:
        assert scheduler.tick_once() == 2
    finally:
        scheduler.stop()

    deepseek_job = store.get_job("deepseek-1")
    zai_job = store.get_job("zai-1")
    assert deepseek_job["state"] == STATE_ACTIVE
    assert zai_job["state"] == STATE_RELOGIN_REQUIRED
    assert pool.accounts["deepseek-1"].cookies == "ds_session_id=new"
    assert pool.invalid == ["zai-1"]


def test_scheduler_isolates_one_crashing_job(store, monkeypatch):
    monkeypatch.setenv("KEEPALIVE_GLOBAL_CONCURRENCY", "2")
    pool = _FakePool()
    scheduler = KeepaliveScheduler(pool, store=store)

    def fake_probe(account, provider):
        if account.id == "deepseek-1":
            raise OSError("network down")
        return (
            keepalive.ProbeResult(200, True, 0, ""),
            "acw_tc=new",
            account.device_id,
            account.token,
            account.refresh_token,
        )

    monkeypatch.setattr(scheduler, "_probe", fake_probe)
    try:
        assert scheduler.tick_once() == 2
    finally:
        scheduler.stop()

    assert store.get_job("deepseek-1")["state"] == "cooldown"
    assert store.get_job("zai-1")["state"] == STATE_ACTIVE
