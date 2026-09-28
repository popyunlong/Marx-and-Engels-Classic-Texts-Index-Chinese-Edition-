"""Regression checks for an issue that previously looked ready but could not be sent."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import fitz

from journal_fulltext import normalize_reflow_content
from journal_quality import (
    build_quality_report,
    file_sha256,
    pdf_identity_error,
    source_body_page_coverage,
    validate_document,
)


class JournalQualityGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.article_dir = Path(self.temp.name)
        source = fitz.open()
        for text in (
            "Verified research on credit and trade\nA. Smith\nDOI: 10.1234/verified\n"
            "Introduction\n" + "A substantial opening discussion of credit and trade. " * 10,
            "A substantial second part of the article about credit markets. " * 12,
            "References\nSmith, A. Research on markets and trade.",
        ):
            page = source.new_page()
            page.insert_text((50, 60), text, fontsize=9)
        source.save(self.article_dir / "source.pdf")
        source.close()
        self.document = {
            "schema_version": 5,
            "page_count": 3,
            "metadata": {
                "title_en": "Verified research on credit and trade",
                "authors_en": ["A. Smith"],
                "abstract_en": "This study examines credit and trade in a verified source paper, "
                "including the mechanisms that shape market access and the outcomes of trade.",
                "abstract_zh": "本研究考察信贷与贸易。",
            },
            "provenance": {"doi": "10.1234/verified", "sha256": file_sha256(self.article_dir / "source.pdf")},
            "paragraphs": [
                {"kind": "body", "page": 1, "text": "A substantial opening discussion of credit and trade.", "zh": "信贷与贸易的讨论。"},
                {"kind": "body", "page": 2, "text": "A substantial second part of the article about credit markets.", "zh": "信贷市场的第二部分。"},
            ],
        }

    def _report(self) -> None:
        coverage, _ = source_body_page_coverage(
            self.article_dir / "source.pdf", self.document["paragraphs"]
        )
        self.document["quality"] = build_quality_report(
            self.document, body_word_coverage=coverage
        )

    def test_matching_paper_and_complete_body_pass(self) -> None:
        self._report()
        self.assertEqual(validate_document(self.document, self.article_dir)["status"], "passed")

    def test_other_papers_pdf_is_rejected(self) -> None:
        self._report()
        self.document["metadata"]["title_en"] = "A different paper on philosophy"
        result = validate_document(self.document, self.article_dir)
        self.assertIn("pdf-title-mismatch", result["errors"])

    def test_missing_opening_body_pages_cannot_pass(self) -> None:
        self.document["paragraphs"] = self.document["paragraphs"][1:]
        self._report()
        result = validate_document(self.document, self.article_dir)
        self.assertEqual(result["body_word_coverage"], 0.5)
        self.assertIn("body-word-coverage", result["errors"])

    def test_only_references_and_orphan_formula_fragment_fail(self) -> None:
        self.document["paragraphs"] = [{"kind": "reference", "page": 3, "text": "References"}]
        self._report()
        self.assertEqual(validate_document(self.document, self.article_dir)["status"], "failed")
        self.document["paragraphs"] = [
            *self.document["paragraphs"],
            {"kind": "body", "page": 1, "text": "w", "zh": "w"},
            {"kind": "body", "page": 2, "text": "Trade depends on credit.", "zh": "贸易依赖信贷。"},
        ]
        self._report()
        self.assertIn("orphan-fragments", validate_document(self.document, self.article_dir)["errors"])

    def test_unpaired_figure_caption_fails(self) -> None:
        self.document["paragraphs"].append(
            {"kind": "caption", "page": 2, "text": "Figure 1. Market outcomes", "zh": "图1　市场结果"}
        )
        self._report()
        self.assertIn("captions-unpaired", validate_document(self.document, self.article_dir)["errors"])

    def test_saved_pass_cannot_hide_new_fragment_or_caption(self) -> None:
        self._report()
        self.assertEqual(self.document["quality"]["status"], "passed")
        self.document["paragraphs"].extend([
            {"kind": "body", "page": 2, "text": "w", "zh": "w"},
            {"kind": "caption", "page": 2, "text": "Figure 1. Market outcomes", "zh": "图1　市场结果"},
        ])
        errors = validate_document(self.document, self.article_dir)["errors"]
        self.assertIn("orphan-fragments", errors)
        self.assertIn("captions-unpaired", errors)

    def test_abstract_cleanup_stops_before_unlabelled_body(self) -> None:
        blocks = [
            {"kind": "heading", "page": 1, "text": "Abstract"},
            {"kind": "body", "page": 1, "text": "An abstract paragraph about the research, "
             "its methods, its evidence, and the conclusions reached by the authors."},
            {"kind": "body", "page": 2, "text": "The opening of the real article discusses its first argument in detail."},
            {"kind": "body", "page": 3, "text": "The second section continues the argument in detail."},
            {"kind": "heading", "page": 4, "text": "References"},
        ]
        normalized = normalize_reflow_content(blocks)["paragraphs"]
        self.assertEqual(normalized[0]["page"], 2)
        self.assertIn("opening of the real article", normalized[0]["text"])


if __name__ == "__main__":
    unittest.main()
