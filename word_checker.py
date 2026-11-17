#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
违禁词检测核心
==============

从原「敏感词检测」工具整理而来，供桌面界面调用。特点：
  * 不依赖第三方库；
  * 扫描时只读文件，不会自动删除；
  * 删除 / 隔离必须由界面二次确认；
  * 支持进度回调与中途停止。
"""

from __future__ import annotations

import csv
import re
import shutil
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Sequence

from novel_merger import IGNORED_DIR_NAMES, OperationCancelled, decode_txt

DEFAULT_WORDS_FILE = "违禁词.txt"
DEFAULT_REPORT_FILE = "检测结果.csv"
DEFAULT_SUMMARY_FILE = "检测摘要.txt"
DEFAULT_DELETE_LOG_FILE = "删除记录.csv"


@dataclass(frozen=True)
class WordHit:
    word: str
    count: int
    lines: tuple[int, ...]


@dataclass
class FileHits:
    path: Path
    hits: list[WordHit]

    @property
    def total(self) -> int:
        return sum(hit.count for hit in self.hits)

    @property
    def words(self) -> str:
        return "、".join(hit.word for hit in self.hits)


@dataclass
class CheckResult:
    targets: list[Path]
    scanned: int
    matched: list[FileHits]
    skipped: list[tuple[Path, str]]
    elapsed: float
    report: Path | None = None
    summary: Path | None = None
    threshold: int = 1

    @property
    def total_hits(self) -> int:
        return sum(item.total for item in self.matched)

    def candidates(self, threshold: int | None = None) -> list[FileHits]:
        limit = self.threshold if threshold is None else threshold
        return [item for item in self.matched if item.total >= limit]


def read_text(path: Path) -> str:
    data = path.read_bytes()
    text, _encoding, _replaced = decode_txt(data)
    return text.replace("\x00", "")


def load_words(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"找不到违禁词库：{path}")
    words: list[str] = []
    seen: set[str] = set()
    for line in read_text(path).splitlines():
        word = line.strip().lstrip("\ufeff")
        if not word or word.startswith("#"):
            continue
        if word not in seen:
            seen.add(word)
            words.append(word)
    if not words:
        raise ValueError(f"违禁词库为空：{path}")
    return words


def build_pattern(words: Sequence[str], ignore_case: bool) -> tuple[re.Pattern[str], dict[str, str]]:
    """长词优先的合并正则。返回 (正则, casefold 到词库原词的映射)。"""
    ordered = sorted(dict.fromkeys(words), key=lambda item: (-len(item), item))
    pattern = re.compile(
        "|".join(re.escape(word) for word in ordered),
        re.IGNORECASE if ignore_case else 0,
    )
    canonical = {word.casefold(): word for word in ordered}
    return pattern, canonical


def _append_line(mapping: dict[str, list[int]], word: str, line_number: int) -> None:
    lines = mapping[word]
    if not lines or lines[-1] != line_number:
        lines.append(line_number)


def scan_file(
    path: Path,
    pattern: re.Pattern[str],
    canonical: dict[str, str],
    ignore_case: bool,
    cancel: Callable[[], bool] | None = None,
) -> list[WordHit]:
    text = read_text(path)
    counts: Counter[str] = Counter()
    line_map: dict[str, list[int]] = defaultdict(list)

    for line_number, line in enumerate(text.splitlines(), 1):
        if cancel and line_number % 600 == 0 and cancel():
            raise OperationCancelled()
        for match in pattern.finditer(line):
            matched = match.group(0)
            word = canonical.get(matched.casefold(), matched) if ignore_case else matched
            counts[word] += 1
            _append_line(line_map, word, line_number)

    return [
        WordHit(word=word, count=count, lines=tuple(line_map[word]))
        for word, count in counts.most_common()
    ]


def _is_ignored_path(path: Path) -> bool:
    parent_parts = path.parts[:-1]
    if any(part.casefold() in {name.casefold() for name in IGNORED_DIR_NAMES}
           for part in parent_parts):
        return True
    return any(part.startswith("违禁词隔离区") for part in parent_parts)


def collect_txt_files(
    targets: Sequence[Path],
    recursive: bool,
    excluded: Iterable[Path] = (),
) -> list[Path]:
    excluded_set = {path.resolve() for path in excluded}
    files: list[Path] = []
    seen: set[Path] = set()
    for target in targets:
        target = target.resolve()
        if target.is_file():
            candidates = [target]
        elif target.is_dir():
            candidates = list(target.rglob("*.txt") if recursive else target.glob("*.txt"))
        else:
            continue
        for path in candidates:
            if not path.is_file() or path.suffix.casefold() != ".txt":
                continue
            resolved = path.resolve()
            if resolved in excluded_set or resolved in seen:
                continue
            if _is_ignored_path(path):
                continue
            seen.add(resolved)
            files.append(path)
    return sorted(files, key=lambda item: item.name.casefold())


def _row_for(file_hits: FileHits, hit: WordHit) -> dict[str, str | int]:
    line_text = ", ".join(str(number) for number in hit.lines)
    return {
        "文件名": file_hits.path.name,
        "文件路径": str(file_hits.path),
        "违禁词": hit.word,
        "次数": hit.count,
        "行号": line_text,
        "文件命中总数": file_hits.total,
    }


def write_report(
    path: Path,
    matched: Sequence[FileHits],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=("文件名", "文件路径", "违禁词", "次数", "行号", "文件命中总数"),
        )
        writer.writeheader()
        for item in matched:
            for hit in item.hits:
                writer.writerow(_row_for(item, hit))


def write_summary(
    path: Path,
    targets: Sequence[Path],
    words_file: Path,
    result: CheckResult,
    ignore_case: bool,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "违禁词检测摘要",
        "=" * 36,
        f"检测时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        f"检测对象：{'；'.join(str(item) for item in targets)}",
        f"违禁词库：{words_file}",
        f"忽略英文大小写：{'是' if ignore_case else '否'}",
        f"扫描 TXT：{result.scanned} 个",
        f"命中文件：{len(result.matched)} 个",
        f"命中总次数：{result.total_hits} 次",
        f"失败 / 跳过：{len(result.skipped)} 个",
        f"删除阈值：{result.threshold} 次",
        f"达到阈值的文件：{len(result.candidates())} 个",
    ]
    if result.skipped:
        lines += ["", "读取失败："]
        lines += [f"  - {path}：{reason}" for path, reason in result.skipped]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def scan_paths(
    targets: Sequence[Path],
    words_file: Path,
    *,
    report_file: Path | None = None,
    summary_file: Path | None = None,
    recursive: bool = False,
    ignore_case: bool = False,
    delete_threshold: int = 1,
    progress: Callable[[int, int, Path], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> CheckResult:
    if delete_threshold < 1:
        raise ValueError("删除阈值必须大于等于 1。")
    started = time.monotonic()
    words = load_words(words_file)
    pattern, canonical = build_pattern(words, ignore_case)
    excluded = [words_file]
    if report_file:
        excluded.append(report_file)
    if summary_file:
        excluded.append(summary_file)
    files = collect_txt_files(targets, recursive, excluded)
    if not files:
        raise ValueError("没有找到需要检测的 TXT 文件。")

    matched: list[FileHits] = []
    skipped: list[tuple[Path, str]] = []
    total = len(files)
    for index, path in enumerate(files, 1):
        if cancel and cancel():
            raise OperationCancelled()
        if progress:
            progress(index, total, path)
        try:
            hits = scan_file(path, pattern, canonical, ignore_case, cancel=cancel)
        except OperationCancelled:
            raise
        except (OSError, UnicodeError) as exc:
            skipped.append((path, str(exc)))
            continue
        if hits:
            matched.append(FileHits(path=path, hits=hits))

    result = CheckResult(
        targets=[Path(item) for item in targets],
        scanned=len(files) - len(skipped),
        matched=matched,
        skipped=skipped,
        elapsed=time.monotonic() - started,
        report=report_file,
        summary=summary_file,
        threshold=delete_threshold,
    )
    if report_file:
        write_report(report_file, matched)
    if summary_file:
        write_summary(summary_file, targets, words_file, result, ignore_case)
    return result


def _unique_destination(directory: Path, name: str) -> Path:
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    index = 2
    while (directory / f"{stem} ({index}){suffix}").exists():
        index += 1
    return directory / f"{stem} ({index}){suffix}"


def _log_row(
    item: FileHits,
    status: str,
    mode: str,
    destination: Path | None,
    reason: str,
) -> dict[str, str]:
    details = "；".join(
        f"{hit.word}：{hit.count} 次，行号：{', '.join(map(str, hit.lines))}"
        for hit in item.hits
    )
    return {
        "处理时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "处理方式": "隔离" if mode == "quarantine" else "永久删除",
        "是否成功": status,
        "文件名": item.path.name,
        "原文件路径": str(item.path),
        "隔离后路径": str(destination) if destination else "",
        "命中违禁词": item.words,
        "命中详情": f"总命中：{item.total} 次；{details}",
        "失败原因": reason,
    }


def process_candidates(
    result: CheckResult,
    *,
    log_file: Path | None = None,
    mode: str = "quarantine",
    quarantine_dir: Path | None = None,
    progress: Callable[[int, int, Path], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> list[dict[str, str]]:
    """处理达到阈值的文件。mode=quarantine 时移入隔离区，绝不永久删除。"""
    if mode not in {"quarantine", "delete"}:
        raise ValueError("mode 必须是 quarantine 或 delete。")
    candidates = result.candidates()
    if not candidates:
        return []

    if mode == "quarantine":
        if quarantine_dir is None:
            base = candidates[0].path.parent
            quarantine_dir = base / f"违禁词隔离区_{datetime.now():%Y%m%d_%H%M%S}"
        quarantine_dir.mkdir(parents=True, exist_ok=True)
    else:
        quarantine_dir = None

    records: list[dict[str, str]] = []
    total = len(candidates)
    for index, item in enumerate(candidates, 1):
        if cancel and cancel():
            raise OperationCancelled()
        if progress:
            progress(index, total, item.path)
        destination: Path | None = None
        try:
            if mode == "quarantine":
                destination = _unique_destination(quarantine_dir, item.path.name)
                shutil.move(str(item.path), str(destination))
                records.append(_log_row(item, "已隔离", mode, destination, ""))
            else:
                item.path.unlink()
                records.append(_log_row(item, "已删除", mode, None, ""))
        except OSError as exc:
            records.append(_log_row(item, "失败", mode, destination, str(exc)))

    if log_file and records:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        exists = log_file.exists()
        fieldnames = (
            "处理时间", "处理方式", "是否成功", "文件名", "原文件路径",
            "隔离后路径", "命中违禁词", "命中详情", "失败原因",
        )
        with log_file.open("a", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            if not exists:
                writer.writeheader()
            writer.writerows(records)
    return records
