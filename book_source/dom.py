# -*- coding: utf-8 -*-
"""HTML / XML / JSON 解析底座。

书源规则需要在 HTML、XPath、CSS、JSON 之间来回切换。这里统一使用：

* ``lxml`` 解析 HTML / XML（容错、真实 XPath 1.0）；
* ``cssselect`` 执行 CSS 选择器；
* ``jsonpath-ng`` 执行 JSONPath。

这些依赖是「书源阅读」功能的可选依赖，不影响小工作箱的其它工具。
"""

from __future__ import annotations

import json
import re
from typing import Any

from lxml import etree
from lxml import html as lxml_html
from jsonpath_ng.ext import parse as _jsonpath_parse

__all__ = [
    "DOMError",
    "parse_html",
    "parse_json",
    "select_css",
    "select_xpath",
    "json_path",
    "text_of",
    "own_text",
    "inner_html",
    "outer_html",
    "attr",
    "elements",
    "descendants",
    "next_element",
    "prev_element",
    "parent_element",
    "tag_name",
    "is_element",
    "looks_like_json",
    "html_to_text",
]

_CONTAINS_RE = re.compile(
    r""":contains(?:Own)?\(\s*(?P<q>["']?)(?P<text>.*?)(?P=q)\s*\)""", re.S
)
_SCRIPT_STYLE_RE = re.compile(r"(?is)<(script|style|iframe)[^>]*>.*?</\1>")
_BR_RE = re.compile(r"(?i)<br\s*/?>")
_BLOCK_CLOSE_RE = re.compile(
    r"(?i)</(p|div|li|tr|h[1-6]|section|article|blockquote|dd|dt|ul|ol|table|pre|figure)>"
)
_TAG_RE = re.compile(r"(?s)<[^>]+>")


class DOMError(ValueError):
    """HTML / XPath / JSONPath 解析失败。"""


def is_element(node: Any) -> bool:
    return isinstance(node, etree._Element) and isinstance(node.tag, str)


def parse_html(text: str | bytes) -> etree._Element:
    """把 HTML 文本解析为文档节点。"""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    text = (text or "").strip()
    if not text:
        return lxml_html.document_fromstring("<html><body></body></html>")
    parser = lxml_html.HTMLParser(
        encoding="utf-8",
        recover=True,
        remove_comments=False,
        remove_blank_text=False,
    )
    try:
        return lxml_html.document_fromstring(text, parser=parser)
    except (etree.ParserError, ValueError) as exc:  # pragma: no cover - 极端输入
        raise DOMError(f"HTML 解析失败：{exc}") from exc


def parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError as exc:
        raise DOMError(f"JSON 解析失败：{exc}") from exc


def looks_like_json(text: str) -> bool:
    stripped = (text or "").lstrip()
    if not stripped or stripped[0] not in "[{":
        return False
    try:
        json.loads(stripped)
        return True
    except ValueError:
        return False


def tag_name(node: Any) -> str:
    if not is_element(node):
        return ""
    tag = node.tag
    if tag.startswith("{"):
        tag = tag.split("}", 1)[-1]
    return tag.lower()


def attr(node: Any, name: str, default: str = "") -> str:
    if not is_element(node):
        return default
    value = node.get(name)
    return default if value is None else value


def elements(node: Any) -> list[etree._Element]:
    if not is_element(node):
        return []
    return [child for child in node if is_element(child)]


def descendants(node: Any) -> list[etree._Element]:
    if not is_element(node):
        return []
    return [item for item in node.iterdescendants() if is_element(item)]


def parent_element(node: Any) -> etree._Element | None:
    if not is_element(node):
        return None
    parent = node.getparent()
    return parent if is_element(parent) else None


def next_element(node: Any) -> etree._Element | None:
    if not is_element(node):
        return None
    sibling = node.getnext()
    while sibling is not None and not is_element(sibling):
        sibling = sibling.getnext()
    return sibling


def prev_element(node: Any) -> etree._Element | None:
    if not is_element(node):
        return None
    sibling = node.getprevious()
    while sibling is not None and not is_element(sibling):
        sibling = sibling.getprevious()
    return sibling


def text_of(node: Any) -> str:
    """等价于 jsoup 的 ``Element.text()``：合并所有后代文本并压缩空白。"""
    if not is_element(node):
        return str(node or "").strip()
    try:
        html = etree.tostring(node, encoding="unicode", method="html")
        text = html_to_text(html)
    except (etree.LxmlError, ValueError):
        text = "".join(node.itertext())
    return re.sub(r"\s+", " ", text).strip()


def _direct_text(node: Any) -> list[str]:
    if not is_element(node):
        return []
    parts: list[str] = []
    if node.text and node.text.strip():
        parts.append(node.text.strip())
    for child in node:
        if child.tail and child.tail.strip():
            parts.append(child.tail.strip())
    return parts


def own_text(node: Any) -> str:
    return " ".join(_direct_text(node))


def inner_html(node: Any) -> str:
    if not is_element(node):
        return str(node or "")
    parts: list[str] = []
    if node.text:
        parts.append(node.text)
    for child in node:
        parts.append(
            etree.tostring(child, encoding="unicode", method="html", with_tail=True)
        )
    return "".join(parts)


def outer_html(node: Any) -> str:
    if not is_element(node):
        return str(node or "")
    return etree.tostring(node, encoding="unicode", method="html", with_tail=False)


def select_css(root: Any, selector: str) -> list[etree._Element]:
    """执行 CSS 选择器，支持 jsoup 常用的 ``:contains``。"""
    selector = (selector or "").strip()
    if not selector or not is_element(root):
        return []
    selector = selector[5:] if selector.lower().startswith("@css:") else selector

    contains_text: str | None = None
    contains_own = False
    match = _CONTAINS_RE.search(selector)
    if match:
        contains_text = match.group("text")
        contains_own = ":containsOwn" in selector
        selector = _CONTAINS_RE.sub("", selector).strip() or "*"

    try:
        found = list(root.cssselect(selector))
    except Exception as exc:
        raise DOMError(f"CSS 选择器错误：{selector}（{exc}）") from exc

    if contains_text is not None:
        needle = contains_text.strip()
        found = [
            item
            for item in found
            if (own_text(item) if contains_own else text_of(item)).find(needle) >= 0
        ]
    return found


def select_xpath(root: Any, expr: str) -> list[Any]:
    expr = (expr or "").strip()
    if not expr or not isinstance(root, etree._Element):
        return []
    if expr.lower().startswith("@xpath:"):
        expr = expr[7:].strip()
    try:
        result = root.xpath(expr)
    except (etree.XPathError, etree.LxmlError) as exc:
        raise DOMError(f"XPath 错误：{expr}（{exc}）") from exc
    if not isinstance(result, list):
        result = [result]
    cleaned: list[Any] = []
    for item in result:
        if isinstance(item, etree._Element):
            if is_element(item):
                cleaned.append(item)
        elif isinstance(item, (str, int, float, bool)):
            cleaned.append(item)
        elif item is None:
            continue
        else:
            cleaned.append(str(item))
    return cleaned


def json_path(data: Any, expr: str) -> list[Any]:
    """执行 JSONPath；兼容不带 ``$.`` 前缀的 legado 写法。"""
    expr = (expr or "").strip()
    if isinstance(data, (bytes, bytearray)):
        data = data.decode("utf-8", "replace")
    if isinstance(data, str):
        data = parse_json(data)
    if not expr:
        return [data]
    if expr.lower().startswith("@json:"):
        expr = expr[6:].strip()
    if not expr:
        return [data]
    if not expr.startswith("$"):
        expr = "$." + expr.lstrip(".")
    try:
        matches = _jsonpath_parse(expr).find(data)
    except Exception as exc:
        raise DOMError(f"JSONPath 错误：{expr}（{exc}）") from exc
    return [match.value for match in matches]


def html_to_text(source: str, *, keep_image_url: bool = False) -> str:
    """把正文 HTML 转成可读纯文本。"""
    if not source:
        return ""
    text = _SCRIPT_STYLE_RE.sub("", source)
    text = _BR_RE.sub("\n", text)
    text = _BLOCK_CLOSE_RE.sub("\n", text)
    if keep_image_url:
        text = re.sub(
            r"""(?is)<img[^>]+src\s*=\s*["']?([^"'\s>]+)["']?[^>]*>""",
            lambda match: f"\n[图片]{match.group(1)}[/图片]\n",
            text,
        )
    else:
        text = re.sub(r"(?is)<img[^>]*>", "", text)
    text = _TAG_RE.sub("", text)
    import html as _html

    text = _html.unescape(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
