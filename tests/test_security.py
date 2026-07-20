from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "src" / "nonebot_plugin_mimo_console" / "security.py"
spec = importlib.util.spec_from_file_location("mimo_console_security_test", MODULE_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError("cannot load security module")
security = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = security
spec.loader.exec_module(security)
AuthError = security.AuthError
AuthStore = security.AuthStore
validate_password = security.validate_password


class SecurityTests(unittest.TestCase):
    def test_password_policy(self) -> None:
        for password in ("short", "lowercase1@", "UPPERCASE1@", "NoNumber@", "NoSpecial1"):
            with self.subTest(password=password), self.assertRaises(AuthError):
                validate_password(password)
        validate_password("StrongPass1@")

    def test_setup_login_verify_and_logout(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = AuthStore(Path(temp) / "auth.json", session_hours=1)
            setup_token = store.issue_setup_token()
            self.assertIsNotNone(setup_token)
            session_token = store.setup(str(setup_token), "admin", "StrongPass1@")
            self.assertTrue(store.configured)
            self.assertEqual(store.verify(session_token).username, "admin")  # type: ignore[union-attr]
            login_token = store.login("admin", "StrongPass1@")
            self.assertIsNotNone(store.verify(login_token))
            store.logout(login_token)
            self.assertIsNone(store.verify(login_token))

    def test_wrong_setup_token_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = AuthStore(Path(temp) / "auth.json")
            store.issue_setup_token()
            with self.assertRaises(AuthError):
                store.setup("wrong-token", "admin", "StrongPass1@")


if __name__ == "__main__":
    unittest.main()
