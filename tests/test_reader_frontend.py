"""阅读器前端可执行性回归测试。

针对 INCIDENT_2026-06-06_READER_JS_HIGHLIGHT_REGRESSION.md：阅读器内联脚本
因顶层重声明（`const highlightText` 与 `function highlightText()` 同名）整段
解析失败，而后端 200、Jinja 语法正确，常规检查都发现不了。

本测试落实复盘「后续改进」的前两条：
1. 渲染 /viewer 后抽取内联脚本，确认不存在必然报错的顶层重声明冲突；
2. 阅读器关键交互（翻页、目录跳转、书页图像加载）确实在脚本里完成了绑定，
   且 /page-image 在可渲染卷册上返回 JPEG。
"""

from __future__ import annotations

import atexit
import os
import re
import shutil
import sys
import tempfile
import unittest
import warnings
from pathlib import Path


warnings.filterwarnings("ignore", category=ResourceWarning)
_TMP_APPDATA = tempfile.mkdtemp(prefix="marx-search-reader-")
atexit.register(lambda: shutil.rmtree(_TMP_APPDATA, ignore_errors=True))
os.environ["APPDATA"] = _TMP_APPDATA
os.environ["APP_MODE"] = "server"
os.environ["PUBLIC_BASE_URL"] = "https://example.test"
os.environ["ZPAY_PID"] = "test-pid"
os.environ["ZPAY_KEY"] = "test-secret"
os.environ["TURNSTILE_ENABLED"] = "0"

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "scripts"))

import app as app_module  # noqa: E402
import check_inline_js  # noqa: E402
from membership import (  # noqa: E402
    create_manual_subscription,
    create_user,
    get_user_by_email,
    list_subscriptions_for_user,
)
from werkzeug.security import generate_password_hash  # noqa: E402


class ReaderFrontendTests(unittest.TestCase):
    def setUp(self) -> None:
        warnings.filterwarnings("ignore", category=ResourceWarning)
        app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        app_module.set_setting(
            "access_policy",
            {"audience": {"registered": {"search": True, "library": True, "ai": True}}},
        )
        app_module._rate_buckets.clear()
        self.client = app_module.app.test_client()
        self._login("reader-frontend@example.test")

    def _csrf_from(self, path: str) -> str:
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        match = re.search(r'name="csrf_token" value="([^"]+)"', html)
        if match is None:
            match = re.search(r"const csrfToken = \"([^\"]+)\";", html)
        self.assertIsNotNone(match)
        return match.group(1)

    def _login(self, email: str, password: str = "correct horse battery staple") -> None:
        user = get_user_by_email(email)
        if user is None:
            user = create_user(
                email=email,
                display_name=email.split("@", 1)[0],
                password_hash=generate_password_hash(password),
                email_verified_at="2026-01-01T00:00:00+00:00",
            )
        if not list_subscriptions_for_user(int(user["id"])):
            create_manual_subscription(user_email=email, plan_code="monthly", note="test")
        token = self._csrf_from("/login")
        response = self.client.post(
            "/login",
            data={"csrf_token": token, "email": email, "password": password},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)

    def _first_volume(self):
        for book in app_module.BOOK_CONFIG_BY_KEY:
            volumes = app_module.corpus.get_volumes(book)
            if volumes:
                return volumes[0]
        self.skipTest("语料库中没有可用卷册")

    def _render_viewer(self) -> str:
        volume = self._first_volume()
        response = self.client.get(
            f"/viewer?file={volume.source_file}&page=1&q=test&h=test&mode=ai"
        )
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def test_viewer_inline_script_has_no_duplicate_declarations(self) -> None:
        """渲染后的阅读器内联脚本不得包含必然报 SyntaxError 的顶层重声明。"""
        html = self._render_viewer()
        scripts = check_inline_js.extract_inline_scripts(html)
        self.assertTrue(scripts, "阅读器页面应至少含一段内联脚本")
        for script in scripts:
            conflicts = check_inline_js.find_duplicate_lexical_declarations(script)
            self.assertEqual(
                conflicts,
                [],
                msg=f"阅读器内联脚本存在顶层重声明冲突：{conflicts}",
            )
        # check_html 复用部署 smoke 的完整检查（含可用时的 node --check）。
        self.assertEqual(check_inline_js.check_html("/viewer", html), [])

    def test_checker_detects_the_original_collision(self) -> None:
        """守卫自证：能抓出本次事故那种 const 与 function 同名冲突。"""
        regressed = (
            "const highlightText = 'q';\n"
            "function highlightText(text, terms){ return text; }\n"
        )
        conflicts = check_inline_js.find_duplicate_lexical_declarations(regressed)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0][0], "highlightText")

    def test_viewer_wires_core_reader_interactions(self) -> None:
        """翻页、目录跳转、书页图像加载与高亮参数必须在脚本中完成绑定。"""
        html = self._render_viewer()
        for needle in (
            "prevBtn.addEventListener('click'",
            "nextBtn.addEventListener('click'",
            "tocList.addEventListener('click'",
            "pageImage.src = buildPageImageUrl(",
            "highlightQueryText",
        ):
            self.assertIn(needle, html, msg=f"阅读器脚本缺少关键绑定：{needle}")

    def test_page_image_returns_jpeg_when_renderable(self) -> None:
        """可渲染卷册的 /page-image 应返回 JPEG（书页图像不能是空白框）。"""
        renderable = None
        for book in app_module.BOOK_CONFIG_BY_KEY:
            for volume in app_module.corpus.get_volumes(book):
                if app_module._pdf_render_available(volume.source_file):
                    renderable = volume
                    break
            if renderable:
                break
        if renderable is None:
            self.skipTest("当前环境未随包提供可渲染的 PDF 卷册")
        response = self.client.get(f"/page-image?file={renderable.source_file}&page=1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/jpeg")
        self.assertGreater(len(response.get_data()), 0)


if __name__ == "__main__":
    unittest.main()
