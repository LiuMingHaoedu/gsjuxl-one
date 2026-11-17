# -*- coding: utf-8 -*-
"""书源、书架、章节缓存与阅读设置的本地存储。

所有数据都在 ``%APPDATA%\\小工作箱\\书源`` 下，和程序本体分离：

* ``sources.json``    —— 导入的书源（legado JSON 数组）
* ``bookshelf.json``  —— 书架、目录、阅读进度
* ``cache/``          —— 章节正文缓存
* ``settings.json``   —— 字号、最近书源等
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from .models import BookshelfItem, BookInfo, Chapter
from .source import BookSource, dedupe_sources, parse_sources

__all__ = ["SourceStore", "Bookshelf", "ChapterCache", "ReaderSettings", "default_data_dir"]


def default_data_dir() -> Path:
    root = os.environ.get("APPDATA")
    base = Path(root) if root else Path.home()
    return base / "小工作箱" / "书源"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


class SourceStore:
    """书源管理。"""

    def __init__(self, directory: str | Path | None = None) -> None:
        self.directory = Path(directory) if directory else default_data_dir()
        self.path = self.directory / "sources.json"
        self._lock = threading.RLock()

    def load(self) -> list[BookSource]:
        with self._lock:
            data = _read_json(self.path, [])
        try:
            return dedupe_sources(parse_sources(data))
        except ValueError:
            return []

    def save(self, sources: Iterable[BookSource]) -> None:
        data = [source.to_dict() for source in sources]
        with self._lock:
            _atomic_write(self.path, json.dumps(data, ensure_ascii=False, indent=2))

    def import_file(self, path: str | Path) -> list[BookSource]:
        payload = Path(path).read_bytes()
        imported = parse_sources(payload)
        if not imported:
            raise ValueError("文件中没有可识别的书源")
        with self._lock:
            current = self.load()
            merged = dedupe_sources([*current, *imported])
            self.save(merged)
        return imported

    def import_text(self, text: str) -> list[BookSource]:
        imported = parse_sources(text)
        if not imported:
            raise ValueError("没有可识别的书源")
        with self._lock:
            merged = dedupe_sources([*self.load(), *imported])
            self.save(merged)
        return imported

    def export_file(self, path: str | Path, sources: Iterable[BookSource] | None = None) -> None:
        items = list(sources) if sources is not None else self.load()
        Path(path).write_text(
            json.dumps([source.to_dict() for source in items], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def add(self, source: BookSource) -> None:
        with self._lock:
            sources = self.load()
            sources = [item for item in sources if item.key != source.key]
            sources.append(source)
            self.save(sources)

    def remove(self, key: str) -> None:
        with self._lock:
            self.save([item for item in self.load() if item.key != key])

    def get(self, key: str) -> BookSource | None:
        return next((item for item in self.load() if item.key == key), None)

    def set_enabled(self, key: str, enabled: bool) -> None:
        with self._lock:
            sources = self.load()
            for source in sources:
                if source.key == key:
                    source.enabled = enabled
            self.save(sources)


class Bookshelf:
    """书架（含目录与进度）。"""

    def __init__(self, directory: str | Path | None = None) -> None:
        self.directory = Path(directory) if directory else default_data_dir()
        self.path = self.directory / "bookshelf.json"
        self._lock = threading.RLock()
        self._items: dict[str, BookshelfItem] | None = None

    def _load(self) -> dict[str, BookshelfItem]:
        if self._items is not None:
            return self._items
        data = _read_json(self.path, [])
        items: dict[str, BookshelfItem] = {}
        if isinstance(data, list):
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                try:
                    item = BookshelfItem.from_dict(entry)
                except (TypeError, ValueError):
                    continue
                if item.book.book_url:
                    items[item.key] = item
        self._items = items
        return items

    def _flush(self) -> None:
        items = self._load()
        payload = [item.to_dict() for item in items.values()]
        with self._lock:
            _atomic_write(self.path, json.dumps(payload, ensure_ascii=False, indent=2))

    def items(self) -> list[BookshelfItem]:
        with self._lock:
            values = list(self._load().values())
        values.sort(key=lambda item: item.read_at, reverse=True)
        return values

    def get(self, key: str) -> BookshelfItem | None:
        with self._lock:
            return self._load().get(key)

    def upsert(self, item: BookshelfItem) -> None:
        with self._lock:
            item.updated_at = time.time()
            self._load()[item.key] = item
            self._flush()

    def remove(self, key: str) -> None:
        with self._lock:
            self._load().pop(key, None)
            self._flush()

    def update_progress(self, key: str, chapter_index: int, scroll: float = 0.0) -> None:
        with self._lock:
            item = self._load().get(key)
            if item is None:
                return
            item.chapter_index = chapter_index
            item.scroll_position = scroll
            item.read_at = time.time()
            self._flush()

    def add(
        self,
        book: BookInfo,
        chapters: list[Chapter] | None = None,
        chapter_index: int = -1,
    ) -> BookshelfItem:
        item = self.get(book.key) or BookshelfItem(book=book)
        item.book = book
        if chapters is not None:
            item.chapters = chapters
        if chapter_index >= 0:
            item.chapter_index = min(chapter_index, max(0, len(item.chapters) - 1))
        item.read_at = time.time()
        self.upsert(item)
        return item


class ChapterCache:
    """章节正文缓存。"""

    def __init__(self, directory: str | Path | None = None) -> None:
        self.directory = (Path(directory) if directory else default_data_dir()) / "cache"
        self._lock = threading.RLock()

    def _path(self, book_key: str, chapter_key: str) -> Path:
        digest = hashlib.sha1(f"{book_key}\n{chapter_key}".encode("utf-8")).hexdigest()
        return self.directory / book_key / f"{digest}.json"

    def get(self, book_key: str, chapter_key: str) -> dict[str, Any] | None:
        path = self._path(book_key, chapter_key)
        data = _read_json(path, None)
        return data if isinstance(data, dict) else None

    def put(self, book_key: str, chapter_key: str, title: str, content: str) -> None:
        path = self._path(book_key, chapter_key)
        payload = {
            "title": title,
            "content": content,
            "cached_at": time.time(),
        }
        with self._lock:
            _atomic_write(path, json.dumps(payload, ensure_ascii=False))

    def has(self, book_key: str, chapter_key: str) -> bool:
        return self._path(book_key, chapter_key).exists()

    def clear_book(self, book_key: str) -> None:
        folder = self.directory / book_key
        if folder.exists():
            shutil.rmtree(folder, ignore_errors=True)

    def stats(self) -> dict[str, int]:
        count = 0
        size = 0
        if self.directory.exists():
            for path in self.directory.rglob("*.json"):
                try:
                    size += path.stat().st_size
                    count += 1
                except OSError:
                    continue
        return {"count": count, "bytes": size}

    def clear_all(self) -> None:
        if self.directory.exists():
            shutil.rmtree(self.directory, ignore_errors=True)


class ReaderSettings:
    """阅读器设置（字号、行距、最近书源）。"""

    DEFAULTS = {
        "font_size": 16,
        "line_spacing": 8,
        "last_source": "",
        "window_geometry": "",
        "reader_font": "Microsoft YaHei UI",
    }

    def __init__(self, directory: str | Path | None = None) -> None:
        self.directory = Path(directory) if directory else default_data_dir()
        self.path = self.directory / "settings.json"
        self._lock = threading.RLock()
        self.data = dict(self.DEFAULTS)
        loaded = _read_json(self.path, {})
        if isinstance(loaded, dict):
            self.data.update(loaded)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, self.DEFAULTS.get(key, default))

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.save()

    def save(self) -> None:
        with self._lock:
            _atomic_write(self.path, json.dumps(self.data, ensure_ascii=False, indent=2))
