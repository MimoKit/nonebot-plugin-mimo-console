from __future__ import annotations

import asyncio
import json
import os
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from mimo_console_agent.api import create_app
from mimo_console_agent.config import AgentConfig, ConfigError, InstanceConfig
from mimo_console_agent.environment import (
    MASK,
    EnvironmentError,
    read_environment,
    update_environment,
)
from mimo_console_agent.manager import (
    AgentError,
    DeploymentManager,
    _canonical_github_repository,
    _declared_source_dependencies,
    _find_source_plugin,
    _image_override,
    _inferred_source_dependencies,
    _require_plain_dependency,
    _update_image_override,
    _update_plugin_list,
    _update_source_plugin_dir,
)
from mimo_console_agent.models import Operation
from mimo_console_agent.runner import clean_output
from mimo_console_agent.storage import OperationStore


class AgentConfigTests(unittest.TestCase):
    def make_config(self, root: Path) -> Path:
        project = root / "bot"
        project.mkdir()
        (project / "Dockerfile").write_text("FROM python:3.12-slim\n", encoding="utf-8")
        (project / "compose.yml").write_text("services: {bot: {}}\n", encoding="utf-8")
        token = root / "agent.token"
        token.write_text("secret\n", encoding="utf-8")
        config = root / "agent.json"
        config.write_text(
            json.dumps(
                {
                    "socket_path": str(root / "agent.sock"),
                    "state_dir": str(root / "state"),
                    "uv_image": "ghcr.io/astral-sh/uv:0.9.29-python3.12-bookworm-slim",
                    "instances": {
                        "personal": {
                            "project_root": str(project),
                            "compose_files": ["compose.yml"],
                            "compose_project": "personal-bot",
                            "service": "bot",
                            "image_repository": "local/personal-bot",
                            "health_url": "http://127.0.0.1:18080/mimo-console/api/health",
                            "token_file": str(token),
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        return config

    def test_loads_allowlisted_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = AgentConfig.load(self.make_config(Path(temp)))
        instance = config.instances["personal"]
        self.assertEqual(instance.service, "bot")
        self.assertEqual(instance.image_repository, "local/personal-bot")
        self.assertEqual(instance.environment_file, instance.project_root / ".env.prod")

    @unittest.skipIf(os.name == "nt", "Agent defaults are Linux-only paths")
    def test_default_socket_uses_stable_mount_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config_path = self.make_config(Path(temp))
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            del raw["socket_path"]
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            config = AgentConfig.load(config_path)
        self.assertEqual(config.socket_path, Path("/run/mimo-agent/agent.sock"))

    def test_rejects_override_outside_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = self.make_config(root)
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            raw["instances"]["personal"]["override_file"] = str(root / "outside.yml")
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "超出项目目录"):
                AgentConfig.load(config_path)

    def test_rejects_environment_file_outside_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = self.make_config(root)
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            raw["instances"]["personal"]["environment_file"] = str(root / ".env.prod")
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "环境文件.*超出项目目录"):
                AgentConfig.load(config_path)

    def test_requires_token_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = self.make_config(root)
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            raw["instances"]["personal"]["token_file"] = str(root / "missing")
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "令牌文件不存在"):
                AgentConfig.load(config_path)

    def test_rejects_token_and_agent_state_inside_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = self.make_config(root)
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            project = Path(raw["instances"]["personal"]["project_root"])
            token = project / "agent.token"
            token.write_text("secret", encoding="utf-8")
            raw["instances"]["personal"]["token_file"] = str(token)
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "令牌文件不能位于项目目录内"):
                AgentConfig.load(config_path)

            raw["instances"]["personal"]["token_file"] = str(root / "agent.token")
            raw["state_dir"] = str(project / "agent-state")
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "state_dir 不能位于"):
                AgentConfig.load(config_path)

    def test_rejects_resource_collisions_between_instances(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = self.make_config(root)
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            official = root / "official"
            official.mkdir()
            (official / "Dockerfile").write_text("FROM python:3.12-slim\n")
            (official / "compose.yml").write_text("services: {bot: {}}\n")
            official_token = root / "official.token"
            official_token.write_text("official")
            raw["instances"]["official"] = {
                "project_root": str(official),
                "compose_files": ["compose.yml"],
                "compose_project": "official-bot",
                "service": "bot",
                "image_repository": "local/personal-bot",
                "health_url": "http://127.0.0.1:18081/mimo-console/api/health",
                "token_file": str(official_token),
            }
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "共用了镜像仓库"):
                AgentConfig.load(config_path)


class ManifestTests(unittest.TestCase):
    def test_adds_and_removes_nonebot_plugin(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pyproject = Path(temp) / "pyproject.toml"
            pyproject.write_text(
                '[project]\nname = "bot"\n\n[tool.nonebot]\nplugins = []\n',
                encoding="utf-8",
            )
            _update_plugin_list(
                pyproject,
                "nonebot_plugin_demo",
                "nonebot-plugin-demo",
                "install",
            )
            installed = pyproject.read_text(encoding="utf-8")
            self.assertIn('"nonebot_plugin_demo"', installed)
            _update_plugin_list(
                pyproject,
                "nonebot_plugin_demo",
                "nonebot-plugin-demo",
                "uninstall",
            )
            self.assertNotIn('"nonebot_plugin_demo"', pyproject.read_text(encoding="utf-8"))

    def test_updates_current_nonebot_project_format(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pyproject = Path(temp) / "pyproject.toml"
            pyproject.write_text(
                "[project]\n"
                'name = "bot"\n\n'
                "[tool.nonebot]\n"
                "plugin_dirs = []\n"
                "builtin_plugins = []\n\n"
                "[tool.nonebot.plugins]\n"
                '"@local" = []\n',
                encoding="utf-8",
            )
            _update_plugin_list(
                pyproject,
                "nonebot_plugin_demo",
                "nonebot-plugin-demo",
                "install",
            )
            installed = pyproject.read_text(encoding="utf-8")
            self.assertIn("nonebot-plugin-demo", installed)
            self.assertIn('"nonebot_plugin_demo"', installed)
            _update_plugin_list(
                pyproject,
                "nonebot_plugin_demo",
                "nonebot-plugin-demo",
                "uninstall",
            )
            removed = pyproject.read_text(encoding="utf-8")
            self.assertNotIn("nonebot-plugin-demo", removed)
            self.assertIn('"@local"', removed)

    def test_override_disables_build_and_pins_image(self) -> None:
        rendered = _image_override("bot", "local/bot:mimo-123")
        self.assertIn('image: "local/bot:mimo-123"', rendered)
        self.assertIn("build: !reset null", rendered)

    def test_override_update_preserves_administrator_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            override = Path(temp) / "override.yml"
            override.write_text(
                "services:\n"
                "  bot:\n"
                '    image: "local/bot:old"\n'
                "    build:\n"
                "      context: .\n"
                "    environment:\n"
                "      KEEP: yes\n"
                "    volumes:\n"
                "      - ./data:/data\n",
                encoding="utf-8",
            )
            override.chmod(0o600)
            original_mode = stat.S_IMODE(override.stat().st_mode)
            _update_image_override(override, "bot", "local/bot:new")
            rendered = override.read_text(encoding="utf-8")
            updated_mode = stat.S_IMODE(override.stat().st_mode)
        self.assertIn('image: "local/bot:new"', rendered)
        self.assertIn("build: !reset null", rendered)
        self.assertIn("KEEP: yes", rendered)
        self.assertIn("./data:/data", rendered)
        self.assertEqual(updated_mode, original_mode)

    def test_configures_source_plugin_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pyproject = Path(temp) / "pyproject.toml"
            pyproject.write_text(
                '[project]\nname = "bot"\n\n[tool.nonebot]\nplugin_dirs = []\n',
                encoding="utf-8",
            )
            _update_source_plugin_dir(pyproject)
            _update_source_plugin_dir(pyproject)
            document = pyproject.read_text(encoding="utf-8")
        self.assertEqual(document.count('"local_plugins"'), 1)

    def test_detects_source_plugin_and_infers_common_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repository = Path(temp)
            module = repository / "nonebot_plugin_demo"
            module.mkdir()
            (module / "__init__.py").write_text(
                "from PIL import Image\n"
                "import pycurl\n"
                "from nonebot import on_command\n"
                "from nonebot_plugin_apscheduler import scheduler\n",
                encoding="utf-8",
            )
            path, name = _find_source_plugin(repository, "wrong_derived_name")
            dependencies = _inferred_source_dependencies(path)
        self.assertEqual(name, "nonebot_plugin_demo")
        self.assertIn("pillow", dependencies)
        self.assertIn("pycurl", dependencies)
        self.assertIn("nonebot-plugin-apscheduler", dependencies)
        self.assertFalse(any(item.startswith("nonebot2") for item in dependencies))

    def test_requirements_inline_comments_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repository = Path(temp)
            module = repository / "nonebot_plugin_demo"
            module.mkdir()
            (module / "__init__.py").write_text("", encoding="utf-8")
            (repository / "requirements.txt").write_text(
                "httpx>=0.27  # HTTP client\n# comment\n",
                encoding="utf-8",
            )
            dependencies = _declared_source_dependencies(repository, module)
        self.assertEqual(dependencies, ["httpx>=0.27"])

    def test_redacts_url_credentials(self) -> None:
        cleaned = clean_output(b"fetch https://user:secret@example.test/simple")
        self.assertEqual(cleaned, "fetch https://***@example.test/simple")

    def test_plain_dependency_rejects_direct_and_local_references(self) -> None:
        for value in (
            "evilpkg @ ../local",
            "evilpkg @ ./.mimo-source/payload",
            "evilpkg @ git+https://github.com/example/evil.git",
            "evilpkg @ https://example.com/evil.whl",
        ):
            with self.subTest(value=value), self.assertRaises(AgentError):
                _require_plain_dependency(value, "pyproject.toml")
        self.assertEqual(
            _require_plain_dependency(
                'httpx>=0.27; python_version >= "3.10"',
                "pyproject.toml",
            ),
            'httpx>=0.27; python_version >= "3.10"',
        )

    def test_canonicalizes_github_repository(self) -> None:
        self.assertEqual(
            _canonical_github_repository("https://github.com/MimoKit/nonebot-plugin-parser"),
            "https://github.com/MimoKit/nonebot-plugin-parser.git",
        )
        with self.assertRaises(AgentError):
            _canonical_github_repository(
                "https://github.com/MimoKit/nonebot-plugin-parser/tree/main"
            )


class EnvironmentTests(unittest.TestCase):
    def test_masks_secrets_and_preserves_masked_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / ".env.prod"
            path.write_text(
                "# managed\nPORT=8080\nAPI_TOKEN=secret\n",
                encoding="utf-8",
            )
            entries = {entry.key: entry for entry in read_environment(path)}
            self.assertEqual(entries["API_TOKEN"].value, MASK)
            self.assertTrue(entries["API_TOKEN"].secret)
            backup = update_environment(
                path,
                {"PORT": "9000", "API_TOKEN": MASK, "NEW_VALUE": "yes"},
                root / "backups",
                2,
            )
            result = path.read_text(encoding="utf-8")
            self.assertIn("# managed", result)
            self.assertIn("PORT=9000", result)
            self.assertIn("API_TOKEN=secret", result)
            self.assertIn("NEW_VALUE=yes", result)
            self.assertTrue((root / "backups" / backup).is_file())
            update_environment(path, {"PORT": "9001"}, root / "backups", 2)
            update_environment(path, {"PORT": "9002"}, root / "backups", 2)
            self.assertEqual(len(list((root / "backups").glob("*.bak"))), 2)

    def test_rejects_invalid_or_oversized_updates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / ".env.prod"
            with self.assertRaises(EnvironmentError):
                update_environment(path, {"bad-key": "value"}, root / "backups", 2)
            with self.assertRaises(EnvironmentError):
                update_environment(
                    path,
                    {"VALID": "first\nsecond"},
                    root / "backups",
                    2,
                )
            with self.assertRaises(EnvironmentError):
                update_environment(
                    path,
                    {"VALID": "x" * 20_000},
                    root / "backups",
                    2,
                )


class OperationStoreTests(unittest.TestCase):
    def test_persists_and_recovers_active_operation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = OperationStore(Path(temp) / "operations.sqlite3")
            operation = Operation.create(
                "personal",
                "install",
                "nonebot_plugin_demo",
                "nonebot-plugin-demo",
            )
            store.save(operation)
            loaded = OperationStore(store.path).active("personal")
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.operation_id, operation.operation_id)
            self.assertEqual(loaded.status, "queued")

    def test_tracks_one_deployment_head_per_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = OperationStore(Path(temp) / "operations.sqlite3")
            first = Operation.create(
                "personal",
                "install",
                "nonebot_plugin_first",
                "nonebot-plugin-first",
            )
            second = Operation.create(
                "personal",
                "update",
                "nonebot_plugin_second",
                "nonebot-plugin-second",
            )
            store.set_deployment_head("personal", first.operation_id)
            store.set_deployment_head("personal", second.operation_id)
            self.assertEqual(
                store.deployment_head("personal"),
                second.operation_id,
            )
            store.clear_deployment_head("personal", first.operation_id)
            self.assertEqual(
                store.deployment_head("personal"),
                second.operation_id,
            )
            store.clear_deployment_head("personal", second.operation_id)
            self.assertEqual(store.deployment_head("personal"), "")


class VerificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_uninstall_verification_requires_module_absent(self) -> None:
        instance = InstanceConfig(
            instance_id="personal",
            token_file=Path("/tmp/token"),
            project_root=Path("/tmp/project"),
            compose_files=(Path("/tmp/project/compose.yml"),),
            compose_project="personal",
            service="bot",
            dockerfile=Path("/tmp/project/Dockerfile"),
            build_context=Path("/tmp/project"),
            image_repository="local/bot",
            override_file=Path("/tmp/project/.mimo/override.yml"),
            environment_file=Path("/tmp/project/.env.prod"),
            health_url="http://127.0.0.1:8080/mimo-console/api/health",
        )
        config = AgentConfig(
            socket_path=Path("/tmp/agent.sock"),
            socket_mode=0o660,
            socket_gid=None,
            state_dir=Path("/tmp/state"),
            docker_bin="docker",
            uv_image="ghcr.io/astral-sh/uv:0.9.29-python3.12-bookworm-slim",
            instances={"personal": instance},
        )
        operation = Operation.create(
            "personal",
            "uninstall",
            "nonebot_plugin_demo",
            "nonebot-plugin-demo",
        )
        operation.new_image = "local/bot:mimo-test"
        with tempfile.TemporaryDirectory() as temp:
            manager = DeploymentManager(
                config,
                OperationStore(Path(temp) / "operations.sqlite3"),
            )
            command = AsyncMock(return_value="")
            with patch("mimo_console_agent.manager.run_command", command):
                await manager._verify(operation, instance)
        self.assertIsNotNone(command.await_args)
        assert command.await_args is not None
        arguments = command.await_args.args[0]
        self.assertIn("/bin/sh", arguments)
        self.assertTrue(any("/app/.venv/bin/python" in item for item in arguments))
        script = arguments[-1]
        self.assertIn("assert found is False", script)


class DeploymentTransactionTests(unittest.IsolatedAsyncioTestCase):
    def make_runtime(self, root: Path):
        project = root / "bot"
        project.mkdir()
        (project / "pyproject.toml").write_text(
            '[project]\nname = "bot"\n',
            encoding="utf-8",
        )
        (project / "uv.lock").write_text("version = 1\n", encoding="utf-8")
        (project / "Dockerfile").write_text("FROM python:3.12-slim\n", encoding="utf-8")
        (project / "compose.yml").write_text("services: {bot: {}}\n", encoding="utf-8")
        (project / ".env.prod").write_text(
            "PORT=8080\nAPI_TOKEN=secret\n",
            encoding="utf-8",
        )
        token = root / "token"
        token.write_text("secret", encoding="utf-8")
        instance = InstanceConfig(
            instance_id="personal",
            token_file=token,
            project_root=project,
            compose_files=(project / "compose.yml",),
            compose_project="personal",
            service="bot",
            dockerfile=project / "Dockerfile",
            build_context=project,
            image_repository="local/bot",
            override_file=project / ".mimo/override.yml",
            environment_file=project / ".env.prod",
            health_url="http://127.0.0.1:8080/mimo-console/api/health",
        )
        config = AgentConfig(
            socket_path=root / "agent.sock",
            socket_mode=0o660,
            socket_gid=None,
            state_dir=root / "state",
            docker_bin="docker",
            uv_image="ghcr.io/astral-sh/uv:0.9.29-python3.12-bookworm-slim",
            instances={"personal": instance},
        )
        store = OperationStore(config.state_dir / "operations.sqlite3")
        return instance, DeploymentManager(config, store), store

    async def test_success_commits_and_marks_operation_succeeded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance, manager, store = self.make_runtime(root)
            operation = store.save(
                Operation.create(
                    "personal",
                    "install",
                    "nonebot_plugin_demo",
                    "nonebot-plugin-demo",
                )
            )
            manager._current_image = AsyncMock(return_value="local/bot:old")  # type: ignore[method-assign]
            manager._preflight = AsyncMock(return_value=None)  # type: ignore[method-assign]
            manager._change_dependencies = AsyncMock(return_value="locked")  # type: ignore[method-assign]
            manager._build = AsyncMock(return_value="built")  # type: ignore[method-assign]
            manager._verify = AsyncMock(return_value="verified")  # type: ignore[method-assign]
            manager._compose_up = AsyncMock(return_value="deployed")  # type: ignore[method-assign]
            manager._wait_healthy = AsyncMock(return_value=None)  # type: ignore[method-assign]
            manager._cleanup_images = AsyncMock(return_value=None)  # type: ignore[method-assign]

            await manager._run(operation, instance)

            loaded = store.get(operation.operation_id)
            assert loaded is not None
            self.assertEqual(loaded.status, "succeeded")
            self.assertIn("mimo-", instance.override_file.read_text(encoding="utf-8"))
            self.assertEqual(
                [step["status"] for step in loaded.steps],
                ["success"] * 6,
            )
            self.assertEqual(
                store.deployment_head("personal"),
                operation.operation_id,
            )

    async def test_image_names_are_unique_between_operations(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance, manager, _ = self.make_runtime(root)
            first = manager._image_name(instance, instance.project_root, "op_" + "a" * 32)
            second = manager._image_name(instance, instance.project_root, "op_" + "b" * 32)
        self.assertNotEqual(first, second)
        self.assertTrue(first.endswith("-" + "a" * 12))

    async def test_uv_runs_in_restricted_container(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, manager, _ = self.make_runtime(root)
            command = AsyncMock(return_value="locked")
            with patch("mimo_console_agent.manager.run_command", command):
                result = await manager._run_uv(
                    root,
                    ["lock", "--check"],
                    60,
                )
        self.assertEqual(result, "locked")
        self.assertEqual(command.await_count, 2)
        arguments = command.await_args_list[0].args[0]
        self.assertEqual(arguments[:3], ["docker", "run", "--rm"])
        self.assertIn("--name", arguments)
        self.assertIn("--label", arguments)
        self.assertIn("--cap-drop", arguments)
        self.assertIn("no-new-privileges", arguments)
        self.assertIn(
            "ghcr.io/astral-sh/uv:0.9.29-python3.12-bookworm-slim",
            arguments,
        )
        cleanup = command.await_args_list[1].args[0]
        self.assertEqual(cleanup[:3], ["docker", "rm", "--force"])

    async def test_helper_timeout_still_removes_named_container(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, manager, _ = self.make_runtime(root)
            command = AsyncMock(side_effect=[AgentError("timeout"), "removed"])
            with (
                patch("mimo_console_agent.manager.run_command", command),
                self.assertRaises(AgentError),
            ):
                await manager._run_uv(root / ("op_" + "a" * 32), ["lock"], 60)
        self.assertEqual(command.await_count, 2)
        self.assertEqual(command.await_args_list[1].args[0][:3], ["docker", "rm", "--force"])

    async def test_github_install_uses_source_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance, manager, _ = self.make_runtime(root)
            operation = Operation.create(
                "personal",
                "install",
                "nonebot_plugin_parser",
                "nonebot-plugin-parser",
                "https://github.com/MimoKit/nonebot-plugin-parser.git",
            )
            work = instance.project_root
            staging = work / ".mimo-source"

            async def clone_source(work, arguments, timeout):
                del work, arguments, timeout
                module = staging / "nonebot_plugin_parser"
                module.mkdir(parents=True)
                (module / "__init__.py").write_text(
                    "import httpx\nfrom nonebot import on_command\n",
                    encoding="utf-8",
                )
                return "cloned"

            manager._run_helper = AsyncMock(side_effect=clone_source)  # type: ignore[method-assign]
            manager._run_uv = AsyncMock(side_effect=["resolved", "checked"])  # type: ignore[method-assign]
            result = await manager._change_dependencies(operation, instance, work)
            manifest = (work / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn("cloned", result)
        self.assertEqual(operation.module_name, "nonebot_plugin_parser")
        self.assertIn("[tool.mimo_console.source_plugins]", manifest)
        self.assertIn('source_managed_dependencies = ["httpx"]', manifest)
        self.assertIn("https://github.com/MimoKit/nonebot-plugin-parser.git", manifest)
        first_arguments = manager._run_uv.await_args_list[0].args[1]  # type: ignore[union-attr]
        self.assertEqual(
            first_arguments,
            [
                "add",
                "--no-sync",
                "httpx",
            ],
        )

    async def test_github_install_rejects_discovered_console_module(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance, manager, _ = self.make_runtime(root)
            operation = Operation.create(
                "personal",
                "install",
                "nonebot_plugin_innocent_name",
                "innocent-project",
                "https://github.com/example/innocent-project.git",
            )
            staging = instance.project_root / ".mimo-source"

            async def clone_source(work, arguments, timeout):
                del work, arguments, timeout
                module = staging / "nonebot_plugin_mimo_console"
                module.mkdir(parents=True)
                (module / "__init__.py").write_text("", encoding="utf-8")
                return "cloned"

            manager._run_helper = AsyncMock(side_effect=clone_source)  # type: ignore[method-assign]
            with self.assertRaisesRegex(AgentError, "不能.*覆盖"):
                await manager._change_dependencies(
                    operation,
                    instance,
                    instance.project_root,
                )

    async def test_self_update_uses_allowlisted_git_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance, manager, _ = self.make_runtime(root)
            operation = Operation.create(
                "personal",
                "update",
                "nonebot_plugin_mimo_console",
                "nonebot-plugin-mimo-console",
                "https://github.com/MimoKit/nonebot-plugin-mimo-console.git",
            )
            manager._run_uv = AsyncMock(return_value="updated")  # type: ignore[method-assign]

            result = await manager._change_dependencies(
                operation,
                instance,
                instance.project_root,
            )

        self.assertEqual(result, "updated")
        arguments = manager._run_uv.await_args.args[1]  # type: ignore[union-attr]
        self.assertEqual(
            arguments,
            [
                "add",
                "--no-sync",
                "nonebot-plugin-mimo-console @ "
                "git+https://github.com/MimoKit/nonebot-plugin-mimo-console.git",
                "--upgrade-package",
                "nonebot-plugin-mimo-console",
            ],
        )

    async def test_submit_rejects_untrusted_update_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance, manager, _ = self.make_runtime(root)
            with self.assertRaises(AgentError):
                await manager.submit(
                    instance,
                    "update",
                    "nonebot_plugin_mimo_console",
                    "nonebot-plugin-mimo-console",
                    "https://github.com/example/fake-console.git",
                )

    async def test_submit_accepts_allowlisted_self_update_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance, manager, _ = self.make_runtime(root)
            manager._run = AsyncMock()  # type: ignore[method-assign]

            operation = await manager.submit(
                instance,
                "update",
                "nonebot_plugin_mimo_console",
                "nonebot-plugin-mimo-console",
                "https://github.com/MimoKit/nonebot-plugin-mimo-console.git",
            )
            await asyncio.sleep(0)

        self.assertEqual(
            operation.repository_url,
            "https://github.com/MimoKit/nonebot-plugin-mimo-console.git",
        )
        manager._run.assert_awaited_once()  # type: ignore[union-attr]

    def test_prepare_excludes_environment_and_in_project_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance, manager, _ = self.make_runtime(root)
            token = instance.project_root / "agent.token"
            token.write_text("token", encoding="utf-8")
            (instance.project_root / "private.key").write_text("key", encoding="utf-8")
            secrets = instance.project_root / "secrets"
            secrets.mkdir()
            (secrets / "credentials.json").write_text("{}", encoding="utf-8")
            instance = replace(instance, token_file=token)
            work = manager.config.state_dir / "work" / "sensitive"
            snapshot = manager.config.state_dir / "snapshots" / "sensitive"

            manager._prepare(instance, work, snapshot)

            self.assertFalse((work / ".env.prod").exists())
            self.assertFalse((work / "agent.token").exists())
            self.assertFalse((work / "private.key").exists())
            self.assertFalse((work / "secrets").exists())
            self.assertTrue((work / "pyproject.toml").is_file())

    async def test_github_install_rejects_symlinked_plugin_files(self) -> None:
        if os.name == "nt":
            self.skipTest("symlink creation requires privileges on Windows")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance, manager, _ = self.make_runtime(root)
            operation = Operation.create(
                "personal",
                "install",
                "nonebot_plugin_evil",
                "nonebot-plugin-evil",
                "https://github.com/MimoKit/nonebot-plugin-evil.git",
            )
            work = instance.project_root
            staging = work / ".mimo-source"

            async def clone_source(work, arguments, timeout):
                del work, arguments, timeout
                module = staging / "nonebot_plugin_evil"
                module.mkdir(parents=True)
                (module / "__init__.py").write_text("", encoding="utf-8")
                # A malicious repo pointing a plugin file at a host path.
                (module / "leak.py").symlink_to("/etc/hostname")
                return "cloned"

            manager._run_helper = AsyncMock(side_effect=clone_source)  # type: ignore[method-assign]
            manager._run_uv = AsyncMock(return_value="checked")  # type: ignore[method-assign]
            with self.assertRaises(AgentError) as ctx:
                await manager._change_dependencies(operation, instance, work)
            self.assertIn("符号链接", str(ctx.exception))
            self.assertFalse((work / "local_plugins" / "nonebot_plugin_evil").exists())

    async def test_prepare_rejects_symlink_in_existing_source_tree(self) -> None:
        if os.name == "nt":
            self.skipTest("symlink creation requires privileges on Windows")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance, manager, _ = self.make_runtime(root)
            module = instance.project_root / "local_plugins" / "nonebot_plugin_existing"
            module.mkdir(parents=True)
            (module / "__init__.py").write_text("", encoding="utf-8")
            (module / "leak.py").symlink_to("/etc/hostname")
            with self.assertRaises(AgentError) as ctx:
                manager._prepare(
                    instance,
                    manager.config.state_dir / "work" / "test",
                    manager.config.state_dir / "snapshots" / "test",
                )
            self.assertIn("符号链接", str(ctx.exception))

    async def test_source_plugin_update_rejects_implicit_rename(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance, manager, _ = self.make_runtime(root)
            (instance.project_root / "pyproject.toml").write_text(
                '[project]\nname = "bot"\n'
                "[tool.nonebot]\n"
                'plugin_dirs = ["local_plugins"]\n'
                "[tool.mimo_console.source_plugins]\n"
                "nonebot_plugin_demo = { project = "
                '"nonebot-plugin-demo", repository = '
                '"https://github.com/example/demo.git", dependencies = [] }\n',
                encoding="utf-8",
            )
            operation = Operation.create(
                "personal",
                "update",
                "nonebot_plugin_demo",
                "nonebot-plugin-demo",
            )
            work = instance.project_root
            staging = work / ".mimo-source"

            async def clone_source(work, arguments, timeout):
                del work, arguments, timeout
                # Repo renamed its package directory since install.
                module = staging / "nonebot_plugin_renamed"
                module.mkdir(parents=True)
                (module / "__init__.py").write_text("", encoding="utf-8")
                return "cloned"

            manager._run_helper = AsyncMock(side_effect=clone_source)  # type: ignore[method-assign]
            manager._run_uv = AsyncMock(return_value="checked")  # type: ignore[method-assign]
            with self.assertRaises(AgentError) as ctx:
                await manager._change_dependencies(operation, instance, work)
            self.assertIn("导入名", str(ctx.exception))

    async def test_source_plugin_uninstall_removes_tracked_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance, manager, _ = self.make_runtime(root)
            module = instance.project_root / "local_plugins" / "nonebot_plugin_demo"
            module.mkdir(parents=True)
            (module / "__init__.py").write_text("", encoding="utf-8")
            (instance.project_root / "pyproject.toml").write_text(
                '[project]\nname = "bot"\n'
                'dependencies = ["httpx"]\n'
                "[tool.nonebot]\n"
                'plugin_dirs = ["local_plugins"]\n'
                "[tool.mimo_console]\n"
                'source_managed_dependencies = ["httpx"]\n'
                "[tool.mimo_console.source_plugins]\n"
                "nonebot_plugin_demo = { project = "
                '"nonebot-plugin-demo", repository = '
                '"https://github.com/example/demo.git", dependencies = ["httpx"] }\n',
                encoding="utf-8",
            )
            operation = Operation.create(
                "personal",
                "uninstall",
                "nonebot_plugin_demo",
                "nonebot-plugin-demo",
            )
            manager._run_uv = AsyncMock(side_effect=["removed", "checked"])  # type: ignore[method-assign]

            result = await manager._change_dependencies(
                operation,
                instance,
                instance.project_root,
            )

            self.assertEqual(result, "removed\nchecked")
            self.assertFalse(module.exists())
            manifest = (instance.project_root / "pyproject.toml").read_text(encoding="utf-8")
            self.assertNotIn("nonebot_plugin_demo =", manifest)
            self.assertEqual(
                operation.repository_url,
                "https://github.com/example/demo.git",
            )
            # The dependency the plugin added (httpx) is not referenced by any other
            # source plugin or the project, so uninstall must uv-remove it too.
            remove_arguments = manager._run_uv.await_args_list[0].args[1]  # type: ignore[union-attr]
            self.assertEqual(remove_arguments, ["remove", "--no-sync", "httpx"])

    async def test_source_plugin_update_removes_dropped_managed_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance, manager, _ = self.make_runtime(root)
            module = instance.project_root / "local_plugins" / "nonebot_plugin_demo"
            module.mkdir(parents=True)
            (module / "__init__.py").write_text("import httpx\n", encoding="utf-8")
            (instance.project_root / "pyproject.toml").write_text(
                '[project]\nname = "bot"\n'
                'dependencies = ["httpx"]\n'
                "[tool.nonebot]\n"
                'plugin_dirs = ["local_plugins"]\n'
                "[tool.mimo_console]\n"
                'source_managed_dependencies = ["httpx"]\n'
                "[tool.mimo_console.source_plugins]\n"
                "nonebot_plugin_demo = { project = "
                '"nonebot-plugin-demo", repository = '
                '"https://github.com/example/demo.git", dependencies = ["httpx"] }\n',
                encoding="utf-8",
            )
            operation = Operation.create(
                "personal",
                "update",
                "nonebot_plugin_demo",
                "nonebot-plugin-demo",
            )
            staging = instance.project_root / ".mimo-source"

            async def clone_source(work, arguments, timeout):
                del work, arguments, timeout
                updated = staging / "nonebot_plugin_demo"
                updated.mkdir(parents=True)
                (updated / "__init__.py").write_text("import PIL\n", encoding="utf-8")
                return "cloned"

            manager._run_helper = AsyncMock(side_effect=clone_source)  # type: ignore[method-assign]
            manager._run_uv = AsyncMock(side_effect=["removed", "added", "checked"])  # type: ignore[method-assign]
            await manager._change_dependencies(
                operation,
                instance,
                instance.project_root,
            )
            calls = [
                call.args[1]
                for call in manager._run_uv.await_args_list  # type: ignore[union-attr]
            ]
            manifest = (instance.project_root / "pyproject.toml").read_text(encoding="utf-8")
        self.assertEqual(calls[0], ["remove", "--no-sync", "httpx"])
        self.assertEqual(calls[1], ["add", "--no-sync", "pillow"])
        self.assertIn('source_managed_dependencies = ["pillow"]', manifest)
        self.assertNotIn('dependencies = ["httpx"] }', manifest)

    async def test_container_lookup_uses_compose_identity_labels(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance, manager, _ = self.make_runtime(root)
            command = AsyncMock(return_value="abc123\n")
        with patch("mimo_console_agent.manager.run_command", command):
            container_id = await manager._container_id(instance)
        self.assertEqual(container_id, "abc123")
        self.assertIsNotNone(command.await_args)
        assert command.await_args is not None
        arguments = command.await_args.args[0]
        self.assertIn("label=com.docker.compose.project=personal", arguments)
        self.assertIn("label=com.docker.compose.service=bot", arguments)

    async def test_health_failure_restores_snapshot_and_old_image(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance, manager, store = self.make_runtime(root)
            original = (instance.project_root / "pyproject.toml").read_text(encoding="utf-8")
            instance.override_file.parent.mkdir(parents=True)
            original_override = (
                'services:\n  bot:\n    image: "local/bot:old"\n    environment:\n      KEEP: yes\n'
            )
            instance.override_file.write_text(original_override, encoding="utf-8")
            operation = store.save(
                Operation.create(
                    "personal",
                    "install",
                    "nonebot_plugin_demo",
                    "nonebot-plugin-demo",
                )
            )

            async def change_dependencies(operation, instance, work):
                del operation, instance
                (work / "pyproject.toml").write_text(
                    '[project]\nname = "changed"\n',
                    encoding="utf-8",
                )
                return "locked"

            manager._current_image = AsyncMock(return_value="local/bot:old")  # type: ignore[method-assign]
            manager._preflight = AsyncMock(return_value=None)  # type: ignore[method-assign]
            manager._change_dependencies = AsyncMock(side_effect=change_dependencies)  # type: ignore[method-assign]
            manager._build = AsyncMock(return_value="built")  # type: ignore[method-assign]
            manager._verify = AsyncMock(return_value="verified")  # type: ignore[method-assign]
            manager._compose_up = AsyncMock(return_value="deployed")  # type: ignore[method-assign]
            manager._wait_healthy = AsyncMock(  # type: ignore[method-assign]
                side_effect=[AgentError("health failed"), None]
            )

            await manager._run(operation, instance)

            loaded = store.get(operation.operation_id)
            assert loaded is not None
            self.assertEqual(loaded.status, "rolled_back")
            steps = {step["name"]: step for step in loaded.steps}
            self.assertEqual(steps["health"]["status"], "failed")
            self.assertEqual(steps["health"]["detail"], "health failed")
            self.assertEqual(steps["rollback"]["status"], "success")
            self.assertEqual(
                (instance.project_root / "pyproject.toml").read_text(encoding="utf-8"),
                original,
            )
            self.assertEqual(
                instance.override_file.read_text(encoding="utf-8"),
                original_override,
            )

    async def test_automatic_rollback_failure_remains_manually_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance, manager, store = self.make_runtime(root)
            operation = store.save(
                Operation.create(
                    "personal",
                    "install",
                    "nonebot_plugin_demo",
                    "nonebot-plugin-demo",
                )
            )
            manager._current_image = AsyncMock(return_value="local/bot:old")  # type: ignore[method-assign]
            manager._preflight = AsyncMock(return_value=None)  # type: ignore[method-assign]
            manager._change_dependencies = AsyncMock(return_value="locked")  # type: ignore[method-assign]
            manager._build = AsyncMock(return_value="built")  # type: ignore[method-assign]
            manager._verify = AsyncMock(return_value="verified")  # type: ignore[method-assign]
            manager._compose_up = AsyncMock(return_value="deployed")  # type: ignore[method-assign]
            manager._wait_healthy = AsyncMock(side_effect=AgentError("health failed"))  # type: ignore[method-assign]
            manager._rollback = AsyncMock(side_effect=AgentError("rollback failed"))  # type: ignore[method-assign]

            await manager._run(operation, instance)

            loaded = store.get(operation.operation_id)
            assert loaded is not None
            self.assertEqual(loaded.status, "failed")
            self.assertIn("rollback failed", loaded.error)
            self.assertEqual(
                store.deployment_head("personal"),
                operation.operation_id,
            )

    async def test_failed_manual_rollback_can_be_retried_from_old_image(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance, manager, store = self.make_runtime(root)
            operation = Operation.create(
                "personal",
                "install",
                "nonebot_plugin_demo",
                "nonebot-plugin-demo",
            )
            operation.status = "failed"
            operation.old_image = "local/bot:old"
            operation.new_image = "local/bot:new"
            store.save(operation)
            store.set_deployment_head("personal", operation.operation_id)
            manager._current_image = AsyncMock(return_value="local/bot:old")  # type: ignore[method-assign]
            manager._image_exists = AsyncMock(return_value=True)  # type: ignore[method-assign]
            manager._run_manual_rollback = AsyncMock()  # type: ignore[method-assign]

            result = await manager.rollback(instance, operation.operation_id)
            await asyncio.sleep(0)

        self.assertEqual(result.status, "rolling_back")
        manager._run_manual_rollback.assert_awaited_once()  # type: ignore[union-attr]

    async def test_first_restart_seeds_override_with_current_image(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance, manager, store = self.make_runtime(root)
            operation = store.save(Operation.create("personal", "restart", "", ""))
            manager._current_image = AsyncMock(return_value="local/bot:old")  # type: ignore[method-assign]
            manager._compose_up = AsyncMock(return_value="restarted")  # type: ignore[method-assign]
            manager._wait_healthy = AsyncMock(return_value=None)  # type: ignore[method-assign]

            await manager._run_restart(operation, instance)

            loaded = store.get(operation.operation_id)
            assert loaded is not None
            self.assertEqual(loaded.status, "succeeded")
            self.assertIn(
                '"local/bot:old"',
                instance.override_file.read_text(encoding="utf-8"),
            )

    async def test_restart_recovery_preserves_existing_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance, manager, store = self.make_runtime(root)
            custom_override = (
                'services:\n  bot:\n    image: "local/bot:old"\n    environment:\n      KEEP: yes\n'
            )
            instance.override_file.parent.mkdir(parents=True, exist_ok=True)
            instance.override_file.write_text(custom_override, encoding="utf-8")
            operation = store.save(Operation.create("personal", "restart", "", ""))
            operation.old_image = "local/bot:old"
            manager._compose_up = AsyncMock(return_value="restored")  # type: ignore[method-assign]
            manager._wait_healthy = AsyncMock(return_value=None)  # type: ignore[method-assign]

            await manager._recover_restart(operation, instance, "restart failed")

            self.assertEqual(
                instance.override_file.read_text(encoding="utf-8"),
                custom_override,
            )
            loaded = store.get(operation.operation_id)
            assert loaded is not None
            self.assertEqual(loaded.status, "rolled_back")

    async def test_manual_rollback_failure_marks_running_step_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance, manager, store = self.make_runtime(root)
            operation = Operation.create(
                "personal",
                "install",
                "nonebot_plugin_demo",
                "nonebot-plugin-demo",
            )
            operation.status = "rolling_back"
            operation.steps.append(
                {"name": "rollback", "status": "running", "detail": ""},
            )
            store.save(operation)
            manager._rollback = AsyncMock(side_effect=AgentError("compose failed"))  # type: ignore[method-assign]

            await manager._run_manual_rollback(operation, instance)

            loaded = store.get(operation.operation_id)
            assert loaded is not None
            self.assertEqual(loaded.status, "failed")
            rollback_step = next(step for step in loaded.steps if step["name"] == "rollback")
            self.assertEqual(rollback_step["status"], "failed")
            self.assertEqual(rollback_step["detail"], "compose failed")

    async def test_image_cleanup_keeps_recent_and_rollback_images(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance, manager, _ = self.make_runtime(root)
            command = AsyncMock(
                side_effect=[
                    "\n".join(
                        [
                            "local/bot:mimo-new",
                            "local/bot:mimo-recent",
                            "local/bot:mimo-old",
                            "local/bot:mimo-expired",
                        ]
                    ),
                    "Deleted: local/bot:mimo-expired",
                ]
            )
            with patch("mimo_console_agent.manager.run_command", command):
                await manager._cleanup_images(
                    instance,
                    {"local/bot:mimo-new", "local/bot:mimo-old"},
                )
            self.assertEqual(command.await_count, 2)
            remove_command = command.await_args_list[1].args[0]
            self.assertIn("local/bot:mimo-expired", remove_command)
            self.assertNotIn("local/bot:mimo-old", remove_command)

    async def test_startup_recovery_restores_live_project_after_interruption(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance, manager, store = self.make_runtime(root)
            original = (instance.project_root / "pyproject.toml").read_text(encoding="utf-8")
            operation = Operation.create(
                "personal",
                "install",
                "nonebot_plugin_demo",
                "nonebot-plugin-demo",
            )
            snapshot = manager.config.state_dir / "snapshots" / operation.operation_id
            snapshot.mkdir(parents=True)
            (snapshot / "pyproject.toml").write_text(original, encoding="utf-8")
            (snapshot / "uv.lock").write_text("version = 1\n", encoding="utf-8")
            operation.snapshot_dir = str(snapshot)
            operation.old_image = "local/bot:old"
            operation.status = "deploying"
            operation.steps.append({"name": "deploy", "status": "running", "detail": ""})
            store.save(operation)
            (instance.project_root / "pyproject.toml").write_text(
                '[project]\nname = "changed"\n',
                encoding="utf-8",
            )
            work = manager.config.state_dir / "work" / operation.operation_id
            work.mkdir(parents=True)
            manager._compose_up = AsyncMock(return_value="restored")  # type: ignore[method-assign]
            manager._wait_healthy = AsyncMock(return_value=None)  # type: ignore[method-assign]

            await manager.recover()

            recovered = store.get(operation.operation_id)
            assert recovered is not None
            self.assertEqual(recovered.status, "rolled_back")
            self.assertEqual(
                (instance.project_root / "pyproject.toml").read_text(encoding="utf-8"),
                original,
            )
            self.assertFalse(work.exists())
            steps = {step["name"]: step for step in recovered.steps}
            self.assertEqual(steps["deploy"]["status"], "failed")
            self.assertEqual(steps["rollback"]["status"], "success")

    async def test_failed_startup_recovery_remains_manually_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            instance, manager, store = self.make_runtime(root)
            operation = Operation.create(
                "personal",
                "install",
                "nonebot_plugin_demo",
                "nonebot-plugin-demo",
            )
            snapshot = manager.config.state_dir / "snapshots" / operation.operation_id
            snapshot.mkdir(parents=True)
            operation.snapshot_dir = str(snapshot)
            operation.old_image = "local/bot:old"
            operation.new_image = "local/bot:new"
            operation.status = "deploying"
            store.save(operation)
            manager._remove_helper_container = AsyncMock(return_value=None)  # type: ignore[method-assign]
            manager._rollback = AsyncMock(side_effect=AgentError("rollback failed"))  # type: ignore[method-assign]

            await manager.recover()

            recovered = store.get(operation.operation_id)
            assert recovered is not None
            self.assertEqual(recovered.status, "failed")
            self.assertIn("rollback failed", recovered.error)
            self.assertEqual(
                store.deployment_head(instance.instance_id),
                operation.operation_id,
            )


class AgentApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_requires_instance_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            helper = DeploymentTransactionTests()
            _, manager, _ = helper.make_runtime(root)
            transport = httpx.ASGITransport(app=create_app(manager.config))
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://agent",
            ) as client:
                denied = await client.get("/v1/status")
                allowed = await client.get(
                    "/v1/status",
                    headers={
                        "Authorization": "Bearer secret",
                        "X-Mimo-Instance": "personal",
                    },
                )
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(allowed.status_code, 200)
        self.assertTrue(allowed.json()["rollback"])
        self.assertTrue(allowed.json()["persistent_config"])

    async def test_configuration_round_trip_uses_instance_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            helper = DeploymentTransactionTests()
            instance, manager, _ = helper.make_runtime(root)
            transport = httpx.ASGITransport(app=create_app(manager.config))
            headers = {
                "Authorization": "Bearer secret",
                "X-Mimo-Instance": "personal",
            }
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://agent",
            ) as client:
                denied = await client.get("/v1/config")
                snapshot = await client.get("/v1/config", headers=headers)
                updated = await client.put(
                    "/v1/config",
                    headers=headers,
                    json={
                        "values": {
                            "PORT": "9000",
                            "API_TOKEN": MASK,
                            "NEW_VALUE": "yes",
                        }
                    },
                )
                oversized = await client.put(
                    "/v1/config",
                    headers=headers,
                    json={"values": {"VALUE": "x" * (257 * 1024)}},
                )
                listed = await client.get("/v1/config/backups", headers=headers)
                backup_id = listed.json()["items"][0]["backup_id"]
                restored = await client.post(
                    "/v1/config/restore",
                    headers=headers,
                    json={"backup_id": backup_id},
                )
                oversized_restore = await client.post(
                    "/v1/config/restore",
                    headers=headers,
                    content=b'{"backup_id":"' + b"x" * (257 * 1024) + b'"}',
                )
                oversized_operation = await client.post(
                    "/v1/operations",
                    headers=headers,
                    content=b'{"action":"install","project_name":"' + b"x" * (257 * 1024) + b'"}',
                )
            persisted = instance.environment_file.read_text(encoding="utf-8")
            backups = list(
                (manager.config.state_dir / "environment-backups" / "personal").glob("*.bak")
            )
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(snapshot.status_code, 200)
        self.assertEqual(snapshot.json()["path"], ".env.prod")
        items = {item["key"]: item for item in snapshot.json()["items"]}
        self.assertEqual(items["API_TOKEN"]["value"], MASK)
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(oversized.status_code, 413)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(oversized_restore.status_code, 413)
        self.assertEqual(oversized_operation.status_code, 413)
        self.assertTrue(updated.json()["backup_created"])
        self.assertIn("PORT=8080", persisted)
        self.assertIn("API_TOKEN=secret", persisted)
        self.assertTrue(backups)


if __name__ == "__main__":
    unittest.main()
