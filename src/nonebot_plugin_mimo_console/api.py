import asyncio
import time
from collections import defaultdict, deque
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from nonebot import get_driver, logger
from pydantic import BaseModel, Field

from .env_editor import locate_env_file, read_env, update_env
from .runtime import dashboard_snapshot, plugin_snapshot
from .security import AuthError, Session
from .state import ConsoleState
from .store import StoreError


class SetupBody(BaseModel):
    setup_token: str = Field(min_length=8, max_length=256)
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=256)


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class ConfigUpdateBody(BaseModel):
    values: dict[str, str]


class PluginActionBody(BaseModel):
    action: Literal["install", "update", "uninstall"]


class AttemptLimiter:
    def __init__(self, limit: int = 10, window: int = 300) -> None:
        self.limit = limit
        self.window = window
        self._attempts: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> None:
        now = time.time()
        values = self._attempts[key]
        while values and values[0] < now - self.window:
            values.popleft()
        if len(values) >= self.limit:
            raise HTTPException(status_code=429, detail="尝试次数过多，请稍后再试")
        values.append(now)

    def clear(self, key: str) -> None:
        self._attempts.pop(key, None)


def create_router(state: ConsoleState) -> APIRouter:
    router = APIRouter()
    bearer = HTTPBearer(auto_error=False)
    limiter = AttemptLimiter()

    def client_key(request: Request) -> str:
        return request.client.host if request.client else "unknown"

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
        state.auth.logout(token)
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
        return {"items": await asyncio.to_thread(plugin_snapshot)}

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
        logger.success(f"[Mimo Console] 已完成插件操作：{body.action} {result['project_link']}")
        return result

    @router.get("/api/config")
    async def get_config(
        session: Annotated[Session, Depends(require_session)],
    ) -> dict[str, Any]:
        environment = str(getattr(get_driver().config, "environment", "prod"))
        path = locate_env_file(state.config.project_root(), environment)
        items = await asyncio.to_thread(read_env, path)
        return {
            "path": str(path),
            "items": [entry.__dict__ for entry in items],
        }

    @router.put("/api/config")
    async def save_config(
        body: ConfigUpdateBody,
        session: Annotated[Session, Depends(require_session)],
    ) -> dict[str, Any]:
        environment = str(getattr(get_driver().config, "environment", "prod"))
        path = locate_env_file(state.config.project_root(), environment)
        try:
            await asyncio.to_thread(update_env, path, body.values, state.backup_dir)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "restart_required": True, "path": str(path)}

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

    index = state.static_dir / "index.html"

    @router.get("", include_in_schema=False)
    async def index_redirect() -> RedirectResponse:
        return RedirectResponse(f"{state.config.mimo_console_path}/")

    @router.get("/", include_in_schema=False)
    async def index_page() -> FileResponse:
        return FileResponse(index, headers={"Cache-Control": "no-store"})

    return router
