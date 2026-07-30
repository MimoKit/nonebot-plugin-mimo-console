from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .version import normalize_github_proxy


class ConsoleConfig(BaseModel):
    """NoneBot 环境变量中的控制台配置。"""

    mimo_console_path: str = "/mimo-console"
    mimo_console_project_root: Path | None = None
    mimo_console_session_hours: int = Field(default=72, ge=1, le=720)
    mimo_console_enable_store: bool = True
    mimo_console_allow_package_management: bool = True
    # Set to the number of trusted reverse-proxy hops in front of the console so
    # login rate limiting keys on the real client IP. Leave at 0 (default) when
    # directly exposed — X-Forwarded-For is attacker-controlled and ignored then.
    mimo_console_trusted_proxy_hops: int = Field(default=0, ge=0, le=16)
    mimo_console_store_cache_seconds: int = Field(default=600, ge=60, le=86400)
    mimo_console_package_timeout: int = Field(default=300, ge=60, le=1800)
    mimo_console_github_proxy: str = ""
    mimo_console_deployment_mode: Literal["auto", "local", "docker-agent"] = "auto"
    mimo_console_instance_id: str = "default"
    mimo_console_agent_socket: Path = Path("/run/mimo-agent/agent.sock")
    mimo_console_agent_token_file: Path = Path("/run/secrets/mimo-agent-token")

    @field_validator("mimo_console_path")
    @classmethod
    def normalize_path(cls, value: str) -> str:
        path = "/" + value.strip().strip("/")
        return path if path != "/" else "/mimo-console"

    @field_validator("mimo_console_github_proxy")
    @classmethod
    def normalize_proxy(cls, value: str) -> str:
        return normalize_github_proxy(value)

    @field_validator("mimo_console_instance_id")
    @classmethod
    def validate_instance_id(cls, value: str) -> str:
        instance_id = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", instance_id):
            raise ValueError("Mimo Console 实例 ID 不合法")
        return instance_id

    def project_root(self) -> Path:
        return (self.mimo_console_project_root or Path.cwd()).expanduser().resolve()
