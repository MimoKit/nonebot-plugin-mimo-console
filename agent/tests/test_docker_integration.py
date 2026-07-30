from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.request
import uuid
from pathlib import Path

from mimo_console_agent.config import AgentConfig, InstanceConfig
from mimo_console_agent.manager import DeploymentManager
from mimo_console_agent.models import Operation
from mimo_console_agent.storage import OperationStore


def _run(*arguments: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        arguments,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_http(url: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except Exception as exc:
            last_error = exc
        time.sleep(0.25)
    raise AssertionError(f"test service did not become healthy: {last_error}")


@unittest.skipUnless(
    os.environ.get("MIMO_RUN_DOCKER_TESTS") == "1",
    "set MIMO_RUN_DOCKER_TESTS=1 to run destructive Docker integration tests",
)
class DockerDeploymentIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_dependency_deploy_and_manual_rollback_preserve_override(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mimo-agent-docker-") as temp:
            root = Path(temp)
            project = root / "bot"
            project.mkdir()
            port = _free_port()
            suffix = uuid.uuid4().hex[:10]
            compose_project = f"mimo-review-{suffix}"
            image_repository = f"mimo-review-bot-{suffix}"
            initial_image = f"{image_repository}:initial"
            server = (
                "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer\n"
                "class Handler(BaseHTTPRequestHandler):\n"
                "    def do_GET(self):\n"
                "        self.send_response(200)\n"
                "        self.end_headers()\n"
                "        self.wfile.write(b'ok')\n"
                "    def log_message(self, *args):\n"
                "        pass\n"
                "ThreadingHTTPServer(('0.0.0.0', 8080), Handler).serve_forever()\n"
            )
            (project / "server.py").write_text(server, encoding="utf-8")
            (project / "Dockerfile").write_text(
                "FROM python:3.12-slim\n"
                "WORKDIR /app\n"
                "COPY server.py .\n"
                'CMD ["python", "server.py"]\n',
                encoding="utf-8",
            )
            (project / "compose.yml").write_text(
                "services:\n"
                "  bot:\n"
                f'    image: "{initial_image}"\n'
                "    build: .\n"
                "    ports:\n"
                f'      - "127.0.0.1:{port}:8080"\n',
                encoding="utf-8",
            )
            original_override = (
                "services:\n"
                "  bot:\n"
                f'    image: "{initial_image}"\n'
                "    build:\n"
                "      context: .\n"
                "    environment:\n"
                '      PRESERVED: "yes"\n'
                "    labels:\n"
                '      mimo.review.preserved: "yes"\n'
            )
            override = project / ".mimo" / "override.yml"
            override.parent.mkdir()
            override.write_text(original_override, encoding="utf-8")
            pyproject = project / "pyproject.toml"
            original_pyproject = (
                "[project]\n"
                'name = "mimo-review-bot"\n'
                'version = "0.0.0"\n'
                'requires-python = ">=3.12"\n'
                "dependencies = []\n"
            )
            pyproject.write_text(original_pyproject, encoding="utf-8")
            _run("uv", "lock", "--project", str(project), cwd=project)
            original_lock = (project / "uv.lock").read_text(encoding="utf-8")
            token = root / "token"
            token.write_text("secret", encoding="utf-8")
            compose = (
                "docker",
                "compose",
                "--project-directory",
                str(project),
                "--project-name",
                compose_project,
                "--file",
                str(project / "compose.yml"),
                "--file",
                str(override),
            )
            try:
                _run(*compose, "up", "--detach", "--build", "bot", cwd=project)
                _wait_http(f"http://127.0.0.1:{port}/health")
                instance = InstanceConfig(
                    instance_id="review",
                    token_file=token,
                    project_root=project,
                    compose_files=(project / "compose.yml",),
                    compose_project=compose_project,
                    service="bot",
                    dockerfile=project / "Dockerfile",
                    build_context=project,
                    image_repository=image_repository,
                    override_file=override,
                    environment_file=project / ".env.prod",
                    health_url=f"http://127.0.0.1:{port}/health",
                    health_timeout=30,
                    build_timeout=300,
                    deploy_timeout=120,
                )
                config = AgentConfig(
                    socket_path=root / "agent.sock",
                    socket_mode=0o660,
                    socket_gid=None,
                    state_dir=root / "state",
                    docker_bin="docker",
                    uv_image="ghcr.io/astral-sh/uv:0.9.29-python3.12-bookworm-slim",
                    instances={"review": instance},
                )
                store = OperationStore(config.state_dir / "operations.sqlite3")
                manager = DeploymentManager(config, store)

                async def verified(*args: object) -> str:
                    del args
                    return "verification bypassed by integration fixture"

                manager._verify = verified  # type: ignore[method-assign]
                real_wait_healthy = manager._wait_healthy
                health_attempt = 0

                async def fail_restart_health_once(target: InstanceConfig) -> None:
                    nonlocal health_attempt
                    health_attempt += 1
                    if health_attempt == 1:
                        raise RuntimeError("forced restart health failure")
                    await real_wait_healthy(target)

                manager._wait_healthy = fail_restart_health_once  # type: ignore[method-assign]
                restart = store.save(Operation.create("review", "restart", "", ""))
                await manager._run_restart(restart, instance)
                recovered_restart = store.get(restart.operation_id)
                assert recovered_restart is not None
                self.assertEqual(
                    recovered_restart.status,
                    "rolled_back",
                    recovered_restart.error,
                )
                self.assertEqual(override.read_text(encoding="utf-8"), original_override)
                manager._wait_healthy = real_wait_healthy  # type: ignore[method-assign]

                operation = store.save(Operation.create("review", "install", "", "idna"))
                await manager._run(operation, instance)
                deployed = store.get(operation.operation_id)
                assert deployed is not None
                if deployed.status != "succeeded":
                    diagnostics = subprocess.run(
                        [*compose, "ps", "--all"],
                        cwd=project,
                        capture_output=True,
                        text=True,
                        check=False,
                    ).stdout
                    diagnostics += subprocess.run(
                        [*compose, "logs", "--no-color", "bot"],
                        cwd=project,
                        capture_output=True,
                        text=True,
                        check=False,
                    ).stdout
                    self.fail(f"{deployed.error}\n{diagnostics}")
                deployed_override = override.read_text(encoding="utf-8")
                self.assertIn("build: !reset null", deployed_override)
                self.assertIn('PRESERVED: "yes"', deployed_override)
                self.assertIn('mimo.review.preserved: "yes"', deployed_override)
                self.assertIn('"idna', pyproject.read_text(encoding="utf-8"))

                container_id = _run(
                    "docker",
                    "ps",
                    "--filter",
                    f"label=com.docker.compose.project={compose_project}",
                    "--filter",
                    "label=com.docker.compose.service=bot",
                    "--format",
                    "{{.ID}}",
                )
                environment = _run(
                    "docker",
                    "inspect",
                    "--format",
                    "{{range .Config.Env}}{{println .}}{{end}}",
                    container_id,
                )
                self.assertIn("PRESERVED=yes", environment)

                await manager.rollback(instance, operation.operation_id)
                rollback_task = manager._tasks[operation.operation_id]
                await asyncio.wait_for(rollback_task, timeout=120)
                rolled_back = store.get(operation.operation_id)
                assert rolled_back is not None
                self.assertEqual(rolled_back.status, "rolled_back", rolled_back.error)
                self.assertEqual(pyproject.read_text(encoding="utf-8"), original_pyproject)
                self.assertEqual(
                    (project / "uv.lock").read_text(encoding="utf-8"),
                    original_lock,
                )
                self.assertEqual(override.read_text(encoding="utf-8"), original_override)
                container_id = _run(
                    "docker",
                    "ps",
                    "--filter",
                    f"label=com.docker.compose.project={compose_project}",
                    "--filter",
                    "label=com.docker.compose.service=bot",
                    "--format",
                    "{{.ID}}",
                )
                current_image = _run(
                    "docker",
                    "inspect",
                    "--format",
                    "{{.Config.Image}}",
                    container_id,
                )
                self.assertEqual(current_image, initial_image)
            finally:
                subprocess.run(
                    [*compose, "down", "--remove-orphans"],
                    cwd=project,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                generated_images = subprocess.run(
                    [
                        "docker",
                        "image",
                        "ls",
                        "--filter",
                        f"reference={image_repository}:mimo-*",
                        "--format",
                        "{{.Repository}}:{{.Tag}}",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                ).stdout.splitlines()
                subprocess.run(
                    [
                        "docker",
                        "image",
                        "rm",
                        "--force",
                        initial_image,
                        *generated_images,
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
