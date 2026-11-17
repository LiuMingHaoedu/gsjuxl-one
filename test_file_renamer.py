from __future__ import annotations

import shutil
import unittest
from pathlib import Path

import file_renamer as fr

TEST_TMP_ROOT = Path(__file__).resolve().parent / "_test_tmp"


class RenameTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TMP_ROOT.mkdir(exist_ok=True)
        self.root = TEST_TMP_ROOT / self._testMethodName / "videos"
        if self.root.parent.exists():
            shutil.rmtree(self.root.parent)
        self.root.mkdir(parents=True)

    def tearDown(self) -> None:
        if self.root.parent.exists():
            shutil.rmtree(self.root.parent, ignore_errors=True)

    def write(self, name: str, content: bytes = b"video") -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_preview_does_not_rename(self) -> None:
        path = self.write("正在播放 电影.mp4")
        items = fr.collect_rename_items([self.root])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].new_name, "电影.mp4")
        self.assertTrue(path.exists())
        self.assertFalse((self.root / "电影.mp4").exists())

    def test_apply_removes_keyword_and_keeps_content(self) -> None:
        path = self.write("正在播放 电影.mp4", b"same-data")
        items = fr.collect_rename_items([self.root])
        records = fr.apply_renames(items, report_file=self.root / "报告.csv")
        self.assertFalse(path.exists())
        target = self.root / "电影.mp4"
        self.assertTrue(target.exists())
        self.assertEqual(target.read_bytes(), b"same-data")
        self.assertEqual(records[0]["状态"], "已改名")
        self.assertTrue((self.root / "报告.csv").exists())

    def test_collision_gets_suffix(self) -> None:
        self.write("电影.mp4")
        self.write("正在播放 电影.mp4")
        items = fr.collect_rename_items([self.root])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].new_name, "电影 (2).mp4")

    def test_leading_and_trailing_separators_cleaned(self) -> None:
        self.write("正在播放 - 电影 - 正在播放.mp4")
        items = fr.collect_rename_items([self.root])
        self.assertEqual(items[0].new_name, "电影.mp4")

    def test_leading_fullwidth_colon_is_removed(self) -> None:
        self.write("正在播放：宝宝，对不起.mp4")
        items = fr.collect_rename_items([self.root])
        self.assertEqual(items[0].new_name, "宝宝，对不起.mp4")

    def test_all_colons_are_removed(self) -> None:
        self.write("标题：正在播放：副标题.mp4")
        items = fr.collect_rename_items([self.root])
        self.assertEqual(items[0].new_name, "标题副标题.mp4")

    def test_colon_only_file_with_optional_scope(self) -> None:
        self.write("普通：标题.mp4")
        self.assertEqual(fr.collect_rename_items([self.root]), [])
        items = fr.collect_rename_items([self.root], all_colons=True)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].new_name, "普通标题.mp4")

    def test_trailing_long_number_removed(self) -> None:
        self.write("电影标题_123456789.mp4")
        self.write("另一部 - 987654321.mp4")
        items = fr.collect_rename_items([self.root], strip_long_numbers=True)
        self.assertEqual({i.new_name for i in items}, {"电影标题.mp4", "另一部.mp4"})

    def test_year_inside_title_not_removed(self) -> None:
        self.write("2024年度电影_123456789.mp4")
        items = fr.collect_rename_items([self.root], strip_long_numbers=True)
        self.assertEqual(items[0].new_name, "2024年度电影.mp4")

    def test_keyword_only_name(self) -> None:
        self.write("正在播放.mp4")
        items = fr.collect_rename_items([self.root])
        self.assertEqual(items[0].new_name, "视频.mp4")

    def test_non_recursive(self) -> None:
        self.write("正在播放 外层.mp4")
        self.write("子目录/正在播放 内层.mp4")
        items = fr.collect_rename_items([self.root], recursive=False)
        self.assertEqual([item.new_name for item in items], ["外层.mp4"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
