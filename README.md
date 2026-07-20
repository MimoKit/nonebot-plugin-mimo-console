<div align="center">
  <a href="https://nonebot.dev/store/plugins">
    <img src="https://raw.githubusercontent.com/fllesser/nonebot-plugin-template/refs/heads/resource/.docs/NoneBotPlugin.svg" width="310" alt="NoneBot Plugin">
  </a>

# Mimo Console

随 NoneBot2 运行的玻璃风 WebUI 管理面板。

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

超级用户也可以发送 `mimo控制台` 或 `NoneBot控制台` 获取访问地址。

## 数据与安全

- 管理员数据由 `nonebot-plugin-localstore` 保存，不会写进插件安装目录。
- Token、Secret、Password、Cookie、API Key 等配置值默认脱敏。
- 配置修改会生成备份，重启 NoneBot 后生效。
- 公网部署建议在反向代理层启用 HTTPS 和额外访问限制。

## 本地开发

```bash
uv sync --all-groups
uv run poe test
uv run ruff check .
uv build
```

项目使用 GPL-3.0 协议。
