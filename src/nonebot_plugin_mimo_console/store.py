from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .backends.base import PackageAction, PackageBackend, PackageRequest
from .backends.local import LocalPackageBackend
from .backends.local import build_nb_command as _build_nb_command
from .backends.local import clean_output as _local_clean_output

REGISTRY_URL = "https://registry.nonebot.dev/plugins.json"
STORE_URL = "https://nonebot.dev/store/plugins"
SELF_UPDATE_REPOSITORY = "https://github.com/MimoKit/nonebot-plugin-mimo-console.git"
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SAFE_MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")
OPERATION_ID_RE = re.compile(r"^(?:op_|local-)[0-9a-f]{32}$")
GITHUB_OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
GITHUB_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
GITHUB_RESERVED_PATHS = {
    "collections",
    "features",
    "login",
    "marketplace",
    "organizations",
    "orgs",
    "settings",
    "signup",
    "sponsors",
    "topics",
}


class StoreError(RuntimeError):
    pass


def normalize_project_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def parse_github_repository(
    repository_url: str,
    module_name: str = "",
    project_name: str = "",
) -> tuple[str, str, str]:
    try:
        parsed = urlparse(repository_url.strip())
        port = parsed.port
    except ValueError as exc:
        raise StoreError("GitHub 仓库地址不合法") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"github.com", "www.github.com"}
        or port is not None
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise StoreError("只支持不含凭据、参数或片段的 GitHub HTTPS 仓库地址")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2:
        raise StoreError("GitHub 地址必须指向仓库根目录")
    owner, repository = parts
    if repository.endswith(".git"):
        repository = repository[:-4]
    if (
        owner.casefold() in GITHUB_RESERVED_PATHS
        or not GITHUB_OWNER_RE.fullmatch(owner)
        or not GITHUB_REPOSITORY_RE.fullmatch(repository)
        or repository in {".", ".."}
    ):
        raise StoreError("GitHub 仓库所有者或仓库名不合法")

    resolved_project = project_name.strip() or repository
    resolved_module = module_name.strip() or re.sub(r"[-.]+", "_", repository)
    if not SAFE_NAME_RE.fullmatch(resolved_project):
        raise StoreError("Python 包名不合法")
    if not SAFE_MODULE_RE.fullmatch(resolved_module):
        raise StoreError("插件导入名不合法")
    if resolved_module == "nonebot_plugin_mimo_console":
        raise StoreError("不能通过 GitHub 安装覆盖 Mimo Console 自身")
    return (
        f"https://github.com/{owner}/{repository}.git",
        resolved_module,
        resolved_project,
    )


def github_avatar_url(homepage: object) -> str:
    try:
        parsed = urlparse(str(homepage or "").strip())
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "github.com",
        "www.github.com",
    }:
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return ""
    owner = parts[0]
    if owner.casefold() in GITHUB_RESERVED_PATHS or not GITHUB_OWNER_RE.fullmatch(owner):
        return ""
    return f"https://github.com/{owner}.png?size=96"


def build_nb_command(
    project_root: Path,
    action: PackageAction,
    project_name: str,
) -> list[str]:
    return _build_nb_command(project_root, action, project_name)


def _find_uv_executable() -> str:
    found = shutil.which("uv")
    if found:
        return found
    user_uv = Path.home() / ".local" / "bin" / "uv"
    if user_uv.is_file():
        return str(user_uv)
    return ""


def build_self_update_command(
    project_root: Path,
    project_name: str,
    git_url: str,
    uv_executable: str | None = None,
) -> list[str]:
    if not SAFE_NAME_RE.fullmatch(project_name):
        raise ValueError("插件包名不合法")
    uv = uv_executable if uv_executable is not None else _find_uv_executable()
    # gh-proxy 风格前缀会把代理前缀拼在 GitHub URL 之前，形成含多个 "://" 的套娃 URL
    # （如 https://gh-proxy.com/https://github.com/...）。uv 解析
    # "git+<套娃URL>@<ref>" 时其 URL 解析器会 panic（AmbiguousAuthority），
    # 而 pip 直接交给 git clone，对套娃 URL 鲁棒。因此前缀代理走 pip；
    # 干净 URL（直连或 CNB 镜像仓库地址）仍走 uv。
    is_prefix_proxy_url = git_url.count("://") > 1
    if uv and (project_root / "pyproject.toml").is_file() and not is_prefix_proxy_url:
        return [
            uv,
            "add",
            f"{project_name} @ git+{git_url}",
            "--upgrade-package",
            project_name,
        ]
    return [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
        f"git+{git_url}",
    ]


def _installed_distributions() -> dict[str, str]:
    result: dict[str, str] = {}
    for distribution in importlib.metadata.distributions():
        name = str(distribution.metadata["Name"] or "").strip()
        if name:
            result[normalize_project_name(name)] = distribution.version
    return result


def _clean_output(raw: bytes, limit: int = 6000) -> str:
    return _local_clean_output(raw, limit)


class PluginStore:
    def __init__(
        self,
        cache_seconds: int = 600,
        backend: PackageBackend | None = None,
    ) -> None:
        self.cache_seconds = cache_seconds
        self.backend = backend or LocalPackageBackend()
        self._items: list[dict[str, Any]] = []
        self._fetched_at = 0.0
        self._fetch_lock = asyncio.Lock()
        self.action_lock = asyncio.Lock()

    async def catalog(self, force: bool = False) -> list[dict[str, Any]]:
        if self._items and not force and time.time() - self._fetched_at < self.cache_seconds:
            return self._items
        async with self._fetch_lock:
            if self._items and not force and time.time() - self._fetched_at < self.cache_seconds:
                return self._items
            try:
                async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
                    response = await client.get(
                        REGISTRY_URL,
                        headers={"User-Agent": "nonebot-plugin-mimo-console"},
                    )
                    response.raise_for_status()
                    payload = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                if self._items:
                    return self._items
                raise StoreError(f"官方插件商店暂时无法访问：{exc}") from exc
            if not isinstance(payload, list):
                raise StoreError("官方插件商店返回了无法识别的数据")
            self._items = [item for item in payload if self._valid_item(item)]
            self._fetched_at = time.time()
            return self._items

    @staticmethod
    def _valid_item(item: object) -> bool:
        if not isinstance(item, dict):
            return False
        module = item.get("module_name")
        project = item.get("project_link")
        return (
            isinstance(module, str)
            and isinstance(project, str)
            and bool(SAFE_NAME_RE.fullmatch(module))
            and bool(SAFE_NAME_RE.fullmatch(project))
        )

    async def page(
        self,
        query: str,
        page: int,
        page_size: int,
        official_only: bool,
    ) -> dict[str, Any]:
        catalog, installed = await asyncio.gather(
            self.catalog(),
            asyncio.to_thread(_installed_distributions),
        )
        needle = query.strip().casefold()
        items: list[dict[str, Any]] = []
        for raw in catalog:
            if not raw.get("valid", False):
                continue
            if official_only and not raw.get("is_official", False):
                continue
            tags = self._normalize_tags(raw.get("tags", []))
            tag_labels = [tag["label"] for tag in tags]
            haystack = " ".join(
                str(raw.get(key) or "")
                for key in ("name", "desc", "module_name", "project_link", "author")
            )
            if needle and needle not in f"{haystack} {' '.join(tag_labels)}".casefold():
                continue
            project_name = str(raw["project_link"])
            installed_version = installed.get(normalize_project_name(project_name))
            items.append(self._serialize_plugin(raw, installed_version, tags=tags))
        items.sort(
            key=lambda item: (
                not item["installed"],
                not item["official"],
                item["name"].casefold(),
            )
        )
        total = len(items)
        start = (page - 1) * page_size
        return {
            "items": items[start : start + page_size],
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": max(1, (total + page_size - 1) // page_size),
            "source": REGISTRY_URL,
            "store_url": STORE_URL,
            "fetched_at": self._fetched_at,
        }

    @staticmethod
    def _normalize_tags(raw_tags: object) -> list[dict[str, str]]:
        tags: list[dict[str, str]] = []
        if not isinstance(raw_tags, list):
            return tags
        for tag in raw_tags:
            if not isinstance(tag, dict) or not tag.get("label"):
                continue
            tags.append(
                {
                    "label": str(tag.get("label")),
                    "color": str(tag.get("color") or "#a78bfa"),
                }
            )
            if len(tags) >= 8:
                break
        return tags

    @staticmethod
    def _normalize_adapters(raw_adapters: object) -> list[str]:
        if raw_adapters is None:
            return []
        if isinstance(raw_adapters, list):
            return [str(item) for item in raw_adapters if item][:12]
        if isinstance(raw_adapters, str) and raw_adapters.strip():
            return [raw_adapters.strip()]
        return []

    def _serialize_plugin(
        self,
        raw: dict[str, Any],
        installed_version: str | None = None,
        tags: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        project_name = str(raw["project_link"])
        tag_items = tags if tags is not None else self._normalize_tags(raw.get("tags", []))
        return {
            "module_name": str(raw["module_name"]),
            "project_link": project_name,
            "name": str(raw.get("name") or raw["module_name"]),
            "description": str(raw.get("desc") or "暂无插件介绍"),
            "author": str(raw.get("author") or "unknown"),
            "homepage": str(raw.get("homepage") or ""),
            "icon": github_avatar_url(raw.get("homepage")),
            "tags": tag_items,
            "tag_labels": [tag["label"] for tag in tag_items],
            "official": bool(raw.get("is_official", False)),
            "type": str(raw.get("type") or "application"),
            "version": str(raw.get("version") or ""),
            "installed": installed_version is not None,
            "installed_version": installed_version or "",
            "updated_at": str(raw.get("time") or ""),
            "supported_adapters": self._normalize_adapters(raw.get("supported_adapters")),
            "valid": bool(raw.get("valid", False)),
            "skip_test": bool(raw.get("skip_test", False)),
            "store_url": f"{STORE_URL}?q={project_name}",
        }

    async def detail(self, module_name: str) -> dict[str, Any]:
        raw = await self.find(module_name)
        installed = await asyncio.to_thread(_installed_distributions)
        project_name = str(raw["project_link"])
        installed_version = installed.get(normalize_project_name(project_name))
        return self._serialize_plugin(raw, installed_version)

    async def find(self, module_name: str) -> dict[str, Any]:
        if not SAFE_NAME_RE.fullmatch(module_name):
            raise StoreError("插件模块名不合法")
        for item in await self.catalog():
            if item.get("module_name") == module_name:
                return item
        raise StoreError("官方插件商店中没有找到这个插件")

    async def manage(
        self,
        project_root: Path,
        module_name: str,
        action: PackageAction,
        timeout: int,
    ) -> dict[str, Any]:
        plugin = await self.find(module_name)
        if action != "uninstall" and not plugin.get("valid", False):
            raise StoreError("该插件未通过商店检查，不能直接安装")
        project_name = str(plugin["project_link"])
        async with self.action_lock:
            try:
                operation = await self.backend.manage(
                    PackageRequest(
                        action=action,
                        module_name=module_name,
                        project_name=project_name,
                        project_root=project_root,
                    ),
                    timeout,
                )
            except (OSError, ValueError, RuntimeError) as exc:
                raise StoreError(str(exc)) from exc
        if operation.status == "failed":
            raise StoreError(operation.error or operation.output or "插件操作失败")
        importlib.invalidate_caches()
        result = operation.as_dict()
        result["project_link"] = operation.project_name
        return result

    async def update_self(
        self,
        project_root: Path,
        repository_url: str,
        timeout: int,
    ) -> dict[str, Any]:
        canonical_url = repository_url.strip()
        if canonical_url != SELF_UPDATE_REPOSITORY:
            raise StoreError("控制台自更新仓库不在受信任白名单中")
        async with self.action_lock:
            try:
                operation = await self.backend.manage(
                    PackageRequest(
                        action="update",
                        module_name="nonebot_plugin_mimo_console",
                        project_name="nonebot-plugin-mimo-console",
                        project_root=project_root,
                        repository_url=canonical_url,
                    ),
                    timeout,
                )
            except (OSError, ValueError, RuntimeError) as exc:
                raise StoreError(str(exc)) from exc
        if operation.status == "failed":
            raise StoreError(operation.error or operation.output or "控制台更新失败")
        result = operation.as_dict()
        result["project_link"] = operation.project_name
        result["repository_url"] = canonical_url
        return result

    async def install_github(
        self,
        project_root: Path,
        repository_url: str,
        module_name: str,
        project_name: str,
        timeout: int,
    ) -> dict[str, Any]:
        canonical_url, resolved_module, resolved_project = parse_github_repository(
            repository_url,
            module_name,
            project_name,
        )
        async with self.action_lock:
            try:
                operation = await self.backend.manage(
                    PackageRequest(
                        action="install",
                        module_name=resolved_module,
                        project_name=resolved_project,
                        project_root=project_root,
                        repository_url=canonical_url,
                    ),
                    timeout,
                )
            except (OSError, ValueError, RuntimeError) as exc:
                raise StoreError(str(exc)) from exc
        if operation.status == "failed":
            raise StoreError(operation.error or operation.output or "GitHub 插件安装失败")
        result = operation.as_dict()
        result["project_link"] = operation.project_name
        result["repository_url"] = canonical_url
        return result

    async def manage_direct_plugin(
        self,
        project_root: Path,
        module_name: str,
        project_name: str,
        repository_url: str,
        action: PackageAction,
        timeout: int,
    ) -> dict[str, Any]:
        if action not in {"update", "uninstall"}:
            raise StoreError("源码插件只支持更新或卸载")
        if not SAFE_MODULE_RE.fullmatch(module_name) or not SAFE_NAME_RE.fullmatch(project_name):
            raise StoreError("源码插件记录不合法")
        canonical_url, resolved_module, resolved_project = parse_github_repository(
            repository_url,
            module_name,
            project_name,
        )
        if resolved_module != module_name or resolved_project != project_name:
            raise StoreError("源码插件记录与 GitHub 仓库信息不一致")
        async with self.action_lock:
            try:
                operation = await self.backend.manage(
                    PackageRequest(
                        action=action,
                        module_name=module_name,
                        project_name=project_name,
                        project_root=project_root,
                        repository_url=canonical_url,
                    ),
                    timeout,
                )
            except (OSError, ValueError, RuntimeError) as exc:
                raise StoreError(str(exc)) from exc
        if operation.status == "failed":
            raise StoreError(operation.error or operation.output or "源码插件操作失败")
        result = operation.as_dict()
        result["project_link"] = operation.project_name
        return result

    async def manage_dependency(
        self,
        project_root: Path,
        project_name: str,
        action: PackageAction,
        timeout: int,
    ) -> dict[str, Any]:
        name = project_name.strip()
        if not SAFE_NAME_RE.fullmatch(name):
            raise StoreError("依赖包名不合法")
        async with self.action_lock:
            try:
                operation = await self.backend.manage(
                    PackageRequest(
                        action=action,
                        module_name="",
                        project_name=name,
                        project_root=project_root,
                    ),
                    timeout,
                )
            except (OSError, ValueError, RuntimeError) as exc:
                raise StoreError(str(exc)) from exc
        if operation.status == "failed":
            raise StoreError(operation.error or operation.output or "依赖操作失败")
        result = operation.as_dict()
        result["project_link"] = operation.project_name
        return result

    async def capabilities(self) -> dict[str, Any]:
        try:
            return await self.backend.capabilities()
        except (OSError, ValueError, RuntimeError) as exc:
            raise StoreError(str(exc)) from exc

    async def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        if not OPERATION_ID_RE.fullmatch(operation_id):
            raise StoreError("操作 ID 不合法")
        try:
            operation = await self.backend.get_operation(operation_id)
        except (OSError, ValueError, RuntimeError) as exc:
            raise StoreError(str(exc)) from exc
        return operation.as_dict() if operation else None

    async def list_operations(self) -> list[dict[str, Any]]:
        try:
            operations = await self.backend.list_operations()
        except (OSError, ValueError, RuntimeError) as exc:
            raise StoreError(str(exc)) from exc
        return [operation.as_dict() for operation in operations]

    async def rollback(self, operation_id: str) -> dict[str, Any]:
        if not OPERATION_ID_RE.fullmatch(operation_id):
            raise StoreError("操作 ID 不合法")
        try:
            return (await self.backend.rollback(operation_id)).as_dict()
        except (OSError, ValueError, RuntimeError) as exc:
            raise StoreError(str(exc)) from exc

    async def restart(self) -> dict[str, Any]:
        try:
            return (await self.backend.restart()).as_dict()
        except (OSError, ValueError, RuntimeError) as exc:
            raise StoreError(str(exc)) from exc
