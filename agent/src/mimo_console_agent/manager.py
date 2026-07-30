from __future__ import annotations

import ast
import asyncio
import hashlib
import hmac
import json
import os
import re
import shutil
import sys
import time
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlparse

import httpx
import tomlkit
from packaging.requirements import InvalidRequirement, Requirement
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, TaggedScalar
from ruamel.yaml.error import YAMLError
from ruamel.yaml.scalarstring import DoubleQuotedScalarString
from tomlkit.exceptions import ParseError

from .config import AgentConfig, InstanceConfig, _inside
from .environment import (
    EnvironmentError,
    list_environment_backups,
    read_environment,
    restore_environment,
    update_environment,
)
from .models import NON_TERMINAL_STATUSES, Operation, PackageAction
from .runner import CommandError, run_command
from .storage import OperationStore

SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SAFE_MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")
GITHUB_OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
GITHUB_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
SOURCE_PLUGIN_DIR = "local_plugins"
SELF_UPDATE_MODULE = "nonebot_plugin_mimo_console"
SELF_UPDATE_PROJECT = "nonebot-plugin-mimo-console"
SELF_UPDATE_REPOSITORY = "https://github.com/MimoKit/nonebot-plugin-mimo-console.git"
# Rollback snapshots (pyproject.toml + uv.lock + override) are kept per operation
# so terminal operations can still be rolled back manually. Bound their number so
# they cannot grow without limit across many deployments.
SNAPSHOT_KEEP = 50
IMPORT_DISTRIBUTION_OVERRIDES = {
    "PIL": "pillow",
    "Crypto": "pycryptodome",
    "OpenSSL": "pyopenssl",
    "bs4": "beautifulsoup4",
    "cv2": "opencv-python",
    "dateutil": "python-dateutil",
    "dotenv": "python-dotenv",
    "magic": "python-magic",
    "sklearn": "scikit-learn",
    "yaml": "pyyaml",
}


class AgentError(RuntimeError):
    pass


def _canonical_github_repository(value: str) -> str:
    try:
        parsed = urlparse(value.strip())
        port = parsed.port
    except ValueError as exc:
        raise AgentError("GitHub 仓库地址不合法") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"github.com", "www.github.com"}
        or port is not None
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise AgentError("只支持不含凭据、参数或片段的 GitHub HTTPS 仓库地址")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2:
        raise AgentError("GitHub 地址必须指向仓库根目录")
    owner, repository = parts
    if repository.endswith(".git"):
        repository = repository[:-4]
    if (
        not GITHUB_OWNER_RE.fullmatch(owner)
        or not GITHUB_REPOSITORY_RE.fullmatch(repository)
        or repository in {".", ".."}
    ):
        raise AgentError("GitHub 仓库所有者或仓库名不合法")
    return f"https://github.com/{owner}/{repository}.git"


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".mimo.tmp")
    shutil.copy2(source, temporary)
    os.replace(temporary, destination)


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".mimo.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _image_override(service: str, image: str) -> str:
    return f"services:\n  {service}:\n    image: {json.dumps(image)}\n    build: !reset null\n"


def _helper_container_name(operation_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", operation_id).strip("-.")
    return f"mimo-agent-helper-{safe[:48] or 'unknown'}"


def _update_image_override(path: Path, service: str, image: str) -> None:
    """Update only the managed image while preserving administrator-owned fields."""
    if not path.is_file():
        _write_text(path, _image_override(service, image))
        return
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    try:
        document = yaml.load(path.read_text(encoding="utf-8"))
    except (OSError, YAMLError) as exc:
        raise AgentError(f"无法解析 Compose override：{exc}") from exc
    if not isinstance(document, CommentedMap):
        raise AgentError("Compose override 顶层必须是 YAML 对象")
    services = document.get("services")
    if not isinstance(services, CommentedMap):
        raise AgentError("Compose override 缺少 services 对象")
    service_config = services.get(service)
    if not isinstance(service_config, CommentedMap):
        raise AgentError(f"Compose override 缺少服务：{service}")
    service_config["image"] = DoubleQuotedScalarString(image)
    service_config["build"] = TaggedScalar("null", tag="!reset")
    temporary = path.with_name(path.name + ".mimo.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            yaml.dump(document, stream)
        shutil.copymode(path, temporary)
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _update_plugin_list(
    path: Path,
    module_name: str,
    project_name: str,
    action: PackageAction,
) -> None:
    document = tomlkit.parse(path.read_text(encoding="utf-8"))
    tool = document.setdefault("tool", tomlkit.table())
    nonebot = tool.setdefault("nonebot", tomlkit.table())
    plugins = nonebot.get("plugins")
    if plugins is None:
        plugins = tomlkit.table()
        plugins.add("@local", tomlkit.array())
        nonebot["plugins"] = plugins
    if isinstance(plugins, list):
        values = [str(item) for item in plugins]
        if action in {"install", "update"} and module_name not in values:
            plugins.append(module_name)
        elif action == "uninstall":
            while module_name in values:
                index = values.index(module_name)
                del plugins[index]
                del values[index]
    elif isinstance(plugins, dict):
        modules = plugins.get(project_name)
        if modules is None and action in {"install", "update"}:
            modules = tomlkit.array()
            plugins[project_name] = modules
        if not isinstance(modules, list) and modules is not None:
            raise AgentError(f"插件清单 {project_name} 不是数组")
        if isinstance(modules, list):
            values = [str(item) for item in modules]
            if action in {"install", "update"} and module_name not in values:
                modules.append(module_name)
            elif action == "uninstall":
                while module_name in values:
                    index = values.index(module_name)
                    del modules[index]
                    del values[index]
                if not modules and project_name != "@local":
                    del plugins[project_name]
    else:
        raise AgentError("[tool.nonebot].plugins 格式不受支持")
    path.write_text(tomlkit.dumps(document), encoding="utf-8")


def _update_source_plugin_dir(path: Path) -> None:
    document = tomlkit.parse(path.read_text(encoding="utf-8"))
    tool = document.setdefault("tool", tomlkit.table())
    nonebot = tool.setdefault("nonebot", tomlkit.table())
    plugin_dirs = nonebot.get("plugin_dirs")
    if plugin_dirs is None:
        plugin_dirs = tomlkit.array()
        nonebot["plugin_dirs"] = plugin_dirs
    if not isinstance(plugin_dirs, list):
        raise AgentError("[tool.nonebot].plugin_dirs 格式不受支持")
    values = [str(item) for item in plugin_dirs]
    if SOURCE_PLUGIN_DIR not in values:
        plugin_dirs.append(SOURCE_PLUGIN_DIR)
    path.write_text(tomlkit.dumps(document), encoding="utf-8")


def _source_plugin_record(
    path: Path,
    module_name: str,
    project_name: str,
) -> dict[str, object] | None:
    document = tomlkit.parse(path.read_text(encoding="utf-8"))
    records = document.get("tool", {}).get("mimo_console", {}).get("source_plugins", {})
    if not isinstance(records, dict):
        return None
    candidates = []
    if module_name:
        candidates.append(records.get(module_name))
    candidates.extend(
        value
        for value in records.values()
        if isinstance(value, dict) and str(value.get("project") or "") == project_name
    )
    for value in candidates:
        if isinstance(value, dict):
            return {str(key): item for key, item in value.items()}
    return None


def _set_source_plugin_record(
    path: Path,
    module_name: str,
    project_name: str,
    repository_url: str,
    dependencies: list[str],
    *,
    managed_additions: list[str] | None = None,
    managed_removals: list[str] | None = None,
) -> None:
    document = tomlkit.parse(path.read_text(encoding="utf-8"))
    tool = document.setdefault("tool", tomlkit.table())
    mimo = tool.setdefault("mimo_console", tomlkit.table())
    records = mimo.setdefault("source_plugins", tomlkit.table())
    if not isinstance(records, dict):
        raise AgentError("[tool.mimo_console].source_plugins 格式不受支持")
    record = tomlkit.inline_table()
    record["project"] = project_name
    record["repository"] = repository_url
    record["dependencies"] = dependencies
    records[module_name] = record
    managed = mimo.setdefault("source_managed_dependencies", tomlkit.array())
    if not isinstance(managed, list):
        raise AgentError("[tool.mimo_console].source_managed_dependencies 格式不受支持")
    values = {
        _requirement_key(str(item)): str(item) for item in managed if _requirement_key(str(item))
    }
    for item in managed_removals or []:
        values.pop(_requirement_key(item), None)
    for item in managed_additions or []:
        key = _requirement_key(item)
        if key:
            values[key] = key
    managed.clear()
    managed.extend(values[key] for key in sorted(values))
    path.write_text(tomlkit.dumps(document), encoding="utf-8")


def _remove_source_plugin_record(
    path: Path,
    module_name: str,
    managed_removals: list[str] | None = None,
) -> None:
    document = tomlkit.parse(path.read_text(encoding="utf-8"))
    mimo = document.get("tool", {}).get("mimo_console", {})
    records = mimo.get("source_plugins", {})
    if isinstance(records, dict):
        records.pop(module_name, None)
    managed = mimo.get("source_managed_dependencies", [])
    if isinstance(managed, list) and managed_removals:
        removed = {_requirement_key(item) for item in managed_removals}
        retained = [str(item) for item in managed if _requirement_key(str(item)) not in removed]
        managed.clear()
        managed.extend(retained)
    path.write_text(tomlkit.dumps(document), encoding="utf-8")


def _requirement_key(requirement: str) -> str:
    # Normalize a PEP 508 requirement to its bare distribution name for comparison.
    name = re.split(r"[\s\[<>=!~;@]", requirement, maxsplit=1)[0]
    return re.sub(r"[-_.]+", "-", name).casefold()


def _project_dependency_keys(path: Path) -> set[str]:
    document = tomlkit.parse(path.read_text(encoding="utf-8"))
    dependencies = document.get("project", {}).get("dependencies", [])
    if not isinstance(dependencies, list):
        return set()
    return {key for item in dependencies if (key := _requirement_key(str(item)))}


def _orphaned_source_dependencies(
    path: Path,
    module_name: str,
    replacement_dependencies: list[str] | None = None,
) -> list[str]:
    # Return the dependencies declared by `module_name`'s source-plugin record that
    # are no longer referenced by any other source-plugin record, so an uninstall
    # can `uv remove` them instead of leaving them behind. Direct project
    # dependencies (declared in [project].dependencies) are never removed here.
    document = tomlkit.parse(path.read_text(encoding="utf-8"))
    records = document.get("tool", {}).get("mimo_console", {}).get("source_plugins", {})
    if not isinstance(records, dict):
        return []
    own = records.get(module_name)
    if not isinstance(own, dict):
        return []
    own_deps = [str(item).strip() for item in own.get("dependencies", []) if str(item).strip()]
    if not own_deps:
        return []
    managed = {
        _requirement_key(str(item))
        for item in document.get("tool", {})
        .get("mimo_console", {})
        .get("source_managed_dependencies", [])
        if _requirement_key(str(item))
    }
    still_referenced = {
        _requirement_key(item) for item in replacement_dependencies or [] if _requirement_key(item)
    }
    for name, record in records.items():
        if name == module_name or not isinstance(record, dict):
            continue
        for item in record.get("dependencies", []):
            still_referenced.add(_requirement_key(str(item)))
    orphaned: list[str] = []
    seen: set[str] = set()
    for requirement in own_deps:
        key = _requirement_key(requirement)
        if key and key in managed and key not in still_referenced and key not in seen:
            seen.add(key)
            orphaned.append(key)
    return orphaned


def _assert_no_symlinks(root: Path) -> None:
    # The repository is cloned by an untrusted third party. copytree(symlinks=False)
    # would follow any symlink and copy the *target's* content into the project and
    # image, so a plugin shipping `__init__.py -> /etc/hostname` could exfiltrate
    # host files. Reject the whole tree if any entry (dir or file) is a symlink.
    if root.is_symlink():
        raise AgentError(f"源码插件包含符号链接，已拒绝：{root.name}")
    for child in root.rglob("*"):
        if child.is_symlink():
            raise AgentError(f"源码插件包含符号链接，已拒绝：{child.relative_to(root).as_posix()}")


def _find_source_plugin(repository: Path, requested: str) -> tuple[Path, str]:
    candidates: list[tuple[Path, str]] = []
    for base in (repository, repository / "src"):
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if (
                child.is_dir()
                and not child.is_symlink()
                and child.name.startswith("nonebot_plugin_")
                and (child / "__init__.py").is_file()
            ):
                candidates.append((child, child.name))
    matching = [candidate for candidate in candidates if candidate[1] == requested]
    if len(matching) == 1:
        return matching[0]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise AgentError(
            "仓库根目录或 src 目录中未识别到 nonebot_plugin_* 插件包；请在安装窗口指定正确的导入名"
        )
    names = "、".join(candidate[1] for candidate in candidates)
    raise AgentError(f"仓库包含多个 NoneBot 插件包（{names}）；请指定要安装的导入名")


def _require_plain_dependency(requirement: str, origin: str) -> str:
    # Reject anything uv could reinterpret as a flag (--default-index, -e ...) or
    # as a local/VCS/URL reference. Only plain PyPI requirements are allowed to
    # reach `uv add`, since the resolver executes build backends from untrusted
    # third-party repositories.
    try:
        parsed = Requirement(requirement)
    except InvalidRequirement as exc:
        raise AgentError(f"源码插件 {origin} 的依赖格式不合法：{requirement}") from exc
    if parsed.url is not None:
        raise AgentError(f"源码插件 {origin} 仅支持普通 PyPI 依赖，不支持该项：{requirement}")
    return requirement


def _declared_source_dependencies(repository: Path, module_dir: Path) -> list[str]:
    dependencies: list[str] = []
    roots = list(dict.fromkeys((repository, module_dir)))
    for root in roots:
        pyproject = root / "pyproject.toml"
        if pyproject.is_file() and not pyproject.is_symlink():
            try:
                document = tomlkit.parse(pyproject.read_text(encoding="utf-8"))
                declared = document.get("project", {}).get("dependencies", [])
            except (OSError, ParseError) as exc:
                raise AgentError(f"无法解析源码插件依赖文件 {pyproject.name}：{exc}") from exc
            if isinstance(declared, list):
                for item in declared:
                    line = str(item).strip()
                    if line:
                        dependencies.append(_require_plain_dependency(line, pyproject.name))
        requirements = root / "requirements.txt"
        if requirements.is_file() and not requirements.is_symlink():
            for raw in requirements.read_text(encoding="utf-8-sig").splitlines():
                line = re.sub(r"\s+#.*$", "", raw).strip()
                if not line or line.startswith("#"):
                    continue
                dependencies.append(_require_plain_dependency(line, requirements.name))
    return dependencies


def _inferred_source_dependencies(module_dir: Path) -> list[str]:
    imported: set[str] = set()
    for source in module_dir.rglob("*.py"):
        if source.is_symlink():
            continue
        try:
            tree = ast.parse(source.read_text(encoding="utf-8-sig"))
        except (OSError, SyntaxError, UnicodeError) as exc:
            raise AgentError(f"无法静态解析源码插件文件 {source.name}：{exc}") from exc
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".", 1)[0])

    ignored = set(sys.stdlib_module_names)
    ignored.update({"nonebot", module_dir.name})
    dependencies: list[str] = []
    for name in sorted(imported - ignored, key=str.casefold):
        if name.startswith("nonebot_plugin_"):
            distribution = name.replace("_", "-")
        else:
            distribution = IMPORT_DISTRIBUTION_OVERRIDES.get(name, name.replace("_", "-"))
        # Private/dunder modules (e.g. `_cffi_backend`) map to invalid PyPI names;
        # drop them rather than let them reach `uv add`.
        try:
            dependencies.append(_require_plain_dependency(distribution, module_dir.name))
        except AgentError:
            continue
    return dependencies


def _deduplicate_dependencies(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = re.split(r"[\s\[<>=!~@]", value, maxsplit=1)[0]
        key = re.sub(r"[-_.]+", "-", key).casefold()
        if key and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _atomic_replace_tree(source: Path, destination: Path) -> None:
    _assert_no_symlinks(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".mimo.tmp")
    if temporary.exists():
        shutil.rmtree(temporary)
    shutil.copytree(source, temporary, symlinks=False)
    if destination.exists():
        shutil.rmtree(destination)
    os.replace(temporary, destination)


class DeploymentManager:
    def __init__(self, config: AgentConfig, store: OperationStore) -> None:
        self.config = config
        self.store = store
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._locks = {instance_id: asyncio.Lock() for instance_id in config.instances}

    def authenticate(self, instance_id: str, token: str) -> InstanceConfig:
        instance = self.config.instances.get(instance_id)
        if instance is None:
            raise AgentError("认证失败")
        try:
            expected = instance.token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise AgentError("实例令牌不可用") from exc
        # Compare on bytes: Starlette decodes headers as latin-1, so a non-ASCII
        # token would make hmac.compare_digest raise TypeError (→ 500) instead of
        # cleanly failing auth.
        if not expected or not hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
            raise AgentError("认证失败")
        return instance

    @staticmethod
    def _environment_display_path(instance: InstanceConfig) -> str:
        return str(instance.environment_file.relative_to(instance.project_root))

    async def read_configuration(self, instance: InstanceConfig) -> dict[str, object]:
        try:
            entries = await asyncio.to_thread(
                read_environment,
                instance.environment_file,
            )
        except (OSError, EnvironmentError) as exc:
            raise AgentError(str(exc)) from exc
        return {
            "path": self._environment_display_path(instance),
            "items": [entry.public_dict() for entry in entries],
        }

    async def update_configuration(
        self,
        instance: InstanceConfig,
        values: dict[str, object],
    ) -> dict[str, object]:
        lock = self._locks[instance.instance_id]
        if lock.locked() or self.store.active(instance.instance_id):
            raise AgentError("当前实例已有操作正在执行")
        async with lock:
            try:
                backup = await asyncio.to_thread(
                    update_environment,
                    instance.environment_file,
                    values,
                    self.config.state_dir / "environment-backups" / instance.instance_id,
                    instance.environment_backup_keep,
                )
            except (OSError, EnvironmentError) as exc:
                raise AgentError(str(exc)) from exc
        return {
            "ok": True,
            "restart_required": True,
            "path": self._environment_display_path(instance),
            "backup_created": bool(backup),
        }

    async def list_configuration_backups(
        self,
        instance: InstanceConfig,
    ) -> dict[str, object]:
        backup_dir = self.config.state_dir / "environment-backups" / instance.instance_id
        items = await asyncio.to_thread(
            list_environment_backups,
            instance.environment_file,
            backup_dir,
        )
        return {"items": items}

    async def restore_configuration(
        self,
        instance: InstanceConfig,
        backup_id: str,
    ) -> dict[str, object]:
        lock = self._locks[instance.instance_id]
        if lock.locked() or self.store.active(instance.instance_id):
            raise AgentError("当前实例已有操作正在执行")
        async with lock:
            backup_dir = self.config.state_dir / "environment-backups" / instance.instance_id
            try:
                safety_backup = await asyncio.to_thread(
                    restore_environment,
                    instance.environment_file,
                    backup_dir,
                    backup_id,
                    instance.environment_backup_keep,
                )
            except (OSError, EnvironmentError) as exc:
                raise AgentError(str(exc)) from exc
        return {
            "ok": True,
            "restart_required": True,
            "path": self._environment_display_path(instance),
            "backup_created": bool(safety_backup),
        }

    async def submit(
        self,
        instance: InstanceConfig,
        action: PackageAction,
        module_name: str,
        project_name: str,
        repository_url: str = "",
    ) -> Operation:
        if action not in {"install", "update", "uninstall"}:
            raise AgentError("不支持的软件包操作")
        if (
            module_name and not SAFE_MODULE_RE.fullmatch(module_name)
        ) or not SAFE_NAME_RE.fullmatch(project_name):
            raise AgentError("软件包名称不合法")
        if repository_url:
            repository_url = _canonical_github_repository(repository_url)
            self_update = (
                action == "update"
                and module_name == SELF_UPDATE_MODULE
                and project_name == SELF_UPDATE_PROJECT
                and repository_url == SELF_UPDATE_REPOSITORY
            )
            if action != "install" and not self_update:
                raise AgentError("GitHub 仓库地址只能用于安装或受信任的控制台自更新")
            if not module_name:
                raise AgentError("GitHub 仓库安装必须指定插件导入名")
        if self.store.active(instance.instance_id):
            raise AgentError("当前实例已有软件包操作正在执行")
        operation = self.store.save(
            Operation.create(
                instance.instance_id,
                action,
                module_name,
                project_name,
                repository_url,
            )
        )
        task = asyncio.create_task(self._run(operation, instance))
        self._tasks[operation.operation_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(operation.operation_id, None))
        return operation

    async def submit_restart(self, instance: InstanceConfig) -> Operation:
        if self.store.active(instance.instance_id):
            raise AgentError("当前实例已有操作正在执行")
        operation = self.store.save(Operation.create(instance.instance_id, "restart", "", ""))
        task = asyncio.create_task(self._run_restart(operation, instance))
        self._tasks[operation.operation_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(operation.operation_id, None))
        return operation

    async def recover(self) -> None:
        for operation in self.store.interrupted():
            interruption = "Agent 重启中断了操作"
            self.store.fail_running_steps(operation, interruption)
            instance = self.config.instances.get(operation.instance_id)
            if instance is None:
                operation.status = "failed"
                operation.error = "Agent 重启后找不到实例配置"
                self.store.save(operation)
                continue
            await self._remove_helper_container(
                _helper_container_name(operation.operation_id),
                instance.project_root,
            )
            try:
                if operation.status in {"deploying", "health_checking", "rolling_back"}:
                    if operation.action == "restart":
                        await self._recover_restart(
                            operation,
                            instance,
                            "Agent 重启后自动恢复容器",
                        )
                    else:
                        await self._rollback(operation, instance, "Agent 重启后自动回滚")
                else:
                    operation.status = "failed"
                    operation.error = "Agent 重启中断了尚未部署的操作，原容器未变更"
                    self.store.save(operation)
            except Exception as exc:
                operation.status = "failed"
                operation.error = f"Agent 重启后回滚失败：{exc}"
                self.store.save(operation)
                if operation.action != "restart" and operation.snapshot_dir:
                    self.store.set_deployment_head(
                        instance.instance_id,
                        operation.operation_id,
                    )
            finally:
                shutil.rmtree(
                    self.config.state_dir / "work" / operation.operation_id,
                    ignore_errors=True,
                )

    async def rollback(self, instance: InstanceConfig, operation_id: str) -> Operation:
        operation = self.store.get(operation_id)
        if operation is None or operation.instance_id != instance.instance_id:
            raise AgentError("操作不存在")
        if operation.action == "restart":
            raise AgentError("重启操作不支持手动回滚")
        if operation.status not in {"succeeded", "failed"}:
            raise AgentError("当前操作状态不能回滚")
        if self.store.deployment_head(instance.instance_id) != operation.operation_id:
            raise AgentError("只能回滚当前部署对应的最近一次软件包操作")
        if not operation.old_image or not await self._image_exists(instance, operation.old_image):
            raise AgentError("回滚所需的旧镜像已不存在")
        current_image = await self._current_image(instance)
        expected_images = (
            {operation.new_image}
            if operation.status == "succeeded"
            else {operation.new_image, operation.old_image}
        )
        if current_image not in expected_images:
            raise AgentError("当前运行镜像已变化，不能再回滚这次操作")
        if self.store.active(instance.instance_id):
            raise AgentError("当前实例已有操作正在执行")
        operation.status = "rolling_back"
        self.store.save(operation)
        task = asyncio.create_task(self._run_manual_rollback(operation, instance))
        self._tasks[operation.operation_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(operation.operation_id, None))
        return operation

    async def _run_manual_rollback(
        self,
        operation: Operation,
        instance: InstanceConfig,
    ) -> None:
        async with self._locks[instance.instance_id]:
            try:
                await self._rollback(operation, instance, "管理员请求回滚")
                self.store.clear_deployment_head(
                    instance.instance_id,
                    operation.operation_id,
                )
            except Exception as exc:
                self.store.fail_running_steps(operation, str(exc))
                operation.status = "failed"
                operation.error = f"手动回滚失败：{exc}"
                self.store.save(operation)

    async def _run(self, operation: Operation, instance: InstanceConfig) -> None:
        async with self._locks[instance.instance_id]:
            work = self.config.state_dir / "work" / operation.operation_id
            snapshot = self.config.state_dir / "snapshots" / operation.operation_id
            deployed = False
            committed = False
            try:
                operation.status = "preparing"
                operation.snapshot_dir = str(snapshot)
                self.store.step(operation, "prepare", "running")
                self._prepare(instance, work, snapshot)
                operation.old_image = await self._current_image(instance)
                await self._preflight(instance, work, operation.old_image)
                self.store.step(operation, "prepare", "success")

                operation.status = "locking"
                self.store.step(operation, "lock", "running")
                output = await self._change_dependencies(operation, instance, work)
                self.store.append_output(operation, output)
                self.store.step(operation, "lock", "success")

                operation.status = "building"
                self.store.step(operation, "build", "running")
                operation.new_image = self._image_name(
                    instance,
                    work,
                    operation.operation_id,
                )
                self.store.save(operation)
                output = await self._build(instance, work, operation.new_image)
                self.store.append_output(operation, output)
                self.store.step(operation, "build", "success")

                operation.status = "verifying"
                self.store.step(operation, "verify", "running")
                output = await self._verify(operation, instance)
                self.store.append_output(operation, output)
                self.store.step(operation, "verify", "success")

                # Persist the destructive boundary before touching the live project.
                # If the Agent is interrupted after this point, startup recovery must
                # restore the snapshot even when Compose has not returned yet.
                operation.status = "deploying"
                self.store.step(operation, "deploy", "running")
                committed = True
                self._commit_project(instance, work, operation)
                _update_image_override(
                    instance.override_file,
                    instance.service,
                    operation.new_image,
                )

                output = await self._compose_up(instance)
                deployed = True
                self.store.append_output(operation, output)
                self.store.step(operation, "deploy", "success")

                operation.status = "health_checking"
                self.store.step(operation, "health", "running")
                await self._wait_healthy(instance)
                self.store.step(operation, "health", "success")
                operation.status = "succeeded"
                operation.error = ""
                self.store.save(operation)
                self.store.set_deployment_head(
                    instance.instance_id,
                    operation.operation_id,
                )
                await self._cleanup_images(
                    instance,
                    {operation.old_image, operation.new_image},
                    operation,
                )
            except Exception as exc:
                operation.error = str(exc)
                self.store.fail_running_steps(operation, str(exc))
                self.store.append_output(
                    operation,
                    getattr(exc, "output", "") or "",
                )
                if deployed or committed:
                    try:
                        await self._rollback(operation, instance, str(exc))
                    except Exception as rollback_exc:
                        operation.status = "failed"
                        operation.error = f"{exc}; 自动回滚失败：{rollback_exc}"
                        self.store.save(operation)
                        self.store.set_deployment_head(
                            instance.instance_id,
                            operation.operation_id,
                        )
                else:
                    operation.status = "failed"
                    self.store.save(operation)
            finally:
                shutil.rmtree(work, ignore_errors=True)
                self._prune_snapshots(instance)

    def _prune_snapshots(self, instance: InstanceConfig) -> None:
        # Snapshots are retained after an operation settles so it can still be
        # rolled back manually, but nothing else prunes them. Keep the newest
        # `snapshot_keep` per instance and drop older terminal ones.
        snapshots_root = self.config.state_dir / "snapshots"
        if not snapshots_root.is_dir():
            return
        operations = self.store.list(instance.instance_id, limit=1000)
        keep = {op.operation_id for op in operations[: instance.snapshot_keep]}
        head = self.store.deployment_head(instance.instance_id)
        if head:
            keep.add(head)
        keep.update(op.operation_id for op in operations if op.status in NON_TERMINAL_STATUSES)
        for op in operations[instance.snapshot_keep :]:
            if op.operation_id in keep or not op.snapshot_dir:
                continue
            snapshot = Path(op.snapshot_dir)
            if _inside(snapshot, snapshots_root) and snapshot.is_dir():
                shutil.rmtree(snapshot, ignore_errors=True)

    async def _run_restart(
        self,
        operation: Operation,
        instance: InstanceConfig,
    ) -> None:
        async with self._locks[instance.instance_id]:
            try:
                operation.old_image = await self._current_image(instance)
                if not instance.override_file.is_file():
                    _write_text(
                        instance.override_file,
                        _image_override(instance.service, operation.old_image),
                    )
                operation.status = "deploying"
                self.store.step(operation, "restart", "running")
                output = await self._compose_up(instance)
                self.store.append_output(operation, output)
                operation.status = "health_checking"
                self.store.save(operation)
                await self._wait_healthy(instance)
                operation.status = "succeeded"
                self.store.step(operation, "restart", "success")
                self.store.save(operation)
            except Exception as exc:
                operation.error = str(exc)
                self.store.fail_running_steps(operation, str(exc))
                try:
                    await self._recover_restart(operation, instance, str(exc))
                except Exception as rollback_exc:
                    operation.status = "failed"
                    operation.error = f"{exc}; 自动恢复失败：{rollback_exc}"
                    self.store.save(operation)

    async def _recover_restart(
        self,
        operation: Operation,
        instance: InstanceConfig,
        reason: str,
    ) -> None:
        operation.status = "rolling_back"
        self.store.step(operation, "recovery", "running", reason)
        # Restart does not change the selected image or administrator-owned
        # override fields. Reuse the existing file instead of replacing it with
        # a minimal generated override during failure recovery.
        output = await self._compose_up(instance)
        self.store.append_output(operation, output)
        await self._wait_healthy(instance)
        operation.status = "rolled_back"
        self.store.step(operation, "recovery", "success", reason)
        self.store.save(operation)

    def _prepare(self, instance: InstanceConfig, work: Path, snapshot: Path) -> None:
        if work.exists():
            shutil.rmtree(work)
        work.parent.mkdir(parents=True, exist_ok=True)
        sensitive_paths = {
            path.resolve()
            for path in (instance.environment_file, instance.token_file)
            if _inside(path, instance.project_root)
        }

        def ignore_sensitive(directory: str, names: list[str]) -> set[str]:
            ignored = set(
                shutil.ignore_patterns(
                    ".git",
                    ".venv",
                    "__pycache__",
                    "*.pyc",
                    ".mimo",
                    ".env",
                    ".env.*",
                    "*.key",
                    "*.pem",
                    "*.token",
                    "secrets",
                    ".secrets",
                )(directory, names)
            )
            parent = Path(directory)
            for name in names:
                if (parent / name).resolve() in sensitive_paths:
                    ignored.add(name)
            return ignored

        shutil.copytree(
            instance.project_root,
            work,
            symlinks=True,
            ignore=ignore_sensitive,
        )
        pyproject = work / "pyproject.toml"
        lock = work / "uv.lock"
        if (
            not pyproject.is_file()
            or not lock.is_file()
            or pyproject.is_symlink()
            or lock.is_symlink()
        ):
            raise AgentError("Docker预览版要求项目包含 pyproject.toml 和 uv.lock")
        snapshot.mkdir(parents=True, exist_ok=False)
        shutil.copy2(instance.project_root / "pyproject.toml", snapshot / "pyproject.toml")
        shutil.copy2(instance.project_root / "uv.lock", snapshot / "uv.lock")
        source_root = instance.project_root / SOURCE_PLUGIN_DIR
        (snapshot / "source-plugins.exists").write_text(
            "1" if source_root.is_dir() else "0",
            encoding="ascii",
        )
        if source_root.is_dir():
            _assert_no_symlinks(source_root)
            shutil.copytree(source_root, snapshot / SOURCE_PLUGIN_DIR, symlinks=False)
        if instance.override_file.is_file():
            shutil.copy2(instance.override_file, snapshot / "docker-compose.override.yml")

    async def _change_dependencies(
        self,
        operation: Operation,
        instance: InstanceConfig,
        work: Path,
    ) -> str:
        pyproject = work / "pyproject.toml"
        action = operation.action
        if (
            action == "update"
            and operation.module_name == SELF_UPDATE_MODULE
            and operation.project_name == SELF_UPDATE_PROJECT
            and operation.repository_url == SELF_UPDATE_REPOSITORY
        ):
            return await self._run_uv(
                work,
                [
                    "add",
                    "--no-sync",
                    f"{SELF_UPDATE_PROJECT} @ git+{SELF_UPDATE_REPOSITORY}",
                    "--upgrade-package",
                    SELF_UPDATE_PROJECT,
                ],
                instance.build_timeout,
            )
        if action == "install" and operation.repository_url:
            return await self._install_source_plugin(operation, instance, work)
        source_record = _source_plugin_record(
            pyproject,
            operation.module_name,
            operation.project_name,
        )
        if source_record is not None:
            repository_url = str(source_record.get("repository") or "")
            if not repository_url:
                raise AgentError("源码插件记录缺少 GitHub 仓库地址")
            operation.repository_url = _canonical_github_repository(repository_url)
            self.store.save(operation)
            if action == "update":
                return await self._install_source_plugin(operation, instance, work)
            if action == "uninstall":
                source = work / SOURCE_PLUGIN_DIR / operation.module_name
                if source.is_symlink():
                    source.unlink()
                elif source.is_dir():
                    shutil.rmtree(source)
                # Compute orphans before dropping the record; remove only the deps
                # this plugin added that no other source plugin or the project still
                # references, so uninstall does not leave dangling dependencies.
                orphaned = _orphaned_source_dependencies(pyproject, operation.module_name)
                _remove_source_plugin_record(
                    pyproject,
                    operation.module_name,
                    orphaned,
                )
                _update_plugin_list(
                    pyproject,
                    operation.module_name,
                    operation.project_name,
                    "uninstall",
                )
                outputs: list[str] = []
                if orphaned:
                    outputs.append(
                        await self._run_uv(
                            work,
                            ["remove", "--no-sync", *orphaned],
                            instance.build_timeout,
                        )
                    )
                outputs.append(
                    await self._run_uv(
                        work,
                        ["lock", "--check"],
                        instance.build_timeout,
                    )
                )
                return "\n".join(part for part in outputs if part)
        if action == "install":
            requirement = operation.project_name
            arguments = ["add", "--no-sync", requirement]
        elif action == "update":
            arguments = [
                "lock",
                "--upgrade-package",
                operation.project_name,
            ]
        elif action == "uninstall":
            arguments = ["remove", "--no-sync", operation.project_name]
        else:
            raise AgentError("重启操作不能修改项目依赖")
        output = await self._run_uv(work, arguments, instance.build_timeout)
        if operation.module_name:
            _update_plugin_list(
                pyproject,
                operation.module_name,
                operation.project_name,
                action,
            )
        await self._run_uv(work, ["lock", "--check"], instance.build_timeout)
        return output

    async def _install_source_plugin(
        self,
        operation: Operation,
        instance: InstanceConfig,
        work: Path,
    ) -> str:
        staging = work / ".mimo-source"
        if staging.exists():
            shutil.rmtree(staging)
        clone_output = await self._run_helper(
            work,
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--",
                operation.repository_url,
                staging.name,
            ],
            instance.build_timeout,
        )
        module_source, module_name = _find_source_plugin(
            staging,
            operation.module_name,
        )
        if module_name == SELF_UPDATE_MODULE:
            raise AgentError("不能通过 GitHub 源码插件安装覆盖 Mimo Console 自身")
        # On update the recorded module name must stay stable. If the repository
        # renamed its package directory, an implicit rename would leave the old
        # module dir and record behind and load both plugins. Reject it rather
        # than silently migrate.
        if (
            operation.action == "update"
            and operation.module_name
            and module_name != operation.module_name
        ):
            raise AgentError(
                f"源码插件更新后导入名发生变化（{operation.module_name} → {module_name}），"
                "已拒绝；请先卸载旧插件再重新安装"
            )
        target = work / SOURCE_PLUGIN_DIR / module_name
        if target.exists() and operation.action == "install":
            raise AgentError(f"源码插件目录已存在：{SOURCE_PLUGIN_DIR}/{module_name}")
        if target.is_symlink():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)

        # Reject symlinks before reading deps or copying: copytree follows them.
        _assert_no_symlinks(module_source)
        dependencies = _deduplicate_dependencies(
            _declared_source_dependencies(staging, module_source)
            + _inferred_source_dependencies(module_source)
        )
        existing_dependency_keys = _project_dependency_keys(work / "pyproject.toml")
        managed_additions = [
            dependency
            for dependency in dependencies
            if _requirement_key(dependency) not in existing_dependency_keys
        ]
        orphaned = (
            _orphaned_source_dependencies(
                work / "pyproject.toml",
                operation.module_name,
                dependencies,
            )
            if operation.action == "update"
            else []
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        source_root = (work / SOURCE_PLUGIN_DIR).resolve()
        if not _inside(target.resolve(), source_root):
            raise AgentError("源码插件目标路径越界")
        shutil.copytree(module_source, target, symlinks=False)
        shutil.rmtree(staging)

        operation.module_name = module_name
        self.store.save(operation)
        _update_source_plugin_dir(work / "pyproject.toml")
        _set_source_plugin_record(
            work / "pyproject.toml",
            module_name,
            operation.project_name,
            operation.repository_url,
            dependencies,
            managed_additions=managed_additions,
            managed_removals=orphaned,
        )

        output_parts = [clone_output]
        if orphaned:
            output_parts.append(
                await self._run_uv(
                    work,
                    ["remove", "--no-sync", *orphaned],
                    instance.build_timeout,
                )
            )
        if dependencies:
            output_parts.append(
                await self._run_uv(
                    work,
                    ["add", "--no-sync", *dependencies],
                    instance.build_timeout,
                )
            )
        output_parts.append(
            await self._run_uv(
                work,
                ["lock", "--check"],
                instance.build_timeout,
            )
        )
        return "\n".join(part for part in output_parts if part)

    async def _run_uv(self, work: Path, arguments: list[str], timeout: int) -> str:
        return await self._run_helper(work, ["uv", *arguments], timeout)

    async def _run_helper(
        self,
        work: Path,
        arguments: list[str],
        timeout: int,
    ) -> str:
        mount = f"type=bind,src={work},dst=/workspace"
        container_name = _helper_container_name(work.name)
        try:
            return await run_command(
                [
                    self.config.docker_bin,
                    "run",
                    "--rm",
                    "--name",
                    container_name,
                    "--label",
                    f"com.mimo-console-agent.operation={work.name}",
                    "--pull",
                    "missing",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--pids-limit",
                    "256",
                    "--mount",
                    mount,
                    "--workdir",
                    "/workspace",
                    "--env",
                    "UV_LINK_MODE=copy",
                    self.config.uv_image,
                    *arguments,
                ],
                cwd=work,
                timeout=timeout,
            )
        finally:
            await asyncio.shield(self._remove_helper_container(container_name, work))

    async def _remove_helper_container(self, name: str, cwd: Path) -> None:
        with suppress(CommandError, OSError):
            await run_command(
                [self.config.docker_bin, "rm", "--force", name],
                cwd=cwd,
                timeout=30,
            )

    def _image_name(
        self,
        instance: InstanceConfig,
        work: Path,
        operation_id: str,
    ) -> str:
        digest = hashlib.sha256()
        digest.update((work / "pyproject.toml").read_bytes())
        digest.update((work / "uv.lock").read_bytes())
        source_root = work / SOURCE_PLUGIN_DIR
        if source_root.is_dir():
            for path in sorted(source_root.rglob("*")):
                if path.is_file() and not path.is_symlink():
                    digest.update(path.relative_to(source_root).as_posix().encode())
                    digest.update(path.read_bytes())
        operation_suffix = operation_id.removeprefix("op_")[:12]
        return f"{instance.image_repository}:mimo-{digest.hexdigest()[:12]}-{operation_suffix}"

    def _stage_relative(self, path: Path, instance: InstanceConfig, work: Path) -> Path:
        return work / path.relative_to(instance.project_root)

    async def _build(self, instance: InstanceConfig, work: Path, image: str) -> str:
        dockerfile = self._stage_relative(instance.dockerfile, instance, work)
        context = self._stage_relative(instance.build_context, instance, work)
        return await run_command(
            [
                self.config.docker_bin,
                "build",
                "--file",
                str(dockerfile),
                "--tag",
                image,
                str(context),
            ],
            cwd=work,
            timeout=instance.build_timeout,
        )

    async def _verify(
        self,
        operation: Operation,
        instance: InstanceConfig,
    ) -> str:
        script = (
            "import importlib.util,sys;"
            f"sys.path.insert(0,{f'/app/{SOURCE_PLUGIN_DIR}'!r});"
            "import nonebot;"
            "nonebot.init();"
            "from nonebot.plugin import load_plugin;"
            "console=load_plugin('nonebot_plugin_mimo_console');"
            "assert console is not None, 'failed to load Mimo Console';"
        )
        if operation.module_name:
            expected = operation.action != "uninstall"
            if expected:
                if operation.module_name == "nonebot_plugin_mimo_console":
                    script += "loaded=console;"
                else:
                    script += f"loaded=load_plugin({operation.module_name!r});"
                script += (
                    f"assert loaded is not None, 'failed to load plugin: {operation.module_name}';"
                )
            else:
                script += (
                    f"found=importlib.util.find_spec({operation.module_name!r}) is not None;"
                    "assert found is False, "
                    f"f'unexpected module state: {operation.module_name} found={{found}}';"
                )
        command = [
            self.config.docker_bin,
            "run",
            "--rm",
        ]
        if instance.environment_file.is_file():
            command.extend(["--env-file", str(instance.environment_file)])
        command.extend(
            [
                "--entrypoint",
                "/bin/sh",
                operation.new_image,
                "-c",
                (
                    "if [ -x /app/.venv/bin/python ]; then "
                    'exec /app/.venv/bin/python -c "$1"; '
                    'else exec python -c "$1"; fi'
                ),
                "mimo-verify",
                script,
            ]
        )
        return await run_command(
            command,
            cwd=instance.project_root,
            timeout=instance.deploy_timeout,
        )

    def _commit_project(
        self,
        instance: InstanceConfig,
        work: Path,
        operation: Operation,
    ) -> None:
        _atomic_copy(work / "pyproject.toml", instance.project_root / "pyproject.toml")
        _atomic_copy(work / "uv.lock", instance.project_root / "uv.lock")
        if operation.repository_url:
            source_root = work / SOURCE_PLUGIN_DIR
            destination = instance.project_root / SOURCE_PLUGIN_DIR
            if source_root.is_dir() and not source_root.is_symlink():
                _atomic_replace_tree(source_root, destination)
            elif destination.is_dir():
                shutil.rmtree(destination)

    async def _image_exists(self, instance: InstanceConfig, image: str) -> bool:
        try:
            await run_command(
                [self.config.docker_bin, "image", "inspect", image],
                cwd=instance.project_root,
                timeout=30,
            )
        except CommandError:
            return False
        return True

    async def _current_image(self, instance: InstanceConfig) -> str:
        container_id = await self._container_id(instance)
        output = await run_command(
            [
                self.config.docker_bin,
                "inspect",
                "--format",
                "{{.Config.Image}}",
                container_id,
            ],
            cwd=instance.project_root,
            timeout=30,
        )
        image = output.strip()
        if not image:
            raise AgentError("无法确定当前容器镜像")
        return image

    async def _cleanup_images(
        self,
        instance: InstanceConfig,
        protected: set[str],
        operation: Operation | None = None,
    ) -> None:
        try:
            output = await run_command(
                [
                    self.config.docker_bin,
                    "image",
                    "ls",
                    "--filter",
                    f"reference={instance.image_repository}:mimo-*",
                    "--format",
                    "{{.Repository}}:{{.Tag}}",
                ],
                cwd=instance.project_root,
                timeout=60,
            )
            images = list(
                dict.fromkeys(line.strip() for line in output.splitlines() if line.strip())
            )
            keep = set(protected)
            keep.update(images[: instance.keep_images])
            expired = [image for image in images if image not in keep]
            if not expired:
                return
            removed = await run_command(
                [self.config.docker_bin, "image", "rm", *expired],
                cwd=instance.project_root,
                timeout=120,
            )
            if operation is not None:
                self.store.append_output(operation, removed)
                self.store.step(
                    operation,
                    "cleanup",
                    "success",
                    f"已清理 {len(expired)} 个旧镜像",
                )
        except (CommandError, OSError) as exc:
            if operation is not None:
                self.store.append_output(operation, getattr(exc, "output", "") or "")
                self.store.step(
                    operation,
                    "cleanup",
                    "warning",
                    f"旧镜像清理失败，不影响当前部署：{exc}",
                )

    def _compose_command(
        self,
        instance: InstanceConfig,
        override_file: Path | None = None,
        *,
        include_override: bool = True,
    ) -> list[str]:
        command = [
            self.config.docker_bin,
            "compose",
            "--project-directory",
            str(instance.project_root),
            "--project-name",
            instance.compose_project,
        ]
        for compose_file in instance.compose_files:
            command.extend(["--file", str(compose_file)])
        if include_override:
            command.extend(["--file", str(override_file or instance.override_file)])
        return command

    async def _container_id(self, instance: InstanceConfig) -> str:
        output = await run_command(
            [
                self.config.docker_bin,
                "ps",
                "--filter",
                f"label=com.docker.compose.project={instance.compose_project}",
                "--filter",
                f"label=com.docker.compose.service={instance.service}",
                "--format",
                "{{.ID}}",
            ],
            cwd=instance.project_root,
            timeout=30,
        )
        identifiers = [line.strip() for line in output.splitlines() if line.strip()]
        if not identifiers:
            raise AgentError(
                f"未找到 Compose 项目 {instance.compose_project} 的运行中服务 {instance.service}"
            )
        if len(identifiers) > 1:
            raise AgentError(
                f"Compose 项目 {instance.compose_project} 的服务 {instance.service} "
                "存在多个运行中容器，已拒绝选择"
            )
        return identifiers[0]

    async def _preflight(
        self,
        instance: InstanceConfig,
        work: Path,
        current_image: str,
    ) -> None:
        version_output = await run_command(
            [self.config.docker_bin, "compose", "version", "--short"],
            cwd=instance.project_root,
            timeout=30,
        )
        match = re.search(r"(\d+)\.(\d+)\.(\d+)", version_output)
        if not match or tuple(map(int, match.groups())) < (2, 24, 4):
            raise AgentError("Docker Compose 必须为 2.24.4 或更高版本")
        probe = work / ".mimo-compose-probe.yml"
        _write_text(probe, _image_override(instance.service, current_image))
        services = await run_command(
            [*self._compose_command(instance, probe), "config", "--services"],
            cwd=instance.project_root,
            timeout=30,
        )
        if instance.service not in services.splitlines():
            raise AgentError(f"Compose 中不存在服务：{instance.service}")

    async def _compose_up(self, instance: InstanceConfig) -> str:
        return await run_command(
            [
                *self._compose_command(instance),
                "up",
                "--detach",
                "--no-deps",
                "--force-recreate",
                instance.service,
            ],
            cwd=instance.project_root,
            timeout=instance.deploy_timeout,
        )

    async def _container_status(self, instance: InstanceConfig) -> str:
        container_id = await self._container_id(instance)
        return (
            await run_command(
                [
                    self.config.docker_bin,
                    "inspect",
                    "--format",
                    "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}",
                    container_id,
                ],
                cwd=instance.project_root,
                timeout=30,
            )
        ).strip()

    async def _wait_healthy(self, instance: InstanceConfig) -> None:
        deadline = time.monotonic() + instance.health_timeout
        last_error = ""
        while time.monotonic() < deadline:
            try:
                status = await self._container_status(instance)
                if status in {"healthy", "running"}:
                    # health_url is restricted to loopback during configuration
                    # loading; never send it through host HTTP(S)_PROXY settings.
                    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                        response = await client.get(instance.health_url)
                    if response.status_code == 200:
                        return
                elif status in {"exited", "dead", "unhealthy"}:
                    raise AgentError(f"容器状态异常：{status}")
            except (CommandError, httpx.HTTPError, AgentError) as exc:
                last_error = str(exc)
            await asyncio.sleep(2)
        raise AgentError(f"健康检查超时：{last_error or 'Mimo Console 未恢复'}")

    async def _rollback(
        self,
        operation: Operation,
        instance: InstanceConfig,
        reason: str,
    ) -> None:
        operation.status = "rolling_back"
        self.store.step(operation, "rollback", "running", reason)
        snapshot = Path(operation.snapshot_dir)
        if not snapshot.is_dir() or not operation.old_image:
            raise AgentError("缺少回滚快照或旧镜像")
        _atomic_copy(snapshot / "pyproject.toml", instance.project_root / "pyproject.toml")
        _atomic_copy(snapshot / "uv.lock", instance.project_root / "uv.lock")
        source_marker = snapshot / "source-plugins.exists"
        source_snapshot = snapshot / SOURCE_PLUGIN_DIR
        source_destination = instance.project_root / SOURCE_PLUGIN_DIR
        if source_marker.is_file() and source_marker.read_text(encoding="ascii").strip() == "1":
            if not source_snapshot.is_dir():
                raise AgentError("源码插件回滚快照不完整")
            _atomic_replace_tree(source_snapshot, source_destination)
        elif source_marker.is_file() and source_destination.is_dir():
            shutil.rmtree(source_destination)
        elif (
            not source_marker.exists()
            and operation.repository_url
            and operation.action == "install"
        ):
            legacy_source = source_destination / operation.module_name
            if legacy_source.is_symlink():
                legacy_source.unlink()
            elif legacy_source.is_dir():
                shutil.rmtree(legacy_source)
        override_snapshot = snapshot / "docker-compose.override.yml"
        if override_snapshot.is_file():
            _atomic_copy(override_snapshot, instance.override_file)
        else:
            _write_text(
                instance.override_file,
                _image_override(instance.service, operation.old_image),
            )
        output = await self._compose_up(instance)
        self.store.append_output(operation, output)
        await self._wait_healthy(instance)
        operation.status = "rolled_back"
        self.store.step(operation, "rollback", "success", reason)
        self.store.save(operation)
