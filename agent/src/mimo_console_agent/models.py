from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

PackageAction = Literal["install", "update", "uninstall"]
OperationAction = Literal["install", "update", "uninstall", "restart"]

NON_TERMINAL_STATUSES = {
    "queued",
    "preparing",
    "locking",
    "building",
    "verifying",
    "deploying",
    "health_checking",
    "rolling_back",
}


@dataclass
class Operation:
    operation_id: str
    instance_id: str
    action: OperationAction
    module_name: str
    project_name: str
    repository_url: str = ""
    status: str = "queued"
    restart_required: bool = False
    output: str = ""
    error: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    old_image: str = ""
    new_image: str = ""
    snapshot_dir: str = ""

    @classmethod
    def create(
        cls,
        instance_id: str,
        action: OperationAction,
        module_name: str,
        project_name: str,
        repository_url: str = "",
    ) -> Operation:
        return cls(
            operation_id=f"op_{uuid.uuid4().hex}",
            instance_id=instance_id,
            action=action,
            module_name=module_name,
            project_name=project_name,
            repository_url=repository_url,
        )

    def public_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("old_image", None)
        value.pop("new_image", None)
        value.pop("snapshot_dir", None)
        value["ok"] = self.status == "succeeded"
        value["project_link"] = self.project_name
        return value
