from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "src" / "nonebot_plugin_mimo_console" / "log_buffer.py"
spec = importlib.util.spec_from_file_location("mimo_console_log_buffer_test", MODULE_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError("cannot load log_buffer module")
log_buffer_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = log_buffer_module
spec.loader.exec_module(log_buffer_module)
LogBuffer = log_buffer_module.LogBuffer


def make_message(content: str) -> SimpleNamespace:
    return SimpleNamespace(
        record={
            "time": datetime.now(),
            "level": SimpleNamespace(name="INFO"),
            "message": content,
            "name": "uvicorn",
        }
    )


class LogBufferTests(unittest.TestCase):
    def test_ignored_http_polling_does_not_fill_logs(self) -> None:
        logs = LogBuffer(ignored_fragments=("/console/api/logs",))
        logs.sink(make_message('GET /console/api/logs?after=0 HTTP/1.1" 200'))
        logs.sink(make_message("real application log"))

        items = logs.list()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["message"], "real application log")


if __name__ == "__main__":
    unittest.main()
