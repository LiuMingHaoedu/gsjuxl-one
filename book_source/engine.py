# -*- coding: utf-8 -*-
"""书源执行引擎：搜索、详情、目录、正文、发现。

引擎本身不认识任何网站，只做四件事：

1. 按书源里的 URL 模板发请求（``{{key}}``、``{{page}}``、``,{json 选项}``）；
2. 把返回的 HTML / JSON 交给规则引擎；
3. 把结果组装成 ``SearchBook`` / ``BookInfo`` / ``Chapter`` / ``BookContent``；
4. 处理分页、相对地址、目录/正文的下一页。

UI、书架、缓存、任务队列都在引擎之外。
"""

from __future__ import annotations

import json
import re
import threading
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable

from . import dom
from .http import (
    DEFAULT_UA,
    HttpClient,
    NetworkError,
    absolute_url,
    alternate_host_url,
    is_connection_refused,
    parse_headers,
)
from .models import BookContent, BookInfo, Chapter, SearchBook
from .rules import RuleContext, RuleEngine, RuleError
from .source import BookSource

__all__ = ["EngineError", "RequestSpec", "BookSourceEngine"]


class EngineError(Exception):
    """书源执行失败（网络、规则、数据不完整等）。"""


@dataclass
class RequestSpec:
    url: str
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    body: str | None = None
    charset: str = ""
    retry: int = 0
    timeout: float | None = None


class BookSourceEngine:
    """执行一份或多份书源。"""

    def __init__(
        self,
        *,
        http: HttpClient | None = None,
        logger: Callable[[str], None] | None = None,
        user_agent: str | None = None,
    ) -> None:
        self.http = http
        self.logger = logger
        self.user_agent = user_agent
        self.rule_engine = RuleEngine(http=http, logger=logger)
        self._clients: dict[str, HttpClient] = {}
        self._client_lock = threading.RLock()

    # ------------------------------------------------------------------
    # 网络
    # ------------------------------------------------------------------
    def client_for(self, source: BookSource) -> HttpClient:
        key = source.url or source.name
        with self._client_lock:
            client = self._clients.get(key)
            if client is not None:
                return client
            if self.http is not None:
                client = self.http
            else:
                interval = _rate_interval(source.concurrent_rate)
                proxy = next(
                    (value for key, value in source.headers.items() if key.lower() == "proxy"),
                    None,
                )
                client = HttpClient(
                    timeout=20,
                    retries=1,
                    min_interval=interval,
                    proxy=proxy,
                    logger=self.logger,
                    user_agent=self.user_agent or DEFAULT_UA,
                )
            self._clients[key] = client
            return client

    def fetch(self, spec: RequestSpec, source: BookSource) -> tuple[str, str, int]:
        client = self.client_for(source)
        headers = {key: value for key, value in source.headers.items() if key.lower() != "proxy"}
        headers.update(spec.headers)
        if spec.method == "POST" and spec.body and not any(
            key.lower() == "content-type" for key in headers
        ):
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"

        def send(target: str, method: str, body: Any) -> Any:
            return client.request(
                target,
                method=method,
                headers=headers,
                body=body,
                encoding=spec.charset,
                timeout=spec.timeout,
                retries=spec.retry or None,
            )

        alternate = alternate_host_url(spec.url, source.url)
        targets = [spec.url]
        if alternate:
            targets.append(alternate)
        variants: list[tuple[str, Any]] = [(spec.method, spec.body)]
        if spec.method == "GET":
            # 少数站点会直接 reset 普通 GET，但接受无 body 的 POST
            variants.append(("POST", None))

        response = None
        last_error: NetworkError | None = None
        for target in targets:
            for method, body in variants:
                try:
                    response = send(target, method, body)
                except NetworkError as exc:
                    if not is_connection_refused(exc):
                        raise
                    last_error = exc
                    continue
                if target != spec.url or method != spec.method:
                    self._log(f"连接被重置，改用 {method} {target} 重试成功")
                spec.url = target
                break
            if response is not None:
                break
        if response is None:
            if last_error is not None:
                raise last_error
            raise EngineError(f"请求失败：{spec.url}")
        if response.status >= 400:
            raise EngineError(f"HTTP {response.status}：{spec.url}")
        text = response.text
        self._log(f"{spec.method} {spec.url} -> {response.status} / {len(text)} 字")
        return text, response.url, response.status

    # ------------------------------------------------------------------
    # URL 模板
    # ------------------------------------------------------------------
    def build_request(
        self,
        template: str,
        source: BookSource,
        *,
        key: str | None = None,
        page: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> RequestSpec:
        template = (template or "").strip()
        if not template:
            raise EngineError("书源 URL 为空")

        variables: dict[str, Any] = {}
        if key is not None:
            variables["key"] = key
        if page is not None:
            variables["page"] = page
        if extra:
            variables.update(extra)

        ctx = RuleContext(
            result=None,
            base_url=source.url,
            variables=variables,
            source=source,
            http=self.client_for(source),
            stage="url",
            cache={},
            logger=self.logger,
        )
        ctx._engine = self.rule_engine

        text = _expand_url_js(template, ctx, self.rule_engine)
        text = self._expand_templates(text, ctx)

        if page is not None:
            text = _expand_page(text, page)

        options: dict[str, Any] = {}
        match = re.search(r"\s*,\s*(\{.*\})\s*$", text, re.S)
        if match:
            options = _loads_lenient(match.group(1))
            text = text[: match.start()].strip()

        url = absolute_url(source.url, text)
        method = str(options.get("method") or "GET").upper()
        headers = parse_headers(options.get("headers"))
        body = options.get("body")
        charset = str(options.get("charset") or "")
        retry = _to_int(options.get("retry"), 0)
        timeout = options.get("timeout")

        if body is not None and not isinstance(body, str):
            body = json.dumps(body, ensure_ascii=False)

        if method == "GET":
            url = _encode_query(url, charset)
        elif body and _looks_like_form(str(body)):
            body = _encode_form(str(body), charset)

        js = options.get("js")
        if js:
            try:
                value = self.rule_engine.eval_expression(str(js), ctx.derive(url))
                if value:
                    url = str(value)
            except Exception as exc:
                self._log(f"URL js 执行失败：{exc}")

        return RequestSpec(
            url=url,
            method=method,
            headers=headers,
            body=str(body) if body is not None else None,
            charset=charset,
            retry=retry,
            timeout=float(timeout) if timeout else None,
        )

    def _expand_templates(self, text: str, ctx: RuleContext) -> str:
        def replace(match: re.Match[str]) -> str:
            expr = match.group(1).strip()
            if not expr:
                return ""
            try:
                value = self.rule_engine.eval_expression(expr, ctx)
            except Exception:
                value = ctx.variables.get(expr, "")
            if value is None:
                return ""
            if isinstance(value, float) and value.is_integer():
                return str(int(value))
            return str(value)

        return re.sub(r"\{\{([\w\W]*?)\}\}", replace, text)

    # ------------------------------------------------------------------
    # 搜索
    # ------------------------------------------------------------------
    def search(
        self,
        source: BookSource,
        keyword: str,
        page: int = 1,
        variables: dict[str, Any] | None = None,
    ) -> list[SearchBook]:
        if not source.search_url:
            raise EngineError(f"书源「{source.name}」没有 searchUrl")
        spec = self.build_request(source.search_url, source, key=keyword, page=page)
        body, base_url, _status = self.fetch(spec, source)
        merged = dict(variables or {})
        merged.setdefault("key", keyword)
        return self._parse_book_list(
            source,
            body,
            base_url,
            is_search=True,
            variables=merged,
            keyword=keyword,
        )

    def explore(
        self,
        source: BookSource,
        url: str,
        page: int = 1,
    ) -> list[SearchBook]:
        spec = self.build_request(url, source, page=page)
        body, base_url, _status = self.fetch(spec, source)
        return self._parse_book_list(source, body, base_url, is_search=False, variables={})

    def _parse_book_list(
        self,
        source: BookSource,
        body: str,
        base_url: str,
        *,
        is_search: bool,
        variables: dict[str, Any],
        keyword: str = "",
    ) -> list[SearchBook]:
        rules = source.rule_search if is_search else (source.rule_explore or source.rule_search)
        if not rules:
            rules = source.rule_search
        json_mode = dom.looks_like_json(body)
        ctx = RuleContext(
            result=body,
            base_url=base_url,
            redirect_url=base_url,
            variables=variables,
            source=source,
            http=self.client_for(source),
            stage="search" if is_search else "explore",
            cache={},
            logger=self.logger,
            json_mode=json_mode,
        )
        ctx._engine = self.rule_engine

        if base_url and source.book_url_pattern:
            try:
                if re.search(source.book_url_pattern, base_url):
                    return self._single_detail(source, body, base_url, variables, keyword)
            except re.error:
                pass

        list_rule = rules.get("bookList") or ""
        reverse = list_rule.startswith("-")
        list_rule = list_rule.lstrip("-+").strip()
        if not list_rule:
            return self._single_detail(source, body, base_url, variables, keyword)

        collections = self.rule_engine.elements(list_rule, ctx)
        if not collections:
            if not source.book_url_pattern:
                return self._single_detail(source, body, base_url, variables, keyword)
            return []

        books: list[SearchBook] = []
        seen: set[str] = set()
        for item in collections:
            item_ctx = ctx.derive(item, variables=dict(variables))
            if isinstance(item, dict):
                item_ctx.json_mode = True
            name = self._safe_string(rules.get("name", ""), item_ctx)
            if not name:
                continue
            book_url = self._url_for(rules.get("bookUrl", ""), item_ctx, base_url)
            if not book_url:
                book_url = base_url
            if book_url in seen:
                continue
            seen.add(book_url)
            book = SearchBook(
                name=_clean_name(name),
                author=self._safe_string(rules.get("author", ""), item_ctx),
                book_url=book_url,
                cover_url=self._url_for(rules.get("coverUrl", ""), item_ctx, base_url),
                intro=dom.html_to_text(self._safe_string(rules.get("intro", ""), item_ctx)),
                kind=",".join(self._safe_strings(rules.get("kind", ""), item_ctx)),
                last_chapter=self._safe_string(rules.get("lastChapter", ""), item_ctx),
                update_time=self._safe_string(rules.get("updateTime", ""), item_ctx),
                word_count=self._safe_string(rules.get("wordCount", ""), item_ctx),
                source_url=source.url,
                source_name=source.name,
                origin="search" if is_search else "explore",
                variables=dict(item_ctx.variables),
            )
            books.append(book)
        if reverse:
            books.reverse()
        return books

    def _single_detail(
        self,
        source: BookSource,
        body: str,
        base_url: str,
        variables: dict[str, Any],
        keyword: str,
    ) -> list[SearchBook]:
        info = self._parse_book_info(source, body, base_url, base_url, variables)
        if not info.name:
            return []
        if keyword and info.name and keyword not in info.name and info.author and keyword not in info.author:
            # 搜索关键词完全对不上时，仍保留结果（很多站点按拼音/别名搜索）
            pass
        return [info]

    # ------------------------------------------------------------------
    # 详情
    # ------------------------------------------------------------------
    def book_info(
        self,
        source: BookSource,
        book: BookInfo,
        *,
        body: str | None = None,
        base_url: str | None = None,
    ) -> BookInfo:
        target = book.book_url
        if not target:
            raise EngineError("书籍地址为空")
        if body is None:
            spec = self.build_request(target, source)
            body, final_url, _status = self.fetch(spec, source)
        else:
            final_url = base_url or target
        return self._parse_book_info(
            source,
            body,
            final_url,
            final_url,
            dict(book.variables),
            existing=book,
        )

    def _parse_book_info(
        self,
        source: BookSource,
        body: str,
        base_url: str,
        redirect_url: str,
        variables: dict[str, Any],
        existing: BookInfo | None = None,
    ) -> BookInfo:
        rules = source.rule_book_info or {}
        json_mode = dom.looks_like_json(body)
        ctx = RuleContext(
            result=body,
            base_url=base_url,
            redirect_url=redirect_url,
            variables=variables,
            source=source,
            http=self.client_for(source),
            stage="book",
            book=existing,
            cache={},
            logger=self.logger,
            json_mode=json_mode,
        )
        ctx._engine = self.rule_engine

        init_rule = rules.get("init")
        if init_rule:
            try:
                initialized = self.rule_engine.evaluate(init_rule, ctx)
                if initialized:
                    ctx.result = initialized[0]
            except RuleError as exc:
                self._log(f"详情初始化规则失败：{exc}")

        info = existing or BookInfo(source_url=source.url, source_name=source.name)
        name = self._safe_string(rules.get("name", ""), ctx)
        if name:
            info.name = _clean_name(name)
        author = self._safe_string(rules.get("author", ""), ctx)
        if author:
            info.author = author
        kind = ",".join(self._safe_strings(rules.get("kind", ""), ctx))
        if kind:
            info.kind = kind
        word_count = self._safe_string(rules.get("wordCount", ""), ctx)
        if word_count:
            info.word_count = word_count
        last_chapter = self._safe_string(rules.get("lastChapter", ""), ctx)
        if last_chapter:
            info.last_chapter = last_chapter
        intro = self._safe_string(rules.get("intro", ""), ctx)
        if intro:
            info.intro = dom.html_to_text(intro)[:5000]
        cover = self._url_for(rules.get("coverUrl", ""), ctx, redirect_url)
        if cover:
            info.cover_url = cover
        toc_url = self._url_for(rules.get("tocUrl", ""), ctx, redirect_url) or redirect_url
        info.toc_url = toc_url
        info.book_url = redirect_url or base_url
        info.source_url = source.url
        info.source_name = source.name
        info.variables.update(ctx.variables)
        return info

    # ------------------------------------------------------------------
    # 目录
    # ------------------------------------------------------------------
    def toc(
        self,
        source: BookSource,
        book: BookInfo,
        *,
        body: str | None = None,
        base_url: str | None = None,
    ) -> list[Chapter]:
        rules = source.rule_toc or {}
        list_rule = (rules.get("chapterList") or "").strip()
        if not list_rule:
            raise EngineError("书源没有 ruleToc.chapterList")
        reverse = list_rule.startswith("-")
        list_rule = list_rule.lstrip("-+").strip()

        target = book.toc_url or book.book_url
        if body is None:
            spec = self.build_request(target, source)
            body, final_url, _status = self.fetch(spec, source)
        else:
            final_url = base_url or target

        chapters: list[Chapter] = []
        visited: set[str] = {final_url}
        next_urls: list[str] = []
        page_body = body
        page_base = base_url or target
        page_redirect = final_url

        for _page in range(12):
            page_ctx = RuleContext(
                result=page_body,
                base_url=page_base,
                redirect_url=page_redirect,
                variables=dict(book.variables),
                source=source,
                http=self.client_for(source),
                stage="toc",
                book=book,
                cache={},
                logger=self.logger,
                json_mode=dom.looks_like_json(page_body),
            )
            page_ctx._engine = self.rule_engine
            elements = self.rule_engine.elements(list_rule, page_ctx)
            for item in elements:
                chapter_ctx = page_ctx.derive(item)
                if isinstance(item, dict):
                    chapter_ctx.json_mode = True
                title = self._safe_string(rules.get("chapterName", ""), chapter_ctx)
                if not title:
                    continue
                chapter_url = self._url_for(rules.get("chapterUrl", ""), chapter_ctx, page_redirect)
                is_volume = self._safe_string(rules.get("isVolume", ""), chapter_ctx).lower() in (
                    "true", "1", "是", "yes",
                )
                if not chapter_url:
                    chapter_url = page_redirect
                chapters.append(
                    Chapter(
                        title=title,
                        url=chapter_url,
                        is_volume=is_volume,
                        is_vip=self._safe_string(rules.get("isVip", ""), chapter_ctx).lower()
                        in ("true", "1", "是", "yes"),
                        update_time=self._safe_string(rules.get("updateTime", ""), chapter_ctx),
                        base_url=page_redirect,
                    )
                )

            next_rule = rules.get("nextTocUrl")
            if not next_rule:
                break
            raw_next = self._safe_strings(next_rule, page_ctx)
            candidates = []
            for item in raw_next:
                absolute = absolute_url(page_redirect, item)
                if absolute and absolute not in visited and absolute not in candidates:
                    candidates.append(absolute)
            next_urls = candidates
            if not next_urls:
                break
            target = next_urls[0]
            visited.add(target)
            try:
                spec = self.build_request(target, source)
                page_body, page_redirect, _status = self.fetch(spec, source)
                page_base = target
            except EngineError as exc:
                self._log(f"目录下一页失败：{exc}")
                break

        if not chapters:
            raise EngineError("目录为空")

        if reverse:
            chapters.reverse()

        formatter = rules.get("formatJs")
        if formatter:
            for index, chapter in enumerate(chapters):
                scope = {
                    "index": index + 1,
                    "chapter": chapter.to_dict(),
                    "title": chapter.title,
                    "book": book.to_dict(),
                    "baseUrl": chapter.base_url,
                    "java": self.rule_engine._java_bridge(
                        RuleContext(
                            base_url=chapter.base_url,
                            source=source,
                            http=self.client_for(source),
                            variables=dict(book.variables),
                            stage="toc",
                            book=book,
                            chapter=chapter,
                            logger=self.logger,
                        )
                    ),
                }
                try:
                    value = self.rule_engine.js.eval(
                        self.rule_engine._with_js_lib(formatter, page_ctx), scope
                    )
                    if value:
                        chapter.title = str(value)
                except Exception as exc:
                    self._log(f"formatJs 失败：{exc}")
                    break

        for index, chapter in enumerate(chapters):
            chapter.index = index
        return chapters

    # ------------------------------------------------------------------
    # 正文
    # ------------------------------------------------------------------
    def content(
        self,
        source: BookSource,
        book: BookInfo,
        chapter: Chapter,
        next_chapter_url: str = "",
    ) -> BookContent:
        rules = source.rule_content or {}
        content_rule = rules.get("content") or ""
        if not content_rule:
            raise EngineError("书源没有 ruleContent.content")

        spec = self.build_request(chapter.url, source)
        body, redirect_url, _status = self.fetch(spec, source)
        result = BookContent(title=chapter.title, url=chapter.url)
        pages: list[str] = []
        visited: set[str] = {redirect_url}
        current_url = chapter.url
        current_redirect = redirect_url
        current_body = body
        current_base = chapter.url

        for page_index in range(20):
            ctx = RuleContext(
                result=current_body,
                base_url=current_base,
                redirect_url=current_redirect,
                variables=dict(book.variables),
                source=source,
                http=self.client_for(source),
                stage="content",
                book=book,
                chapter=chapter,
                title=chapter.title,
                next_chapter_url=next_chapter_url,
                cache={},
                logger=self.logger,
                json_mode=dom.looks_like_json(current_body),
            )
            ctx._engine = self.rule_engine
            raw = self.rule_engine.string(content_rule, ctx, unescape=False)
            if raw:
                pages.append(self._format_content(raw, book))

            next_rule = rules.get("nextContentUrl")
            if not next_rule:
                break
            next_list = self.rule_engine.strings(next_rule, ctx)
            candidates = []
            for item in next_list:
                absolute = absolute_url(current_redirect, item)
                if not absolute or absolute in visited:
                    continue
                if next_chapter_url and absolute == absolute_url(current_redirect, next_chapter_url):
                    continue
                candidates.append(absolute)
            if not candidates:
                break
            target = candidates[0]
            visited.add(target)
            try:
                spec = self.build_request(target, source)
                current_body, current_redirect, _status = self.fetch(spec, source)
                current_url = target
                current_base = target
            except EngineError as exc:
                self._log(f"正文下一页失败：{exc}")
                break

        content = "\n".join(pages).strip()
        if not content:
            raise EngineError("正文为空")

        replace_rule = rules.get("replaceRegex")
        if replace_rule:
            try:
                replace_ctx = RuleContext(
                    result=content,
                    base_url=current_redirect,
                    variables=dict(book.variables),
                    source=source,
                    http=self.client_for(source),
                    stage="content",
                    book=book,
                    chapter=chapter,
                    logger=self.logger,
                )
                replace_ctx._engine = self.rule_engine
                content = self.rule_engine.string(replace_rule, replace_ctx, unescape=False)
            except RuleError as exc:
                self._log(f"replaceRegex 失败：{exc}")

        title_rule = rules.get("title")
        if title_rule:
            try:
                title_ctx = RuleContext(
                    result=body,
                    base_url=chapter.url,
                    variables=dict(book.variables),
                    source=source,
                    http=self.client_for(source),
                    stage="content",
                    book=book,
                    chapter=chapter,
                    logger=self.logger,
                )
                title_ctx._engine = self.rule_engine
                title = self.rule_engine.string(title_rule, title_ctx)
                if title:
                    result.title = title
            except RuleError:
                pass

        result.content = content
        result.next_url = next_chapter_url
        return result

    def _format_content(self, raw: str, book: BookInfo) -> str:
        text = raw
        if text.lstrip().startswith("<") or re.search(
            r"(?is)</?(?:p|br|div|span|img|a)\b", text
        ):
            text = dom.html_to_text(text, keep_image_url=False)
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = [line.strip() for line in text.split("\n")]
        lines = [line for line in lines if line]
        return "\n\n".join(lines)

    # ------------------------------------------------------------------
    # 发现
    # ------------------------------------------------------------------
    def explore_links(self, source: BookSource) -> list[tuple[str, str]]:
        """解析 exploreUrl：支持 ``名称::地址`` 多行、JSON 数组或 ``@js:``。"""
        raw = (source.explore_url or "").strip()
        if not raw:
            return []
        if raw.lower().startswith("@js:") or raw.lower().startswith("<js>"):
            code = raw[4:] if raw.lower().startswith("@js:") else raw
            code = re.sub(r"(?is)<js>(.*?)</js>", r"\1", code).strip()
            ctx = RuleContext(
                result=None,
                base_url=source.url,
                source=source,
                http=self.client_for(source),
                variables={"page": 1},
                cache={},
                logger=self.logger,
            )
            ctx._engine = self.rule_engine
            try:
                value = self.rule_engine.eval_expression(code, ctx)
            except Exception as exc:
                self._log(f"exploreUrl JS 失败：{exc}")
                return []
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    value = None
            links: list[tuple[str, str]] = []
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        name = str(item.get("title") or item.get("name") or "发现")
                        url = str(item.get("url") or "")
                    elif isinstance(item, str) and "::" in item:
                        name, url = item.split("::", 1)
                    else:
                        continue
                    if url:
                        links.append((name.strip(), url.strip()))
            return links
        if raw.lstrip().startswith("["):
            try:
                data = json.loads(raw)
            except ValueError:
                data = None
            if isinstance(data, list):
                links = []
                for item in data:
                    if isinstance(item, dict):
                        name = str(item.get("title") or item.get("name") or "发现")
                        url = str(item.get("url") or "")
                        if url:
                            links.append((name, url))
                return links
        links = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            if "::" in line:
                name, url = line.split("::", 1)
            elif "，" in line:
                name, url = line.split("，", 1)
            else:
                name, url = "发现", line
            name = name.strip()
            url = url.strip()
            if url:
                links.append((name, url))
        return links

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    def _url_for(self, rule: str, ctx: RuleContext, base: str) -> str:
        if not rule:
            return ""
        try:
            value = self.rule_engine.first_string(rule, ctx)
        except RuleError as exc:
            self._log(f"URL 规则失败：{rule}（{exc}）")
            return ""
        if not value:
            return ""
        return absolute_url(base, value)

    def _safe_string(self, rule: str, ctx: RuleContext) -> str:
        if not rule:
            return ""
        try:
            return self.rule_engine.string(rule, ctx)
        except RuleError as exc:
            self._log(f"规则失败：{rule}（{exc}）")
            return ""

    def _safe_strings(self, rule: str, ctx: RuleContext) -> list[str]:
        if not rule:
            return []
        try:
            return self.rule_engine.strings(rule, ctx)
        except RuleError as exc:
            self._log(f"规则失败：{rule}（{exc}）")
            return []

    def _log(self, message: str) -> None:
        if self.logger:
            try:
                self.logger(message)
            except Exception:
                pass


# ----------------------------------------------------------------------
# URL 辅助
# ----------------------------------------------------------------------
def _expand_url_js(template: str, ctx: RuleContext, engine: RuleEngine) -> str:
    if "@js:" not in template and "<js>" not in template.lower():
        return template
    result = template
    # <js>...</js>
    result = re.sub(
        r"(?is)<js>(.*?)</js>",
        lambda match: _eval_url_js(match.group(1), result, ctx, engine),
        result,
    )
    if "@js:" in result:
        head, _, code = result.partition("@js:")
        result = _eval_url_js(code, head, ctx, engine)
    return result


def _eval_url_js(code: str, prefix: str, ctx: RuleContext, engine: RuleEngine) -> str:
    code = code.replace("@result", "__bs_url_result")
    scope = engine._js_scope(ctx.derive(prefix))
    scope["__bs_url_result"] = prefix
    try:
        value = engine.js.eval(engine._with_js_lib(code, ctx), scope)
    except Exception as exc:
        ctx.log(f"URL JS 失败：{exc}")
        return prefix
    if value is None:
        return ""
    return str(value)


def _expand_page(text: str, page: int) -> str:
    def replace(match: re.Match[str]) -> str:
        pages = [item.strip() for item in match.group(1).split(",")]
        if not pages:
            return ""
        index = max(0, page - 1)
        return pages[min(index, len(pages) - 1)]

    return re.sub(r"<([^<>]*,[^<>]*)>", replace, text)


def _encode_query(url: str, charset: str = "") -> str:
    parts = urllib.parse.urlsplit(url)
    if not parts.query:
        return url
    encoding = charset or "utf-8"
    pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    encoded = []
    for key, value in pairs:
        if _is_encoded(key):
            new_key = key
        else:
            new_key = urllib.parse.quote(key, safe="", encoding=encoding, errors="replace")
        if _is_encoded(value):
            new_value = value
        else:
            new_value = urllib.parse.quote(value, safe="", encoding=encoding, errors="replace")
        encoded.append(f"{new_key}={new_value}")
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, "&".join(encoded), parts.fragment)
    )


def _is_encoded(text: str) -> bool:
    return bool(re.search(r"%[0-9A-Fa-f]{2}", text))


def _encode_form(text: str, charset: str = "") -> str:
    encoding = charset or "utf-8"
    parts: list[str] = []
    for item in (text or "").split("&"):
        if not item:
            continue
        if "=" in item:
            key, value = item.split("=", 1)
        else:
            key, value = item, ""
        if not _is_encoded(key):
            key = urllib.parse.quote(key, safe="", encoding=encoding, errors="replace")
        if not _is_encoded(value):
            value = urllib.parse.quote(value, safe="", encoding=encoding, errors="replace")
        parts.append(f"{key}={value}")
    return "&".join(parts)


def _looks_like_form(text: str) -> bool:
    return bool(re.match(r"^[\w\-.\[\]]+=[^&]*(?:&[\w\-.\[\]]+=[^&]*)*$", text or ""))


def _loads_lenient(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except ValueError:
        quoted = re.sub(r"([{,]\s*)([A-Za-z_][\w]*)\s*:", r'\1"\2":', text)
        quoted = quoted.replace("'", '"')
        try:
            data = json.loads(quoted)
        except ValueError:
            return {}
    return data if isinstance(data, dict) else {}


def _rate_interval(concurrent_rate: str) -> float:
    text = (concurrent_rate or "").strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text) / 1000.0
    match = re.match(r"\s*(\d+)\s*/\s*(\d+)\s*$", text)
    if not match:
        return 0.0
    count = max(1, int(match.group(1)))
    millis = max(1, int(match.group(2)))
    return millis / 1000.0 / count


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clean_name(name: str) -> str:
    name = re.sub(r"\s+", " ", (name or "").strip())
    return name[:200]
