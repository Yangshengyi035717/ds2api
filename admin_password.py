"""Persistent administrator password with environment fallback.

The initial password still comes from ``DEEPSEEK_ADMIN_PASSWORD``. Once the
operator changes it from the WebUI, only a PBKDF2-SHA256 verifier is stored in
``data/admin_auth.json``; the plaintext password is never written to disk.

Deleting the verifier file deliberately falls back to the environment value,
which is useful for disaster recovery.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("ds2api.admin_password")

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_STORE_PATH = PROJECT_ROOT / "data" / "admin_auth.json"
PBKDF2_ITERATIONS = 600_000
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 256
_WEAK_PASSWORDS = {
    "",
    "admin",
    "password",
    "password123",
    "123456",
    "12345678",
    "changeme",
    "qwerty123",
}


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def _validate_new_password(password: str) -> str:
    if not isinstance(password, str):
        raise ValueError("新密码格式无效")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"新密码至少需要 {MIN_PASSWORD_LENGTH} 个字符")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ValueError(f"新密码不能超过 {MAX_PASSWORD_LENGTH} 个字符")
    if "\x00" in password:
        raise ValueError("新密码不能包含 NUL 字符")
    if password.lower() in _WEAK_PASSWORDS:
        raise ValueError("新密码过于常见，请换一个更安全的密码")
    return password


class AdminPasswordStore:
    """Thread-safe PBKDF2 verifier with a transparent env fallback."""

    def __init__(
        self,
        path: str | Path | None = None,
        environment_password: str | None = None,
    ) -> None:
        configured = path or os.environ.get("ADMIN_PASSWORD_STORE_PATH")
        selected = Path(configured) if configured else DEFAULT_STORE_PATH
        self._path = selected if selected.is_absolute() else PROJECT_ROOT / selected
        self._environment_password = (
            environment_password
            if environment_password is not None
            else os.environ.get("DEEPSEEK_ADMIN_PASSWORD", "admin")
        )
        self._salt: bytes | None = None
        self._hash: bytes | None = None
        self._iterations = PBKDF2_ITERATIONS
        self._updated_at = 0
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("配置根节点必须是对象")
            if raw.get("version") != 1:
                raise ValueError("不支持的配置版本")
            if raw.get("algorithm") != "pbkdf2_sha256":
                raise ValueError("不支持的密码算法")
            iterations = int(raw.get("iterations") or 0)
            if iterations < 100_000 or iterations > 5_000_000:
                raise ValueError("PBKDF2 迭代次数超出允许范围")
            salt = _b64decode(str(raw.get("salt") or ""))
            digest = _b64decode(str(raw.get("hash") or ""))
            if len(salt) < 16 or len(digest) != 32:
                raise ValueError("密码 verifier 长度无效")
        except Exception as exc:  # noqa: BLE001 - fall back, never brick login
            log.warning(
                "admin_password_store_load_failed path=%s error=%s",
                self._path,
                exc,
            )
            return

        self._salt = salt
        self._hash = digest
        self._iterations = iterations
        self._updated_at = int(raw.get("updated_at") or 0)

    @property
    def source(self) -> str:
        return "webui" if self._hash is not None else "env"

    @property
    def updated_at(self) -> int:
        return self._updated_at

    def verify(self, password: str) -> bool:
        value = password if isinstance(password, str) else ""
        if len(value) > MAX_PASSWORD_LENGTH:
            return False
        with self._lock:
            salt = self._salt
            expected = self._hash
            iterations = self._iterations
        if salt is None or expected is None:
            return hmac.compare_digest(
                value.encode("utf-8"),
                (self._environment_password or "").encode("utf-8"),
            )
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            value.encode("utf-8"),
            salt,
            iterations,
            dklen=32,
        )
        return hmac.compare_digest(actual, expected)

    def change(self, current_password: str, new_password: str) -> None:
        with self._lock:
            if not self.verify(current_password):
                raise PermissionError("当前密码不正确")
            value = _validate_new_password(new_password)
            if self.verify(value):
                raise ValueError("新密码不能与当前密码相同")

            salt = secrets.token_bytes(16)
            digest = hashlib.pbkdf2_hmac(
                "sha256",
                value.encode("utf-8"),
                salt,
                PBKDF2_ITERATIONS,
                dklen=32,
            )
            updated_at = int(time.time())
            payload = {
                "version": 1,
                "algorithm": "pbkdf2_sha256",
                "iterations": PBKDF2_ITERATIONS,
                "salt": _b64encode(salt),
                "hash": _b64encode(digest),
                "updated_at": updated_at,
            }
            self._write_atomic(payload)
            self._salt = salt
            self._hash = digest
            self._iterations = PBKDF2_ITERATIONS
            self._updated_at = updated_at

    def _write_atomic(self, payload: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self._path.parent, 0o700)
        except OSError:
            pass
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self._path.name}.",
            suffix=".tmp",
            dir=str(self._path.parent),
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, self._path)
            try:
                os.chmod(self._path, 0o600)
            except OSError:
                pass
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def snapshot(self) -> dict[str, Any]:
        return {
            "set": bool(self._environment_password or self._hash is not None),
            "source": self.source,
            "weak": self.is_weak(),
            "updated_at": self.updated_at,
        }

    def is_weak(self) -> bool:
        # A runtime verifier is created only through the validated change flow.
        if self._hash is not None:
            return False
        return (self._environment_password or "") in _WEAK_PASSWORDS


_store: AdminPasswordStore | None = None
_store_lock = threading.Lock()


def get_admin_password_store() -> AdminPasswordStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = AdminPasswordStore()
        return _store
