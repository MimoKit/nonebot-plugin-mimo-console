from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "src" / "nonebot_plugin_mimo_console" / "version.py"
spec = importlib.util.spec_from_file_location("mimo_console_version_test", MODULE_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError("cannot load version module")
version = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = version
spec.loader.exec_module(version)

normalize_tag = version.normalize_tag
is_newer = version.is_newer
get_installed_version = version.get_installed_version
LatestReleaseCache = version.LatestReleaseCache
normalize_github_proxy = version.normalize_github_proxy
is_mirror_repo = version.is_mirror_repo
resolve_git_url = version.resolve_git_url
resolve_version_url = version.resolve_version_url


class NormalizeTagTests(unittest.TestCase):
    def test_strips_v_prefix(self) -> None:
        self.assertEqual(normalize_tag("v0.2.0"), "0.2.0")

    def test_accepts_plain_version(self) -> None:
        self.assertEqual(normalize_tag("1.2.3"), "1.2.3")

    def test_accepts_prerelease(self) -> None:
        self.assertEqual(normalize_tag("v1.0.0rc1"), "1.0.0rc1")
        self.assertEqual(normalize_tag("v0.1.0a2"), "0.1.0a2")

    def test_rejects_garbage(self) -> None:
        for value in ("", "abc", "v", "1.2", "latest", "v1.2"):
            with self.subTest(value=value):
                self.assertEqual(normalize_tag(value), "")


class IsNewerTests(unittest.TestCase):
    def test_patch_increment_is_newer(self) -> None:
        self.assertTrue(is_newer("0.1.1", "0.1.0"))

    def test_minor_increment_is_newer(self) -> None:
        self.assertTrue(is_newer("0.2.0", "0.1.9"))

    def test_major_increment_is_newer(self) -> None:
        self.assertTrue(is_newer("1.0.0", "0.9.9"))

    def test_same_version_not_newer(self) -> None:
        self.assertFalse(is_newer("0.1.0", "0.1.0"))

    def test_older_not_newer(self) -> None:
        self.assertFalse(is_newer("0.0.9", "0.1.0"))

    def test_empty_returns_false(self) -> None:
        self.assertFalse(is_newer("", "0.1.0"))
        self.assertFalse(is_newer("0.1.0", ""))

    def test_prerelease_is_lower_than_release(self) -> None:
        self.assertFalse(is_newer("1.0.0a1", "1.0.0"))
        self.assertTrue(is_newer("1.0.0", "1.0.0a1"))


class GetInstalledVersionTests(unittest.TestCase):
    def test_returns_string(self) -> None:
        self.assertIsInstance(get_installed_version(), str)


class LatestReleaseCacheSnapshotTests(unittest.TestCase):
    def _cache(self, latest: str) -> LatestReleaseCache:
        cache = LatestReleaseCache()
        cache._latest = latest
        return cache

    def test_snapshot_marks_update_when_latest_newer(self) -> None:
        snap = self._cache("0.2.0").snapshot("0.1.0")
        self.assertEqual(snap["current"], "0.1.0")
        self.assertEqual(snap["latest"], "0.2.0")
        self.assertTrue(snap["has_update"])

    def test_snapshot_empty_when_no_latest(self) -> None:
        snap = self._cache("").snapshot("0.1.0")
        self.assertEqual(snap["latest"], "")
        self.assertFalse(snap["has_update"])

    def test_snapshot_no_update_on_same_version(self) -> None:
        snap = self._cache("0.1.0").snapshot("0.1.0")
        self.assertFalse(snap["has_update"])

    def test_snapshot_no_update_when_installed_unknown(self) -> None:
        snap = self._cache("0.2.0").snapshot("")
        self.assertFalse(snap["has_update"])


class GithubProxyTests(unittest.TestCase):
    def test_normalize_empty_means_direct(self) -> None:
        self.assertEqual(normalize_github_proxy(""), "")
        self.assertEqual(normalize_github_proxy("   "), "")

    def test_normalize_strips_trailing_slash(self) -> None:
        self.assertEqual(normalize_github_proxy("https://gh-proxy.com/"), "https://gh-proxy.com")

    def test_normalize_rejects_invalid(self) -> None:
        for value in ("ftp://x", "https://a b", "https://user@host", "not-a-url", "https://x/#f"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_github_proxy(value)

    def test_is_mirror_repo(self) -> None:
        self.assertTrue(is_mirror_repo(version.CNB_MIRROR_REPO))
        self.assertTrue(is_mirror_repo(f"{version.CNB_MIRROR_REPO}.git"))
        self.assertFalse(is_mirror_repo("https://gh-proxy.com"))

    def test_resolve_git_url_direct(self) -> None:
        self.assertEqual(resolve_git_url(""), version.PACKAGE_GIT_URL)

    def test_resolve_git_url_prefix(self) -> None:
        self.assertEqual(
            resolve_git_url("https://gh-proxy.com"),
            f"https://gh-proxy.com/{version.PACKAGE_GIT_URL}",
        )

    def test_resolve_git_url_mirror(self) -> None:
        self.assertEqual(resolve_git_url(version.CNB_MIRROR_REPO), version.CNB_MIRROR_REPO)

    def test_resolve_version_url_direct(self) -> None:
        self.assertEqual(resolve_version_url(""), version.MASTER_PYPROJECT_URL)

    def test_resolve_version_url_prefix(self) -> None:
        self.assertEqual(
            resolve_version_url("https://gh-proxy.com/"),
            f"https://gh-proxy.com/{version.MASTER_PYPROJECT_URL}",
        )

    def test_resolve_version_url_mirror(self) -> None:
        self.assertEqual(
            resolve_version_url(version.CNB_MIRROR_REPO),
            f"{version.CNB_MIRROR_REPO}/-/raw/master/pyproject.toml",
        )


if __name__ == "__main__":
    unittest.main()
