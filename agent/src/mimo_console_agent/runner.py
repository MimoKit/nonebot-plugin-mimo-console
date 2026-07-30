from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
URL_CREDENTIAL_RE = re.compile(r"(https?://)[^/@\s]+@")


class CommandError(RuntimeError):
    def __init__(self, message: str, output: str = "") -> None:
        super().__init__(message)
        self.output = output


def clean_output(raw: bytes, limit: int = 200_000) -> str:
    text = raw.decode("utf-8", errors="replace")
    text = ANSI_RE.sub("", text).replace("\r\n", "\n")
    text = URL_CREDENTIAL_RE.sub(r"\1***@", text)
    return text[-limit:]


async def run_command(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: int,
    env: Mapping[str, str] | None = None,
) -> str:
    merged_env = os.environ.copy()
    merged_env.update({"NO_COLOR": "1", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    if env:
        merged_env.update(env)
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        env=merged_env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise CommandError(f"命令超过 {timeout} 秒，已终止") from None
    cleaned = clean_output(output)
    if process.returncode != 0:
        raise CommandError(
            cleaned or f"命令执行失败（{process.returncode}）",
            cleaned,
        )
    return cleaned
