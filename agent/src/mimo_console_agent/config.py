from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SAFE_SERVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SAFE_IMAGE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}(?::[A-Za-z0-9][A-Za-z0-9._-]{0,127})?$"
)


class ConfigError(ValueError):
    pass


def _resolved(path: object, *, base: Path | None = None) -> Path:
    if path is None or not str(path).strip():
        raise ConfigError("缺少必填路径")
    value = Path(str(path)).expanduser()
    if not value.is_absolute():
        if base is None:
            raise ConfigError(f"路径必须是绝对路径：{value}")
        value = base / value
    return value.resolve()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _bounded_int(
    raw: object,
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(str(raw if raw is not None else default))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} 必须是整数") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} 必须在 {minimum}-{maximum} 之间")
    return value


def _reject_instance_collisions(instances: dict[str, InstanceConfig]) -> None:
    unique_fields: tuple[tuple[str, str], ...] = (
        ("project_root", "项目目录"),
        ("token_file", "令牌文件"),
        ("compose_project", "Compose 项目名"),
        ("override_file", "Compose override"),
        ("environment_file", "环境文件"),
        ("image_repository", "镜像仓库"),
        ("health_url", "健康检查地址"),
    )
    for attribute, label in unique_fields:
        owners: dict[object, str] = {}
        for instance_id, instance in instances.items():
            value = getattr(instance, attribute)
            previous = owners.get(value)
            if previous is not None:
                raise ConfigError(f"实例 {previous} 与 {instance_id} 共用了{label}：{value}")
            owners[value] = instance_id


@dataclass(frozen=True)
class InstanceConfig:
    instance_id: str
    token_file: Path
    project_root: Path
    compose_files: tuple[Path, ...]
    compose_project: str
    service: str
    dockerfile: Path
    build_context: Path
    image_repository: str
    override_file: Path
    environment_file: Path
    health_url: str
    health_timeout: int = 120
    build_timeout: int = 1800
    deploy_timeout: int = 300
    keep_images: int = 3
    snapshot_keep: int = 10
    environment_backup_keep: int = 20

    @classmethod
    def from_mapping(cls, instance_id: str, raw: dict[str, Any]) -> InstanceConfig:
        if not SAFE_ID_RE.fullmatch(instance_id):
            raise ConfigError(f"实例 ID 不合法：{instance_id}")
        project_root = _resolved(raw.get("project_root"))
        if not project_root.is_dir():
            raise ConfigError(f"项目目录不存在：{project_root}")
        compose_files = tuple(
            _resolved(path, base=project_root) for path in raw.get("compose_files", [])
        )
        if not compose_files or any(not path.is_file() for path in compose_files):
            raise ConfigError(f"{instance_id} 必须配置存在的 Compose 文件")
        service = str(raw.get("service") or "").strip()
        compose_project = str(raw.get("compose_project") or "").strip()
        for label, value in (
            ("service", service),
            ("compose_project", compose_project),
        ):
            if not SAFE_SERVICE_RE.fullmatch(value):
                raise ConfigError(f"{instance_id} 的 {label} 不合法")
        dockerfile = _resolved(raw.get("dockerfile", "Dockerfile"), base=project_root)
        build_context = _resolved(raw.get("build_context", "."), base=project_root)
        override_file = _resolved(
            raw.get("override_file", ".mimo/docker-compose.override.yml"),
            base=project_root,
        )
        environment_file = _resolved(
            raw.get("environment_file", ".env.prod"),
            base=project_root,
        )
        for label, path in (
            ("Dockerfile", dockerfile),
            ("构建上下文", build_context),
            ("Compose override", override_file),
            ("环境文件", environment_file),
        ):
            if not _inside(path, project_root):
                raise ConfigError(f"{instance_id} 的{label}超出项目目录")
        if not dockerfile.is_file():
            raise ConfigError(f"Dockerfile 不存在：{dockerfile}")
        if not build_context.is_dir():
            raise ConfigError(f"构建上下文不存在：{build_context}")
        if environment_file.exists() and not environment_file.is_file():
            raise ConfigError(f"环境文件不是普通文件：{environment_file}")
        image_repository = str(raw.get("image_repository") or "").strip()
        has_tag = ":" in image_repository.rsplit("/", 1)[-1]
        if not SAFE_IMAGE_RE.fullmatch(image_repository) or has_tag:
            raise ConfigError("image_repository 必须是不带标签的安全镜像名")
        health_url = str(raw.get("health_url") or "").strip()
        parsed = urlparse(health_url)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
            or parsed.username
            or parsed.password
        ):
            raise ConfigError(f"{instance_id} 的 health_url 必须使用无凭据的回环地址")
        token_file = _resolved(raw.get("token_file"))
        if not token_file.is_file():
            raise ConfigError(f"实例令牌文件不存在：{token_file}")
        health_timeout = _bounded_int(raw.get("health_timeout"), "health_timeout", 120, 10, 900)
        build_timeout = _bounded_int(raw.get("build_timeout"), "build_timeout", 1800, 60, 7200)
        deploy_timeout = _bounded_int(raw.get("deploy_timeout"), "deploy_timeout", 300, 30, 1800)
        keep_images = _bounded_int(raw.get("keep_images"), "keep_images", 3, 1, 20)
        snapshot_keep = _bounded_int(raw.get("snapshot_keep"), "snapshot_keep", 10, 1, 100)
        environment_backup_keep = _bounded_int(
            raw.get("environment_backup_keep"),
            "environment_backup_keep",
            20,
            1,
            100,
        )
        return cls(
            instance_id=instance_id,
            token_file=token_file,
            project_root=project_root,
            compose_files=compose_files,
            compose_project=compose_project,
            service=service,
            dockerfile=dockerfile,
            build_context=build_context,
            image_repository=image_repository,
            override_file=override_file,
            environment_file=environment_file,
            health_url=health_url,
            health_timeout=health_timeout,
            build_timeout=build_timeout,
            deploy_timeout=deploy_timeout,
            keep_images=keep_images,
            snapshot_keep=snapshot_keep,
            environment_backup_keep=environment_backup_keep,
        )


@dataclass(frozen=True)
class AgentConfig:
    socket_path: Path
    socket_mode: int
    socket_gid: int | None
    state_dir: Path
    docker_bin: str
    uv_image: str
    instances: dict[str, InstanceConfig]

    @classmethod
    def load(cls, path: Path) -> AgentConfig:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"无法读取 Agent 配置：{exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError("Agent 配置必须是 JSON 对象")
        state_dir = _resolved(raw.get("state_dir", "/var/lib/mimo-console-agent"))
        socket_path = _resolved(raw.get("socket_path", "/run/mimo-agent/agent.sock"))
        socket_mode_raw = raw.get("socket_mode", "0660")
        try:
            socket_mode = (
                socket_mode_raw
                if isinstance(socket_mode_raw, int)
                else int(str(socket_mode_raw), 8)
            )
        except (TypeError, ValueError) as exc:
            raise ConfigError("socket_mode 必须是八进制权限，例如 0660") from exc
        if socket_mode < 0o600 or socket_mode > 0o770:
            raise ConfigError("socket_mode 必须在 0600-0770 之间")
        socket_gid_raw = raw.get("socket_gid")
        try:
            socket_gid = int(socket_gid_raw) if socket_gid_raw is not None else None
        except (TypeError, ValueError) as exc:
            raise ConfigError("socket_gid 必须是整数") from exc
        if socket_gid is not None and socket_gid < 0:
            raise ConfigError("socket_gid 不能为负数")
        raw_instances = raw.get("instances")
        if not isinstance(raw_instances, dict) or not raw_instances:
            raise ConfigError("Agent 至少需要一个实例")
        instances = {
            str(instance_id): InstanceConfig.from_mapping(str(instance_id), value)
            for instance_id, value in raw_instances.items()
            if isinstance(value, dict)
        }
        if len(instances) != len(raw_instances):
            raise ConfigError("实例配置必须是 JSON 对象")
        for instance in instances.values():
            if _inside(instance.token_file, instance.project_root):
                raise ConfigError(f"{instance.instance_id} 的令牌文件不能位于项目目录内")
            if _inside(state_dir, instance.project_root):
                raise ConfigError(f"state_dir 不能位于 {instance.instance_id} 的项目目录内")
            if _inside(socket_path, instance.project_root):
                raise ConfigError(f"socket_path 不能位于 {instance.instance_id} 的项目目录内")
        _reject_instance_collisions(instances)
        uv_image = str(raw.get("uv_image") or "").strip()
        image_leaf = uv_image.rsplit("/", 1)[-1]
        is_pinned = ":" in image_leaf or "@sha256:" in uv_image
        if (
            not uv_image
            or uv_image.startswith("-")
            or any(character.isspace() for character in uv_image)
            or not is_pinned
        ):
            raise ConfigError("uv_image 必须是带版本的安全容器镜像名")
        return cls(
            socket_path=socket_path,
            socket_mode=socket_mode,
            socket_gid=socket_gid,
            state_dir=state_dir,
            docker_bin=str(raw.get("docker_bin") or "docker"),
            uv_image=uv_image,
            instances=instances,
        )
