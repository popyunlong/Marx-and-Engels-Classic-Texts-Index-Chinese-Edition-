"""journal_review 分类稳健性 + 综述生成回归测试。

锁定此前「整批文章掉进暂未归类」的根因修复：
① 学科名归一化兜回规范桶（缺字/改写/近义/未知）；
② 分类 JSON 被截断时按对象抢救，丢尾部不完整的一个而非整组作废；
③ 端到端：AI 可用时文章被分进具体领域而非全部「暂未归类」，且文末 GB2015 引文全覆盖；
④ AI 不可用时降级为「全部暂未归类 + 引文」，不抛异常。
"""
from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import tempfile
import unittest
import warnings

warnings.filterwarnings("ignore", category=ResourceWarning)
_TMP_APPDATA = tempfile.mkdtemp(prefix="marx-search-journalreview-")
atexit.register(lambda: shutil.rmtree(_TMP_APPDATA, ignore_errors=True))
os.environ.setdefault("APPDATA", _TMP_APPDATA)
os.environ.setdefault("APP_MODE", "server")

import journal_review as jr  # noqa: E402


class _FakeConfig:
    enabled = True
    model = "fake-review-model"


class _FakeClient:
    """最小可用 client：分类请求回 JSON 数组，综述请求回固定 Markdown 小节。"""

    def __init__(self, discipline: str = "政治经济学与资本主义批判") -> None:
        self.config = _FakeConfig()
        self.discipline = discipline

    def chat_complete(self, messages, max_tokens=0, **kwargs):
        system = messages[0]["content"]
        user = messages[1]["content"]
        if "只返回 JSON" in system:  # 分类调用
            ids = [int(x) for x in re.findall(r'"id":\s*(\d+)', user)]
            return json.dumps(
                [{"id": i, "discipline": self.discipline, "problem_type": "前沿问题"} for i in ids],
                ensure_ascii=False,
            )
        return "### 经典问题\n\n本领域本周暂无此类研究。\n\n### 前沿问题与前沿研究\n\n有学者提出某观点 [1]。"


class NormalizeDisciplineTest(unittest.TestCase):
    def test_exact_match(self) -> None:
        self.assertEqual(jr._normalize_discipline("思想政治教育"), "思想政治教育")
        self.assertEqual(jr._normalize_discipline("政治经济学与资本主义批判"), "政治经济学与资本主义批判")

    def test_drift_maps_to_canonical(self) -> None:
        # 缺「研究」后缀。
        self.assertEqual(jr._normalize_discipline("国外马克思主义"), "国外马克思主义研究")
        # 加括号注脚。
        self.assertEqual(jr._normalize_discipline("马克思主义中国化研究（理论成果）"), "马克思主义中国化研究")
        # 关键词兜底。
        self.assertEqual(jr._normalize_discipline("金融化与资产权力"), "政治经济学与资本主义批判")
        self.assertEqual(jr._normalize_discipline("第三世界发展研究"), "帝国主义、全球化与发展研究")
        self.assertEqual(jr._normalize_discipline("数字平台劳动"), "劳动、阶级与社会再生产")

    def test_unknown_returns_unclassified(self) -> None:
        self.assertEqual(jr._normalize_discipline(""), jr.UNCLASSIFIED)
        self.assertEqual(jr._normalize_discipline(None), jr.UNCLASSIFIED)
        self.assertEqual(jr._normalize_discipline("天体物理学"), jr.UNCLASSIFIED)


class ParseEntriesSalvageTest(unittest.TestCase):
    def test_truncated_array_recovers_complete_objects(self) -> None:
        # 第 3 个对象被截断 → 只救回前两个完整对象，而非整组作废。
        truncated = '[{"id":1,"discipline":"x"},{"id":2,"discipline":"y"},{"id":3,"disc'
        entries = jr._parse_classification_entries(truncated)
        self.assertEqual([e["id"] for e in entries], [1, 2])

    def test_clean_array_parses(self) -> None:
        self.assertEqual(
            jr._parse_classification_entries('[{"id":7,"discipline":"z"}]'),
            [{"id": 7, "discipline": "z"}],
        )

    def test_fenced_json_with_prose(self) -> None:
        wrapped = "这是分类结果：\n```json\n[{\"id\":5,\"discipline\":\"q\"}]\n```\n以上。"
        self.assertEqual(jr._parse_classification_entries(wrapped), [{"id": 5, "discipline": "q"}])


class BuildReviewTest(unittest.TestCase):
    def _articles(self, n: int):
        return [
            {
                "id": i,
                "title": f"Article {i}",
                "title_zh": f"文章{i}",
                "journal_name": "Test Journal",
                "authors": ["A", "B"],
                "abstract": "abstract text",
            }
            for i in range(1, n + 1)
        ]

    def test_articles_land_in_real_discipline(self) -> None:
        md, html, model = jr.build_literature_review(
            self._articles(3), ai_client=_FakeClient(), settings={"lookback_days": 7}
        )
        self.assertIn("政治经济学与资本主义批判", md)
        self.assertNotIn("暂未归类", md)  # 全部分走，不应出现兜底小节
        self.assertEqual(model, "fake-review-model")
        self.assertIn("[1]", md)  # 文末引文全覆盖
        self.assertIn("[3]", md)
        self.assertTrue(html.strip())

    def test_ai_disabled_falls_back_without_crash(self) -> None:
        md, _html, _model = jr.build_literature_review(self._articles(2), ai_client=None, settings={})
        self.assertIn("暂未归类", md)  # 无 AI 时全部兜底
        self.assertIn("[2]", md)


if __name__ == "__main__":
    unittest.main()
