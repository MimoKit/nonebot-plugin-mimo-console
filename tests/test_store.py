from __future__ import annotations

import asyncio
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PATH = ROOT / "src" / "nonebot_plugin_mimo_console"
MODULE_PATH = PACKAGE_PATH / "store.py"
package = types.ModuleType("nonebot_plugin_mimo_console")
package.__path__ = [str(PACKAGE_PATH)]  # type: ignore[attr-defined]
sys.modules.setdefault("nonebot_plugin_mimo_console", package)
spec = importlib.util.spec_from_file_location("nonebot_plugin_mimo_console.store", MODULE_PATH)
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

    def test_build_self_update_command_prefers_uv_git_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "pyproject.toml").write_text("[project]\nname='bot'\n", encoding="utf-8")
            command = store.build_self_update_command(
                root,
                "nonebot-plugin-mimo-console",
                "https://github.com/MimoKit/nonebot-plugin-mimo-console.git",
                uv_executable="/usr/bin/uv",
            )
        self.assertEqual(command[0], "/usr/bin/uv")
        self.assertEqual(
            command[1:3],
            [
                "add",
                "nonebot-plugin-mimo-console @ git+https://github.com/MimoKit/nonebot-plugin-mimo-console.git",
            ],
        )
        self.assertEqual(command[-2:], ["--upgrade-package", "nonebot-plugin-mimo-console"])

    def test_build_self_update_command_falls_back_to_pip(self) -> None:
        command = store.build_self_update_command(
            Path("bot-project"),
            "nonebot-plugin-mimo-console",
            "https://github.com/MimoKit/nonebot-plugin-mimo-console.git",
            uv_executable="",
        )
        self.assertEqual(command[:4], [sys.executable, "-m", "pip", "install"])
        self.assertEqual(
            command[-1], "git+https://github.com/MimoKit/nonebot-plugin-mimo-console.git"
        )

    def test_build_self_update_command_uses_pip_for_prefix_proxy(self) -> None:
        # gh-proxy 风格前缀会把代理前缀拼在 GitHub URL 前，形成含多个 "://" 的套娃
        # URL（如 https://gh-proxy.com/https://github.com/...）。即便项目用 uv 管理，
        # 此种 URL 也必须走 pip——uv 解析 "git+<套娃URL>@<ref>" 会让解析器 panic。
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "pyproject.toml").write_text("[project]\nname='bot'\n", encoding="utf-8")
            command = store.build_self_update_command(
                root,
                "nonebot-plugin-mimo-console",
                "https://gh-proxy.com/https://github.com/MimoKit/nonebot-plugin-mimo-console.git",
                uv_executable="/usr/bin/uv",
            )
        self.assertNotEqual(command[0], "/usr/bin/uv")
        self.assertEqual(command[:4], [sys.executable, "-m", "pip", "install"])
        self.assertEqual(
            command[-1],
            "git+https://gh-proxy.com/https://github.com/MimoKit/nonebot-plugin-mimo-console.git",
        )

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

    def test_parses_github_repository_and_derives_names(self) -> None:
        url, module_name, project_name = store.parse_github_repository(
            "https://github.com/MimoKit/nonebot-plugin-parser"
        )
        self.assertEqual(
            url,
            "https://github.com/MimoKit/nonebot-plugin-parser.git",
        )
        self.assertEqual(module_name, "nonebot_plugin_parser")
        self.assertEqual(project_name, "nonebot-plugin-parser")

    def test_github_repository_allows_explicit_names(self) -> None:
        _, module_name, project_name = store.parse_github_repository(
            "https://github.com/example/custom.git",
            "nonebot_plugin_custom",
            "nonebot-plugin-custom",
        )
        self.assertEqual(module_name, "nonebot_plugin_custom")
        self.assertEqual(project_name, "nonebot-plugin-custom")

    def test_rejects_unsafe_github_repository_urls(self) -> None:
        values = (
            "http://github.com/MimoKit/plugin",
            "https://user:secret@github.com/MimoKit/plugin",
            "https://github.com/MimoKit/plugin/tree/main",
            "https://github.com/MimoKit/plugin?ref=main",
            "https://example.com/MimoKit/plugin",
        )
        for value in values:
            with self.subTest(value=value), self.assertRaises(store.StoreError):
                store.parse_github_repository(value)

    def test_serialized_store_plugin_contains_icon(self) -> None:
        raw = {
            "module_name": "nonebot_plugin_status",
            "project_link": "nonebot-plugin-status",
            "homepage": "https://github.com/nonebot/plugin-status",
            "valid": True,
        }
        item = store.PluginStore()._serialize_plugin(raw)
        self.assertEqual(item["icon"], "https://github.com/nonebot.png?size=96")

    def test_direct_source_action_preserves_repository_url(self) -> None:
        class Backend:
            request: Any = None

            async def manage(self, request, timeout):
                del timeout
                self.request = request
                return types.SimpleNamespace(
                    status="succeeded",
                    error="",
                    output="",
                    project_name=request.project_name,
                    as_dict=lambda: {"status": "succeeded"},
                )

        backend = Backend()
        plugin_store = store.PluginStore(backend=backend)
        result = asyncio.run(
            plugin_store.manage_direct_plugin(
                Path("."),
                "nonebot_plugin_demo",
                "nonebot-plugin-demo",
                "https://github.com/example/demo",
                "update",
                30,
            )
        )
        self.assertEqual(result["status"], "succeeded")
        request = backend.request
        assert request is not None
        self.assertEqual(
            request.repository_url,
            "https://github.com/example/demo.git",
        )

    def test_self_update_bypasses_registry_and_preserves_trusted_repository(self) -> None:
        class Backend:
            request: Any = None

            async def manage(self, request, timeout):
                del timeout
                self.request = request
                return types.SimpleNamespace(
                    status="queued",
                    error="",
                    output="",
                    project_name=request.project_name,
                    as_dict=lambda: {"status": "queued"},
                )

        backend = Backend()
        plugin_store = store.PluginStore(backend=backend)
        result = asyncio.run(
            plugin_store.update_self(
                Path("."),
                "https://github.com/MimoKit/nonebot-plugin-mimo-console.git",
                30,
            )
        )
        self.assertEqual(result["status"], "queued")
        request = backend.request
        assert request is not None
        self.assertEqual(request.action, "update")
        self.assertEqual(request.module_name, "nonebot_plugin_mimo_console")
        self.assertEqual(request.project_name, "nonebot-plugin-mimo-console")
        self.assertEqual(
            request.repository_url,
            "https://github.com/MimoKit/nonebot-plugin-mimo-console.git",
        )


if __name__ == "__main__":
    unittest.main()
