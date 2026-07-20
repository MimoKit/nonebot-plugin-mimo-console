from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "src" / "nonebot_plugin_mimo_console" / "env_editor.py"
spec = importlib.util.spec_from_file_location("mimo_console_env_editor_test", MODULE_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError("cannot load env_editor module")
env_editor = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = env_editor
spec.loader.exec_module(env_editor)
MASK = env_editor.MASK
read_env = env_editor.read_env
update_env = env_editor.update_env


class EnvEditorTests(unittest.TestCase):
    def test_adapter_credentials_are_masked(self) -> None:
        self.assertTrue(env_editor.is_secret_key("QQ_BOTS"))
        self.assertTrue(env_editor.is_secret_key("ONEBOT_BOTS"))
        self.assertTrue(env_editor.is_secret_key("TELEGRAM_AUTHORIZATION"))

    def test_read_masks_secret_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / ".env.prod"
            path.write_text("PORT=8080\nAPI_TOKEN=secret\n", encoding="utf-8")
            values = {item.key: item for item in read_env(path)}
            self.assertEqual(values["PORT"].value, "8080")
            self.assertEqual(values["API_TOKEN"].value, MASK)
            self.assertTrue(values["API_TOKEN"].secret)

    def test_update_preserves_comments_and_unchanged_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / ".env.prod"
            path.write_text("# comment\nPORT=8080\nAPI_TOKEN=secret\n", encoding="utf-8")
            update_env(path, {"PORT": "9000", "API_TOKEN": MASK, "NEW_VALUE": "yes"})
            result = path.read_text(encoding="utf-8")
            self.assertIn("# comment", result)
            self.assertIn("PORT=9000", result)
            self.assertIn("API_TOKEN=secret", result)
            self.assertIn("NEW_VALUE=yes", result)
            self.assertTrue(list(path.parent.glob(".env.prod.*.bak")))

    def test_update_rejects_invalid_keys_and_newlines(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / ".env.prod"
            with self.assertRaises(ValueError):
                update_env(path, {"bad-key": "value"})
            with self.assertRaises(ValueError):
                update_env(path, {"VALID_KEY": "first\nsecond"})


if __name__ == "__main__":
    unittest.main()
