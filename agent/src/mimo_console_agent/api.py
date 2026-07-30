from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .config import AgentConfig, InstanceConfig
from .manager import AgentError, DeploymentManager
from .models import Operation
from .storage import OperationStore

MAX_CONFIG_REQUEST_BYTES = 256 * 1024


class RequestPayloadError(ValueError):
    def __init__(self, detail: str, status_code: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


async def _read_json_object(
    request: Request,
    *,
    limit: int = MAX_CONFIG_REQUEST_BYTES,
) -> dict[str, Any]:
    raw_length = request.headers.get("content-length")
    if raw_length is not None:
        try:
            content_length = int(raw_length)
        except ValueError as exc:
            raise RequestPayloadError("Content-Length 不合法") from exc
        if content_length < 0:
            raise RequestPayloadError("Content-Length 不合法")
        if content_length > limit:
            raise RequestPayloadError("JSON 请求超过 256 KiB 限制", 413)

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise RequestPayloadError("JSON 请求超过 256 KiB 限制", 413)
        body.extend(chunk)
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RequestPayloadError("请求必须是 JSON") from exc
    if not isinstance(payload, dict):
        raise RequestPayloadError("请求必须是 JSON 对象")
    return payload


def _bearer(request: Request) -> str:
    value = request.headers.get("authorization", "")
    if not value.startswith("Bearer "):
        return ""
    return value[7:]


def _public(operation: Operation, store: OperationStore) -> dict[str, Any]:
    value = operation.public_dict()
    value["rollback_available"] = (
        operation.action != "restart"
        and operation.status in {"succeeded", "failed"}
        and store.deployment_head(operation.instance_id) == operation.operation_id
    )
    return value


def create_app(config: AgentConfig) -> Starlette:
    store = OperationStore(config.state_dir / "operations.sqlite3")
    manager = DeploymentManager(config, store)

    def instance_for(request: Request) -> InstanceConfig:
        instance_id = request.headers.get("x-mimo-instance", "")
        return manager.authenticate(instance_id, _bearer(request))

    async def status(request: Request) -> JSONResponse:
        instance = instance_for(request)
        active = store.active(instance.instance_id)
        return JSONResponse(
            {
                "available": True,
                "persistent_image": True,
                "persistent_config": True,
                "rollback": True,
                "instance_id": instance.instance_id,
                "active_operation": _public(active, store) if active else None,
            }
        )

    async def list_operations(request: Request) -> JSONResponse:
        instance = instance_for(request)
        return JSONResponse(
            {"items": [_public(item, store) for item in store.list(instance.instance_id)]}
        )

    async def read_configuration(request: Request) -> JSONResponse:
        instance = instance_for(request)
        return JSONResponse(await manager.read_configuration(instance))

    async def update_configuration(request: Request) -> JSONResponse:
        instance = instance_for(request)
        try:
            payload = await _read_json_object(request)
        except RequestPayloadError as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        if not isinstance(payload.get("values"), dict):
            return JSONResponse({"detail": "values 必须是 JSON 对象"}, status_code=400)
        return JSONResponse(await manager.update_configuration(instance, payload["values"]))

    async def list_configuration_backups(request: Request) -> JSONResponse:
        instance = instance_for(request)
        return JSONResponse(await manager.list_configuration_backups(instance))

    async def restore_configuration(request: Request) -> JSONResponse:
        instance = instance_for(request)
        try:
            payload = await _read_json_object(request)
        except RequestPayloadError as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        backup_id = payload.get("backup_id")
        if not isinstance(backup_id, str) or not backup_id or len(backup_id) > 255:
            return JSONResponse({"detail": "backup_id 不合法"}, status_code=400)
        return JSONResponse(await manager.restore_configuration(instance, backup_id))

    async def get_operation(request: Request) -> JSONResponse:
        instance = instance_for(request)
        operation = store.get(request.path_params["operation_id"])
        if operation is None or operation.instance_id != instance.instance_id:
            return JSONResponse({"detail": "操作不存在"}, status_code=404)
        return JSONResponse(_public(operation, store))

    async def create_operation(request: Request) -> JSONResponse:
        instance = instance_for(request)
        try:
            payload = await _read_json_object(request)
        except RequestPayloadError as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        operation = await manager.submit(
            instance,
            str(payload.get("action") or ""),  # type: ignore[arg-type]
            str(payload.get("module_name") or ""),
            str(payload.get("project_name") or ""),
            str(payload.get("repository_url") or ""),
        )
        return JSONResponse(_public(operation, store), status_code=202)

    async def restart_instance(request: Request) -> JSONResponse:
        instance = instance_for(request)
        operation = await manager.submit_restart(instance)
        return JSONResponse(_public(operation, store), status_code=202)

    async def rollback_operation(request: Request) -> JSONResponse:
        instance = instance_for(request)
        operation = await manager.rollback(instance, request.path_params["operation_id"])
        return JSONResponse(_public(operation, store))

    @asynccontextmanager
    async def lifespan(app: Starlette):
        del app
        await manager.recover()
        yield

    async def agent_error(request: Request, exc: Exception) -> JSONResponse:
        del request
        status = 401 if str(exc) in {"认证失败", "实例令牌不可用"} else 400
        return JSONResponse({"detail": str(exc)}, status_code=status)

    app = Starlette(
        routes=[
            Route("/v1/status", status, methods=["GET"]),
            Route("/v1/config", read_configuration, methods=["GET"]),
            Route("/v1/config", update_configuration, methods=["PUT"]),
            Route(
                "/v1/config/backups",
                list_configuration_backups,
                methods=["GET"],
            ),
            Route(
                "/v1/config/restore",
                restore_configuration,
                methods=["POST"],
            ),
            Route("/v1/operations", list_operations, methods=["GET"]),
            Route("/v1/operations", create_operation, methods=["POST"]),
            Route("/v1/restart", restart_instance, methods=["POST"]),
            Route(
                "/v1/operations/{operation_id:str}",
                get_operation,
                methods=["GET"],
            ),
            Route(
                "/v1/operations/{operation_id:str}/rollback",
                rollback_operation,
                methods=["POST"],
            ),
        ],
        lifespan=lifespan,
        exception_handlers={AgentError: agent_error},
    )
    app.state.manager = manager
    app.state.operation_store = store
    return app
