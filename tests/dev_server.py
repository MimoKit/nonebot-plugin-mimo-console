"""本地视觉验收入口，不会进入发布 wheel。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import nonebot  # noqa: E402

nonebot.init(
    driver="~fastapi",
    host=os.environ.get("MIMO_DEV_HOST", "127.0.0.1"),
    port=int(os.environ.get("MIMO_DEV_PORT", "18794")),
    mimo_console_project_root=ROOT,
    localstore_use_cwd=True,
)
nonebot.load_plugin("nonebot_plugin_mimo_console")

if __name__ == "__main__":
    nonebot.run()
