from __future__ import annotations

import base64
import importlib.metadata
import platform
import re
import time
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psutil
from nonebot import get_driver, get_loaded_plugins

from .log_buffer import STARTED_AT

IMAGE_MIME_TYPES = {
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
ICON_NAMES = ("icon", "logo", "avatar")
ICON_DIRECTORIES = ("", "assets", "resources", "static")
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
MAX_ICON_BYTES = 1024 * 1024


def _version(package: str) -> str:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _bytes(value: int | float) -> int:
    return int(value)


def _safe_image_url(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith(("https://", "http://")):
        return text
    if re.fullmatch(
        r"data:image/(?:gif|jpeg|png|webp);base64,[A-Za-z0-9+/=]+",
        text,
        flags=re.IGNORECASE,
    ):
        return text
    return ""


def _github_avatar_url(homepage: object) -> str:
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


@lru_cache(maxsize=128)
def _read_icon_data(path_text: str, modified_ns: int, size: int) -> str:
    del modified_ns
    path = Path(path_text)
    mime = IMAGE_MIME_TYPES.get(path.suffix.casefold())
    if not mime or size <= 0 or size > MAX_ICON_BYTES:
        return ""
    try:
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError:
        return ""
    return f"data:{mime};base64,{encoded}"


def _local_plugin_icon(module_file: object) -> str:
    if not module_file:
        return ""
    try:
        module_dir = Path(str(module_file)).resolve().parent
    except OSError:
        return ""
    for directory in ICON_DIRECTORIES:
        root = module_dir / directory if directory else module_dir
        for name in ICON_NAMES:
            for suffix in IMAGE_MIME_TYPES:
                candidate = root / f"{name}{suffix}"
                try:
                    stat = candidate.stat()
                except OSError:
                    continue
                if candidate.is_file():
                    return _read_icon_data(str(candidate), stat.st_mtime_ns, stat.st_size)
    return ""


def _plugin_icon(metadata: object, module_file: object) -> str:
    extra = getattr(metadata, "extra", None)
    extra_data = extra if isinstance(extra, dict) else {}
    for value in (
        getattr(metadata, "icon", None),
        extra_data.get("icon"),
        extra_data.get("logo"),
        extra_data.get("avatar"),
    ):
        icon = _safe_image_url(value)
        if icon:
            return icon
    local_icon = _local_plugin_icon(module_file)
    if local_icon:
        return local_icon
    return _github_avatar_url(getattr(metadata, "homepage", None))


def dashboard_snapshot(project_root: Path) -> dict[str, Any]:
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage(str(project_root.anchor or project_root))
    network = psutil.net_io_counters()
    driver = get_driver()
    plugins = get_loaded_plugins()
    process = psutil.Process()
    return {
        "system": {
            "hostname": platform.node() or "NoneBot Host",
            "platform": f"{platform.system()} {platform.release()}",
            "python": platform.python_version(),
            "nonebot": _version("nonebot2"),
            "uptime": int(time.time() - STARTED_AT),
            "cpu_percent": psutil.cpu_percent(interval=None),
            "cpu_count": psutil.cpu_count() or 0,
            "memory_percent": memory.percent,
            "memory_used": _bytes(memory.used),
            "memory_total": _bytes(memory.total),
            "disk_percent": disk.percent,
            "disk_used": _bytes(disk.used),
            "disk_total": _bytes(disk.total),
            "network_sent": _bytes(network.bytes_sent),
            "network_recv": _bytes(network.bytes_recv),
            "process_memory": _bytes(process.memory_info().rss),
        },
        "counts": {
            "plugins": len(plugins),
            "bots": len(driver.bots),
            "matchers": sum(len(getattr(plugin, "matcher", ())) for plugin in plugins),
        },
        "bots": [
            {
                "id": str(bot.self_id),
                "adapter": bot.adapter.get_name(),
            }
            for bot in driver.bots.values()
        ],
    }


def plugin_snapshot() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for plugin in sorted(get_loaded_plugins(), key=lambda item: item.name.casefold()):
        metadata = plugin.metadata
        module_file = getattr(plugin.module, "__file__", None)
        result.append(
            {
                "name": plugin.name,
                "module": plugin.module_name,
                "title": getattr(metadata, "name", None) or plugin.name,
                "description": getattr(metadata, "description", None) or "暂无插件介绍",
                "usage": getattr(metadata, "usage", None) or "",
                "type": getattr(metadata, "type", None) or "plugin",
                "homepage": getattr(metadata, "homepage", None) or "",
                "icon": _plugin_icon(metadata, module_file),
                "matchers": len(getattr(plugin, "matcher", ())),
                "path": str(Path(module_file).parent) if module_file else "",
            }
        )
    return result
