# -*- coding: utf-8 -*-
"""书源模型与导入/导出。

一个书源就是一份 legado 兼容的 JSON。程序本体不认识任何具体网站，
所有“怎么搜、怎么取详情、怎么取目录、怎么取正文”都写在书源里。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .http import parse_headers

__all__ = [
    "BookSource",
    "SourceError",
    "SOURCE_EXAMPLE",
    "parse_sources",
    "load_sources_file",
    "dedupe_sources",
    "RULE_SEARCH_KEYS",
    "RULE_EXPLORE_KEYS",
    "RULE_BOOK_INFO_KEYS",
    "RULE_TOC_KEYS",
    "RULE_CONTENT_KEYS",
]

RULE_SEARCH_KEYS = (
    "bookList", "name", "author", "intro", "kind", "lastChapter",
    "updateTime", "bookUrl", "coverUrl", "wordCount", "checkKeyWord",
)
RULE_EXPLORE_KEYS = (
    "bookList", "name", "author", "intro", "kind", "lastChapter",
    "updateTime", "bookUrl", "coverUrl", "wordCount",
)
RULE_BOOK_INFO_KEYS = (
    "init", "name", "author", "intro", "kind", "lastChapter",
    "updateTime", "coverUrl", "tocUrl", "wordCount", "canReName",
)
RULE_TOC_KEYS = (
    "preUpdateJs", "chapterList", "chapterName", "chapterUrl",
    "formatJs", "isVolume", "isVip", "updateTime", "nextTocUrl",
)
RULE_CONTENT_KEYS = (
    "content", "title", "nextContentUrl", "webJs", "sourceRegex",
    "replaceRegex", "imageStyle", "payAction", "callBackJs",
)

_FIELD_MAP = {
    "bookSourceUrl": "url",
    "bookSourceName": "name",
    "bookSourceGroup": "group",
    "bookSourceType": "type",
    "bookSourceComment": "comment",
    "enabled": "enabled",
    "enabledExplore": "enabled_explore",
    "enabledCookieJar": "enabled_cookie_jar",
    "header": "header",
    "jsLib": "js_lib",
    "concurrentRate": "concurrent_rate",
    "coverDecodeJs": "cover_decode_js",
    "variableComment": "variable_comment",
    "loginUrl": "login_url",
    "loginUi": "login_ui",
    "loginCheckJs": "login_check_js",
    "searchUrl": "search_url",
    "exploreUrl": "explore_url",
    "exploreScreen": "explore_screen",
    "bookUrlPattern": "book_url_pattern",
    "ruleSearch": "rule_search",
    "ruleExplore": "rule_explore",
    "ruleBookInfo": "rule_book_info",
    "ruleToc": "rule_toc",
    "ruleContent": "rule_content",
    "ruleReview": "rule_review",
    "weight": "weight",
    "customOrder": "custom_order",
    "lastUpdateTime": "last_update_time",
    "respondTime": "respond_time",
    "eventListener": "event_listener",
    "customButton": "custom_button",
    "homepageModules": "homepage_modules",
}

_REVERSE_FIELD_MAP = {value: key for key, value in _FIELD_MAP.items()}


class SourceError(ValueError):
    """书源 JSON 格式错误。"""


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "off", "")
    return bool(value)


def _coerce_rules(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        return {str(k): "" if v is None else str(v) for k, v in value.items()}
    if isinstance(value, str) and value.strip():
        try:
            data = json.loads(value)
        except ValueError:
            return {}
        if isinstance(data, dict):
            return {str(k): "" if v is None else str(v) for k, v in data.items()}
    return {}


@dataclass
class BookSource:
    """legado 兼容的书源。"""

    url: str = ""
    name: str = ""
    group: str = ""
    type: int = 0
    comment: str = ""
    enabled: bool = True
    enabled_explore: bool = True
    enabled_cookie_jar: bool = True
    header: str = ""
    js_lib: str = ""
    concurrent_rate: str = ""
    cover_decode_js: str = ""
    variable_comment: str = ""
    login_url: str = ""
    login_ui: str = ""
    login_check_js: str = ""
    search_url: str = ""
    explore_url: str = ""
    explore_screen: str = ""
    book_url_pattern: str = ""
    rule_search: dict[str, str] = field(default_factory=dict)
    rule_explore: dict[str, str] = field(default_factory=dict)
    rule_book_info: dict[str, str] = field(default_factory=dict)
    rule_toc: dict[str, str] = field(default_factory=dict)
    rule_content: dict[str, str] = field(default_factory=dict)
    rule_review: dict[str, str] = field(default_factory=dict)
    weight: int = 0
    custom_order: int = 0
    last_update_time: int = 0
    respond_time: int = 180000
    event_listener: bool = False
    custom_button: bool = False
    homepage_modules: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)
    # 运行时变量（不写入书源 JSON，等价于 legado 的 sourceVariable_/v_ 缓存）
    variable_text: str = field(default="", repr=False)
    cache: dict[str, Any] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------
    # 构造 / 序列化
    # ------------------------------------------------------------------
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BookSource":
        if not isinstance(data, dict):
            raise SourceError("书源必须是 JSON 对象")
        values: dict[str, Any] = {"raw": dict(data)}
        for key, value in data.items():
            attr = _FIELD_MAP.get(key)
            if not attr:
                continue
            values[attr] = value
        source = cls(
            url=str(values.get("url") or "").strip(),
            name=str(values.get("name") or "").strip(),
            group=str(values.get("group") or "").strip(),
            type=_coerce_int(values.get("type"), 0),
            comment=str(values.get("comment") or ""),
            enabled=_coerce_bool(values.get("enabled"), True),
            enabled_explore=_coerce_bool(values.get("enabled_explore"), True),
            enabled_cookie_jar=_coerce_bool(values.get("enabled_cookie_jar"), True),
            header=str(values.get("header") or ""),
            js_lib=str(values.get("js_lib") or ""),
            concurrent_rate=str(values.get("concurrent_rate") or ""),
            cover_decode_js=str(values.get("cover_decode_js") or ""),
            variable_comment=str(values.get("variable_comment") or ""),
            login_url=str(values.get("login_url") or ""),
            login_ui=str(values.get("login_ui") or ""),
            login_check_js=str(values.get("login_check_js") or ""),
            search_url=str(values.get("search_url") or ""),
            explore_url=str(values.get("explore_url") or ""),
            explore_screen=str(values.get("explore_screen") or ""),
            book_url_pattern=str(values.get("book_url_pattern") or ""),
            rule_search=_coerce_rules(values.get("rule_search")),
            rule_explore=_coerce_rules(values.get("rule_explore")),
            rule_book_info=_coerce_rules(values.get("rule_book_info")),
            rule_toc=_coerce_rules(values.get("rule_toc")),
            rule_content=_coerce_rules(values.get("rule_content")),
            rule_review=_coerce_rules(values.get("rule_review")),
            weight=_coerce_int(values.get("weight"), 0),
            custom_order=_coerce_int(values.get("custom_order"), 0),
            last_update_time=_coerce_int(values.get("last_update_time"), 0),
            respond_time=_coerce_int(values.get("respond_time"), 180000),
            event_listener=_coerce_bool(values.get("event_listener"), False),
            custom_button=_coerce_bool(values.get("custom_button"), False),
            homepage_modules=str(values.get("homepage_modules") or ""),
        )
        return source

    def to_dict(self) -> dict[str, Any]:
        """导出为 legado 可识别的 JSON。"""
        data: dict[str, Any] = {
            "bookSourceUrl": self.url,
            "bookSourceName": self.name,
            "bookSourceGroup": self.group,
            "bookSourceType": self.type,
            "bookSourceComment": self.comment,
            "enabled": self.enabled,
            "enabledExplore": self.enabled_explore,
            "enabledCookieJar": self.enabled_cookie_jar,
            "header": self.header,
            "jsLib": self.js_lib,
            "concurrentRate": self.concurrent_rate,
            "coverDecodeJs": self.cover_decode_js,
            "variableComment": self.variable_comment,
            "loginUrl": self.login_url,
            "loginUi": self.login_ui,
            "loginCheckJs": self.login_check_js,
            "searchUrl": self.search_url,
            "exploreUrl": self.explore_url,
            "exploreScreen": self.explore_screen,
            "bookUrlPattern": self.book_url_pattern,
            "ruleSearch": dict(self.rule_search),
            "ruleExplore": dict(self.rule_explore),
            "ruleBookInfo": dict(self.rule_book_info),
            "ruleToc": dict(self.rule_toc),
            "ruleContent": dict(self.rule_content),
            "ruleReview": dict(self.rule_review),
            "weight": self.weight,
            "customOrder": self.custom_order,
            "lastUpdateTime": self.last_update_time,
            "respondTime": self.respond_time,
            "eventListener": self.event_listener,
            "customButton": self.custom_button,
            "homepageModules": self.homepage_modules,
        }
        # 保留书源作者/其它自定义字段，避免导出时丢信息
        for key, value in self.raw.items():
            if key not in data and key not in _FIELD_MAP:
                data[key] = value
        return data

    # ------------------------------------------------------------------
    # 便捷属性
    # ------------------------------------------------------------------
    @property
    def key(self) -> str:
        return self.url or self.name

    @property
    def short_id(self) -> str:
        digest = hashlib.sha1(self.key.encode("utf-8")).hexdigest()
        return digest[:10]

    @property
    def headers(self) -> dict[str, str]:
        return parse_headers(self.header)

    @property
    def host(self) -> str:
        from urllib.parse import urlsplit

        try:
            return urlsplit(self.url).netloc
        except ValueError:
            return ""

    @property
    def can_search(self) -> bool:
        return bool(self.search_url.strip()) and bool(
            self.rule_search.get("bookList") or self.rule_search.get("bookUrl")
        )

    @property
    def can_explore(self) -> bool:
        return bool(self.explore_url.strip()) and bool(self.rule_explore.get("bookList"))

    @property
    def has_js_rules(self) -> bool:
        for rules in (
            self.rule_search, self.rule_explore, self.rule_book_info,
            self.rule_toc, self.rule_content,
        ):
            for value in rules.values():
                if "@js:" in value:
                    return True
        return bool(self.login_check_js)

    def rules_for(self, stage: str) -> dict[str, str]:
        return {
            "search": self.rule_search,
            "explore": self.rule_explore,
            "book": self.rule_book_info,
            "toc": self.rule_toc,
            "content": self.rule_content,
        }.get(stage, {})

    def validate(self) -> list[str]:
        """返回问题列表，空列表代表可用。"""
        problems: list[str] = []
        if not self.name:
            problems.append("缺少书源名称（bookSourceName）")
        if not self.url:
            problems.append("缺少书源地址（bookSourceUrl）")
        elif not re.match(r"^https?://", self.url, re.I):
            problems.append("书源地址必须以 http:// 或 https:// 开头")
        if not self.search_url and not self.explore_url:
            problems.append("searchUrl 与 exploreUrl 至少填写一个")
        if self.search_url and not self.rule_search.get("bookList") and not self.rule_search.get("bookUrl"):
            problems.append("有 searchUrl，但 ruleSearch.bookList 为空")
        if self.rule_search and not self.rule_search.get("name"):
            problems.append("ruleSearch.name 为空，搜索结果无法显示书名")
        if self.rule_toc and not self.rule_toc.get("chapterList"):
            problems.append("ruleToc.chapterList 为空，无法取目录")
        if self.rule_content and not self.rule_content.get("content"):
            problems.append("ruleContent.content 为空，无法取正文")
        return problems

    def clone(self) -> "BookSource":
        return BookSource.from_dict(self.to_dict())

    def matches(self, keyword: str) -> bool:
        keyword = (keyword or "").strip().lower()
        if not keyword:
            return True
        return keyword in self.name.lower() or keyword in self.group.lower() or keyword in self.url.lower()

    def __str__(self) -> str:
        return f"{self.name}（{self.host or self.url}）"


def parse_sources(payload: Any) -> list[BookSource]:
    """解析书源 JSON。

    支持：单个对象、对象数组、``{"bookSources": [...]}``、导出的文本。
    """
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8-sig", "replace")
    if isinstance(payload, str):
        text = payload.lstrip("\ufeff").strip()
        if not text:
            return []
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise SourceError(f"书源不是合法 JSON：{exc}") from exc
    if isinstance(payload, dict):
        for key in ("bookSources", "sources", "data", "list"):
            value = payload.get(key)
            if isinstance(value, list):
                payload = value
                break
        else:
            payload = [payload]
    if not isinstance(payload, list):
        raise SourceError("书源 JSON 顶层必须是对象或数组")
    sources: list[BookSource] = []
    for item in payload:
        if isinstance(item, str):
            try:
                item = json.loads(item)
            except ValueError:
                continue
        if isinstance(item, dict) and (item.get("bookSourceUrl") or item.get("bookSourceName")):
            sources.append(BookSource.from_dict(item))
    return sources


def load_sources_file(path: str | Path) -> list[BookSource]:
    path = Path(path)
    data = path.read_bytes()
    return parse_sources(data)


def dedupe_sources(sources: Iterable[BookSource]) -> list[BookSource]:
    """按书源地址去重；后出现的书源补齐先出现书源的空字段。"""
    result: list[BookSource] = []
    index: dict[str, BookSource] = {}
    for source in sources:
        key = source.key
        if not key:
            continue
        if key not in index:
            index[key] = source
            result.append(source)
            continue
        existing = index[key]
        for attr in (
            "group", "header", "search_url", "explore_url", "login_url",
            "login_ui", "login_check_js", "book_url_pattern", "comment",
        ):
            if not getattr(existing, attr) and getattr(source, attr):
                setattr(existing, attr, getattr(source, attr))
        for attr in (
            "rule_search", "rule_explore", "rule_book_info", "rule_toc", "rule_content",
        ):
            current = getattr(existing, attr)
            for rule_key, rule_value in getattr(source, attr).items():
                if rule_value and not current.get(rule_key):
                    current[rule_key] = rule_value
        existing.weight = max(existing.weight, source.weight)
    return result


# 供「新建书源」对话框使用的空模板
SOURCE_EXAMPLE: dict[str, Any] = {
    "bookSourceName": "新建书源",
    "bookSourceUrl": "https://example.com",
    "bookSourceGroup": "自定义",
    "bookSourceType": 0,
    "enabled": True,
    "enabledExplore": False,
    "header": "",
    "searchUrl": "/search?q={{key}}&page={{page}}",
    "ruleSearch": {
        "bookList": "class.book-list@tag.li",
        "name": "class.title@text",
        "author": "class.author@text",
        "bookUrl": "tag.a@href",
        "coverUrl": "tag.img@src",
        "intro": "class.intro@text",
        "lastChapter": "class.latest@text",
    },
    "ruleBookInfo": {
        "name": "id.info@tag.h1@text",
        "author": "id.info@tag.span.0@text",
        "intro": "id.intro@text",
        "coverUrl": "id.info@tag.img@src",
        "tocUrl": "",
    },
    "ruleToc": {
        "chapterList": "id.list@tag.dd",
        "chapterName": "tag.a@text",
        "chapterUrl": "tag.a@href",
    },
    "ruleContent": {
        "content": "id.content@html",
        "nextContentUrl": "",
    },
}
