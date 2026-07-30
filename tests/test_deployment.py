from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PATH = ROOT / "src" / "nonebot_plugin_mimo_console"

package = sys.modules.setdefault(
    "nonebot_plugin_mimo_console",
    types.ModuleType("nonebot_plugin_mimo_console"),
)
package.__path__ = [str(PACKAGE_PATH)]  # type: ignore[attr-defined]


def load(name: str):
    path = PACKAGE_PATH / f"{name}.py"
    spec = importlib.util.spec_from_file_location(
        f"nonebot_plugin_mimo_console.{name}",
        path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


load("version")
config_module = load("config")
deployment = load("deployment")


class DeploymentDetectionTests(unittest.TestCase):
    def config(self, root: Path, mode: str = "auto"):
        return config_module.ConsoleConfig(
            mimo_console_deployment_mode=mode,
            mimo_console_agent_socket=root / "run" / "agent.sock",
            mimo_console_agent_token_file=root / "secrets" / "agent.token",
        )

    def test_detects_plain_python_installation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self.config(Path(temp))
            with patch.object(deployment, "is_containerized", return_value=False):
                result = deployment.detect_deployment(config)
        self.assertEqual(result.mode, "python")
        self.assertEqual(result.backend_mode, "local")
        self.assertTrue(result.auto_detected)

    def test_detects_docker_without_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self.config(Path(temp))
            with patch.object(deployment, "is_containerized", return_value=True):
                result = deployment.detect_deployment(config)
        self.assertEqual(result.mode, "docker-local")
        self.assertEqual(result.backend_mode, "local")

    def test_agent_token_mount_takes_priority_even_while_socket_is_offline(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "run").mkdir()
            (root / "secrets").mkdir()
            (root / "secrets" / "agent.token").write_text("token", encoding="utf-8")
            config = self.config(root)
            with patch.object(deployment, "is_containerized", return_value=True):
                result = deployment.detect_deployment(config)
        self.assertEqual(result.mode, "docker-agent")
        self.assertEqual(result.backend_mode, "docker-agent")
        self.assertEqual(result.reason, "agent-mount")

    def test_explicit_local_mode_remains_available_as_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self.config(Path(temp), mode="local")
            with patch.object(deployment, "is_containerized", return_value=True):
                result = deployment.detect_deployment(config)
        self.assertEqual(result.mode, "docker-local")
        self.assertFalse(result.auto_detected)


if __name__ == "__main__":
    unittest.main()
