from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import fields
from pathlib import Path

from .models import NON_TERMINAL_STATUSES, Operation

MAX_OUTPUT = 200_000


class OperationStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY,
                    instance_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    module_name TEXT NOT NULL,
                    project_name TEXT NOT NULL,
                    repository_url TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    restart_required INTEGER NOT NULL,
                    output TEXT NOT NULL,
                    error TEXT NOT NULL,
                    steps TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    old_image TEXT NOT NULL,
                    new_image TEXT NOT NULL,
                    snapshot_dir TEXT NOT NULL
                )
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(operations)").fetchall()
            }
            if "repository_url" not in columns:
                connection.execute(
                    "ALTER TABLE operations ADD COLUMN repository_url TEXT NOT NULL DEFAULT ''"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS deployment_heads (
                    instance_id TEXT PRIMARY KEY,
                    operation_id TEXT NOT NULL
                )
                """
            )

    def save(self, operation: Operation) -> Operation:
        operation.updated_at = time.time()
        data = vars(operation).copy()
        data["restart_required"] = int(operation.restart_required)
        data["steps"] = json.dumps(operation.steps, ensure_ascii=False)
        columns = [item.name for item in fields(Operation)]
        placeholders = ", ".join("?" for _ in columns)
        updates = ", ".join(f"{column}=excluded.{column}" for column in columns[1:])
        with self._lock, self._connection() as connection:
            connection.execute(
                f"""
                INSERT INTO operations ({", ".join(columns)})
                VALUES ({placeholders})
                ON CONFLICT(operation_id) DO UPDATE SET {updates}
                """,
                [data[column] for column in columns],
            )
        return operation

    @staticmethod
    def _from_row(row: sqlite3.Row) -> Operation:
        return Operation(
            operation_id=str(row["operation_id"]),
            instance_id=str(row["instance_id"]),
            action=str(row["action"]),  # type: ignore[arg-type]
            module_name=str(row["module_name"]),
            project_name=str(row["project_name"]),
            repository_url=str(row["repository_url"]),
            status=str(row["status"]),
            restart_required=bool(row["restart_required"]),
            output=str(row["output"]),
            error=str(row["error"]),
            steps=list(json.loads(str(row["steps"]))),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            old_image=str(row["old_image"]),
            new_image=str(row["new_image"]),
            snapshot_dir=str(row["snapshot_dir"]),
        )

    def get(self, operation_id: str) -> Operation | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        return self._from_row(row) if row else None

    def list(self, instance_id: str, limit: int = 100) -> list[Operation]:
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM operations
                WHERE instance_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (instance_id, limit),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def active(self, instance_id: str) -> Operation | None:
        placeholders = ", ".join("?" for _ in NON_TERMINAL_STATUSES)
        with self._lock, self._connection() as connection:
            row = connection.execute(
                f"""
                SELECT * FROM operations
                WHERE instance_id = ? AND status IN ({placeholders})
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (instance_id, *sorted(NON_TERMINAL_STATUSES)),
            ).fetchone()
        return self._from_row(row) if row else None

    def deployment_head(self, instance_id: str) -> str:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT operation_id FROM deployment_heads WHERE instance_id = ?",
                (instance_id,),
            ).fetchone()
        return str(row["operation_id"]) if row else ""

    def set_deployment_head(self, instance_id: str, operation_id: str) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO deployment_heads (instance_id, operation_id)
                VALUES (?, ?)
                ON CONFLICT(instance_id) DO UPDATE
                SET operation_id = excluded.operation_id
                """,
                (instance_id, operation_id),
            )

    def clear_deployment_head(self, instance_id: str, operation_id: str) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                DELETE FROM deployment_heads
                WHERE instance_id = ? AND operation_id = ?
                """,
                (instance_id, operation_id),
            )

    def interrupted(self) -> list[Operation]:
        placeholders = ", ".join("?" for _ in NON_TERMINAL_STATUSES)
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM operations WHERE status IN ({placeholders})",
                tuple(sorted(NON_TERMINAL_STATUSES)),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def append_output(self, operation: Operation, text: str) -> None:
        if not text:
            return
        operation.output = (operation.output + text)[-MAX_OUTPUT:]
        self.save(operation)

    def step(self, operation: Operation, name: str, status: str, detail: str = "") -> None:
        for item in operation.steps:
            if item.get("name") == name:
                item.update(status=status, detail=detail)
                break
        else:
            operation.steps.append({"name": name, "status": status, "detail": detail})
        self.save(operation)

    def fail_running_steps(self, operation: Operation, detail: str) -> None:
        changed = False
        for item in operation.steps:
            if item.get("status") == "running":
                item.update(status="failed", detail=detail)
                changed = True
        if changed:
            self.save(operation)
