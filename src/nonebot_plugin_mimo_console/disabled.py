from __future__ import annotations

import json
from pathlib import Path


class DisabledStore:
    """被禁用插件名的持久化存储（JSON 数组文件）。"""

    def __init__(self, data_file: Path, protected: tuple[str, ...] = ()) -> None:
        self.data_file = data_file
        self._protected = set(protected)
        self._names: set[str] = self._load()

    def _load(self) -> set[str]:
        try:
            data = json.loads(self.data_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        if not isinstance(data, list):
            return set()
        return {str(name) for name in data if name}

    def _save(self) -> None:
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        temp = self.data_file.with_suffix(self.data_file.suffix + ".tmp")
        temp.write_text(
            json.dumps(sorted(self._names), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp.replace(self.data_file)

    @property
    def names(self) -> set[str]:
        return set(self._names)

    def is_disabled(self, name: str) -> bool:
        return name in self._names

    def set(self, name: str, disabled: bool) -> None:
        if name in self._protected:
            raise ValueError("不能禁用控制台自身")
        if disabled:
            self._names.add(name)
        else:
            self._names.discard(name)
        self._save()
