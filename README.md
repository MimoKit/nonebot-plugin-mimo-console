<div align="center">
  <a href="https://nonebot.dev/store/plugins">
    <img src="https://raw.githubusercontent.com/fllesser/nonebot-plugin-template/refs/heads/resource/.docs/NoneBotPlugin.svg" width="310" alt="NoneBot Plugin">
  </a>

# Mimo Console

随 NoneBot2 运行的 WebUI 管理面板。

[![PyPI](https://img.shields.io/pypi/v/nonebot-plugin-mimo-console.svg)](https://pypi.org/project/nonebot-plugin-mimo-console/)
[![Python](https://img.shields.io/badge/Python-3.10%20--%203.14-3776ab?logo=python&logoColor=white)](https://www.python.org/)
[![CI](https://github.com/MimoKit/nonebot-plugin-mimo-console/actions/workflows/ci.yml/badge.svg)](https://github.com/MimoKit/nonebot-plugin-mimo-console/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-GPLv3-6b7280.svg)](./LICENSE)

</div>

## 能做什么

- 查看 CPU、内存、磁盘、网络、进程占用和运行时间。
- 查看当前连接的 Bot、适配器和已加载插件。
- 搜索 NoneBot 官方插件商店，安装、更新或卸载插件。
- 在网页中修改 dotenv 配置，敏感字段自动脱敏，保存前自动备份。
- 搜索和筛选当前进程日志。
- 自定义 WebUI 背景图，支持远程链接或本地上传。
- 检测 GitHub 最新版本并一键更新，概览页支持重启 NoneBot。
- 使用初始化令牌创建管理员，后续通过账号密码登录。

前端资源随插件一起安装，不依赖单独的 Web 服务，也不绑定任何消息适配器。

## 安装

使用 NB-CLI：

```bash
nb plugin install nonebot-plugin-mimo-console
```

或使用 uv / pip：

```bash
uv add nonebot-plugin-mimo-console
# pip install nonebot-plugin-mimo-console
```

使用 uv 或 pip 安装后，请确认 NoneBot 项目的 `pyproject.toml` 已加载插件：

```toml
[tool.nonebot]
plugins = ["nonebot_plugin_mimo_console"]
```

项目需要启用 FastAPI 驱动：

```dotenv
DRIVER=~fastapi
```

启动 NoneBot 后，日志会显示 WebUI 地址和首次初始化令牌。默认地址：

```text
http://127.0.0.1:8080/mimo-console/
```

第一次打开时输入日志中的令牌并创建管理员；完成初始化后只需使用管理员账号登录。

## 配置

所有配置均可省略。

| 配置项 | 默认值 | 作用 |
| --- | --- | --- |
| `MIMO_CONSOLE_PATH` | `/mimo-console` | WebUI 挂载路径 |
| `MIMO_CONSOLE_PROJECT_ROOT` | 当前工作目录 | NoneBot 项目目录 |
| `MIMO_CONSOLE_SESSION_HOURS` | `72` | 登录有效时长，范围 1-720 小时 |
| `MIMO_CONSOLE_ENABLE_STORE` | `true` | 显示官方插件商店 |
| `MIMO_CONSOLE_ALLOW_PACKAGE_MANAGEMENT` | `true` | 允许安装、更新和卸载插件 |
| `MIMO_CONSOLE_STORE_CACHE_SECONDS` | `600` | 商店数据缓存时间 |
| `MIMO_CONSOLE_PACKAGE_TIMEOUT` | `300` | 插件操作超时时间（秒） |
| `MIMO_CONSOLE_DEPLOYMENT_MODE` | `auto` | 安装方式：`auto` 自动识别，也可强制为 `local` 或 `docker-agent` |
| `MIMO_CONSOLE_INSTANCE_ID` | `default` | Docker Agent 中配置的实例 ID |
| `MIMO_CONSOLE_AGENT_SOCKET` | `/run/mimo-agent/agent.sock` | Docker Agent Unix Socket |
| `MIMO_CONSOLE_AGENT_TOKEN_FILE` | `/run/secrets/mimo-agent-token` | 当前实例的 Agent 令牌文件 |
| `MIMO_CONSOLE_GITHUB_PROXY` | 空 | GitHub 加速前缀或镜像仓库地址，用于 README、版本检查和自更新 |

超级用户也可以发送 `mimo控制台` 或 `NoneBot控制台` 获取访问地址。

## Docker 部署预览

默认会自动识别普通 Python、无 Agent 的 Docker 容器和 Docker + Mimo Agent。
普通 Python 环境会直接维护当前项目；无 Agent 的 Docker 容器也可操作，但变更
只作用于当前容器，重建后可能丢失。Docker + Agent 模式不会在运行中的 NoneBot
容器里安装依赖，也不会把 Docker Socket
暴露给插件。宿主机上的受限 Agent 会更新项目锁文件、构建并验证新镜像、替换
指定 Compose 服务，并在健康检查失败时自动回滚。WebUI 的配置编辑也由 Agent
持久化到宿主机项目的指定 dotenv 文件，容器重建后不会丢失。

第一版支持 Linux Docker Engine、Docker Compose 2.24.4+ 和带 `uv.lock` 的
标准 NoneBot 项目。完整安装和多实例配置见
[`agent/README.md`](https://github.com/MimoKit/nonebot-plugin-mimo-console/blob/master/agent/README.md)，
设计与安全边界见
[`docs/docker-deployment.md`](https://github.com/MimoKit/nonebot-plugin-mimo-console/blob/master/docs/docker-deployment.md)。

## 版本更新与重启

- 概览页显示当前版本，并与 GitHub 最新 release 比对，有新版时可一键通过 `nb plugin update` 自更新（结果缓存约 30 分钟）。
- 更新与修改配置一样，需要重启 NoneBot 才能生效。
- 概览页的「重启」按钮会发送 `SIGINT` 让进程优雅退出，**需要 NoneBot 进程由外部进程管理器托管**（systemd / MCSManager autoRestart / pm2 / Docker `restart=always` 等）才会自动重新拉起；未托管时进程会停止。

## 数据与安全

- Mimo Console 具有修改 dotenv、管理 Python 包、查看日志、禁用插件响应器和触发进程重启等高权限能力，其权限等同于运行 NoneBot 的系统用户。
- 不建议将 WebUI 直接暴露到公网；确需公网访问时，应在反向代理层启用 HTTPS、来源限制或额外认证。
- 不需要在线安装、更新和卸载插件时，建议设置 `MIMO_CONSOLE_ALLOW_PACKAGE_MANAGEMENT=false`。
- 管理员数据由 `nonebot-plugin-localstore` 保存，不会写进插件安装目录。
- Token、Secret、Password、Cookie、API Key 等配置值默认脱敏。
- 配置修改会生成备份，重启 NoneBot 后生效；修改项目配置和依赖前仍建议自行备份。
- 自定义背景图保存在 localstore 数据目录，上传文件名随机化；远程 URL 会由服务端下载为本地文件，并限制为公网 80/443 地址、受支持的图片格式和 5MB 以内。
- “禁用插件”只会阻止对应插件的 Matcher 继续响应事件，不会停止其已注册的后台任务；Mimo Console 自身不可被禁用。

## 本地开发

```bash
uv sync --all-groups
uv run poe test
uv run ruff check .
uv build
```

项目使用 GPL-3.0 协议。

## Star History

<div align="center">
  <a href="https://www.star-history.com/?repos=MimoKit%2Fnonebot-plugin-mimo-console&type=date&legend=top-left">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=MimoKit/nonebot-plugin-mimo-console&type=Date&theme=dark&legend=top-left">
      <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=MimoKit/nonebot-plugin-mimo-console&type=Date&legend=top-left">
      <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=MimoKit/nonebot-plugin-mimo-console&type=Date&legend=top-left">
    </picture>
  </a>
</div>

## 致谢

感谢所有参与 Mimo Console 开发、测试与改进的贡献者：

- [MimoKit](https://github.com/MimoKit)
- [M / yiwuerxin](https://github.com/yiwuerxin)
- [spaxie](https://github.com/spaxie)
- [syuan326](https://github.com/syuan326)
- [gufei233](https://github.com/gufei233)
