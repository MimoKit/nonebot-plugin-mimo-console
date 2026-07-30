from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .backends import ConfigurationBackend
from .background import BackgroundStore
from .config import ConsoleConfig
from .deployment import DeploymentDetection
from .disabled import DisabledStore
from .log_buffer import LogBuffer
from .security import AuthStore
from .store import PluginStore
from .version import LatestReleaseCache


@dataclass
class ConsoleState:
    config: ConsoleConfig
    deployment: DeploymentDetection
    auth: AuthStore
    logs: LogBuffer
    static_dir: Path
    configuration: ConfigurationBackend
    store: PluginStore
    background: BackgroundStore
    release_cache: LatestReleaseCache
    disabled: DisabledStore
    setup_token: str | None = None
    log_sink_id: int | None = None
