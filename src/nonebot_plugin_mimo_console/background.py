from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import secrets
import socket
from contextlib import suppress
from pathlib import Path
from threading import RLock
from typing import Any, Literal
from urllib.parse import urlparse

import httpx

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
    """校验远程背景 URL：必须是 http/https 且不含危险字符。"""
    text = (url or "").strip()
    if not text:
        raise BackgroundError("背景 URL 不能为空")
    if len(text) > URL_MAX_LENGTH:
        raise BackgroundError("背景 URL 过长")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise BackgroundError("背景 URL 含有不被允许的控制字符")
    if any(ch in text for ch in ('"', "\\", "<", ">")):
        raise BackgroundError("背景 URL 含有不被允许的字符")
    try:
        parsed = urlparse(text)
    except ValueError as exc:
        raise BackgroundError("背景 URL 格式不正确") from exc
    if parsed.scheme not in {"http", "https"}:
        raise BackgroundError("背景 URL 必须以 http 或 https 开头")
    if not parsed.hostname:
        raise BackgroundError("背景 URL 缺少主机名")
    if parsed.username or parsed.password:
        raise BackgroundError("背景 URL 不能包含用户名或密码")
    try:
        port = parsed.port
    except ValueError as exc:
        raise BackgroundError("背景 URL 端口不合法") from exc
    if port not in {None, 80, 443}:
        raise BackgroundError("背景 URL 仅支持 80 或 443 端口")
    return text


async def _ensure_public_host(url: str) -> list[str]:
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    try:
        addresses = await asyncio.to_thread(
            socket.getaddrinfo,
            hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise BackgroundError("无法解析背景图片主机") from exc
    if not addresses:
        raise BackgroundError("无法解析背景图片主机")
    resolved: list[str] = []
    for item in addresses:
        ip = str(item[4][0])
        address = ipaddress.ip_address(ip)
        if not address.is_global:
            raise BackgroundError("背景图片地址不能指向本机或内网")
        resolved.append(ip)
    # Return the exact IPs we validated so the connection can be pinned to them,
    # closing the DNS-rebinding window between validation and httpx's own lookup.
    return resolved


async def _download_candidate(
    client: httpx.AsyncClient,
    current: str,
    pinned_ip: str | None,
) -> tuple[str | None, tuple[str, str, bytes] | None]:
    request_url = current
    headers = {"User-Agent": "nonebot-plugin-mimo-console"}
    extensions: dict[str, object] = {}
    if pinned_ip is not None:
        parsed = urlparse(current)
        host_header = parsed.hostname or ""
        if parsed.port:
            host_header = f"{host_header}:{parsed.port}"
        headers["Host"] = host_header
        extensions["sni_hostname"] = parsed.hostname or ""
        netloc = f"[{pinned_ip}]" if ":" in pinned_ip else pinned_ip
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        request_url = parsed._replace(netloc=netloc).geturl()

    async with client.stream(
        "GET",
        request_url,
        headers=headers,
        extensions=extensions or None,
    ) as response:
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("location", "")
            if not location:
                raise BackgroundError("背景图片重定向缺少目标地址")
            # Resolve against the hostname URL, not the IP-pinned request URL,
            # so relative redirects preserve the validated hostname.
            return normalize_background_url(str(httpx.URL(current).join(location))), None
        if response.status_code != 200:
            raise BackgroundError(f"下载背景图片失败（HTTP {response.status_code}）")
        content_type = response.headers.get("content-type", "")
        mime = content_type.split(";", 1)[0].strip().lower()
        if mime not in _MIME_TO_EXT:
            raise BackgroundError("远程地址返回的不是支持的图片格式")
        try:
            declared_size = int(response.headers.get("content-length", "0"))
        except ValueError:
            declared_size = 0
        if declared_size > MAX_BACKGROUND_BYTES:
            raise BackgroundError("图片大小不能超过 5MB")
        data = bytearray()
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > MAX_BACKGROUND_BYTES:
                raise BackgroundError("图片大小不能超过 5MB")
        if not data:
            raise BackgroundError("下载的背景图片为空")
        filename = Path(urlparse(current).path).name or f"background{_MIME_TO_EXT[mime]}"
        return None, (filename, mime, bytes(data))


async def download_background(
    url: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[str, str, bytes]:
    """受限下载远程背景，逐次校验重定向目标并限制响应体大小。"""
    current = normalize_background_url(url)
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(20, connect=10),
        trust_env=False,
        transport=transport,
    ) as client:
        for _ in range(6):
            # Resolve + validate, then try every validated address. This preserves
            # DNS-rebinding protection without losing IPv6/IPv4 fallback.
            validated_ips = await _ensure_public_host(current)
            candidates: list[str | None] = (
                list(dict.fromkeys(validated_ips)) if transport is None else [None]
            )
            last_error: httpx.HTTPError | None = None
            redirected = False
            for pinned_ip in candidates:
                try:
                    redirect, result = await _download_candidate(client, current, pinned_ip)
                except httpx.HTTPError as exc:
                    last_error = exc
                    continue
                if redirect is not None:
                    current = redirect
                    redirected = True
                    break
                if result is not None:
                    return result
            if redirected:
                continue
            if last_error is not None:
                raise BackgroundError(f"下载背景图片失败：{last_error}") from last_error
            raise BackgroundError("背景图片主机没有可用的公网地址")
    raise BackgroundError("背景图片重定向次数过多")


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
    ) -> None:
        self.data_file = data_file
        self.image_dir = image_dir
        self._lock = RLock()
        self._data = self._read()

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
            if bg_type == "url" and filename and not is_safe_upload_filename(filename):
                filename = ""
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

    def set_remote_download(
        self,
        url: str,
        original_name: str,
        content_type: str,
        data: bytes,
    ) -> dict[str, Any]:
        safe_url = normalize_background_url(url)
        if not data:
            raise BackgroundError("下载的背景图片为空")
        if len(data) > MAX_BACKGROUND_BYTES:
            raise BackgroundError("图片大小不能超过 5MB")
        filename = build_upload_filename(original_name, content_type)
        self.image_dir.mkdir(parents=True, exist_ok=True)
        (self.image_dir / filename).write_bytes(data)
        with self._lock:
            self._delete_upload_locked()
            self._data = {
                "type": "url",
                "url": safe_url,
                "filename": filename,
            }
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
