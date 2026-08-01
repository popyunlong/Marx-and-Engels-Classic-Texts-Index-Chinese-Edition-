"""聚合计数（search_chaptered）须与钻取命中（chapter_hits）一致，即对周期串也用【重叠】计数。

回归点：search_chaptered 曾用 str.count（非重叠），与 chapter_hits 的 find 步进 +1（重叠）不一致，
导致「一一」类周期查询显示的篇章/总计数 < 钻取列出的命中数。_count_overlapping 统一为重叠语义。
"""
from __future__ import annotations

import unittest

from search import _count_overlapping


class OverlappingCountTests(unittest.TestCase):
    def test_periodic_overlaps_more_than_str_count(self) -> None:
        s = "一一一一"
        # 重叠：位置 0,1,2 各起一个「一一」= 3；非重叠的 str.count 只数 2
        self.assertEqual(_count_overlapping(s, "一一", 0, len(s)), 3)
        self.assertEqual(s.count("一一"), 2)

    def test_non_periodic_matches_str_count(self) -> None:
        s = "abcabc"
        self.assertEqual(_count_overlapping(s, "abc", 0, len(s)), 2)
        self.assertEqual(_count_overlapping(s, "abc", 0, len(s)), s.count("abc"))

    def test_range_is_respected(self) -> None:
        s = "一一一一"
        # 仅在 [1,3) 内查：只能从位置 1 起一个「一一」
        self.assertEqual(_count_overlapping(s, "一一", 1, 3), 1)

    def test_empty_and_missing(self) -> None:
        self.assertEqual(_count_overlapping("abc", "", 0, 3), 0)
        self.assertEqual(_count_overlapping("abc", "x", 0, 3), 0)


if __name__ == "__main__":
    unittest.main()
