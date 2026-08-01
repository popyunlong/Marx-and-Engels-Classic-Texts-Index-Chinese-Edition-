from __future__ import annotations

import unittest
from pathlib import Path

import yaml

import app as app_module
from book_config import book_config_map
from search import Corpus, Page
from scripts.build_western_marxism import clean_model_text, parse_toc_response, text_layer_usable


WESTERN_KEYS = {
    "历史与阶级意识", "马克思主义和哲学", "狱中札记", "希望的原理（第一卷）",
    "启蒙辩证法", "保卫马克思", "空间的生产",
}


class WesternMarxismConfigTests(unittest.TestCase):
    def test_seven_books_are_independent_single_volumes_in_one_collection(self) -> None:
        configs = book_config_map()
        self.assertTrue(WESTERN_KEYS <= set(configs))
        for key in WESTERN_KEYS:
            cfg = configs[key]
            self.assertTrue(cfg.single_volume)
            self.assertEqual(cfg.collection, "western_marxism")
            self.assertTrue(cfg.authors)
            self.assertTrue(cfg.translators)

    def test_source_manifest_is_complete_and_verified_for_local_files(self) -> None:
        path = Path(app_module.__file__).resolve().parent / "config" / "western_marxism_sources.yaml"
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        rows = payload["books"]
        self.assertEqual({row["key"] for row in rows}, WESTERN_KEYS)
        self.assertTrue(all(row["file"].startswith("pdfs/西马文库/") for row in rows))
        self.assertTrue(all(row["status"] == "ready" for row in rows))
        self.assertTrue(all(row["license_basis"] for row in rows))
        self.assertTrue(all(row["metadata_verified"] for row in rows))
        self.assertTrue(all(len(str(row["sha256"])) == 64 for row in rows))

    def test_scope_contains_exactly_the_seven_books(self) -> None:
        scope = app_module._CORPUS_SCOPE_BY_ID["western_marxism"]
        self.assertEqual(set(scope["books"]), WESTERN_KEYS)
        self.assertIn("卢卡奇", scope["hints"])
        self.assertIn("空间的生产", scope["hints"])

    def test_reader_groups_collection_without_merging_books(self) -> None:
        fake = []
        for index, key in enumerate(sorted(WESTERN_KEYS), 160):
            fake.append({
                "book": key, "book_title": f"《{key}》", "book_sort_order": index,
                "collection": "western_marxism", "collection_label": "西马文库",
                "volume": 1, "source_file": f"pdfs/西马文库/{key}.pdf",
            })
        groups = app_module._library_volume_groups(fake)
        self.assertEqual(len(groups), 1)
        self.assertTrue(groups[0]["is_collection"])
        self.assertEqual(groups[0]["book_count"], 7)
        self.assertEqual({b["key"] for b in groups[0]["books"]}, WESTERN_KEYS)

        regular, sections = app_module._library_display_sections(fake)
        self.assertEqual(regular, [])
        self.assertEqual(len(sections), 1)
        self.assertEqual([b["key"] for b in sections[0]["books"]], [v["book"] for v in fake])

    def test_library_template_places_western_before_foreign_library(self) -> None:
        template = (Path(app_module.__file__).resolve().parent / "templates" / "library.html").read_text(encoding="utf-8")
        self.assertLess(template.index('for section in library_sections'), template.index('>外文文库</div>'))

    def test_homepage_supported_libraries_includes_foreign_library(self) -> None:
        template = (Path(app_module.__file__).resolve().parent / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn('corpus_items.append("外文文库")', template)


class WesternMarxismCitationTests(unittest.TestCase):
    @staticmethod
    def corpus_stub() -> Corpus:
        corpus = object.__new__(Corpus)
        corpus.volumes_cfg = {"历史与阶级意识": {1: 2017}, "publisher": "人民出版社", "place": "北京"}
        corpus.party_meta = {}
        corpus.citation_templates = {}
        corpus.book_configs = list(book_config_map().values())
        corpus.book_config_by_key = book_config_map()
        return corpus

    def test_single_volume_footnote_has_authors_translators_and_no_volume(self) -> None:
        citation = self.corpus_stub()._make_citation(
            "历史与阶级意识", 1, [Page(12, "7", "x", "x")],
            source_file="pdfs/西马文库/历史与阶级意识.pdf",
        )
        self.assertIn("卢卡奇：《历史与阶级意识》", citation)
        self.assertIn("杜章智、任立、燕宏远译", citation)
        self.assertIn("第7页", citation)
        self.assertNotIn("第1卷", citation)

    def test_gb_citation_has_responsibility_statement_and_no_volume(self) -> None:
        citation = self.corpus_stub()._make_citation_gb(
            "历史与阶级意识", 1, [Page(12, "7", "x", "x")]
        )
        self.assertIn("卢卡奇.历史与阶级意识[M]", citation)
        self.assertIn("杜章智,任立,燕宏远,译", citation)
        self.assertNotIn("第1卷", citation)


class WesternMarxismPipelineTests(unittest.TestCase):
    def test_text_layer_quality_gate(self) -> None:
        self.assertTrue(text_layer_usable("这是一段可以正常检索的中文学术著作正文，它包含足够多的汉字和完整的句子。"))
        self.assertFalse(text_layer_usable("abc 123 ���"))

    def test_model_cleanup_rejects_explanatory_only_output(self) -> None:
        text, ok = clean_model_text("以下是图片中的文字：")
        self.assertFalse(ok)
        self.assertEqual(text, "")

    def test_toc_json_parser_keeps_level_and_printed_page(self) -> None:
        rows = parse_toc_response(
            '```json\n[{"title":"第一章 物化与无产阶级意识","level":1,"printed_page":"123"}]\n```',
            source_pdf_page=8,
        )
        self.assertEqual(rows[0]["title"], "第一章 物化与无产阶级意识")
        self.assertEqual(rows[0]["printed_page"], "123")
        self.assertEqual(rows[0]["source_pdf_page"], 8)


if __name__ == "__main__":
    unittest.main()
