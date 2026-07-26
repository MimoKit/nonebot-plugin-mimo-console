from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "src" / "nonebot_plugin_mimo_console" / "disabled.py"
spec = importlib.util.spec_from_file_location("mimo_console_disabled_test", MODULE_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError("cannot load disabled module")
disabled_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = disabled_module
spec.loader.exec_module(disabled_module)

DisabledStore = disabled_module.DisabledStore


class DisabledStoreTests(unittest.TestCase):
    def _store(self, protected: tuple[str, ...] = ()) -> tuple[DisabledStore, Path]:
        tmp = Path(tempfile.mkdtemp(prefix="mimo-disabled-test-"))
        return DisabledStore(tmp / "disabled_plugins.json", protected=protected), tmp

    def test_empty_by_default(self) -> None:
        store, _ = self._store()
        self.assertEqual(store.names, set())
        self.assertFalse(store.is_disabled("some_plugin"))

    def test_set_and_persist(self) -> None:
        store, tmp = self._store()
        store.set("some_plugin", True)
        self.assertTrue(store.is_disabled("some_plugin"))
        reloaded = DisabledStore(tmp / "disabled_plugins.json")
        self.assertTrue(reloaded.is_disabled("some_plugin"))

    def test_enable_removes_name(self) -> None:
        store, _ = self._store()
        store.set("some_plugin", True)
        store.set("some_plugin", False)
        self.assertFalse(store.is_disabled("some_plugin"))

    def test_protected_rejected(self) -> None:
        store, _ = self._store(protected=("nonebot_plugin_mimo_console",))
        with self.assertRaises(ValueError):
            store.set("nonebot_plugin_mimo_console", True)
        self.assertFalse(store.is_disabled("nonebot_plugin_mimo_console"))

    def test_corrupt_file_treated_as_empty(self) -> None:
        store, tmp = self._store()
        (tmp / "disabled_plugins.json").write_text("not json", encoding="utf-8")
        reloaded = DisabledStore(tmp / "disabled_plugins.json")
        self.assertEqual(reloaded.names, set())

    def test_saved_file_is_sorted_json_list(self) -> None:
        store, tmp = self._store()
        store.set("b_plugin", True)
        store.set("a_plugin", True)
        data = json.loads((tmp / "disabled_plugins.json").read_text(encoding="utf-8"))
        self.assertEqual(data, ["a_plugin", "b_plugin"])


if __name__ == "__main__":
    unittest.main()
