from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

PackageAction = Literal["install", "update", "uninstall"]
OperationAction = Literal["install", "update", "uninstall", "restart"]
OperationStatus = Literal[
    "queued",
    "preparing",
    "locking",
    "building",
    "verifying",
    "deploying",
    "health_checking",
    "succeeded",
    "rolling_back",
    "rolled_back",
    "failed",
]


@dataclass(frozen=True)
class PackageRequest:
    action: PackageAction
    module_name: str
    project_name: str
    project_root: Path
    repository_url: str = ""


@dataclass
class PackageOperation:
    operation_id: str
    action: OperationAction
    module_name: str
    project_name: str
    status: OperationStatus
    restart_required: bool = False
    rollback_available: bool = False
    output: str = ""
    error: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        # Preserve the response keys returned by Mimo Console 0.1.x.
        value["ok"] = self.status == "succeeded"
        return value


@dataclass(frozen=True)
class ConfigurationEntry:
    key: str
    value: str
    secret: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ConfigurationSnapshot:
    path: str
    items: list[ConfigurationEntry]

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "items": [item.as_dict() for item in self.items],
        }


@dataclass(frozen=True)
class ConfigurationUpdate:
    path: str
    restart_required: bool
    backup_created: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "path": self.path,
            "restart_required": self.restart_required,
            "backup_created": self.backup_created,
        }


@dataclass(frozen=True)
class ConfigurationBackup:
    backup_id: str
    created_at: str
    size: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ConfigurationBackend(Protocol):
    async def read_configuration(self, environment: str) -> ConfigurationSnapshot: ...

    async def update_configuration(
        self,
        environment: str,
        values: dict[str, str],
    ) -> ConfigurationUpdate: ...

    async def list_configuration_backups(
        self,
        environment: str,
    ) -> list[ConfigurationBackup]: ...

    async def restore_configuration(
        self,
        environment: str,
        backup_id: str,
    ) -> ConfigurationUpdate: ...


class PackageBackend(Protocol):
    async def capabilities(self) -> dict[str, Any]: ...

    async def manage(self, request: PackageRequest, timeout: int) -> PackageOperation: ...

    async def get_operation(self, operation_id: str) -> PackageOperation | None: ...

    async def list_operations(self) -> list[PackageOperation]: ...

    async def rollback(self, operation_id: str) -> PackageOperation: ...

    async def restart(self) -> PackageOperation: ...
