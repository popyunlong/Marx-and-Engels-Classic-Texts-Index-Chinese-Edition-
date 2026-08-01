import unittest

from volume_presentation import TRUSTED_TOC_DATE_BOOKS, volume_presentation


class VolumePresentationTests(unittest.TestCase):
    def test_only_verified_document_collections_use_toc_date_inference(self) -> None:
        self.assertEqual(
            TRUSTED_TOC_DATE_BOOKS,
            {"建党以来重要文献选编", "建国以来重要文献选编"},
        )
        item = volume_presentation(
            "建国以来重要文献选编",
            1,
            "建国以来重要文献选编（第一册）",
            unit="册",
            trusted_date_span="1949.9—1950.12",
        )
        self.assertEqual(item.heading, "第 1 册")
        self.assertEqual(item.subtitle, "1949.9—1950.12")

    def test_wenji_uses_content_titles_instead_of_toc_year_guess(self) -> None:
        item = volume_presentation(
            "文集", 4, "马克思恩格斯文集[第4卷]恩格斯1884-1895年著作"
        )
        self.assertEqual(item.heading, "第 4 卷")
        self.assertEqual(item.subtitle, "恩格斯1884—1895年著作")

        capital = volume_presentation("文集", 6, "马克思恩格斯文集[第6卷]马克思《资本论》第二卷")
        self.assertEqual(capital.subtitle, "《资本论》第二卷")

    def test_complete_works_use_bibliographic_sections_not_guessed_dates(self) -> None:
        first = volume_presentation("全集", 26, "马克思恩格斯全集 第26卷 第2册(OCR)")
        second = volume_presentation("全集二版", 35, "任何带错误年份的文件名")
        letters = volume_presentation("全集二版", 49, "马克思恩格斯全集（第二版）第49卷")
        self.assertEqual(first.subtitle, "《剩余价值理论》第二册")
        self.assertEqual(second.subtitle, "《资本论》及其手稿卷")
        self.assertEqual(letters.subtitle, "书信卷")

    def test_lenin_uses_edition_volume_label(self) -> None:
        item = volume_presentation(
            "列宁全集",
            54,
            "列宁全集 2版增订(第54卷)：关于帝国主义的笔记 1912-1916年 ",
        )
        self.assertEqual(item.subtitle, "关于帝国主义的笔记 1912—1916年")

    def test_curated_leader_work_ranges_are_stable(self) -> None:
        self.assertEqual(
            volume_presentation("毛泽东选集", 2, "毛泽东选集第二卷").subtitle,
            "抗日战争时期（上）",
        )
        self.assertEqual(
            volume_presentation("邓小平文选", 3, "邓小平文选第三卷").subtitle,
            "1982.9—1992.2",
        )
        self.assertEqual(
            volume_presentation("治国理政", 5, "治国理政第五卷").subtitle,
            "2022.5—2024.12",
        )
        self.assertEqual(
            volume_presentation("习近平经济文选", 1, "经济文选第一卷").subtitle,
            "2012.11—2024.12",
        )

    def test_document_series_use_real_part_names(self) -> None:
        self.assertEqual(
            volume_presentation("十九大以来重要文献选编", 2, "十九大以来重要文献选编（中）").heading,
            "中册",
        )
        plenum = volume_presentation("历届全会公报", 1103, "内部文件名")
        self.assertEqual((plenum.heading, plenum.subtitle), ("十一届三中全会", "公报"))
        congress = volume_presentation("历次党代会报告", 20, "内部文件名")
        self.assertEqual((congress.heading, congress.subtitle), ("二十大", "全国代表大会报告"))

    def test_unverified_dates_in_generic_filenames_are_not_shown(self) -> None:
        item = volume_presentation("保卫马克思", 1, "保卫马克思（1844年）", single_volume=True)
        self.assertEqual((item.heading, item.subtitle), ("", ""))


if __name__ == "__main__":
    unittest.main()
