from __future__ import annotations

import argparse
import os
import socket
from pathlib import Path

import uvicorn

from .api import create_app
from .config import AgentConfig


def main() -> None:
    parser = argparse.ArgumentParser(description="Mimo Console Docker deployment agent")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("MIMO_AGENT_CONFIG", "/etc/mimo-agent.json")),
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="validate configuration and exit",
    )
    arguments = parser.parse_args()
    config = AgentConfig.load(arguments.config)
    if arguments.check_config:
        print(f"Agent 配置有效：{len(config.instances)} 个实例")
        return
    config.state_dir.mkdir(parents=True, exist_ok=True)
    config.socket_path.parent.mkdir(parents=True, exist_ok=True)
    if config.socket_path.exists() and not config.socket_path.is_socket():
        raise RuntimeError(f"Agent Socket 路径已被普通文件占用：{config.socket_path}")
    if config.socket_path.is_socket():
        config.socket_path.unlink()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old_umask = os.umask(0o077)
    try:
        listener.bind(str(config.socket_path))
    finally:
        os.umask(old_umask)
    os.chmod(config.socket_path, config.socket_mode)
    if config.socket_gid is not None:
        os.chown(config.socket_path, -1, config.socket_gid)
    listener.listen(2048)
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(config),
            log_level="info",
            proxy_headers=False,
            server_header=False,
        )
    )
    try:
        server.run(sockets=[listener])
    finally:
        listener.close()
        if config.socket_path.is_socket():
            config.socket_path.unlink()


if __name__ == "__main__":
    main()
