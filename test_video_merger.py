from __future__ import annotations

import shutil
import struct
import unittest
from pathlib import Path

import novel_merger as nm
import video_merger as vm

TEST_TMP_ROOT = Path(__file__).resolve().parent / "_test_tmp"


def box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def make_mp4(duration: float = 60.0, width: int = 1920, height: int = 1080,
             payload: bytes = b"video-data") -> bytes:
    mvhd = bytearray(20)
    struct.pack_into(">I", mvhd, 0, 0)          # version + flags
    struct.pack_into(">I", mvhd, 12, 1000)      # timescale
    struct.pack_into(">I", mvhd, 16, int(duration * 1000))
    tkhd = bytearray(84)
    struct.pack_into(">I", tkhd, 0, 0)
    struct.pack_into(">I", tkhd, 12, 1)         # track id
    struct.pack_into(">I", tkhd, 20, int(duration * 1000))
    struct.pack_into(">I", tkhd, 76, width << 16)
    struct.pack_into(">I", tkhd, 80, height << 16)
    ftyp = box(b"ftyp", b"isom\x00\x00\x02\x00isomiso2mp41")
    moov = box(b"moov", box(b"mvhd", bytes(mvhd)) + box(b"trak", box(b"tkhd", bytes(tkhd))))
    return ftyp + moov + box(b"mdat", payload)


class VideoMergerTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TMP_ROOT.mkdir(exist_ok=True)
        self.root = TEST_TMP_ROOT / self._testMethodName / "videos"
        if self.root.parent.exists():
            shutil.rmtree(self.root.parent)
        self.root.mkdir(parents=True)
        self.output = self.root / "out"

    def tearDown(self) -> None:
        if self.root.parent.exists():
            shutil.rmtree(self.root.parent, ignore_errors=True)

    def write(self, folder: str, name: str, data: bytes) -> Path:
        path = self.root / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_parse_mp4_metadata(self) -> None:
        path = self.write("1", "sample.mp4", make_mp4(125, 1920, 1080))
        meta = vm.parse_mp4_metadata(path)
        self.assertAlmostEqual(meta.duration or 0, 125.0, places=2)
        self.assertEqual((meta.width, meta.height), (1920, 1080))

    def test_exact_duplicates_keep_cleaner_name(self) -> None:
        data = make_mp4(60, 1920, 1080, b"same")
        self.write("1", "力量.mp4", data)
        self.write("2", "前缀力量.mp4", data)

        result = vm.merge_videos(self.root, self.output, recursive=True)
        self.assertEqual(len(result.groups), 1)
        self.assertEqual(len(list(self.output.glob("*.mp4"))), 1)
        self.assertTrue((self.output / "力量.mp4").exists())
        self.assertTrue((self.output / vm.DEFAULT_REPORT_CSV).exists())
        self.assertTrue((self.output / vm.DEFAULT_REPORT_TXT).exists())

    def test_different_resolution_keeps_higher_quality(self) -> None:
        self.write("a", "电影故事 1080p.mp4", make_mp4(120, 1920, 1080, b"hd" * 200))
        self.write("b", "电影故事 720p.mp4", make_mp4(120, 1280, 720, b"sd"))

        result = vm.merge_videos(self.root, self.output, recursive=True)
        self.assertEqual(len(result.groups), 1)
        self.assertEqual({p.name for p in self.output.glob("*.mp4")}, {"电影故事 1080p.mp4"})
        self.assertTrue((self.output / vm.DEFAULT_REPORT_CSV).exists())

    def test_preview_generates_report_without_copying(self) -> None:
        self.write("a", "电影故事 1080p.mp4", make_mp4(120, 1920, 1080, b"hd" * 200))
        self.write("b", "电影故事 720p.mp4", make_mp4(120, 1280, 720, b"sd"))
        report = self.root.parent / "视频检测报告.csv"
        summary = self.root.parent / "视频检测报告_摘要.txt"

        result = vm.merge_videos(
            self.root, self.output, recursive=True, dry_run=True,
            report_file=report, summary_file=summary,
        )
        self.assertFalse(self.output.exists())
        self.assertTrue(report.exists())
        self.assertTrue(summary.exists())
        text = report.read_text(encoding="utf-8-sig")
        self.assertIn("重复", text)
        self.assertIn("电影故事 720p.mp4", text)
        self.assertEqual(len(result.groups), 1)

    def test_quarantine_moves_only_duplicate_files(self) -> None:
        winner = self.write("a", "电影故事 1080p.mp4", make_mp4(120, 1920, 1080, b"hd" * 200))
        duplicate = self.write("b", "电影故事 720p.mp4", make_mp4(120, 1280, 720, b"sd"))
        result = vm.merge_videos(self.root, self.output, recursive=True, dry_run=True)
        quarantine = self.root.parent / "视频隔离区"
        log = self.root.parent / "视频隔离记录.csv"

        records = vm.quarantine_duplicates(result.groups, quarantine, log_file=log)
        self.assertTrue(winner.exists())
        self.assertFalse(duplicate.exists())
        self.assertTrue((quarantine / duplicate.name).exists())
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["是否成功"], "已隔离")
        self.assertTrue(log.exists())

    def test_different_episodes_are_not_merged(self) -> None:
        self.write("a", "剧集 第1集.mp4", make_mp4(120, 1920, 1080, b"ep1"))
        self.write("b", "剧集 第2集.mp4", make_mp4(120, 1920, 1080, b"ep2"))

        result = vm.merge_videos(self.root, self.output, recursive=True)
        self.assertEqual(len(result.groups), 2)
        self.assertEqual(len(list(self.output.glob("*.mp4"))), 2)

    def test_no_metadata_only_exact_hash(self) -> None:
        # 不是合法 MP4，解析不到时长/分辨率，只允许完整哈希判重。
        self.write("a", "同名.mp4", b"one")
        self.write("b", "同名.mp4", b"two")

        result = vm.merge_videos(self.root, self.output, recursive=True)
        self.assertEqual(len(result.groups), 2)
        self.assertEqual({p.name for p in self.output.glob("*.mp4")},
                         {"同名.mp4", "同名 (2).mp4"})

    def test_same_name_same_metadata_but_different_content_is_not_near_merged(self) -> None:
        self.write("a", "电影 1080p.mp4", make_mp4(120, 1920, 1080, b"one"))
        self.write("b", "电影 1080p.mp4", make_mp4(120, 1920, 1080, b"two"))
        videos, _ = vm.scan_videos(self.root, self.output, True)
        videos, _ = vm.calculate_hashes(videos)
        self.assertIsNone(vm.compare_videos(videos[0], videos[1]))

    def test_cancel_before_scan(self) -> None:
        self.write("a", "取消.mp4", make_mp4(payload=b"cancel"))
        with self.assertRaises(nm.OperationCancelled):
            vm.merge_videos(self.root, self.output, cancel=lambda: True)
        self.assertFalse(self.output.exists())

    def test_short_video_name_does_not_merge_without_metadata(self) -> None:
        # 没有元数据时，即使名字相似也不能合并，避免误删不同视频。
        self.write("a", "短片.mp4", b"aaa")
        self.write("b", "前缀短片.mp4", b"bbb")
        result = vm.merge_videos(self.root, self.output, recursive=True)
        self.assertEqual(len(result.groups), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
