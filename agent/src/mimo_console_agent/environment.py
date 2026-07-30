from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SECRET_MARKERS = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "COOKIE",
    "API_KEY",
    "ACCESS_KEY",
    "CREDENTIAL",
    "AUTHORIZATION",
)
MASK = "••••••••"
MAX_ENV_FILE_BYTES = 1024 * 1024
MAX_ENV_ITEMS = 512
MAX_ENV_VALUE_CHARS = 16_384
MAX_ENV_TOTAL_CHARS = 128 * 1024
BACKUP_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,255}\.bak$")


class EnvironmentError(ValueError):
    pass


@dataclass(frozen=True)
class EnvironmentEntry:
    key: str
    value: str
    secret: bool

    def public_dict(self) -> dict[str, Any]:
        return {"key": self.key, "value": self.value, "secret": self.secret}


def is_secret_key(key: str) -> bool:
    upper = key.upper()
    return (
        upper == "BOTS"
        or upper.endswith("_BOTS")
        or any(marker in upper for marker in SECRET_MARKERS)
    )


def _read_text(path: Path) -> str:
    if not path.is_file():
        return ""
    if path.stat().st_size > MAX_ENV_FILE_BYTES:
        raise EnvironmentError("环境文件超过 1 MiB 限制")
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise EnvironmentError("环境文件必须使用 UTF-8 编码") from exc


def read_environment(path: Path) -> list[EnvironmentEntry]:
    entries: list[EnvironmentEntry] = []
    for raw in _read_text(path).splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not KEY_RE.fullmatch(key):
            continue
        secret = is_secret_key(key)
        entries.append(
            EnvironmentEntry(
                key=key,
                value=MASK if secret and value else value,
                secret=secret,
            )
        )
    return entries


def _sanitize_updates(updates: dict[str, object]) -> dict[str, str]:
    if len(updates) > MAX_ENV_ITEMS:
        raise EnvironmentError(f"一次最多更新 {MAX_ENV_ITEMS} 个配置项")
    sanitized: dict[str, str] = {}
    total = 0
    for key, value in updates.items():
        if not KEY_RE.fullmatch(key):
            raise EnvironmentError(f"无效配置键：{key}")
        if not isinstance(value, str):
            raise EnvironmentError(f"配置 {key} 必须是字符串")
        if len(value) > MAX_ENV_VALUE_CHARS:
            raise EnvironmentError(f"配置 {key} 超过长度限制")
        if "\n" in value or "\r" in value or "\0" in value:
            raise EnvironmentError(f"配置 {key} 不能包含换行或空字符")
        total += len(key) + len(value)
        if total > MAX_ENV_TOTAL_CHARS:
            raise EnvironmentError("配置更新总大小超过限制")
        if is_secret_key(key) and value == MASK:
            continue
        sanitized[key] = value
    return sanitized


def _render_environment(original: str, updates: dict[str, str]) -> str:
    lines = original.splitlines()
    remaining = dict(updates)
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            output.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in remaining:
            output.append(f"{key}={remaining.pop(key)}")
        else:
            output.append(line)
    if remaining and output and output[-1].strip():
        output.append("")
    output.extend(f"{key}={value}" for key, value in remaining.items())
    return "\n".join(output).rstrip() + "\n"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    original_stat = path.stat() if path.is_file() else None
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if original_stat is not None:
            os.chmod(temporary, stat.S_IMODE(original_stat.st_mode))
            if hasattr(os, "chown"):
                os.chown(temporary, original_stat.st_uid, original_stat.st_gid)
        else:
            os.chmod(temporary, 0o640)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _backup(path: Path, backup_dir: Path, keep: int) -> str:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = backup_dir / f"{path.name}.{stamp}.{uuid.uuid4().hex[:8]}.bak"
    shutil.copy2(path, backup)
    backups = sorted(
        backup_dir.glob(f"{path.name}.*.bak"),
        key=lambda item: item.stat().st_mtime_ns,
        reverse=True,
    )
    for expired in backups[keep:]:
        expired.unlink(missing_ok=True)
    return backup.name


def update_environment(
    path: Path,
    updates: dict[str, object],
    backup_dir: Path,
    keep: int,
) -> str:
    sanitized = _sanitize_updates(updates)
    original = _read_text(path)
    rendered = _render_environment(original, sanitized)
    if len(rendered.encode("utf-8")) > MAX_ENV_FILE_BYTES:
        raise EnvironmentError("更新后的环境文件超过 1 MiB 限制")
    backup_name = _backup(path, backup_dir, keep) if path.is_file() else ""
    _atomic_write(path, rendered)
    return backup_name


def list_environment_backups(path: Path, backup_dir: Path) -> list[dict[str, Any]]:
    if not backup_dir.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for backup in backup_dir.glob(f"{path.name}.*.bak"):
        if backup.is_symlink() or not backup.is_file():
            continue
        metadata = backup.stat()
        items.append(
            {
                "backup_id": backup.name,
                "created_at": datetime.fromtimestamp(
                    metadata.st_mtime,
                    timezone.utc,
                ).isoformat(),
                "size": metadata.st_size,
            }
        )
    return sorted(items, key=lambda item: str(item["created_at"]), reverse=True)


def restore_environment(
    path: Path,
    backup_dir: Path,
    backup_id: str,
    keep: int,
) -> str:
    if not BACKUP_ID_RE.fullmatch(backup_id) or not backup_id.startswith(f"{path.name}."):
        raise EnvironmentError("备份 ID 不合法")
    backup = backup_dir / backup_id
    if backup.is_symlink() or not backup.is_file():
        raise EnvironmentError("配置备份不存在")
    content = _read_text(backup)
    safety_backup = _backup(path, backup_dir, keep) if path.is_file() else ""
    _atomic_write(path, content)
    return safety_backup
