#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MP4 视频合并去重核心
====================

在同名 / 同内容 / 同时长 / 同分辨率等条件之间做综合判断，把不同文件夹里的
重复视频保留质量最好的一份，复制到最终文件夹。不会拼接视频，也不会修改源文件。

MP4 元数据优先用内置的 ISO-BMFF 解析器读取（时长、分辨率），无需 ffmpeg；
如果系统恰好装了 ffprobe，则在内置解析失败时作为兜底。

安全策略：
  * 只有二进制完全相同，或“文件名/集数不冲突 + 时长一致 + 视频元数据可读”时，
    才会判为近似重复；
  * 没有元数据的文件只按完整哈希判重，避免把同名不同内容的视频误合并；
  * 默认复制，绝不移动/删除源文件。
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
import struct
import subprocess
import time
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable, Sequence

from novel_merger import IGNORED_DIR_NAMES, OperationCancelled, safe_filename, unique_output_name

VIDEO_SUFFIXES = {".mp4"}
DEFAULT_OUTPUT_DIR = "视频合并结果"
DEFAULT_REPORT_CSV = "视频合并报告.csv"
DEFAULT_REPORT_TXT = "视频合并摘要.txt"
HASH_CHUNK = 4 * 1024 * 1024
COPY_CHUNK = 8 * 1024 * 1024

# 清晰度 / 编码等技术词，不参与标题比对
QUALITY_RE = re.compile(
    r"(?ix)"
    r"(?:2160p|1080p|720p|480p|360p|4k|8k|uhd|fhd|hdr(?:10)?|10bit|8bit|"
    r"x265|x264|h\.?265|h\.?264|hevc|avc|av1|web[- .]?dl|webrip|bluray|"
    r"bdrip|remux|aac|flac|dts(?:-hd)?|truehd|5\.1|7\.1|60fps|30fps)"
)
TECH_BRACKET_RE = re.compile(
    r"[\[\(【（][^\]\)】）]{0,80}?"
    r"(?:2160p|1080p|720p|480p|4k|8k|x264|x265|h264|h265|hevc|hdr|10bit|"
    r"aac|dts|中字|中文字幕|国粤|国语|粤语|双语|无水印|高清|蓝光)"
    r"[^\]\)】）]{0,80}?[\]\)】）]",
    re.I,
)
COPY_SUFFIX_RE = re.compile(
    r"(?:\s*[-—–_]\s*副本|\(\s*\d+\s*\)|（\s*\d+\s*）|\[\s*副本\s*\]|【\s*副本\s*】)\s*$"
)
EXTRAS_RE = re.compile(r"(预告|花絮|番外|彩蛋|片段|解说|幕后|制作特辑|特典)", re.I)
EPISODE_PATTERNS = (
    re.compile(r"第\s*0*(\d{1,4})\s*[集话期部季]"),
    re.compile(r"\bs(\d{1,2})e(\d{1,3})\b", re.I),
    re.compile(r"\bep?\s*0*(\d{1,3})\b", re.I),
    re.compile(r"(?<!\d)(\d{1,3})\s*[集话期]"),
)


@dataclass(frozen=True)
class VideoMeta:
    duration: float | None = None
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True)
class VideoName:
    raw: str
    key: str
    episodes: tuple[str, ...]
    extras: tuple[str, ...]
    score: int
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class VideoFile:
    path: Path
    source: str
    source_rank: int
    size: int
    name: VideoName
    meta: VideoMeta
    exact_hash: str = ""


@dataclass
class VideoGroup:
    videos: list[VideoFile]
    winner: VideoFile | None = None
    output_name: str = ""
    similarities: dict[Path, float] = field(default_factory=dict)
    reason: str = ""


@dataclass
class VideoOutcome:
    root: Path
    output: Path
    groups: list[VideoGroup]
    videos: list[VideoFile]
    skipped: list[tuple[Path, str]]
    elapsed: float
    copied_bytes: int
    renamed: int
    dry_run: bool
    report_file: Path | None = None
    summary_file: Path | None = None


# ---------------------------------------------------------------------------
# 文件名
# ---------------------------------------------------------------------------

def analyze_video_name(path_or_name: str | Path) -> VideoName:
    raw = Path(path_or_name).stem
    text = unicodedata.normalize("NFKC", raw).replace("\u3000", " ").replace("\xa0", " ")
    text = re.sub(r"[\t\r\n]+", " ", text)
    had_edge = text != text.strip()
    text = re.sub(r"[ ]{2,}", " ", text).strip()

    reasons: list[str] = []
    score = 50
    if had_edge:
        score -= 4
        reasons.append("首尾空格")
    if re.search(r" {2,}", raw):
        score -= 3
        reasons.append("多余空格")

    copy_hit = bool(COPY_SUFFIX_RE.search(text))
    if copy_hit:
        score -= 26
        reasons.append("副本/重复标记")
    text = COPY_SUFFIX_RE.sub("", text)
    text = TECH_BRACKET_RE.sub(" ", text)
    text = QUALITY_RE.sub(" ", text)
    text = re.sub(r"[\[\]【】()（）{}<>《》]+", " ", text)
    text = re.sub(r"[ _./\\|:：;；,，、\-–—+&]+", " ", text).strip()
    key = re.sub(r"[^\w\u3400-\u9fff]+", "", text.casefold())
    if not key:
        key = re.sub(r"[^\w\u3400-\u9fff]+", "", raw.casefold())

    episodes: set[str] = set()
    for pattern in EPISODE_PATTERNS:
        for match in pattern.finditer(text):
            groups = [part for part in match.groups() if part]
            episodes.add("e" + "-".join(groups))
    extras = {part for part in EXTRAS_RE.findall(text)}
    if not key:
        score -= 20
        reasons.append("标题为空")
    if len(key) > 80:
        score -= 6
        reasons.append("标题过长")
    if extras:
        score += 2

    return VideoName(
        raw=raw,
        key=key,
        episodes=tuple(sorted(episodes)),
        extras=tuple(sorted(extras)),
        score=score,
        reasons=tuple(reasons),
    )


def names_conflict(a: VideoName, b: VideoName) -> bool:
    if a.episodes and b.episodes and a.episodes != b.episodes:
        return True
    if bool(a.extras) != bool(b.extras):
        return True
    return False


def name_similarity(a: VideoName, b: VideoName) -> float:
    if not a.key or not b.key:
        return 0.0
    if a.key == b.key:
        return 1.0
    if names_conflict(a, b):
        return 0.0
    shorter, longer = sorted((a.key, b.key), key=len)
    if len(shorter) >= 4 and shorter in longer:
        # “力量” vs “前缀力量”这类前缀差异，只有在标题本身有足够长度时才算。
        if len(shorter) / max(1, len(longer)) >= 0.55:
            return 0.94
    return SequenceMatcher(None, a.key, b.key, autojunk=False).ratio()


# ---------------------------------------------------------------------------
# MP4 元数据解析
# ---------------------------------------------------------------------------

def _read_box_header(fh, file_size: int):
    start = fh.tell()
    head = fh.read(8)
    if len(head) < 8:
        return None
    size = int.from_bytes(head[:4], "big")
    box_type = head[4:8]
    header_size = 8
    if size == 1:
        extra = fh.read(8)
        if len(extra) < 8:
            return None
        size = int.from_bytes(extra, "big")
        header_size = 16
    elif size == 0:
        size = file_size - start
    if size < header_size or start + size > file_size:
        return None
    return box_type, size, header_size, start


def _parse_mvhd(data: bytes) -> tuple[float | None, int | None]:
    if len(data) < 4:
        return None, None
    version = data[0]
    try:
        if version == 1:
            if len(data) < 32:
                return None, None
            timescale = struct.unpack_from(">I", data, 20)[0]
            duration = struct.unpack_from(">Q", data, 24)[0]
        else:
            if len(data) < 20:
                return None, None
            timescale = struct.unpack_from(">I", data, 12)[0]
            duration = struct.unpack_from(">I", data, 16)[0]
    except struct.error:
        return None, None
    if not timescale:
        return None, None
    return duration / timescale, timescale


def _parse_tkhd(data: bytes) -> tuple[int | None, int | None]:
    if len(data) < 4:
        return None, None
    version = data[0]
    try:
        if version == 1:
            if len(data) < 96:
                return None, None
            width, height = struct.unpack_from(">II", data, 88)
        else:
            if len(data) < 84:
                return None, None
            width, height = struct.unpack_from(">II", data, 76)
    except struct.error:
        return None, None
    return int(width / 65536), int(height / 65536)


def parse_mp4_metadata(path: Path) -> VideoMeta:
    """读取 MP4 的 mvhd / tkhd；只读文件头部，不加载整个视频。"""
    try:
        file_size = path.stat().st_size
        if file_size < 32:
            return VideoMeta()
        with path.open("rb") as fh:
            position = 0
            while position + 8 <= file_size:
                fh.seek(position)
                header = _read_box_header(fh, file_size)
                if not header:
                    break
                box_type, box_size, header_size, start = header
                payload_start = start + header_size
                payload_size = box_size - header_size
                if box_type == b"moov":
                    duration = None
                    width = None
                    height = None
                    child_pos = payload_start
                    moov_end = start + box_size
                    while child_pos + 8 <= moov_end:
                        fh.seek(child_pos)
                        child = _read_box_header(fh, file_size)
                        if not child:
                            break
                        ctype, csize, cheader, cstart = child
                        cpayload = cstart + cheader
                        cpayload_size = csize - cheader
                        if ctype == b"mvhd":
                            fh.seek(cpayload)
                            duration, _ = _parse_mvhd(fh.read(min(cpayload_size, 256)))
                        elif ctype == b"trak":
                            # 在 trak 里只找 tkhd，不深入其他大 box。
                            trak_end = cstart + csize
                            tpos = cpayload
                            while tpos + 8 <= trak_end:
                                fh.seek(tpos)
                                sub = _read_box_header(fh, file_size)
                                if not sub:
                                    break
                                stype, ssize, sheader, sstart = sub
                                if stype == b"tkhd":
                                    fh.seek(sstart + sheader)
                                    w, h = _parse_tkhd(fh.read(min(ssize - sheader, 256)))
                                    if w and h and w > (width or 0) and h > (height or 0):
                                        width, height = w, h
                                tpos = sstart + ssize
                        child_pos = cstart + csize
                    if duration is not None or width:
                        return VideoMeta(duration=duration, width=width, height=height)
                position = start + box_size
    except (OSError, ValueError):
        pass
    return VideoMeta()


def ffprobe_metadata(path: Path) -> VideoMeta:
    """可选的兜底：仅在系统有 ffprobe 时使用。"""
    exe = shutil.which("ffprobe")
    if not exe:
        return VideoMeta()
    command = [
        exe, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "format=duration:stream=width,height",
        "-of", "json", str(path),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=20,
                                encoding="utf-8", errors="replace")
        if result.returncode != 0:
            return VideoMeta()
        payload = json.loads(result.stdout or "{}")
        duration = payload.get("format", {}).get("duration")
        streams = payload.get("streams") or []
        width = streams[0].get("width") if streams else None
        height = streams[0].get("height") if streams else None
        return VideoMeta(
            duration=float(duration) if duration not in (None, "") else None,
            width=int(width) if width else None,
            height=int(height) if height else None,
        )
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return VideoMeta()


def probe_metadata(path: Path) -> VideoMeta:
    meta = parse_mp4_metadata(path)
    if meta.duration is not None and meta.width:
        return meta
    fallback = ffprobe_metadata(path)
    if fallback.duration is not None or fallback.width:
        return fallback
    return meta


# ---------------------------------------------------------------------------
# 扫描与哈希
# ---------------------------------------------------------------------------

def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _ignored(path: Path, root: Path) -> bool:
    try:
        parts = path.relative_to(root).parts[:-1]
    except ValueError:
        parts = path.parts[:-1]
    if any(part.startswith("视频隔离区") for part in parts):
        return True
    return any(part.casefold() in {name.casefold() for name in IGNORED_DIR_NAMES}
               for part in parts)


def collect_video_files(root: Path, recursive: bool, output: Path) -> list[Path]:
    iterator = root.rglob("*") if recursive else root.glob("*")
    files: list[Path] = []
    for path in iterator:
        if not path.is_file() or path.suffix.casefold() not in VIDEO_SUFFIXES:
            continue
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if output.exists() and _is_relative_to(resolved, output.resolve()):
            continue
        if _ignored(path, root):
            continue
        files.append(path)
    return sorted(files, key=lambda item: item.name.casefold())


def scan_videos(
    root: Path,
    output: Path,
    recursive: bool,
    *,
    progress: Callable[[int, int, Path], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> tuple[list[VideoFile], list[tuple[Path, str]]]:
    files = collect_video_files(root, recursive, output)
    videos: list[VideoFile] = []
    skipped: list[tuple[Path, str]] = []
    total = len(files)
    for index, path in enumerate(files, 1):
        if cancel and cancel():
            raise OperationCancelled()
        if progress:
            progress(index, total, path)
        try:
            size = path.stat().st_size
            if size <= 0:
                skipped.append((path, "空文件"))
                continue
            name = analyze_video_name(path)
            meta = probe_metadata(path)
            try:
                source = str(path.parent.relative_to(root)) if path.parent != root else "."
            except ValueError:
                source = path.parent.name
            videos.append(VideoFile(
                path=path,
                source=source or ".",
                source_rank=0,
                size=size,
                name=name,
                meta=meta,
            ))
        except OSError as exc:
            skipped.append((path, str(exc)))
    return videos, skipped


def calculate_hashes(
    videos: list[VideoFile],
    *,
    progress: Callable[[int, int, Path], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> tuple[list[VideoFile], int]:
    """只对大小相同的文件计算完整 SHA-256；不同大小必不可能是二进制相同。"""
    size_counts = Counter(video.size for video in videos)
    candidates = [video for video in videos if size_counts[video.size] > 1]
    total_bytes = sum(video.size for video in candidates)
    done_bytes = 0
    hashed: list[VideoFile] = []
    by_path = {video.path: index for index, video in enumerate(videos)}

    for video in candidates:
        if cancel and cancel():
            raise OperationCancelled()
        digest = hashlib.sha256()
        try:
            with video.path.open("rb") as fh:
                while True:
                    if cancel and cancel():
                        raise OperationCancelled()
                    chunk = fh.read(HASH_CHUNK)
                    if not chunk:
                        break
                    digest.update(chunk)
                    done_bytes += len(chunk)
                    if progress:
                        progress(done_bytes, max(1, total_bytes), video.path)
            videos[by_path[video.path]] = VideoFile(
                path=video.path,
                source=video.source,
                source_rank=video.source_rank,
                size=video.size,
                name=video.name,
                meta=video.meta,
                exact_hash=digest.hexdigest(),
            )
        except OSError:
            continue
    return videos, total_bytes


# ---------------------------------------------------------------------------
# 相似度与分组
# ---------------------------------------------------------------------------

def _aspect_ok(a: VideoMeta, b: VideoMeta) -> bool:
    if not (a.width and a.height and b.width and b.height):
        return True
    ratio_a = a.width / a.height
    ratio_b = b.width / b.height
    return abs(ratio_a - ratio_b) / max(ratio_a, ratio_b) <= 0.08


def _duration_ok(a: VideoMeta, b: VideoMeta) -> bool:
    if a.duration is None or b.duration is None:
        return False
    tolerance = max(2.5, 0.012 * max(a.duration, b.duration))
    return abs(a.duration - b.duration) <= tolerance


def compare_videos(a: VideoFile, b: VideoFile) -> float | None:
    if a.exact_hash and a.exact_hash == b.exact_hash:
        return 1.0
    # 同大小文件已经算过完整哈希；哈希不同说明二进制确实不同，不再凭元数据合并。
    if a.exact_hash and b.exact_hash and a.exact_hash != b.exact_hash:
        return None
    # 没有视频元数据时只信完整哈希，避免仅凭同名误删不同视频。
    if a.meta.duration is None or b.meta.duration is None:
        return None
    if not (a.meta.width and b.meta.width):
        return None
    # 太短/太通用的标题（如“1”“视频”）不参与近似合并。
    if min(len(a.name.key), len(b.name.key)) < 4:
        return None
    if names_conflict(a.name, b.name):
        return None
    if not _duration_ok(a.meta, b.meta) or not _aspect_ok(a.meta, b.meta):
        return None
    longer = max(a.size, b.size)
    ratio = min(a.size, b.size) / longer
    if ratio < 0.02:
        return None
    similarity = name_similarity(a.name, b.name)
    if similarity >= 0.90:
        return similarity
    if similarity >= 0.78 and ratio >= 0.25:
        return similarity
    if similarity >= 0.62 and ratio >= 0.80:
        return similarity
    return None


def find_video_groups(
    videos: Sequence[VideoFile],
    *,
    progress: Callable[[int, int], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> tuple[list[VideoGroup], list[tuple[int, int, float]]]:
    n = len(videos)
    parent = list(range(n))
    rank = [0] * n

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        if rank[ra] < rank[rb]:
            ra, rb = rb, ra
        parent[rb] = ra
        if rank[ra] == rank[rb]:
            rank[ra] += 1

    links: list[tuple[int, int, float]] = []
    for i in range(n):
        if cancel and cancel():
            raise OperationCancelled()
        for j in range(i + 1, n):
            similarity = compare_videos(videos[i], videos[j])
            if similarity is not None:
                links.append((i, j, similarity))
                union(i, j)
        if progress and ((i + 1) % 10 == 0 or i + 1 == n):
            progress(i + 1, n)

    grouped: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        grouped[find(i)].append(i)
    groups: list[VideoGroup] = []
    for indexes in grouped.values():
        members = [videos[i] for i in indexes]
        group_links = [link for link in links if find(link[0]) == find(indexes[0])]
        similarities: dict[Path, float] = {}
        for i, j, similarity in group_links:
            similarities[videos[i].path] = max(similarities.get(videos[i].path, 0.0), similarity)
            similarities[videos[j].path] = max(similarities.get(videos[j].path, 0.0), similarity)
        groups.append(VideoGroup(videos=members, similarities=similarities))
    groups.sort(key=lambda g: min((v.source.casefold(), v.path.name.casefold()) for v in g.videos))
    return groups, links


def choose_video_winner(group: VideoGroup) -> tuple[VideoFile, str]:
    def quality(video: VideoFile):
        has_meta = 1 if video.meta.duration is not None else 0
        height = video.meta.height or 0
        width = video.meta.width or 0
        duration = video.meta.duration or 0.0
        return (
            has_meta,
            height,
            width,
            duration,
            video.size,
            video.name.score,
            -len(video.name.key),
            -video.path.name.casefold().count("副本"),
        )

    winner = max(group.videos, key=quality)
    if len(group.videos) == 1:
        return winner, "唯一文件，直接保留"
    best_quality = quality(winner)
    if best_quality[1] > max((v.meta.height or 0) for v in group.videos if v is not winner):
        return winner, "分辨率更高，优先保留"
    if best_quality[4] > max((v.size for v in group.videos if v is not winner)):
        return winner, "同分辨率下体积/码率更大，优先保留"
    return winner, "质量接近，保留文件名更干净的一份"


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "未知"
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _format_size(size: int) -> str:
    if size >= 1024 ** 3:
        return f"{size / 1024 ** 3:.2f} GB"
    return f"{size / 1024 ** 2:.1f} MB"


def _validate_output(root: Path, output: Path) -> None:
    if output == root or _is_relative_to(root, output):
        raise ValueError("输出文件夹不能是根目录或根目录的上级文件夹。")
    if output.exists() and not output.is_dir():
        raise ValueError(f"输出路径已存在且不是文件夹：{output}")


def _copy_with_progress(
    source: Path,
    destination: Path,
    callback: Callable[[int], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with source.open("rb") as src, destination.open("wb") as dst:
            while True:
                if cancel and cancel():
                    raise OperationCancelled()
                chunk = src.read(COPY_CHUNK)
                if not chunk:
                    break
                dst.write(chunk)
                if callback:
                    callback(len(chunk))
        shutil.copystat(source, destination)
    except Exception:
        try:
            if destination.exists():
                destination.unlink()
        except OSError:
            pass
        raise


def _report_rows(groups: Sequence[VideoGroup]) -> list[dict[str, str | int]]:
    rows: list[dict[str, str | int]] = []
    for group_no, group in enumerate(groups, 1):
        winner = group.winner
        if winner is None:
            continue
        for video in group.videos:
            is_winner = video is winner
            if len(group.videos) == 1:
                status = "保留"
                note = "唯一文件"
            elif is_winner:
                status = "保留"
                note = group.reason
            else:
                status = "重复"
                note = "与保留版本重复；未复制，可选择隔离"
            rows.append({
                "组号": group_no,
                "处理": status,
                "输出文件名": group.output_name if is_winner else "",
                "来源": video.source,
                "原文件名": video.path.name,
                "大小(字节)": video.size,
                "时长": _format_duration(video.meta.duration),
                "分辨率": f"{video.meta.width}x{video.meta.height}"
                if video.meta.width and video.meta.height else "未知",
                "相似度": "100%" if is_winner
                else f"{group.similarities.get(video.path, 1.0):.0%}",
                "说明": note,
            })
    return rows


def _write_report_file(report_file: Path, rows: Sequence[dict[str, str | int]]) -> None:
    report_file.parent.mkdir(parents=True, exist_ok=True)
    with report_file.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=(
            "组号", "处理", "输出文件名", "来源", "原文件名",
            "大小(字节)", "时长", "分辨率", "相似度", "说明",
        ))
        writer.writeheader()
        writer.writerows(rows)


def _write_summary_file(
    summary_file: Path,
    groups: Sequence[VideoGroup],
    videos: Sequence[VideoFile],
    skipped: Sequence[tuple[Path, str]],
    elapsed: float,
    copied_bytes: int,
    renamed: int,
    dry_run: bool,
) -> None:
    summary_file.parent.mkdir(parents=True, exist_ok=True)
    merged = len(videos) - len(groups)
    repeats = sum(len(group.videos) - 1 for group in groups)
    lines = [
        "MP4 视频检测报告摘要",
        "=" * 40,
        f"运行时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        f"运行模式：{'仅检测 / 预览（未复制）' if dry_run else '检测并复制保留视频'}",
        f"扫描 MP4：{len(videos)} 个",
        f"保留视频：{len(groups)} 个",
        f"重复视频：{repeats} 个",
        f"同名改名：{renamed} 个",
        f"复制数据：{_format_size(copied_bytes)}",
        f"读取失败 / 跳过：{len(skipped)} 个",
        f"耗时：{elapsed:.1f} 秒",
        "",
        "重复文件可按界面提示移入视频隔离区，不会自动删除。",
    ]
    if skipped:
        lines += ["", "跳过明细："]
        lines.extend(f"  - {path}：{reason}" for path, reason in skipped)
    summary_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_reports(
    output: Path,
    report_file: Path,
    summary_file: Path,
    groups: Sequence[VideoGroup],
    videos: Sequence[VideoFile],
    skipped: Sequence[tuple[Path, str]],
    elapsed: float,
    copied_bytes: int,
    renamed: int,
    dry_run: bool,
) -> None:
    rows = _report_rows(groups)
    _write_report_file(report_file, rows)
    _write_summary_file(summary_file, groups, videos, skipped,
                        elapsed, copied_bytes, renamed, dry_run)

    # 正式复制时，除用户指定报告位置外，再在输出目录留一份，方便随视频一起带走。
    if not dry_run:
        output.mkdir(parents=True, exist_ok=True)
        output_report = output / DEFAULT_REPORT_CSV
        output_summary = output / DEFAULT_REPORT_TXT
        if output_report.resolve() != report_file.resolve():
            _write_report_file(output_report, rows)
        if output_summary.resolve() != summary_file.resolve():
            _write_summary_file(output_summary, groups, videos, skipped,
                                elapsed, copied_bytes, renamed, dry_run)



def _unique_destination(directory: Path, name: str) -> Path:
    destination = directory / name
    if not destination.exists():
        return destination
    stem, suffix = destination.stem, destination.suffix
    index = 2
    while (directory / f"{stem} ({index}){suffix}").exists():
        index += 1
    return directory / f"{stem} ({index}){suffix}"


def quarantine_duplicates(
    groups: Sequence[VideoGroup],
    quarantine_dir: Path | str,
    *,
    log_file: Path | str | None = None,
    progress: Callable[[int, int, Path], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> list[dict[str, str]]:
    """把每组的非保留版本移入隔离区；绝不移动 winner，也不永久删除。"""
    quarantine_path = Path(quarantine_dir).expanduser().resolve()
    duplicates: list[tuple[int, VideoFile]] = []
    for group_no, group in enumerate(groups, 1):
        if group.winner is None:
            continue
        for video in group.videos:
            if video is not group.winner:
                duplicates.append((group_no, video))
    if not duplicates:
        return []

    quarantine_path.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, str]] = []
    total = len(duplicates)
    for index, (group_no, video) in enumerate(duplicates, 1):
        if cancel and cancel():
            raise OperationCancelled()
        if progress:
            progress(index, total, video.path)
        destination: Path | None = None
        try:
            if not video.path.exists():
                raise FileNotFoundError("文件已不存在")
            destination = _unique_destination(quarantine_path, video.path.name)
            shutil.move(str(video.path), str(destination))
            records.append({
                "处理时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "组号": str(group_no),
                "文件名": video.path.name,
                "原路径": str(video.path),
                "隔离后路径": str(destination),
                "是否成功": "已隔离",
                "说明": f"保留版本：{group_winner_name(groups, group_no)}",
            })
        except OSError as exc:
            records.append({
                "处理时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "组号": str(group_no),
                "文件名": video.path.name,
                "原路径": str(video.path),
                "隔离后路径": str(destination) if destination else "",
                "是否成功": "失败",
                "说明": str(exc),
            })

    if log_file:
        log_path = Path(log_file).expanduser().resolve()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        exists = log_path.exists()
        with log_path.open("a", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=(
                "处理时间", "组号", "文件名", "原路径",
                "隔离后路径", "是否成功", "说明",
            ))
            if not exists:
                writer.writeheader()
            writer.writerows(records)
    return records


def group_winner_name(groups: Sequence[VideoGroup], group_no: int) -> str:
    if 1 <= group_no <= len(groups):
        winner = groups[group_no - 1].winner
        if winner is not None:
            return winner.path.name
    return ""


def merge_videos(
    root: Path | str,
    output: Path | str,
    *,
    recursive: bool = True,
    overwrite: bool = False,
    exact_hash: bool = True,
    dry_run: bool = False,
    report_file: Path | str | None = None,
    summary_file: Path | str | None = None,
    progress: Callable[[str, float, str], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> VideoOutcome:
    started = time.monotonic()
    root_path = Path(root).expanduser().resolve()
    output_path = Path(output).expanduser()
    if not output_path.is_absolute():
        output_path = root_path / output_path
    output_path = output_path.resolve()

    if report_file is None:
        report_path = (root_path / "视频检测报告.csv") if dry_run else (output_path / DEFAULT_REPORT_CSV)
    else:
        report_path = Path(report_file).expanduser()
        if not report_path.is_absolute():
            report_path = root_path / report_path
        report_path = report_path.resolve()
    if summary_file is None:
        summary_path = report_path.with_name(report_path.stem + "_摘要.txt")
    else:
        summary_path = Path(summary_file).expanduser()
        if not summary_path.is_absolute():
            summary_path = root_path / summary_path
        summary_path = summary_path.resolve()

    if not root_path.is_dir():
        raise ValueError(f"根目录不存在：{root_path}")
    if output_path.exists() and not overwrite and not dry_run:
        raise FileExistsError(
            f"输出文件夹已存在：{output_path}\n请换一个输出名称，或勾选“覆盖已有输出”。"
        )
    _validate_output(root_path, output_path)

    def emit(phase: str, fraction: float, message: str) -> None:
        if progress:
            progress(phase, max(0.0, min(1.0, fraction)), message)

    emit("scan", 0.01, "正在扫描 MP4 文件并读取视频信息...")

    def scan_progress(index: int, total: int, path: Path) -> None:
        emit("scan", 0.02 + 0.28 * index / max(1, total),
             f"读取 {index}/{total}：{path.name}")

    videos, skipped = scan_videos(
        root_path, output_path, recursive,
        progress=scan_progress, cancel=cancel,
    )
    if not videos:
        raise ValueError("没有找到可处理的 MP4 文件。")

    if exact_hash:
        emit("hash", 0.31, "正在对同大小视频计算完整哈希...")

        def hash_progress(done: int, total: int, path: Path) -> None:
            emit("hash", 0.31 + 0.24 * done / max(1, total),
                 f"校验 {done / 1024 ** 3:.2f}/{total / 1024 ** 3:.2f} GB")

        videos, _ = calculate_hashes(videos, progress=hash_progress, cancel=cancel)

    emit("compare", 0.57, "正在比较视频标题、时长与分辨率...")

    def compare_progress(done: int, total: int) -> None:
        emit("compare", 0.57 + 0.08 * done / max(1, total),
             f"比较进度 {done}/{total}")

    groups, _links = find_video_groups(videos, progress=compare_progress, cancel=cancel)
    for group in groups:
        winner, reason = choose_video_winner(group)
        group.winner = winner
        group.reason = reason

    if not dry_run:
        if output_path.exists() and overwrite:
            shutil.rmtree(output_path)
        output_path.mkdir(parents=True, exist_ok=True)

    emit("write", 0.66, "正在复制保留的视频...")
    used: set[str] = set()
    renamed = 0
    copied_bytes = 0
    total_bytes = sum(group.winner.size for group in groups if group.winner)

    for index, group in enumerate(groups, 1):
        winner = group.winner
        assert winner is not None
        clean = safe_filename(winner.path.name)
        group.output_name = unique_output_name(winner.path.name, used)
        if group.output_name != clean:
            renamed += 1
        if not dry_run:
            destination = output_path / group.output_name
            before = copied_bytes

            def copied(chunk_len: int) -> None:
                nonlocal copied_bytes
                copied_bytes += chunk_len
                emit("write", 0.66 + 0.33 * copied_bytes / max(1, total_bytes),
                     f"复制 {index}/{len(groups)}：{winner.path.name}")

            _copy_with_progress(winner.path, destination, copied, cancel=cancel)
            if destination.stat().st_size != winner.size:
                raise OSError(f"复制校验失败：{winner.path} -> {destination}")
        else:
            # 预览时仍显示进度，便于用户知道处理规模。
            copied_bytes += winner.size
            emit("write", 0.66 + 0.33 * index / max(1, len(groups)),
                 f"预览 {index}/{len(groups)}：{winner.path.name}")

    elapsed = time.monotonic() - started
    _write_reports(output_path, report_path, summary_path, groups, videos, skipped,
                   elapsed, copied_bytes, renamed, dry_run)
    emit("done", 1.0, f"完成：保留 {len(groups)} 个，重复 {len(videos) - len(groups)} 个。")
    return VideoOutcome(
        root=root_path,
        output=output_path,
        groups=groups,
        videos=videos,
        skipped=skipped,
        elapsed=elapsed,
        copied_bytes=copied_bytes,
        renamed=renamed,
        dry_run=dry_run,
        report_file=report_path,
        summary_file=summary_path,
    )
