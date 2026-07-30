import asyncio
import importlib.metadata
import os
import re
import signal
import subprocess
import sys
import time
from collections import defaultdict, deque
from email.message import Message
from typing import Annotated, Any, Literal, cast

import httpx
import tomlkit
from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from nonebot import get_driver, logger
from pydantic import BaseModel, Field

from .background import MAX_BACKGROUND_BYTES, BackgroundError, download_background
from .dependencies import dependency_snapshot
from .readme import render_readme_html
from .runtime import dashboard_snapshot, plugin_snapshot
from .security import AuthError, Session
from .state import ConsoleState
from .store import StoreError, _clean_output, build_self_update_command, normalize_project_name
from .version import (
    GITHUB_PROXY_PRESETS,
    PACKAGE_GIT_URL,
    PACKAGE_NAME,
    get_installed_version,
    is_mirror_repo,
    normalize_github_proxy,
    probe_mirror_repo,
    resolve_git_url,
    resolve_version_url,
)


class SetupBody(BaseModel):
    setup_token: str = Field(min_length=8, max_length=256)
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=256)


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class ConfigUpdateBody(BaseModel):
    values: dict[str, str]


class ConfigRestoreBody(BaseModel):
    backup_id: str = Field(min_length=1, max_length=255)


class BackgroundUrlBody(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


class PluginActionBody(BaseModel):
    action: Literal["install", "update", "uninstall"]


class GithubInstallBody(BaseModel):
    repository_url: str = Field(min_length=1, max_length=512)
    module_name: str = Field(default="", max_length=128)
    project_name: str = Field(default="", max_length=128)


class PluginDisabledBody(BaseModel):
    plugin: str = Field(min_length=1, max_length=128)
    disabled: bool


class GithubProxyBody(BaseModel):
    proxy: str = Field(default="", max_length=512)


class AttemptLimiter:
    def __init__(self, limit: int = 10, window: int = 300) -> None:
        self.limit = limit
        self.window = window
        self._attempts: dict[str, deque[float]] = defaultdict(deque)

    def _evict_stale(self, now: float) -> None:
        # Drop buckets whose attempts have all aged out so the dict cannot grow
        # without bound when requests arrive from many rotating source IPs.
        stale = [
            key
            for key, values in self._attempts.items()
            if not values or values[-1] < now - self.window
        ]
        for key in stale:
            del self._attempts[key]

    def check(self, key: str) -> None:
        now = time.time()
        self._evict_stale(now)
        values = self._attempts[key]
        while values and values[0] < now - self.window:
            values.popleft()
        if len(values) >= self.limit:
            raise HTTPException(status_code=429, detail="尝试次数过多，请稍后再试")
        values.append(now)

    def clear(self, key: str) -> None:
        self._attempts.pop(key, None)


def merge_source_plugin_records(
    items: list[dict[str, Any]],
    source_records: dict[str, Any],
    disabled: set[str],
) -> list[dict[str, Any]]:
    loaded_modules: set[str] = set()
    for item in items:
        module_name = str(item.get("module") or "")
        loaded_modules.add(module_name)
        item["loaded"] = True
        item["disabled"] = item.get("name") in disabled
        record = source_records.get(module_name)
        if isinstance(record, dict):
            item["source_project"] = str(record.get("project") or "")
            item["source_repository"] = str(record.get("repository") or "")

    for module_name, record in source_records.items():
        module_name = str(module_name)
        if (
            not module_name
            or module_name == "nonebot_plugin_mimo_console"
            or module_name in loaded_modules
            or not isinstance(record, dict)
        ):
            continue
        project_name = str(record.get("project") or "")
        repository = str(record.get("repository") or "")
        items.append(
            {
                "name": module_name,
                "module": module_name,
                "title": project_name or module_name,
                "description": "该 GitHub 源码插件已安装，但当前 NoneBot 进程未成功加载",
                "usage": "",
                "type": "plugin",
                "homepage": repository,
                "icon": "",
                "matchers": 0,
                "path": "",
                "config_keys": [],
                "distribution": project_name,
                "loaded": False,
                "disabled": module_name in disabled,
                "source_project": project_name,
                "source_repository": repository,
            }
        )
    return items


def create_router(state: ConsoleState) -> APIRouter:
    router = APIRouter()
    bearer = HTTPBearer(auto_error=False)
    limiter = AttemptLimiter()
    background_lock = asyncio.Lock()

    def source_plugin_records() -> dict[str, Any]:
        pyproject = state.config.project_root() / "pyproject.toml"
        if not pyproject.is_file():
            return {}
        try:
            document = tomlkit.parse(pyproject.read_text(encoding="utf-8"))
            records = document.get("tool", {}).get("mimo_console", {}).get("source_plugins", {})
        except (OSError, ValueError, TypeError):
            return {}
        return dict(records) if isinstance(records, dict) else {}

    def client_key(request: Request) -> str:
        peer = request.client.host if request.client else "unknown"
        hops = state.config.mimo_console_trusted_proxy_hops
        if hops <= 0:
            return peer
        # Behind N trusted proxies the real client is the Nth-from-last entry of
        # X-Forwarded-For; earlier entries are attacker-supplied and untrusted.
        forwarded = request.headers.get("x-forwarded-for", "")
        chain = [part.strip() for part in forwarded.split(",") if part.strip()]
        if len(chain) >= hops:
            return chain[-hops]
        return peer

    def raw_token(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> str:
        return credentials.credentials if credentials else ""

    def require_session(token: Annotated[str, Depends(raw_token)]) -> Session:
        session = state.auth.verify(token)
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="登录已失效，请重新登录",
            )
        return session

    async def github_readme(homepage: str) -> dict[str, Any]:
        match = re.match(r"https?://github\.com/([^/]+)/([^/?#]+)", homepage)
        if not match:
            return {
                "ok": False,
                "content": "",
                "detail": "主页不是 GitHub 仓库，无法获取 README",
            }
        owner, repo = match.group(1), match.group(2).removesuffix(".git")
        proxy = state.config.mimo_console_github_proxy
        branches = ["main", "master"]
        filenames = ["README.md", "readme.md", "README.rst", "README"]
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            for branch in branches:
                for filename in filenames:
                    if proxy and not is_mirror_repo(proxy):
                        raw_url = (
                            f"{proxy}https://raw.githubusercontent.com/"
                            f"{owner}/{repo}/{branch}/{filename}"
                        )
                    else:
                        raw_url = (
                            f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{filename}"
                        )
                    try:
                        response = await client.get(
                            raw_url,
                            headers={"User-Agent": PACKAGE_NAME},
                        )
                        if response.status_code == 200:
                            return {
                                "ok": True,
                                "content": response.text,
                                "content_html": render_readme_html(response.text),
                                "detail": "",
                                "base_url": (
                                    f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/"
                                ),
                            }
                    except (httpx.HTTPError, OSError):
                        continue
        return {"ok": False, "content": "", "detail": "无法获取 README 文件"}

    def distribution_readme(distribution_name: str) -> tuple[str, str]:
        if not distribution_name:
            return "", ""
        try:
            metadata = cast(Message, importlib.metadata.metadata(distribution_name))
        except importlib.metadata.PackageNotFoundError:
            return "", ""
        homepage = str(metadata.get("Home-page") or "").strip()
        if not homepage:
            for project_url in metadata.get_all("Project-URL") or []:
                label, separator, url = project_url.partition(",")
                if separator and label.strip().casefold() in {
                    "homepage",
                    "repository",
                    "source",
                    "source code",
                }:
                    homepage = url.strip()
                    break
        payload = metadata.get_payload()
        content = payload.strip() if isinstance(payload, str) else ""
        return homepage, content

    @router.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "configured": state.auth.configured}

    @router.get("/api/auth/status")
    async def auth_status() -> dict[str, Any]:
        return {"configured": state.auth.configured}

    @router.post("/api/auth/setup")
    async def setup(body: SetupBody, request: Request) -> dict[str, Any]:
        key = client_key(request)
        limiter.check(f"setup:{key}")
        try:
            token = await asyncio.to_thread(
                state.auth.setup,
                body.setup_token,
                body.username,
                body.password,
            )
        except AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        limiter.clear(f"setup:{key}")
        state.setup_token = None
        return {"token": token, "username": state.auth.username}

    @router.post("/api/auth/login")
    async def login(body: LoginBody, request: Request) -> dict[str, Any]:
        key = client_key(request)
        limiter.check(f"login:{key}")
        try:
            token = await asyncio.to_thread(state.auth.login, body.username, body.password)
        except AuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        limiter.clear(f"login:{key}")
        return {"token": token, "username": state.auth.username}

    @router.get("/api/auth/me")
    async def me(session: Annotated[Session, Depends(require_session)]) -> dict[str, Any]:
        return {"username": session.username, "expires_at": session.expires_at}

    @router.post("/api/auth/logout")
    async def logout(
        token: Annotated[str, Depends(raw_token)],
        session: Annotated[Session, Depends(require_session)],
    ) -> dict[str, bool]:
        await asyncio.to_thread(state.auth.logout, token)
        return {"ok": True}

    @router.get("/api/dashboard")
    async def dashboard(
        session: Annotated[Session, Depends(require_session)],
    ) -> dict[str, Any]:
        return await asyncio.to_thread(dashboard_snapshot, state.config.project_root())

    @router.get("/api/plugins")
    async def plugins(
        session: Annotated[Session, Depends(require_session)],
    ) -> dict[str, Any]:
        disabled = state.disabled.names
        items = await asyncio.to_thread(plugin_snapshot)
        source_records = await asyncio.to_thread(source_plugin_records)
        return {
            "items": merge_source_plugin_records(items, source_records, disabled),
            "package_management": state.config.mimo_console_allow_package_management,
        }

    @router.post("/api/plugins/{plugin_name}/action")
    async def manage_loaded_source_plugin(
        plugin_name: str,
        body: PluginActionBody,
        session: Annotated[Session, Depends(require_session)],
    ) -> dict[str, Any]:
        if not state.config.mimo_console_allow_package_management:
            raise HTTPException(status_code=403, detail="插件安装功能已在配置中关闭")
        if body.action == "install":
            raise HTTPException(status_code=400, detail="此接口只支持更新或卸载")
        items = await plugins(session)
        item = next(
            (
                value
                for value in items["items"]
                if value.get("name") == plugin_name or value.get("module") == plugin_name
            ),
            None,
        )
        records = await asyncio.to_thread(source_plugin_records)
        module_name = (
            plugin_name
            if isinstance(records.get(plugin_name), dict)
            else str((item or {}).get("module") or "")
        )
        if module_name == "nonebot_plugin_mimo_console":
            raise HTTPException(status_code=400, detail="不能在控制台中管理控制台自身")
        record = records.get(module_name)
        if not isinstance(record, dict):
            raise HTTPException(status_code=400, detail="该插件不是由 GitHub 源码安装")
        project_name = str(record.get("project") or "")
        repository = str(record.get("repository") or "")
        if not project_name or not repository:
            raise HTTPException(status_code=400, detail="GitHub 源码插件记录不完整")
        if state.store.action_lock.locked():
            raise HTTPException(status_code=409, detail="另一个插件操作仍在进行中")
        try:
            return await state.store.manage_direct_plugin(
                state.config.project_root(),
                module_name,
                project_name,
                repository,
                body.action,
                state.config.mimo_console_package_timeout,
            )
        except (OSError, ValueError, StoreError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/api/plugins/{plugin_name}/readme")
    async def loaded_plugin_readme(
        plugin_name: str,
        session: Annotated[Session, Depends(require_session)],
    ) -> dict[str, Any]:
        plugins = await asyncio.to_thread(plugin_snapshot)
        item = next(
            (
                plugin
                for plugin in plugins
                if plugin.get("name") == plugin_name or plugin.get("module") == plugin_name
            ),
            None,
        )
        if item is None:
            records = await asyncio.to_thread(source_plugin_records)
            record = records.get(plugin_name)
            if isinstance(record, dict):
                repository = str(record.get("repository") or "")
                if repository:
                    return await github_readme(repository)
            raise HTTPException(status_code=404, detail="插件未加载或不存在")
        metadata_homepage, metadata_readme = await asyncio.to_thread(
            distribution_readme,
            str(item.get("distribution") or ""),
        )
        homepage = str(item.get("homepage") or metadata_homepage).strip()
        result: dict[str, Any] | None = None
        if homepage:
            result = await github_readme(homepage)
            if result["ok"]:
                return result
        if metadata_readme:
            return {
                "ok": True,
                "content": metadata_readme,
                "content_html": render_readme_html(metadata_readme),
                "detail": "",
            }
        if not homepage:
            return {"ok": False, "content": "", "detail": "该插件未提供 README 或主页链接"}
        return result or {"ok": False, "content": "", "detail": "无法获取 README 文件"}

    @router.put("/api/plugins/disabled")
    async def set_plugin_disabled(
        body: PluginDisabledBody,
        session: Annotated[Session, Depends(require_session)],
    ) -> dict[str, Any]:
        name = body.plugin.strip()
        loaded = {item["name"] for item in await asyncio.to_thread(plugin_snapshot)}
        if name not in loaded:
            raise HTTPException(status_code=404, detail="插件未加载或不存在")
        try:
            await asyncio.to_thread(state.disabled.set, name, body.disabled)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        action = "禁用" if body.disabled else "启用"
        logger.warning(f"[Mimo Console] 已{action}插件：{name}")
        return {
            "ok": True,
            "plugin": name,
            "disabled": body.disabled,
            "disabled_plugins": sorted(state.disabled.names),
        }

    @router.get("/api/dependencies")
    async def dependencies(
        session: Annotated[Session, Depends(require_session)],
    ) -> dict[str, Any]:
        plugins = await asyncio.to_thread(plugin_snapshot)
        plugin_distributions = {
            str(item.get("distribution") or "") for item in plugins if item.get("distribution")
        }
        result = await asyncio.to_thread(
            dependency_snapshot,
            state.config.project_root(),
            plugin_distributions,
        )
        result["package_management"] = state.config.mimo_console_allow_package_management
        result["deployment"] = state.deployment.as_dict()
        result["deployment_mode"] = state.deployment.mode
        return result

    @router.post("/api/dependencies/{project_name}/action")
    async def manage_dependency(
        project_name: str,
        body: PluginActionBody,
        session: Annotated[Session, Depends(require_session)],
    ) -> dict[str, Any]:
        if not state.config.mimo_console_allow_package_management:
            raise HTTPException(status_code=403, detail="依赖管理功能已在配置中关闭")
        plugins = await asyncio.to_thread(plugin_snapshot)
        plugin_distributions = {
            str(item.get("distribution") or "") for item in plugins if item.get("distribution")
        }
        snapshot = await asyncio.to_thread(
            dependency_snapshot,
            state.config.project_root(),
            plugin_distributions,
        )
        normalized = normalize_project_name(project_name)
        current = next(
            (item for item in snapshot["items"] if item["normalized_name"] == normalized),
            None,
        )
        if body.action in {"update", "uninstall"}:
            if current is None or not current["direct"]:
                raise HTTPException(status_code=400, detail="只能更新或卸载项目直接依赖")
            if not current["manageable"]:
                detail = (
                    "插件依赖请在插件中心管理"
                    if current["kind"] == "plugin"
                    else "该依赖是 NoneBot 或控制台运行所必需，不能在这里卸载"
                )
                raise HTTPException(status_code=400, detail=detail)
        if state.store.action_lock.locked():
            raise HTTPException(status_code=409, detail="另一个软件包操作仍在进行中")
        try:
            result = await state.store.manage_dependency(
                state.config.project_root(),
                project_name,
                body.action,
                state.config.mimo_console_package_timeout,
            )
        except (OSError, ValueError, StoreError) as exc:
            logger.warning(f"[Mimo Console] 依赖操作失败：{body.action} {project_name}")
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        verb = "已提交" if result.get("status") == "queued" else "已完成"
        logger.success(f"[Mimo Console] {verb}依赖操作：{body.action} {project_name}")
        return result

    @router.get("/api/store/plugins")
    async def store_plugins(
        session: Annotated[Session, Depends(require_session)],
        query: str = Query(default="", max_length=100),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=18, ge=6, le=48),
        official_only: bool = False,
    ) -> dict[str, Any]:
        if not state.config.mimo_console_enable_store:
            raise HTTPException(status_code=403, detail="官方插件商店已在配置中关闭")
        try:
            result = await state.store.page(query, page, page_size, official_only)
        except StoreError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        result["package_management"] = state.config.mimo_console_allow_package_management
        return result

    @router.get("/api/store/plugins/{module_name}")
    async def store_plugin_detail(
        module_name: str,
        session: Annotated[Session, Depends(require_session)],
    ) -> dict[str, Any]:
        if not state.config.mimo_console_enable_store:
            raise HTTPException(status_code=403, detail="官方插件商店已在配置中关闭")
        try:
            item = await state.store.detail(module_name)
        except StoreError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            "item": item,
            "package_management": state.config.mimo_console_allow_package_management,
        }

    @router.post("/api/store/plugins/{module_name}/action")
    async def manage_store_plugin(
        module_name: str,
        body: PluginActionBody,
        session: Annotated[Session, Depends(require_session)],
    ) -> dict[str, Any]:
        if not state.config.mimo_console_enable_store:
            raise HTTPException(status_code=403, detail="官方插件商店已在配置中关闭")
        if not state.config.mimo_console_allow_package_management:
            raise HTTPException(status_code=403, detail="插件安装功能已在配置中关闭")
        if module_name == "nonebot_plugin_mimo_console" and body.action == "uninstall":
            raise HTTPException(status_code=400, detail="不能在控制台中卸载控制台自身")
        if state.store.action_lock.locked():
            raise HTTPException(status_code=409, detail="另一个插件操作仍在进行中")
        try:
            result = await state.store.manage(
                state.config.project_root(),
                module_name,
                body.action,
                state.config.mimo_console_package_timeout,
            )
        except (OSError, ValueError, StoreError) as exc:
            logger.warning(f"[Mimo Console] 插件操作失败：{body.action} {module_name}")
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        verb = "已提交" if result.get("status") == "queued" else "已完成"
        logger.success(f"[Mimo Console] {verb}插件操作：{body.action} {result['project_link']}")
        return result

    @router.post("/api/store/github/install")
    async def install_github_plugin(
        body: GithubInstallBody,
        session: Annotated[Session, Depends(require_session)],
    ) -> dict[str, Any]:
        if not state.config.mimo_console_allow_package_management:
            raise HTTPException(status_code=403, detail="插件安装功能已在配置中关闭")
        if state.store.action_lock.locked():
            raise HTTPException(status_code=409, detail="另一个插件操作仍在进行中")
        try:
            result = await state.store.install_github(
                state.config.project_root(),
                body.repository_url,
                body.module_name,
                body.project_name,
                state.config.mimo_console_package_timeout,
            )
        except (OSError, ValueError, StoreError) as exc:
            logger.warning("[Mimo Console] GitHub 插件安装提交失败")
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        logger.success(f"[Mimo Console] 已提交 GitHub 插件安装：{result['project_link']}")
        return result

    @router.get("/api/store/deployment")
    async def package_deployment(
        session: Annotated[Session, Depends(require_session)],
    ) -> dict[str, Any]:
        try:
            result = await state.store.capabilities()
            result["package_management"] = state.config.mimo_console_allow_package_management
            result["github_install"] = state.config.mimo_console_allow_package_management and bool(
                result.get("github_install", result.get("mode") == "docker-agent")
            )
            result["detection"] = state.deployment.as_dict()
            return result
        except (OSError, ValueError, StoreError) as exc:
            if state.deployment.backend_mode == "docker-agent":
                return {
                    "mode": "docker-agent",
                    "instance_id": state.config.mimo_console_instance_id,
                    "available": False,
                    "persistent_image": True,
                    "rollback": False,
                    "package_management": (state.config.mimo_console_allow_package_management),
                    "github_install": False,
                    "detection": state.deployment.as_dict(),
                    "error": str(exc),
                }
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.get("/api/store/operations")
    async def package_operations(
        session: Annotated[Session, Depends(require_session)],
    ) -> dict[str, Any]:
        try:
            return {"items": await state.store.list_operations()}
        except (OSError, ValueError, StoreError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.get("/api/store/operations/{operation_id}")
    async def package_operation(
        operation_id: str,
        session: Annotated[Session, Depends(require_session)],
    ) -> dict[str, Any]:
        try:
            operation = await state.store.get_operation(operation_id)
        except (OSError, ValueError, StoreError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if operation is None:
            raise HTTPException(status_code=404, detail="操作不存在")
        return operation

    @router.post("/api/store/operations/{operation_id}/rollback")
    async def rollback_package_operation(
        operation_id: str,
        session: Annotated[Session, Depends(require_session)],
    ) -> dict[str, Any]:
        try:
            return await state.store.rollback(operation_id)
        except (OSError, ValueError, StoreError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/api/store/plugins/{module_name}/readme")
    async def store_plugin_readme(
        module_name: str,
        session: Annotated[Session, Depends(require_session)],
    ) -> dict[str, Any]:
        """Fetch and render the GitHub README for a store plugin."""
        if not state.config.mimo_console_enable_store:
            raise HTTPException(status_code=403, detail="官方插件商店已在配置中关闭")
        try:
            item = await state.store.detail(module_name)
        except StoreError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        homepage = (item or {}).get("homepage", "")
        if not homepage:
            return {"ok": False, "content": "", "detail": "该插件未提供主页链接"}
        return await github_readme(homepage)

    @router.get("/api/config")
    async def get_config(
        session: Annotated[Session, Depends(require_session)],
    ) -> dict[str, Any]:
        environment = str(getattr(get_driver().config, "environment", "prod"))
        try:
            snapshot = await state.configuration.read_configuration(environment)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return snapshot.as_dict()

    @router.put("/api/config")
    async def save_config(
        body: ConfigUpdateBody,
        session: Annotated[Session, Depends(require_session)],
    ) -> dict[str, Any]:
        environment = str(getattr(get_driver().config, "environment", "prod"))
        try:
            result = await state.configuration.update_configuration(
                environment,
                body.values,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return result.as_dict()

    @router.get("/api/config/backups")
    async def config_backups(
        session: Annotated[Session, Depends(require_session)],
    ) -> dict[str, Any]:
        environment = str(getattr(get_driver().config, "environment", "prod"))
        try:
            items = await state.configuration.list_configuration_backups(environment)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"items": [item.as_dict() for item in items]}

    @router.post("/api/config/restore")
    async def restore_config(
        body: ConfigRestoreBody,
        session: Annotated[Session, Depends(require_session)],
    ) -> dict[str, Any]:
        environment = str(getattr(get_driver().config, "environment", "prod"))
        try:
            result = await state.configuration.restore_configuration(
                environment,
                body.backup_id,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return result.as_dict()

    @router.get("/api/logs")
    async def logs(
        session: Annotated[Session, Depends(require_session)],
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=300, ge=1, le=1000),
    ) -> dict[str, Any]:
        return {"items": state.logs.list(after=after, limit=limit)}

    @router.delete("/api/logs")
    async def clear_logs(
        session: Annotated[Session, Depends(require_session)],
    ) -> dict[str, bool]:
        state.logs.clear()
        return {"ok": True}

    def background_payload(snap: dict[str, Any]) -> dict[str, Any]:
        if snap["type"] == "url":
            if snap["filename"]:
                return {
                    "source": "url",
                    "url": (
                        f"{state.config.mimo_console_path}/api/background/file/{snap['filename']}"
                    ),
                    "remote_url": snap["url"],
                }
            return {"source": "url", "url": snap["url"]}
        if snap["type"] == "upload" and snap["filename"]:
            return {
                "source": "upload",
                "url": f"{state.config.mimo_console_path}/api/background/file/{snap['filename']}",
            }
        return {"source": "none", "url": ""}

    @router.get("/api/background")
    async def get_background() -> dict[str, Any]:
        return background_payload(await asyncio.to_thread(state.background.snapshot))

    @router.put("/api/background")
    async def set_background_url(
        body: BackgroundUrlBody,
        session: Annotated[Session, Depends(require_session)],
    ) -> dict[str, Any]:
        try:
            async with background_lock:
                filename, content_type, data = await download_background(body.url)
                snap = await asyncio.to_thread(
                    state.background.set_remote_download,
                    body.url,
                    filename,
                    content_type,
                    data,
                )
        except BackgroundError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return background_payload(snap)

    @router.post("/api/background/upload")
    async def upload_background(
        session: Annotated[Session, Depends(require_session)],
        file: UploadFile,
    ) -> dict[str, Any]:
        async with background_lock:
            data = await file.read()
            if len(data) > MAX_BACKGROUND_BYTES:
                raise HTTPException(status_code=413, detail="图片大小不能超过 5MB")
            try:
                snap = await asyncio.to_thread(
                    state.background.set_upload,
                    file.filename or "",
                    file.content_type or "",
                    data,
                )
            except BackgroundError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return background_payload(snap)

    @router.delete("/api/background")
    async def clear_background(
        session: Annotated[Session, Depends(require_session)],
    ) -> dict[str, Any]:
        async with background_lock:
            snap = await asyncio.to_thread(state.background.clear)
        return background_payload(snap)

    @router.get("/api/background/file/{filename}")
    async def serve_background_file(filename: str) -> FileResponse:
        # 该路由不要求登录态：CSS `background: url(...)` 请求无法附带
        # Authorization 头，靠不可猜的随机文件名 + 目录隔离保护。
        try:
            path = await asyncio.to_thread(state.background.resolve_file, filename)
        except BackgroundError:
            raise HTTPException(status_code=404, detail="背景图片不存在") from None
        return FileResponse(path)

    @router.get("/api/system/version")
    async def system_version(
        session: Annotated[Session, Depends(require_session)],
        force: bool = Query(default=False),
    ) -> dict[str, Any]:
        await state.release_cache.fetch(force=force, proxy=state.config.mimo_console_github_proxy)
        return state.release_cache.snapshot(get_installed_version())

    @router.get("/api/system/github-proxy")
    async def get_github_proxy(
        session: Annotated[Session, Depends(require_session)],
    ) -> dict[str, Any]:
        return {
            "proxy": state.config.mimo_console_github_proxy,
            "presets": list(GITHUB_PROXY_PRESETS),
        }

    @router.put("/api/system/github-proxy")
    async def set_github_proxy(
        body: GithubProxyBody,
        session: Annotated[Session, Depends(require_session)],
    ) -> dict[str, Any]:
        try:
            proxy = normalize_github_proxy(body.proxy)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        environment = str(getattr(get_driver().config, "environment", "prod"))
        try:
            await state.configuration.update_configuration(
                environment,
                {"MIMO_CONSOLE_GITHUB_PROXY": proxy},
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # 热更新内存配置，无需重启即可生效
        state.config.mimo_console_github_proxy = proxy
        logger.info(f"[Mimo Console] GitHub 加速已设置为：{proxy or '直连'}")
        return {"ok": True, "proxy": proxy}

    @router.post("/api/system/github-proxy/test")
    async def test_github_proxy(
        body: GithubProxyBody,
        session: Annotated[Session, Depends(require_session)],
    ) -> dict[str, Any]:
        try:
            proxy = normalize_github_proxy(body.proxy)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        started = time.perf_counter()
        if is_mirror_repo(proxy):
            # 镜像仓库无 raw 直链，用 git ls-remote 探测可达性
            reachable = await probe_mirror_repo(proxy)
            latency = int((time.perf_counter() - started) * 1000)
            if not reachable:
                return {
                    "ok": False,
                    "latency_ms": None,
                    "detail": "镜像仓库无法通过 git 匿名访问",
                }
            return {"ok": True, "latency_ms": latency, "detail": ""}
        url = resolve_version_url(proxy)
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                response = await client.get(url, headers={"User-Agent": PACKAGE_NAME})
            latency = int((time.perf_counter() - started) * 1000)
        except (httpx.HTTPError, OSError) as exc:
            return {"ok": False, "latency_ms": None, "detail": f"连接失败：{exc}"}
        if response.status_code != 200:
            return {
                "ok": False,
                "latency_ms": latency,
                "detail": f"加速地址返回 HTTP {response.status_code}",
            }
        return {"ok": True, "latency_ms": latency, "detail": ""}

    @router.post("/api/system/update")
    async def system_update(
        session: Annotated[Session, Depends(require_session)],
    ) -> dict[str, Any]:
        if state.store.action_lock.locked():
            raise HTTPException(status_code=409, detail="另一个插件操作仍在进行中")
        if state.deployment.backend_mode == "docker-agent":
            try:
                return await state.store.update_self(
                    state.config.project_root(),
                    PACKAGE_GIT_URL,
                    state.config.mimo_console_package_timeout,
                )
            except (OSError, ValueError, StoreError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        command = build_self_update_command(
            state.config.project_root(),
            PACKAGE_NAME,
            resolve_git_url(state.config.mimo_console_github_proxy),
        )
        env = os.environ.copy()
        env.update({"NO_COLOR": "1", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
        async with state.store.action_lock:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=state.config.project_root(),
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                output, _ = await asyncio.wait_for(
                    process.communicate(),
                    timeout=state.config.mimo_console_package_timeout,
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                raise HTTPException(
                    status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                    detail=f"更新超过 {state.config.mimo_console_package_timeout} 秒，已终止",
                ) from None
        clean = _clean_output(output)
        if process.returncode != 0:
            logger.warning("[Mimo Console] 自更新失败")
            raise HTTPException(
                status_code=400,
                detail=clean or f"更新失败（{process.returncode}）",
            )
        logger.success("[Mimo Console] 已完成自更新，需要重启")
        return {"ok": True, "restart_required": True, "output": clean}

    @router.post("/api/system/restart")
    async def restart_nonebot(
        session: Annotated[Session, Depends(require_session)],
    ) -> dict[str, Any]:
        if state.deployment.backend_mode == "docker-agent":
            try:
                return await state.store.restart()
            except (OSError, ValueError, StoreError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        # 派一个 detached 子进程 watcher：当前进程退出、端口释放后，
        # watcher 用原启动命令重新执行，实现自重启——不依赖任何外部进程管理器。
        # 代价：新进程脱离原托管（如 MCSManager 面板会显示 stopped，但子进程在跑）。
        restarter = (
            "import os, time\n"
            "time.sleep(2.5)\n"
            "args = os.environ['MIMO_RESTART_ARGS'].split('\\x1f')\n"
            "os.execvp(args[0], args)\n"
        )
        env = os.environ.copy()
        env["MIMO_RESTART_ARGS"] = "\x1f".join([sys.executable, *sys.argv])
        subprocess.Popen(
            [sys.executable, "-c", restarter],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=os.getcwd(),
            env=env,
        )
        logger.warning("[Mimo Console] 收到重启请求，已派出 watcher，进程即将退出")
        asyncio.get_running_loop().call_later(1.5, lambda: os.kill(os.getpid(), signal.SIGTERM))
        return {"ok": True, "restart_required": True}

    index = state.static_dir / "index.html"

    @router.get("", include_in_schema=False)
    async def index_redirect() -> RedirectResponse:
        return RedirectResponse(f"{state.config.mimo_console_path}/")

    @router.get("/", include_in_schema=False)
    async def index_page() -> HTMLResponse:
        html = index.read_text(encoding="utf-8")
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    return router
