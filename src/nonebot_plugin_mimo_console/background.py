from __future__ import annotations

import json
import os
import re
import secrets
from contextlib import suppress
from pathlib import Path
from threading import RLock
from typing import Any, Literal
from urllib.parse import urlparse

BackgroundType = Literal["none", "url", "upload"]

# 上传图片允许的扩展名白名单。
ALLOWED_IMAGE_EXTS: frozenset[str] = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})
_MIME_TO_EXT: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
MAX_BACKGROUND_BYTES = 5 * 1024 * 1024
URL_MAX_LENGTH = 2048
# 存储文件名形如 <token>.<ext>，token 由 secrets 生成，正则同时承担防穿越职责。
_FILENAME_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}\.(?:jpg|jpeg|png|webp|gif)$")


class BackgroundError(ValueError):
    """自定义背景设置相关错误。"""


def normalize_background_url(url: str) -> str:
    """校验远程背景 URL：必须是 http/https 且不含会破坏 CSS url() 的字符。"""
    text = (url or "").strip()
    if not text:
        raise BackgroundError("背景 URL 不能为空")
    if len(text) > URL_MAX_LENGTH:
        raise BackgroundError("背景 URL 过长")
    # 在 CSS `url("...")` 上下文里，引号与反斜杠会闭合或转义出字符串，直接禁掉。
    if any(ch in text for ch in ('"', "\\")):
        raise BackgroundError("背景 URL 含有不被允许的字符")
    try:
        parsed = urlparse(text)
    except ValueError as exc:
        raise BackgroundError("背景 URL 格式不正确") from exc
    if parsed.scheme not in {"http", "https"}:
        raise BackgroundError("背景 URL 必须以 http 或 https 开头")
    if not parsed.hostname:
        raise BackgroundError("背景 URL 缺少主机名")
    return text


def build_upload_filename(original_name: str, content_type: str) -> str:
    """根据原始文件名或 MIME 推断扩展名，生成随机存储文件名。"""
    ext = Path(original_name or "").suffix.lower()
    if ext not in ALLOWED_IMAGE_EXTS:
        mime = (content_type or "").split(";")[0].strip().lower()
        ext = _MIME_TO_EXT.get(mime, "")
    if ext not in ALLOWED_IMAGE_EXTS:
        raise BackgroundError("仅支持 jpg/jpeg/png/webp/gif 图片")
    return f"{secrets.token_urlsafe(16)}{ext}"


def is_safe_upload_filename(filename: str) -> bool:
    return bool(_FILENAME_RE.fullmatch(filename))


class BackgroundStore:
    """自定义 WebUI 背景图：远程 URL 或本地上传，状态持久化到 localstore。"""

    def __init__(
        self,
        data_file: Path,
        image_dir: Path,
        default_url: str | None = None,
    ) -> None:
        self.data_file = data_file
        self.image_dir = image_dir
        self.default_url = self._safe_default(default_url)
        self._lock = RLock()
        self._data = self._read()

    @staticmethod
    def _safe_default(default_url: str | None) -> str:
        if not default_url:
            return ""
        try:
            return normalize_background_url(default_url)
        except BackgroundError:
            return ""

    def _read(self) -> dict[str, Any]:
        if not self.data_file.is_file():
            return {}
        try:
            value = json.loads(self.data_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _write(self) -> None:
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        temp = self.data_file.with_suffix(self.data_file.suffix + ".tmp")
        temp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with suppress(OSError):
            os.chmod(temp, 0o600)
        temp.replace(self.data_file)

    def snapshot(self) -> dict[str, Any]:
        """返回当前背景设置（不拼接最终 URL，由 API 层负责）。"""
        with self._lock:
            bg_type = str(self._data.get("type") or "none")
            if bg_type not in ("none", "url", "upload"):
                bg_type = "none"
            url = str(self._data.get("url") or "")
            filename = str(self._data.get("filename") or "")
            if bg_type == "url" and not url:
                bg_type = "none"
            if bg_type == "upload" and not is_safe_upload_filename(filename):
                bg_type = "none"
                filename = ""
            return {"type": bg_type, "url": url, "filename": filename}

    def set_url(self, url: str) -> dict[str, Any]:
        safe = normalize_background_url(url)
        with self._lock:
            self._delete_upload_locked()
            self._data = {"type": "url", "url": safe, "filename": ""}
            self._write()
            return self.snapshot()

    def set_upload(
        self,
        original_name: str,
        content_type: str,
        data: bytes,
    ) -> dict[str, Any]:
        if not data:
            raise BackgroundError("上传的图片为空")
        if len(data) > MAX_BACKGROUND_BYTES:
            raise BackgroundError("图片大小不能超过 5MB")
        filename = build_upload_filename(original_name, content_type)
        self.image_dir.mkdir(parents=True, exist_ok=True)
        (self.image_dir / filename).write_bytes(data)
        with self._lock:
            self._delete_upload_locked()
            self._data = {"type": "upload", "url": "", "filename": filename}
            self._write()
            return self.snapshot()

    def clear(self) -> dict[str, Any]:
        with self._lock:
            self._delete_upload_locked()
            self._data = {}
            self._write()
            return self.snapshot()

    def _delete_upload_locked(self) -> None:
        filename = str(self._data.get("filename") or "")
        if is_safe_upload_filename(filename):
            with suppress(OSError):
                (self.image_dir / filename).unlink(missing_ok=True)

    def resolve_file(self, filename: str) -> Path:
        """供文件下载路由使用：严格校验文件名并返回解析后的安全路径。"""
        if not is_safe_upload_filename(filename):
            raise BackgroundError("非法的背景图片文件名")
        base = self.image_dir.resolve()
        target = (base / filename).resolve()
        # base / filename 已受正则约束，这里再做一次路径穿越兜底。
        if target.parent != base:
            raise BackgroundError("非法的背景图片路径")
        if not target.is_file():
            raise BackgroundError("背景图片不存在")
        return target
