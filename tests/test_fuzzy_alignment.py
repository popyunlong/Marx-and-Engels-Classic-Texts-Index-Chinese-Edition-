"""Real calculator parity, bounded concurrency, and failure isolation."""
import concurrent.futures
import json
import os
import random
import subprocess
import unittest
from unittest.mock import patch

from rapidfuzz import fuzz

import fuzzy_alignment


class FuzzyAlignmentTests(unittest.TestCase):
    def setUp(self):
        self.pool = fuzzy_alignment._Pool()
        self.addCleanup(self.pool.close)

    def test_original_alignment_scores_and_offsets_including_chinese(self):
        rng = random.Random(20261001)
        alphabet = "中国社会主义工人阶级劳动人民解放abcdefgh"
        cases = [("中国工人阶级的解放应当是工人阶级自己的事情",
                  "前言中国工人阶级的解放应当是工人阶级自已的事情后记")]
        for length in (10, 20, 36, 59, 64, 65, 120, 300):
            query = "".join(rng.choice(alphabet) for _ in range(length))
            changed = query[:length // 2] + "错" + query[length // 2 + 1:]
            prefix = "".join(rng.choice(alphabet) for _ in range(400))
            cases.append((query, prefix + changed + prefix + changed))
        cases.extend([("不存在的引文内容没有任何共同字符", "abcdefgh" * 2000),
                      ("abc", ""), ("", "abc"), ("aaaaabaaaaa", "aaaabaaaaabaaaaa")])
        for query, text in cases:
            for cutoff in (0, 80, 95, 100):
                with self.subTest(length=len(query), cutoff=cutoff):
                    expected = fuzz.partial_ratio_alignment(query, text, score_cutoff=cutoff)
                    self.assertEqual(self.pool.align(query, text, cutoff), expected)

    def test_two_concurrent_calculators_keep_responses_separate(self):
        inputs = [("工人阶级的解放" + str(i), "前言工人阶级的解放" + str(i) + "后记")
                  for i in range(20)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as callers:
            results = list(callers.map(lambda pair: self.pool.align(*pair, 80), inputs))
        self.assertEqual(results, [fuzz.partial_ratio_alignment(*pair, score_cutoff=80)
                                   for pair in inputs])
        self.assertLessEqual(len(self.pool.workers), 2)
        self.assertTrue(all(worker.process.pid != os.getpid() for worker in self.pool.workers))

    def test_child_failure_raises_instead_of_silent_missing_results_or_web_scan(self):
        self.pool.align("abcdefgh", "xxabcdefgh", 80)
        worker = self.pool.workers[0]
        worker.process.kill()
        worker.process.wait()
        with patch.object(fuzz, "partial_ratio_alignment", side_effect=AssertionError("web scan")):
            with self.assertRaisesRegex(RuntimeError, "calculator exited"):
                self.pool.align("abcdefgh", "xxabcdefgh", 80)
        self.assertEqual(self.pool.workers, [])
        self.assertIsNotNone(self.pool.align("abcdefgh", "xxabcdefgh", 80))

    def test_worker_environment_does_not_inherit_application_secrets(self):
        with patch.dict(os.environ, {"ZPAY_KEY": "not-for-calculator", "PADDLE_TOKEN": "private"}), \
                patch.object(subprocess, "Popen", wraps=subprocess.Popen) as launch:
            self.pool.align("abcdefgh", "xxabcdefgh", 80)
        args, kwargs = launch.call_args
        self.assertIn("-I", args[0])
        self.assertNotIn("ZPAY_KEY", kwargs["env"])
        self.assertNotIn("PADDLE_TOKEN", kwargs["env"])
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)

    def test_packaged_desktop_uses_existing_algorithm(self):
        with patch.object(fuzzy_alignment.sys, "frozen", True, create=True):
            self.assertEqual(fuzzy_alignment.partial_ratio_alignment("abcdef", "xxabcdef", score_cutoff=80),
                             fuzz.partial_ratio_alignment("abcdef", "xxabcdef", score_cutoff=80))


if __name__ == "__main__":
    unittest.main()
