"""Synthetic editorial gold set. No paper or private library material is used."""
import json
from pathlib import Path
from zipfile import ZipFile

from docx import Document

import citation_assistant as core
import citation_agent_test_backend as agent
import citation_quote_evidence as evidence
from build_index import normalize
from search import Corpus, Page, Volume

# Each entry is an independently specified source location and a manuscript variant.
# The surrounding boilerplate intentionally recurs in every volume.
VARIANTS = [
    ("纺织工人考察羊毛市场", "纺织工人考察羊毛市埸"),
    ("机械工坊校正齿轮尺寸", "机械工坊校正齿轮尺"),
    ("农民合作社储藏燕麦", "农民合作社储藏燕麦粒"),
    ("港口工人搬运陶瓷货物", "港口工人搬运陶瓷贷物"),
    ("矿场测绘员核对巷道", "矿场测绘员核巷道"),
    ("木匠协会讨论松木价格", "木匠协会讨论松木木价格"),
    ("染坊师傅调配蓝色颜料", "染坊师傅调配兰色颜料"),
    ("陶工记录瓷土烧成温度", "陶工记录瓷土烧温度"),
    ("码头账房复查驳船单据", "码头账房复查驳船單椐"),
    ("制革车间统计鞣皮耗时", "制革车间统计糅皮耗时"),
    ("石匠检验桥拱接缝", "石匠检验桥拱接逢"),
    ("印刷学徒整理铅字字盘", "印刷学徒整理铅字盘"),
    ("园艺社员移栽杏树幼苗", "园艺社员移栽杏树幼描"),
    ("磨坊主人筛选黑麦种子", "磨坊主人筛选黑黑麦种子"),
    ("船坞技师检查桅杆接头", "船坞技师查桅杆接头"),
    ("矿工搜集铁矿石样品", "矿工搜集铁旷石样品"),
    ("农艺学校测量黏土湿度", "农艺学校测黏土湿度"),
    ("渔村账册记载鱼网损耗", "渔村账册记载渔网损耗"),
    ("铁道员工检修扳道装置", "铁道员工检修板道装置"),
    ("裁缝学徒裁剪棉布衬里", "裁缝学徒剪棉布衬里"),
    ("林场工人清点杉木库存", "林埸工人清点杉木库寸"),
    ("铜匠测量锅壁厚度", "铜匠测量锅璧厚度"),
]
PREFIX = "调查者在现场笔记中逐项整理了"
SUFFIX = "的详细记录，随后交给同伴核对。"


class LocalCorpus:
    _export_page_raw_map = staticmethod(Corpus._export_page_raw_map)

    def _make_hit(self, volume, start, end, kind, score, paper, fuzzy_errors=None):
        pages = volume.pages[volume.page_index_at(start):volume.page_index_at(end - 1) + 1]
        return dict(book=volume.book, volume=volume.volume, source_file=volume.source_file,
                    match_type=kind, score=score, fuzzy_errors=fuzzy_errors,
                    citation=f"{volume.book} 第{volume.volume}卷 第{pages[0].printed_page}页",
                    pdf_pages=[p.pdf_page for p in pages], printed_pages=[p.printed_page for p in pages])


def volume(number, *texts, printed=True):
    pages = [Page(i + 1, str(number * 10 + i) if printed else None, raw, normalize(raw))
             for i, raw in enumerate(texts)]
    return Volume.build("调查材料", number, f"s{number}.pdf", f"第{number}卷", pages)


def classify(raw, options, confirm=True):
    for option in options:
        if confirm:
            # Fixed cooperative model stub. Even an unconditional confirmation must
            # not bypass the local checks for negations, dates and source collisions.
            option["quote_evidence"].update(ai_confirmed=True, ai_verdict="minor_difference")
    first = options[0]
    paragraph = "“" + raw + "”"
    return agent._apply_test_evidence_rules({
        "kind": "generate", "paper_text": raw, "paragraph_index": 0, "raw_start": 0,
        "raw_end": len(paragraph), "source_options": options, "match_type": first["match_type"],
        "score": first["score"], "fuzzy_errors": first["fuzzy_errors"], "issue_code": "suggest_add",
    }, {0: {"text": paragraph, "safe_comment_spans": [[0, len(paragraph)]]}})


def test_synthetic_editorial_corpus_recall_and_precision(tmp_path):
    corpus = LocalCorpus()
    sources = [volume(i, PREFIX + original, SUFFIX) for i, (original, _) in enumerate(VARIANTS, 1)]
    gold = {PREFIX + manuscript + SUFFIX: n for n, (_, manuscript) in enumerate(VARIANTS, 1)}
    retrieved = evidence.verify_many(corpus, sources, gold)
    matched = automatic = wrong = 0
    for paper, expected in gold.items():
        options = retrieved.get(paper) or []
        matched += any(opt["volume"] == expected for opt in options)
        if not options:
            continue
        item = classify(paper, options)
        if item["auto_selected"]:
            automatic += 1
            wrong += options[item.get("selected_option", 0)]["volume"] != expected
        best = options[0]["quote_evidence"]
        for change in best["differences"]:
            assert paper[change["paper_start"]:change["paper_end"]] == change["paper"]
            assert best["source_text"][change["source_start"]:change["source_end"]] == change["source"]
    metrics = dict(cases=len(gold), correct_source_recalled=matched, automatically_adopted=automatic,
                   wrong_automatic_source=wrong)
    (tmp_path / "quote-calibration.json").write_text(json.dumps(metrics), encoding="utf-8")
    print("citation calibration", json.dumps(metrics))
    assert matched / len(gold) >= 0.95, metrics
    assert automatic >= 18 and wrong == 0, metrics


def test_semantic_differences_duplicate_printings_and_missing_pages():
    corpus = LocalCorpus()
    cases = [
        ("工坊并未放弃改良方案", "工坊并放弃改良方案"),
        ("本次购买需要300英镑", "本次购买需要800英镑"),
        ("恩格斯接收这些样品", "恩格土接收这些样品"),
    ]
    for original, manuscript in cases:
        paper = PREFIX + manuscript + SUFFIX
        options = evidence.verify_options(corpus, [volume(1, PREFIX + original + SUFFIX)], paper)
        assert options and not classify(paper, options)["auto_selected"]
    source, paper = PREFIX + VARIANTS[0][0] + SUFFIX, PREFIX + VARIANTS[0][1] + SUFFIX
    # Intra-volume second occurrence is also a competing source location.
    multiple = evidence.verify_options(corpus, [volume(1, source + "相隔一章" + source), volume(2, source)], paper)
    assert len(multiple) == 3 and not classify(paper, multiple)["auto_selected"]
    missing = classify(paper, evidence.verify_options(corpus, [volume(3, source, printed=False)], paper))
    assert missing["writeback_mode"] == "none"
    short = classify("纺织工人考察当地羊毛市埸", evidence.verify_options(
        corpus, [volume(1, "纺织工人考察当地羊毛市场")], "纺织工人考察当地羊毛市埸"))
    assert not short["auto_selected"]
    assert not evidence.verify_options(corpus, [volume(1, source)], "天文学家记录月面山脉近旁的环形陨石坑")


def test_long_quote_requires_verified_tail_and_safe_word_writeback(tmp_path, monkeypatch):
    # A prefix alone must never verify a quotation longer than the network fragment.
    raw = ("这里记载工场主的生产活动以及具体劳动过程。" * 20) + "最后留给读者思考。"
    source = raw[:-9] + "与论文末尾完全相冲突的文字。"
    assert not evidence.verify_options(LocalCorpus(), [volume(1, source)], raw)
    orig, changed = VARIANTS[-1]
    paper = PREFIX + changed + SUFFIX
    opts = evidence.verify_options(LocalCorpus(), [volume(2, PREFIX + orig + SUFFIX)], paper)
    item = classify(paper, opts, confirm=False)
    assert not item["auto_selected"] and item["writeback_mode"] == "footnote"
    item.update(decision="accepted", proposed_citation=opts[0]["citation"])
    file = tmp_path / "manuscript.docx"
    document = Document()
    original_paragraph = "“" + paper + "”"
    document.add_paragraph(original_paragraph)
    document.save(file)
    monkeypatch.setattr(core, "_job_dir", lambda *args, **kw: tmp_path)
    result = core.export_docx(dict(id="edit", user_id=3, mode="generate", note_kind="footnote",
                                   original_filename=file.name, input_path=str(file)), [item])
    with ZipFile(result) as pkg:
        body = core._parse_xml(pkg.read("word/document.xml"))
        assert "".join(body.xpath("//w:body//w:t/text()", namespaces=core.NS)) == original_paragraph
        assert len(body.xpath("//w:footnoteReference", namespaces=core.NS)) == 1
        comments = core._parse_xml(pkg.read("word/comments.xml"))
        assert "璧 → 原书 壁" in "".join(comments.xpath("//w:t/text()", namespaces=core.NS))
