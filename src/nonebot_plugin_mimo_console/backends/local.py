from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import MutableMapping, MutableSequence
from contextlib import suppress
from pathlib import Path
from typing import Any

import tomlkit

from .base import PackageAction, PackageOperation, PackageRequest

SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _uv_environment_args(project_root: Path) -> list[str] | None:
    """Return uv flags only when uv will sync the interpreter running NoneBot."""
    executable = Path(sys.executable).resolve()
    project_environment = (project_root / ".venv").resolve()
    if _inside(executable, project_environment):
        return ["--python", sys.executable]
    active_raw = os.environ.get("VIRTUAL_ENV", "").strip()
    if active_raw:
        active_environment = Path(active_raw).expanduser().resolve()
        if _inside(executable, active_environment):
            return ["--active", "--python", sys.executable]
    return None


def _register_plugin(project_root: Path, project_name: str, module_name: str) -> None:
    path = project_root / "pyproject.toml"
    if not path.is_file():
        return
    document = tomlkit.parse(path.read_text(encoding="utf-8"))
    tool = document.setdefault("tool", tomlkit.table())
    nonebot = tool.setdefault("nonebot", tomlkit.table())
    plugins = nonebot.get("plugins")
    if isinstance(plugins, MutableSequence):
        if module_name not in plugins:
            plugins.append(module_name)
    elif isinstance(plugins, MutableMapping):
        modules = plugins.get(project_name)
        if modules is None:
            plugins[project_name] = [module_name]
        elif not isinstance(modules, MutableSequence):
            raise ValueError(f"插件清单 {project_name} 不是数组")
        elif module_name not in modules:
            modules.append(module_name)
    else:
        nonebot["plugins"] = [module_name]
    temporary = path.with_name(path.name + ".mimo.tmp")
    temporary.write_text(tomlkit.dumps(document), encoding="utf-8")
    temporary.replace(path)


def _set_source_record(
    project_root: Path,
    module_name: str,
    project_name: str,
    repository_url: str,
) -> None:
    # Mirror the Docker agent's [tool.mimo_console.source_plugins] record so the
    # plugin detail page exposes the same update/uninstall lifecycle regardless
    # of deployment mode.
    path = project_root / "pyproject.toml"
    if not path.is_file() or not module_name:
        return
    document = tomlkit.parse(path.read_text(encoding="utf-8"))
    tool = document.setdefault("tool", tomlkit.table())
    mimo = tool.setdefault("mimo_console", tomlkit.table())
    records = mimo.setdefault("source_plugins", tomlkit.table())
    if not isinstance(records, MutableMapping):
        return
    record = tomlkit.inline_table()
    record["project"] = project_name
    record["repository"] = repository_url
    record["dependencies"] = []
    records[module_name] = record
    temporary = path.with_name(path.name + ".mimo.tmp")
    temporary.write_text(tomlkit.dumps(document), encoding="utf-8")
    temporary.replace(path)


def _remove_source_record(project_root: Path, module_name: str) -> None:
    path = project_root / "pyproject.toml"
    if not path.is_file() or not module_name:
        return
    document = tomlkit.parse(path.read_text(encoding="utf-8"))
    records = document.get("tool", {}).get("mimo_console", {}).get("source_plugins", {})
    if not isinstance(records, MutableMapping) or module_name not in records:
        return
    del records[module_name]
    temporary = path.with_name(path.name + ".mimo.tmp")
    temporary.write_text(tomlkit.dumps(document), encoding="utf-8")
    temporary.replace(path)


def _unregister_plugin(project_root: Path, project_name: str, module_name: str) -> None:
    path = project_root / "pyproject.toml"
    if not path.is_file():
        return
    document = tomlkit.parse(path.read_text(encoding="utf-8"))
    plugins = document.get("tool", {}).get("nonebot", {}).get("plugins")
    if isinstance(plugins, MutableSequence):
        while module_name in plugins:
            plugins.remove(module_name)
    elif isinstance(plugins, MutableMapping):
        modules = plugins.get(project_name)
        if isinstance(modules, MutableSequence):
            while module_name in modules:
                modules.remove(module_name)
            if not modules and project_name != "@local":
                del plugins[project_name]
    temporary = path.with_name(path.name + ".mimo.tmp")
    temporary.write_text(tomlkit.dumps(document), encoding="utf-8")
    temporary.replace(path)


def build_nb_command(
    project_root: Path,
    action: PackageAction,
    project_name: str,
) -> list[str]:
    if action not in {"install", "update", "uninstall"}:
        raise ValueError("不支持的软件包操作")
    if not SAFE_NAME_RE.fullmatch(project_name):
        raise ValueError("插件包名不合法")
    return [
        sys.executable,
        "-m",
        "nb_cli",
        "--cwd",
        str(project_root),
        "--python",
        sys.executable,
        "--no-venv",
        "plugin",
        action,
        project_name,
    ]


def build_dependency_command(
    project_root: Path,
    action: PackageAction,
    project_name: str,
) -> list[str]:
    if action not in {"install", "update", "uninstall"}:
        raise ValueError("不支持的软件包操作")
    if not SAFE_NAME_RE.fullmatch(project_name):
        raise ValueError("依赖包名不合法")
    uv = shutil.which("uv")
    uv_environment = _uv_environment_args(project_root)
    if uv and uv_environment is not None and (project_root / "pyproject.toml").is_file():
        if action == "install":
            return [uv, "add", *uv_environment, project_name]
        if action == "update":
            return [
                uv,
                "add",
                *uv_environment,
                project_name,
                "--upgrade-package",
                project_name,
            ]
        return [uv, "remove", *uv_environment, project_name]
    if action == "uninstall":
        return [sys.executable, "-m", "pip", "uninstall", "-y", project_name]
    command = [sys.executable, "-m", "pip", "install"]
    if action == "update":
        command.append("--upgrade")
    command.append(project_name)
    return command


def build_github_command(
    project_root: Path,
    action: PackageAction,
    project_name: str,
    repository_url: str,
) -> list[str]:
    if action not in {"install", "update", "uninstall"}:
        raise ValueError("不支持的软件包操作")
    if not SAFE_NAME_RE.fullmatch(project_name):
        raise ValueError("插件包名不合法")
    requirement = f"git+{repository_url}"
    uv = shutil.which("uv")
    uv_environment = _uv_environment_args(project_root)
    if uv and uv_environment is not None and (project_root / "pyproject.toml").is_file():
        if action == "uninstall":
            return [uv, "remove", *uv_environment, project_name]
        command = [uv, "add", *uv_environment, requirement]
        if action == "update":
            command.extend(["--upgrade-package", project_name])
        return command
    if action == "uninstall":
        return [sys.executable, "-m", "pip", "uninstall", "-y", project_name]
    command = [sys.executable, "-m", "pip", "install"]
    if action == "update":
        command.append("--upgrade")
    command.append(requirement)
    return command


def clean_output(raw: bytes, limit: int = 6000) -> str:
    text = raw.decode("utf-8", errors="replace")
    text = ANSI_RE.sub("", text).replace("\r\n", "\n").strip()
    text = re.sub(r"(https?://)[^/@\s]+@", r"\1***@", text)
    return text[-limit:]


class LocalPackageBackend:
    """Current-process nb-cli backend used by non-Docker deployments."""

    def __init__(self, deployment_mode: str = "python") -> None:
        self._operations: dict[str, PackageOperation] = {}
        self.deployment_mode = deployment_mode

    async def capabilities(self) -> dict[str, Any]:
        return {
            "mode": self.deployment_mode,
            "available": True,
            "persistent_image": False,
            "rollback": False,
            "github_install": shutil.which("git") is not None,
            "containerized": self.deployment_mode == "docker-local",
        }

    async def manage(self, request: PackageRequest, timeout: int) -> PackageOperation:
        operation_id = f"local-{uuid.uuid4().hex}"
        now = time.time()
        operation = PackageOperation(
            operation_id=operation_id,
            action=request.action,
            module_name=request.module_name,
            project_name=request.project_name,
            status="preparing",
            created_at=now,
            updated_at=now,
        )
        self._operations[operation_id] = operation
        if importlib.util.find_spec("nb_cli") is None:
            operation.status = "failed"
            operation.error = "当前环境缺少 nb-cli，请重新安装本插件后再试"
            operation.updated_at = time.time()
            return operation
        command = (
            build_github_command(
                request.project_root,
                request.action,
                request.project_name,
                request.repository_url,
            )
            if request.repository_url
            else build_nb_command(
                request.project_root,
                request.action,
                request.project_name,
            )
            if request.module_name
            else build_dependency_command(
                request.project_root,
                request.action,
                request.project_name,
            )
        )
        env = os.environ.copy()
        env.update({"NO_COLOR": "1", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=request.project_root,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **kwargs,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            operation.status = "failed"
            operation.error = f"软件包操作超过 {timeout} 秒，已终止"
            operation.updated_at = time.time()
            return operation
        cleaned = clean_output(output)
        operation.output = cleaned
        operation.updated_at = time.time()
        if process.returncode != 0:
            operation.status = "failed"
            operation.error = cleaned or f"nb-cli 执行失败（{process.returncode}）"
            return operation
        if request.repository_url and request.action in {"install", "update"}:
            try:
                if request.action == "install":
                    await asyncio.to_thread(
                        _register_plugin,
                        request.project_root,
                        request.project_name,
                        request.module_name,
                    )
                await asyncio.to_thread(
                    _set_source_record,
                    request.project_root,
                    request.module_name,
                    request.project_name,
                    request.repository_url,
                )
            except (OSError, ValueError, TypeError) as exc:
                operation.status = "failed"
                operation.error = f"插件已安装，但写入 NoneBot 插件配置失败：{exc}"
                return operation
        elif request.repository_url and request.action == "uninstall" and request.module_name:
            try:
                await asyncio.to_thread(
                    _unregister_plugin,
                    request.project_root,
                    request.project_name,
                    request.module_name,
                )
                await asyncio.to_thread(
                    _remove_source_record,
                    request.project_root,
                    request.module_name,
                )
            except (OSError, ValueError, TypeError) as exc:
                operation.status = "failed"
                operation.error = f"插件包已卸载，但清理 NoneBot 插件配置失败：{exc}"
                return operation
        elif request.action == "uninstall" and request.module_name:
            with suppress(OSError, ValueError, TypeError):
                await asyncio.to_thread(
                    _remove_source_record,
                    request.project_root,
                    request.module_name,
                )
        operation.status = "succeeded"
        operation.restart_required = True
        return operation

    async def get_operation(self, operation_id: str) -> PackageOperation | None:
        return self._operations.get(operation_id)

    async def list_operations(self) -> list[PackageOperation]:
        return sorted(
            self._operations.values(),
            key=lambda item: item.created_at,
            reverse=True,
        )

    async def rollback(self, operation_id: str) -> PackageOperation:
        del operation_id
        raise ValueError("本地包管理模式不支持自动回滚")

    async def restart(self) -> PackageOperation:
        raise ValueError("本地包管理模式由当前进程执行重启")
