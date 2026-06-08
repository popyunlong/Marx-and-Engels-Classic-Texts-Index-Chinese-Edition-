from __future__ import annotations

import unittest

from jinja2 import Environment

import site_content
from site_content import (
    AutoSiteTextExtension,
    auto_key_for,
    discover_auto_literals,
    inject_auto_site_text,
    render_auto_site_text,
    _process_template_source,
    TEMPLATE_DIR,
    _iter_template_paths,
)


class AutoSiteTextEngineTests(unittest.TestCase):
    def _render(self, source: str, name: str = "demo.html", overrides: dict[str, str] | None = None) -> str:
        env = Environment(autoescape=True, extensions=[AutoSiteTextExtension])
        env.globals["site_text_auto"] = lambda key, b64="": render_auto_site_text(key, b64, overrides or {})
        template = env.from_string(source)
        # from_string 不带模板名，preprocess(name=None) 不会跳过——正合测试需要。
        return template.render()

    def test_text_content_is_wrapped(self) -> None:
        rewritten = inject_auto_site_text("<button>进入全文阅读</button>", "demo.html")
        self.assertIn("site_text_auto(", rewritten)
        self.assertIn(auto_key_for("进入全文阅读"), rewritten)

    def test_no_override_is_byte_identical(self) -> None:
        source = (
            "<h1>经典文献完整目录</h1>\n"
            "<p>按卷浏览目录，点击任一条目即可进入全文阅读器。</p>\n"
            "<button>进入全文阅读</button>"
        )
        # 渲染后（无覆盖）必须与原文逐字节一致。
        self.assertEqual(self._render(source), source)

    def test_override_replaces_text(self) -> None:
        source = "<button>进入全文阅读</button>"
        key = auto_key_for("进入全文阅读")
        out = self._render(source, overrides={key: "立即阅读"})
        self.assertEqual(out, "<button>立即阅读</button>")

    def test_skips_attributes_scripts_styles_jinja(self) -> None:
        cases = [
            '<input placeholder="搜索篇目">',                      # 属性
            "<script>var t = '正在加载';</script>",                # 脚本
            "<style>/* 中文注释 */</style>",                       # 样式
            "<p>{{ site_text('index.search_title') }}</p>",       # 已有 site_text
            "<!-- 这是注释 -->",                                   # HTML 注释
            "{% if x %}条件{% endif %}",                           # Jinja 语句（'条件'在标签外是文本，应接入）
        ]
        for src in cases[:-1]:
            with self.subTest(src=src):
                self.assertNotIn("site_text_auto(", inject_auto_site_text(src, "demo.html"))

    def test_jinja_block_text_is_still_wrapped(self) -> None:
        # {% if %} 内、标签外的纯文本属于内容区，应被接入。
        rewritten = inject_auto_site_text("{% if x %}已开通{% endif %}", "demo.html")
        self.assertIn("site_text_auto(", rewritten)

    def test_console_template_is_skipped(self) -> None:
        src = "<h2>站点文案</h2>"
        self.assertEqual(inject_auto_site_text(src, "control.html"), src)
        self.assertEqual(inject_auto_site_text(src, "templates/control.html"), src)

    def test_injector_never_raises(self) -> None:
        # 即使给畸形输入也只回退原文，不抛异常。
        weird = "<<<{{{%%%中文<script"
        self.assertIsInstance(inject_auto_site_text(weird, "demo.html"), str)

    def test_all_real_templates_still_parse(self) -> None:
        env = Environment(autoescape=True)
        for path in _iter_template_paths(TEMPLATE_DIR):
            source = path.read_text(encoding="utf-8")
            rewritten = inject_auto_site_text(source, path.name)
            with self.subTest(template=path.name):
                # 注入后的源码必须仍是合法 Jinja（能解析）。
                env.parse(rewritten)

    def test_discovery_matches_injection_keys(self) -> None:
        literals = discover_auto_literals()
        self.assertTrue(literals, "应至少发现一条自动接入文字")
        for key, info in literals.items():
            self.assertTrue(key.startswith("auto."))
            self.assertEqual(key, auto_key_for(str(info["default"])))


if __name__ == "__main__":
    unittest.main()
