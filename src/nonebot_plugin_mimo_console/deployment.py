from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .config import ConsoleConfig

DetectedMode = Literal["python", "docker-local", "docker-agent"]


def is_containerized() -> bool:
    if Path("/.dockerenv").is_file():
        return True
    return os.environ.get("CONTAINER", "").strip().lower() in {
        "docker",
        "oci",
        "podman",
    }


def has_agent_mount(config: ConsoleConfig) -> bool:
    # A token mount is kept even while the host Agent is restarting. Treat it
    # as authoritative so an Agent outage never silently falls back to
    # ephemeral package changes inside the running container.
    return config.mimo_console_agent_socket.is_socket() or (
        config.mimo_console_agent_token_file.is_file()
        and config.mimo_console_agent_socket.parent.is_dir()
    )


@dataclass(frozen=True)
class DeploymentDetection:
    mode: DetectedMode
    backend_mode: Literal["local", "docker-agent"]
    requested_mode: Literal["auto", "local", "docker-agent"]
    containerized: bool
    auto_detected: bool
    reason: str

    def as_dict(self) -> dict[str, str | bool]:
        return {
            "mode": self.mode,
            "backend_mode": self.backend_mode,
            "requested_mode": self.requested_mode,
            "containerized": self.containerized,
            "auto_detected": self.auto_detected,
            "reason": self.reason,
        }


def detect_deployment(config: ConsoleConfig) -> DeploymentDetection:
    requested = config.mimo_console_deployment_mode
    containerized = is_containerized()
    if requested == "docker-agent":
        return DeploymentDetection(
            mode="docker-agent",
            backend_mode="docker-agent",
            requested_mode=requested,
            containerized=containerized,
            auto_detected=False,
            reason="configured",
        )
    if requested == "local":
        return DeploymentDetection(
            mode="docker-local" if containerized else "python",
            backend_mode="local",
            requested_mode=requested,
            containerized=containerized,
            auto_detected=False,
            reason="configured",
        )
    if has_agent_mount(config):
        return DeploymentDetection(
            mode="docker-agent",
            backend_mode="docker-agent",
            requested_mode=requested,
            containerized=containerized,
            auto_detected=True,
            reason="agent-mount",
        )
    return DeploymentDetection(
        mode="docker-local" if containerized else "python",
        backend_mode="local",
        requested_mode=requested,
        containerized=containerized,
        auto_detected=True,
        reason="container" if containerized else "python-runtime",
    )
