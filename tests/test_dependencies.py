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


load("store")
dependencies = load("dependencies")


class FakeDistribution:
    def __init__(self, name: str, version: str) -> None:
        self.metadata = {"Name": name}
        self.version = version


class DependencySnapshotTests(unittest.TestCase):
    def test_classifies_direct_transitive_plugin_and_core_packages(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "pyproject.toml").write_text(
                """
[project]
name = "demo"
dependencies = [
  "nonebot2>=2.4",
  "httpx>=0.28",
  "nonebot-plugin-demo>=1",
]
""".strip(),
                encoding="utf-8",
            )
            distributions = [
                FakeDistribution("nonebot2", "2.4.4"),
                FakeDistribution("httpx", "0.28.1"),
                FakeDistribution("nonebot-plugin-demo", "1.2.0"),
                FakeDistribution("anyio", "4.10.0"),
            ]
            with patch.object(
                dependencies.importlib.metadata,
                "distributions",
                return_value=distributions,
            ):
                snapshot = dependencies.dependency_snapshot(
                    root,
                    {"nonebot-plugin-demo"},
                )

        items = {item["normalized_name"]: item for item in snapshot["items"]}
        self.assertEqual(snapshot["direct"], 3)
        self.assertTrue(items["httpx"]["manageable"])
        self.assertEqual(items["nonebot-plugin-demo"]["kind"], "plugin")
        self.assertFalse(items["nonebot-plugin-demo"]["manageable"])
        self.assertEqual(items["nonebot2"]["kind"], "core")
        self.assertFalse(items["nonebot2"]["manageable"])
        self.assertFalse(items["anyio"]["direct"])
        self.assertFalse(items["anyio"]["manageable"])

    def test_unloaded_source_plugin_distribution_stays_protected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "pyproject.toml").write_text(
                """
[project]
name = "demo"
dependencies = [
  "nonebot-plugin-broken @ git+https://github.com/example/broken.git",
  "httpx>=0.28",
]

[tool.mimo_console.source_plugins.nonebot_plugin_broken]
project = "nonebot-plugin-broken"
repository = "https://github.com/example/broken.git"
""".strip(),
                encoding="utf-8",
            )
            distributions = [
                FakeDistribution("nonebot-plugin-broken", "0.1.0"),
                FakeDistribution("httpx", "0.28.1"),
            ]
            with patch.object(
                dependencies.importlib.metadata,
                "distributions",
                return_value=distributions,
            ):
                snapshot = dependencies.dependency_snapshot(
                    root,
                    set(),
                )

        items = {item["normalized_name"]: item for item in snapshot["items"]}
        self.assertEqual(items["nonebot-plugin-broken"]["kind"], "plugin")
        self.assertFalse(items["nonebot-plugin-broken"]["manageable"])
        self.assertTrue(items["httpx"]["manageable"])

    def test_uses_python_310_compatible_toml_parser(self) -> None:
        source = (PACKAGE_PATH / "dependencies.py").read_text(encoding="utf-8")
        self.assertNotIn("import tomllib", source)
        self.assertIn("tomlkit.parse", source)


if __name__ == "__main__":
    unittest.main()
