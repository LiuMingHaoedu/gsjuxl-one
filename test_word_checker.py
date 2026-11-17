from __future__ import annotations

import shutil
import unittest
from pathlib import Path

import word_checker as wc

TEST_TMP_ROOT = Path(__file__).resolve().parent / "_test_tmp"


class WordCheckerTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TMP_ROOT.mkdir(exist_ok=True)
        self.root = TEST_TMP_ROOT / self._testMethodName / "data"
        if self.root.parent.exists():
            shutil.rmtree(self.root.parent)
        self.root.mkdir(parents=True)
        self.words = self.root.parent / "违禁词.txt"
        self.words.write_text("# 注释\nnull\n本章节暂未购买\n请刷新重试\n", encoding="utf-8")

    def tearDown(self) -> None:
        if self.root.parent.exists():
            shutil.rmtree(self.root.parent, ignore_errors=True)

    def write(self, name: str, text: str, encoding: str = "utf-8") -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding=encoding)
        return path

    def test_load_words_ignores_comments_and_duplicates(self) -> None:
        words = wc.load_words(self.words)
        self.assertEqual(words, ["null", "本章节暂未购买", "请刷新重试"])

    def test_scan_case_insensitive_and_line_numbers(self) -> None:
        file = self.write("a.txt", "第一行\nNULL 和 Null\n第三行 null\n本章节暂未购买\n")
        result = wc.scan_paths(
            [file], self.words,
            ignore_case=True,
            report_file=self.root.parent / "检测结果.csv",
            summary_file=self.root.parent / "检测摘要.txt",
        )
        self.assertEqual(result.scanned, 1)
        self.assertEqual(len(result.matched), 1)
        hit = next(item for item in result.matched[0].hits if item.word == "null")
        self.assertEqual(hit.count, 3)
        self.assertEqual(hit.lines, (2, 3))
        self.assertTrue(result.report and result.report.exists())
        self.assertTrue(result.summary and result.summary.exists())

    def test_longest_word_wins(self) -> None:
        words_file = self.root.parent / "short.txt"
        words_file.write_text("ab\nabc\n", encoding="utf-8")
        file = self.write("b.txt", "abc ab\n")
        result = wc.scan_paths([file], words_file)
        counts = {hit.word: hit.count for hit in result.matched[0].hits}
        self.assertEqual(counts, {"abc": 1, "ab": 1})

    def test_no_hit(self) -> None:
        file = self.write("c.txt", "正常内容\n没有命中词\n")
        result = wc.scan_paths([file], self.words)
        self.assertEqual(result.matched, [])
        self.assertEqual(result.total_hits, 0)

    def test_report_excludes_itself_from_scan(self) -> None:
        self.write("d.txt", "null\n")
        report = self.root / "检测结果.csv"
        report.write_text("旧报告", encoding="utf-8")
        result = wc.scan_paths(
            [self.root], self.words,
            report_file=report,
            summary_file=self.root / "检测摘要.txt",
        )
        self.assertEqual(result.scanned, 1)
        self.assertEqual(report.read_text(encoding="utf-8-sig").count("null"), 1)

    def test_quarantine_moves_file_and_writes_log(self) -> None:
        file = self.write("e.txt", "null\n")
        result = wc.scan_paths([file], self.words)
        quarantine = self.root.parent / "隔离区"
        records = wc.process_candidates(
            result,
            mode="quarantine",
            quarantine_dir=quarantine,
            log_file=self.root.parent / "删除记录.csv",
        )
        self.assertFalse(file.exists())
        self.assertTrue((quarantine / "e.txt").exists())
        self.assertEqual(records[0]["是否成功"], "已隔离")
        self.assertTrue((self.root.parent / "删除记录.csv").exists())

    def test_permanent_delete_only_when_asked(self) -> None:
        file = self.write("f.txt", "请刷新重试\n")
        result = wc.scan_paths([file], self.words)
        wc.process_candidates(result, mode="delete")
        self.assertFalse(file.exists())

    def test_threshold_candidates(self) -> None:
        file = self.write("g.txt", "null\nnull\n")
        result = wc.scan_paths([file], self.words, delete_threshold=3)
        self.assertEqual(result.candidates(), [])
        result2 = wc.scan_paths([file], self.words, delete_threshold=2)
        self.assertEqual(len(result2.candidates()), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
