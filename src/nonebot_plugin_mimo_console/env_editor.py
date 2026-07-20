from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

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


@dataclass(frozen=True)
class EnvEntry:
    key: str
    value: str
    secret: bool


def is_secret_key(key: str) -> bool:
    upper = key.upper()
    return (
        upper == "BOTS"
        or upper.endswith("_BOTS")
        or any(marker in upper for marker in SECRET_MARKERS)
    )


def locate_env_file(project_root: Path, environment: str = "prod") -> Path:
    candidates = [project_root / f".env.{environment}", project_root / ".env"]
    return next((path for path in candidates if path.is_file()), candidates[0])


def read_env(path: Path) -> list[EnvEntry]:
    if not path.is_file():
        return []
    entries: list[EnvEntry] = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not KEY_RE.fullmatch(key):
            continue
        secret = is_secret_key(key)
        entries.append(EnvEntry(key, MASK if secret and value else value, secret))
    return entries


def update_env(path: Path, updates: dict[str, str], backup_dir: Path | None = None) -> None:
    sanitized: dict[str, str] = {}
    for key, value in updates.items():
        if not KEY_RE.fullmatch(key):
            raise ValueError(f"无效配置键：{key}")
        text = str(value)
        if "\n" in text or "\r" in text or "\0" in text:
            raise ValueError(f"配置 {key} 不能包含换行或空字符")
        if is_secret_key(key) and text == MASK:
            continue
        sanitized[key] = text

    path.parent.mkdir(parents=True, exist_ok=True)
    original = path.read_text(encoding="utf-8-sig") if path.is_file() else ""
    lines = original.splitlines()
    remaining = dict(sanitized)
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

    if path.is_file():
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target_dir = backup_dir or path.parent
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target_dir / f"{path.name}.{timestamp}.bak")
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    temp.replace(path)
