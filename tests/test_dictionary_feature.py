from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import unittest
import warnings
from pathlib import Path


warnings.filterwarnings("ignore", category=ResourceWarning)
_TMP_APPDATA = tempfile.mkdtemp(prefix="marx-search-dictionary-")
os.environ["APPDATA"] = _TMP_APPDATA
os.environ["APP_MODE"] = "server"
os.environ["PUBLIC_BASE_URL"] = "https://example.test"
os.environ["ZPAY_PID"] = "test-pid"
os.environ["ZPAY_KEY"] = "test-secret"
os.environ["TURNSTILE_ENABLED"] = "0"

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import app as app_module  # noqa: E402
from dictionary_store import dictionary_available, dictionary_groups, dictionary_suggest  # noqa: E402
from membership import create_user, get_user_by_email  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402


def tearDownModule() -> None:
    shutil.rmtree(_TMP_APPDATA, ignore_errors=True)


class DictionaryFeatureTests(unittest.TestCase):
    def setUp(self) -> None:
        if not dictionary_available():
            self.skipTest("data/dictionary.sqlite has not been built")
        app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        app_module.set_setting(
            "access_policy",
            {
                "audience": {
                    "guest": {"search": True, "dictionary": False},
                    "registered": {"search": True, "dictionary": True},
                },
                "global": {"search": True, "dictionary": True},
            },
        )
        self.client = app_module.app.test_client()

    def _login_registered(self) -> None:
        email = "dictionary-user@example.test"
        user = get_user_by_email(email)
        if user is None:
            user = create_user(
                email=email,
                display_name="dictionary-user",
                password_hash=generate_password_hash("correct horse battery staple"),
                email_verified_at="2026-01-01T00:00:00+00:00",
            )
        with self.client.session_transaction() as session:
            session["user_id"] = int(user["id"])

    def test_dictionary_permission_redirects_guest_and_allows_registered(self) -> None:
        guest_response = self.client.get("/dictionary")
        self.assertEqual(guest_response.status_code, 302)
        self.assertIn("/login", guest_response.headers.get("Location", ""))

        self._login_registered()
        response = self.client.get("/dictionary")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("马克思主义大辞典", html)
        self.assertIn("/api/dictionary/suggest", html)

    def test_homepage_contains_dictionary_card(self) -> None:
        self._login_registered()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("马克思主义大辞典", html)
        self.assertIn("/dictionary", html)
        self.assertIn("reader-dictionary", html)

    def test_dictionary_suggest_and_entry_page(self) -> None:
        self._login_registered()
        results = dictionary_suggest("资本")
        self.assertTrue(results)
        response = self.client.get("/api/dictionary/suggest?q=资本")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["results"])

        slug = data["results"][0]["slug"]
        entry_response = self.client.get(f"/dictionary/entry/{slug}")
        self.assertEqual(entry_response.status_code, 200)
        html = entry_response.get_data(as_text=True)
        self.assertIn("对应书籍页码引文", html)
        self.assertIn("徐光春主编：《马克思主义大辞典》", html)

    def test_dictionary_data_is_grouped_and_polished(self) -> None:
        groups = dictionary_groups()
        letters = {group["letter"] for group in groups}
        self.assertIn("Z", letters)
        self.assertGreater(sum(group["count"] for group in groups), 500)
        self._login_registered()
        response = self.client.get("/api/dictionary/suggest?q=矛盾")
        self.assertEqual(response.status_code, 200)
        first = response.get_json()["results"][0]
        entry_response = self.client.get(f"/dictionary/entry/{first['slug']}")
        html = entry_response.get_data(as_text=True)
        self.assertNotIn("�", html)
        self.assertNotRegex(html, r"⋯{2,}")
        self.assertNotRegex(html, r"词目拼音索引")


if __name__ == "__main__":
    unittest.main()
