#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MP4 文件名批量清理
==================

把文件名中的指定文字（默认“正在播放”）删除，例如：

    正在播放 电影.mp4        -> 电影.mp4
    电影 正在播放.mp4        -> 电影.mp4
    正在播放电影.mp4         -> 电影.mp4
    电影正在播放（1080p）.mp4 -> 电影（1080p）.mp4

只改文件名，不修改视频内容。重名时自动加 (2)、(3) 后缀。
"""

from __future__ import annotations

import csv
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

DEFAULT_KEYWORD = "正在播放"
DEFAULT_REPORT = "MP4改名报告.csv"
LONG_TRAILING_NUMBER_RE = re.compile(r"[\s._\-—–]*\d{4,}$")


@dataclass(frozen=True)
class RenameItem:
    path: Path
    new_name: str
    new_path: Path
    action: str
    reason: str = ""


def _clean_stem(stem: str, keyword: str, strip_long_numbers: bool = False) -> str:
    # 不做 NFKC：NFKC 会把中文冒号“：”变成 Windows 非法字符“:”。
    text = unicodedata.normalize("NFC", stem).replace("\u3000", " ").replace("\xa0", " ")
    text = text.replace(keyword, "")
    # 用户要求：文件名中的中英文冒号也全部去掉。
    text = text.replace(":", "").replace("：", "")
    if strip_long_numbers:
        # 只删除末尾的 4 位以上编号，避免误删标题里的年份/分辨率。
        text = LONG_TRAILING_NUMBER_RE.sub("", text)
    # 去掉删除关键词后残留在首尾的分隔符、空白和标点。
    edge_chars = r"\s._\-—–·;；,，、!?！？…。"
    text = re.sub(rf"^[{edge_chars}]+", "", text)
    text = re.sub(rf"[{edge_chars}]+$", "", text)
    text = re.sub(r"[ ]{2,}", " ", text).strip()
    # 只处理 Windows 文件名非法字符；全角中文标点保留。
    text = re.sub(r'[<>"/\\|?*\x00-\x1f]', "_", text)
    text = re.sub(r"[ ]{2,}", " ", text).strip(" .")
    return text


def _unique_name(directory: Path, stem: str, suffix: str, used: set[str]) -> str:
    candidate = f"{stem}{suffix}"
    if candidate.casefold() not in used:
        return candidate
    index = 2
    while f"{stem} ({index}){suffix}".casefold() in used:
        index += 1
    return f"{stem} ({index}){suffix}"


def collect_rename_items(
    roots: Sequence[Path],
    keyword: str = DEFAULT_KEYWORD,
    *,
    recursive: bool = True,
    all_colons: bool = False,
    strip_long_numbers: bool = False,
) -> list[RenameItem]:
    keyword = keyword.strip()
    if not keyword:
        raise ValueError("要删除的文字不能为空。")
    if not roots:
        raise ValueError("没有指定文件夹。")

    files: list[Path] = []
    for root in roots:
        root = Path(root).expanduser().resolve()
        if root.is_file() and root.suffix.casefold() == ".mp4":
            files.append(root)
            continue
        if not root.is_dir():
            continue
        iterator = root.rglob("*") if recursive else root.glob("*")
        for path in iterator:
            if path.is_file() and path.suffix.casefold() == ".mp4":
                files.append(path)

    files = sorted(set(files), key=lambda p: (p.parent.as_posix().casefold(), p.name.casefold()))
    planned: dict[Path, str] = {}
    used_by_dir: dict[Path, set[str]] = {}
    for path in files:
        has_keyword = keyword in path.stem
        has_colon = ":" in path.stem or "：" in path.stem
        has_long_number = bool(LONG_TRAILING_NUMBER_RE.search(path.stem))
        if not has_keyword and not (all_colons and has_colon) and not (
            strip_long_numbers and has_long_number
        ):
            continue
        new_stem = _clean_stem(
            path.stem, keyword, strip_long_numbers=strip_long_numbers,
        )
        if not new_stem:
            new_stem = "视频"
        suffix = path.suffix or ".mp4"
        if new_stem == path.stem:
            continue
        used = used_by_dir.setdefault(path.parent, set())
        # 目录中已有的名字（包括不准备改名的文件）也要避开。
        if not used:
            used.update(
                p.name.casefold()
                for p in path.parent.iterdir()
                if p.is_file() and p != path
            )
        new_name = _unique_name(path.parent, new_stem, suffix, used)
        new_path = path.parent / new_name
        used.add(new_name.casefold())
        planned[path] = new_name

    items: list[RenameItem] = []
    for path in files:
        if path not in planned:
            continue
        new_name = planned[path]
        new_path = path.parent / new_name
        if new_name == path.name:
            items.append(RenameItem(path, new_name, path, "跳过", "文件名已无需清理"))
        elif new_path.exists() and new_path != path:
            items.append(RenameItem(path, new_name, new_path, "失败", "目标文件已存在"))
        else:
            items.append(RenameItem(path, new_name, new_path, "待改名", "删除关键词"))
    return items


def apply_renames(
    items: Sequence[RenameItem],
    *,
    report_file: Path | None = None,
    progress: Callable[[int, int, Path], None] | None = None,
) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    total = len(items)
    for index, item in enumerate(items, 1):
        if progress:
            progress(index, total, item.path)
        status = item.action
        reason = item.reason
        if status == "待改名":
            try:
                item.path.rename(item.new_path)
            except OSError as exc:
                status = "失败"
                reason = str(exc)
            else:
                status = "已改名"
                reason = "删除关键词成功"
        records.append({
            "处理时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "状态": status,
            "原文件名": item.path.name,
            "新文件名": item.new_name,
            "原路径": str(item.path),
            "新路径": str(item.new_path),
            "说明": reason,
        })

    if report_file and records:
        report_file = Path(report_file)
        report_file.parent.mkdir(parents=True, exist_ok=True)
        with report_file.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=(
                "处理时间", "状态", "原文件名", "新文件名", "原路径", "新路径", "说明",
            ))
            writer.writeheader()
            writer.writerows(records)
    return records


def find_mp4_roots(root: Path, recursive: bool = True) -> list[Path]:
    root = Path(root).expanduser().resolve()
    if root.is_file():
        return [root] if root.suffix.casefold() == ".mp4" else []
    if not root.is_dir():
        return []
    iterator = root.rglob("*") if recursive else root.glob("*")
    return sorted(p for p in iterator if p.is_file() and p.suffix.casefold() == ".mp4")
