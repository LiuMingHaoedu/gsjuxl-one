from __future__ import annotations

import io
import random
import shutil
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import novel_merger as nm

TEST_TMP_ROOT = Path(__file__).resolve().parent / "_test_tmp"


def novel_text(seed: str, paragraphs: int = 80, chars: int = 140) -> str:
    alphabet = "天地玄黄宇宙洪荒日月盈昃辰宿列张寒来暑往秋收冬藏闰余成岁律吕调阳云腾致雨露结为霜金生丽水玉出昆冈"
    rng = random.Random(seed)
    lines = [f"第1章 {seed}的故事"]
    for i in range(paragraphs):
        body = "".join(rng.choice(alphabet) for _ in range(chars))
        body = "，".join(body[k:k + 30] for k in range(0, len(body), 30))
        lines.append(f"{seed}第{i}段，{body}。")
    lines.append("全文完")
    return "\n".join(lines)


class MergerTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TMP_ROOT.mkdir(exist_ok=True)
        self.root = TEST_TMP_ROOT / self._testMethodName / "小说"
        if self.root.parent.exists():
            shutil.rmtree(self.root.parent)
        self.root.mkdir(parents=True)
        self.output = self.root / "合并结果"

    def tearDown(self) -> None:
        if self.root.parent.exists():
            shutil.rmtree(self.root.parent, ignore_errors=True)

    def write(self, folder: str, name: str, text: str, encoding: str = "utf-8") -> Path:
        directory = self.root / folder
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text(text, encoding=encoding)
        return path

    def run_cli(self, *extra: str) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            code = nm.main([
                "--root", str(self.root),
                "--output", str(self.output),
                *extra,
            ])
        return code, out.getvalue()

    def output_names(self) -> set[str]:
        ignored = {nm.REPORT_CSV, nm.REPORT_TXT}
        return {p.name for p in self.output.glob("*.txt") if p.name not in ignored}

    def test_prefix_name_keeps_clean_name(self) -> None:
        text = novel_text("力量")
        self.write("1", "力量.txt", text)
        self.write("2", "前缀力量.txt", text)

        code, log = self.run_cli()
        self.assertEqual(code, 0, log)
        self.assertEqual(self.output_names(), {"力量.txt"})
        self.assertEqual((self.output / "力量.txt").read_text(encoding="utf-8"), text)
        self.assertTrue((self.output / nm.REPORT_CSV).exists())

    def test_noisy_copy_name_loses_to_clean_name(self) -> None:
        text = novel_text("归途")
        self.write("1", "归途 作者：甲.txt", text)
        self.write("2", "归途 作者：甲_d5_t19.txt", text)

        code, log = self.run_cli()
        self.assertEqual(code, 0, log)
        self.assertEqual(self.output_names(), {"归途 作者：甲.txt"})

    def test_complete_marker_wins(self) -> None:
        text = novel_text("长夜")
        self.write("1", "长夜.txt", text)
        self.write("2", "长夜（全） 作者：乙.txt", text)

        code, log = self.run_cli()
        self.assertEqual(code, 0, log)
        self.assertEqual(self.output_names(), {"长夜（全） 作者：乙.txt"})

    def test_different_volumes_are_not_merged(self) -> None:
        self.write("1", "星辰 第一卷 作者：丙.txt", novel_text("星辰上卷"))
        self.write("2", "星辰 第二卷 作者：丙.txt", novel_text("星辰下卷"))

        code, log = self.run_cli()
        self.assertEqual(code, 0, log)
        self.assertEqual(len(self.output_names()), 2, log)

    def test_longer_version_wins(self) -> None:
        short = novel_text("山海", paragraphs=60)
        long = short + "\n第61章 尾声\n" + ("多年以后，他们再次回到那座小城。\n" * 30)
        self.write("1", "山海 作者：丁.txt", short)
        self.write("2", "山海 作者：丁（全）.txt", long)

        code, log = self.run_cli()
        self.assertEqual(code, 0, log)
        self.assertEqual(len(self.output_names()), 1, log)
        out = next(
            p for p in self.output.glob("*.txt")
            if p.name not in {nm.REPORT_CSV, nm.REPORT_TXT}
        ).read_text(encoding="utf-8")
        self.assertIn("第61章", out)

    def test_near_duplicate_with_small_edits(self) -> None:
        text = novel_text("微光")
        lines = text.splitlines()
        for i in (5, 10, 15):
            lines[i] = lines[i].replace("天", "地", 1)
        changed = "\n".join(lines)
        self.write("1", "微光.txt", text)
        self.write("2", "微光 修订版.txt", changed)

        code, log = self.run_cli()
        self.assertEqual(code, 0, log)
        self.assertEqual(len(self.output_names()), 1, log)

    def test_different_books_with_same_filename_both_kept(self) -> None:
        self.write("1", "同名.txt", novel_text("甲书"))
        self.write("2", "同名.txt", novel_text("乙书"))

        code, log = self.run_cli()
        self.assertEqual(code, 0, log)
        self.assertEqual(self.output_names(), {"同名.txt", "同名 (2).txt"})

    def test_gb18030_and_utf8_are_same_book(self) -> None:
        text = novel_text("编码")
        self.write("1", "编码.txt", text, encoding="utf-8")
        self.write("2", "编码 作者：戊.txt", text, encoding="gb18030")

        code, log = self.run_cli()
        self.assertEqual(code, 0, log)
        self.assertEqual(len(self.output_names()), 1, log)

    def test_dry_run_creates_nothing(self) -> None:
        text = novel_text("预览")
        self.write("1", "预览.txt", text)
        self.write("2", "前缀预览.txt", text)
        self.output.mkdir()
        (self.output / "已有.txt").write_text("old", encoding="utf-8")

        code, log = self.run_cli("--dry-run")
        self.assertEqual(code, 0, log)
        self.assertEqual({p.name for p in self.output.iterdir()}, {"已有.txt"})

    def test_refuses_output_inside_source(self) -> None:
        self.write("1", "书.txt", novel_text("保护"))
        out = self.root / "1" / "输出"
        result = nm.main(["--root", str(self.root), "--output", str(out)])
        self.assertEqual(result, 2)

    def test_different_names_similar_content_shorter_title_wins(self) -> None:
        text = novel_text("余温")
        self.write("1", "余温.txt", text)
        self.write("2", "新余温.txt", text)

        code, log = self.run_cli()
        self.assertEqual(code, 0, log)
        self.assertEqual(self.output_names(), {"余温.txt"})

    def test_name_parser_keeps_author(self) -> None:
        info = nm.analyze_name("长夜（全） 作者：小明.txt")
        self.assertEqual(info.title, "长夜")
        self.assertEqual(info.author, "小明")
        self.assertTrue(info.is_complete)

    def test_merge_library_api_reports_progress(self) -> None:
        text = novel_text("接口")
        self.write("1", "接口.txt", text)
        self.write("2", "前缀接口.txt", text)
        phases: list[str] = []

        def progress(phase: str, fraction: float, message: str) -> None:
            phases.append(phase)
            self.assertGreaterEqual(fraction, 0.0)
            self.assertLessEqual(fraction, 1.0)

        outcome = nm.merge_library(
            self.root, self.output, progress=progress,
        )
        self.assertEqual(len(outcome.groups), 1)
        self.assertEqual(outcome.stats.scanned, 2)
        self.assertTrue((self.output / "接口.txt").exists())
        self.assertIn("scan", phases)
        self.assertIn("compare", phases)
        self.assertIn("write", phases)
        self.assertEqual(phases[-1], "done")

    def test_merge_library_api_can_cancel(self) -> None:
        self.write("1", "取消.txt", novel_text("取消"))
        with self.assertRaises(nm.OperationCancelled):
            nm.merge_library(self.root, self.output, cancel=lambda: True)
        self.assertFalse(self.output.exists())

    def test_root_txt_does_not_break_default_sources(self) -> None:
        text = novel_text("根目录")
        self.write("1", "根目录.txt", text)
        self.write("2", "前缀根目录.txt", text)
        (self.root / "违禁词.txt").write_text("null\n", encoding="utf-8")

        code, log = self.run_cli()
        self.assertEqual(code, 0, log)
        self.assertEqual(self.output_names(), {"根目录.txt"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
