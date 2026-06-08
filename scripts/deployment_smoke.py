from __future__ import annotations

import argparse
import os
import sys
import traceback
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_inline_js  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deployment smoke checks for the web app.")
    parser.add_argument(
        "--mode",
        choices=("desktop", "server"),
        default="server",
        help="Runtime mode used while importing the app.",
    )
    parser.add_argument(
        "--skip-http",
        action="store_true",
        help="Only parse templates and import the app; skip Flask test-client requests.",
    )
    return parser.parse_args()


def check_templates(root: Path) -> None:
    templates_dir = root / "templates"
    env = Environment(loader=FileSystemLoader(str(templates_dir)))
    for template in sorted(templates_dir.glob("*.html")):
        env.parse(template.read_text(encoding="utf-8"))


def check_inline_javascript(root: Path) -> None:
    """检查模板内联脚本是否存在必然报错的顶层重声明冲突。

    见 INCIDENT_2026-06-06_READER_JS_HIGHLIGHT_REGRESSION.md：阅读器内联脚本里
    `const highlightText` 与 `function highlightText()` 重名导致整段脚本无法解析，
    而 Jinja 语法检查与 HTTP 200 都发现不了。此处对所有模板做静态扫描兜底；
    渲染后的端到端校验（/viewer、/page-image、翻页与目录）见 tests/test_reader_frontend.py。
    """
    templates_dir = root / "templates"
    problems: list[str] = []
    for template in sorted(templates_dir.glob("*.html")):
        problems.extend(
            check_inline_js.check_html(template.name, template.read_text(encoding="utf-8"))
        )
    if problems:
        raise RuntimeError("内联 JavaScript 检查未通过：\n  - " + "\n  - ".join(problems))


def check_app_import_and_routes(root: Path, mode: str, skip_http: bool) -> None:
    os.environ["APP_MODE"] = mode
    sys.path.insert(0, str(root))

    import app as app_module  # noqa: PLC0415

    # server 模式下 PUBLIC_BASE_URL 若误配为 http:// 会让会话 cookie 静默以非 Secure 下发，
    # 形成「对外 https 但 cookie 可被降级嗅探」的隐蔽不一致。部署期就拦住。
    # 本地冒烟通常不设 PUBLIC_BASE_URL（base 为空时跳过，不误伤）。
    if mode == "server":
        base = (os.environ.get("PUBLIC_BASE_URL") or "").strip()
        if base and not base.startswith("https://"):
            raise RuntimeError(
                f"server 模式 PUBLIC_BASE_URL 必须以 https:// 开头（当前: {base!r}），否则会话 cookie 非 Secure"
            )

    if skip_http:
        return

    app_module.app.testing = True
    client = app_module.app.test_client()

    # 访客可渲染的页面：必须 200。模板运行期错误(未定义变量等)会在此暴露为 5xx。
    expected_200 = (
        "/",
        "/api/runtime",
        "/pricing",
        "/login",
        "/register",
        "/forgot-password",
    )
    for path in expected_200:
        response = client.get(path)
        if response.status_code != 200:
            raise RuntimeError(f"{path} returned {response.status_code}, expected 200")

    # 核心链路：只断言"路由已接好且未崩溃(<500)"，不耦合鉴权/会话/语料数据，
    # 避免把权限性的 3xx/4xx 误判为故障而错误阻断部署；真正的回归(导入错误、
    # 模板异常、未捕获异常 → 5xx)会被这里拦下。访客这些路由通常重定向到登录。
    no_server_error = ("/library", "/account", "/viewer")
    for path in no_server_error:
        response = client.get(path)
        if response.status_code >= 500:
            raise RuntimeError(f"{path} returned server error {response.status_code}")

    # 检索接口：POST 触达视图函数(访客预期 4xx 鉴权/CSRF)，但绝不应 5xx。
    search_resp = client.post("/api/search", json={"q": "测试", "book": "文集"})
    if search_resp.status_code >= 500:
        raise RuntimeError(
            f"/api/search returned server error {search_resp.status_code}"
        )


def main() -> int:
    args = parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        check_templates(root)
        check_inline_javascript(root)
        check_app_import_and_routes(root, args.mode, args.skip_http)
    except Exception:
        traceback.print_exc()
        return 1
    print(f"deployment smoke ok: mode={args.mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
