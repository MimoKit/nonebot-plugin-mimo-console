from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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


if __name__ == "__main__":
    unittest.main()
