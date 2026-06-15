#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把外部静态 HTML 书库（如朋友镜像的列宁 ПСС / VIL-UAIO）按卷搬进本项目的
static_library/<book>/ 下，并改写少量绝对资产引用，使其在自托管的 /wenku/raw/<book>
服务前缀下正确解析。

设计要点：
- 只做「读源→写目标新文件」，不在原地覆写已存在文件（Bash 沙箱里就地覆写会静默失效）。
- 重跑某卷前先删掉该卷目标目录再整卷写出，保证幂等。
- 仅改写两处绝对引用：`/vil.css`→`<prefix>/vil.css`；并删掉朋友的 `/mlr.js`
  （滚动记忆/表格包裹脚本，我们自带阅读器外壳，不需要它，留着只会按它自己的路径正则空跑）。
  其余相对链接（上一页/下一页/目录/图片/脚注 #锚点）天然随服务路径正确解析，无需改。

用法示例（在项目根目录）：
  python scripts/vendor_static_library.py \
      --source /d/_marxtmp/ru/VIL-UAIO \
      --css /d/_marxtmp/vil.css \
      --book lenin-ru \
      --serve-prefix /wenku/raw/lenin-ru \
      --volumes 1
  # 全量：把 --volumes 换成 --all
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEST_ROOT = PROJECT_ROOT / "static_library"

_MLR_SCRIPT_RE = re.compile(r"""<script[^>]*src=['"]/mlr\.js['"][^>]*>\s*</script>""", re.IGNORECASE)
# 朋友 MEGA 目录/正文里的链接带 target=_blank（引号/无引号三种写法），点击会在新标签页打开裸内容页、
# 脱离阅读器外壳（顶栏翻译/AI 导读/引文随之消失）。一律剥掉，让链接在同源 iframe 内导航。
_TARGET_BLANK_RE = re.compile(r"""\s+target\s*=\s*(?:"_blank"|'_blank'|_blank)""", re.IGNORECASE)


def rewrite_html(text: str, serve_prefix: str) -> str:
    serve_prefix = serve_prefix.rstrip("/")
    # /vil.css（绝对）→ <prefix>/vil.css。两种引号都覆盖。
    text = text.replace('href="/vil.css"', f'href="{serve_prefix}/vil.css"')
    text = text.replace("href='/vil.css'", f"href='{serve_prefix}/vil.css'")
    # 删掉朋友的 mlr.js
    text = _MLR_SCRIPT_RE.sub("", text)
    # 剥掉 target=_blank，保证链接留在阅读器 iframe 内（否则外壳与按钮消失）
    text = _TARGET_BLANK_RE.sub("", text)
    return text


def vendor_volume(source_vol: Path, dest_vol: Path, serve_prefix: str) -> tuple[int, int]:
    if dest_vol.exists():
        shutil.rmtree(dest_vol)
    dest_vol.mkdir(parents=True, exist_ok=True)
    n_html = n_other = 0
    for src in sorted(source_vol.rglob("*")):
        if src.is_dir():
            continue
        rel = src.relative_to(source_vol)
        dst = dest_vol / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.suffix.lower() in (".html", ".htm"):
            raw = src.read_text(encoding="utf-8", errors="replace")
            dst.write_text(rewrite_html(raw, serve_prefix), encoding="utf-8")
            n_html += 1
        else:
            shutil.copy2(src, dst)
            n_other += 1
    return n_html, n_other


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", required=True, help="源书库目录（含各卷子目录 1/2/3…）")
    ap.add_argument("--book", required=True, help="目标书 key，如 lenin-ru")
    ap.add_argument("--serve-prefix", required=True, help="该书的服务前缀，如 /wenku/raw/lenin-ru")
    ap.add_argument("--css", default="", help="可选：要一并搬入书根的样式表文件（如朋友的 vil.css）")
    ap.add_argument("--volumes", nargs="*", type=str, default=[], help="要搬的卷号列表，如 1 2 33")
    ap.add_argument("--all", action="store_true", help="搬源目录下全部数字命名的卷")
    args = ap.parse_args(argv)

    source = Path(args.source)
    if not source.is_dir():
        print(f"[错误] 源目录不存在：{source}", file=sys.stderr)
        return 2

    dest_book = DEST_ROOT / args.book
    dest_book.mkdir(parents=True, exist_ok=True)

    if args.all:
        vols = sorted((p.name for p in source.iterdir() if p.is_dir() and p.name.isdigit()), key=int)
    else:
        vols = args.volumes
    if not vols:
        print("[错误] 未指定卷：用 --volumes 1 2 … 或 --all", file=sys.stderr)
        return 2

    if args.css:
        css_src = Path(args.css)
        if css_src.is_file():
            (dest_book / css_src.name).write_text(
                css_src.read_text(encoding="utf-8", errors="replace"), encoding="utf-8"
            )
            print(f"[资产] {css_src.name} → {dest_book / css_src.name}")
        else:
            print(f"[警告] --css 文件不存在：{css_src}", file=sys.stderr)

    total_html = total_other = 0
    for vol in vols:
        sv = source / vol
        if not sv.is_dir():
            print(f"[跳过] 源中无第 {vol} 卷：{sv}", file=sys.stderr)
            continue
        nh, no = vendor_volume(sv, dest_book / vol, args.serve_prefix)
        total_html += nh
        total_other += no
        print(f"[卷 {vol}] HTML {nh} + 其他 {no} → {dest_book / vol}")

    print(f"[完成] 书={args.book} 共 HTML {total_html} + 其他 {total_other}，目标={dest_book}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
