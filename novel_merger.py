#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小说合并 / 去重工具
====================

扫描根目录下的多个小说文件夹，把内容相同或高度相似、只是文件名不同或
版本略有差异的 TXT 归为一组；每组只保留质量最好的一份，复制到最终文件夹。

判断依据：
  1. 清洗文本后的完整内容哈希（完全相同）；
  2. 句段指纹（内容高度相似、大小接近）；
  3. 文件名质量（干净、完整、带作者/完结标识者优先）；
  4. 正文完整度、编码可读性、文件大小。

只读取和复制原文件，不修改、不删除任何源文件。无需安装第三方库。

常用命令：
    python novel_merger.py --root . --output 合并结果
    python novel_merger.py --root . --dry-run
    python novel_merger.py "D:/小说" -o "D:/小说/最终结果" --recursive

作者：DeepSeek Harness
版本：1.0.0
"""

from __future__ import annotations

import argparse
import codecs
import csv
import hashlib
import heapq
import re
import shutil
import sys
import time
import unicodedata
import zlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable, Iterable, Sequence

VERSION = "1.0.0"
TXT_SUFFIXES = {".txt"}
SKETCH_SIZE = 192
MIN_TEXT_LENGTH = 20
DEFAULT_SIMILARITY = 0.68
DEFAULT_SIZE_RATIO = 0.72
OUTPUT_DIR_DEFAULT = "合并结果"
REPORT_CSV = "合并报告.csv"
REPORT_TXT = "合并摘要.txt"

# 目录 / 文件过滤
IGNORED_DIR_NAMES = {
    ".git", ".idea", ".vscode", "__pycache__", "node_modules",
    "System Volume Information", "$RECYCLE.BIN", "TXT违禁词检测工具",
}
IGNORED_FILE_NAMES = {"desktop.ini", "thumbs.db"}

# 广告 / 网站行。只在整行较短时剔除，避免误删正文。
AD_PATTERNS = (
    re.compile(r"https?://|www\.|\.com\b|\.net\b|\.org\b", re.I),
    re.compile(r"笔趣阁|请记住本站|最新网址|手机版阅读|加入书签|章节错误", re.I),
    re.compile(r"本站.{0,12}(?:域名|网址|地址)|无弹窗|更新最快|txt下载", re.I),
    re.compile(r"全文阅读|免费阅读|求收藏|求推荐|首发地址|转码", re.I),
)
AD_LINE_MAX_LEN = 240

# 用于相似度的连续文字片段（标点、空白会自然分段）
MIN_UNIT_CHARS = 8
UNIT_RUN_RE = re.compile(r"(?:[^\W_]){%d,}" % MIN_UNIT_CHARS, re.UNICODE)
MATCH_STRIP_RE = re.compile(r"[\W_]+", re.UNICODE)
WORD_COUNT_RE = re.compile(r"\s*字数\s*[：:].*$", re.I)
CHAPTER_RE = re.compile(
    r"^\s*(?:第[0-9零〇一二三四五六七八九十百千万两]+[章节回卷部集篇]"
    r"|Chapter\s+\d+|CHAPTER\s+\d+)",
    re.M,
)
LEADING_ORDER_RE = re.compile(r"^\s*\d{1,3}\s*[.、．_\-]\s*")
COPY_SUFFIX_RE = re.compile(
    r"(?:\s*[-—–_]\s*副本|\(\s*副本\s*\)|（\s*副本\s*）"
    r"|\(\s*\d+\s*\)|（\s*\d+\s*）)\s*$"
)
SCRAPER_SUFFIX_RE = re.compile(r"(?:_d\d+_t\d+|_t\d+|_d\d+)\s*$", re.I)
TRAILING_NUMBER_RE = re.compile(r"\s+\d{1,3}\s*$")
FULL_MARK_RE = re.compile(
    r"[（(\[【]\s*(?:全|全文|完结|完本|全书完)\s*[)）\]】]"
    r"|(?:全文完|全书完|已完结|完结|完本)\s*$"
)
SERIAL_MARK_RE = re.compile(r"[（(\[【]?\s*连载中?\s*[)）\]】]?", re.I)
WORD_COUNT_IN_AUTHOR_RE = re.compile(r"字数\s*[：:]", re.I)


@dataclass(frozen=True)
class NameInfo:
    raw: str
    title: str
    author: str
    match_key: str
    score: int
    is_complete: bool
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class Book:
    path: Path
    source: str
    source_rank: int
    size: int
    encoding: str
    replacement_count: int
    body_len: int
    total_len: int
    exact_hash: str
    sketch: frozenset[int]
    unit_count: int
    chapter_count: int
    tail_complete: bool
    name: NameInfo
    error: str = ""


@dataclass
class Link:
    i: int
    j: int
    similarity: float
    containment: float
    jaccard: float
    reason: str


@dataclass
class Group:
    books: list[Book]
    winner: Book | None = None
    output_name: str = ""
    links: list[Link] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class ScanStats:
    source_dirs: list[Path] = field(default_factory=list)
    scanned: int = 0
    skipped: int = 0
    read_errors: list[tuple[Path, str]] = field(default_factory=list)
    ignored: list[tuple[Path, str]] = field(default_factory=list)


class OperationCancelled(Exception):
    """用户主动停止扫描 / 合并时抛出。"""


@dataclass
class MergeOutcome:
    root: Path
    output: Path
    groups: list[Group]
    stats: ScanStats
    elapsed: float
    renamed: int
    dry_run: bool


# ---------------------------------------------------------------------------
# 文件名分析
# ---------------------------------------------------------------------------

def _split_author(stem: str) -> tuple[str, str]:
    """只在末尾存在“作者：”时拆分，避免误伤标题中的“作者”二字。"""
    # 少数书名本身含“作者”二字，因此取最后一个“作者：”作为作者字段起点。
    matches = list(re.finditer(r"作者\s*[：:]", stem))
    if not matches:
        return stem, ""
    pos = matches[-1].start()
    title = stem[:pos].strip()
    author = stem[matches[-1].end():].strip()
    return (title or stem), author


def _has_repeated_title(title: str) -> bool:
    parts = [p.strip() for p in re.split(r"[＋+&、]", title) if p.strip()]
    if len(parts) < 2:
        return False
    first = re.sub(r"\s+", "", parts[0]).casefold()
    if not first:
        return False
    return any(re.sub(r"\s+", "", p).casefold() == first for p in parts[1:])


def analyze_name(path_or_name: str | Path) -> NameInfo:
    raw = Path(path_or_name).stem
    text = unicodedata.normalize("NFKC", raw).replace("\u3000", " ").replace("\xa0", " ")
    text = re.sub(r"[\t\r\n]+", " ", text)
    had_edge_space = text != text.strip()
    text = re.sub(r"[ ]{2,}", " ", text).strip()

    title, author = _split_author(text)
    reasons: list[str] = []
    score = 50

    # 作者与字数
    if author:
        score += 14
        if WORD_COUNT_IN_AUTHOR_RE.search(author):
            score -= 7
            reasons.append("作者字段混入字数")
        clean_author = WORD_COUNT_RE.sub("", author).strip()
        if clean_author and clean_author not in {"佚名", "不详", "未知", "无", "无名"}:
            score += 3
    else:
        clean_author = ""

    is_complete = bool(FULL_MARK_RE.search(text)) or bool(
        re.search(r"[（(\[【]\s*全\s*[)）\]】]", text)
    )
    if is_complete:
        score += 10
        reasons.append("有完结标识")
    if SERIAL_MARK_RE.search(text):
        score -= 4
        reasons.append("标注连载中")

    title_clean = FULL_MARK_RE.sub("", title)
    title_clean = re.sub(
        r"[（(\[【]\s*(?:全|全文|完结|完本|全书完|连载中?|高[Hh]|加料版?|无绿改?|修改版?|重置版?)\s*[)）\]】]",
        "", title_clean,
    )
    title_clean = SERIAL_MARK_RE.sub("", title_clean)
    title_clean = title_clean.strip(" _-—·.，,。")

    if LEADING_ORDER_RE.search(text):
        score -= 16
        reasons.append("带序号前缀")
    if COPY_SUFFIX_RE.search(text):
        score -= 28
        reasons.append("副本/重复标记")
    if SCRAPER_SUFFIX_RE.search(text):
        score -= 14
        reasons.append("采集器后缀")
    if TRAILING_NUMBER_RE.search(title):
        score -= 7
        reasons.append("标题尾部数字")
    if _has_repeated_title(title):
        score -= 12
        reasons.append("标题重复")
    if had_edge_space:
        score -= 4
        reasons.append("首尾空格")
    if re.search(r" {2,}", raw):
        score -= 3
        reasons.append("多余空格")
    if not title_clean:
        score -= 25
        reasons.append("标题为空")
    elif len(title_clean) < 2:
        score -= 8
        reasons.append("标题过短")
    elif len(title_clean) > 90:
        score -= 8
        reasons.append("标题过长")
    if len(text) > 150:
        score -= 4
        reasons.append("文件名过长")

    # 用于内容相同、名字不同时的排序：优先干净标题，兼顾作者。
    match_key = re.sub(r"\s+", "", f"{title_clean}{clean_author}").casefold()
    if not match_key:
        match_key = re.sub(r"\s+", "", text).casefold()

    return NameInfo(
        raw=raw,
        title=title_clean or title.strip(),
        author=clean_author,
        match_key=match_key,
        score=score,
        is_complete=is_complete,
        reasons=tuple(reasons),
    )


# ---------------------------------------------------------------------------
# 文本读取、清洗、指纹
# ---------------------------------------------------------------------------

def decode_txt(data: bytes) -> tuple[str, str, int]:
    """返回 (文本, 编码, 替换字符数)。优先 BOM / UTF-8 / GB18030。"""
    if data.startswith(codecs.BOM_UTF32_LE) or data.startswith(codecs.BOM_UTF32_BE):
        return data.decode("utf-32", errors="replace"), "utf-32", data.decode("utf-32", errors="replace").count("\ufffd")
    if data.startswith(codecs.BOM_UTF16_LE) or data.startswith(codecs.BOM_UTF16_BE):
        text = data.decode("utf-16", errors="replace")
        return text, "utf-16", text.count("\ufffd")
    if data.startswith(codecs.BOM_UTF8):
        try:
            return data.decode("utf-8-sig"), "utf-8-sig", 0
        except UnicodeDecodeError:
            pass

    for encoding in ("utf-8", "gb18030", "big5"):
        try:
            return data.decode(encoding), encoding, 0
        except UnicodeDecodeError:
            continue

    # 最后兜底：仍然保留可读字符，并记录坏字符数量。
    text = data.decode("utf-8", errors="replace")
    return text, "utf-8(replace)", text.count("\ufffd")


def _is_ad_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > AD_LINE_MAX_LEN:
        return False
    return any(p.search(stripped) for p in AD_PATTERNS)


def strip_boilerplate(text: str) -> str:
    lines = text.splitlines()
    kept = [line for line in lines if not _is_ad_line(line)]
    return "\n".join(kept).strip()


def normalize_for_match(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    return MATCH_STRIP_RE.sub("", text)


def _unit_hash(unit: str) -> int:
    raw = unit.encode("utf-8", "replace")
    return (zlib.crc32(raw) << 32) | zlib.adler32(raw)


def build_sketch(text: str, k: int = SKETCH_SIZE) -> tuple[frozenset[int], int]:
    """句段底部 k 指纹。不用第三方库，适合中文长文本的近似去重。"""
    if k <= 0:
        return frozenset(), 0

    # 最大堆（存负数），只保留哈希值最小的 k 个片段。
    heap: list[int] = []
    current: set[int] = set()
    unit_count = 0

    for m in UNIT_RUN_RE.finditer(text):
        run = m.group(0)
        unit_count += 1
        if len(run) <= 240:
            chunks = (run,)
        else:
            chunks = (run[i:i + 240] for i in range(0, len(run), 240))
        for chunk in chunks:
            if len(chunk) < MIN_UNIT_CHARS:
                continue
            h = _unit_hash(chunk.casefold())
            if h in current:
                continue
            if len(heap) < k:
                heapq.heappush(heap, -h)
                current.add(h)
            elif h < -heap[0]:
                removed = -heapq.heapreplace(heap, -h)
                current.discard(removed)
                current.add(h)

    return frozenset(current), unit_count


def read_book(path: Path, source: str, source_rank: int) -> Book:
    data = path.read_bytes()
    text, encoding, replacements = decode_txt(data)
    text = text.replace("\x00", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    total_len = len(text)
    body = strip_boilerplate(text)
    if not body:
        body = text.strip()

    # 匹配哈希使用完整正文；正文里的广告行已剔除。
    norm_body = normalize_for_match(body)
    if not norm_body:
        norm_body = normalize_for_match(text)
    exact_hash = hashlib.blake2b(
        norm_body.encode("utf-8", "replace"), digest_size=16
    ).hexdigest()
    sketch, unit_count = build_sketch(body)
    tail = re.sub(r"[\s\W_]+", "", body[-400:]).casefold()
    tail_complete = bool(
        re.search(r"(?:全文完|全书完|大结局|完结|完本|终章|尾声)$", tail)
    )
    chapters = len(CHAPTER_RE.findall(body))

    return Book(
        path=path,
        source=source,
        source_rank=source_rank,
        size=len(data),
        encoding=encoding,
        replacement_count=replacements,
        body_len=len(norm_body),
        total_len=total_len,
        exact_hash=exact_hash,
        sketch=sketch,
        unit_count=unit_count,
        chapter_count=chapters,
        tail_complete=tail_complete,
        name=analyze_name(path),
    )


# ---------------------------------------------------------------------------
# 相似度与分组
# ---------------------------------------------------------------------------

def compare_books(a: Book, b: Book, threshold: float = DEFAULT_SIMILARITY,
                  size_ratio_min: float = DEFAULT_SIZE_RATIO) -> Link | None:
    if a.error or b.error:
        return None
    if a.body_len <= 0 or b.body_len <= 0:
        return None

    shorter = min(a.body_len, b.body_len)
    longer = max(a.body_len, b.body_len)
    size_ratio = shorter / longer

    exact = bool(a.exact_hash and a.exact_hash == b.exact_hash)
    if exact:
        return Link(0, 0, 1.0, 1.0, 1.0, "正文完全相同")

    if size_ratio < size_ratio_min:
        return None
    if not a.sketch or not b.sketch:
        return None

    inter = len(a.sketch & b.sketch)
    if not inter:
        return None
    containment = inter / min(len(a.sketch), len(b.sketch))
    jaccard = inter / len(a.sketch | b.sketch)

    # 短文本更保守；长文本要求内容大部分重叠。
    if shorter < 500:
        if containment >= 0.92 and jaccard >= 0.85:
            return Link(0, 0, jaccard, containment, jaccard, "短文本几乎相同")
        return None

    if containment >= 0.82 and size_ratio >= 0.85:
        return Link(0, 0, max(jaccard, containment), containment, jaccard,
                    f"内容高度重合（包含度 {containment:.0%}）")
    if jaccard >= threshold:
        return Link(0, 0, jaccard, containment, jaccard,
                    f"内容高度相似（Jaccard {jaccard:.0%}）")
    if containment >= 0.72 and size_ratio >= 0.92 and jaccard >= 0.60:
        return Link(0, 0, jaccard, containment, jaccard,
                    f"内容高度相似（包含度 {containment:.0%}）")
    return None


def scan_sources(
    root: Path,
    source_dirs: Sequence[Path],
    recursive: bool,
    progress: Callable[[int, int, Path], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> tuple[list[Book], ScanStats]:
    stats = ScanStats(source_dirs=list(source_dirs))
    books: list[Book] = []
    candidates: list[tuple[int, Path]] = []

    for rank, source_dir in enumerate(source_dirs):
        iterator = source_dir.rglob("*") if recursive else source_dir.glob("*")
        for path in iterator:
            if not path.is_file():
                continue
            if path.suffix.casefold() not in TXT_SUFFIXES:
                continue
            if path.name.casefold() in IGNORED_FILE_NAMES:
                continue
            try:
                rel_parts = path.relative_to(root).parts[:-1]
            except ValueError:
                rel_parts = path.parts[:-1]
            if any(part.casefold() in {n.casefold() for n in IGNORED_DIR_NAMES}
                   for part in rel_parts):
                stats.skipped += 1
                continue
            candidates.append((rank, path))

    total = len(candidates)
    for index, (rank, path) in enumerate(candidates, 1):
        if cancel and cancel():
            raise OperationCancelled()
        if progress:
            progress(index, total, path)
        try:
            if path.stat().st_size == 0:
                stats.ignored.append((path, "空文件"))
                continue
            book = read_book(path, source_dirs[rank].name or str(source_dirs[rank]), rank)
        except OSError as exc:
            stats.read_errors.append((path, str(exc)))
            continue

        stats.scanned += 1
        if book.body_len < MIN_TEXT_LENGTH:
            stats.ignored.append((path, f"正文过短（{book.body_len} 字）"))
            continue
        books.append(book)
    return books, stats


def find_groups(
    books: Sequence[Book],
    threshold: float,
    size_ratio_min: float,
    verbose: bool = False,
    progress: Callable[[int, int], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> tuple[list[Group], list[Link]]:
    n = len(books)
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

    links: list[Link] = []
    checked = 0
    for i in range(n):
        if cancel and cancel():
            raise OperationCancelled()
        a = books[i]
        for j in range(i + 1, n):
            if cancel and (j - i) % 256 == 0 and cancel():
                raise OperationCancelled()
            b = books[j]
            shorter = min(a.body_len, b.body_len)
            longer = max(a.body_len, b.body_len)
            if longer <= 0 or shorter / longer < size_ratio_min:
                continue
            checked += 1
            link = compare_books(a, b, threshold, size_ratio_min)
            if link is not None:
                link.i, link.j = i, j
                links.append(link)
                union(i, j)
        if progress and ((i + 1) % 10 == 0 or i + 1 == n):
            progress(i + 1, n)
        if verbose and (i + 1) % 100 == 0:
            print(f"  已比较 {i + 1}/{n} 个文件...", flush=True)

    grouped: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        grouped[find(i)].append(i)

    groups: list[Group] = []
    for indexes in grouped.values():
        members = [books[i] for i in indexes]
        group_links = [l for l in links if find(l.i) == find(indexes[0])]
        groups.append(Group(books=members, links=group_links))

    groups.sort(key=lambda g: min((b.source_rank, b.path.name.casefold()) for b in g.books))
    if verbose:
        print(f"  实际比较 {checked} 对，发现 {len(links)} 条重复连接。")
    return groups, links


# ---------------------------------------------------------------------------
# 择优
# ---------------------------------------------------------------------------

def _choose_winner(group: Group) -> Book:
    books = group.books
    if len(books) == 1:
        return books[0]

    max_len = max(b.body_len for b in books)
    tolerance = max(100, int(max_len * 0.001))
    eligible = [b for b in books if max_len - b.body_len <= tolerance]
    if not eligible:
        eligible = books

    # 解码坏字符明显更多的版本先淘汰。
    min_repl = min(b.replacement_count for b in eligible)
    if min_repl >= 0:
        clean = [b for b in eligible if b.replacement_count <= min_repl + 2]
        if clean:
            eligible = clean

    def key(b: Book):
        # 分数越高越好：文件名质量 > 完结标识 > 正文长度 > 章节数。
        return (
            b.name.score,
            1 if b.name.is_complete else 0,
            1 if b.tail_complete else 0,
            -len(b.name.title),
            b.body_len,
            b.chapter_count,
            -b.replacement_count,
            -b.source_rank,
            tuple(-ord(c) for c in b.path.name.casefold()),
        )

    return max(eligible, key=key)


def describe_winner(winner: Book, group: Group) -> str:
    if len(group.books) == 1:
        return "唯一版本，直接保留"
    max_len = max(b.body_len for b in group.books)
    if winner.body_len >= max_len - max(100, int(max_len * 0.001)):
        if winner.name.score >= max(b.name.score for b in group.books):
            if winner.name.is_complete and not all(b.name.is_complete for b in group.books):
                return "正文几乎相同，优先保留带完结标识、文件名更干净的版本"
            return "正文几乎相同，优先保留文件名更干净的版本"
        return "正文最完整，优先保留"
    return "内容更完整，优先保留"


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

WINDOWS_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_filename(name: str, max_stem: int = 150) -> str:
    name = name.replace("\r", " ").replace("\n", " ")
    name = WINDOWS_BAD_CHARS.sub("_", name)
    stem, suffix = Path(name).stem, Path(name).suffix or ".txt"
    stem = re.sub(r"[ ]{2,}", " ", stem).strip(" .")
    if not stem:
        stem = "未命名"
    if stem.upper() in WINDOWS_RESERVED:
        stem = "_" + stem
    if len(stem) > max_stem:
        digest = hashlib.sha1(stem.encode("utf-8")).hexdigest()[:8]
        stem = stem[: max_stem - 9].rstrip(" .") + "_" + digest
    return f"{stem}{suffix.lower()}"


def unique_output_name(name: str, used: set[str]) -> str:
    base = safe_filename(name)
    stem, suffix = Path(base).stem, Path(base).suffix
    candidate = base
    n = 2
    while candidate.casefold() in used:
        candidate = f"{stem} ({n}){suffix}"
        n += 1
    used.add(candidate.casefold())
    return candidate


def _write_reports(output: Path, groups: Sequence[Group], stats: ScanStats,
                   elapsed: float, dry_run: bool = False,
                   renamed: int = 0) -> None:
    rows: list[dict[str, object]] = []
    for group_no, group in enumerate(groups, 1):
        winner = group.winner
        assert winner is not None
        if len(group.books) == 1:
            rows.append({
                "组号": group_no,
                "处理": "保留",
                "输出文件名": group.output_name,
                "来源": winner.source,
                "原文件名": winner.path.name,
                "大小(字节)": winner.size,
                "正文字数": winner.body_len,
                "相似度": "100.0%",
                "说明": group.notes[0] if group.notes else "唯一版本",
            })
            continue
        for book in group.books:
            if book is winner:
                rows.append({
                    "组号": group_no,
                    "处理": "保留",
                    "输出文件名": group.output_name,
                    "来源": book.source,
                    "原文件名": book.path.name,
                    "大小(字节)": book.size,
                    "正文字数": book.body_len,
                    "相似度": "100.0%",
                    "说明": describe_winner(winner, group),
                })
            else:
                sims = []
                for other in group.books:
                    if other is not book:
                        link = compare_books(book, other)
                        if link:
                            sims.append(link.similarity)
                similarity = max(sims) if sims else 0.0
                rows.append({
                    "组号": group_no,
                    "处理": "合并",
                    "输出文件名": "",
                    "来源": book.source,
                    "原文件名": book.path.name,
                    "大小(字节)": book.size,
                    "正文字数": book.body_len,
                    "相似度": f"{similarity:.1%}",
                    "说明": "与保留版本重复，未复制",
                })

    if not dry_run:
        with (output / REPORT_CSV).open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else [
                "组号", "处理", "输出文件名", "来源", "原文件名",
                "大小(字节)", "正文字数", "相似度", "说明",
            ])
            writer.writeheader()
            writer.writerows(rows)

        duplicate_groups = sum(1 for g in groups if len(g.books) > 1)
        removed = sum(len(g.books) - 1 for g in groups)
        lines = [
            "小说合并摘要",
            "=" * 36,
            f"程序版本：{VERSION}",
            f"运行时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
            f"来源文件夹：{'、'.join(p.name for p in stats.source_dirs)}",
            f"扫描 TXT：{stats.scanned} 个",
            f"有效小说：{sum(len(g.books) for g in groups)} 个",
            f"最终文件：{len(groups)} 个",
            f"重复组：{duplicate_groups} 组，合并掉 {removed} 个文件",
            f"同名改名：{renamed} 个（原名冲突，输出时加了 (2)、(3) 等后缀）",
            f"跳过 / 过短 / 空文件：{len(stats.ignored)} 个",
            f"读取失败：{len(stats.read_errors)} 个",
            f"耗时：{elapsed:.1f} 秒",
            "",
            "择优规则：正文更完整优先；正文几乎相同时，文件名更干净、带作者或完结标识者优先。",
            "源文件未做任何修改、移动或删除。",
        ]
        if stats.ignored:
            lines.append("")
            lines.append("跳过明细：")
            lines.extend(f"  - {p}：{r}" for p, r in stats.ignored)
        if stats.read_errors:
            lines.append("")
            lines.append("读取失败：")
            lines.extend(f"  - {p}：{r}" for p, r in stats.read_errors)
        (output / REPORT_TXT).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_output(root: Path, output: Path, sources: Sequence[Path]) -> None:
    """确认清空输出文件夹时绝不会碰到根目录或任何来源文件夹。"""
    if output == root or _is_within(root, output):
        raise ValueError("输出文件夹不能是根目录或根目录的上级文件夹。")
    for source in sources:
        if output == source:
            raise ValueError(f"输出文件夹不能与来源文件夹相同：{source}")
        if _is_within(output, source):
            raise ValueError(f"输出文件夹不能放在来源文件夹内部：{source}")
    if output.exists() and not output.is_dir():
        raise ValueError(f"输出路径已存在且不是文件夹：{output}")


def write_output(root: Path, output: Path, groups: Sequence[Group],
                 stats: ScanStats, elapsed: float, overwrite: bool,
                 dry_run: bool,
                 progress: Callable[[int, int], None] | None = None,
                 cancel: Callable[[], bool] | None = None) -> int:
    if output.exists() and not dry_run:
        if output.resolve() == root.resolve():
            raise ValueError("输出文件夹不能与根目录相同。")
        if not overwrite:
            raise FileExistsError(
                f"输出文件夹已存在：{output}\n"
                "请换一个 --output 名称，或加 --overwrite 清空后重建。"
            )
        shutil.rmtree(output)
    if not dry_run:
        output.mkdir(parents=True, exist_ok=True)

    used: set[str] = set()
    renamed = 0
    total = len(groups)
    for index, group in enumerate(groups, 1):
        if cancel and cancel():
            raise OperationCancelled()
        winner = group.winner
        assert winner is not None
        clean_name = safe_filename(winner.path.name)
        group.output_name = unique_output_name(winner.path.name, used)
        if group.output_name != clean_name:
            renamed += 1
        if not dry_run:
            destination = output / group.output_name
            shutil.copy2(winner.path, destination)
            if not destination.exists() or destination.stat().st_size != winner.path.stat().st_size:
                raise OSError(f"复制校验失败：{winner.path} -> {destination}")
        if progress:
            progress(index, total)

    if not dry_run:
        _write_reports(output, groups, stats, elapsed, dry_run=False, renamed=renamed)
    return renamed


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------

def discover_sources(root: Path, output: Path, recursive: bool,
                     explicit: Sequence[str] = ()) -> list[Path]:
    if explicit:
        sources = []
        for item in explicit:
            p = Path(item)
            if not p.is_absolute():
                p = (root / p)
            p = p.resolve()
            if p.is_dir():
                sources.append(p)
            else:
                print(f"警告：来源文件夹不存在，已忽略：{p}", file=sys.stderr)
        return sources

    sources: list[Path] = []
    for p in sorted(root.iterdir(), key=lambda x: x.name.casefold()):
        if not p.is_dir():
            continue
        if p.name.casefold() in {n.casefold() for n in IGNORED_DIR_NAMES}:
            continue
        if p.resolve() == output.resolve():
            continue
        if p.name.startswith("."):
            continue
        try:
            has_txt = any(
                f.is_file() and f.suffix.casefold() in TXT_SUFFIXES
                for f in (p.rglob("*") if recursive else p.glob("*"))
            )
        except OSError:
            has_txt = False
        if has_txt:
            sources.append(p)

    # 只有在没有任何子文件夹书库时，才把根目录本身当作书库。
    if not sources and any(f.is_file() and f.suffix.casefold() in TXT_SUFFIXES
                           for f in root.glob("*")):
        sources.append(root)
    return sources


def merge_library(
    root: Path | str,
    output: Path | str,
    *,
    source_paths: Sequence[Path | str] | None = None,
    recursive: bool = False,
    overwrite: bool = False,
    threshold: float = DEFAULT_SIMILARITY,
    size_ratio: float = DEFAULT_SIZE_RATIO,
    dry_run: bool = False,
    progress: Callable[[str, float, str], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> MergeOutcome:
    """供桌面界面调用的合并入口；不打印，只通过 progress 回报进度。"""
    started = time.monotonic()
    root_path = Path(root).expanduser().resolve()
    output_path = Path(output).expanduser()
    if not output_path.is_absolute():
        output_path = root_path / output_path
    output_path = output_path.resolve()

    if not root_path.is_dir():
        raise ValueError(f"根目录不存在：{root_path}")
    if not (0.0 < threshold <= 1.0):
        raise ValueError("相似度阈值必须在 0 到 1 之间。")

    if output_path.exists() and not overwrite and not dry_run:
        raise FileExistsError(
            f"输出文件夹已存在：{output_path}\n"
            "请换一个输出名称，或勾选“覆盖已有输出”。"
        )

    explicit = [str(p) for p in (source_paths or [])]
    sources = discover_sources(root_path, output_path, recursive, explicit)
    if not sources:
        raise ValueError(f"在 {root_path} 下没有找到包含 TXT 的文件夹。")
    validate_output(root_path, output_path, sources)

    def emit(phase: str, fraction: float, message: str) -> None:
        if progress:
            progress(phase, max(0.0, min(1.0, fraction)), message)

    emit("scan", 0.01, "正在读取文件并计算内容指纹...")

    def scan_progress(index: int, total: int, path: Path) -> None:
        emit("scan", 0.02 + 0.34 * index / max(1, total),
             f"读取 {index}/{total}：{path.name}")

    books, stats = scan_sources(
        root_path, sources, recursive, progress=scan_progress, cancel=cancel
    )
    if not books:
        raise ValueError("没有找到可处理的小说 TXT 文件。")

    emit("compare", 0.37, f"已读取 {len(books)} 本，正在逐对比较内容...")

    def compare_progress(done: int, total: int) -> None:
        emit("compare", 0.37 + 0.50 * done / max(1, total),
             f"比较进度 {done}/{total}")

    groups, _links = find_groups(
        books, threshold, size_ratio,
        progress=compare_progress, cancel=cancel,
    )
    for group in groups:
        group.winner = _choose_winner(group)
        group.notes.append(describe_winner(group.winner, group))

    emit("write", 0.90, "正在写出最终文件和报告...")
    renamed = write_output(
        root_path, output_path, groups, stats,
        time.monotonic() - started, overwrite, dry_run,
        progress=lambda i, total: emit(
            "write", 0.90 + 0.10 * i / max(1, total),
            f"写出 {i}/{total}",
        ),
    )
    elapsed = time.monotonic() - started
    emit("done", 1.0, f"完成：保留 {len(groups)} 本，合并 {len(books) - len(groups)} 本。")
    return MergeOutcome(
        root=root_path,
        output=output_path,
        groups=groups,
        stats=stats,
        elapsed=elapsed,
        renamed=renamed,
        dry_run=dry_run,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="novel_merger.py",
        description="小说 TXT 合并去重：保留内容更完整、文件名更干净的一份。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python novel_merger.py\n"
            "  python novel_merger.py --root \"D:/小说\" --output \"D:/小说/最终结果\"\n"
            "  python novel_merger.py --dry-run --verbose\n"
        ),
    )
    parser.add_argument("root", nargs="?", default=".", help="根目录（默认当前目录）")
    parser.add_argument("--root", dest="root_opt", metavar="ROOT", default=None,
                        help="根目录（与位置参数等价，便于脚本调用）")
    parser.add_argument("-o", "--output", default=OUTPUT_DIR_DEFAULT,
                        help=f"输出文件夹（默认 {OUTPUT_DIR_DEFAULT}）")
    parser.add_argument("--source", action="append", default=[],
                        help="只扫描指定子文件夹，可重复使用")
    parser.add_argument("--recursive", action="store_true",
                        help="递归扫描来源文件夹（默认只看第一层）")
    parser.add_argument("--overwrite", action="store_true",
                        help="输出文件夹已存在时清空重建")
    parser.add_argument("--dry-run", action="store_true",
                        help="只分析和显示结果，不复制文件")
    parser.add_argument("--threshold", type=float, default=DEFAULT_SIMILARITY,
                        help=f"内容相似度阈值，0-1（默认 {DEFAULT_SIMILARITY}）")
    parser.add_argument("--size-ratio", type=float, default=DEFAULT_SIZE_RATIO,
                        help=f"参与比较的最小正文长度比（默认 {DEFAULT_SIZE_RATIO}）")
    parser.add_argument("--verbose", action="store_true", help="输出每个重复组的详情")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    started = time.monotonic()
    root = Path(args.root_opt or args.root).expanduser().resolve()
    output = Path(args.output).expanduser()
    if not output.is_absolute():
        output = root / output
    output = output.resolve()

    if not root.is_dir():
        print(f"错误：根目录不存在：{root}", file=sys.stderr)
        return 2
    if not (0.0 < args.threshold <= 1.0):
        print("错误：--threshold 必须在 0 到 1 之间。", file=sys.stderr)
        return 2

    if output.exists() and not args.overwrite and not args.dry_run:
        print(f"错误：输出文件夹已存在：{output}", file=sys.stderr)
        print("请换一个 --output 名称，或加 --overwrite 清空后重建。", file=sys.stderr)
        return 2

    try:
        sources = discover_sources(root, output, args.recursive, args.source)
    except OSError as exc:
        print(f"错误：无法读取根目录：{exc}", file=sys.stderr)
        return 2
    if not sources:
        print(f"错误：在 {root} 下没有找到包含 TXT 的子文件夹。", file=sys.stderr)
        return 2
    try:
        validate_output(root, output, sources)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    print(f"小说合并工具 v{VERSION}")
    print(f"根目录：{root}")
    print(f"来源：{'、'.join(str(p) for p in sources)}")
    print(f"输出：{output}{'（预览，不写入）' if args.dry_run else ''}")
    print("正在读取和计算内容指纹...", flush=True)

    try:
        books, stats = scan_sources(root, sources, args.recursive)
    except Exception as exc:  # noqa: BLE001 - CLI 需要给出可读错误
        print(f"错误：扫描失败：{exc}", file=sys.stderr)
        return 1

    if not books:
        print("错误：没有找到可处理的小说 TXT 文件。", file=sys.stderr)
        return 2

    print(f"已读取 {len(books)} 个有效 TXT，正在逐对比较...", flush=True)
    groups, _links = find_groups(books, args.threshold, args.size_ratio, args.verbose)

    for group in groups:
        group.winner = _choose_winner(group)
        group.notes.append(describe_winner(group.winner, group))

    duplicate_groups = sum(1 for g in groups if len(g.books) > 1)
    removed = sum(len(g.books) - 1 for g in groups)
    elapsed = time.monotonic() - started

    if args.verbose:
        for no, group in enumerate(groups, 1):
            if len(group.books) <= 1:
                continue
            w = group.winner
            assert w is not None
            print(f"\n[{no}] 保留：{w.path.name}")
            print(f"     来源：{w.source}  字数：{w.body_len}  文件名得分：{w.name.score}")
            print(f"     理由：{group.notes[0]}")
            for b in group.books:
                if b is not w:
                    print(f"     合并：{b.path.name}  （{b.source}，{b.body_len} 字）")

    try:
        renamed = write_output(root, output, groups, stats, elapsed, args.overwrite, args.dry_run)
    except FileExistsError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"错误：写入输出失败：{exc}", file=sys.stderr)
        return 1

    print("")
    print("完成。")
    print(f"  扫描 TXT：{stats.scanned} 个")
    print(f"  最终保留：{len(groups)} 个")
    print(f"  合并重复：{removed} 个（{duplicate_groups} 组）")
    print(f"  同名改名：{renamed} 个")
    print(f"  跳过 / 过短：{len(stats.ignored)} 个，读取失败：{len(stats.read_errors)} 个")
    print(f"  耗时：{elapsed:.1f} 秒")
    if args.dry_run:
        print("  当前为预览模式，未写入任何文件。")
    else:
        print(f"  输出文件夹：{output}")
        print(f"  详细报告：{output / REPORT_CSV}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
