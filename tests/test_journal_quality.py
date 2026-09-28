"""Publication checks must reject wrong or incomplete source PDFs."""
from __future__ import annotations

import sys
import tempfile
import types
import unittest
import hashlib
from pathlib import Path
from unittest.mock import patch

from journal_quality import quality_failure_summary, source_pdf_rejection_reasons


TITLE = "Financial Development and Credit Markets"
PAGE_FOUR = ("Credit allocation among banks and firms changes production. " * 35).strip()
PAGE_FIVE = ("Labor demand and wages respond to trade patterns. " * 35).strip()


class _Page:
    def __init__(self, text: str) -> None:
        self.text = text

    def get_text(self, _format: str, **_kwargs) -> str:
        return self.text


class _Pdf:
    def __init__(self) -> None:
        self.pages = [_Page(TITLE), _Page("Highlights"), _Page("Abstract"),
                      _Page(PAGE_FOUR), _Page(PAGE_FIVE)]
        self.page_count = len(self.pages)

    def __getitem__(self, index: int) -> _Page:
        return self.pages[index]

    def close(self) -> None:
        pass


class SourcePdfQualityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "source.pdf").write_bytes(b"test pdf stand-in")
        pdf = _Pdf()
        self.module = types.SimpleNamespace(open=lambda _path: pdf, Rect=lambda box: box)
        patcher = patch.dict(sys.modules, {"fitz": self.module})
        patcher.start()
        self.addCleanup(patcher.stop)

    def document(self, *, title: str = TITLE, complete: bool = True) -> dict:
        blocks = [{"page": 4, "kind": "body", "text": PAGE_FOUR}]
        if complete:
            blocks.append({"page": 5, "kind": "body", "text": PAGE_FIVE})
        source_hash = hashlib.sha256((self.root / "source.pdf").read_bytes()).hexdigest()
        return {"page_count": 5, "metadata": {"title_en": title},
                "provenance": {"sha256": source_hash}, "paragraphs": blocks}

    def test_complete_document_passes(self) -> None:
        self.assertEqual(source_pdf_rejection_reasons(self.document(), self.root), [])

    def test_wrong_title_and_missing_middle_page_fail(self) -> None:
        errors = source_pdf_rejection_reasons(
            self.document(title="An Unrelated Philosophy Paper", complete=False), self.root
        )
        self.assertIn("source-title-mismatch", errors)
        self.assertTrue(any(item.startswith("source-page-5-coverage:") for item in errors))

    def test_cross_page_paragraph_counts_for_both_pages(self) -> None:
        document = self.document(complete=False)
        document["paragraphs"] = [{
            "page": 4, "page_end": 5, "kind": "body", "text": PAGE_FOUR + " " + PAGE_FIVE,
        }]
        self.assertEqual(source_pdf_rejection_reasons(document, self.root), [])

    def test_source_replacement_is_rejected(self) -> None:
        document = self.document()
        (self.root / "source.pdf").write_bytes(b"replacement")
        self.assertIn("source-pdf-hash-mismatch",
                      source_pdf_rejection_reasons(document, self.root))

    def test_admin_summary_names_the_actual_failures(self) -> None:
        summary = quality_failure_summary({
            "failed_article_ids": ["1366"],
            "articles": {"1366": {"errors": ["source-title-mismatch", "captions-unpaired"]}},
        })
        self.assertIn("来源 PDF 不符", summary)
        self.assertIn("图表未配对", summary)


if __name__ == "__main__":
    unittest.main()
