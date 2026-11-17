# -*- coding: utf-8 -*-
"""「书源阅读」页面（Tkinter）。

页面只负责：搜索、书架、目录、阅读、缓存、书源管理。
抓取与解析全部交给 ``BookSourceEngine``，书源本身是独立 JSON 配置。
"""

from __future__ import annotations

import json
import queue
import re
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Any, Callable
from urllib.parse import urlsplit

from .demo_site import DemoSite
from .engine import BookSourceEngine
from .js import javascript_available
from .models import BookInfo, BookshelfItem, Chapter, SearchBook
from .source import BookSource, SourceError, load_sources_file, parse_sources
from .store import Bookshelf, ChapterCache, ReaderSettings, SourceStore

BG = "#f3f6fb"
CARD = "#ffffff"
BORDER = "#dbe3ef"
TEXT = "#162033"
MUTED = "#64748b"
ACCENT = "#2563eb"
ACCENT_DARK = "#1d4ed8"
PAPER = "#fbfaf6"
PAPER_TEXT = "#1f2937"

APP_DIR = Path(__file__).resolve().parent.parent


class BookSourcePage(tk.Frame):
    """书源阅读主页面。"""

    def __init__(self, master: tk.Widget, app: Any) -> None:
        super().__init__(master, bg=BG)
        self.app = app
        self.root = app.root
        self.family = getattr(app, "font_family", "Microsoft YaHei UI")

        self.source_store = SourceStore()
        self.shelf = Bookshelf()
        self.cache = ChapterCache()
        self.settings = ReaderSettings()
        self.engine = BookSourceEngine(logger=self._log)
        from .tasks import TaskQueue

        self.tasks = TaskQueue(max_workers=4, name="book-source")

        self.sources: list[BookSource] = []
        self.results: list[SearchBook] = []
        self.result_map: dict[str, SearchBook] = {}
        self.shelf_map: dict[str, str] = {}
        self.chapters: list[Chapter] = []
        self.current_book: BookInfo | None = None
        self.current_source: BookSource | None = None
        self.current_index = -1
        self.current_item: BookshelfItem | None = None
        self.current_content: str = ""
        self._callbacks: dict[str, tuple[Callable[[Any], None] | None, Callable[[str], None] | None]] = {}
        self._ui_queue: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self._search_pending = 0
        self._search_total = 0
        self._closed = False
        self._cache_cancel: threading.Event | None = None
        self._demo_site: DemoSite | None = None
        self._manager: tk.Toplevel | None = None
        self._editor: tk.Toplevel | None = None

        self._build_ui()
        self._reload_sources()
        self._refresh_shelf()
        self._apply_font()
        if not javascript_available():
            self._status("未检测到 pythonmonkey：不带 @js 的书源可用，JS 书源需要安装依赖。")
        self.root.after(120, self._poll)

    # ------------------------------------------------------------------
    # 外观
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        bar = tk.Frame(self, bg=BG)
        bar.grid(row=0, column=0, sticky="ew", padx=8, pady=(6, 4))
        bar.columnconfigure(1, weight=1)

        tk.Label(bar, text="搜索", bg=BG, fg=MUTED, font=(self.family, 10)).grid(
            row=0, column=0, padx=(0, 6)
        )
        self.search_var = tk.StringVar()
        entry = tk.Entry(
            bar,
            textvariable=self.search_var,
            font=(self.family, 11),
            relief="solid",
            bd=1,
            bg=CARD,
            fg=TEXT,
            insertbackground=TEXT,
            width=24,
        )
        entry.grid(row=0, column=1, sticky="ew", ipady=5)
        entry.bind("<Return>", lambda _event: self._search())
        self.search_entry = entry

        self.source_var = tk.StringVar(value="全部书源")
        self.source_combo = ttk.Combobox(
            bar, textvariable=self.source_var, width=18, state="readonly",
            font=(self.family, 9),
        )
        self.source_combo.grid(row=0, column=2, padx=(8, 0))
        self.source_combo.bind("<<ComboboxSelected>>", lambda _event: None)

        self.search_button = ttk.Button(bar, text="搜索", style="Accent.TButton", command=self._search)
        self.search_button.grid(row=0, column=3, padx=(8, 0))
        ttk.Button(bar, text="书源管理", command=self._open_manager).grid(row=0, column=4, padx=(6, 0))
        self.demo_button = ttk.Button(bar, text="示例站点", command=self._toggle_demo)
        self.demo_button.grid(row=0, column=5, padx=(6, 0))
        ttk.Button(bar, text="发现", command=self._open_explore).grid(row=0, column=6, padx=(6, 0))

        self.body = tk.Frame(self, bg=BG)
        self.body.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 4))
        self.body.rowconfigure(0, weight=1)
        self.body.columnconfigure(0, minsize=300, weight=0)
        self.body.columnconfigure(1, minsize=240, weight=0)
        self.body.columnconfigure(2, weight=1)

        self._build_left()
        self._build_middle()
        self._build_reader()

        self.status_var = tk.StringVar(value="就绪")

    def _build_left(self) -> None:
        left = tk.Frame(self.body, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        left.rowconfigure(1, weight=1)
        left.columnconfigure(0, weight=1)

        header = tk.Frame(left, bg=CARD)
        header.grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 2))
        tk.Label(header, text="我的阅读", bg=CARD, fg=TEXT, font=(self.family, 11, "bold")).pack(side="left")
        ttk.Button(header, text="移出书架", command=self._remove_from_shelf).pack(side="right")

        self.left_tabs = ttk.Notebook(left)
        self.left_tabs.grid(row=1, column=0, sticky="nsew", padx=6, pady=(2, 6))

        shelf_tab = tk.Frame(self.left_tabs, bg=CARD)
        results_tab = tk.Frame(self.left_tabs, bg=CARD)
        self.left_tabs.add(shelf_tab, text="书架")
        self.left_tabs.add(results_tab, text="搜索结果")

        self.shelf_tree = self._make_tree(
            shelf_tab,
            columns=(("name", "书名", 120), ("author", "作者", 70), ("progress", "进度", 50)),
            height=14,
        )
        self.shelf_tree.bind("<Double-1>", self._open_shelf_item)

        self.results_tree = self._make_tree(
            results_tab,
            columns=(("name", "书名", 120), ("author", "作者", 70), ("source", "来源", 110)),
            height=14,
        )
        self.results_tree.bind("<Double-1>", self._open_result)

    def _build_middle(self) -> None:
        middle = tk.Frame(self.body, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        middle.grid(row=0, column=1, sticky="nsew", padx=(0, 6))
        middle.rowconfigure(4, weight=1)
        middle.columnconfigure(0, weight=1)

        tk.Label(middle, text="书籍详情", bg=CARD, fg=MUTED, font=(self.family, 9)).grid(
            row=0, column=0, sticky="w", padx=12, pady=(8, 0)
        )
        self.book_title_var = tk.StringVar(value="尚未选择书籍")
        self.book_author_var = tk.StringVar(value="从搜索结果或书架中打开一本书")
        tk.Label(
            middle, textvariable=self.book_title_var, bg=CARD, fg=TEXT,
            font=(self.family, 13, "bold"), anchor="w", wraplength=260,
        ).grid(row=1, column=0, sticky="ew", padx=12, pady=(2, 0))
        tk.Label(
            middle, textvariable=self.book_author_var, bg=CARD, fg=MUTED,
            font=(self.family, 9), anchor="w", wraplength=260,
        ).grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 4))

        tk.Label(middle, text="目录", bg=CARD, fg=MUTED, font=(self.family, 9)).grid(
            row=3, column=0, sticky="nw", padx=12, pady=(4, 0)
        )
        toc_frame = tk.Frame(middle, bg=CARD)
        toc_frame.grid(row=4, column=0, sticky="nsew", padx=6, pady=(0, 6))
        toc_frame.rowconfigure(0, weight=1)
        toc_frame.columnconfigure(0, weight=1)
        self.toc_tree = ttk.Treeview(toc_frame, columns=("no", "title"), show="headings", selectmode="browse")
        self.toc_tree.heading("no", text="#")
        self.toc_tree.heading("title", text="章节")
        self.toc_tree.column("no", width=42, anchor="center", stretch=False)
        self.toc_tree.column("title", width=160, anchor="w")
        self.toc_tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(toc_frame, orient="vertical", command=self.toc_tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.toc_tree.configure(yscrollcommand=scroll.set)
        self.toc_tree.bind("<Double-1>", self._open_toc_item)
        middle.rowconfigure(4, weight=1)

    def _build_reader(self) -> None:
        right = tk.Frame(self.body, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        right.grid(row=0, column=2, sticky="nsew")
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)

        toolbar = tk.Frame(right, bg=CARD)
        toolbar.grid(row=0, column=0, sticky="ew", padx=10, pady=(6, 4))
        self.prev_button = ttk.Button(toolbar, text="‹ 上章", width=6, command=self._prev_chapter)
        self.prev_button.pack(side="left")
        self.next_button = ttk.Button(toolbar, text="下章 ›", width=6, command=self._next_chapter)
        self.next_button.pack(side="left", padx=(4, 0))
        self.add_button = ttk.Button(toolbar, text="加书架", width=6, command=self._add_to_shelf)
        self.add_button.pack(side="left", padx=(12, 0))
        self.cache_button = ttk.Button(toolbar, text="缓存", width=5, command=self._toggle_cache_all)
        self.cache_button.pack(side="left", padx=(4, 0))
        ttk.Button(toolbar, text="A-", width=3, command=lambda: self._change_font(-1)).pack(side="right")
        ttk.Button(toolbar, text="A+", width=3, command=lambda: self._change_font(1)).pack(side="right", padx=(0, 4))

        text_frame = tk.Frame(right, bg=PAPER)
        text_frame.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 6))
        text_frame.rowconfigure(0, weight=1)
        text_frame.columnconfigure(0, weight=1)
        self.reader = tk.Text(
            text_frame,
            wrap="word",
            width=30,
            bg=PAPER,
            fg=PAPER_TEXT,
            relief="flat",
            padx=26,
            pady=18,
            insertwidth=0,
            cursor="arrow",
            spacing1=3,
            spacing3=3,
            state="disabled",
        )
        self.reader.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(text_frame, orient="vertical", command=self.reader.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.reader.configure(yscrollcommand=scroll.set)
        self.reader.tag_configure("title", font=(self.family, 20, "bold"), spacing3=14)
        self.reader.tag_configure("body", font=(self.family, 16), spacing1=5, spacing3=5)
        self.reader.tag_configure("muted", font=(self.family, 12), foreground=MUTED)

        self.reader_status = tk.StringVar(value="未打开章节")
        tk.Label(
            right, textvariable=self.reader_status, bg=CARD, fg=MUTED,
            font=(self.family, 9), anchor="w",
        ).grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 6))

    def _make_tree(
        self,
        parent: tk.Widget,
        columns: tuple[tuple[str, str, int], ...],
        height: int = 12,
    ) -> ttk.Treeview:
        frame = tk.Frame(parent, bg=CARD)
        frame.pack(fill="both", expand=True, padx=2, pady=2)
        ids = [item[0] for item in columns]
        tree = ttk.Treeview(frame, columns=ids, show="headings", selectmode="browse", height=height)
        for key, title, width in columns:
            tree.heading(key, text=title)
            tree.column(key, width=width, anchor="w")
        tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        scroll.pack(side="right", fill="y")
        tree.configure(yscrollcommand=scroll.set)
        return tree

    # ------------------------------------------------------------------
    # 数据
    # ------------------------------------------------------------------
    def _reload_sources(self) -> None:
        all_sources = self.source_store.load()
        self.sources = [source for source in all_sources if source.enabled]
        names = ["全部书源"] + [source.name for source in self.sources]
        self.source_combo.configure(values=names)
        if self.source_var.get() not in names:
            self.source_var.set("全部书源")
        if not self.sources:
            self._status("还没有书源，点击「书源管理」导入，或点击「示例站点」开始体验。")

    def _selected_sources(self) -> list[BookSource]:
        label = self.source_var.get()
        if label == "全部书源":
            return list(self.sources)
        return [source for source in self.sources if source.name == label]

    def _source_for(self, source_url: str) -> BookSource | None:
        source = next((item for item in self.sources if item.url == source_url), None)
        if source is not None:
            return source
        return next((item for item in self.source_store.load() if item.url == source_url), None)

    def _refresh_shelf(self) -> None:
        for iid in self.shelf_tree.get_children():
            self.shelf_tree.delete(iid)
        self.shelf_map.clear()
        for item in self.shelf.items():
            iid = f"shelf:{item.key}"
            self.shelf_map[iid] = item.key
            self.shelf_tree.insert(
                "", "end", iid=iid,
                values=(item.book.name, item.book.author, item.progress_text),
            )

    # ------------------------------------------------------------------
    # 搜索
    # ------------------------------------------------------------------
    def _search(self) -> None:
        keyword = self.search_var.get().strip()
        if not keyword:
            self._status("请输入搜索关键词")
            self.search_entry.focus_set()
            return
        selected = self._selected_sources()
        if not selected:
            messagebox.showinfo("没有书源", "请先在「书源管理」中导入并启用书源。", parent=self)
            return
        for iid in self.results_tree.get_children():
            self.results_tree.delete(iid)
        self.results.clear()
        self.result_map.clear()
        self._search_pending = len(selected)
        self._search_total = len(selected)
        self.search_button.state(["disabled"])
        self.left_tabs.select(1)
        self._status(f"正在 {len(selected)} 个书源中搜索「{keyword}」…")
        for source in selected:
            self._submit(
                f"搜索 {source.name}",
                (lambda s=source: self.engine.search(s, keyword)),
                (lambda books, s=source: self._search_done(s, books)),
                (lambda error, s=source: self._search_failed(s, error)),
            )

    def _search_done(self, source: BookSource, books: list[SearchBook]) -> None:
        for book in books or []:
            key = f"{book.source_url}\n{book.book_url}"
            if key in self.result_map:
                continue
            self.result_map[key] = book
            self.results.append(book)
            self.results_tree.insert(
                "", "end", iid=f"r{len(self.results)}",
                values=(book.name, book.author, book.source_name),
            )
        self._search_step_done(source.name, len(books or []))

    def _search_failed(self, source: BookSource, error: str) -> None:
        self._search_step_done(source.name, 0, error)

    def _search_step_done(self, name: str, count: int, error: str = "") -> None:
        self._search_pending = max(0, self._search_pending - 1)
        if error:
            self._status(f"{name} 搜索失败：{error}")
        else:
            self._status(f"{name} 返回 {count} 本，累计 {len(self.results)} 本")
        if self._search_pending == 0:
            self.search_button.state(["!disabled"])
            self._status(f"搜索完成：{len(self.results)} 本（{self._search_total} 个书源）")

    def _open_explore(self) -> None:
        sources = self._selected_sources()
        if not sources:
            messagebox.showinfo("没有书源", "请先导入并启用书源。", parent=self)
            return
        entries: list[tuple[str, BookSource, str]] = []
        for source in sources:
            try:
                for title, url in self.engine.explore_links(source):
                    entries.append((f"{source.name} · {title}", source, url))
            except Exception as exc:
                self._status(f"{source.name} 发现规则失败：{exc}")
        if not entries:
            messagebox.showinfo(
                "没有发现规则",
                "当前书源没有可用的 exploreUrl，或规则执行失败。",
                parent=self,
            )
            return

        window = tk.Toplevel(self)
        window.title("发现")
        window.geometry("420x560")
        window.configure(bg=CARD)
        window.transient(self.root)
        tk.Label(
            window, text="选择分类或榜单", bg=CARD, fg=TEXT,
            font=(self.family, 11, "bold"),
        ).pack(fill="x", padx=12, pady=(10, 4))
        frame = tk.Frame(window, bg=CARD)
        frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        listbox = tk.Listbox(
            frame, font=(self.family, 10), bg="#ffffff", fg=TEXT,
            relief="flat", highlightthickness=0, activestyle="none",
        )
        listbox.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=listbox.yview)
        scroll.pack(side="right", fill="y")
        listbox.configure(yscrollcommand=scroll.set)
        for label, _source, _url in entries:
            listbox.insert("end", label)

        def choose(_event: tk.Event | None = None) -> None:
            selection = listbox.curselection()
            if not selection:
                return
            _label, source, url = entries[selection[0]]
            window.destroy()
            self._run_explore(source, url)

        listbox.bind("<Double-1>", choose)
        ttk.Button(window, text="打开选中分类", command=choose).pack(pady=(0, 10))

    def _run_explore(self, source: BookSource, url: str) -> None:
        for iid in self.results_tree.get_children():
            self.results_tree.delete(iid)
        self.results.clear()
        self.result_map.clear()
        self._search_pending = 1
        self._search_total = 1
        self.search_button.state(["disabled"])
        self.left_tabs.select(1)
        self._status(f"正在从「{source.name}」加载发现列表…")
        self._submit(
            f"发现 {source.name}",
            lambda: self.engine.explore(source, url),
            lambda books: self._search_done(source, books),
            lambda error: self._search_failed(source, error),
        )

    # ------------------------------------------------------------------
    # 打开书籍
    # ------------------------------------------------------------------
    def _open_result(self, _event: tk.Event) -> None:
        selection = self.results_tree.selection()
        if not selection:
            return
        index = int(selection[0][1:]) - 1
        if 0 <= index < len(self.results):
            self._open_book(self.results[index])

    def _open_shelf_item(self, _event: tk.Event) -> None:
        selection = self.shelf_tree.selection()
        if not selection:
            return
        key = self.shelf_map.get(selection[0])
        item = self.shelf.get(key) if key else None
        if item is None:
            return
        source = self._source_for(item.book.source_url)
        if source is None:
            messagebox.showwarning("书源不存在", "这本书所属的书源已被删除，请重新导入。", parent=self)
            return
        self._book_loaded(source, item.book, item.chapters)

    def _open_book(self, book: SearchBook) -> None:
        source = self._source_for(book.source_url)
        if source is None:
            messagebox.showwarning("书源不存在", "这本书所属的书源已被删除，请重新导入。", parent=self)
            return
        self.current_book = BookInfo.from_dict(book.to_dict())
        self.current_source = source
        self.chapters = []
        self.current_index = -1
        self.current_item = self.shelf.get(self.current_book.key)
        self.book_title_var.set(book.name)
        self.book_author_var.set(f"{book.author or '佚名'} · {source.name}")
        self._clear_toc()
        self._show_message("正在获取书籍详情与目录…")
        self._status(f"正在打开《{book.name}》…")

        def work() -> tuple[BookInfo, list[Chapter]]:
            info = self.engine.book_info(source, BookInfo.from_dict(book.to_dict()))
            chapters = self.engine.toc(source, info)
            return info, chapters

        self._submit(
            f"详情 {book.name}",
            work,
            lambda value: self._book_loaded(source, *value),
            lambda error: self._book_failed(book.name, error),
        )

    def _book_loaded(self, source: BookSource, info: BookInfo, chapters: list[Chapter]) -> None:
        self.current_book = info
        self.current_source = source
        self.chapters = chapters
        self.current_item = self.shelf.get(info.key)
        self.book_title_var.set(info.name or "未知书名")
        detail = info.author or "佚名"
        if info.kind:
            detail += f" · {info.kind}"
        self.book_author_var.set(f"{detail} · {source.name}")
        self._fill_toc(chapters)
        if self.current_item is not None:
            index = self.current_item.chapter_index if self.current_item.chapter_index >= 0 else 0
            if chapters:
                self._load_chapter(index)
        hint = self._toc_gap_hint(chapters)
        self._status(f"《{info.name}》共 {len(chapters)} 章" + (f"；{hint}" if hint else ""))

    def _book_failed(self, name: str, error: str) -> None:
        self._status(f"打开《{name}》失败：{error}")
        hint = self._network_hint(error)
        self._show_message(f"加载失败\n\n{error}" + (f"\n\n{hint}" if hint else ""))

    @staticmethod
    def _toc_gap_hint(chapters: list[Chapter]) -> str:
        numbers: list[int] = []
        for chapter in chapters:
            match = re.search(r"第\s*(\d+)\s*[章回节话集]", chapter.title)
            if match:
                numbers.append(int(match.group(1)))
        if len(numbers) < 3:
            return ""
        missing = sorted(set(range(min(numbers), max(numbers) + 1)) - set(numbers))
        if not missing:
            return ""
        preview = "、".join(str(item) for item in missing[:6])
        if len(missing) > 6:
            preview += " 等"
        return f"注意：源站目录缺少第 {preview} 章（不是程序漏抓）"

    def _network_hint(self, error: str) -> str:
        text = str(error)
        if not any(
            key in text
            for key in ("10061", "10054", "拒绝", "重置", "Connection refused", "Reset")
        ):
            return ""
        lines = [
            "提示：书源里的域名可能写错，或站点已换域名/被反爬拦截。",
            "可以先更新书源，或在书源里把旧域名改成站点当前域名。",
        ]
        source_host = self.current_source.host if self.current_source is not None else ""
        match = re.search(r"https?://[^\s）)]+", text)
        request_host = ""
        if match:
            request_host = urlsplit(match.group(0)).netloc
        if source_host and request_host and source_host.lower() != request_host.lower():
            lines.append(f"书源域名：{source_host}")
            lines.append(f"请求域名：{request_host}")
            lines.append("两者不一致，规则里可能写死了旧域名。")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 目录
    # ------------------------------------------------------------------
    def _clear_toc(self) -> None:
        for iid in self.toc_tree.get_children():
            self.toc_tree.delete(iid)

    def _fill_toc(self, chapters: list[Chapter]) -> None:
        self._clear_toc()
        for index, chapter in enumerate(chapters):
            self.toc_tree.insert(
                "", "end", iid=str(index),
                values=(index + 1, chapter.title),
            )
        self.toc_tree.yview_moveto(0)

    def _open_toc_item(self, _event: tk.Event) -> None:
        selection = self.toc_tree.selection()
        if selection:
            self._load_chapter(int(selection[0]))

    def _select_toc(self, index: int) -> None:
        iid = str(index)
        if self.toc_tree.exists(iid):
            self.toc_tree.selection_set(iid)
            self.toc_tree.focus(iid)
            self.toc_tree.see(iid)

    # ------------------------------------------------------------------
    # 正文
    # ------------------------------------------------------------------
    def _load_chapter(self, index: int) -> None:
        if not self.chapters or self.current_book is None or self.current_source is None:
            return
        if not (0 <= index < len(self.chapters)):
            return
        self.current_index = index
        self._select_toc(index)
        chapter = self.chapters[index]
        self.reader_status.set(f"第 {index + 1}/{len(self.chapters)} 章 · {chapter.title}")
        self.prev_button.state(["!disabled"] if index > 0 else ["disabled"])
        self.next_button.state(["!disabled"] if index + 1 < len(self.chapters) else ["disabled"])
        self._save_progress(index)

        cached = self.cache.get(self.current_book.key, chapter.key)
        if cached:
            self.current_content = str(cached.get("content", ""))
            self._display(chapter.title, self.current_content, cached=True)
            return

        self._show_message("正在加载正文…")
        source = self.current_source
        book = self.current_book
        next_url = self.chapters[index + 1].url if index + 1 < len(self.chapters) else ""

        def work() -> str:
            return self.engine.content(source, book, chapter, next_url).content

        self._submit(
            f"正文 {chapter.title}",
            work,
            lambda content: self._chapter_loaded(chapter, content),
            lambda error: self._chapter_failed(chapter, error),
        )

    def _chapter_loaded(self, chapter: Chapter, content: str) -> None:
        if self.current_book is None:
            return
        self.cache.put(self.current_book.key, chapter.key, chapter.title, content)
        self.current_content = content
        self._display(chapter.title, content, cached=False)
        self._save_progress(self.current_index)

    def _chapter_failed(self, chapter: Chapter, error: str) -> None:
        self._status(f"《{chapter.title}》加载失败：{error}")
        hint = self._network_hint(error)
        self._show_message(f"章节加载失败\n\n{error}" + (f"\n\n{hint}" if hint else ""))

    def _display(self, title: str, content: str, *, cached: bool) -> None:
        self.reader.configure(state="normal")
        self.reader.delete("1.0", "end")
        self.reader.insert("end", title + "\n", "title")
        body = (content or "").strip()
        body = re.sub(r"\n{3,}", "\n\n", body)
        self.reader.insert("end", body + "\n", "body")
        self.reader.configure(state="disabled")
        self.reader.yview_moveto(0)
        suffix = " · 已缓存" if cached else ""
        if self.chapters and self.current_index >= 0:
            self.reader_status.set(
                f"第 {self.current_index + 1}/{len(self.chapters)} 章 · {title}{suffix}"
            )

    def _show_message(self, message: str) -> None:
        self.reader.configure(state="normal")
        self.reader.delete("1.0", "end")
        self.reader.insert("end", message + "\n", "muted")
        self.reader.configure(state="disabled")

    def _prev_chapter(self) -> None:
        if self.current_index > 0:
            self._load_chapter(self.current_index - 1)

    def _next_chapter(self) -> None:
        if self.current_index + 1 < len(self.chapters):
            self._load_chapter(self.current_index + 1)

    def _save_progress(self, index: int) -> None:
        if self.current_book is None:
            return
        if self.current_item is None:
            return
        self.current_item.chapter_index = index
        self.shelf.update_progress(self.current_book.key, index)
        self._refresh_shelf()

    # ------------------------------------------------------------------
    # 书架
    # ------------------------------------------------------------------
    def _add_to_shelf(self) -> None:
        if self.current_book is None:
            self._status("请先打开一本书")
            return
        if not self.chapters:
            self._status("目录还没加载完成")
            return
        existing = self.shelf.get(self.current_book.key)
        if existing is not None:
            self._status("这本书已在书架中")
            return
        self.current_item = self.shelf.add(
            self.current_book, self.chapters, chapter_index=max(0, self.current_index)
        )
        self._refresh_shelf()
        self._status(f"已加入书架：《{self.current_book.name}》")

    def _remove_from_shelf(self) -> None:
        selection = self.shelf_tree.selection()
        if not selection:
            self._status("请在书架中选择一本书")
            return
        key = self.shelf_map.get(selection[0])
        if not key:
            return
        item = self.shelf.get(key)
        if item is None:
            return
        if not messagebox.askyesno(
            "移出书架", f"确定把《{item.book.name}》移出书架？\n（章节缓存不会删除）", parent=self
        ):
            return
        self.shelf.remove(key)
        if self.current_book is not None and self.current_book.key == key:
            self.current_item = None
        self._refresh_shelf()
        self._status("已移出书架")

    # ------------------------------------------------------------------
    # 缓存
    # ------------------------------------------------------------------
    def _toggle_cache_all(self) -> None:
        if self._cache_cancel is not None:
            self._cache_cancel.set()
            self._status("正在停止缓存…")
            return
        if not self.chapters or self.current_book is None or self.current_source is None:
            self._status("请先打开一本书并加载目录")
            return
        book = self.current_book
        source = self.current_source
        chapters = list(self.chapters)
        cancel = threading.Event()
        self._cache_cancel = cancel
        self.cache_button.configure(text="停止缓存")

        def work() -> tuple[int, int]:
            ok = 0
            failed = 0
            total = len(chapters)
            for index, chapter in enumerate(chapters):
                if cancel.is_set():
                    break
                if self.cache.has(book.key, chapter.key):
                    ok += 1
                else:
                    try:
                        next_url = chapters[index + 1].url if index + 1 < len(chapters) else ""
                        content = self.engine.content(source, book, chapter, next_url).content
                        self.cache.put(book.key, chapter.key, chapter.title, content)
                        ok += 1
                    except Exception as exc:
                        failed += 1
                        self._ui_queue.put(("info", f"缓存失败：{chapter.title}（{exc}）"))
                self._ui_queue.put(("cache_progress", (index + 1, total)))
            return ok, failed

        self._submit("缓存全本", work, self._cache_done, self._cache_failed)

    def _cache_done(self, result: tuple[int, int]) -> None:
        ok, failed = result
        self._cache_cancel = None
        self.cache_button.configure(text="缓存")
        self._status(f"缓存结束：成功 {ok} 章，失败 {failed} 章")

    def _cache_failed(self, error: str) -> None:
        self._cache_cancel = None
        self.cache_button.configure(text="缓存")
        self._status(f"缓存失败：{error}")

    # ------------------------------------------------------------------
    # 字号
    # ------------------------------------------------------------------
    def _apply_font(self) -> None:
        size = int(self.settings.get("font_size", 16) or 16)
        size = max(12, min(30, size))
        self.reader.tag_configure("body", font=(self.family, size), spacing1=5, spacing3=5)
        self.reader.tag_configure("title", font=(self.family, min(28, size + 4), "bold"), spacing3=14)

    def _change_font(self, delta: int) -> None:
        size = int(self.settings.get("font_size", 16) or 16) + delta
        size = max(12, min(30, size))
        self.settings.set("font_size", size)
        self._apply_font()

    # ------------------------------------------------------------------
    # 任务
    # ------------------------------------------------------------------
    def _submit(
        self,
        name: str,
        fn: Callable[[], Any],
        on_done: Callable[[Any], None] | None,
        on_error: Callable[[str], None] | None,
    ) -> str:
        task_id = self.tasks.submit(name, fn)
        self._callbacks[task_id] = (on_done, on_error)
        return task_id

    def _poll(self) -> None:
        if self._closed:
            return
        for event, task in self.tasks.poll():
            if event == "started":
                continue
            on_done, on_error = self._callbacks.pop(task.task_id, (None, None))
            if event == "done":
                if on_done:
                    try:
                        on_done(task.result)
                    except Exception as exc:  # 防止回调异常打断轮询
                        self._status(f"界面更新失败：{exc}")
            elif event == "error":
                if on_error:
                    try:
                        on_error(task.error)
                    except Exception:
                        pass
                else:
                    self._status(f"{task.name} 失败：{task.error}")
        while True:
            try:
                kind, payload = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "cache_progress":
                done, total = payload
                self._status(f"正在缓存：{done}/{total}")
            elif kind == "info":
                self._status(str(payload))
        try:
            self.root.after(120, self._poll)
        except tk.TclError:
            pass

    def _status(self, message: str) -> None:
        self.status_var.set(message)
        try:
            self.app.footer_var.set(message)
        except Exception:
            pass

    def _log(self, message: str) -> None:
        # 抓取日志不刷屏，只在出错时由调用方展示
        pass

    # ------------------------------------------------------------------
    # 书源管理
    # ------------------------------------------------------------------
    def _open_manager(self) -> None:
        if self._manager is not None and self._manager.winfo_exists():
            self._manager.lift()
            return
        window = tk.Toplevel(self)
        self._manager = window
        window.title("书源管理")
        window.geometry("1080x600")
        window.configure(bg=BG)
        window.transient(self.root)

        top = tk.Frame(window, bg=BG)
        top.pack(fill="x", padx=10, pady=8)
        row1 = tk.Frame(top, bg=BG)
        row1.pack(fill="x", pady=(0, 4))
        row2 = tk.Frame(top, bg=BG)
        row2.pack(fill="x")
        ttk.Button(row1, text="导入文件", command=self._import_file).pack(side="left")
        ttk.Button(row1, text="从链接导入", command=self._import_url).pack(side="left", padx=(6, 0))
        ttk.Button(row1, text="从剪贴板导入", command=self._import_clipboard).pack(side="left", padx=(6, 0))
        ttk.Button(row1, text="导出全部", command=self._export_sources).pack(side="left", padx=(6, 0))
        ttk.Button(row2, text="启用/停用", command=self._toggle_source).pack(side="left")
        ttk.Button(row2, text="编辑", command=self._edit_source).pack(side="left", padx=(6, 0))
        ttk.Button(row2, text="测试", command=self._test_source).pack(side="left", padx=(6, 0))
        ttk.Button(row2, text="删除", command=self._delete_source).pack(side="left", padx=(6, 0))

        frame = tk.Frame(window, bg=CARD)
        frame.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        self.mgr_tree = ttk.Treeview(
            frame, columns=("name", "group", "status", "search", "host"),
            show="headings", selectmode="browse",
        )
        for key, title, width in (
            ("name", "名称", 190), ("group", "分组", 100), ("status", "状态", 60),
            ("search", "搜索", 60), ("host", "域名", 180),
        ):
            self.mgr_tree.heading(key, text=title)
            self.mgr_tree.column(key, width=width, anchor="w")
        self.mgr_tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.mgr_tree.yview)
        scroll.pack(side="right", fill="y")
        self.mgr_tree.configure(yscrollcommand=scroll.set)
        self.mgr_tree.bind("<Double-1>", lambda _event: self._edit_source())

        self.mgr_status = tk.StringVar(value="书源是独立的 JSON 配置；程序本身不含任何站点逻辑。")
        tk.Label(
            window, textvariable=self.mgr_status, bg=BG, fg=MUTED,
            font=(self.family, 9), anchor="w",
        ).pack(fill="x", padx=12, pady=(0, 8))

        self._refresh_manager()

    def _refresh_manager(self) -> None:
        if self._manager is None or not self._manager.winfo_exists():
            return
        for iid in self.mgr_tree.get_children():
            self.mgr_tree.delete(iid)
        for source in self.source_store.load():
            problems = source.validate()
            status = "启用" if source.enabled else "停用"
            search = "有" if source.can_search else "无"
            self.mgr_tree.insert(
                "", "end", iid=source.key,
                values=(source.name, source.group or "-", status, search, source.host),
            )
            if problems:
                self.mgr_status.set(f"{source.name}：{'；'.join(problems)}")
        self._reload_sources()

    def _selected_source(self) -> BookSource | None:
        selection = self.mgr_tree.selection() if hasattr(self, "mgr_tree") else ()
        if not selection:
            return None
        return self.source_store.get(selection[0])

    def _import_file(self) -> None:
        path = filedialog.askopenfilename(
            parent=self._manager or self,
            title="导入书源 JSON",
            filetypes=[("书源 JSON", "*.json"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            imported = self.source_store.import_file(path)
        except (OSError, ValueError, SourceError) as exc:
            messagebox.showerror("导入失败", str(exc), parent=self._manager or self)
            return
        self._refresh_manager()
        self._status(f"已导入 {len(imported)} 个书源")

    def _import_url(self) -> None:
        url = simpledialog.askstring(
            "从链接导入",
            "书源 JSON 链接：",
            parent=self._manager or self,
        )
        if not url:
            return
        self.mgr_status.set("正在下载书源…")

        def work() -> list[BookSource]:
            from .http import HttpClient

            response = HttpClient(timeout=20).get(
                url, headers={"User-Agent": "Mozilla/5.0"}
            )
            return self.source_store.import_text(response.text)

        def done(imported: list[BookSource]) -> None:
            self._refresh_manager()
            self._status(f"已从链接导入 {len(imported)} 个书源")
            if self._manager is not None and self._manager.winfo_exists():
                self.mgr_status.set(f"已从链接导入 {len(imported)} 个书源")

        def failed(error: str) -> None:
            if self._manager is not None and self._manager.winfo_exists():
                self.mgr_status.set(f"链接导入失败：{error}")
            messagebox.showerror("导入失败", error, parent=self._manager or self)

        self._submit("从链接导入", work, done, failed)

    def _import_clipboard(self) -> None:
        try:
            text = self.clipboard_get()
        except tk.TclError:
            text = ""
        if not text.strip():
            messagebox.showinfo("剪贴板为空", "请先复制书源 JSON 文本。", parent=self._manager or self)
            return
        try:
            imported = self.source_store.import_text(text)
        except (ValueError, SourceError) as exc:
            messagebox.showerror("导入失败", str(exc), parent=self._manager or self)
            return
        self._refresh_manager()
        self._status(f"已从剪贴板导入 {len(imported)} 个书源")

    def _export_sources(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self._manager or self,
            title="导出书源",
            defaultextension=".json",
            filetypes=[("书源 JSON", "*.json")],
        )
        if path:
            self.source_store.export_file(path)
            self._status(f"书源已导出到 {path}")

    def _toggle_source(self) -> None:
        source = self._selected_source()
        if source is None:
            self.mgr_status.set("请先选择一个书源")
            return
        new_state = not source.enabled
        self.source_store.set_enabled(source.key, new_state)
        self._refresh_manager()
        self.mgr_status.set(f"{source.name} 已{'启用' if new_state else '停用'}")

    def _delete_source(self) -> None:
        source = self._selected_source()
        if source is None:
            self.mgr_status.set("请先选择一个书源")
            return
        if not messagebox.askyesno(
            "删除书源", f"确定删除「{source.name}」？", parent=self._manager or self
        ):
            return
        self.source_store.remove(source.key)
        self._refresh_manager()
        self.mgr_status.set(f"已删除「{source.name}」")

    def _test_source(self) -> None:
        source = self._selected_source()
        if source is None:
            self.mgr_status.set("请先选择一个书源")
            return
        keyword = simpledialog.askstring(
            "测试书源", "搜索关键词：", initialvalue="斗破", parent=self._manager or self
        )
        if not keyword:
            return
        self.mgr_status.set(f"正在测试「{source.name}」…")

        def done(books: list[SearchBook]) -> None:
            self.mgr_status.set(f"「{source.name}」返回 {len(books)} 本：{', '.join(b.name for b in books[:3])}")

        def failed(error: str) -> None:
            self.mgr_status.set(f"「{source.name}」测试失败：{error}")

        self._submit(f"测试 {source.name}", lambda: self.engine.search(source, keyword), done, failed)

    def _edit_source(self) -> None:
        source = self._selected_source()
        if source is None:
            self.mgr_status.set("请先选择一个书源")
            return
        if self._editor is not None and self._editor.winfo_exists():
            self._editor.lift()
            return
        window = tk.Toplevel(self._manager or self)
        self._editor = window
        window.title(f"编辑书源 · {source.name}")
        window.geometry("820x680")
        window.configure(bg=BG)
        text = ScrolledText(window, font=("Consolas", 10), wrap="none", undo=True)
        text.pack(fill="both", expand=True, padx=10, pady=(10, 6))
        text.insert("1.0", json.dumps(source.to_dict(), ensure_ascii=False, indent=2))

        bar = tk.Frame(window, bg=BG)
        bar.pack(fill="x", padx=10, pady=(0, 8))

        def save() -> None:
            try:
                sources = parse_sources(text.get("1.0", "end"))
                if len(sources) != 1:
                    raise ValueError("请只保留一个书源对象")
                self.source_store.add(sources[0])
            except (ValueError, SourceError) as exc:
                messagebox.showerror("保存失败", str(exc), parent=window)
                return
            self._refresh_manager()
            self._status(f"书源「{sources[0].name}」已保存")
            window.destroy()

        def format_json() -> None:
            try:
                data = json.loads(text.get("1.0", "end"))
            except ValueError as exc:
                messagebox.showerror("JSON 错误", str(exc), parent=window)
                return
            text.delete("1.0", "end")
            text.insert("1.0", json.dumps(data, ensure_ascii=False, indent=2))

        ttk.Button(bar, text="保存", style="Accent.TButton", command=save).pack(side="right")
        ttk.Button(bar, text="取消", command=window.destroy).pack(side="right", padx=(0, 6))
        ttk.Button(bar, text="格式化", command=format_json).pack(side="left")
        tk.Label(
            bar, text="url / searchUrl / ruleSearch / ruleToc / ruleContent 均遵循 legado 语法",
            bg=BG, fg=MUTED, font=(self.family, 9),
        ).pack(side="left", padx=(10, 0))

    # ------------------------------------------------------------------
    # 示例站点
    # ------------------------------------------------------------------
    def _toggle_demo(self) -> None:
        if self._demo_site is not None:
            self._demo_site.stop()
            self._demo_site = None
            self.demo_button.configure(text="示例站点")
            self._status("示例站点已停止")
            return
        site: DemoSite | None = None
        try:
            site = DemoSite(8765).start()
        except OSError:
            try:
                site = DemoSite(0).start()
            except OSError as exc:
                messagebox.showerror("无法启动示例站点", str(exc), parent=self)
                return
        self._demo_site = site
        self.demo_button.configure(text="停止示例")
        folder = APP_DIR / "书源示例"
        # 清理上一次导入的示例书源，避免旧端口/旧地址残留
        for source in list(self.source_store.load()):
            if source.name.startswith("内置示例") or (
                "127.0.0.1" in source.url
                and any(part in source.url for part in ("/html", "/json", "/regex"))
            ):
                self.source_store.remove(source.key)
        imported = 0
        for path in sorted(folder.glob("*.json")):
            try:
                for source in load_sources_file(path):
                    data = source.to_dict()
                    if "127.0.0.1:8765" in data.get("bookSourceUrl", ""):
                        data["bookSourceUrl"] = data["bookSourceUrl"].replace(
                            "http://127.0.0.1:8765", site.base_url
                        )
                    self.source_store.add(BookSource.from_dict(data))
                    imported += 1
            except (OSError, ValueError):
                continue
        self._reload_sources()
        if self._manager is not None and self._manager.winfo_exists():
            self._refresh_manager()
        self._status(f"示例站点已启动：{site.base_url}，导入 {imported} 个示例书源")

    # ------------------------------------------------------------------
    def destroy(self) -> None:
        self._closed = True
        try:
            self.tasks.shutdown()
        except Exception:
            pass
        if self._demo_site is not None:
            try:
                self._demo_site.stop()
            except Exception:
                pass
            self._demo_site = None
        super().destroy()
