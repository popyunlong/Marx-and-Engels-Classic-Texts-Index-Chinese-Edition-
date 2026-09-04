import unittest

import app as app_module
from book_config import book_config_map


class LibraryCollectionTests(unittest.TestCase):
    def test_books_are_assigned_to_three_reader_collections(self) -> None:
        configs = book_config_map()
        expected = {
            "classical_marxism": {
                "文集", "全集", "全集二版", "马恩选集", "列宁全集",
            },
            "marxism_china": {
                "毛泽东选集", "毛泽东文集", "邓小平文选", "江泽民文选", "胡锦涛文选",
            },
            "xi_thought": {
                "治国理政", "习近平经济文选", "习近平新时代中国特色社会主义思想学习纲要",
                "习近平经济思想学习纲要", "习近平法治思想学习纲要",
                "习近平生态文明思想学习纲要", "习近平文化思想学习纲要",
                "习近平总书记关于党的建设的重要思想概论",
            },
            "party_state_documents": {
                "历次党代会报告", "历届全会公报", "十八大以来重要文献选编",
                "十九大以来重要文献选编", "二十大以来重要文献选编", "五年规划",
            },
        }
        for collection, keys in expected.items():
            actual = {key for key, cfg in configs.items() if cfg.collection == collection}
            self.assertTrue(keys.issubset(actual), f"{collection} 缺少基础书库：{sorted(keys - actual)}")

    def test_new_xi_discourse_books_are_independent_single_volume_libraries(self) -> None:
        configs = book_config_map()
        expected = {
            "论坚持党对一切工作的领导",
            "论党的宣传思想工作",
            "论中国共产党历史",
            "论把握新发展阶段、贯彻新发展理念、构建新发展格局",
            "论党的自我革命",
            "习近平关于党的群众路线教育实践活动论述摘编",
            "习近平关于总体国家安全观论述摘编",
            "习近平关于网络强国论述摘编",
            "习近平关于社会主义精神文明建设论述摘编",
            "习近平关于树立和践行正确政绩观论述摘编",
        }
        for key in expected:
            cfg = configs[key]
            self.assertEqual(cfg.collection, "xi_thought")
            self.assertTrue(cfg.single_volume)
            self.assertEqual(cfg.folder, "pdfs/习近平专题论述")

        xi_scope = next(scope for scope in app_module.CORPUS_SCOPES if scope["id"] == "xi")
        self.assertTrue(
            expected.issubset(set(xi_scope["books"])),
            f"习近平指定著作列表缺少：{sorted(expected - set(xi_scope['books']))}",
        )

    def test_collection_labels_are_complete(self) -> None:
        self.assertEqual(app_module._COLLECTION_LABELS["classical_marxism"], "马克思主义经典著作")
        self.assertEqual(app_module._COLLECTION_LABELS["marxism_china"], "马克思主义中国化时代化经典著作")
        self.assertEqual(app_module._COLLECTION_LABELS["xi_thought"], "习近平新时代中国特色社会主义思想")
        self.assertEqual(app_module._COLLECTION_LABELS["party_state_documents"], "党和国家重要文献")

    def test_reader_uses_full_xi_book_titles(self) -> None:
        configs = book_config_map()
        self.assertEqual(configs["治国理政"].title, "《习近平谈治国理政》")
        self.assertEqual(configs["习近平经济文选"].title, "《习近平经济文选》")

    def test_collections_render_as_flat_independent_sections(self) -> None:
        fake = []
        for index, (key, collection) in enumerate((
            ("文集", "classical_marxism"),
            ("毛泽东选集", "marxism_china"),
            ("治国理政", "xi_thought"),
            ("历次党代会报告", "party_state_documents"),
            ("历史与阶级意识", "western_marxism"),
            ("黑格尔早期神学著作", "hegel_works"),
        ), 40):
            fake.append({
                "book": key, "book_title": f"《{key}》", "book_sort_order": index,
                "collection": collection, "collection_label": app_module._COLLECTION_LABELS[collection],
                "volume": 1, "source_file": f"pdfs/{key}.pdf",
            })
        regular, sections = app_module._library_display_sections(fake)
        self.assertEqual(regular, [])
        self.assertEqual([section["label"] for section in sections], [
            "黑格尔著作集",
            "马克思主义经典著作",
            "马克思主义中国化时代化经典著作",
            "习近平新时代中国特色社会主义思想",
            "党和国家重要文献",
            "西马文库",
        ])


if __name__ == "__main__":
    unittest.main()
