# Mimo Console Docker Agent

这个 Agent 是 Mimo Console Docker 模式的宿主机执行端。它只接受固定的插件
安装、更新、卸载、重启和回滚操作，不提供任意命令接口。

## 前置条件

- Linux Docker Engine。
- Docker Compose 2.24.4 或更高版本。
- NoneBot 项目包含 `pyproject.toml`、`uv.lock`、Dockerfile 和 Compose 文件。
- Dockerfile 使用 `uv.lock` 构建依赖。
- Mimo Console 的 `/api/health` 可通过宿主机回环地址访问。

## 安装、升级与卸载 Agent

仓库内置了幂等的 systemd 管理脚本。准备好 Agent JSON 配置和实例令牌后执行：

```bash
sudo agent/scripts/manage-service.sh install \
  --source ./agent \
  --config /etc/mimo-console-agent/agent.json

sudo agent/scripts/manage-service.sh upgrade \
  --source ./agent \
  --config /etc/mimo-console-agent/agent.json

sudo agent/scripts/manage-service.sh uninstall
```

安装和升级会先在临时目录完成锁文件同步与配置校验，再切换运行目录并重启
服务；新服务启动失败时会自动恢复旧运行目录，启动成功后清理临时备份。卸载只
移除运行目录和 systemd 服务，不删除 `/etc/mimo-console-agent` 中的配置、令牌或
`/var/lib/mimo-console-agent` 中的操作记录。

也可以手动安装：

把仓库放到 `/opt/nonebot-plugin-mimo-console` 后执行：

```bash
cd /opt/nonebot-plugin-mimo-console
docker build \
  -f agent/docker/uv-git.Dockerfile \
  -t local/mimo-console-uv-git:0.9.29-1 \
  agent
uv sync --project agent --python /usr/bin/python3 --frozen
install -d -m 0750 \
  /etc/mimo-console-agent \
  /var/lib/mimo-console-agent \
  /var/lib/mimo-console-agent/docker-config
groupadd --system --gid 1999 mimo-console
install -d -m 0750 -o root -g 1999 /run/mimo-agent
openssl rand -hex 32 > /etc/mimo-console-agent/personal.token
chgrp 1999 /etc/mimo-console-agent/personal.token
chmod 0640 /etc/mimo-console-agent/personal.token
```

复制并修改：

- `examples/mimo-agent.json` → `/etc/mimo-console-agent/agent.json`
- `examples/mimo-console-agent.service` →
  `/etc/systemd/system/mimo-console-agent.service`

示例使用 `1999`；如果这个 GID 已占用，请同时更换 groupadd、`socket_gid`、
令牌文件组和 Compose `group_add` 中的数字。这个专用组只授予连接 Socket 和
读取当前实例令牌的能力。

```bash
systemctl daemon-reload
systemctl enable --now mimo-console-agent
systemctl status mimo-console-agent
```

systemd 服务中的 `DOCKER_CONFIG` 必须指向 `ReadWritePaths` 允许写入的专用
目录；否则 `ProtectHome=true` 会阻止 Docker Buildx 初始化配置。如果项目不在
`/srv/nonebot-personal`，也必须同步修改 `ReadWritePaths`。多个 NoneBot
实例需要分别加入 `instances`，使用不同的 `instance_id`、令牌、项目目录、
Compose 项目名、镜像仓库和健康检查端口。

## NoneBot Compose 连接

在受管服务中只挂载 Agent Socket 和当前实例令牌，不要挂载 Docker Socket：

```yaml
services:
  bot:
    build: .
    restart: unless-stopped
    ports:
      - "127.0.0.1:18080:8080"
    env_file:
      - .env.prod
    group_add:
      - "1999"
    volumes:
      - /run/mimo-agent:/run/mimo-agent
      - /etc/mimo-console-agent/personal.token:/run/secrets/mimo-agent-token:ro
      - ./data/localstore:/app/.localstore
```

NoneBot 环境变量：

```dotenv
MIMO_CONSOLE_DEPLOYMENT_MODE=docker-agent
MIMO_CONSOLE_INSTANCE_ID=personal
MIMO_CONSOLE_AGENT_SOCKET=/run/mimo-agent/agent.sock
MIMO_CONSOLE_AGENT_TOKEN_FILE=/run/secrets/mimo-agent-token
LOCALSTORE_DATA_DIR=/app/.localstore/data
LOCALSTORE_CONFIG_DIR=/app/.localstore/config
LOCALSTORE_CACHE_DIR=/app/.localstore/cache
```

必须挂载 Socket 所在目录，而不是直接挂载 Socket 文件。这样 Agent
重启并重新创建 Socket 后，运行中的 NoneBot 容器仍能连接。首次启用时先启动
Agent，再创建或重建 NoneBot 容器。

## 部署事务

Agent 会在隔离副本中修改依赖，通过受限 UV 工具容器解析锁文件，再构建唯一
镜像。镜像模块验证通过后才写回 `pyproject.toml` 和 `uv.lock`，并只重建配置
的 Compose 服务。新容器或 WebUI 健康检查失败时，会恢复旧清单和旧镜像。

操作记录保存在 `state_dir/operations.sqlite3`。Agent 意外重启后，尚未部署的
任务标记为失败；已经进入部署阶段的任务会自动恢复旧容器。

## 配置文件持久化

每个实例的 `environment_file` 指定唯一允许 WebUI 读写的 dotenv 文件，默认
为项目内的 `.env.prod`。路径必须位于 `project_root` 内，NoneBot 容器不能从
请求中指定其他宿主机路径。

Agent 在返回配置时脱敏 Token、Password、Cookie 等敏感字段。保存时保留注释
和未修改的敏感值，使用同目录原子替换，并把旧文件备份到
`state_dir/environment-backups/<instance_id>`。`environment_backup_keep`
控制每个实例最多保留的备份数，默认 20。WebUI 可以列出并还原这些备份；还原
前会再次保存当前配置。更新或还原后需要通过 WebUI 重启实例，使 Compose
`env_file` 中的新值生效。

## 多实例隔离与镜像回收

一个 Agent 可以管理多个 NoneBot 实例，但各实例必须使用独立的项目目录、
Compose 项目名、令牌、环境文件、override、镜像仓库和健康检查地址。Agent
启动时会拒绝存在资源冲突的配置，防止个人 QQ 与官方 QQ 的操作串到另一实例。

每次部署成功后，Agent 会回收当前实例镜像仓库中的旧 `mimo-*` 镜像，同时保护
当前镜像、可回滚旧镜像和 `keep_images` 指定数量的近期镜像。清理失败只记录
警告，不会把已经健康的部署标记为失败。

## 当前预览限制

- 支持 NoneBot 官方商店插件，以及使用 GitHub HTTPS 仓库地址安装的
  `nonebot_plugin_*` 源码插件。源码插件声明的依赖必须是普通 PyPI 依赖，
  不接受本地路径、额外 VCS 地址或任意安装参数。
- 只支持 UV 项目；PDM、Poetry 和 requirements-only 项目将在后续阶段加入。
