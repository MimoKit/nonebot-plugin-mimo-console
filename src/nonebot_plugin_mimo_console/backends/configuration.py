from __future__ import annotations

import asyncio
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from ..env_editor import locate_env_file, read_env, update_env
from .base import (
    ConfigurationBackup,
    ConfigurationEntry,
    ConfigurationSnapshot,
    ConfigurationUpdate,
)

BACKUP_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,255}\.bak$")


def _local_backups(path: Path, backup_dir: Path) -> list[ConfigurationBackup]:
    if not backup_dir.is_dir():
        return []
    items: list[ConfigurationBackup] = []
    for backup in backup_dir.glob(f"{path.name}.*.bak"):
        if backup.is_symlink() or not backup.is_file():
            continue
        metadata = backup.stat()
        items.append(
            ConfigurationBackup(
                backup_id=backup.name,
                created_at=datetime.fromtimestamp(
                    metadata.st_mtime,
                    timezone.utc,
                ).isoformat(),
                size=metadata.st_size,
            )
        )
    return sorted(items, key=lambda item: item.created_at, reverse=True)


def _restore_local(path: Path, backup_dir: Path, backup_id: str) -> bool:
    if not BACKUP_ID_RE.fullmatch(backup_id) or not backup_id.startswith(f"{path.name}."):
        raise ValueError("备份 ID 不合法")
    backup = backup_dir / backup_id
    if backup.is_symlink() or not backup.is_file():
        raise ValueError("配置备份不存在")
    backup_dir.mkdir(parents=True, exist_ok=True)
    created = False
    if path.is_file():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        shutil.copy2(path, backup_dir / f"{path.name}.{stamp}.bak")
        created = True
    temporary = path.with_name(path.name + ".mimo-restore.tmp")
    shutil.copy2(backup, temporary)
    temporary.replace(path)
    return created


class LocalConfigurationBackend:
    def __init__(self, project_root: Path, backup_dir: Path) -> None:
        self.project_root = project_root
        self.backup_dir = backup_dir

    async def read_configuration(self, environment: str) -> ConfigurationSnapshot:
        path = locate_env_file(self.project_root, environment)
        entries = await asyncio.to_thread(read_env, path)
        return ConfigurationSnapshot(
            path=str(path),
            items=[
                ConfigurationEntry(
                    key=entry.key,
                    value=entry.value,
                    secret=entry.secret,
                )
                for entry in entries
            ],
        )

    async def update_configuration(
        self,
        environment: str,
        values: dict[str, str],
    ) -> ConfigurationUpdate:
        path = locate_env_file(self.project_root, environment)
        backup_created = path.is_file()
        await asyncio.to_thread(update_env, path, values, self.backup_dir)
        return ConfigurationUpdate(
            path=str(path),
            restart_required=True,
            backup_created=backup_created,
        )

    async def list_configuration_backups(
        self,
        environment: str,
    ) -> list[ConfigurationBackup]:
        path = locate_env_file(self.project_root, environment)
        return await asyncio.to_thread(_local_backups, path, self.backup_dir)

    async def restore_configuration(
        self,
        environment: str,
        backup_id: str,
    ) -> ConfigurationUpdate:
        path = locate_env_file(self.project_root, environment)
        backup_created = await asyncio.to_thread(
            _restore_local,
            path,
            self.backup_dir,
            backup_id,
        )
        return ConfigurationUpdate(
            path=str(path),
            restart_required=True,
            backup_created=backup_created,
        )
