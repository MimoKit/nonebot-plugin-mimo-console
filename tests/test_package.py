from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_loading_script(source: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


class PackageTests(unittest.TestCase):
    def test_static_assets_are_packaged(self) -> None:
        static = ROOT / "src" / "nonebot_plugin_mimo_console" / "static"
        for relative in ("index.html", "assets/styles.css", "assets/app.js"):
            path = static / relative
            self.assertTrue(path.is_file(), relative)
            self.assertGreater(path.stat().st_size, 100)

    def test_official_plugin_metadata_fields_are_declared(self) -> None:
        source = (ROOT / "src" / "nonebot_plugin_mimo_console" / "__init__.py").read_text(
            encoding="utf-8"
        )
        fields = (
            "name=",
            "description=",
            "usage=",
            "type=",
            "homepage=",
            "config=",
            "supported_adapters=None",
        )
        for field in fields:
            self.assertIn(field, source)

    def test_localstore_is_used_for_runtime_data(self) -> None:
        source = (ROOT / "src" / "nonebot_plugin_mimo_console" / "__init__.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('require("nonebot_plugin_localstore")', source)
        self.assertIn('get_plugin_data_file("auth.json")', source)

    def test_plugin_does_not_import_an_adapter(self) -> None:
        package = ROOT / "src" / "nonebot_plugin_mimo_console"
        source = "\n".join(path.read_text(encoding="utf-8") for path in package.glob("*.py"))
        self.assertNotIn("nonebot.adapters", source)
        self.assertNotIn("nonebot_adapter_", source)

    def test_store_dependencies_are_declared(self) -> None:
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('"nb-cli>=1.4.2"', pyproject)
        self.assertIn('"httpx>=0.27.0', pyproject)

    def test_loads_without_asgi_driver_for_noneflow(self) -> None:
        result = run_loading_script(
            """
            import nonebot

            nonebot.init(driver="~none")
            plugin = nonebot.load_plugin("nonebot_plugin_mimo_console")
            assert plugin is not None
            assert plugin.metadata is not None
            assert plugin.metadata.type == "application"
            assert plugin.metadata.supported_adapters is None
            """
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_mounts_routes_with_fastapi_driver(self) -> None:
        result = run_loading_script(
            """
            import nonebot

            nonebot.init(driver="~fastapi")
            plugin = nonebot.load_plugin("nonebot_plugin_mimo_console")
            assert plugin is not None
            app = nonebot.get_app()
            paths = set(app.openapi()["paths"])
            assert "/mimo-console/api/auth/status" in paths
            assert any(
                getattr(route, "path", "") == "/mimo-console/assets"
                for route in app.routes
            )
            """
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
