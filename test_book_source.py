# -*- coding: utf-8 -*-
"""书源引擎的端到端测试（全部走内置示例站点，不访问外网）。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from book_source.demo_site import DemoSite
from book_source.engine import BookSourceEngine
from book_source.source import BookSource, load_sources_file, parse_sources
from book_source.store import Bookshelf, ChapterCache, SourceStore
from book_source import dom


def _load(name: str, base: str) -> BookSource:
    source = load_sources_file(Path(__file__).resolve().parent / "书源示例" / name)[0]
    data = source.to_dict()
    data["bookSourceUrl"] = base
    return BookSource.from_dict(data)


class BookSourceEngineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.site = DemoSite(0).start()
        cls.engine = BookSourceEngine()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.site.stop()

    def test_html_search_detail_toc_content(self) -> None:
        source = _load("示例书源_HTML.json", self.site.base_url)
        books = self.engine.search(source, "斗破")
        self.assertEqual(len(books), 1)
        book = books[0]
        self.assertEqual(book.name, "斗破苍穹")
        self.assertEqual(book.author, "天蚕土豆")
        self.assertEqual(book.book_url, self.site.base_url + "/book/1.html")
        self.assertEqual(book.cover_url, self.site.base_url + "/cover/1.png")
        self.assertIn("简介", book.intro)
        self.assertEqual(book.last_chapter, "第6章 休养生息")

        info = self.engine.book_info(source, book)
        self.assertEqual(info.toc_url, self.site.base_url + "/toc/1.html?page=1")
        chapters = self.engine.toc(source, info)
        self.assertEqual(len(chapters), 6)
        self.assertEqual(chapters[0].title, "第1章 陨落的天才")
        self.assertTrue(chapters[0].url.endswith("/chapter/1/1.html"))

        content = self.engine.content(source, info, chapters[0], chapters[1].url)
        self.assertIn("第1章", content.content)
        self.assertIn("第 2 页", content.content)
        self.assertIn("风从山谷", content.content)

    def test_json_source_with_js(self) -> None:
        source = _load("示例书源_JSON.json", self.site.base_url)
        books = self.engine.search(source, "斗破")
        self.assertEqual(len(books), 1)
        self.assertEqual(books[0].book_url, self.site.base_url + "/book/1.html")
        self.assertEqual(books[0].word_count, "4000")

    def test_regex_source(self) -> None:
        source = _load("示例书源_正则.json", self.site.base_url)
        books = self.engine.search(source, "斗破")
        self.assertEqual(len(books), 1)
        self.assertEqual(books[0].name, "斗破苍穹")
        self.assertEqual(books[0].author, "天蚕土豆")
        self.assertEqual(books[0].book_url, self.site.base_url + "/book/1.html")

    def test_gbk_and_gzip(self) -> None:
        source = _load("示例书源_HTML.json", self.site.base_url)
        for path in ("/gbk/search?q={{key}}&page={{page}}", "/gzip/search?q={{key}}&page={{page}}"):
            data = source.to_dict()
            data["searchUrl"] = path
            candidate = BookSource.from_dict(data)
            books = self.engine.search(candidate, "斗破")
            self.assertEqual(len(books), 1, path)
            self.assertEqual(books[0].name, "斗破苍穹", path)

    def test_js_with_ajax(self) -> None:
        source = _load("示例书源_HTML.json", self.site.base_url)
        data = source.to_dict()
        data["ruleSearch"] = dict(data["ruleSearch"])
        data["ruleSearch"]["lastChapter"] = (
            "@js:var id = result.match(/data-id=\"(\\d+)\"/)[1];"
            "var obj = JSON.parse(java.ajax('/api/book/' + id));"
            "result = obj.data.lastChapter"
        )
        candidate = BookSource.from_dict(data)
        books = self.engine.search(candidate, "斗破")
        self.assertEqual(books[0].last_chapter, "第6章 休养生息")

    def test_rule_operators(self) -> None:
        source = _load("示例书源_HTML.json", self.site.base_url)
        data = source.to_dict()
        data["ruleSearch"] = dict(data["ruleSearch"])
        data["ruleSearch"]["name"] = "class.none@text||class.book-name@text"
        data["ruleSearch"]["author"] = "class.none@text&&class.author@text"
        data["ruleSearch"]["intro"] = "class.intro@text##简介##梗概"
        data["ruleSearch"]["kind"] = "@get:{bid}"
        data["ruleSearch"]["bookUrl"] = "class.book-link@href@put:{bid: data-id}"
        candidate = BookSource.from_dict(data)
        books = self.engine.search(candidate, "斗破")
        self.assertEqual(books[0].name, "斗破苍穹")
        self.assertEqual(books[0].author, "天蚕土豆")
        self.assertIn("梗概", books[0].intro)
        self.assertEqual(books[0].kind, "1")

    def test_reverse_list(self) -> None:
        source = _load("示例书源_HTML.json", self.site.base_url)
        data = source.to_dict()
        data["ruleSearch"] = dict(data["ruleSearch"])
        data["ruleSearch"]["bookList"] = "-class.book-item"
        candidate = BookSource.from_dict(data)
        books = self.engine.search(candidate, "")
        self.assertEqual([book.name for book in books], ["武动乾坤", "斗破苍穹"])

    def test_css_and_xpath_rules(self) -> None:
        source = _load("示例书源_HTML.json", self.site.base_url)
        data = source.to_dict()
        data["ruleSearch"] = dict(data["ruleSearch"])
        data["ruleSearch"]["name"] = "@CSS:.book-name@text"
        data["ruleSearch"]["bookUrl"] = "@XPath://a[@class='book-link']/@href"
        candidate = BookSource.from_dict(data)
        books = self.engine.search(candidate, "斗破")
        self.assertEqual(books[0].name, "斗破苍穹")
        self.assertTrue(books[0].book_url.endswith("/book/1.html"))

    def test_wrong_host_falls_back_to_source_host(self) -> None:
        source = _load("示例书源_HTML.json", self.site.base_url)
        data = source.to_dict()
        data["ruleSearch"] = dict(data["ruleSearch"])
        data["ruleSearch"]["bookUrl"] = "http://127.0.0.1:1/book/1.html"
        candidate = BookSource.from_dict(data)
        books = self.engine.search(candidate, "斗破")
        self.assertEqual(len(books), 1)
        info = self.engine.book_info(candidate, books[0])
        self.assertEqual(info.name, "斗破苍穹")
        self.assertTrue(info.book_url.startswith(self.site.base_url))

    def test_explore_js(self) -> None:
        source = BookSource.from_dict(
            {
                "bookSourceName": "explore",
                "bookSourceUrl": "https://explore.example",
                "exploreUrl": "@js:JSON.stringify([{title:'分类',url:'/s/{{page}}'}])",
            }
        )
        links = BookSourceEngine().explore_links(source)
        self.assertEqual(links, [("分类", "/s/{{page}}")])

    def test_source_validation(self) -> None:
        source = BookSource.from_dict({"bookSourceName": "x"})
        problems = source.validate()
        self.assertTrue(any("bookSourceUrl" in item for item in problems))
        self.assertTrue(any("searchUrl" in item for item in problems))

    def test_parse_sources_variants(self) -> None:
        payload = [{"bookSourceName": "A", "bookSourceUrl": "https://a.example"}]
        self.assertEqual(len(parse_sources(payload)), 1)
        self.assertEqual(len(parse_sources({"bookSources": payload})), 1)
        self.assertEqual(len(parse_sources('{"bookSourceName":"B","bookSourceUrl":"https://b.example"}')), 1)


class StoreTest(unittest.TestCase):
    def test_source_store_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = SourceStore(folder)
            source = BookSource.from_dict(
                {"bookSourceName": "A", "bookSourceUrl": "https://a.example"}
            )
            store.save([source])
            loaded = store.load()
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].name, "A")
            store.set_enabled(source.key, False)
            self.assertFalse(store.load()[0].enabled)

    def test_bookshelf_and_cache(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            shelf = Bookshelf(folder)
            from book_source.models import BookInfo, Chapter

            book = BookInfo(name="书", author="作者", book_url="https://x/book", source_url="https://x")
            chapters = [Chapter(title="第1章", url="https://x/1"), Chapter(title="第2章", url="https://x/2")]
            item = shelf.add(book, chapters, chapter_index=0)
            self.assertEqual(shelf.get(item.key).chapter.title, "第1章")
            shelf.update_progress(item.key, 1)
            self.assertEqual(shelf.items()[0].chapter_index, 1)
            shelf.remove(item.key)
            self.assertEqual(shelf.items(), [])

            cache = ChapterCache(folder)
            cache.put("b", "c", "标题", "正文")
            self.assertEqual(cache.get("b", "c")["content"], "正文")
            self.assertTrue(cache.has("b", "c"))
            self.assertGreaterEqual(cache.stats()["count"], 1)


class RuleUnitTest(unittest.TestCase):
    def setUp(self) -> None:
        from book_source import dom
        from book_source.rules import RuleContext, RuleEngine

        self.engine = RuleEngine()
        self.ctx = RuleContext(
            result=dom.parse_html(
                '<ul class="list"><li><a href="1">A</a></li>'
                '<li><a href="2">B</a></li><li><a href="3">C</a></li></ul>'
            ),
            variables={"key": "K"},
        )

    def test_index_and_interleave(self) -> None:
        self.assertEqual(self.engine.string("class.list@tag.li.0@tag.a@text", self.ctx), "A")
        self.assertEqual(self.engine.string("class.list@tag.li[-1]@tag.a@text", self.ctx), "C")
        values = self.engine.evaluate(
            "class.list@tag.li@tag.a@href%%class.list@tag.li@tag.a@text", self.ctx
        )
        self.assertEqual(values, ["1", "A", "2", "B", "3", "C"])

    def test_exclusion_index_and_bare_tag(self) -> None:
        from book_source import dom
        from book_source.rules import RuleContext

        ctx = RuleContext(
            result=dom.parse_html(
                '<div class="directoryArea"><p>header</p>'
                '<p><a href="/1">一</a></p><p><a href="/2">二</a></p></div>'
            )
        )
        self.assertEqual(
            self.engine.strings(".directoryArea p!0@a@text", ctx),
            ["一", "二"],
        )

    def test_templates_and_put(self) -> None:
        self.assertEqual(self.engine.string("{{key}}", self.ctx), "K")
        self.assertEqual(
            self.engine.string("@put:{x: class.list@tag.li.0@tag.a@text}@get:{x}", self.ctx),
            "A",
        )

    def test_js_lib_is_available(self) -> None:
        from book_source.source import BookSource

        self.ctx.source = BookSource.from_dict(
            {
                "bookSourceName": "x",
                "bookSourceUrl": "https://x.example",
                "jsLib": "function wrap(s){ return '[' + s + ']' }",
            }
        )
        self.assertEqual(self.engine.string("@js:wrap('a')", self.ctx), "[a]")

    def test_source_cache_and_cookie_js(self) -> None:
        from book_source.http import HttpClient
        from book_source.rules import RuleContext
        from book_source.source import BookSource

        source = BookSource.from_dict(
            {"bookSourceName": "compat", "bookSourceUrl": "https://compat.example"}
        )
        ctx = RuleContext(
            result="",
            base_url="https://compat.example/",
            source=source,
            http=HttpClient(),
        )
        self.assertEqual(
            self.engine.string(
                "@js:source.put('k','v'); source.get('k') + '|' + cache.get('k')", ctx
            ),
            "v|v",
        )
        self.engine.eval_expression("cache.put('c','1')", ctx)
        self.assertEqual(self.engine.string("@js:cache.get('c')", ctx), "1")
        self.engine.eval_expression("source.putVariable('{\"host\":\"x\"}')", ctx)
        self.assertEqual(
            self.engine.string("@js:JSON.parse(source.getVariable()).host", ctx), "x"
        )
        self.engine.eval_expression("cookie.setCookie(baseUrl, 'a=1')", ctx)
        self.assertEqual(self.engine.string("@js:cookie.getCookie(baseUrl)", ctx), "a=1")
        self.engine.eval_expression("cookie.removeCookie(baseUrl)", ctx)
        self.assertEqual(self.engine.string("@js:cookie.getCookie(baseUrl)", ctx), "")

    def test_replace_and_css_xpath(self) -> None:
        self.assertEqual(
            self.engine.string("class.list@tag.li@tag.a@text##A##甲", self.ctx), "甲\nB\nC"
        )
        self.assertEqual(self.engine.string("@CSS:.list li a@text", self.ctx), "A\nB\nC")
        self.assertEqual(self.engine.string("@XPath://a[@href='2']/text()", self.ctx), "B")


class DomTest(unittest.TestCase):
    def test_parse_and_select(self) -> None:
        doc = dom.parse_html('<div class="a"><p id="x">hi</p><p>2</p></div>')
        self.assertEqual(len(dom.select_css(doc, "div.a p")), 2)
        self.assertEqual(dom.select_xpath(doc, "//p/@id"), ["x"])
        self.assertEqual(dom.text_of(doc.cssselect("p")[0]), "hi")

    def test_json_path(self) -> None:
        data = {"a": {"list": [{"name": "x"}, {"name": "y"}]}}
        self.assertEqual(dom.json_path(data, "$.a.list[*].name"), ["x", "y"])
        self.assertEqual(dom.json_path(data, "$..name"), ["x", "y"])


if __name__ == "__main__":
    unittest.main()
