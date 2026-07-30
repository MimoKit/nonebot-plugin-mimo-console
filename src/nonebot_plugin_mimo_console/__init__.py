from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.staticfiles import StaticFiles
from nonebot import get_app, get_driver, get_plugin_config, logger, on_command, require
from nonebot.exception import IgnoredException
from nonebot.matcher import Matcher
from nonebot.message import run_preprocessor
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata

from .api import create_router
from .backends import (
    DockerAgentBackend,
    LocalConfigurationBackend,
    LocalPackageBackend,
)
from .background import BackgroundStore
from .config import ConsoleConfig
from .deployment import detect_deployment
from .disabled import DisabledStore
from .log_buffer import LogBuffer
from .security import AuthStore
from .state import ConsoleState
from .store import PluginStore
from .version import LatestReleaseCache

require("nonebot_plugin_localstore")
import nonebot_plugin_localstore as localstore  # noqa: E402,I001

__plugin_meta__ = PluginMetadata(
    name="Mimo Console",
    description="NoneBot2 的轻量 WebUI 管理面板",
    usage="启动后打开 /mimo-console，或由超级用户发送 mimo控制台",
    type="application",
    homepage="https://github.com/MimoKit/nonebot-plugin-mimo-console",
    config=ConsoleConfig,
    supported_adapters=None,
)


console_config = get_plugin_config(ConsoleConfig)
project_root = console_config.project_root()
backup_dir = localstore.get_plugin_data_dir() / "backups"
deployment = detect_deployment(console_config)
if deployment.backend_mode == "docker-agent":
    package_backend = DockerAgentBackend(
        socket_path=console_config.mimo_console_agent_socket,
        token_file=console_config.mimo_console_agent_token_file,
        instance_id=console_config.mimo_console_instance_id,
    )
    configuration_backend = package_backend
else:
    package_backend = LocalPackageBackend(deployment_mode=deployment.mode)
    configuration_backend = LocalConfigurationBackend(project_root, backup_dir)
static_dir = Path(__file__).parent / "static"
console_path = console_config.mimo_console_path
auth = AuthStore(
    localstore.get_plugin_data_file("auth.json"),
    console_config.mimo_console_session_hours,
)
console_state = ConsoleState(
    config=console_config,
    deployment=deployment,
    auth=auth,
    logs=LogBuffer(
        ignored_fragments=(
            f"{console_path}/api/logs",
            f"{console_path}/api/dashboard",
        )
    ),
    static_dir=static_dir,
    configuration=configuration_backend,
    store=PluginStore(
        console_config.mimo_console_store_cache_seconds,
        backend=package_backend,
    ),
    background=BackgroundStore(
        data_file=localstore.get_plugin_data_file("background.json"),
        image_dir=localstore.get_plugin_data_dir() / "backgrounds",
    ),
    release_cache=LatestReleaseCache(),
    disabled=DisabledStore(
        localstore.get_plugin_data_file("disabled_plugins.json"),
        protected=("nonebot_plugin_mimo_console",),
    ),
)


@run_preprocessor
async def _disabled_plugin_guard(matcher: Matcher) -> None:
    """被控制台禁用的插件，其响应器直接忽略事件（后台任务不受影响）。"""
    plugin_name = matcher.plugin_name or ""
    if plugin_name and console_state.disabled.is_disabled(plugin_name):
        raise IgnoredException(f"插件 {plugin_name} 已被 Mimo Console 禁用")


def _mount_console() -> bool:
    try:
        app = get_app()
    except AssertionError:
        app = None
    if (
        app is None
        or not callable(getattr(app, "include_router", None))
        or not callable(getattr(app, "mount", None))
    ):
        logger.warning(
            "[Mimo Console] 当前 Driver 未提供可用的 ASGI App，插件已加载但 WebUI 未启用；"
            "请配置 DRIVER=~fastapi"
        )
        return False
    app.include_router(create_router(console_state), prefix=console_path)
    app.mount(
        f"{console_path}/assets",
        StaticFiles(directory=static_dir / "assets"),
        name="mimo-console-assets",
    )
    return True


console_mounted = _mount_console()

driver = get_driver()
console_command = on_command("mimo控制台", aliases={"NoneBot控制台"}, permission=SUPERUSER)


@driver.on_startup
async def _console_startup() -> None:
    console_state.log_sink_id = logger.add(console_state.logs.sink, level="INFO", catch=True)
    console_state.setup_token = await asyncio.to_thread(console_state.auth.issue_setup_token)
    path = console_config.mimo_console_path
    if console_mounted:
        logger.success(f"[Mimo Console] WebUI 已挂载：{path}")
    else:
        logger.warning("[Mimo Console] WebUI 未挂载，当前 Driver 不支持 ASGI 服务")
    if console_state.setup_token:
        logger.warning("[Mimo Console] 首次初始化令牌（仅本次启动有效）：")
        logger.warning(f"[Mimo Console] {console_state.setup_token}")


@driver.on_shutdown
async def _console_shutdown() -> None:
    if console_state.log_sink_id is not None:
        logger.remove(console_state.log_sink_id)


@console_command.handle()
async def _show_console_address() -> None:
    host = str(getattr(driver.config, "host", "127.0.0.1"))
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    port = int(getattr(driver.config, "port", 8080))
    message = f"Mimo Console：\nhttp://{host}:{port}{console_config.mimo_console_path}"
    if not console_state.auth.configured:
        message += "\n首次使用请从 NoneBot 启动日志复制初始化令牌。"
    await console_command.finish(message)
