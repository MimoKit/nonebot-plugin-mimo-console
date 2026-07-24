from __future__ import annotations

import re
import time
from typing import Any

import httpx

PACKAGE_NAME = "nonebot-plugin-mimo-console"
GITHUB_LATEST_RELEASE = (
    "https://api.github.com/repos/MimoKit/nonebot-plugin-mimo-console/releases/latest"
)
# 形如 v0.1.0 / 0.1.0 / v1.2.3rc1 → 取 0.1.0 / 1.2.3rc1
_TAG_RE = re.compile(r"^v?(\d+\.\d+\.\d+[A-Za-z0-9.+~-]*)$")


class VersionError(RuntimeError):
    """版本检测相关错误。"""


def get_installed_version() -> str:
    """返回当前安装的本插件版本，无法确定时返回空串。"""
    try:
        from importlib.metadata import version

        return str(version(PACKAGE_NAME))
    except Exception:  # noqa: BLE001 - 任何元数据异常都视为不可得
        return ""


def normalize_tag(tag: str) -> str:
    """剥离前缀 v，校验为合法语义版本；非法返回空串。"""
    match = _TAG_RE.match((tag or "").strip())
    return match.group(1) if match else ""


def is_newer(latest: str, current: str) -> bool:
    """latest 是否严格大于 current。优先 packaging.version，缺失则回退字符串比较。"""
    if not latest or not current:
        return False
    try:
        from packaging.version import InvalidVersion, Version

        return Version(latest) > Version(current)
    except (InvalidVersion, ImportError, ValueError):
        return latest != current


class LatestReleaseCache:
    """GitHub 最新 release 的带缓存读取，避免触发限流。"""

    def __init__(self, cache_seconds: int = 1800) -> None:
        self.cache_seconds = cache_seconds
        self._data: dict[str, Any] | None = None
        self._fetched_at = 0.0

    async def fetch(self, force: bool = False) -> dict[str, Any]:
        fresh = self._data is not None and time.time() - self._fetched_at < self.cache_seconds
        if fresh and not force:
            return self._data or {}
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                response = await client.get(
                    GITHUB_LATEST_RELEASE,
                    headers={
                        "Accept": "application/vnd.github+json",
                        "User-Agent": PACKAGE_NAME,
                    },
                )
                if response.status_code == 404:
                    self._data = {}  # 还没有 release
                else:
                    response.raise_for_status()
                    payload = response.json()
                    self._data = payload if isinstance(payload, dict) else {}
        except (httpx.HTTPError, ValueError):
            # 网络或解析失败时保留旧缓存，首次失败则置空
            if self._data is None:
                self._data = {}
        self._fetched_at = time.time()
        return self._data or {}

    def snapshot(self, installed: str) -> dict[str, Any]:
        """根据已缓存的 release 构造给前端的版本信息。"""
        data = self._data or {}
        latest = normalize_tag(str(data.get("tag_name") or ""))
        return {
            "current": installed,
            "latest": latest,
            "has_update": is_newer(latest, installed),
            "release_url": str(data.get("html_url") or ""),
            "release_notes": str(data.get("body") or ""),
            "published_at": str(data.get("published_at") or ""),
        }
