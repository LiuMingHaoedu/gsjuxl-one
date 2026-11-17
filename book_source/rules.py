# -*- coding: utf-8 -*-
"""规则解析引擎：复刻 legado 小说书源的规则语法。

支持：

* 默认规则：``class.book-list@tag.li@tag.a@text``、``id.content@html``；
* ``@CSS:``、``@XPath:``、``@Json:``、``@js:``、``<js>``；
* ``&&`` 连接、``||`` 取第一个非空、``%%`` 按索引交错；
* ``##正则##替换``（四段时只替换第一个匹配）；
* ``@put:{key: 规则}``、``@get:{key}``、``{{表达式}}``；
* ``-`` 前缀反转列表。

规则引擎只认识 HTML / JSON / 字符串，不关心具体网站；具体网站全部写在书源里。
"""

from __future__ import annotations

import base64
import hashlib
import html as _html
import json
import random
import re
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from lxml import etree

from . import dom
from .http import (
    HttpClient,
    NetworkError,
    absolute_url,
    alternate_host_url,
    is_connection_refused,
)
from .js import JSContext, JSError
from .models import BookInfo, Chapter, SearchBook

__all__ = [
    "RuleError",
    "RuleContext",
    "RuleEngine",
    "html_to_text",
]


_JS_RE = re.compile(r"(?is)<js>(.*?)</js>|@js:([\w\W]*)")
_PUT_RE = re.compile(r"@put\s*:\s*(\{[^}]+?\})", re.I)
_GET_RE = re.compile(r"@get\s*:\s*\{([^}]+?)\}", re.I)
_TEMPLATE_RE = re.compile(r"\{\{([\w\W]*?)\}\}", re.S)
_SELECTOR_KINDS = ("class.", "id.", "tag.", "text.")
_ACTION_TOKENS = {
    "text": "text",
    "textnodes": "text_nodes",
    "owntext": "own_text",
    "html": "html",
    "all": "all",
}
_STRUCT_TOKENS = {
    "children": "children",
    "parent": "parent",
    "sibling": "sibling",
    "next": "sibling",
    "prev": "prev",
}
_HTML_TAGS = {
    "a", "p", "span", "div", "li", "ul", "ol", "dd", "dt", "dl", "td", "tr",
    "table", "img", "em", "strong", "b", "i", "h1", "h2", "h3", "h4", "h5",
    "h6", "section", "article", "br",
}


class RuleError(Exception):
    """规则语法或取值失败。"""


class RegexMatch:
    """正则书源列表中的一条匹配，字段规则可用 ``$1`` 取值。"""

    __slots__ = ("full", "groups")

    def __init__(self, match: re.Match[str]) -> None:
        self.full = match.group(0)
        self.groups = list(match.groups(default=""))

    def group(self, index: int) -> str:
        if index == 0:
            return self.full
        if 0 < index <= len(self.groups):
            return self.groups[index - 1]
        return ""

    def __str__(self) -> str:
        return self.full


@dataclass
class RuleContext:
    """一次规则求值的上下文。"""

    result: Any = None
    base_url: str = ""
    redirect_url: str = ""
    variables: dict[str, Any] = field(default_factory=dict)
    source: Any = None
    http: HttpClient | None = None
    stage: str = ""
    book: Any = None
    chapter: Any = None
    title: str = ""
    next_chapter_url: str = ""
    json_mode: bool = False
    cache: dict[str, Any] = field(default_factory=dict)
    logger: Callable[[str], None] | None = None
    _engine: Any = field(default=None, repr=False)

    def derive(self, result: Any, **kwargs: Any) -> "RuleContext":
        data = {
            "result": result,
            "base_url": self.base_url,
            "redirect_url": self.redirect_url,
            "variables": self.variables,
            "source": self.source,
            "http": self.http,
            "stage": self.stage,
            "book": self.book,
            "chapter": self.chapter,
            "title": self.title,
            "next_chapter_url": self.next_chapter_url,
            "json_mode": self.json_mode,
            "cache": self.cache,
            "logger": self.logger,
            "_engine": self._engine,
        }
        data.update(kwargs)
        return RuleContext(**data)

    def log(self, message: str) -> None:
        if self.logger:
            try:
                self.logger(message)
            except Exception:
                pass


class RuleEngine:
    """把书源规则应用到上下文上。"""

    def __init__(
        self,
        *,
        http: HttpClient | None = None,
        js_context: JSContext | None = None,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.http = http
        self.logger = logger
        self.js = js_context or JSContext(logger=logger)

    # ------------------------------------------------------------------
    # 对外
    # ------------------------------------------------------------------
    def evaluate(self, rule: str, ctx: RuleContext) -> list[Any]:
        ctx._engine = self
        if ctx.http is None:
            ctx.http = self.http
        rule = (rule or "").strip()
        if not rule:
            return []
        return _as_list(self._eval_rule(rule, ctx))

    def string(self, rule: str, ctx: RuleContext, *, unescape: bool = True) -> str:
        if not (rule or "").strip():
            return ""
        values = self.evaluate(rule, ctx)
        parts = [_value_to_string(value) for value in values]
        result = "\n".join(part for part in parts if part != "").strip()
        if unescape and "&" in result:
            result = _html.unescape(result)
        return result

    def strings(self, rule: str, ctx: RuleContext) -> list[str]:
        if not (rule or "").strip():
            return []
        values = self.evaluate(rule, ctx)
        result: list[str] = []
        for value in values:
            text = _value_to_string(value)
            if not text:
                continue
            if "&" in text:
                text = _html.unescape(text)
            result.extend(line.strip() for line in text.splitlines() if line.strip())
        return result

    def first_string(self, rule: str, ctx: RuleContext) -> str:
        if not (rule or "").strip():
            return ""
        values = self.evaluate(rule, ctx)
        if not values:
            return ""
        return _value_to_string(values[0])

    def elements(self, rule: str, ctx: RuleContext) -> list[Any]:
        if not (rule or "").strip():
            return []
        values = self.evaluate(rule, ctx)
        result: list[Any] = []
        for value in values:
            if isinstance(value, (etree._Element, dict, RegexMatch)):
                result.append(value)
            elif isinstance(value, list):
                result.extend(value)
        return result

    # ------------------------------------------------------------------
    # 规则拆分与求值
    # ------------------------------------------------------------------
    def _eval_rule(self, rule: str, ctx: RuleContext) -> Any:
        result: Any = None
        position = 0
        for match in _JS_RE.finditer(rule):
            prefix = rule[position : match.start()].strip()
            if prefix:
                result = self._eval_normal(prefix, ctx, result)
                result = _js_input(result)
            code = match.group(1) or match.group(2) or ""
            js_ctx = ctx if result is None else ctx.derive(result)
            result = self._eval_js(code, js_ctx)
            position = match.end()
        tail = rule[position:].strip()
        if tail:
            result = self._eval_normal(tail, ctx, result)
        if result is None:
            return _as_list(ctx.result)
        return result

    def _eval_normal(self, rule: str, ctx: RuleContext, input_result: Any = None) -> Any:
        base_ctx = ctx if input_result is None else ctx.derive(input_result)
        alternatives = _split_top(rule, ("||",))
        if len(alternatives) > 1:
            for _op, chunk in alternatives:
                chunk = chunk.strip()
                if not chunk:
                    continue
                value = self._eval_concat(chunk, base_ctx)
                if value:
                    return value
            return []

        return self._eval_concat(rule, base_ctx)

    def _eval_concat(self, rule: str, ctx: RuleContext) -> list[Any]:
        parts = _split_top(rule, ("&&", "%%"))
        if len(parts) == 1:
            return self._eval_part(parts[0][1].strip(), ctx)

        operator = next((op for op, _ in parts[1:] if op), "&&")
        evaluated: list[list[Any]] = []
        for _op, chunk in parts:
            chunk = chunk.strip()
            if not chunk:
                continue
            evaluated.append(self._eval_part(chunk, ctx))
        if not evaluated:
            return []
        if operator == "||":
            for values in evaluated:
                if values:
                    return values
            return []
        if operator == "%%":
            result: list[Any] = []
            longest = max(len(values) for values in evaluated)
            for index in range(longest):
                for values in evaluated:
                    if index < len(values):
                        result.append(values[index])
            return result
        result = []
        for values in evaluated:
            result.extend(values)
        return result

    def _eval_part(self, part: str, ctx: RuleContext) -> list[Any]:
        if not part:
            return _as_list(ctx.result)

        # $1 / $2：正则书源列表中的分组
        if re.fullmatch(r"\$\d{1,2}", part) and isinstance(ctx.result, RegexMatch):
            return [ctx.result.group(int(part[1:]))]

        # @put 先执行，然后从规则中移除
        puts = _PUT_RE.findall(part)
        if puts:
            part = _PUT_RE.sub("", part).strip()
            for payload in puts:
                self._apply_put(payload, ctx)
            if not part:
                return []

        had_template = "{{" in part or bool(_GET_RE.search(part))
        part = _substitute(part, ctx)
        if not part:
            return []
        if had_template and not _looks_like_rule(part):
            return [part]

        rewrite = _split_replace(part)
        base, pattern, replacement, replace_first = rewrite
        values = self._eval_base(base, ctx)
        if pattern is not None:
            values = [
                _apply_regex_replace(_value_to_string(value), pattern, replacement, replace_first)
                for value in values
            ]
        return values

    def _eval_base(self, rule: str, ctx: RuleContext) -> list[Any]:
        rule = rule.strip()
        if not rule:
            return _as_list(ctx.result)

        lowered = rule.lower()
        if lowered.startswith("@@"):
            rule = rule[2:].strip()
            return self._eval_default(rule, ctx)
        if lowered.startswith("@css:"):
            return self._eval_css(rule[5:], ctx)
        if lowered.startswith("@xpath:"):
            return self._eval_xpath(rule[7:], ctx)
        if lowered.startswith("@json:"):
            return self._eval_json(rule[6:], ctx)
        if rule.startswith("$.") or rule.startswith("$["):
            return self._eval_json(rule, ctx)
        if ctx.json_mode:
            return self._eval_json(rule, ctx)
        if rule.startswith("//") or rule.startswith("/"):
            return self._eval_xpath(rule, ctx)
        if rule.startswith(":") and ctx.stage in ("search", "explore", "toc"):
            return self._eval_regex(rule[1:], ctx)
        return self._eval_default(rule, ctx)

    # ------------------------------------------------------------------
    # 默认规则
    # ------------------------------------------------------------------
    def _eval_default(self, rule: str, ctx: RuleContext) -> list[Any]:
        tokens = [token.strip() for token in rule.split("@")]
        tokens = [token for token in tokens if token]
        if not tokens:
            return _as_list(ctx.result)

        nodes = _roots(ctx.result)
        if not nodes:
            if isinstance(ctx.result, str):
                return [ctx.result]
            return []

        # 只有一段：可能是选择器，也可能是属性
        if len(tokens) == 1:
            token = tokens[0]
            if _looks_like_selector(token):
                return self._select_steps(nodes, [token])
            if isinstance(ctx.result, etree._Element):
                return _extract_from_elements([ctx.result], token)
            return [ctx.result]

        *steps, last = tokens
        selected = self._select_steps(nodes, steps)
        if not selected:
            return []
        if _is_action(last):
            return _extract_from_elements(selected, last)
        if _looks_like_selector(last):
            return self._select_steps(selected, [last])
        return _extract_from_elements(selected, last)

    def _select_steps(self, nodes: list[Any], steps: list[str]) -> list[Any]:
        current = [node for node in nodes if isinstance(node, etree._Element)]
        for step in steps:
            if not current:
                return []
            current = self._select_one_step(current, step)
        return current

    def _select_one_step(self, nodes: list[Any], step: str) -> list[Any]:
        match = re.match(r"^(.*?)(?:\.(-?\d+)|\[(-?\d+)\])?$", step)
        selector = (match.group(1) if match else step).strip()
        index_text = None
        if match:
            index_text = match.group(2) if match.group(2) is not None else match.group(3)
        index = int(index_text) if index_text is not None else None
        exclude: set[int] | None = None
        exclude_match = re.search(r"!(-?\d+(?::-?\d+)*)$", selector)
        if exclude_match:
            exclude = {
                int(part) for part in exclude_match.group(1).split(":") if part
            }
            selector = selector[: exclude_match.start()].strip()

        results: list[Any] = []
        seen: set[int] = set()
        for node in nodes:
            if not isinstance(node, etree._Element):
                continue
            selected = self._select_in_node(node, selector)
            if index is not None:
                if -len(selected) <= index < len(selected):
                    selected = [selected[index]]
                else:
                    selected = []
            elif exclude and selected:
                length = len(selected)
                selected = [
                    item
                    for offset, item in enumerate(selected)
                    if not any(
                        (value % length if value < 0 else value) == offset
                        for value in exclude
                    )
                ]
            for item in selected:
                if id(item) not in seen:
                    seen.add(id(item))
                    results.append(item)
        return results

    @staticmethod
    def _select_in_node(node: etree._Element, selector: str) -> list[Any]:
        lowered = selector.lower()
        if lowered in _STRUCT_TOKENS:
            action = _STRUCT_TOKENS[lowered]
            if action == "children":
                return dom.elements(node)
            if action == "parent":
                parent = dom.parent_element(node)
                return [parent] if parent is not None else []
            if action == "sibling":
                sibling = dom.next_element(node)
                return [sibling] if sibling is not None else []
            if action == "prev":
                sibling = dom.prev_element(node)
                return [sibling] if sibling is not None else []
            return []

        for prefix in _SELECTOR_KINDS:
            if lowered.startswith(prefix):
                value = selector[len(prefix) :].strip()
                if prefix == "class.":
                    return [
                        item
                        for item in dom.descendants(node)
                        if value in (dom.attr(item, "class") or "").split()
                    ]
                if prefix == "id.":
                    return [item for item in dom.descendants(node) if dom.attr(item, "id") == value]
                if prefix == "tag.":
                    return [item for item in dom.descendants(node) if dom.tag_name(item) == value.lower()]
                if prefix == "text.":
                    return [item for item in dom.descendants(node) if value in dom.own_text(item)]
        try:
            return dom.select_css(node, selector)
        except dom.DOMError:
            return []

    # ------------------------------------------------------------------
    # CSS / XPath / JSON / 正则
    # ------------------------------------------------------------------
    def _eval_css(self, rule: str, ctx: RuleContext) -> list[Any]:
        rule = rule.strip()
        roots = _roots(ctx.result)
        if not roots:
            return []
        selector, action = _split_css_action(rule)
        elements: list[Any] = []
        for root in roots:
            try:
                elements.extend(dom.select_css(root, selector))
            except dom.DOMError as exc:
                raise RuleError(str(exc)) from exc
        if action is None:
            return elements
        return _extract_from_elements(elements, action)

    def _eval_xpath(self, expr: str, ctx: RuleContext) -> list[Any]:
        expr = expr.strip()
        roots = _roots(ctx.result)
        if not roots and isinstance(ctx.result, str) and "<" in ctx.result:
            try:
                roots = [dom.parse_html(ctx.result)]
            except dom.DOMError:
                roots = []
        if not roots:
            return []
        values: list[Any] = []
        for root in roots:
            try:
                values.extend(dom.select_xpath(root, expr))
            except dom.DOMError as exc:
                raise RuleError(str(exc)) from exc
        return values

    def _eval_json(self, expr: str, ctx: RuleContext) -> list[Any]:
        expr = expr.strip()
        payload = ctx.result
        if isinstance(payload, (str, bytes, bytearray)):
            try:
                payload = dom.parse_json(payload.decode("utf-8") if isinstance(payload, (bytes, bytearray)) else payload)
            except dom.DOMError as exc:
                raise RuleError(str(exc)) from exc
        if not expr:
            return _as_list(payload)
        try:
            return dom.json_path(payload, expr)
        except dom.DOMError as exc:
            raise RuleError(str(exc)) from exc

    def _eval_regex(self, pattern: str, ctx: RuleContext) -> list[Any]:
        text = _value_to_string(ctx.result)
        try:
            matches = list(re.finditer(pattern, text))
        except re.error as exc:
            raise RuleError(f"正则错误：{pattern}（{exc}）") from exc
        return [RegexMatch(match) for match in matches]

    # ------------------------------------------------------------------
    # @put / JS
    # ------------------------------------------------------------------
    def _apply_put(self, payload: str, ctx: RuleContext) -> None:
        for key, value_rule in _parse_mapping(payload).items():
            try:
                value = self.string(value_rule, ctx)
            except RuleError:
                value = ""
            ctx.variables[key] = value
            if ctx.book is not None and hasattr(ctx.book, "variables"):
                try:
                    ctx.book.variables[key] = value
                except Exception:
                    pass

    def _eval_js(self, code: str, ctx: RuleContext) -> Any:
        scope = self._js_scope(ctx)
        code = self._with_js_lib(code, ctx)
        try:
            return self.js.eval(code, scope)
        except JSError as exc:
            raise RuleError(str(exc)) from exc

    def eval_expression(self, code: str, ctx: RuleContext) -> Any:
        """执行一段 JS 表达式（URL 模板、exploreUrl、formatJs 等共用）。"""
        ctx._engine = self
        if ctx.http is None:
            ctx.http = self.http
        return self._eval_js(code, ctx)

    @staticmethod
    def _with_js_lib(code: str, ctx: RuleContext) -> str:
        lib = getattr(ctx.source, "js_lib", "") if ctx.source is not None else ""
        lib = (lib or "").strip()
        if not lib:
            return code
        return lib + "\n;\n" + code

    def _js_scope(self, ctx: RuleContext) -> dict[str, Any]:
        source = ctx.source
        book = _book_dict(ctx.book)
        chapter = _chapter_dict(ctx.chapter)
        return {
            "java": self._java_bridge(ctx),
            "result": _js_result(ctx.result),
            "baseUrl": ctx.base_url,
            "title": ctx.title or chapter.get("title", ""),
            "src": _js_result(ctx.result),
            "nextChapterUrl": ctx.next_chapter_url,
            "book": book,
            "chapter": chapter,
            "source": _source_dict(source),
            "cookie": _cookie_object(ctx),
            "cache": _cache_object(ctx),
            "key": ctx.variables.get("key", ""),
            "page": ctx.variables.get("page", 0),
            "infoMap": {},
            "fromBookInfo": ctx.stage == "book",
            "Packages": {
                "java": {
                    "lang": {
                        "Thread": {
                            "sleep": lambda ms: time.sleep(
                                max(0.0, min(10.0, float(ms) / 1000.0))
                            )
                        },
                        "System": {
                            "currentTimeMillis": lambda: int(time.time() * 1000)
                        },
                    }
                }
            },
        }

    def _java_bridge(self, ctx: RuleContext) -> dict[str, Any]:
        http = ctx.http or self.http

        def client() -> HttpClient:
            if http is None:
                raise RuleError("当前书源没有可用的网络层")
            return http

        def headers_for(extra: Any = None) -> dict[str, str]:
            result = (
                {
                    key: value
                    for key, value in ctx.source.headers.items()
                    if key.lower() != "proxy"
                }
                if ctx.source is not None
                else {}
            )
            if isinstance(extra, dict):
                result.update({str(key): str(value) for key, value in extra.items()})
            return result

        def perform(
            url: Any,
            *,
            method: str | None = None,
            body: Any = None,
            headers: Any = None,
            timeout: Any = None,
        ) -> Any:
            raw, options = _split_url_options(str(url))
            target = absolute_url(ctx.base_url, raw)
            merged = headers_for(options.get("headers"))
            if isinstance(headers, dict):
                merged.update({str(key): str(value) for key, value in headers.items()})
            chosen_method = str(method or options.get("method") or "GET").upper()
            chosen_body = body if body is not None else options.get("body")

            def send(request_url: str, request_method: str, request_body: Any) -> Any:
                return client().request(
                    request_url,
                    method=request_method,
                    headers=merged,
                    body=request_body,
                    encoding=str(options.get("charset") or ""),
                    timeout=float(timeout) if timeout else None,
                    retries=_to_int(options.get("retry"), 0) or None,
                )

            alternate = (
                alternate_host_url(target, ctx.source.url)
                if ctx.source is not None
                else ""
            )
            candidates: list[tuple[str, str, Any]] = [(target, chosen_method, chosen_body)]
            if alternate:
                candidates.append((alternate, chosen_method, chosen_body))
            if chosen_method == "GET":
                candidates.append((target, "POST", None))
                if alternate:
                    candidates.append((alternate, "POST", None))

            last_error: NetworkError | None = None
            for request_url, request_method, request_body in candidates:
                try:
                    return send(request_url, request_method, request_body)
                except NetworkError as exc:
                    if not is_connection_refused(exc):
                        raise
                    last_error = exc
                    continue
            if last_error is not None:
                raise last_error
            raise RuleError("请求失败")

        def ajax(url: Any, timeout: Any = None) -> str:
            try:
                response = perform(url, timeout=timeout)
                ctx.log(f"java.ajax {response.request_url} -> {response.status}")
                return response.text
            except NetworkError as exc:
                ctx.log(f"java.ajax 失败：{exc}")
                return str(exc)
            except Exception as exc:
                ctx.log(f"java.ajax 失败：{exc}")
                return str(exc)

        def request_json(url: Any, body: Any = None, method: str = "GET", headers: Any = None) -> Any:
            try:
                return perform(url, method=method, body=body, headers=headers)
            except NetworkError:
                return None

        def raw_response(response: Any) -> dict[str, Any]:
            return {
                "code": response.status,
                "body": response.text,
                "request": lambda: {
                    "url": lambda: response.url,
                    "method": lambda: "GET",
                    "headers": lambda: dict(response.headers),
                },
            }

        def connect(url: Any, header: Any = None) -> dict[str, Any]:
            response = request_json(url, headers=header)
            if response is None:
                return {"body": "", "code": 0, "url": str(url), "headers": {}}
            return {
                "body": response.text,
                "code": response.status,
                "url": response.url,
                "headers": dict(response.headers),
                "raw": lambda: raw_response(response),
            }

        def get(url: Any, headers: Any = None, timeout: Any = None) -> dict[str, Any]:
            response = request_json(url, headers=headers)
            if response is None:
                return {
                    "body": lambda: "",
                    "code": 0,
                    "statusCode": lambda: 0,
                    "url": str(url),
                    "headers": lambda: {},
                    "header": lambda name: "",
                    "raw": lambda: {"code": 0, "request": lambda: {"url": lambda: str(url)}},
                }
            return {
                "body": lambda: response.text,
                "code": response.status,
                "statusCode": lambda: response.status,
                "url": response.url,
                "headers": lambda: dict(response.headers),
                "header": lambda name: response.header(str(name)),
                "raw": lambda: raw_response(response),
            }

        def post(url: Any, body: Any = None, headers: Any = None, timeout: Any = None) -> dict[str, Any]:
            response = request_json(url, body=body, method="POST", headers=headers)
            if response is None:
                return {
                    "body": lambda: "",
                    "code": 0,
                    "statusCode": lambda: 0,
                    "url": str(url),
                    "headers": lambda: {},
                    "header": lambda name: "",
                    "raw": lambda: {"code": 0, "request": lambda: {"url": lambda: str(url)}},
                }
            return {
                "body": lambda: response.text,
                "code": response.status,
                "statusCode": lambda: response.status,
                "url": response.url,
                "headers": lambda: dict(response.headers),
                "header": lambda name: response.header(str(name)),
                "raw": lambda: raw_response(response),
            }

        def ajax_all(urls: Any) -> list[dict[str, Any]]:
            result: list[dict[str, Any]] = []
            for url in urls or []:
                text = ajax(url)
                result.append({"body": text, "url": str(url), "code": 200})
            return result

        def get_cookie(tag: Any = "") -> str:
            if http is None:
                return ""
            target = absolute_url(ctx.base_url, str(tag)) if tag else ctx.base_url
            return http.get_cookie(target)

        def put(key: Any, value: Any) -> str:
            name = str(key)
            ctx.variables[name] = value
            if ctx.book is not None and hasattr(ctx.book, "variables"):
                try:
                    ctx.book.variables[name] = value
                except Exception:
                    pass
            if ctx.source is not None and isinstance(getattr(ctx.source, "cache", None), dict):
                ctx.source.cache[f"__var__{name}"] = value
            return str(value)

        def get_var(key: Any, default: Any = "") -> Any:
            name = str(key)
            if name == "bookName":
                return _book_dict(ctx.book).get("name", "")
            if name == "title":
                return ctx.title or _chapter_dict(ctx.chapter).get("title", "")
            if name in ctx.variables:
                return ctx.variables[name]
            if ctx.source is not None and isinstance(getattr(ctx.source, "cache", None), dict):
                return ctx.source.cache.get(f"__var__{name}", default)
            return default

        def query_ttf(*_args: Any, **_kwargs: Any) -> None:
            ctx.log("queryTTF 在桌面版暂不可用")
            return None

        def sleep(ms: Any) -> None:
            try:
                seconds = float(ms) / 1000.0
            except (TypeError, ValueError):
                seconds = 0.0
            time.sleep(max(0.0, min(10.0, seconds)))

        return {
            "ajax": ajax,
            "ajaxAll": ajax_all,
            "connect": connect,
            "get": get,
            "post": post,
            "sleep": sleep,
            "base64Encode": lambda text, flags=2: _b64_encode(text),
            "base64Decode": lambda text, *args: _b64_decode(text),
            "md5Encode": lambda text: _md5(text),
            "md5Encode16": lambda text: _md5(text)[8:24],
            "hexEncodeToString": lambda text: str(text).encode("utf-8").hex(),
            "hexDecodeToString": lambda text: bytes.fromhex(str(text)).decode("utf-8", "replace"),
            "strToBytes": lambda text, charset="UTF-8": str(text),
            "bytesToStr": lambda data, charset="UTF-8": str(data),
            "timeFormat": lambda value: _time_format(value),
            "timeFormatUTC": lambda value, fmt, offset: _time_format(value, fmt, offset),
            "encodeURI": lambda text, charset="UTF-8": _encode_uri(text, charset),
            "htmlFormat": lambda text: html_to_text(str(text)),
            "toNumChapter": lambda text: text,
            "randomUUID": lambda: str(uuid.uuid4()),
            "log": lambda message: ctx.log(f"[JS] {message}"),
            "toast": lambda message: ctx.log(f"[JS toast] {message}"),
            "longToast": lambda message: ctx.log(f"[JS toast] {message}"),
            "logType": lambda value: ctx.log(f"[JS type] {type(value).__name__}"),
            "getCookie": get_cookie,
            "put": put,
            "get": get_var,
            "queryTTF": query_ttf,
        }


# ----------------------------------------------------------------------
# 上下文辅助
# ----------------------------------------------------------------------
def _is_single_group(rule: str) -> bool:
    return bool(re.fullmatch(r"\$\d{1,2}", rule))


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _roots(result: Any) -> list[etree._Element]:
    if isinstance(result, etree._Element):
        return [result]
    if isinstance(result, str) and "<" in result:
        try:
            return [dom.parse_html(result)]
        except dom.DOMError:
            return []
    if isinstance(result, list):
        return [item for item in result if isinstance(item, etree._Element)]
    return []


def _looks_like_selector(token: str) -> bool:
    lowered = token.lower()
    if lowered in _STRUCT_TOKENS:
        return True
    if lowered in _HTML_TAGS:
        return True
    if any(lowered.startswith(prefix) for prefix in _SELECTOR_KINDS):
        return True
    if lowered.startswith(("html.", "body.", "#", ".", "[")):
        return True
    if any(char in token for char in (" ", ">", "[", "#", ".")):
        return True
    return False


def _looks_like_rule(text: str) -> bool:
    text = text.strip()
    if not text:
        return False
    lowered = text.lower()
    if lowered.startswith(("@@", "@css:", "@xpath:", "@json:", "@js:")):
        return True
    if text.startswith(("$.", "$[", "//")):
        return True
    if any(lowered.startswith(prefix) for prefix in _SELECTOR_KINDS):
        return True
    if lowered in _STRUCT_TOKENS:
        return True
    if "@" in text:
        return True
    return False


def _is_action(token: str) -> bool:
    return token.lower() in _ACTION_TOKENS


def _extract_from_elements(elements: Iterable[Any], action: str) -> list[Any]:
    lowered = action.lower()
    values: list[Any] = []
    for element in elements:
        if not isinstance(element, etree._Element):
            continue
        if lowered == "text":
            text = dom.text_of(element)
            if text:
                values.append(text)
        elif lowered == "text_nodes":
            parts = []
            if element.text and element.text.strip():
                parts.append(element.text.strip())
            for child in element:
                if child.tail and child.tail.strip():
                    parts.append(child.tail.strip())
            if parts:
                values.append("\n".join(parts))
        elif lowered == "own_text":
            text = dom.own_text(element)
            if text:
                values.append(text)
        elif lowered in ("html", "all"):
            html = dom.outer_html(element)
            html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", html)
            if html:
                values.append(html)
        else:
            value = dom.attr(element, action)
            if value and value not in values:
                values.append(value)
    return values


def _split_css_action(rule: str) -> tuple[str, str | None]:
    rule = rule.strip()
    if not rule:
        return "", None
    if "@" not in rule:
        return rule, None
    selector, _, action = rule.rpartition("@")
    action = action.strip()
    if action.lower() in _ACTION_TOKENS or re.fullmatch(r"[\w:-]+", action):
        return selector.strip(), action.lower()
    return rule, None


def _value_to_string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, etree._Element):
        return dom.text_of(value)
    if isinstance(value, RegexMatch):
        return value.group(0)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(_value_to_string(item) for item in value)
    if isinstance(value, dict):
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)
    return str(value).strip()


def _book_dict(book: Any) -> dict[str, Any]:
    if book is None:
        return {}
    if isinstance(book, dict):
        return book
    if isinstance(book, (BookInfo, SearchBook)):
        return book.to_dict()
    if hasattr(book, "to_dict"):
        try:
            return book.to_dict()
        except Exception:
            pass
    return {}


def _chapter_dict(chapter: Any) -> dict[str, Any]:
    if chapter is None:
        return {}
    if isinstance(chapter, dict):
        return chapter
    if isinstance(chapter, Chapter):
        return chapter.to_dict()
    if hasattr(chapter, "to_dict"):
        try:
            return chapter.to_dict()
        except Exception:
            pass
    return {}


def _source_dict(source: Any) -> dict[str, Any]:
    if source is None:
        return {}
    cache = getattr(source, "cache", None)
    if not isinstance(cache, dict):
        cache = {}

    def get(key: Any, default: Any = "") -> Any:
        return cache.get(str(key), default)

    def put(key: Any, value: Any) -> str:
        cache[str(key)] = value
        return str(value)

    def get_variable() -> str:
        return str(getattr(source, "variable_text", "") or "")

    def put_variable(value: Any) -> None:
        setattr(source, "variable_text", "" if value is None else str(value))

    return {
        "key": getattr(source, "url", ""),
        "bookSourceUrl": getattr(source, "url", ""),
        "bookSourceName": getattr(source, "name", ""),
        "bookSourceGroup": getattr(source, "group", ""),
        "bookSourceComment": getattr(source, "comment", ""),
        "bookSourceType": getattr(source, "type", 0),
        "concurrentRate": getattr(source, "concurrent_rate", ""),
        "header": getattr(source, "header", ""),
        "jsLib": getattr(source, "js_lib", ""),
        "enabledCookieJar": getattr(source, "enabled_cookie_jar", True),
        "loginUrl": getattr(source, "login_url", ""),
        "loginUi": getattr(source, "login_ui", ""),
        "variableComment": getattr(source, "variable_comment", ""),
        "getKey": lambda: getattr(source, "url", ""),
        "getTag": lambda: getattr(source, "name", ""),
        "get": get,
        "put": put,
        "getVariable": get_variable,
        "putVariable": put_variable,
        "setVariable": put_variable,
        "getLoginHeader": lambda: "",
        "putLoginHeader": lambda header: None,
        "removeLoginHeader": lambda: None,
    }


def _cookie_object(ctx: RuleContext) -> dict[str, Any]:
    http = ctx.http

    def target(url: Any) -> str:
        return absolute_url(ctx.base_url, str(url)) if url else ctx.base_url

    def get(url: Any = "") -> str:
        if http is None:
            return ""
        try:
            return http.get_cookie(target(url))
        except Exception:
            return ""

    def set_cookie(url: Any, cookie: Any) -> None:
        if http is None:
            return
        try:
            http.set_cookie(target(url), str(cookie or ""))
        except Exception:
            pass

    def replace(url: Any, cookie: Any) -> None:
        if http is not None:
            try:
                http.remove_cookies(target(url))
            except Exception:
                pass
        set_cookie(url, cookie)

    def remove(url: Any = "") -> None:
        if http is None:
            return
        try:
            http.remove_cookies(target(url))
        except Exception:
            pass

    def get_key(url: Any, key: Any) -> str:
        text = get(url)
        for part in text.split(";"):
            name, _, value = part.strip().partition("=")
            if name == str(key):
                return value
        return ""

    def cookie_to_map(cookie: Any) -> dict[str, str]:
        result: dict[str, str] = {}
        for part in str(cookie or "").split(";"):
            name, sep, value = part.strip().partition("=")
            if sep and name:
                result[name] = value
        return result

    def map_to_cookie(mapping: Any) -> str:
        if not isinstance(mapping, dict):
            return ""
        return "; ".join(f"{key}={value}" for key, value in mapping.items())

    return {
        "getCookie": get,
        "setCookie": set_cookie,
        "replaceCookie": replace,
        "removeCookie": remove,
        "getKey": get_key,
        "cookieToMap": cookie_to_map,
        "mapToCookie": map_to_cookie,
        "clear": lambda: http.clear_cookies() if http is not None else None,
    }


def _cache_object(ctx: RuleContext) -> dict[str, Any]:
    source_cache = getattr(ctx.source, "cache", None) if ctx.source is not None else None
    store = source_cache if isinstance(source_cache, dict) else ctx.cache

    def get(key: Any, default: Any = "") -> Any:
        return store.get(str(key), default)

    def put(key: Any, value: Any) -> None:
        store[str(key)] = value

    def delete(key: Any) -> None:
        store.pop(str(key), None)

    return {
        "get": get,
        "put": put,
        "remove": delete,
        "delete": delete,
        "getFromMemory": get,
        "putMemory": put,
        "deleteMemory": delete,
    }


def _js_result(value: Any) -> Any:
    if isinstance(value, etree._Element):
        return dom.outer_html(value)
    if isinstance(value, RegexMatch):
        return value.group(0)
    if isinstance(value, list):
        return [_js_result(item) for item in value]
    if isinstance(value, dict):
        return {key: _js_result(item) for key, item in value.items()}
    return value


def _js_input(value: Any) -> Any:
    """把上一条普通规则的结果转成 JS 更容易使用的形态。

    legado 的 ``getString`` 在进入 ``@js:`` 前会把 Elements 转成字符串；
    这里保持同样行为，但 JSON 对象/数组仍原样交给 JS，方便 ``result.id``。
    """
    if isinstance(value, list):
        if len(value) == 1 and isinstance(value[0], (str, etree._Element, RegexMatch)):
            return _value_to_string(value[0])
        if value and all(isinstance(item, str) for item in value):
            return "\n".join(value)
        return value
    if isinstance(value, etree._Element):
        return _value_to_string(value)
    return value


# ----------------------------------------------------------------------
# 模板 / 变量 / @put
# ----------------------------------------------------------------------
def _substitute(text: str, ctx: RuleContext) -> str:
    text = _GET_RE.sub(lambda match: str(ctx.variables.get(match.group(1).strip(), "")), text)
    if ctx.result is not None and isinstance(ctx.result, RegexMatch):
        text = re.sub(r"\$(\d{1,2})", lambda match: ctx.result.group(int(match.group(1))), text)

    def replace_template(match: re.Match[str]) -> str:
        expr = match.group(1).strip()
        if not expr:
            return ""
        if expr.startswith(("@", "$.", "$[", "//")):
            engine = ctx._engine
            if engine is None:
                return ""
            try:
                return engine.string(expr, ctx)
            except RuleError:
                return ""
        engine = ctx._engine
        if engine is None:
            return str(ctx.variables.get(expr, ""))
        try:
            value = engine.eval_expression(expr, ctx)
        except Exception:
            value = ctx.variables.get(expr, "")
        return _js_to_text(value)

    return _TEMPLATE_RE.sub(replace_template, text)


def _js_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _parse_mapping(payload: str) -> dict[str, str]:
    payload = payload.strip()
    if payload.startswith("{"):
        payload = payload[1:]
    if payload.endswith("}"):
        payload = payload[:-1]
    result: dict[str, str] = {}
    for item in _split_commas(payload):
        if ":" not in item:
            continue
        key, _, value = item.partition(":")
        key = key.strip().strip("\"'")
        if key:
            result[key] = value.strip()
    return result


def _split_url_options(url: str) -> tuple[str, dict[str, Any]]:
    """拆出 legado URL 末尾的 ``,{json 选项}``。"""
    text = str(url or "")
    match = re.search(r"\s*,\s*(\{.*\})\s*$", text, re.S)
    if not match:
        return text, {}
    payload = match.group(1)
    try:
        options = json.loads(payload)
    except ValueError:
        quoted = re.sub(r"([{,]\s*)([A-Za-z_][\w]*)\s*:", r'\1"\2":', payload)
        try:
            options = json.loads(quoted)
        except ValueError:
            options = None
    if not isinstance(options, dict):
        return text, {}
    return text[: match.start()].strip(), options


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _split_commas(text: str) -> list[str]:
    parts: list[str] = []
    buffer: list[str] = []
    depth = 0
    quote = ""
    for char in text:
        if quote:
            buffer.append(char)
            if char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
            buffer.append(char)
            continue
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(0, depth - 1)
        if char == "," and depth == 0:
            parts.append("".join(buffer))
            buffer = []
            continue
        buffer.append(char)
    if buffer:
        parts.append("".join(buffer))
    return parts


def _split_top(text: str, separators: Sequence[str]) -> list[tuple[str, str]]:
    """按顶层分隔符拆分；跳过引号、括号与 ``## 正则 ##`` 区域。"""
    if not text:
        return [("", "")]
    seps = sorted(separators, key=len, reverse=True)
    pieces: list[tuple[str, str]] = []
    buffer: list[str] = []
    op = ""
    depth = 0
    quote = ""
    in_regex = False
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if in_regex:
            buffer.append(char)
            if char == "\\" and index + 1 < length:
                buffer.append(text[index + 1])
                index += 2
                continue
            if char == "#" and index + 1 < length and text[index + 1] == "#":
                buffer.append("#")
                index += 2
                in_regex = False
                continue
            index += 1
            continue
        if quote:
            buffer.append(char)
            if char == "\\" and index + 1 < length:
                buffer.append(text[index + 1])
                index += 2
                continue
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in "\"'`":
            quote = char
            buffer.append(char)
            index += 1
            continue
        if char == "#" and index + 1 < length and text[index + 1] == "#":
            buffer.append("##")
            index += 2
            in_regex = True
            continue
        if char in "([{":
            depth += 1
            buffer.append(char)
            index += 1
            continue
        if char in ")]}":
            depth = max(0, depth - 1)
            buffer.append(char)
            index += 1
            continue
        if depth == 0:
            matched = next((sep for sep in seps if text.startswith(sep, index)), "")
            if matched:
                pieces.append((op, "".join(buffer)))
                buffer = []
                op = matched
                index += len(matched)
                continue
        buffer.append(char)
        index += 1
    pieces.append((op, "".join(buffer)))
    return pieces


def _split_replace(rule: str) -> tuple[str, str | None, str, bool]:
    if "##" not in rule:
        return rule, None, "", False
    pieces = rule.split("##")
    if len(pieces) < 2:
        return rule, None, "", False
    base = pieces[0]
    pattern = pieces[1]
    replacement = pieces[2] if len(pieces) > 2 else ""
    replace_first = len(pieces) > 3
    return base, pattern, replacement, replace_first


def _apply_regex_replace(text: str, pattern: str, replacement: str, replace_first: bool) -> str:
    if not pattern:
        return text
    replacement = re.sub(r"\$(\d{1,2})", lambda match: r"\g<" + match.group(1) + ">", replacement)
    replacement = replacement.replace("$$", "$")
    try:
        if replace_first:
            match = re.search(pattern, text)
            if not match:
                return ""
            return text[: match.start()] + match.expand(replacement) + text[match.end() :]
        return re.sub(pattern, replacement, text)
    except re.error:
        return text


# ----------------------------------------------------------------------
# JS 辅助函数
# ----------------------------------------------------------------------
def _encode_uri(text: Any, charset: str = "UTF-8") -> str:
    try:
        return urllib.parse.quote_plus(
            str(text), encoding=str(charset or "UTF-8"), errors="replace", safe=""
        )
    except LookupError:
        return urllib.parse.quote_plus(str(text), safe="")


def _b64_encode(text: Any) -> str:
    return base64.b64encode(str(text).encode("utf-8")).decode("ascii")


def _b64_decode(text: Any) -> str:
    raw = str(text or "").strip()
    raw += "=" * (-len(raw) % 4)
    try:
        return base64.b64decode(raw).decode("utf-8", "replace")
    except Exception:
        return ""


def _md5(text: Any) -> str:
    return hashlib.md5(str(text).encode("utf-8")).hexdigest()


def _time_format(value: Any, fmt: str = "%Y-%m-%d %H:%M:%S", offset_hours: Any = 8) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value or "")
    if number > 10_000_000_000:
        number /= 1000
    try:
        return time.strftime(fmt, time.localtime(number))
    except (OverflowError, OSError, ValueError):
        return str(value)


def html_to_text(source: str, *, keep_image_url: bool = False) -> str:
    return dom.html_to_text(source, keep_image_url=keep_image_url)
