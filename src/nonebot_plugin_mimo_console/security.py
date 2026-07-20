from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
SPECIAL_RE = re.compile(r"[^A-Za-z0-9]")


class AuthError(ValueError):
    pass


def validate_password(password: str) -> None:
    if len(password) < 8:
        raise AuthError("密码至少需要 8 位")
    if not re.search(r"[A-Z]", password):
        raise AuthError("密码至少需要一个大写字母")
    if not re.search(r"[a-z]", password):
        raise AuthError("密码至少需要一个小写字母")
    if not re.search(r"\d", password):
        raise AuthError("密码至少需要一个数字")
    if not SPECIAL_RE.search(password):
        raise AuthError("密码至少需要一个特殊符号，例如 @")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _password_hash(password: str, salt: bytes) -> str:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=2**14,
        r=8,
        p=1,
        dklen=32,
    ).hex()


@dataclass(frozen=True)
class Session:
    username: str
    expires_at: float


class AuthStore:
    def __init__(self, path: Path, session_hours: int = 72) -> None:
        self.path = path
        self.session_seconds = session_hours * 3600
        self._lock = RLock()
        self._sessions: dict[str, Session] = {}
        self._setup_token = ""
        self._data = self._read()

    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with suppress(OSError):
            os.chmod(temp, 0o600)
        temp.replace(self.path)

    @property
    def configured(self) -> bool:
        return bool(self._data.get("username") and self._data.get("password_hash"))

    @property
    def username(self) -> str:
        return str(self._data.get("username") or "")

    def issue_setup_token(self) -> str | None:
        if self.configured:
            return None
        with self._lock:
            self._setup_token = secrets.token_urlsafe(24)
            self._data["setup_token_hash"] = _digest(self._setup_token)
            self._write()
            return self._setup_token

    def setup(self, setup_token: str, username: str, password: str) -> str:
        with self._lock:
            if self.configured:
                raise AuthError("管理员账号已经初始化")
            expected = str(self._data.get("setup_token_hash") or "")
            if not expected or not hmac.compare_digest(_digest(setup_token), expected):
                raise AuthError("初始化令牌不正确")
            if not USERNAME_RE.fullmatch(username):
                raise AuthError("用户名需要 3-32 位，只能使用字母、数字、点、横线和下划线")
            validate_password(password)
            salt = secrets.token_bytes(16)
            self._data = {
                "username": username,
                "password_salt": salt.hex(),
                "password_hash": _password_hash(password, salt),
                "created_at": int(time.time()),
            }
            self._write()
            return self._new_session(username)

    def login(self, username: str, password: str) -> str:
        with self._lock:
            if not self.configured:
                raise AuthError("控制台还没有初始化")
            if not hmac.compare_digest(username, self.username):
                raise AuthError("用户名或密码错误")
            try:
                salt = bytes.fromhex(str(self._data["password_salt"]))
                actual = _password_hash(password, salt)
            except (KeyError, ValueError):
                raise AuthError("管理员凭据损坏，请删除 auth.json 后重新初始化") from None
            if not hmac.compare_digest(actual, str(self._data["password_hash"])):
                raise AuthError("用户名或密码错误")
            return self._new_session(username)

    def _new_session(self, username: str) -> str:
        raw = secrets.token_urlsafe(32)
        self._sessions[_digest(raw)] = Session(
            username=username,
            expires_at=time.time() + self.session_seconds,
        )
        return raw

    def verify(self, token: str) -> Session | None:
        if not token:
            return None
        with self._lock:
            now = time.time()
            expired = [key for key, value in self._sessions.items() if value.expires_at <= now]
            for key in expired:
                self._sessions.pop(key, None)
            return self._sessions.get(_digest(token))

    def logout(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(_digest(token), None)
