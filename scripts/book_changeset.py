# -*- coding: utf-8 -*-
"""整书库语料的「导出 → 定向注入」搬运工具（本地建库，服务器只做写入）。

为什么不在服务器上直接建库：corpus.sqlite 已 640MB+，整库上传不可行（既有约定）；
而在服务器上跑 build_scan_volumes + build_toc 要读全部 sidecar、且 build_toc 会
`Corpus.load_default()` 把整个语料载入内存——生产机 8GB、网站自身已占 2GB，为了几个
新书库去冒这个险不划算。改成：**本地把书建好，只把新增书库的 pages/toc_entries 行导出**
（一本 1.6 万页的书压缩后约几 MB），服务器侧只做 DELETE+INSERT，几秒完成、几乎不占 CPU。

导出的是「按 book 键整体替换」的快照，天然幂等：重复注入结果一致；注入前自动备份 DB。

用法：
  # 本地导出（可多本）
  python scripts/book_changeset.py export --book 陈云年谱 --book 邓小平年谱 --out data/_cs.jsonl.gz
  # 服务器侧注入
  python scripts/book_changeset.py inject --in data/_cs.jsonl.gz [--dry-run] [--no-backup]
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from build_index import DB_PATH  # noqa: E402

HASH_PATH = DB_PATH.with_suffix(DB_PATH.suffix + ".sha256")

PAGE_COLS = ["book", "volume", "source_file", "pdf_page", "printed_page",
             "raw_text", "normalized_text"]
TOC_COLS = ["book", "volume", "source_file", "title", "pdf_page", "printed_page",
            "level", "kind", "sort_order"]


def update_hash(db_path: Path) -> str:
    digest = hashlib.sha256()
    with db_path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    h = digest.hexdigest()
    # sha256 文件必须是 LF 结尾——服务器侧按字节比对，CRLF 会导致校验永远不过
    db_path.with_suffix(db_path.suffix + ".sha256").write_text(h + "\n", encoding="utf-8", newline="\n")
    return h


def cmd_export(args: argparse.Namespace) -> None:
    conn = sqlite3.connect(str(DB_PATH))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    n_pages = n_toc = 0
    with gzip.open(out, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "_meta": {"books": args.book, "exported_at": time.strftime("%F %T"),
                      "page_cols": PAGE_COLS, "toc_cols": TOC_COLS},
        }, ensure_ascii=False) + "\n")
        for book in args.book:
            pages = conn.execute(
                f"SELECT {','.join(PAGE_COLS)} FROM pages WHERE book=? ORDER BY volume, pdf_page",
                (book,)).fetchall()
            tocs = conn.execute(
                f"SELECT {','.join(TOC_COLS)} FROM toc_entries WHERE book=? ORDER BY volume, sort_order",
                (book,)).fetchall()
            if not pages:
                raise SystemExit(f"[{book}] 本地 pages 表里没有这本书，导出中止")
            fh.write(json.dumps({"book": book, "n_pages": len(pages), "n_toc": len(tocs)},
                                ensure_ascii=False) + "\n")
            for row in pages:
                fh.write(json.dumps(["P", *row], ensure_ascii=False) + "\n")
            for row in tocs:
                fh.write(json.dumps(["T", *row], ensure_ascii=False) + "\n")
            vols = sorted({r[1] for r in pages})
            nonempty = sum(1 for r in pages if (r[6] or "").strip())
            print(f"[{book}] {len(vols)} 卷 / pages {len(pages)} 行（非空 {nonempty}）"
                  f" / 目录 {len(tocs)} 条")
            n_pages += len(pages)
            n_toc += len(tocs)
    conn.close()
    size = out.stat().st_size / 1024 / 1024
    print(f"\n已导出 {len(args.book)} 个书库：pages {n_pages} 行、目录 {n_toc} 条 → {out}（{size:.1f} MB）")


def cmd_inject(args: argparse.Namespace) -> None:
    from catalog_release import assert_legacy_catalog_write_allowed
    assert_legacy_catalog_write_allowed()
    src = Path(getattr(args, "in"))
    if not src.exists():
        raise SystemExit(f"找不到变更集：{src}")

    books: list[str] = []
    pages: list[tuple] = []
    tocs: list[tuple] = []
    declared: dict[str, tuple[int, int]] = {}
    with gzip.open(src, "rt", encoding="utf-8") as fh:
        for line in fh:
            obj = json.loads(line)
            if isinstance(obj, dict):
                if "_meta" in obj:
                    continue
                books.append(obj["book"])
                declared[obj["book"]] = (obj["n_pages"], obj["n_toc"])
                continue
            (pages if obj[0] == "P" else tocs).append(tuple(obj[1:]))

    # 完整性自检：条数必须与导出端声明的一致，防止传输截断后注入半本书
    got_pages: dict[str, int] = {}
    got_toc: dict[str, int] = {}
    for r in pages:
        got_pages[r[0]] = got_pages.get(r[0], 0) + 1
    for r in tocs:
        got_toc[r[0]] = got_toc.get(r[0], 0) + 1
    for book, (np_, nt_) in declared.items():
        if got_pages.get(book, 0) != np_ or got_toc.get(book, 0) != nt_:
            raise SystemExit(
                f"[{book}] 变更集不完整：pages {got_pages.get(book, 0)}/{np_}、"
                f"目录 {got_toc.get(book, 0)}/{nt_}。请重新传输，勿注入。")

    print(f"待注入 {len(books)} 个书库：{'、'.join(books)}")
    for book in books:
        print(f"  {book}: pages {got_pages.get(book, 0)} 行、目录 {got_toc.get(book, 0)} 条")
    if args.dry_run:
        print("[dry-run] 未写库。")
        return

    if not args.no_backup:
        bak = DB_PATH.with_suffix(DB_PATH.suffix + f".bak-inject-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(DB_PATH, bak)
        print(f"已备份 → {bak.name}")

    conn = sqlite3.connect(str(DB_PATH))
    try:
        for book in books:
            conn.execute("DELETE FROM pages WHERE book=?", (book,))
            conn.execute("DELETE FROM toc_entries WHERE book=?", (book,))
        conn.executemany(
            f"INSERT INTO pages ({','.join(PAGE_COLS)}) VALUES ({','.join('?' * len(PAGE_COLS))})",
            pages)
        conn.executemany(
            f"INSERT INTO toc_entries ({','.join(TOC_COLS)}) VALUES ({','.join('?' * len(TOC_COLS))})",
            tocs)
        conn.commit()
    finally:
        conn.close()
    h = update_hash(DB_PATH)
    print(f"已注入 pages {len(pages)} 行、目录 {len(tocs)} 条；sha256={h[:16]}…")


def main() -> None:
    ap = argparse.ArgumentParser(description="书库语料导出/定向注入。")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("--book", action="append", required=True)
    e.add_argument("--out", required=True)
    e.set_defaults(func=cmd_export)
    i = sub.add_parser("inject")
    i.add_argument("--in", required=True)
    i.add_argument("--dry-run", action="store_true")
    i.add_argument("--no-backup", action="store_true")
    i.set_defaults(func=cmd_inject)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
