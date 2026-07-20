from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import importlib.util
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import httpx

REGISTRY_URL = "https://registry.nonebot.dev/plugins.json"
STORE_URL = "https://nonebot.dev/store/plugins"
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
GITHUB_OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
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
PackageAction = Literal["install", "update", "uninstall"]


class StoreError(RuntimeError):
    pass


def normalize_project_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


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


def _installed_distributions() -> dict[str, str]:
    result: dict[str, str] = {}
    for distribution in importlib.metadata.distributions():
        name = str(distribution.metadata["Name"] or "").strip()
        if name:
            result[normalize_project_name(name)] = distribution.version
    return result


def _clean_output(raw: bytes, limit: int = 6000) -> str:
    text = raw.decode("utf-8", errors="replace")
    text = ANSI_RE.sub("", text).replace("\r\n", "\n").strip()
    text = re.sub(r"(https?://)[^/@\s]+@", r"\1***@", text)
    return text[-limit:]


class PluginStore:
    def __init__(self, cache_seconds: int = 600) -> None:
        self.cache_seconds = cache_seconds
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
        if importlib.util.find_spec("nb_cli") is None:
            raise StoreError("当前环境缺少 nb-cli，请重新安装本插件后再试")
        project_name = str(plugin["project_link"])
        command = build_nb_command(project_root, action, project_name)
        env = os.environ.copy()
        env.update({"NO_COLOR": "1", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        async with self.action_lock:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=project_root,
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
                raise StoreError(f"插件操作超过 {timeout} 秒，已终止") from None
        clean_output = _clean_output(output)
        if process.returncode != 0:
            raise StoreError(clean_output or f"nb-cli 执行失败（{process.returncode}）")
        importlib.invalidate_caches()
        return {
            "ok": True,
            "action": action,
            "module_name": module_name,
            "project_link": project_name,
            "restart_required": True,
            "output": clean_output,
        }
