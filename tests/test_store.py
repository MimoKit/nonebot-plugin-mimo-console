from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "src" / "nonebot_plugin_mimo_console" / "store.py"
spec = importlib.util.spec_from_file_location("mimo_console_store_test", MODULE_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError("cannot load store module")
store = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = store
spec.loader.exec_module(store)


class StoreTests(unittest.TestCase):
    def test_normalize_project_name(self) -> None:
        self.assertEqual(
            store.normalize_project_name("NoneBot.Plugin_Test"),
            "nonebot-plugin-test",
        )

    def test_build_nb_command_uses_current_environment(self) -> None:
        root = Path("bot-project")
        command = store.build_nb_command(root, "install", "nonebot-plugin-status")
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(command[1:3], ["-m", "nb_cli"])
        self.assertIn("--no-venv", command)
        self.assertEqual(command[-3:], ["plugin", "install", "nonebot-plugin-status"])

    def test_build_nb_command_rejects_extra_arguments(self) -> None:
        for value in ("plugin name", "plugin/../../bad", "--help"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                store.build_nb_command(Path("."), "install", value)

    def test_registry_item_validation(self) -> None:
        valid = {
            "module_name": "nonebot_plugin_status",
            "project_link": "nonebot-plugin-status",
        }
        self.assertTrue(store.PluginStore._valid_item(valid))
        self.assertFalse(
            store.PluginStore._valid_item(
                {**valid, "project_link": "nonebot-plugin-status --upgrade"}
            )
        )

    def test_command_output_is_sanitized(self) -> None:
        raw = b"\x1b[31mfailed https://user:password@example.com/simple\x1b[0m"
        cleaned = store._clean_output(raw)
        self.assertNotIn("password", cleaned)
        self.assertEqual(cleaned, "failed https://***@example.com/simple")

    def test_github_homepage_provides_avatar(self) -> None:
        self.assertEqual(
            store.github_avatar_url("https://github.com/nonebot/plugin-status"),
            "https://github.com/nonebot.png?size=96",
        )
        self.assertEqual(store.github_avatar_url("https://example.com/project"), "")

    def test_serialized_store_plugin_contains_icon(self) -> None:
        raw = {
            "module_name": "nonebot_plugin_status",
            "project_link": "nonebot-plugin-status",
            "homepage": "https://github.com/nonebot/plugin-status",
            "valid": True,
        }
        item = store.PluginStore()._serialize_plugin(raw)
        self.assertEqual(item["icon"], "https://github.com/nonebot.png?size=96")


if __name__ == "__main__":
    unittest.main()
