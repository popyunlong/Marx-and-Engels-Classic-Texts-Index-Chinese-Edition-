# -*- coding: utf-8 -*-
"""安全·定向重建：只更新《文集》正文（raw_text / normalized_text），其余书目零改动。

做法：
  1) 复制现有 data/corpus.sqlite -> data/corpus.sqlite.new（在副本上操作，绝不动原库）；
  2) 对 10 卷《文集》PDF 逐页重抽取，按 build_index 的修订+OCR标点清理流水线生成
     新的 raw_text 与 normalized_text，UPDATE 副本里对应的 文集 行（按 book/volume/pdf_page）；
  3) 全集 / 列宁全集 / toc_entries 等其它行与表完全不动；
  4) 报告：改动行数、与原文逐行对比（确认未改动页保持字节一致）、修订统计。

只产出 corpus.sqlite.new 供校验，不覆盖原库。
"""
import sys, io, os, shutil, sqlite3
import fitz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import build_index as b  # noqa: E402

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 可用命令行覆盖：python rebuild_wenji_pages.py <src.sqlite> <dst.sqlite>
SRC = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "data", "corpus.sqlite")
DST = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, "data", "corpus.sqlite.new")

PDFS = {
    1: "马克思恩格斯文集[第1卷]马克思恩格斯1843-1848年著作.pdf",
    2: "马克思恩格斯文集[第2卷]马克思恩格斯1848-1859年著作.pdf",
    3: "马克思恩格斯文集[第3卷]马克思恩格斯1864-1883年著作.pdf",
    4: "马克思恩格斯文集[第4卷]恩格斯1884-1895年著作.pdf",
    5: "马克思恩格斯文集[第5卷]马克思《资本论》第一卷.pdf",
    6: "马克思恩格斯文集[第6卷]马克思《资本论》第二卷.pdf",
    7: "马克思恩格斯文集[第7卷]马克思《资本论》第三卷.pdf",
    8: "马克思恩格斯文集[第8卷]马克思《资本论》手稿选编.pdf",
    9: "马克思恩格斯文集[第9卷]恩格斯《反杜林论》《自然辩证法》.pdf",
    10: "马克思恩格斯文集[第10卷]马克思恩格斯书信选编.pdf",
}


def main():
    if not os.path.exists(SRC):
        print("找不到原库", SRC); sys.exit(1)
    print("复制原库 -> 副本 ...")
    shutil.copy2(SRC, DST)

    conn = sqlite3.connect(DST)
    # 预取现有 文集 行（用于对比 + 确认主键存在）
    existing = {}
    for vol, pg, raw in conn.execute(
        "select volume, pdf_page, raw_text from pages where book='文集'"
    ):
        existing[(vol, pg)] = raw
    print(f"现有 文集 行数 = {len(existing)}")

    changed = 0
    changed_by_corr = 0
    missing_pages = 0
    updates = []
    for vol in range(1, 11):
        pdf = os.path.join(ROOT, "pdfs", "文集", PDFS[vol])
        print(f"  处理第{vol}卷 ...", flush=True)
        with fitz.open(pdf) as doc:
            for i, page in enumerate(doc, start=1):
                raw = page.get_text("text")
                raw2 = b.apply_text_corrections("文集", vol, i, raw)
                raw2 = b.apply_ocr_punct_cleanup(raw2, page, vol)
                if (vol, i) not in existing:
                    missing_pages += 1
                    continue
                if raw2 != existing[(vol, i)]:
                    changed += 1
                    if raw2 != raw:           # 因修订/清理而变（预期）
                        changed_by_corr += 1
                    updates.append((raw2, b.normalize(raw2), vol, i))
    print(f"PDF 页与库匹配缺失 = {missing_pages}（应为 0）")
    print(f"将更新行数 = {changed}（其中因修订/清理变动 = {changed_by_corr}）")
    other = changed - changed_by_corr
    print(f"非修订原因变动行数 = {other}（PyMuPDF 版本漂移指标，应≈0）")

    conn.executemany(
        "UPDATE pages SET raw_text=?, normalized_text=? WHERE book='文集' AND volume=? AND pdf_page=?",
        updates,
    )
    conn.commit()

    # 校验：行数与各书计数不变
    print("--- 校验副本各书行数 ---")
    for book, cnt in conn.execute("select book, count(*) from pages group by book order by book"):
        print(f"   {book}: {cnt}")
    print("--- 校验 toc_entries 行数（应不变）---")
    print("   toc_entries:", conn.execute("select count(*) from toc_entries").fetchone()[0])
    conn.close()

    cs = b._corrections_stats
    print("--- 修订统计 ---")
    print(f"   手工修订已应用={cs['applied']}（文集级={cs['book_wide']} 卷级={cs['volume_wide']} "
          f"页锚定={cs['applied']-cs['book_wide']-cs['volume_wide']}） 未命中={cs['missing']} 命中异常={cs['ambiguous']}")
    print(f"   OCR标点清理移除={cs['ocr_punct']} 锚定跳过={cs['ocr_skip']}")
    print("DONE_REBUILD")


if __name__ == "__main__":
    main()
