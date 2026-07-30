from __future__ import annotations

import importlib.metadata
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.exceptions import ParseError

from .store import normalize_project_name

REQUIREMENT_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
DEFAULT_PROTECTED = {
    normalize_project_name("nonebot2"),
    normalize_project_name("nonebot-plugin-mimo-console"),
}


def _direct_dependencies(project_root: Path) -> dict[str, dict[str, str]]:
    path = project_root / "pyproject.toml"
    if not path.is_file():
        return {}
    try:
        document = tomlkit.parse(path.read_text(encoding="utf-8"))
    except (OSError, ParseError):
        return {}
    raw_items = document.get("project", {}).get("dependencies", [])
    if not isinstance(raw_items, list):
        return {}
    result: dict[str, dict[str, str]] = {}
    for raw in raw_items:
        requirement = str(raw).strip()
        match = REQUIREMENT_NAME_RE.match(requirement)
        if not match:
            continue
        name = match.group(1)
        result[normalize_project_name(name)] = {
            "name": name,
            "requirement": requirement,
        }
    return result


def _source_plugin_distributions(project_root: Path) -> set[str]:
    path = project_root / "pyproject.toml"
    if not path.is_file():
        return set()
    try:
        document = tomlkit.parse(path.read_text(encoding="utf-8"))
    except (OSError, ParseError):
        return set()
    records = document.get("tool", {}).get("mimo_console", {}).get("source_plugins", {})
    if not isinstance(records, Mapping):
        return set()
    return {
        str(record.get("project") or "")
        for record in records.values()
        if isinstance(record, Mapping) and record.get("project")
    }


def dependency_snapshot(
    project_root: Path,
    plugin_distributions: set[str] | None = None,
) -> dict[str, Any]:
    direct = _direct_dependencies(project_root)
    plugins = {
        normalize_project_name(item)
        for item in (
            set(plugin_distributions or set()) | _source_plugin_distributions(project_root)
        )
    }
    protected = DEFAULT_PROTECTED | plugins
    installed: dict[str, dict[str, str]] = {}
    for distribution in importlib.metadata.distributions():
        name = str(distribution.metadata["Name"] or "").strip()
        if not name:
            continue
        installed[normalize_project_name(name)] = {
            "name": name,
            "version": str(distribution.version or ""),
        }

    items: list[dict[str, Any]] = []
    for key in sorted(set(installed) | set(direct)):
        installed_item = installed.get(key, {})
        direct_item = direct.get(key, {})
        is_direct = key in direct
        is_plugin = key in plugins
        is_protected = key in protected
        items.append(
            {
                "name": installed_item.get("name") or direct_item.get("name") or key,
                "normalized_name": key,
                "version": installed_item.get("version", ""),
                "requirement": direct_item.get("requirement", ""),
                "direct": is_direct,
                "installed": key in installed,
                "kind": "plugin" if is_plugin else "core" if is_protected else "dependency",
                "manageable": is_direct and not is_protected,
            }
        )
    items.sort(
        key=lambda item: (
            not item["direct"],
            item["kind"] != "dependency",
            item["name"].casefold(),
        )
    )
    return {
        "path": str(project_root / "pyproject.toml"),
        "items": items,
        "total": len(items),
        "direct": sum(1 for item in items if item["direct"]),
    }
