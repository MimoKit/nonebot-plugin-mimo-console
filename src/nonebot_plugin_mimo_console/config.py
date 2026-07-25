from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from .version import normalize_github_proxy


class ConsoleConfig(BaseModel):
    """NoneBot 环境变量中的控制台配置。"""

    mimo_console_path: str = "/mimo-console"
    mimo_console_project_root: Path | None = None
    mimo_console_session_hours: int = Field(default=72, ge=1, le=720)
    mimo_console_enable_store: bool = True
    mimo_console_allow_package_management: bool = True
    mimo_console_store_cache_seconds: int = Field(default=600, ge=60, le=86400)
    mimo_console_package_timeout: int = Field(default=300, ge=60, le=1800)
    mimo_console_background_url: str | None = None
    mimo_console_github_proxy: str = ""

    @field_validator("mimo_console_path")
    @classmethod
    def normalize_path(cls, value: str) -> str:
        path = "/" + value.strip().strip("/")
        return path if path != "/" else "/mimo-console"

    @field_validator("mimo_console_github_proxy")
    @classmethod
    def normalize_proxy(cls, value: str) -> str:
        return normalize_github_proxy(value)

    def project_root(self) -> Path:
        return (self.mimo_console_project_root or Path.cwd()).expanduser().resolve()
