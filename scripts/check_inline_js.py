"""渲染后内联 JavaScript 的可执行性检查。

背景见 INCIDENT_2026-06-06_READER_JS_HIGHLIGHT_REGRESSION.md：
阅读器模板新增的 `const highlightText` 与既有 `function highlightText()` 重名，
触发 `Identifier '...' has already been declared` 语法错误，导致整段内联脚本
在解析阶段就失败，前端交互全部瘫痪。后端 200、模板 Jinja 语法正确，都无法发现它。

本模块提供一个**零依赖**的检测器，专门捕获这一类必然导致语法错误的回归：
同一脚本顶层作用域内、同名的「词法声明」冲突（const / let / class 与任何其它
同名声明并存）。这类冲突在任何 JavaScript 引擎里都是 SyntaxError。

如果运行环境恰好装了 Node.js，会额外用 `node --check` 做一次完整语法校验作为
加分项；但不依赖它，确保在没有 Node 的服务器/桌面打包环境里也始终有这道防线。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

# 显式安全开关：在 node 可执行但本机环境异常（权限/沙箱）时，可设此环境变量
# 跳过 node --check。零依赖的词法重声明检测始终运行，核心防线不受影响。
_SKIP_NODE_ENV_VARS = ("MARX_SKIP_NODE_CHECK", "SKIP_NODE_JS_CHECK")

# 顶层声明关键字。
_DECL_KEYWORDS = {"const", "let", "var", "function", "class"}
# 词法绑定关键字：与同名的任何其它声明并存即为 SyntaxError。
_LEXICAL_KEYWORDS = {"const", "let", "class"}
# 只有在「语句起始位置」出现的声明关键字才算真正的顶层声明，
# 借此排除 `const x = function foo(){}` 里的函数表达式名、`for (const x of ...)`
# 里的循环变量等并非顶层绑定的情况。
_STATEMENT_STARTERS = {";", "{", "}"}
_STATEMENT_STARTER_WORDS = {"export", "default"}
# `/` 之前出现这些标记时，`/` 应解释为正则字面量的开始而非除号。
_REGEX_PREFIX_PUNCT = set("(,=:[!&|?{};+-*/%<>~^")
_REGEX_PREFIX_WORDS = {
    "return", "typeof", "instanceof", "in", "of", "new", "delete", "void",
    "do", "else", "case", "yield", "await", "throw",
}

_INLINE_SCRIPT_RE = re.compile(
    r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
_IDENT_START = re.compile(r"[A-Za-z_$]")
_IDENT_PART = re.compile(r"[A-Za-z0-9_$]")


def extract_inline_scripts(html: str) -> list[str]:
    """抽取 HTML 中所有没有 src 属性的内联 <script> 文本。"""
    return [match.group(1) for match in _INLINE_SCRIPT_RE.finditer(html)]


def _skip_string(s: str, i: int, quote: str) -> int:
    n = len(s)
    i += 1
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == quote:
            return i + 1
        if c == "\n":  # 未闭合，止损返回
            return i + 1
        i += 1
    return n


def _skip_template(s: str, i: int) -> int:
    """跳过模板字符串（含 ${...} 替换，可嵌套字符串/模板）。"""
    n = len(s)
    i += 1
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "`":
            return i + 1
        if c == "$" and i + 1 < n and s[i + 1] == "{":
            i = _skip_substitution(s, i + 2)
            continue
        i += 1
    return n


def _skip_substitution(s: str, i: int) -> int:
    """从 ${ 之后开始，跳过到与之匹配的 } 之后。"""
    n = len(s)
    depth = 1
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c in "'\"":
            i = _skip_string(s, i, c)
            continue
        if c == "`":
            i = _skip_template(s, i)
            continue
        if c == "/" and i + 1 < n and s[i + 1] == "/":
            i = _skip_line_comment(s, i)
            continue
        if c == "/" and i + 1 < n and s[i + 1] == "*":
            i = _skip_block_comment(s, i)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _skip_line_comment(s: str, i: int) -> int:
    n = len(s)
    while i < n and s[i] != "\n":
        i += 1
    return i


def _skip_block_comment(s: str, i: int) -> int:
    n = len(s)
    i += 2
    while i < n:
        if s[i] == "*" and i + 1 < n and s[i + 1] == "/":
            return i + 2
        i += 1
    return n


def _skip_regex(s: str, i: int) -> int:
    n = len(s)
    i += 1
    in_class = False
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "[":
            in_class = True
        elif c == "]":
            in_class = False
        elif c == "/" and not in_class:
            i += 1
            while i < n and _IDENT_PART.match(s[i]):  # 跳过 flags
                i += 1
            return i
        elif c == "\n":  # 未闭合，止损
            return i
        i += 1
    return n


def find_duplicate_lexical_declarations(js: str) -> list[tuple[str, list[str]]]:
    """返回顶层作用域里发生词法重声明冲突的 (名字, [声明类型...])。

    捕获的是必然报 SyntaxError 的冲突，例如 const + function 同名（本次事故），
    或 let/const/class 之间、与 var 同名等。仅 var+var / var+function / function+
    function 这类合法重复不会被报告。
    """
    n = len(js)
    i = 0
    depth = 0  # 仅统计 {} 嵌套；() [] 不开新声明作用域
    prev = None  # 上一个有意义的标记（词或单字符），用于正则/语句判定
    declarations: dict[str, list[str]] = {}

    while i < n:
        c = js[i]

        if c in " \t\r\n":
            i += 1
            continue
        if c == "/" and i + 1 < n and js[i + 1] == "/":
            i = _skip_line_comment(js, i)
            continue
        if c == "/" and i + 1 < n and js[i + 1] == "*":
            i = _skip_block_comment(js, i)
            continue
        if c == "/":
            regex_ok = prev is None or prev in _REGEX_PREFIX_PUNCT or prev in _REGEX_PREFIX_WORDS
            if regex_ok:
                i = _skip_regex(js, i)
                prev = ")"  # 正则是表达式结尾
                continue
            prev = "/"
            i += 1
            continue
        if c in "'\"":
            i = _skip_string(js, i, c)
            prev = ")"
            continue
        if c == "`":
            i = _skip_template(js, i)
            prev = ")"
            continue
        if c == "{":
            depth += 1
            prev = "{"
            i += 1
            continue
        if c == "}":
            depth -= 1
            prev = "}"
            i += 1
            continue
        if _IDENT_START.match(c):
            start = i
            i += 1
            while i < n and _IDENT_PART.match(js[i]):
                i += 1
            word = js[start:i]
            statement_position = prev is None or prev in _STATEMENT_STARTERS or prev in _STATEMENT_STARTER_WORDS
            if depth == 0 and word in _DECL_KEYWORDS and statement_position:
                name = _read_following_identifier(js, i)
                if name is not None:
                    declarations.setdefault(name, []).append(word)
            prev = word
            continue
        if c.isdigit():
            i += 1
            while i < n and _IDENT_PART.match(js[i]):
                i += 1
            prev = "0"
            continue
        prev = c
        i += 1

    conflicts: list[tuple[str, list[str]]] = []
    for name, kinds in declarations.items():
        if len(kinds) >= 2 and any(k in _LEXICAL_KEYWORDS for k in kinds):
            conflicts.append((name, kinds))
    return conflicts


def _read_following_identifier(s: str, i: int) -> str | None:
    """跳过空白/注释后读取下一个标识符（声明的名字）。"""
    n = len(s)
    while i < n:
        c = s[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c == "/" and i + 1 < n and s[i + 1] == "/":
            i = _skip_line_comment(s, i)
            continue
        if c == "/" and i + 1 < n and s[i + 1] == "*":
            i = _skip_block_comment(s, i)
            continue
        break
    if i < n and _IDENT_START.match(s[i]):
        start = i
        i += 1
        while i < n and _IDENT_PART.match(s[i]):
            i += 1
        return s[start:i]
    return None


def _node_check_skipped() -> bool:
    return any((os.environ.get(name) or "").strip() not in ("", "0", "false", "False") for name in _SKIP_NODE_ENV_VARS)


def node_check_available() -> bool:
    if _node_check_skipped():
        return False
    return shutil.which("node") is not None


def run_node_check(js: str) -> str | None:
    """有 Node 时用 `node --check` 做完整语法校验，返回错误信息或 None。

    仅当 node 真正报告语法错误（returncode != 0 且有诊断输出）时返回错误。
    若 node 因本机环境（权限/沙箱）根本无法执行，视为环境问题、返回 None 不阻断
    ——零依赖的词法重声明检测始终运行，核心防线不受影响。
    """
    if not node_check_available():
        return None
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
        handle.write(js)
        temp_path = handle.name
    try:
        result = subprocess.run(
            ["node", "--check", temp_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return (result.stderr or result.stdout or "node --check failed").strip()
        return None
    except Exception:  # pragma: no cover - 环境异常时不阻断，留给词法检测兜底
        return None
    finally:
        try:
            Path(temp_path).unlink()
        except OSError:
            pass


def check_html(label: str, html: str) -> list[str]:
    """检查一段渲染后的 HTML，返回问题列表（空列表表示通过）。"""
    problems: list[str] = []
    scripts = extract_inline_scripts(html)
    for index, script in enumerate(scripts):
        location = f"{label} 第 {index + 1} 段内联脚本"
        for name, kinds in find_duplicate_lexical_declarations(script):
            problems.append(
                f"{location}：标识符 '{name}' 在顶层被重复声明（{' + '.join(kinds)}），"
                f"会触发 JavaScript SyntaxError，整段脚本将无法执行。"
            )
        node_error = run_node_check(script)
        if node_error:
            problems.append(f"{location}：node --check 报告语法错误：{node_error}")
    return problems
