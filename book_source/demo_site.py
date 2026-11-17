# -*- coding: utf-8 -*-
"""内置示例站点。

用途：
* 单元测试：不依赖外网，验证搜索、详情、目录分页、正文分页、JSON 接口、
  GBK 编码、gzip 解压、JS 规则；
* 界面演示：启动后可以直接导入「示例书源」体验完整阅读流程。

站点只监听 127.0.0.1。
"""

from __future__ import annotations

import gzip
import html
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

__all__ = ["DemoSite", "BOOKS"]

BOOKS: dict[int, dict[str, Any]] = {
    1: {
        "name": "斗破苍穹",
        "author": "天蚕土豆",
        "intro": "这里是斗破苍穹的简介，少年萧炎从天才跌落，又在药老的帮助下重新崛起。",
        "cover": "/cover/1.png",
        "chapters": [
            "第1章 陨落的天才",
            "第2章 斗气大陆",
            "第3章 客人",
            "第4章 云岚宗",
            "第5章 药老",
            "第6章 休养生息",
        ],
    },
    2: {
        "name": "武动乾坤",
        "author": "天蚕土豆",
        "intro": "这里是武动乾坤的简介，林动在大炎王朝一步步走向强者之路。",
        "cover": "/cover/2.png",
        "chapters": [
            "第1章 林动",
            "第2章 石符",
            "第3章 淬体",
            "第4章 族比",
            "第5章 血衣门",
        ],
    },
    3: {
        "name": "诡秘之主",
        "author": "爱潜水的乌贼",
        "intro": "这里是诡秘之主的简介，克莱恩在蒸汽与机械的时代寻找回家的路。",
        "cover": "/cover/3.png",
        "chapters": [
            "第1章 绯红",
            "第2章 奥黛丽",
            "第3章 占卜",
            "第4章 值夜者",
        ],
    },
}

_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c6360000000020001e221bc330000000049454e44ae426082"
)


def _book_ids() -> list[int]:
    return sorted(BOOKS)


def _chapter_text(book_id: int, chapter_index: int, page: int = 1) -> str:
    book = BOOKS[book_id]
    title = book["chapters"][chapter_index]
    if page == 1:
        paragraphs = [
            f"{title}。这是第 {chapter_index + 1} 章的正文，来自内置示例站点。",
            "萧炎抬起头，望着远处的山峦，心中想着接下来的修行。",
            "风从山谷里吹来，带着青草的香气，也带来了新的消息。",
        ]
    else:
        paragraphs = [
            f"{title}（第 2 页）。正文分页测试：这一页只有一段内容。",
            "故事还在继续，下一页就是下一章。",
        ]
    return "\n".join(f"<p>{html.escape(item)}</p>" for item in paragraphs)


def _search_page(keyword: str, page: int) -> str:
    books = [book for book in BOOKS.values() if keyword in book["name"] or keyword in book["author"]]
    if not keyword:
        books = list(BOOKS.values())
    items = []
    for index, book in enumerate(books):
        if page > 1 and index < (page - 1) * 2:
            continue
        if index >= page * 2:
            continue
        book_id = next(key for key, value in BOOKS.items() if value is book)
        items.append(
            f"""<li class="book-item" data-id="{book_id}">
  <a class="book-link" href="/book/{book_id}.html">
    <img class="cover" src="{book['cover']}" alt="cover">
    <h3 class="book-name">{html.escape(book['name'])}</h3>
  </a>
  <span class="author">{html.escape(book['author'])}</span>
  <p class="intro">{html.escape(book['intro'])}</p>
  <span class="latest">{html.escape(book['chapters'][-1])}</span>
</li>"""
        )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>搜索 {html.escape(keyword)}</title></head>
<body>
<div id="search-page">
  <div class="book-list"><ul>{''.join(items)}</ul></div>
  <a id="next-page" href="/search?q={html.escape(keyword)}&amp;page={page + 1}">下一页</a>
</div>
</body></html>"""


def _book_page(book_id: int) -> str:
    book = BOOKS[book_id]
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{html.escape(book['name'])}</title></head>
<body>
<div id="book-info">
  <h1 class="book-name">{html.escape(book['name'])}</h1>
  <span class="author">{html.escape(book['author'])}</span>
  <img id="book-cover" src="{book['cover']}" alt="cover">
  <div id="book-intro">{html.escape(book['intro'])}</div>
  <div class="kind">玄幻</div>
  <a id="toc-link" href="/toc/{book_id}.html?page=1">目录</a>
</div>
</body></html>"""


def _toc_page(book_id: int, page: int) -> str:
    book = BOOKS[book_id]
    start = (page - 1) * 3
    end = min(start + 3, len(book["chapters"]))
    items = []
    for index in range(start, end):
        title = book["chapters"][index]
        items.append(
            f'<li class="chapter-item"><a href="/chapter/{book_id}/{index + 1}.html">'
            f"{html.escape(title)}</a></li>"
        )
    next_link = ""
    if end < len(book["chapters"]):
        next_link = f'<a id="next-page" href="/toc/{book_id}.html?page={page + 1}">下一页</a>'
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{html.escape(book['name'])} 目录</title></head>
<body>
<h1>{html.escape(book['name'])}</h1>
<div id="chapter-list"><ul>{''.join(items)}</ul></div>
{next_link}
</body></html>"""


def _chapter_page(book_id: int, chapter: int, page: int) -> str:
    book = BOOKS[book_id]
    if chapter < 1 or chapter > len(book["chapters"]):
        return ""
    title = book["chapters"][chapter - 1]
    next_page = ""
    if page == 1:
        next_page = (
            f'<a id="next-page" href="/chapter/{book_id}/{chapter}.html?page=2">下一页</a>'
        )
    next_chapter = ""
    if chapter < len(book["chapters"]):
        next_chapter = (
            f'<a id="next-chapter" href="/chapter/{book_id}/{chapter + 1}.html">下一章</a>'
        )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{html.escape(title)}</title></head>
<body>
<h1 id="chapter-title">{html.escape(title)}</h1>
<div id="chapter-content">{_chapter_text(book_id, chapter - 1, page)}</div>
{next_page}
{next_chapter}
</body></html>"""


def _json_search(keyword: str) -> str:
    books = [
        {
            "id": book_id,
            "name": book["name"],
            "author": book["author"],
            "intro": book["intro"],
            "cover": book["cover"],
            "lastChapter": book["chapters"][-1],
        }
        for book_id, book in BOOKS.items()
        if not keyword or keyword in book["name"] or keyword in book["author"]
    ]
    return json.dumps({"code": 0, "msg": "ok", "data": {"total": len(books), "books": books}}, ensure_ascii=False)


def _json_book(book_id: int) -> str:
    book = BOOKS.get(book_id)
    if not book:
        return json.dumps({"code": 404, "msg": "not found", "data": None})
    return json.dumps(
        {
            "code": 0,
            "msg": "ok",
            "data": {
                "id": book_id,
                "name": book["name"],
                "author": book["author"],
                "intro": book["intro"],
                "cover": book["cover"],
                "lastChapter": book["chapters"][-1],
                "chapters": [
                    {"title": title, "url": f"/chapter/{book_id}/{index + 1}.html"}
                    for index, title in enumerate(book["chapters"])
                ],
            },
        },
        ensure_ascii=False,
    )


class _Handler(BaseHTTPRequestHandler):
    server_version = "BookSourceDemo/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # 静默
        return

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path
        page = int((query.get("page") or ["1"])[0] or 1)
        keyword = (query.get("q") or [""])[0]

        if path == "/search":
            self._send_html(_search_page(keyword, page))
            return
        if path == "/regex-search":
            books = [book for book in BOOKS.values() if not keyword or keyword in book["name"]]
            items = "".join(
                f'<li><a href="/book/{index}.html">{html.escape(book["name"])}</a>'
                f'<em>{html.escape(book["author"])}</em></li>'
                for index, book in enumerate(books, start=1)
            )
            self._send_html(f'<html><body><ul class="regex-list">{items}</ul></body></html>')
            return
        if path == "/gbk/search":
            body = _search_page(keyword, page).replace('charset="utf-8"', 'charset="gbk"')
            self._send(body.encode("gbk", "replace"), "text/html; charset=gbk")
            return
        if path == "/gzip/search":
            body = _search_page(keyword, page).encode("utf-8")
            self._send(gzip.compress(body), "text/html; charset=utf-8", content_encoding="gzip")
            return
        if path == "/api/search":
            self._send_json(_json_search(keyword))
            return
        if path.startswith("/api/book/"):
            try:
                book_id = int(path.rsplit("/", 1)[-1])
            except ValueError:
                self._send_json('{"code":400}', status=400)
                return
            self._send_json(_json_book(book_id))
            return
        if path.startswith("/book/") and path.endswith(".html"):
            try:
                book_id = int(path.split("/")[2].split(".")[0])
            except (IndexError, ValueError):
                self._not_found()
                return
            if book_id not in BOOKS:
                self._not_found()
                return
            self._send_html(_book_page(book_id))
            return
        if path.startswith("/toc/") and path.endswith(".html"):
            try:
                book_id = int(path.split("/")[2].split(".")[0])
            except (IndexError, ValueError):
                self._not_found()
                return
            if book_id not in BOOKS:
                self._not_found()
                return
            self._send_html(_toc_page(book_id, page))
            return
        if path.startswith("/chapter/"):
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[2].endswith(".html"):
                try:
                    book_id = int(parts[1])
                    chapter = int(parts[2].split(".")[0])
                except ValueError:
                    self._not_found()
                    return
                body = _chapter_page(book_id, chapter, page)
                if body:
                    self._send_html(body)
                    return
            self._not_found()
            return
        if path.startswith("/cover/"):
            self._send(_PNG, "image/png")
            return
        if path == "/":
            self._send_html(
                "<html><body><h1>书源示例站点</h1><p>搜索、详情、目录、正文、JSON 接口均可用。</p></body></html>"
            )
            return
        self._not_found()

    # ------------------------------------------------------------------
    def _send_html(self, body: str) -> None:
        self._send(body.encode("utf-8"), "text/html; charset=utf-8")

    def _send_json(self, body: str, status: int = 200) -> None:
        self._send(body.encode("utf-8"), "application/json; charset=utf-8", status=status)

    def _send(
        self,
        data: bytes,
        content_type: str,
        *,
        status: int = 200,
        content_encoding: str = "",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if content_encoding:
            self.send_header("Content-Encoding", content_encoding)
        self.end_headers()
        self.wfile.write(data)

    def _not_found(self) -> None:
        self._send(b"not found", "text/plain; charset=utf-8", status=404)


class DemoSite:
    """线程化的示例站点。"""

    def __init__(self, port: int = 0) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        self.port = self.httpd.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._thread: threading.Thread | None = None

    def start(self) -> "DemoSite":
        if self._thread is None:
            self._thread = threading.Thread(
                target=self.httpd.serve_forever,
                name="book-source-demo",
                daemon=True,
            )
            self._thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def __enter__(self) -> "DemoSite":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()


if __name__ == "__main__":  # pragma: no cover - 手动演示
    site = DemoSite(int(__import__("os").environ.get("DEMO_PORT", "8765"))).start()
    print(f"示例站点：{site.base_url}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        site.stop()
