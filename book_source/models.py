# -*- coding: utf-8 -*-
"""书源引擎的数据模型。

这里的对象是 UI、书架、缓存、任务队列之间传递的“纯数据”，
不包含任何抓取逻辑，方便序列化到书架文件。
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import asdict, dataclass, field, fields
from typing import Any

__all__ = [
    "SearchBook",
    "BookInfo",
    "Chapter",
    "BookContent",
    "BookshelfItem",
    "book_key",
]


def book_key(source_url: str, book_url: str) -> str:
    """一本书的稳定主键：书源 + 书籍地址。"""
    digest = hashlib.sha1(f"{source_url}\n{book_url}".encode("utf-8"))
    return digest.hexdigest()[:16]


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip()
    return value


@dataclass
class SearchBook:
    """搜索结果。"""

    name: str = ""
    author: str = ""
    book_url: str = ""
    cover_url: str = ""
    intro: str = ""
    kind: str = ""
    last_chapter: str = ""
    update_time: str = ""
    word_count: str = ""
    source_url: str = ""
    source_name: str = ""
    origin: str = "search"
    variables: dict[str, str] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.name = _clean(self.name)
        self.author = _clean(self.author)
        self.book_url = _clean(self.book_url)
        self.cover_url = _clean(self.cover_url)
        self.intro = _clean(self.intro)

    @property
    def key(self) -> str:
        return book_key(self.source_url, self.book_url)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SearchBook":
        known = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})


@dataclass
class BookInfo(SearchBook):
    """书籍详情，比搜索结果多了目录地址。"""

    toc_url: str = ""
    can_rename: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BookInfo":
        known = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})


@dataclass
class Chapter:
    """目录项。"""

    title: str = ""
    url: str = ""
    index: int = 0
    is_vip: bool = False
    is_volume: bool = False
    update_time: str = ""
    base_url: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.title = _clean(self.title)
        self.url = _clean(self.url)

    @property
    def key(self) -> str:
        return hashlib.sha1(f"{self.url}\n{self.title}".encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Chapter":
        known = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})


@dataclass
class BookContent:
    """一章正文。"""

    title: str = ""
    content: str = ""
    url: str = ""
    next_url: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BookshelfItem:
    """书架条目（含目录与阅读进度）。"""

    book: BookInfo = field(default_factory=BookInfo)
    chapters: list[Chapter] = field(default_factory=list)
    chapter_index: int = -1
    scroll_position: float = 0.0
    added_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    read_at: float = field(default_factory=time.time)

    @property
    def key(self) -> str:
        return self.book.key

    @property
    def chapter(self) -> Chapter | None:
        if 0 <= self.chapter_index < len(self.chapters):
            return self.chapters[self.chapter_index]
        return None

    @property
    def progress_text(self) -> str:
        if not self.chapters:
            return "未加载目录"
        if self.chapter_index < 0:
            return "未开始"
        return f"{self.chapter_index + 1}/{len(self.chapters)}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "book": self.book.to_dict(),
            "chapters": [chapter.to_dict() for chapter in self.chapters],
            "chapter_index": self.chapter_index,
            "scroll_position": self.scroll_position,
            "added_at": self.added_at,
            "updated_at": self.updated_at,
            "read_at": self.read_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BookshelfItem":
        book_data = data.get("book") or {}
        book = BookInfo.from_dict(book_data) if isinstance(book_data, dict) else BookInfo()
        chapters = [
            Chapter.from_dict(item) for item in data.get("chapters") or [] if isinstance(item, dict)
        ]
        return cls(
            book=book,
            chapters=chapters,
            chapter_index=int(data.get("chapter_index", -1) or -1),
            scroll_position=float(data.get("scroll_position", 0.0) or 0.0),
            added_at=float(data.get("added_at", time.time()) or time.time()),
            updated_at=float(data.get("updated_at", time.time()) or time.time()),
            read_at=float(data.get("read_at", time.time()) or time.time()),
        )
