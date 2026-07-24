from __future__ import annotations

import re
import time
from typing import Any

import httpx

PACKAGE_NAME = "nonebot-plugin-mimo-console"
# 直接读上游 master 的 pyproject.toml version，无需 maintainer 发 release。
MASTER_PYPROJECT_URL = (
    "https://raw.githubusercontent.com/MimoKit/nonebot-plugin-mimo-console/master/pyproject.toml"
)
_TAG_RE = re.compile(r"^v?(\d+\.\d+\.\d+[A-Za-z0-9.+~-]*)$")
_PYPROJECT_VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)


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
        from packaging.version import Version
    except ImportError:
        return latest != current
    try:
        return Version(latest) > Version(current)
    except ValueError:  # packaging.version.InvalidVersion 是 ValueError 子类
        return latest != current


class LatestReleaseCache:
    """上游 master 最新版本号的带缓存读取（直接拉 pyproject，无需 release/tag）。"""

    def __init__(self, cache_seconds: int = 1800) -> None:
        self.cache_seconds = cache_seconds
        self._latest: str = ""
        self._fetched_at: float = 0.0

    async def fetch(self, force: bool = False) -> str:
        fresh = self._latest != "" and time.time() - self._fetched_at < self.cache_seconds
        if fresh and not force:
            return self._latest
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                response = await client.get(
                    MASTER_PYPROJECT_URL,
                    headers={"User-Agent": PACKAGE_NAME},
                )
                response.raise_for_status()
                match = _PYPROJECT_VERSION_RE.search(response.text)
                self._latest = match.group(1) if match else ""
        except (httpx.HTTPError, OSError):
            # 网络失败保留上次缓存的版本号；从未成功过则为空串
            pass
        self._fetched_at = time.time()
        return self._latest

    def snapshot(self, installed: str) -> dict[str, Any]:
        latest = self._latest
        return {
            "current": installed,
            "latest": latest,
            "has_update": is_newer(latest, installed),
        }
