from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "src" / "nonebot_plugin_mimo_console" / "background.py"
spec = importlib.util.spec_from_file_location("mimo_console_background_test", MODULE_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError("cannot load background module")
background = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = background
spec.loader.exec_module(background)

BackgroundError = background.BackgroundError
BackgroundStore = background.BackgroundStore
normalize_background_url = background.normalize_background_url
build_upload_filename = background.build_upload_filename
is_safe_upload_filename = background.is_safe_upload_filename


class UrlValidationTests(unittest.TestCase):
    def test_accepts_http_and_https(self) -> None:
        self.assertEqual(
            normalize_background_url("https://example.com/a.jpg"),
            "https://example.com/a.jpg",
        )
        self.assertEqual(
            normalize_background_url("http://example.com/b.png"),
            "http://example.com/b.png",
        )

    def test_rejects_non_http_schemes(self) -> None:
        for value in (
            "javascript:alert(1)",
            "ftp://example.com/a",
            "file:///etc/passwd",
            "data:image/png;base64,AAAA",
        ):
            with self.subTest(value=value), self.assertRaises(BackgroundError):
                normalize_background_url(value)

    def test_rejects_empty_and_missing_host(self) -> None:
        for value in ("", "   ", "https://", "https:///path"):
            with self.subTest(value=value), self.assertRaises(BackgroundError):
                normalize_background_url(value)

    def test_rejects_css_injection_chars(self) -> None:
        # 引号与反斜杠会破坏 CSS `url("...")` 上下文，必须拒绝
        with self.assertRaises(BackgroundError):
            normalize_background_url('https://example.com/a").x{background:red}')
        with self.assertRaises(BackgroundError):
            normalize_background_url("https://example.com/a\\")
        with self.assertRaises(BackgroundError):
            normalize_background_url("https://example.com/way-too-long" + "x" * 2200)

    def test_strips_whitespace(self) -> None:
        self.assertEqual(
            normalize_background_url("  https://example.com/a.jpg  "),
            "https://example.com/a.jpg",
        )


class UploadFilenameTests(unittest.TestCase):
    def test_uses_extension_from_name(self) -> None:
        name = build_upload_filename("photo.JPG", "image/jpeg")
        self.assertTrue(name.endswith(".jpg"))
        self.assertTrue(is_safe_upload_filename(name))

    def test_falls_back_to_mime(self) -> None:
        name = build_upload_filename("noext", "image/png")
        self.assertTrue(name.endswith(".png"))
        self.assertTrue(is_safe_upload_filename(name))

    def test_rejects_unknown_type(self) -> None:
        for original, mime in (
            ("a.exe", "application/octet-stream"),
            ("a.bmp", "image/bmp"),
            ("a", ""),
        ):
            with self.subTest(original=original), self.assertRaises(BackgroundError):
                build_upload_filename(original, mime)

    def test_generated_names_are_unique(self) -> None:
        names = {build_upload_filename("a.png", "image/png") for _ in range(20)}
        self.assertEqual(len(names), 20)


class BackgroundStoreTests(unittest.TestCase):
    def test_default_url_is_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = BackgroundStore(
                Path(temp) / "bg.json",
                Path(temp) / "imgs",
                default_url="https://example.com/x.jpg",
            )
            self.assertEqual(store.default_url, "https://example.com/x.jpg")

    def test_invalid_default_url_is_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = BackgroundStore(
                Path(temp) / "bg.json",
                Path(temp) / "imgs",
                default_url="javascript:alert(1)",
            )
            self.assertEqual(store.default_url, "")

    def test_set_url_persists_across_reload(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_file = Path(temp) / "bg.json"
            image_dir = Path(temp) / "imgs"
            store = BackgroundStore(data_file, image_dir)
            snap = store.set_url("https://example.com/wall.jpg")
            self.assertEqual(snap["type"], "url")
            self.assertEqual(snap["url"], "https://example.com/wall.jpg")
            reloaded = BackgroundStore(data_file, image_dir)
            self.assertEqual(reloaded.snapshot()["type"], "url")

    def test_set_upload_writes_file_and_cleans_previous(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            image_dir = Path(temp) / "imgs"
            store = BackgroundStore(Path(temp) / "bg.json", image_dir)
            first = store.set_upload("a.png", "image/png", b"\x89PNG\r\n\x1a\n" + b"data")
            first_file = image_dir / first["filename"]
            self.assertTrue(first_file.is_file())
            self.assertEqual(first["type"], "upload")
            second = store.set_upload("b.jpg", "image/jpeg", b"\xff\xd8\xff\xe0data")
            self.assertFalse(first_file.exists())
            self.assertTrue((image_dir / second["filename"]).is_file())

    def test_set_upload_rejects_oversize_and_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = BackgroundStore(Path(temp) / "bg.json", Path(temp) / "imgs")
            with self.assertRaises(BackgroundError):
                store.set_upload("a.png", "image/png", b"")
            with self.assertRaises(BackgroundError):
                store.set_upload(
                    "a.png",
                    "image/png",
                    b"x" * (background.MAX_BACKGROUND_BYTES + 1),
                )

    def test_set_upload_rejects_bad_type(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = BackgroundStore(Path(temp) / "bg.json", Path(temp) / "imgs")
            with self.assertRaises(BackgroundError):
                store.set_upload("a.exe", "application/octet-stream", b"x" * 16)

    def test_clear_removes_upload_and_resets(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            image_dir = Path(temp) / "imgs"
            store = BackgroundStore(Path(temp) / "bg.json", image_dir)
            snap = store.set_upload("a.png", "image/png", b"data")
            uploaded = image_dir / snap["filename"]
            self.assertTrue(uploaded.is_file())
            cleared = store.clear()
            self.assertEqual(cleared["type"], "none")
            self.assertFalse(uploaded.exists())

    def test_switching_to_url_removes_upload(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            image_dir = Path(temp) / "imgs"
            store = BackgroundStore(Path(temp) / "bg.json", image_dir)
            snap = store.set_upload("a.png", "image/png", b"data")
            uploaded = image_dir / snap["filename"]
            store.set_url("https://example.com/x.jpg")
            self.assertFalse(uploaded.exists())
            self.assertEqual(store.snapshot()["type"], "url")

    def test_corrupted_json_falls_back_to_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_file = Path(temp) / "bg.json"
            data_file.write_text("{ not json", encoding="utf-8")
            store = BackgroundStore(data_file, Path(temp) / "imgs")
            self.assertEqual(store.snapshot()["type"], "none")

    def test_tampered_filename_is_downgraded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_file = Path(temp) / "bg.json"
            data_file.write_text(
                json.dumps({"type": "upload", "filename": "../etc/passwd.png"}),
                encoding="utf-8",
            )
            store = BackgroundStore(data_file, Path(temp) / "imgs")
            self.assertEqual(store.snapshot()["type"], "none")
            self.assertEqual(store.snapshot()["filename"], "")


class ResolveFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.image_dir = Path(self._temp.name) / "imgs"
        self.store = BackgroundStore(Path(self._temp.name) / "bg.json", self.image_dir)
        self.snap = self.store.set_upload("a.png", "image/png", b"\x89PNGdata")
        self.filename = self.snap["filename"]

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_resolves_valid_filename(self) -> None:
        path = self.store.resolve_file(self.filename)
        self.assertTrue(path.is_file())
        self.assertEqual(path.parent, self.image_dir.resolve())

    def test_rejects_traversal(self) -> None:
        for bad in ("../etc/passwd.png", "a/b.png", "/etc/passwd.png", "sub.dir/x.png"):
            with self.subTest(bad=bad), self.assertRaises(BackgroundError):
                self.store.resolve_file(bad)

    def test_rejects_unknown_filename(self) -> None:
        with self.assertRaises(BackgroundError):
            self.store.resolve_file("nonexistent-aaaaaaaaaaaaaaaa.png")


if __name__ == "__main__":
    unittest.main()
