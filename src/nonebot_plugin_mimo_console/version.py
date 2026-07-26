from __future__ import annotations

import asyncio
import re
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

PACKAGE_NAME = "nonebot-plugin-mimo-console"
PACKAGE_GIT_URL = "https://github.com/MimoKit/nonebot-plugin-mimo-console.git"
# 直接读上游 master 的 pyproject.toml version，无需 maintainer 发 release。
MASTER_PYPROJECT_URL = (
    "https://raw.githubusercontent.com/MimoKit/nonebot-plugin-mimo-console/master/pyproject.toml"
)
# CNB 镜像仓库（完整仓库地址，与代理前缀二选一）。
CNB_MIRROR_REPO = "https://cnb.cool/MimokitStudio/nonebot-plugin-mimo-console"
# 供 WebUI 选择的 GitHub 加速项：gh-proxy 风格前缀 + CNB 镜像；空字符串表示直连。
GITHUB_PROXY_PRESETS: tuple[str, ...] = (
    "https://edgeone.gh-proxy.com",
    "https://hk.gh-proxy.com",
    "https://gh-proxy.com",
    "https://gh.llkk.cc",
    CNB_MIRROR_REPO,
)
_TAG_RE = re.compile(r"^v?(\d+\.\d+\.\d+[A-Za-z0-9.+~-]*)$")
_PYPROJECT_VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)
_PROXY_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)


class VersionError(RuntimeError):
    """版本检测相关错误。"""


def normalize_github_proxy(value: str) -> str:
    """校验并规范化 GitHub 加速地址（代理前缀或镜像仓库），空串表示直连；非法值抛 ValueError。"""
    text = (value or "").strip().rstrip("/")
    if not text:
        return ""
    if (
        not _PROXY_SCHEME_RE.match(text)
        or any(char.isspace() for char in text)
        or "@" in text
        or "#" in text
    ):
        raise ValueError("加速地址必须是合法的 http/https URL")
    return text


def is_mirror_repo(value: str) -> bool:
    """该加速地址是否为完整镜像仓库地址（而非 gh-proxy 风格的代理前缀）。"""
    text = value.rstrip("/")
    return text.endswith(PACKAGE_NAME) or text.endswith(f"{PACKAGE_NAME}.git")


def apply_github_proxy(url: str, proxy: str) -> str:
    """把 GitHub URL 改写成走加速代理前缀的形式；代理为空时原样返回。"""
    prefix = normalize_github_proxy(proxy)
    return f"{prefix}/{url}" if prefix else url


def resolve_git_url(proxy: str) -> str:
    """按加速配置返回自更新实际使用的 git 仓库地址。"""
    prefix = normalize_github_proxy(proxy)
    if not prefix:
        return PACKAGE_GIT_URL
    if is_mirror_repo(prefix):
        return prefix
    return f"{prefix}/{PACKAGE_GIT_URL}"


def resolve_version_url(proxy: str) -> str:
    """按加速配置返回版本检测实际读取的 pyproject 地址（仅前缀代理；镜像仓库走 git 协议）。"""
    prefix = normalize_github_proxy(proxy)
    return f"{prefix}/{MASTER_PYPROJECT_URL}" if prefix else MASTER_PYPROJECT_URL


async def run_git(args: list[str], timeout: int) -> tuple[int, bytes]:
    """运行 git 子进程，返回 (returncode, 输出)；异常或超时返回 (-1, b"")。"""
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except (OSError, asyncio.TimeoutError):
        return -1, b""
    return process.returncode or 0, output


async def probe_mirror_repo(repo_url: str, timeout: int = 10) -> bool:
    """git ls-remote 探测镜像仓库是否匿名可达。"""
    code, _ = await run_git(["git", "ls-remote", repo_url, "HEAD"], timeout)
    return code == 0


async def fetch_mirror_version(repo_url: str, timeout: int = 30) -> str:
    """镜像仓库没有 GitHub 式 raw 直链，用稀疏浅克隆只拉 pyproject.toml 读版本号。"""
    with tempfile.TemporaryDirectory(prefix="mimo-mirror-") as tmp:
        code, _ = await run_git(
            ["git", "clone", "--depth", "1", "--filter=blob:none", "--sparse", repo_url, tmp],
            timeout,
        )
        if code != 0:
            return ""
        code, _ = await run_git(
            ["git", "-C", tmp, "sparse-checkout", "set", "--no-cone", "/pyproject.toml"],
            timeout,
        )
        if code != 0:
            return ""
        try:
            text = Path(tmp, "pyproject.toml").read_text(encoding="utf-8")
        except OSError:
            return ""
        match = _PYPROJECT_VERSION_RE.search(text)
        return match.group(1) if match else ""


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

    async def fetch(self, force: bool = False, proxy: str = "") -> str:
        fresh = self._latest != "" and time.time() - self._fetched_at < self.cache_seconds
        if fresh and not force:
            return self._latest
        prefix = normalize_github_proxy(proxy)
        if is_mirror_repo(prefix):
            # 镜像仓库无 raw 直链，走 git 浅克隆；失败保留上次缓存的版本号
            self._latest = await fetch_mirror_version(prefix) or self._latest
            self._fetched_at = time.time()
            return self._latest
        url = resolve_version_url(prefix)
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                response = await client.get(
                    url,
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
