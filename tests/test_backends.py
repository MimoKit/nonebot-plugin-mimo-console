from __future__ import annotations

import asyncio
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import tomlkit

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PATH = ROOT / "src" / "nonebot_plugin_mimo_console"
BACKENDS_PATH = PACKAGE_PATH / "backends"

package = sys.modules.setdefault(
    "nonebot_plugin_mimo_console",
    types.ModuleType("nonebot_plugin_mimo_console"),
)
package.__path__ = [str(PACKAGE_PATH)]  # type: ignore[attr-defined]
backends_package = sys.modules.setdefault(
    "nonebot_plugin_mimo_console.backends",
    types.ModuleType("nonebot_plugin_mimo_console.backends"),
)
backends_package.__path__ = [str(BACKENDS_PATH)]  # type: ignore[attr-defined]


def load(name: str):
    path = BACKENDS_PATH / f"{name}.py"
    spec = importlib.util.spec_from_file_location(
        f"nonebot_plugin_mimo_console.backends.{name}",
        path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


load("base")
configuration = load("configuration")
docker_agent = load("docker_agent")
local = load("local")


class DockerAgentPayloadTests(unittest.TestCase):
    def valid_payload(self) -> dict[str, object]:
        return {
            "operation_id": "op_" + "a" * 32,
            "action": "install",
            "module_name": "nonebot_plugin_demo",
            "project_name": "nonebot-plugin-demo",
            "status": "queued",
        }

    def test_parses_valid_payload(self) -> None:
        operation = docker_agent._operation_from_payload(self.valid_payload())
        self.assertEqual(operation.status, "queued")
        self.assertEqual(operation.action, "install")

    def test_rejects_unknown_status(self) -> None:
        payload = self.valid_payload()
        payload["status"] = "shelling_out"
        with self.assertRaises(docker_agent.DockerAgentError):
            docker_agent._operation_from_payload(payload)

    def test_rejects_unknown_action(self) -> None:
        payload = self.valid_payload()
        payload["action"] = "execute"
        with self.assertRaises(docker_agent.DockerAgentError):
            docker_agent._operation_from_payload(payload)

    def test_parses_configuration_payload(self) -> None:
        snapshot = docker_agent._configuration_from_payload(
            {
                "path": ".env.prod",
                "items": [
                    {"key": "PORT", "value": "8080", "secret": False},
                    {"key": "API_TOKEN", "value": "••••••••", "secret": True},
                ],
            }
        )
        self.assertEqual(snapshot.path, ".env.prod")
        self.assertTrue(snapshot.items[1].secret)

    def test_rejects_invalid_configuration_payload(self) -> None:
        with self.assertRaises(docker_agent.DockerAgentError):
            docker_agent._configuration_from_payload(
                {"path": ".env.prod", "items": [{"key": "PORT"}]}
            )

    def test_self_update_forwards_trusted_repository(self) -> None:
        backend = docker_agent.DockerAgentBackend(
            Path("/run/mimo-agent.sock"),
            Path("/run/mimo-agent.token"),
            "personal",
        )
        backend._request = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "operation_id": "op_" + "a" * 32,
                "action": "update",
                "module_name": "nonebot_plugin_mimo_console",
                "project_name": "nonebot-plugin-mimo-console",
                "status": "queued",
            }
        )
        request = docker_agent.PackageRequest(
            action="update",
            module_name="nonebot_plugin_mimo_console",
            project_name="nonebot-plugin-mimo-console",
            project_root=Path("."),
            repository_url="https://github.com/MimoKit/nonebot-plugin-mimo-console.git",
        )

        operation = asyncio.run(backend.manage(request, 30))

        self.assertEqual(operation.status, "queued")
        payload = backend._request.await_args.args[2]  # type: ignore[union-attr]
        self.assertEqual(payload["repository_url"], request.repository_url)


class LocalConfigurationBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_and_updates_project_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / ".env.prod"
            path.write_text("PORT=8080\nAPI_TOKEN=secret\n", encoding="utf-8")
            backend = configuration.LocalConfigurationBackend(
                root,
                root / "backups",
            )
            snapshot = await backend.read_configuration("prod")
            result = await backend.update_configuration(
                "prod",
                {"PORT": "9000", "API_TOKEN": "••••••••"},
            )
            persisted = path.read_text(encoding="utf-8")
        self.assertEqual(snapshot.path, str(path))
        self.assertTrue(snapshot.items[1].secret)
        self.assertTrue(result.backup_created)
        self.assertIn("PORT=9000", persisted)
        self.assertIn("API_TOKEN=secret", persisted)


class LocalDependencyCommandTests(unittest.TestCase):
    def test_uses_uv_for_project_dependency_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
            interpreter = root / ".venv" / "bin" / "python"
            with (
                patch.object(local.shutil, "which", return_value="/usr/bin/uv"),
                patch.object(local.sys, "executable", str(interpreter)),
            ):
                install = local.build_dependency_command(root, "install", "httpx")
                update = local.build_dependency_command(root, "update", "httpx")
                uninstall = local.build_dependency_command(root, "uninstall", "httpx")
        environment = ["--python", str(interpreter)]
        self.assertEqual(install, ["/usr/bin/uv", "add", *environment, "httpx"])
        self.assertEqual(
            update,
            [
                "/usr/bin/uv",
                "add",
                *environment,
                "httpx",
                "--upgrade-package",
                "httpx",
            ],
        )
        self.assertEqual(uninstall, ["/usr/bin/uv", "remove", *environment, "httpx"])

    def test_uses_current_python_when_uv_project_environment_differs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
            with (
                patch.object(local.shutil, "which", return_value="/usr/bin/uv"),
                patch.dict(local.os.environ, {}, clear=True),
                patch.object(local.sys, "executable", "/opt/nonebot/bin/python"),
            ):
                command = local.build_dependency_command(root, "install", "httpx")
        self.assertEqual(
            command,
            ["/opt/nonebot/bin/python", "-m", "pip", "install", "httpx"],
        )

    def test_rejects_requirement_expressions(self) -> None:
        with self.assertRaises(ValueError):
            local.build_dependency_command(Path.cwd(), "install", "httpx>=0.28")

    def test_builds_github_install_command_for_uv_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
            interpreter = root / ".venv" / "bin" / "python"
            with (
                patch.object(local.shutil, "which", return_value="/usr/bin/uv"),
                patch.object(local.sys, "executable", str(interpreter)),
            ):
                command = local.build_github_command(
                    root,
                    "install",
                    "nonebot-plugin-demo",
                    "https://github.com/example/demo.git",
                )
        self.assertEqual(
            command,
            [
                "/usr/bin/uv",
                "add",
                "--python",
                str(interpreter),
                "git+https://github.com/example/demo.git",
            ],
        )

    def test_builds_github_update_and_uninstall_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
            interpreter = root / ".venv" / "bin" / "python"
            with (
                patch.object(local.shutil, "which", return_value="/usr/bin/uv"),
                patch.object(local.sys, "executable", str(interpreter)),
            ):
                update = local.build_github_command(
                    root,
                    "update",
                    "nonebot-plugin-demo",
                    "https://github.com/example/demo.git",
                )
                uninstall = local.build_github_command(
                    root,
                    "uninstall",
                    "nonebot-plugin-demo",
                    "https://github.com/example/demo.git",
                )
        self.assertEqual(
            update,
            [
                "/usr/bin/uv",
                "add",
                "--python",
                str(interpreter),
                "git+https://github.com/example/demo.git",
                "--upgrade-package",
                "nonebot-plugin-demo",
            ],
        )
        self.assertEqual(
            uninstall,
            [
                "/usr/bin/uv",
                "remove",
                "--python",
                str(interpreter),
                "nonebot-plugin-demo",
            ],
        )

    def test_registers_github_plugin_in_list_project_format(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "pyproject.toml"
            path.write_text(
                '[tool.nonebot]\nplugins = ["existing_plugin"]\n',
                encoding="utf-8",
            )
            local._register_plugin(root, "nonebot-plugin-demo", "nonebot_plugin_demo")
            content = path.read_text(encoding="utf-8")
        self.assertIn('"existing_plugin"', content)
        self.assertIn('"nonebot_plugin_demo"', content)

    def test_registers_github_plugin_in_mapping_project_format(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "pyproject.toml"
            path.write_text(
                '[tool.nonebot.plugins]\nexisting = ["existing_plugin"]\n',
                encoding="utf-8",
            )
            local._register_plugin(root, "nonebot-plugin-demo", "nonebot_plugin_demo")
            content = path.read_text(encoding="utf-8")
        self.assertIn("nonebot-plugin-demo", content)
        self.assertIn("nonebot_plugin_demo", content)

    def test_registering_mapping_plugin_preserves_existing_modules(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "pyproject.toml"
            path.write_text(
                "[tool.nonebot.plugins]\n"
                '"nonebot-plugin-demo" = ["first_module", "second_module"]\n',
                encoding="utf-8",
            )
            local._register_plugin(
                root,
                "nonebot-plugin-demo",
                "nonebot_plugin_demo",
            )
            content = path.read_text(encoding="utf-8")
        self.assertIn("first_module", content)
        self.assertIn("second_module", content)
        self.assertIn("nonebot_plugin_demo", content)

    def test_source_record_round_trip_matches_agent_format(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "pyproject.toml"
            path.write_text('[project]\nname = "bot"\n', encoding="utf-8")
            local._set_source_record(
                root,
                "nonebot_plugin_demo",
                "nonebot-plugin-demo",
                "https://github.com/example/demo.git",
            )
            document = tomlkit.parse(path.read_text(encoding="utf-8")).unwrap()
            record = document["tool"]["mimo_console"]["source_plugins"]["nonebot_plugin_demo"]
            self.assertEqual(record["project"], "nonebot-plugin-demo")
            self.assertEqual(record["repository"], "https://github.com/example/demo.git")
            # Removing the record leaves the manifest without the module entry so
            # the detail page stops offering the source-plugin lifecycle.
            local._remove_source_record(root, "nonebot_plugin_demo")
            document = tomlkit.parse(path.read_text(encoding="utf-8")).unwrap()
            records = document.get("tool", {}).get("mimo_console", {}).get("source_plugins", {})
            self.assertNotIn("nonebot_plugin_demo", records)

    def test_unregisters_source_plugin_from_mapping_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "pyproject.toml"
            path.write_text(
                '[tool.nonebot.plugins]\n"nonebot-plugin-demo" = ["nonebot_plugin_demo"]\n',
                encoding="utf-8",
            )
            local._unregister_plugin(
                root,
                "nonebot-plugin-demo",
                "nonebot_plugin_demo",
            )
            document = tomlkit.parse(path.read_text(encoding="utf-8")).unwrap()
        self.assertNotIn(
            "nonebot-plugin-demo",
            document["tool"]["nonebot"]["plugins"],
        )


if __name__ == "__main__":
    unittest.main()
