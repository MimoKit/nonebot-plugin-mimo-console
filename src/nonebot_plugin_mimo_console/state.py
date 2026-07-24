from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .background import BackgroundStore
from .config import ConsoleConfig
from .log_buffer import LogBuffer
from .security import AuthStore
from .store import PluginStore
from .version import LatestReleaseCache


@dataclass
class ConsoleState:
    config: ConsoleConfig
    auth: AuthStore
    logs: LogBuffer
    static_dir: Path
    backup_dir: Path
    store: PluginStore
    background: BackgroundStore
    release_cache: LatestReleaseCache
    setup_token: str | None = None
    log_sink_id: int | None = None
