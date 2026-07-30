from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx

from ..env_editor import MASK, is_secret_key
from .base import (
    ConfigurationBackup,
    ConfigurationEntry,
    ConfigurationSnapshot,
    ConfigurationUpdate,
    PackageOperation,
    PackageRequest,
)

VALID_ACTIONS = {"install", "update", "uninstall", "restart"}
VALID_STATUSES = {
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
}
CONFIG_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_CONFIG_ITEMS = 512
MAX_CONFIG_VALUE_CHARS = 16_384


class DockerAgentError(RuntimeError):
    pass


def _operation_from_payload(payload: dict[str, Any]) -> PackageOperation:
    required = {
        "operation_id",
        "action",
        "module_name",
        "project_name",
        "status",
    }
    if not required.issubset(payload):
        raise DockerAgentError("Docker Agent 返回了无法识别的操作数据")
    action = str(payload["action"])
    status = str(payload["status"])
    if action not in VALID_ACTIONS or status not in VALID_STATUSES:
        raise DockerAgentError("Docker Agent 返回了无法识别的操作状态")
    return PackageOperation(
        operation_id=str(payload["operation_id"]),
        action=action,  # type: ignore[arg-type]
        module_name=str(payload["module_name"]),
        project_name=str(payload["project_name"]),
        status=status,  # type: ignore[arg-type]
        restart_required=bool(payload.get("restart_required", False)),
        rollback_available=bool(payload.get("rollback_available", False)),
        output=str(payload.get("output", "")),
        error=str(payload.get("error", "")),
        steps=list(payload.get("steps") or []),
        created_at=float(payload.get("created_at", 0.0)),
        updated_at=float(payload.get("updated_at", 0.0)),
    )


def _configuration_from_payload(payload: dict[str, Any]) -> ConfigurationSnapshot:
    path = payload.get("path")
    items = payload.get("items")
    if (
        not isinstance(path, str)
        or not path
        or len(path) > 4096
        or "\0" in path
        or not isinstance(items, list)
        or len(items) > MAX_CONFIG_ITEMS
    ):
        raise DockerAgentError("Docker Agent 返回了无法识别的配置数据")
    parsed: list[ConfigurationEntry] = []
    for item in items:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("key"), str)
            or not isinstance(item.get("value"), str)
            or not isinstance(item.get("secret"), bool)
            or not CONFIG_KEY_RE.fullmatch(item["key"])
            or len(item["value"]) > MAX_CONFIG_VALUE_CHARS
        ):
            raise DockerAgentError("Docker Agent 返回了无法识别的配置项")
        parsed.append(
            ConfigurationEntry(
                key=item["key"],
                value=item["value"],
                secret=item["secret"],
            )
        )
    return ConfigurationSnapshot(path=path, items=parsed)


class DockerAgentBackend:
    def __init__(
        self,
        socket_path: Path,
        token_file: Path,
        instance_id: str,
    ) -> None:
        self.socket_path = socket_path
        self.token_file = token_file
        self.instance_id = instance_id

    def _token(self) -> str:
        try:
            token = self.token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise DockerAgentError(f"无法读取 Docker Agent 令牌：{exc}") from exc
        if not token:
            raise DockerAgentError("Docker Agent 令牌为空")
        return token

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        if not self.socket_path.is_socket():
            raise DockerAgentError(f"Docker Agent Socket 不可用：{self.socket_path}")
        transport = httpx.AsyncHTTPTransport(uds=str(self.socket_path))
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "X-Mimo-Instance": self.instance_id,
        }
        try:
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://mimo-agent",
                timeout=timeout,
            ) as client:
                response = await client.request(method, path, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise DockerAgentError(f"无法连接 Docker Agent：{exc}") from exc
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise DockerAgentError("Docker Agent 返回了非 JSON 响应") from exc
        if response.is_error:
            detail = data.get("detail") if isinstance(data, dict) else None
            raise DockerAgentError(str(detail or f"Docker Agent HTTP {response.status_code}"))
        if not isinstance(data, dict):
            raise DockerAgentError("Docker Agent 返回了无法识别的数据")
        return data

    async def capabilities(self) -> dict[str, Any]:
        data = await self._request("GET", "/v1/status")
        data["mode"] = "docker-agent"
        return data

    async def read_configuration(self, environment: str) -> ConfigurationSnapshot:
        del environment
        return _configuration_from_payload(await self._request("GET", "/v1/config"))

    async def update_configuration(
        self,
        environment: str,
        values: dict[str, str],
    ) -> ConfigurationUpdate:
        del environment
        # read_configuration returns secret values as a mask; if the UI resubmits
        # an unchanged secret we must not forward the mask sentinel, or the agent
        # would overwrite the real secret with the literal mask. Mirror the local
        # backend's env_editor filtering here rather than rely on the agent to
        # replicate it.
        filtered = {
            key: value
            for key, value in values.items()
            if not (is_secret_key(key) and value == MASK)
        }
        data = await self._request("PUT", "/v1/config", {"values": filtered})
        path = data.get("path")
        if (
            not isinstance(path, str)
            or not path
            or data.get("ok") is not True
            or not isinstance(data.get("restart_required"), bool)
            or not isinstance(data.get("backup_created", False), bool)
        ):
            raise DockerAgentError("Docker Agent 返回了无法识别的配置更新结果")
        return ConfigurationUpdate(
            path=path,
            restart_required=data["restart_required"],
            backup_created=bool(data.get("backup_created", False)),
        )

    async def list_configuration_backups(
        self,
        environment: str,
    ) -> list[ConfigurationBackup]:
        del environment
        data = await self._request("GET", "/v1/config/backups")
        items = data.get("items")
        if not isinstance(items, list) or len(items) > 100:
            raise DockerAgentError("Docker Agent 返回了无法识别的配置备份列表")
        parsed: list[ConfigurationBackup] = []
        for item in items:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("backup_id"), str)
                or not isinstance(item.get("created_at"), str)
                or not isinstance(item.get("size"), int)
                or len(item["backup_id"]) > 255
                or item["size"] < 0
            ):
                raise DockerAgentError("Docker Agent 返回了无法识别的配置备份")
            parsed.append(
                ConfigurationBackup(
                    backup_id=item["backup_id"],
                    created_at=item["created_at"],
                    size=item["size"],
                )
            )
        return parsed

    async def restore_configuration(
        self,
        environment: str,
        backup_id: str,
    ) -> ConfigurationUpdate:
        del environment
        data = await self._request(
            "POST",
            "/v1/config/restore",
            {"backup_id": backup_id},
        )
        path = data.get("path")
        if (
            not isinstance(path, str)
            or not path
            or data.get("ok") is not True
            or not isinstance(data.get("restart_required"), bool)
            or not isinstance(data.get("backup_created", False), bool)
        ):
            raise DockerAgentError("Docker Agent 返回了无法识别的配置还原结果")
        return ConfigurationUpdate(
            path=path,
            restart_required=data["restart_required"],
            backup_created=bool(data.get("backup_created", False)),
        )

    async def manage(self, request: PackageRequest, timeout: int) -> PackageOperation:
        del timeout
        self_update = (
            request.action == "update"
            and request.module_name == "nonebot_plugin_mimo_console"
            and request.project_name == "nonebot-plugin-mimo-console"
        )
        data = await self._request(
            "POST",
            "/v1/operations",
            {
                "action": request.action,
                "module_name": request.module_name,
                "project_name": request.project_name,
                # The Agent resolves update/uninstall sources from its persisted
                # project record. Only an install or the dedicated, allowlisted
                # Mimo Console self-update may introduce a repository URL.
                "repository_url": (
                    request.repository_url if request.action == "install" or self_update else ""
                ),
            },
        )
        return _operation_from_payload(data)

    async def get_operation(self, operation_id: str) -> PackageOperation | None:
        try:
            data = await self._request("GET", f"/v1/operations/{operation_id}")
        except DockerAgentError as exc:
            if str(exc) == "操作不存在":
                return None
            raise
        return _operation_from_payload(data)

    async def list_operations(self) -> list[PackageOperation]:
        data = await self._request("GET", "/v1/operations")
        items = data.get("items")
        if not isinstance(items, list):
            raise DockerAgentError("Docker Agent 返回了无法识别的操作列表")
        return [_operation_from_payload(item) for item in items if isinstance(item, dict)]

    async def rollback(self, operation_id: str) -> PackageOperation:
        data = await self._request("POST", f"/v1/operations/{operation_id}/rollback")
        return _operation_from_payload(data)

    async def restart(self) -> PackageOperation:
        data = await self._request("POST", "/v1/restart")
        return _operation_from_payload(data)
