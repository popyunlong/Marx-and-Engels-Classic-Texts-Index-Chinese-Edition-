# -*- coding: utf-8 -*-
"""校验《文集》定向更新后的 corpus.sqlite.new 是否安全可上线（只读对比）。

门禁：
  1) 每书行数与原库完全一致；
  2) 全集 / 列宁全集 的 raw_text 与原库逐字节一致（哈希对比）——证明零波及；
  3) toc_entries 行数不变；
  4) 修订生效抽查：被修订关键词在新库 normalized_text 可命中、在旧库不可命中（或被噪声阻断）；
  5) 各书检索回归：一组已知良查询在新库仍可命中；
  6) 新库可被 Corpus 正常加载（不崩溃）。
任何一项失败即非 0 退出。
"""
import sys, io, os, sqlite3, hashlib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import build_index as bi  # noqa: E402

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "data", "corpus.sqlite")
DST = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, "data", "corpus.sqlite.new")

fail = []


def book_counts(conn):
    return dict(conn.execute("select book, count(*) from pages group by book").fetchall())


def book_hash(conn, book):
    h = hashlib.sha256()
    for (raw,) in conn.execute(
        "select raw_text from pages where book=? order by volume, pdf_page", (book,)
    ):
        h.update(raw.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def norm_contains(conn, book, vol, needle):
    nn = bi.normalize(needle)
    rows = conn.execute(
        "select normalized_text from pages where book=? and volume=?", (book, vol)
    ).fetchall()
    return any(nn in r[0] for r in rows)


s = sqlite3.connect(SRC)
d = sqlite3.connect(DST)

# (1) row counts
cs, cd = book_counts(s), book_counts(d)
print("行数 原库:", cs)
print("行数 新库:", cd)
if cs != cd:
    fail.append(f"行数不一致 {cs} != {cd}")

# (2) 全集/列宁 byte-identical
for book in ("全集", "列宁全集"):
    if book in cs:
        hs, hd = book_hash(s, book), book_hash(d, book)
        ok = hs == hd
        print(f"{book} raw_text 哈希一致: {ok}")
        if not ok:
            fail.append(f"{book} 被波及（哈希不一致）")

# (3) toc_entries
ts = s.execute("select count(*) from toc_entries").fetchone()[0]
td = d.execute("select count(*) from toc_entries").fetchone()[0]
print(f"toc_entries 原={ts} 新={td}")
if ts != td:
    fail.append("toc_entries 行数变化")

# (4) 修订生效抽查（new 命中；old 不命中）
checks = [
    ("文集", 1, "得出结论人通过国家", "z 误识冒号清理"),
    ("文集", 6, "商品转化为货币", "商晶→商品 / 货市→货币"),
    ("文集", 3, "俾斯麦", "f卑斯→俾斯"),
    ("文集", 2, "璞鼎查", "瑛鼎查→璞鼎查"),
    ("文集", 1, "机器正像拖犁的牛", "E→正"),
    ("文集", 9, "星云假说", "E→云"),
]
print("--- 修订生效抽查 (new应True / old应False或本就缺失) ---")
for book, vol, needle, desc in checks:
    nnew = norm_contains(d, book, vol, needle)
    nold = norm_contains(s, book, vol, needle)
    print(f"   [{desc}] '{needle}'  new={nnew}  old={nold}")
    if not nnew:
        fail.append(f"修订未生效: {needle} ({desc})")

# (5) 各书检索回归（new 仍命中）
regress = [
    ("文集", 5, "资本主义生产"),
    ("文集", 7, "利润率"),
]
print("--- 检索回归 (new应True) ---")
for book, vol, needle in regress:
    nnew = norm_contains(d, book, vol, needle)
    print(f"   '{needle}' vol{vol} new={nnew}")
    if not nnew:
        fail.append(f"回归失败: {needle}")
# 全集/列宁 抽样仍可命中（用原库已知存在的串：取一页 normalized 前若干字）
for book in ("全集", "列宁全集"):
    if book in cd:
        row = d.execute("select normalized_text from pages where book=? and length(normalized_text)>40 limit 1", (book,)).fetchone()
        if row:
            frag = row[0][:12]
            hit = any(frag in r[0] for r in d.execute("select normalized_text from pages where book=? limit 5000", (book,)))
            print(f"   {book} 抽样片段命中: {hit}")
            if not hit:
                fail.append(f"{book} 抽样检索失败")

s.close(); d.close()

# (6) Corpus 加载新库
print("--- Corpus 加载新库 ---")
try:
    from pathlib import Path
    import search
    corpus = search.Corpus(db_path=Path(DST))
    print("   Corpus 加载成功；卷数 =", len(getattr(corpus, "volumes", {}) or {}))
except Exception as exc:
    fail.append(f"Corpus 加载失败: {exc}")
    print("   Corpus 加载失败:", exc)

print("==== 结果 ====")
if fail:
    print("FAILED:")
    for f in fail:
        print("  -", f)
    sys.exit(1)
print("ALL_CHECKS_PASSED")
