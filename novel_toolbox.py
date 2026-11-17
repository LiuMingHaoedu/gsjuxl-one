#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小工作箱
========

一个 Tkinter 桌面程序，把三件事放在同一个窗口里：
  1. 小说 TXT 合并去重；
  2. 违禁词检测与命中文件隔离；
  3. MP4 视频去重、检测报告、隔离和文件名清理。

不需要安装第三方库。直接运行：
    python novel_toolbox.py
或双击「启动小工作箱.bat」。
"""

from __future__ import annotations

import ctypes
import os
import queue
import shutil
import subprocess
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont
from tkinter.scrolledtext import ScrolledText

import file_renamer as fr
import novel_merger as nm
import video_merger as vm
import word_checker as wc

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
    RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR))
else:
    BASE_DIR = Path(__file__).resolve().parent
    RESOURCE_DIR = BASE_DIR
ASSETS_DIR = RESOURCE_DIR / "assets"
APP_VERSION = "1.3.1"

BG = "#f3f6fb"
CARD = "#ffffff"
BORDER = "#dbe3ef"
TEXT = "#162033"
MUTED = "#64748b"
ACCENT = "#2563eb"
ACCENT_DARK = "#1d4ed8"
SOFT = "#eff6ff"
GREEN = "#15803d"
RED = "#b91c1c"
DARK = "#0b1220"
SIDEBAR = "#0f172a"
SIDEBAR_HOVER = "#17243b"
SIDEBAR_ACTIVE = "#1e293b"
SIDEBAR_TEXT = "#cbd5e1"
SIDEBAR_MUTED = "#7d8ca3"

SETTINGS_DIR = Path(os.environ.get("APPDATA", str(BASE_DIR))) / "小工作箱"
SETTINGS_FILE = SETTINGS_DIR / "settings.json"
STANDARD_WORDS_FILE = SETTINGS_DIR / "标准违禁词.txt"


def ensure_standard_words() -> Path:
    """用户可编辑的标准词库；首次运行时从程序内置词库复制。"""
    source = RESOURCE_DIR / "标准违禁词.txt"
    if not source.exists():
        source = RESOURCE_DIR / "违禁词.txt"
    try:
        SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        if not STANDARD_WORDS_FILE.exists() and source.exists():
            shutil.copyfile(source, STANDARD_WORDS_FILE)
        if STANDARD_WORDS_FILE.exists():
            return STANDARD_WORDS_FILE
    except OSError:
        pass
    return source

TOOL_SPECS = {
    "merge": {
        "title": "小说合并",
        "subtitle": "TXT 书库去重",
        "icon": "nav-merge.png",
        "card_icon": "card-merge.png",
        "desc": "对比文件名、内容与大小，保留更完整、命名更干净的一本",
        "points": "内容指纹判重\n文件名质量评分\n报告与摘要",
    },
    "check": {
        "title": "违禁词检测",
        "subtitle": "扫描与隔离",
        "icon": "nav-check.png",
        "card_icon": "card-check.png",
        "desc": "扫描 TXT 中的违禁词，默认隔离达到阈值的文件",
        "points": "命中词/次数/行号\n默认移入隔离区\n永久删除二次确认",
    },
    "video": {
        "title": "MP4 去重",
        "subtitle": "视频清晰度去重",
        "icon": "nav-video.png",
        "card_icon": "card-video.png",
        "desc": "识别同名、同时长、同画幅的视频，保留更高清的版本",
        "points": "清晰度择优\n检测报告\n视频隔离区\n文件名清理",
    },
}

PAGE_INFO = {
    "home": ("首页", "选择一个工具开始；常用工具会自动记录到最近使用"),
    "merge": ("小说合并", TOOL_SPECS["merge"]["desc"]),
    "check": ("违禁词检测", TOOL_SPECS["check"]["desc"]),
    "video": ("MP4 去重", TOOL_SPECS["video"]["desc"]),
}


def load_settings() -> dict:
    try:
        import json
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, ImportError):
        return {}


def save_settings(data: dict) -> None:
    try:
        import json
        SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


def enable_dpi_awareness() -> None:
    if not sys.platform.startswith("win"):
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def shorten(text: str, limit: int = 110) -> str:
    text = str(text).replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def open_path(path: Path, select_file: bool = False) -> None:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"路径不存在：{path}")
    if sys.platform.startswith("win"):
        if select_file and path.is_file():
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class NovelToolbox:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.cancel_event = threading.Event()
        self.busy = False
        self.active_tab: str | None = None
        self._done_callback = None
        self._closing = False
        self.check_result: wc.CheckResult | None = None
        self.check_rows: dict[str, tuple[wc.FileHits, wc.WordHit]] = {}
        self.last_output: Path | None = None
        self.last_quarantine: Path | None = None
        self.last_report: Path | None = None
        self.last_video_output: Path | None = None
        self.last_video_report: Path | None = None
        self.last_video_quarantine: Path | None = None
        self.video_outcome: vm.VideoOutcome | None = None
        self.video_quarantined = False
        self._pending_quarantine: Path | None = None
        self.video_report_window: tk.Toplevel | None = None
        self.rename_window: tk.Toplevel | None = None
        self.standard_words_window: tk.Toplevel | None = None
        self.settings = load_settings()
        self.standard_words_path = ensure_standard_words()
        self.favorites = [
            key for key in self.settings.get("favorites", [])
            if key in TOOL_SPECS
        ]
        self.recent = [
            key for key in self.settings.get("recent", [])
            if key in TOOL_SPECS
        ][:5]
        self.sidebar_collapsed = bool(self.settings.get("sidebar_collapsed", False))
        self.open_tabs: list[str] = []
        self.current_page = "home"
        self.card_photos: dict[str, tk.PhotoImage] = {}
        self._merge_phase = ""
        self._merge_bucket = -1
        self._check_bucket = -1
        self._video_phase = ""
        self._video_bucket = -1

        self.font_family = "Microsoft YaHei UI"
        available = set(tkfont.families(root))
        if self.font_family not in available:
            self.font_family = "Microsoft YaHei" if "Microsoft YaHei" in available else "TkDefaultFont"

        self._configure_window()
        self._configure_style()
        self._build_ui()
        self._apply_defaults()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Escape>", lambda _event: self.cancel_current())
        self._poll_events()

    # ------------------------------------------------------------------
    # 外观
    # ------------------------------------------------------------------
    def _configure_window(self) -> None:
        self.root.title("小工作箱 · 小说 / 视频 / 违禁词")
        self.root.configure(bg=BG)
        icon = RESOURCE_DIR / "app.ico"
        if icon.exists():
            try:
                self.root.iconbitmap(default=str(icon))
            except tk.TclError:
                pass
        try:
            dpi_scale = max(1.0, min(2.25, self.root.winfo_fpixels("1i") / 96.0))
        except tk.TclError:
            dpi_scale = 1.0
        logical_width, logical_height = 1320, 860
        width = int(logical_width * dpi_scale)
        height = int(logical_height * dpi_scale)
        self.root.minsize(1320, 840)
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        width = min(width, max(1100, screen_w - 80))
        height = min(height, max(720, screen_h - 100))
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 2 - 20)
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        saved_geometry = self.settings.get("geometry") if hasattr(self, "settings") else None
        if isinstance(saved_geometry, str) and "x" in saved_geometry:
            try:
                self.root.geometry(saved_geometry)
            except tk.TclError:
                pass
        self.root.minsize(
            min(int(logical_width * dpi_scale), width),
            min(int(840 * dpi_scale), height),
        )
        self.root.rowconfigure(0, weight=1)
        self.root.columnconfigure(0, minsize=220)
        self.root.columnconfigure(1, weight=1)

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        f = self.font_family
        style.configure(".", font=(f, 10), background=BG, foreground=TEXT)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Card.TLabel", background=CARD, foreground=TEXT)
        style.configure("Muted.TLabel", background=CARD, foreground=MUTED)
        style.configure("Hint.TLabel", background=CARD, foreground=MUTED, font=(f, 9))
        style.configure("Danger.TLabel", background=CARD, foreground=RED, font=(f, 9))
        style.configure("Title.TLabel", background=BG, foreground=TEXT, font=(f, 20, "bold"))
        style.configure("Subtitle.TLabel", background=BG, foreground=MUTED, font=(f, 10))
        style.configure("Section.TLabel", background=CARD, foreground=TEXT, font=(f, 10, "bold"))
        style.configure("StatTitle.TLabel", background=CARD, foreground=MUTED, font=(f, 9))
        style.configure("StatValue.TLabel", background=CARD, foreground=ACCENT, font=(f, 16, "bold"))
        style.configure("Card.TLabelframe", background=CARD, bordercolor=BORDER,
                        relief="solid", borderwidth=1)
        style.configure("Card.TLabelframe.Label", background=CARD, foreground=TEXT,
                        font=(f, 10, "bold"))
        style.configure("Card.TCheckbutton", background=CARD, foreground=TEXT)
        style.map("Card.TCheckbutton", background=[("active", CARD)])
        style.configure("TButton", font=(f, 10), padding=(12, 7))
        style.configure("Accent.TButton", font=(f, 10, "bold"), padding=(16, 8),
                        background=ACCENT, foreground="#ffffff", borderwidth=0)
        style.map("Accent.TButton",
                  background=[("active", ACCENT_DARK), ("disabled", "#93b4f7")],
                  foreground=[("disabled", "#eff6ff")])
        style.configure("Secondary.TButton", padding=(12, 7), background="#ffffff",
                        foreground=TEXT, bordercolor=BORDER)
        style.map("Secondary.TButton", background=[("active", "#eef4ff")])
        style.configure("Danger.TButton", font=(f, 10, "bold"), padding=(14, 7),
                        background="#fee2e2", foreground=RED, bordercolor="#fecaca")
        style.map("Danger.TButton", background=[("active", "#fecaca")])
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", font=(f, 10, "bold"), padding=(22, 10),
                        background="#e8eef8", foreground=MUTED)
        style.map("TNotebook.Tab",
                  background=[("selected", CARD)],
                  foreground=[("selected", ACCENT)])
        style.configure("TEntry", fieldbackground="#ffffff", foreground=TEXT,
                        bordercolor=BORDER, padding=5)
        style.configure("TSpinbox", fieldbackground="#ffffff", foreground=TEXT,
                        bordercolor=BORDER, padding=4)
        style.configure("Accent.Horizontal.TProgressbar", troughcolor="#e5ebf5",
                        background=ACCENT, thickness=8, borderwidth=0)
        style.configure("Treeview", background=CARD, fieldbackground=CARD,
                        foreground=TEXT, rowheight=27, borderwidth=0)
        style.configure("Treeview.Heading", font=(f, 9, "bold"), background="#eef3fb",
                        foreground=MUTED, relief="flat", padding=(6, 5))
        style.map("Treeview", background=[("selected", "#dbeafe")],
                  foreground=[("selected", "#1e3a8a")])
        style.configure("Card.Horizontal.TScale", background=CARD, troughcolor="#e5ebf5")

    # ------------------------------------------------------------------
    # 构建界面
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self.sidebar = tk.Frame(self.root, bg=SIDEBAR, width=220)
        self.sidebar.grid(row=0, column=0, sticky="ns")
        self.sidebar.grid_propagate(False)
        self._build_sidebar()

        main = tk.Frame(self.root, bg=BG)
        main.grid(row=0, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)
        self._build_page_header(main)

        self.tab_bar = tk.Frame(main, bg=BG)
        self.tab_bar.grid(row=1, column=0, sticky="ew", padx=22, pady=(0, 2))

        self.page_host = tk.Frame(main, bg=BG)
        self.page_host.grid(row=2, column=0, sticky="nsew", padx=18, pady=(2, 2))
        self.page_host.rowconfigure(0, weight=1)
        self.page_host.columnconfigure(0, weight=1)

        self.home_tab = tk.Frame(self.page_host, bg=BG)
        self.merge_tab = tk.Frame(self.page_host, bg=BG)
        self.check_tab = tk.Frame(self.page_host, bg=BG)
        self.video_tab = tk.Frame(self.page_host, bg=BG)
        for page in (self.home_tab, self.merge_tab, self.check_tab, self.video_tab):
            page.grid(row=0, column=0, sticky="nsew")
            page.grid_remove()
        self.pages = {
            "home": self.home_tab,
            "merge": self.merge_tab,
            "check": self.check_tab,
            "video": self.video_tab,
        }
        self._build_home_tab()
        self._build_merge_tab()
        self._build_check_tab()
        self._build_video_tab()

        footer = tk.Frame(main, bg=BG)
        footer.grid(row=3, column=0, sticky="ew", padx=22, pady=(0, 8))
        footer.columnconfigure(1, weight=1)
        self.footer_var = tk.StringVar(value="就绪")
        tk.Label(footer, text="●", bg=BG, fg=GREEN,
                 font=(self.font_family, 8)).grid(row=0, column=0, sticky="w")
        tk.Label(footer, textvariable=self.footer_var, bg=BG, fg=MUTED,
                 font=(self.font_family, 9), anchor="w").grid(
            row=0, column=1, sticky="w", padx=(6, 0))
        tk.Label(footer, text="本地处理 · 源文件只读 · 隔离/永久删除均需确认",
                 bg=BG, fg=MUTED, font=(self.font_family, 9)).grid(
            row=0, column=2, sticky="e")

        self._refresh_tabs()
        self.show_page("home", save=False)

    def _build_sidebar(self) -> None:
        self.sidebar_brand = tk.Frame(self.sidebar, bg=SIDEBAR)
        self.sidebar_brand.pack(fill="x", padx=14, pady=(18, 18))
        self.logo_photo = None
        try:
            self.logo_photo = tk.PhotoImage(file=str(ASSETS_DIR / "logo-small.png"))
            tk.Label(self.sidebar_brand, image=self.logo_photo, bg=SIDEBAR).pack(side="left")
        except tk.TclError:
            pass
        self.brand_text = tk.Frame(self.sidebar_brand, bg=SIDEBAR)
        self.brand_text.pack(side="left", padx=(10, 0))
        tk.Label(self.brand_text, text="小工作箱", bg=SIDEBAR, fg="#ffffff",
                 font=(self.font_family, 16, "bold")).pack(anchor="w")
        tk.Label(self.brand_text, text=f"v{APP_VERSION}", bg=SIDEBAR, fg=SIDEBAR_MUTED,
                 font=(self.font_family, 8)).pack(anchor="w")
        self.collapse_btn = tk.Label(
            self.sidebar_brand, text="«" if not self.sidebar_collapsed else "»",
            bg=SIDEBAR, fg=SIDEBAR_MUTED, font=(self.font_family, 13),
            cursor="hand2", padx=4,
        )
        self.collapse_btn.pack(side="right")
        self.collapse_btn.bind("<Button-1>", lambda _e: self._toggle_sidebar())

        self.nav_container = tk.Frame(self.sidebar, bg=SIDEBAR)
        self.nav_container.pack(fill="x")

        self.sidebar_bottom = tk.Frame(self.sidebar, bg=SIDEBAR)
        self.sidebar_bottom.pack(side="bottom", fill="x", pady=(0, 8))
        tk.Frame(self.sidebar_bottom, height=1, bg="#1e293b").pack(
            fill="x", padx=16, pady=(0, 12))
        self.sidebar_hint_text = tk.Label(
            self.sidebar_bottom, text="本地处理\n文件不会上传", bg=SIDEBAR,
            fg=SIDEBAR_MUTED, font=(self.font_family, 8),
            justify="left", anchor="w",
        )
        self.sidebar_hint_text.pack(fill="x", padx=20)
        self.sidebar_hint = tk.Label(
            self.sidebar_bottom, text="● 安全模式", bg=SIDEBAR, fg="#34d399",
            font=(self.font_family, 8), anchor="w",
        )
        self.sidebar_hint.pack(fill="x", padx=20, pady=(8, 0))
        self._apply_sidebar_state()
        self._refresh_nav()

    def _toggle_sidebar(self) -> None:
        self.sidebar_collapsed = not self.sidebar_collapsed
        self._apply_sidebar_state()
        self._refresh_nav()
        self._save_settings()

    def _apply_sidebar_state(self) -> None:
        width = 76 if self.sidebar_collapsed else 220
        self.sidebar.configure(width=width)
        self.root.columnconfigure(0, minsize=width)
        self.collapse_btn.configure(text="»" if self.sidebar_collapsed else "«")
        if self.sidebar_collapsed:
            self.brand_text.pack_forget()
            self.sidebar_brand.pack_configure(padx=12)
            self.sidebar_hint_text.pack_forget()
            self.sidebar_hint.pack_forget()
        else:
            if not self.brand_text.winfo_ismapped():
                self.brand_text.pack(side="left", padx=(10, 0))
            self.sidebar_brand.pack_configure(padx=14)
            if not self.sidebar_hint_text.winfo_ismapped():
                self.sidebar_hint_text.pack(fill="x", padx=20)
            if not self.sidebar_hint.winfo_ismapped():
                self.sidebar_hint.pack(fill="x", padx=20, pady=(8, 0))

    def _make_nav_group(self, title: str) -> None:
        if self.sidebar_collapsed:
            return
        tk.Label(self.nav_container, text=title, bg=SIDEBAR, fg=SIDEBAR_MUTED,
                 font=(self.font_family, 8, "bold"), anchor="w").pack(
            fill="x", padx=20, pady=(10, 4))

    def _refresh_nav(self) -> None:
        if not hasattr(self, "nav_container"):
            return
        for child in self.nav_container.winfo_children():
            child.destroy()
        self.nav_items: dict[str, dict] = {}
        self._make_nav_item(
            "home", "首页", "工具总览", "nav-home.png", is_home=True
        )
        favourites = [key for key in self.favorites if key in TOOL_SPECS]
        recent = [key for key in self.recent if key in TOOL_SPECS and key not in favourites]
        ordered = favourites + recent
        rest = [key for key in TOOL_SPECS if key not in ordered]
        if self.sidebar_collapsed:
            for key in ordered + rest:
                self._make_nav_item(key, **self._nav_spec(key))
            return
        if favourites:
            self._make_nav_group("收藏")
            for key in favourites:
                self._make_nav_item(key, **self._nav_spec(key))
        if recent:
            self._make_nav_group("最近使用")
            for key in recent[:3]:
                self._make_nav_item(key, **self._nav_spec(key))
        if rest:
            self._make_nav_group("全部工具" if (favourites or recent) else "工具")
            for key in rest:
                self._make_nav_item(key, **self._nav_spec(key))
        self._paint_nav_selection()

    def _nav_spec(self, key: str) -> dict:
        spec = TOOL_SPECS[key]
        return {
            "title": spec["title"], "subtitle": spec["subtitle"],
            "icon_name": spec["icon"], "is_home": False,
        }

    def _make_nav_item(self, key: str, title: str, subtitle: str, icon_name: str,
                       is_home: bool = False) -> None:
        collapsed = self.sidebar_collapsed
        outer = tk.Frame(self.nav_container, bg=SIDEBAR, cursor="hand2")
        outer.pack(fill="x", padx=8 if collapsed else 10, pady=2)
        accent = tk.Frame(outer, bg=SIDEBAR, width=3)
        accent.pack(side="left", fill="y")
        inner = tk.Frame(outer, bg=SIDEBAR, cursor="hand2")
        inner.pack(side="left", fill="x", expand=True,
                   padx=(10 if collapsed else 12, 4 if collapsed else 8), pady=8)

        image = None
        try:
            image = tk.PhotoImage(file=str(ASSETS_DIR / icon_name))
            icon = tk.Label(inner, image=image, bg=SIDEBAR)
        except tk.TclError:
            icon = tk.Label(inner, text="•", bg=SIDEBAR, fg="#ffffff",
                            font=(self.font_family, 12, "bold"))
        if collapsed:
            icon.pack(anchor="center")
        else:
            icon.pack(side="left")

        text = None
        title_label = None
        subtitle_label = None
        star = None
        if not collapsed:
            text = tk.Frame(inner, bg=SIDEBAR, cursor="hand2")
            text.pack(side="left", padx=(10, 0), fill="x", expand=True)
            title_label = tk.Label(text, text=title, bg=SIDEBAR, fg=SIDEBAR_TEXT,
                                   font=(self.font_family, 10, "bold"), anchor="w")
            title_label.pack(anchor="w")
            subtitle_label = tk.Label(text, text=subtitle, bg=SIDEBAR, fg=SIDEBAR_MUTED,
                                      font=(self.font_family, 8), anchor="w")
            subtitle_label.pack(anchor="w")
            if not is_home:
                star = tk.Label(
                    inner, text="★" if key in self.favorites else "☆",
                    bg=SIDEBAR, fg="#fbbf24" if key in self.favorites else SIDEBAR_MUTED,
                    font=(self.font_family, 12), cursor="hand2", padx=4,
                )
                star.pack(side="right")
                star.bind("<Button-1>", lambda _e, k=key: self._toggle_favorite(k))

        self.nav_items[key] = {
            "outer": outer, "accent": accent, "inner": inner, "icon": icon,
            "text": text, "title": title_label, "subtitle": subtitle_label,
            "image": image, "star": star,
        }

        def click(_event=None, page_key=key):
            self.show_page(page_key)

        def enter(_event=None, page_key=key):
            if self.current_page != page_key:
                self._paint_nav(page_key, SIDEBAR_HOVER, "#ffffff")

        def leave(_event=None, page_key=key):
            if self.current_page != page_key:
                self._paint_nav(page_key, SIDEBAR, SIDEBAR_TEXT)

        for widget in (outer, accent, inner, icon, text, title_label, subtitle_label):
            if widget is None:
                continue
            widget.bind("<Button-1>", click)
            widget.bind("<Enter>", enter)
            widget.bind("<Leave>", leave)

    def _toggle_favorite(self, key: str) -> None:
        if key in self.favorites:
            self.favorites.remove(key)
        else:
            self.favorites.insert(0, key)
        self.recent = [k for k in self.recent if k != key]
        self._refresh_nav()
        self._refresh_home_stars()
        self._save_settings()

    def _paint_nav(self, key: str, background: str, foreground: str) -> None:
        item = self.nav_items.get(key)
        if not item:
            return
        item["outer"].configure(bg=background)
        item["inner"].configure(bg=background)
        item["icon"].configure(bg=background)
        item["accent"].configure(bg=background)
        if item["text"]:
            item["text"].configure(bg=background)
        if item["title"]:
            item["title"].configure(bg=background, fg=foreground)
        if item["subtitle"]:
            item["subtitle"].configure(bg=background, fg=SIDEBAR_MUTED)
        if item["star"]:
            item["star"].configure(bg=background)

    def _paint_nav_selection(self) -> None:
        for nav_key in self.nav_items:
            selected = nav_key == self.current_page
            self._paint_nav(
                nav_key,
                SIDEBAR_ACTIVE if selected else SIDEBAR,
                "#ffffff" if selected else SIDEBAR_TEXT,
            )
            self.nav_items[nav_key]["accent"].configure(
                bg=ACCENT if selected else SIDEBAR
            )

    def _build_page_header(self, parent: tk.Widget) -> None:
        header = tk.Frame(parent, bg=BG)
        header.grid(row=0, column=0, sticky="ew", padx=22, pady=(16, 6))
        header.columnconfigure(0, weight=1)
        self.page_title_var = tk.StringVar()
        self.page_desc_var = tk.StringVar()
        tk.Label(header, textvariable=self.page_title_var, bg=BG, fg=TEXT,
                 font=(self.font_family, 20, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(header, textvariable=self.page_desc_var, bg=BG, fg=MUTED,
                 font=(self.font_family, 10)).grid(row=1, column=0, sticky="w", pady=(2, 0))
        chip = tk.Label(header, text="本地工具 · 不上传", bg="#e0e7ff", fg="#3730a3",
                        font=(self.font_family, 8, "bold"), padx=10, pady=4)
        chip.grid(row=0, column=1, rowspan=2, sticky="e")

    def show_page(self, key: str, save: bool = True) -> None:
        if key not in self.pages:
            return
        if key != "home":
            if key not in self.open_tabs:
                self.open_tabs.append(key)
            self.recent = [key] + [item for item in self.recent if item != key]
            self.recent = self.recent[:5]
        for page_key, page in self.pages.items():
            if page_key == key:
                page.grid()
            else:
                page.grid_remove()
        self.current_page = key
        title, desc = PAGE_INFO[key]
        self.page_title_var.set(title)
        self.page_desc_var.set(desc)
        self._refresh_tabs()
        self._refresh_nav()
        if save:
            self._save_settings()

    def _refresh_tabs(self) -> None:
        if not hasattr(self, "tab_bar"):
            return
        for child in self.tab_bar.winfo_children():
            child.destroy()
        keys = ["home"] + self.open_tabs
        for key in keys:
            active = key == self.current_page
            tab = tk.Frame(
                self.tab_bar, bg=CARD if active else BG,
                highlightbackground=BORDER if not active else "#bfdbfe",
                highlightthickness=1, cursor="hand2",
            )
            tab.pack(side="left", padx=(0, 6), pady=(3, 0))
            label = tk.Label(
                tab, text=PAGE_INFO[key][0], bg=CARD if active else BG,
                fg=ACCENT if active else MUTED,
                font=(self.font_family, 9, "bold" if active else "normal"),
                padx=12, pady=5, cursor="hand2",
            )
            label.pack(side="left")
            label.bind("<Button-1>", lambda _e, page_key=key: self.show_page(page_key))
            tab.bind("<Button-1>", lambda _e, page_key=key: self.show_page(page_key))
            if key != "home":
                close = tk.Label(
                    tab, text="×", bg=CARD if active else BG,
                    fg=MUTED, font=(self.font_family, 10), padx=7, pady=5, cursor="hand2",
                )
                close.pack(side="left")
                close.bind("<Button-1>", lambda _e, page_key=key: self.close_tab(page_key))
        tk.Frame(self.tab_bar, bg=BORDER, height=1).pack(side="bottom", fill="x")

    def close_tab(self, key: str) -> None:
        if key == "home" or key not in self.open_tabs:
            return
        self.open_tabs.remove(key)
        if self.current_page == key:
            fallback = self.open_tabs[-1] if self.open_tabs else "home"
            self.show_page(fallback)
        else:
            self._refresh_tabs()
            self._save_settings()

    def _build_home_tab(self) -> None:
        wrap = tk.Frame(self.home_tab, bg=BG)
        wrap.pack(fill="both", expand=True, padx=6, pady=(2, 0))
        wrap.columnconfigure(0, weight=1)

        hero = tk.Frame(wrap, bg="#0f172a", padx=26, pady=20)
        hero.grid(row=0, column=0, sticky="ew", pady=(0, 16))
        hero.columnconfigure(0, weight=1)
        tk.Label(hero, text="一个窗口，处理三类文件", bg="#0f172a", fg="#ffffff",
                 font=(self.font_family, 18, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(
            hero,
            text="小说 TXT · 违禁词 · MP4 视频。所有处理都在本机完成，文件不会上传。",
            bg="#0f172a", fg="#94a3b8", font=(self.font_family, 9),
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))
        tk.Label(
            hero, text="● 本地处理", bg="#0f172a", fg="#6ee7b7",
            font=(self.font_family, 9, "bold"),
        ).grid(row=0, column=1, rowspan=2, sticky="e", padx=(20, 0))

        grid = tk.Frame(wrap, bg=BG)
        grid.grid(row=1, column=0, sticky="nsew")
        for index in range(3):
            grid.columnconfigure(index, weight=1, uniform="tool")
        self.home_card_stars: dict[str, tk.Label] = {}
        self.home_cards: dict[str, tk.Frame] = {}
        for index, key in enumerate(("merge", "check", "video")):
            spec = TOOL_SPECS[key]
            card = tk.Frame(
                grid, bg=CARD, highlightbackground=BORDER,
                highlightthickness=1, cursor="hand2", padx=18, pady=16,
            )
            card.grid(row=0, column=index, sticky="nsew",
                      padx=(0 if index == 0 else 8, 0 if index == 2 else 8))
            card.grid_propagate(False)
            card.configure(width=320, height=255)
            self.home_cards[key] = card
            image = None
            try:
                image = tk.PhotoImage(file=str(ASSETS_DIR / spec["card_icon"]))
                self.card_photos[key] = image
                icon = tk.Label(card, image=image, bg=CARD)
            except tk.TclError:
                icon = tk.Label(card, text="▣", bg=CARD, fg=ACCENT,
                                font=(self.font_family, 24, "bold"))
            icon.pack(anchor="w")
            title = tk.Label(card, text=spec["title"], bg=CARD, fg=TEXT,
                             font=(self.font_family, 14, "bold"), anchor="w")
            title.pack(anchor="w", pady=(8, 2))
            desc = tk.Label(card, text=spec["desc"], bg=CARD, fg=MUTED,
                            font=(self.font_family, 9), justify="left",
                            wraplength=275, anchor="w")
            desc.pack(anchor="w")
            points = tk.Label(
                card, text="  ·  " + spec["points"].replace("\n", "\n  ·  "),
                bg=CARD, fg="#475569", font=(self.font_family, 8),
                justify="left", anchor="w",
            )
            points.pack(anchor="w", pady=(9, 0))
            open_label = tk.Label(card, text="打开工具  →", bg=CARD, fg=ACCENT,
                                  font=(self.font_family, 9, "bold"), cursor="hand2")
            open_label.pack(side="bottom", anchor="w")
            star = tk.Label(
                card, text="★" if key in self.favorites else "☆",
                bg=CARD, fg="#f59e0b" if key in self.favorites else "#cbd5e1",
                font=(self.font_family, 15), cursor="hand2",
            )
            star.place(relx=1.0, x=-14, y=12, anchor="ne")
            self.home_card_stars[key] = star
            star.bind("<Button-1>", lambda _e, k=key: self._toggle_favorite(k))

            def enter(_event=None, frame=card, k=key):
                frame.configure(highlightbackground=ACCENT)
            def leave(_event=None, frame=card, k=key):
                frame.configure(highlightbackground=BORDER)
            for widget in (card, icon, title, desc, points, open_label):
                widget.bind("<Button-1>", lambda _e, k=key: self.show_page(k))
                widget.bind("<Enter>", enter)
                widget.bind("<Leave>", leave)

        hint = tk.Label(
            wrap, text="提示：点击卡片右上角 ☆ 收藏工具；收藏和最近使用会固定在侧栏顶部。",
            bg=BG, fg=MUTED, font=(self.font_family, 9), anchor="w",
        )
        hint.grid(row=2, column=0, sticky="w", pady=(12, 0))
        wrap.rowconfigure(1, weight=1)

    def _refresh_home_stars(self) -> None:
        for key, star in getattr(self, "home_card_stars", {}).items():
            star.configure(
                text="★" if key in self.favorites else "☆",
                fg="#f59e0b" if key in self.favorites else "#cbd5e1",
            )


    def _make_card(self, parent: tk.Widget, title: str) -> ttk.LabelFrame:
        return ttk.LabelFrame(parent, text="  " + title, style="Card.TLabelframe",
                              padding=(14, 10))

    def _make_log(self, parent: tk.Widget, height: int) -> ScrolledText:
        log = ScrolledText(parent, height=height, width=30, wrap="word", bg=DARK, fg="#dbeafe",
                           insertbackground="#dbeafe", relief="flat", borderwidth=0,
                           highlightthickness=0, font=("Consolas", 9), padx=10, pady=8)
        log.tag_configure("info", foreground="#cbd5e1")
        log.tag_configure("phase", foreground="#93c5fd")
        log.tag_configure("ok", foreground="#86efac")
        log.tag_configure("warn", foreground="#fde68a")
        log.tag_configure("error", foreground="#fca5a5")
        log.configure(state="disabled")
        return log

    def _log(self, widget: ScrolledText, text: str, tag: str = "info") -> None:
        widget.configure(state="normal")
        widget.insert("end", text.rstrip() + "\n", tag)
        widget.see("end")
        widget.configure(state="disabled")

    def _clear_log(self, widget: ScrolledText) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.configure(state="disabled")

    def _make_stats(self, parent: tk.Widget) -> tk.Frame:
        frame = tk.Frame(parent, bg=BG)
        for column in range(4):
            frame.grid_columnconfigure(column, weight=1, uniform="stats")
        return frame

    def _stat_card(self, parent: tk.Widget, column: int, title: str,
                   variable: tk.StringVar) -> None:
        card = tk.Frame(parent, bg=CARD, highlightbackground=BORDER,
                        highlightthickness=1)
        card.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 8, 0))
        tk.Label(card, text=title, bg=CARD, fg=MUTED,
                 font=(self.font_family, 9), anchor="w").pack(
            fill="x", padx=14, pady=(9, 0))
        tk.Label(card, textvariable=variable, bg=CARD, fg=ACCENT,
                 font=(self.font_family, 16, "bold"), anchor="w").pack(
            fill="x", padx=14, pady=(0, 8))

    def _option_details(self, parent: tk.Widget, items: list[tuple[str, str]],
                        row: int, columnspan: int = 2,
                        wraplength: int = 430) -> tk.Frame:
        frame = tk.Frame(parent, bg="#f8fafc", highlightbackground="#e8eef8",
                         highlightthickness=1)
        frame.grid(row=row, column=1, columnspan=columnspan, sticky="ew",
                   pady=(6, 0))
        for index, (title, description) in enumerate(items):
            line = tk.Frame(frame, bg="#f8fafc")
            line.pack(fill="x", padx=10, pady=(7 if index == 0 else 2,
                                               7 if index == len(items) - 1 else 0))
            tk.Label(line, text=title, bg="#f8fafc", fg=TEXT,
                     font=(self.font_family, 9, "bold"), width=12,
                     anchor="nw").pack(side="left", anchor="n")
            tk.Label(line, text=description, bg="#f8fafc", fg=MUTED,
                     font=(self.font_family, 9), justify="left",
                     wraplength=wraplength, anchor="w").pack(
                side="left", fill="x", expand=True)
        return frame


    def _build_merge_tab(self) -> None:
        tab = self.merge_tab
        tab.columnconfigure(0, weight=3)
        tab.columnconfigure(1, weight=2)
        tab.rowconfigure(0, weight=1)

        left = tk.Frame(tab, bg=BG)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        right = tk.Frame(tab, bg=BG)
        right.grid(row=0, column=1, sticky="nsew")

        source_card = self._make_card(left, "1. 选择书库")
        source_card.grid(row=0, column=0, sticky="ew", pady=(8, 8))
        source_card.columnconfigure(1, weight=1)

        ttk.Label(source_card, text="小说根目录", style="Card.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 10), pady=4)
        self.merge_root_var = tk.StringVar()
        ttk.Entry(source_card, textvariable=self.merge_root_var).grid(
            row=0, column=1, sticky="ew", padx=(0, 8), pady=4)
        ttk.Button(source_card, text="浏览…", style="Secondary.TButton",
                   command=self._browse_merge_root).grid(row=0, column=2, pady=4)
        ttk.Label(source_card,
                  text="自动读取根目录下的 TXT；默认只扫描一级文件夹，勾选“包含子文件夹”可扫描更深层。",
                  style="Hint.TLabel", wraplength=480).grid(
            row=1, column=1, columnspan=2, sticky="w", pady=(0, 7))

        ttk.Label(source_card, text="输出文件夹", style="Card.TLabel").grid(
            row=2, column=0, sticky="w", padx=(0, 10), pady=4)
        self.merge_output_var = tk.StringVar()
        ttk.Entry(source_card, textvariable=self.merge_output_var).grid(
            row=2, column=1, sticky="ew", padx=(0, 8), pady=4)
        ttk.Button(source_card, text="浏览…", style="Secondary.TButton",
                   command=self._browse_merge_output).grid(row=2, column=2, pady=4)

        output_info = tk.Frame(source_card, bg=SOFT, highlightbackground="#dbeafe",
                               highlightthickness=1)
        output_info.grid(row=3, column=1, columnspan=2, sticky="ew", pady=(2, 5))
        self.merge_output_info_var = tk.StringVar()
        tk.Label(output_info, text="输出到", bg=SOFT, fg=ACCENT,
                 font=(self.font_family, 9, "bold"), padx=9, pady=4).pack(side="left")
        tk.Label(output_info, textvariable=self.merge_output_info_var, bg=SOFT,
                 fg=TEXT, font=(self.font_family, 9), anchor="w",
                 justify="left", wraplength=430).pack(
            side="left", fill="x", expand=True, padx=(0, 9), pady=4)

        self.merge_recursive_var = tk.BooleanVar(value=False)
        self.merge_overwrite_var = tk.BooleanVar(value=False)
        checks = ttk.Frame(source_card, style="Card.TFrame")
        checks.grid(row=4, column=1, columnspan=2, sticky="w", pady=(3, 0))
        ttk.Checkbutton(checks, text="包含子文件夹",
                        variable=self.merge_recursive_var,
                        style="Card.TCheckbutton").pack(side="left", padx=(0, 22))
        ttk.Checkbutton(checks, text="覆盖已有输出",
                        variable=self.merge_overwrite_var,
                        style="Card.TCheckbutton").pack(side="left")

        self._option_details(
            source_card,
            [
                ("包含子文件夹", "勾选后扫描根目录下所有层级的 TXT；不勾选只扫描一级文件夹。"),
                ("覆盖已有输出", "输出文件夹已存在时先清空再生成；不勾选会先弹窗询问。"),
            ],
            row=5, columnspan=2, wraplength=430,
        )

        options_card = self._make_card(left, "2. 合并规则")
        options_card.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        options_card.columnconfigure(2, weight=1)
        ttk.Label(options_card, text="内容相似度阈值", style="Card.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 12))
        self.merge_threshold_var = tk.DoubleVar(value=nm.DEFAULT_SIMILARITY)
        self.merge_threshold_label = tk.StringVar(
            value=f"{nm.DEFAULT_SIMILARITY:.2f}")
        scale = ttk.Scale(options_card, from_=0.50, to=0.95,
                          variable=self.merge_threshold_var, length=280,
                          style="Card.Horizontal.TScale",
                          command=self._on_threshold_change)
        scale.grid(row=0, column=1, sticky="w", pady=2)
        tk.Label(options_card, textvariable=self.merge_threshold_label, bg=CARD,
                 fg=ACCENT, font=(self.font_family, 10, "bold"), width=5,
                 anchor="w").grid(row=0, column=2, sticky="w", padx=(8, 0))
        ttk.Label(options_card,
                  text="默认 0.68；数值越大越严格。正文相同时，文件名更干净、带“（全）”标识的版本优先。",
                  style="Hint.TLabel", wraplength=520).grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(3, 0))

        actions = tk.Frame(left, bg=BG)
        actions.grid(row=2, column=0, sticky="ew", pady=(0, 4))
        self.merge_start_btn = ttk.Button(actions, text="开始合并", style="Accent.TButton",
                                          command=lambda: self.start_merge(False))
        self.merge_start_btn.pack(side="left")
        self.merge_preview_btn = ttk.Button(actions, text="仅预览", style="Secondary.TButton",
                                            command=lambda: self.start_merge(True))
        self.merge_preview_btn.pack(side="left", padx=8)
        self.merge_cancel_btn = ttk.Button(actions, text="停止", style="Secondary.TButton",
                                           state="disabled", command=self.cancel_current)
        self.merge_cancel_btn.pack(side="left")
        self.merge_open_btn = ttk.Button(actions, text="打开输出文件夹", style="Secondary.TButton",
                                         state="disabled", command=self.open_merge_output)
        self.merge_open_btn.pack(side="right")
        self.merge_check_btn = ttk.Button(actions, text="检测合并结果", style="Secondary.TButton",
                                          state="disabled", command=self.check_merge_output)
        self.merge_check_btn.pack(side="right", padx=8)

        progress_line = tk.Frame(left, bg=BG)
        progress_line.grid(row=3, column=0, sticky="ew", pady=(2, 7))
        progress_line.columnconfigure(0, weight=1)
        self.merge_progress = ttk.Progressbar(progress_line, maximum=100,
                                              style="Accent.Horizontal.TProgressbar")
        self.merge_progress.grid(row=0, column=0, sticky="ew")
        self.merge_status_var = tk.StringVar(value="等待开始")
        tk.Label(progress_line, textvariable=self.merge_status_var, bg=BG, fg=MUTED,
                 font=(self.font_family, 9), anchor="e", width=34).grid(
            row=0, column=1, sticky="e", padx=(12, 0))

        stats = self._make_stats(left)
        stats.grid(row=4, column=0, sticky="ew", pady=(8, 8))
        self.merge_stat_scanned = tk.StringVar(value="0")
        self.merge_stat_kept = tk.StringVar(value="0")
        self.merge_stat_merged = tk.StringVar(value="0")
        self.merge_stat_time = tk.StringVar(value="0")
        self._stat_card(stats, 0, "扫描 TXT", self.merge_stat_scanned)
        self._stat_card(stats, 1, "最终保留", self.merge_stat_kept)
        self._stat_card(stats, 2, "合并重复", self.merge_stat_merged)
        self._stat_card(stats, 3, "耗时", self.merge_stat_time)

        tips = self._make_card(left, "3. 输出说明")
        tips.grid(row=5, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(
            tips,
            text=("• 合并报告.csv 记录每本书的保留原因和去重明细。\n"
                  "• 预览模式只分析，不会创建输出文件夹。\n"
                  "• 源文件不会被修改或删除。"),
            style="Hint.TLabel", justify="left", wraplength=480,
        ).grid(row=0, column=0, sticky="w")

        log_card = self._make_card(right, "运行日志")
        log_card.grid(row=0, column=0, sticky="nsew", pady=(8, 8))
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)
        log_card.rowconfigure(0, weight=1)
        log_card.columnconfigure(0, weight=1)
        self.merge_log = self._make_log(log_card, height=8)
        self.merge_log.grid(row=0, column=0, sticky="nsew")

    def _build_check_tab(self) -> None:
        tab = self.check_tab
        tab.columnconfigure(0, weight=5)
        tab.columnconfigure(1, weight=6)
        tab.rowconfigure(0, weight=1)

        left = tk.Frame(tab, bg=BG)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        right = tk.Frame(tab, bg=BG)
        right.grid(row=0, column=1, sticky="nsew")

        settings = self._make_card(left, "1. 检测设置")
        settings.grid(row=0, column=0, sticky="ew", pady=(8, 8))
        settings.columnconfigure(1, weight=1)

        ttk.Label(settings, text="检测对象", style="Card.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 10), pady=4)
        self.check_target_var = tk.StringVar()
        ttk.Entry(settings, textvariable=self.check_target_var).grid(
            row=0, column=1, sticky="ew", padx=(0, 8), pady=4)
        ttk.Button(settings, text="文件夹…", style="Secondary.TButton",
                   command=self._browse_check_folder).grid(row=0, column=2, padx=(0, 6))
        ttk.Button(settings, text="单个文件…", style="Secondary.TButton",
                   command=self._browse_check_file).grid(row=0, column=3)

        ttk.Label(settings, text="违禁词库", style="Card.TLabel").grid(
            row=1, column=0, sticky="w", padx=(0, 10), pady=4)
        self.check_words_var = tk.StringVar()
        ttk.Entry(settings, textvariable=self.check_words_var).grid(
            row=1, column=1, sticky="ew", padx=(0, 8), pady=4)
        ttk.Button(settings, text="浏览…", style="Secondary.TButton",
                   command=self._browse_words).grid(row=1, column=2, padx=(0, 6))
        ttk.Button(settings, text="编辑词库", style="Secondary.TButton",
                   command=self._open_words).grid(row=1, column=3)

        ttk.Label(settings, text="检测报告", style="Card.TLabel").grid(
            row=2, column=0, sticky="w", padx=(0, 10), pady=4)
        self.check_report_var = tk.StringVar()
        ttk.Entry(settings, textvariable=self.check_report_var).grid(
            row=2, column=1, sticky="ew", padx=(0, 8), pady=4)
        ttk.Button(settings, text="浏览…", style="Secondary.TButton",
                   command=self._browse_report).grid(row=2, column=2, columnspan=2,
                                                     sticky="w")

        report_info = tk.Frame(settings, bg=SOFT, highlightbackground="#dbeafe",
                               highlightthickness=1)
        report_info.grid(row=3, column=1, columnspan=3, sticky="ew", pady=(2, 5))
        self.check_report_info_var = tk.StringVar()
        tk.Label(report_info, text="报告保存到", bg=SOFT, fg=ACCENT,
                 font=(self.font_family, 9, "bold"), padx=9, pady=4).pack(side="left")
        tk.Label(report_info, textvariable=self.check_report_info_var, bg=SOFT,
                 fg=TEXT, font=(self.font_family, 9), anchor="w",
                 justify="left", wraplength=330).pack(
            side="left", fill="x", expand=True, padx=(0, 9), pady=4)

        self.standard_words_info_var = tk.StringVar()
        standard_line = tk.Frame(settings, bg=SOFT, highlightbackground="#dbeafe",
                                 highlightthickness=1)
        standard_line.grid(row=4, column=1, columnspan=3, sticky="ew", pady=(0, 5))
        tk.Label(standard_line, text="标准词库", bg=SOFT, fg=ACCENT,
                 font=(self.font_family, 9, "bold"), padx=9, pady=4).pack(side="left")
        tk.Label(standard_line, textvariable=self.standard_words_info_var, bg=SOFT,
                 fg=TEXT, font=(self.font_family, 9), anchor="w",
                 justify="left", wraplength=210).pack(
            side="left", fill="x", expand=True, padx=(0, 6), pady=4)
        ttk.Button(standard_line, text="现场编辑", style="Secondary.TButton",
                   command=self.show_standard_words_editor).pack(side="right", padx=5)

        self.check_recursive_var = tk.BooleanVar(value=False)
        self.check_ignore_case_var = tk.BooleanVar(value=True)
        options = ttk.Frame(settings, style="Card.TFrame")
        options.grid(row=5, column=1, columnspan=3, sticky="w", pady=(3, 0))
        ttk.Checkbutton(options, text="包含子文件夹", variable=self.check_recursive_var,
                        style="Card.TCheckbutton").pack(side="left", padx=(0, 22))
        ttk.Checkbutton(options, text="忽略英文大小写", variable=self.check_ignore_case_var,
                        style="Card.TCheckbutton").pack(side="left", padx=(0, 22))
        ttk.Label(options, text="删除阈值", style="Card.TLabel").pack(side="left", padx=(0, 6))
        self.check_threshold_var = tk.StringVar(value="1")
        ttk.Spinbox(options, from_=1, to=99999, width=6,
                    textvariable=self.check_threshold_var).pack(side="left")
        ttk.Label(options, text="次", style="Card.TLabel").pack(side="left", padx=(4, 0))

        self.check_permanent_var = tk.BooleanVar(value=False)
        perm_line = ttk.Frame(settings, style="Card.TFrame")
        perm_line.grid(row=6, column=1, columnspan=3, sticky="w", pady=(3, 0))
        ttk.Checkbutton(perm_line, text="永久删除（不推荐，不进回收站）",
                        variable=self.check_permanent_var,
                        command=self._update_process_button,
                        style="Card.TCheckbutton").pack(side="left")
        ttk.Label(perm_line, text="默认只隔离；永久删除会要求二次确认。",
                  style="Danger.TLabel").pack(side="left", padx=(16, 0))

        self._option_details(
            settings,
            [
                ("包含子文件夹", "勾选后扫描目标文件夹下所有层级的 TXT；不勾选只扫描第一层。"),
                ("忽略英文大小写", "勾选后 NULL、Null、null 都算命中；不勾选则严格区分大小写。"),
                ("永久删除", "默认关闭，文件会移入隔离区；只有勾选并确认两次才会永久删除。"),
            ],
            row=7, columnspan=3, wraplength=320,
        )

        tips = self._make_card(left, "使用提示")
        tips.grid(row=1, column=0, sticky="ew")
        tip_text = (
            "• 检测报告会记录文件名、路径、违禁词、次数和行号。\n"
            "• “隔离”只是把文件移动到隔离区，之后可以手动恢复。\n"
            "• 词库为纯文本，每行一个词；# 开头的行会被忽略。\n"
            "• 扫描只读，不会修改 TXT 内容。"
        )
        ttk.Label(tips, text=tip_text, style="Hint.TLabel",
                  justify="left", wraplength=440).grid(row=0, column=0, sticky="w")

        actions = tk.Frame(right, bg=BG)
        actions.grid(row=0, column=0, sticky="ew", pady=(8, 4))
        action_row1 = tk.Frame(actions, bg=BG)
        action_row1.pack(fill="x")
        self.check_start_btn = ttk.Button(action_row1, text="开始检测", style="Accent.TButton",
                                          command=self.start_check)
        self.check_start_btn.pack(side="left")
        self.check_cancel_btn = ttk.Button(action_row1, text="停止", style="Secondary.TButton",
                                           state="disabled", command=self.cancel_current)
        self.check_cancel_btn.pack(side="left", padx=8)
        self.check_open_btn = ttk.Button(action_row1, text="打开报告", style="Secondary.TButton",
                                         state="disabled", command=self.open_check_report)
        self.check_open_btn.pack(side="left")
        self.check_process_btn = ttk.Button(action_row1, text="隔离达标文件", style="Accent.TButton",
                                            state="disabled", command=self.process_hits)
        self.check_process_btn.pack(side="right")
        action_row2 = tk.Frame(actions, bg=BG)
        action_row2.pack(fill="x", pady=(5, 0))
        tk.Label(action_row2, text="达到阈值的文件默认移入隔离区，可手动恢复。",
                 bg=BG, fg=MUTED, font=(self.font_family, 9)).pack(side="left")
        self.check_quarantine_btn = ttk.Button(action_row2, text="打开隔离区",
                                               style="Secondary.TButton",
                                               state="disabled", command=self.open_quarantine)
        self.check_quarantine_btn.pack(side="right")

        progress_line = tk.Frame(right, bg=BG)
        progress_line.grid(row=1, column=0, sticky="ew", pady=(2, 7))
        progress_line.columnconfigure(0, weight=1)
        self.check_progress = ttk.Progressbar(progress_line, maximum=100,
                                              style="Accent.Horizontal.TProgressbar")
        self.check_progress.grid(row=0, column=0, sticky="ew")
        self.check_status_var = tk.StringVar(value="等待开始")
        tk.Label(progress_line, textvariable=self.check_status_var, bg=BG, fg=MUTED,
                 font=(self.font_family, 9), anchor="e", width=34).grid(
            row=0, column=1, sticky="e", padx=(12, 0))

        stats = self._make_stats(right)
        stats.grid(row=2, column=0, sticky="ew", pady=(8, 8))
        self.check_stat_scanned = tk.StringVar(value="0")
        self.check_stat_matched = tk.StringVar(value="0")
        self.check_stat_hits = tk.StringVar(value="0")
        self.check_stat_candidates = tk.StringVar(value="0")
        self._stat_card(stats, 0, "扫描 TXT", self.check_stat_scanned)
        self._stat_card(stats, 1, "命中文件", self.check_stat_matched)
        self._stat_card(stats, 2, "命中次数", self.check_stat_hits)
        self._stat_card(stats, 3, "达到阈值", self.check_stat_candidates)

        results = self._make_card(right, "2. 命中结果")
        results.grid(row=3, column=0, sticky="nsew", pady=(0, 8))
        right.rowconfigure(3, weight=1)
        right.columnconfigure(0, weight=1)
        results.rowconfigure(0, weight=1)
        results.columnconfigure(0, weight=1)
        tree_frame = tk.Frame(results, bg=CARD)
        tree_frame.grid(row=0, column=0, sticky="nsew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self.check_tree = ttk.Treeview(
            tree_frame,
            columns=("file", "word", "count", "lines"),
            show="headings", selectmode="browse", height=4,
        )
        self.check_tree.heading("file", text="文件名")
        self.check_tree.heading("word", text="违禁词")
        self.check_tree.heading("count", text="次数")
        self.check_tree.heading("lines", text="行号（最多显示前 20 个）")
        self.check_tree.column("file", width=180, minwidth=120, stretch=True, anchor="w")
        self.check_tree.column("word", width=90, minwidth=70, stretch=False)
        self.check_tree.column("count", width=45, minwidth=40, stretch=False,
                               anchor="center")
        self.check_tree.column("lines", width=120, minwidth=100, stretch=True, anchor="w")
        self.check_tree.grid(row=0, column=0, sticky="nsew")
        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.check_tree.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.check_tree.xview)
        hsb.grid(row=1, column=0, sticky="ew")
        self.check_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.check_tree.tag_configure("odd", background="#fbfdff")
        self.check_tree.tag_configure("even", background="#ffffff")
        self.check_tree.bind("<<TreeviewSelect>>", self._show_selected_detail)
        self.check_detail_var = tk.StringVar(value="选中一行可查看完整路径与行号。")
        tk.Label(results, textvariable=self.check_detail_var, bg=CARD, fg=MUTED,
                 font=(self.font_family, 9), anchor="w").grid(
            row=1, column=0, sticky="ew", pady=(5, 0))

        log_card = self._make_card(right, "检测日志")
        log_card.grid(row=4, column=0, sticky="ew")
        log_card.columnconfigure(0, weight=1)
        self.check_log = self._make_log(log_card, height=2)
        self.check_log.grid(row=0, column=0, sticky="ew")

    def _build_video_tab(self) -> None:
        tab = self.video_tab
        tab.columnconfigure(0, weight=3)
        tab.columnconfigure(1, weight=2)
        tab.rowconfigure(0, weight=1)

        left = tk.Frame(tab, bg=BG)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        right = tk.Frame(tab, bg=BG)
        right.grid(row=0, column=1, sticky="nsew")

        source = self._make_card(left, "1. 选择视频库")
        source.grid(row=0, column=0, sticky="ew", pady=(8, 8))
        source.columnconfigure(1, weight=1)

        ttk.Label(source, text="视频根目录", style="Card.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 10), pady=4)
        self.video_root_var = tk.StringVar()
        ttk.Entry(source, textvariable=self.video_root_var).grid(
            row=0, column=1, sticky="ew", padx=(0, 8), pady=4)
        ttk.Button(source, text="浏览…", style="Secondary.TButton",
                   command=self._browse_video_root).grid(row=0, column=2, pady=4)
        ttk.Label(source,
                  text="扫描该目录下的 .mp4 视频；默认包含子文件夹。不会拼接视频。",
                  style="Hint.TLabel", wraplength=420).grid(
            row=1, column=1, columnspan=2, sticky="w", pady=(0, 7))

        ttk.Label(source, text="输出文件夹", style="Card.TLabel").grid(
            row=2, column=0, sticky="w", padx=(0, 10), pady=4)
        self.video_output_var = tk.StringVar()
        ttk.Entry(source, textvariable=self.video_output_var).grid(
            row=2, column=1, sticky="ew", padx=(0, 8), pady=4)
        ttk.Button(source, text="浏览…", style="Secondary.TButton",
                   command=self._browse_video_output).grid(row=2, column=2, pady=4)

        output_info = tk.Frame(source, bg=SOFT, highlightbackground="#dbeafe",
                               highlightthickness=1)
        output_info.grid(row=3, column=1, columnspan=2, sticky="ew", pady=(2, 3))
        self.video_output_info_var = tk.StringVar()
        tk.Label(output_info, text="输出到", bg=SOFT, fg=ACCENT,
                 font=(self.font_family, 9, "bold"), padx=9, pady=3).pack(side="left")
        tk.Label(output_info, textvariable=self.video_output_info_var, bg=SOFT,
                 fg=TEXT, font=(self.font_family, 9), anchor="w",
                 justify="left", wraplength=430).pack(
            side="left", fill="x", expand=True, padx=(0, 9), pady=3)

        report_info = tk.Frame(source, bg=SOFT, highlightbackground="#dbeafe",
                               highlightthickness=1)
        report_info.grid(row=4, column=1, columnspan=2, sticky="ew", pady=(0, 5))
        self.video_report_info_var = tk.StringVar()
        tk.Label(report_info, text="检测报告", bg=SOFT, fg=ACCENT,
                 font=(self.font_family, 9, "bold"), padx=9, pady=3).pack(side="left")
        tk.Label(report_info, textvariable=self.video_report_info_var, bg=SOFT,
                 fg=TEXT, font=(self.font_family, 9), anchor="w",
                 justify="left", wraplength=430).pack(
            side="left", fill="x", expand=True, padx=(0, 9), pady=3)

        self.video_recursive_var = tk.BooleanVar(value=True)
        self.video_overwrite_var = tk.BooleanVar(value=False)
        self.video_hash_var = tk.BooleanVar(value=True)
        checks = ttk.Frame(source, style="Card.TFrame")
        checks.grid(row=5, column=1, columnspan=2, sticky="w", pady=(3, 0))
        ttk.Checkbutton(checks, text="包含子文件夹", variable=self.video_recursive_var,
                        style="Card.TCheckbutton").pack(side="left", padx=(0, 18))
        ttk.Checkbutton(checks, text="完整哈希校验", variable=self.video_hash_var,
                        style="Card.TCheckbutton").pack(side="left", padx=(0, 18))
        ttk.Checkbutton(checks, text="覆盖已有输出", variable=self.video_overwrite_var,
                        style="Card.TCheckbutton").pack(side="left")

        self._option_details(
            source,
            [
                ("包含子文件夹", "勾选后扫描视频根目录下所有层级；不勾选只扫描第一层。"),
                ("完整哈希校验", "只对大小相同的视频计算 SHA-256；关闭可加快速度，但精确判重会减少。"),
                ("覆盖已有输出", "输出文件夹已存在时先清空再生成；不勾选会先弹窗询问。"),
            ],
            row=6, columnspan=2, wraplength=340,
        )

        rules = self._make_card(left, "2. 识别规则")
        rules.grid(row=1, column=0, sticky="ew")
        rule_text = (
            "• 同名、同时长、同画幅的视频视为同一部，保留分辨率最高的一份。\n"
            "• 同分辨率时保留体积更大、文件名更干净的版本。\n"
            "• 第 1 集 / 第 2 集、正片 / 预告片不会互相合并。\n"
            "• 没有视频元数据时只按完整哈希判重，宁可少合并，不误删。"
        )
        ttk.Label(rules, text=rule_text, style="Hint.TLabel",
                  justify="left", wraplength=480).grid(row=0, column=0, sticky="w")

        tips = self._make_card(left, "3. 输出说明")
        tips.grid(row=2, column=0, sticky="ew")
        ttk.Label(
            tips,
            text=("• 输出为“视频合并结果”，内含保留的视频、CSV 报告和摘要。\n"
                  "• 检测报告单独保存为“视频检测报告.csv”，预览也会生成。\n"
                  "• 主动隔离会把重复视频移入隔离区，不永久删除。"),
            style="Hint.TLabel", justify="left", wraplength=480,
        ).grid(row=0, column=0, sticky="w")

        actions = tk.Frame(right, bg=BG)
        actions.grid(row=0, column=0, sticky="ew", pady=(8, 4))
        row1 = tk.Frame(actions, bg=BG)
        row1.pack(fill="x")
        self.video_start_btn = ttk.Button(row1, text="开始去重", style="Accent.TButton",
                                          command=lambda: self.start_video(False))
        self.video_start_btn.pack(side="left")
        self.video_preview_btn = ttk.Button(row1, text="仅预览", style="Secondary.TButton",
                                            command=lambda: self.start_video(True))
        self.video_preview_btn.pack(side="left", padx=8)
        self.video_cancel_btn = ttk.Button(row1, text="停止", style="Secondary.TButton",
                                           state="disabled", command=self.cancel_current)
        self.video_cancel_btn.pack(side="left")
        row2 = tk.Frame(actions, bg=BG)
        row2.pack(fill="x", pady=(5, 0))
        tk.Label(row2, text="检测报告与去重报告会保存为 CSV。",
                 bg=BG, fg=MUTED, font=(self.font_family, 9)).pack(side="left")
        self.video_report_btn = ttk.Button(row2, text="打开报告", style="Secondary.TButton",
                                           state="disabled", command=self.open_video_report)
        self.video_report_btn.pack(side="right")
        self.video_open_btn = ttk.Button(row2, text="打开输出", style="Secondary.TButton",
                                         state="disabled", command=self.open_video_output)
        self.video_open_btn.pack(side="right", padx=8)
        row3 = tk.Frame(actions, bg=BG)
        row3.pack(fill="x", pady=(5, 0))
        self.video_view_btn = ttk.Button(row3, text="查看检测报告", style="Secondary.TButton",
                                         state="disabled", command=self.show_video_report)
        self.video_view_btn.pack(side="left")
        self.video_rename_btn = ttk.Button(row3, text="批量清理文件名",
                                           style="Secondary.TButton",
                                           command=self.show_rename_dialog)
        self.video_rename_btn.pack(side="left", padx=8)
        self.video_quarantine_btn = ttk.Button(row3, text="隔离重复视频", style="Danger.TButton",
                                               state="disabled", command=self.quarantine_video_duplicates)
        self.video_quarantine_btn.pack(side="right")
        self.video_quarantine_open_btn = ttk.Button(row3, text="打开隔离区",
                                                    style="Secondary.TButton",
                                                    state="disabled", command=self.open_video_quarantine)
        self.video_quarantine_open_btn.pack(side="right", padx=8)

        progress_line = tk.Frame(right, bg=BG)
        progress_line.grid(row=1, column=0, sticky="ew", pady=(2, 7))
        progress_line.columnconfigure(0, weight=1)
        self.video_progress = ttk.Progressbar(progress_line, maximum=100,
                                              style="Accent.Horizontal.TProgressbar")
        self.video_progress.grid(row=0, column=0, sticky="ew")
        self.video_status_var = tk.StringVar(value="等待开始")
        tk.Label(progress_line, textvariable=self.video_status_var, bg=BG, fg=MUTED,
                 font=(self.font_family, 9), anchor="e", width=34).grid(
            row=0, column=1, sticky="e", padx=(12, 0))

        stats = self._make_stats(right)
        stats.grid(row=2, column=0, sticky="ew", pady=(8, 8))
        self.video_stat_scanned = tk.StringVar(value="0")
        self.video_stat_kept = tk.StringVar(value="0")
        self.video_stat_merged = tk.StringVar(value="0")
        self.video_stat_time = tk.StringVar(value="0")
        self._stat_card(stats, 0, "扫描 MP4", self.video_stat_scanned)
        self._stat_card(stats, 1, "最终保留", self.video_stat_kept)
        self._stat_card(stats, 2, "合并重复", self.video_stat_merged)
        self._stat_card(stats, 3, "耗时", self.video_stat_time)

        log_card = self._make_card(right, "视频处理日志")
        log_card.grid(row=3, column=0, sticky="nsew", pady=(0, 8))
        right.rowconfigure(3, weight=1)
        right.columnconfigure(0, weight=1)
        log_card.rowconfigure(0, weight=1)
        log_card.columnconfigure(0, weight=1)
        self.video_log = self._make_log(log_card, height=10)
        self.video_log.grid(row=0, column=0, sticky="nsew")


    def _apply_defaults(self) -> None:
        paths = self.settings.get("paths", {})
        values = [
            (self.merge_root_var, "merge_root"),
            (self.merge_output_var, "merge_output"),
            (self.check_target_var, "check_target"),
            (self.check_words_var, "check_words"),
            (self.check_report_var, "check_report"),
            (self.video_root_var, "video_root"),
            (self.video_output_var, "video_output"),
        ]
        for variable, key in values:
            variable.set(str(paths.get(key, "") or ""))
        self.video_report_var = str(paths.get("video_report", "") or "")
        for variable in (self.merge_output_var, self.check_report_var, self.video_output_var):
            variable.trace_add("write", lambda *_args: self._update_output_infos())
        self._update_output_infos()

    def _update_output_infos(self) -> None:
        def shown(variable: tk.StringVar) -> str:
            value = variable.get().strip()
            return value if value else "（未选择）"

        self.merge_output_info_var.set(shown(self.merge_output_var))
        self.check_report_info_var.set(shown(self.check_report_var))
        self.video_output_info_var.set(shown(self.video_output_var))
        self.video_report_info_var.set(self.video_report_var or "（检测时生成）")
        if hasattr(self, "standard_words_info_var"):
            self.standard_words_info_var.set(self._standard_words_summary())

    def _standard_words_summary(self) -> str:
        path = self.standard_words_path
        try:
            count = sum(
                1 for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            )
            return f"{path.name} · {count} 条 · 可现场修改"
        except OSError:
            return "标准词库暂不可读"

    def _save_settings(self) -> None:
        paths = {
            "merge_root": self.merge_root_var.get().strip(),
            "merge_output": self.merge_output_var.get().strip(),
            "check_target": self.check_target_var.get().strip(),
            "check_words": self.check_words_var.get().strip(),
            "check_report": self.check_report_var.get().strip(),
            "video_root": self.video_root_var.get().strip(),
            "video_output": self.video_output_var.get().strip(),
            "video_report": str(self.video_report_var or "").strip(),
        }
        data = dict(self.settings)
        data.update({
            "paths": paths,
            "favorites": self.favorites,
            "recent": self.recent,
            "sidebar_collapsed": self.sidebar_collapsed,
        })
        try:
            if self.root.state() == "normal":
                data["geometry"] = self.root.geometry()
        except tk.TclError:
            pass
        save_settings(data)
        self.settings = data

    def _abs_path(self, variable: tk.StringVar) -> Path:
        text = variable.get().strip().strip('"')
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = BASE_DIR / path
        return path.resolve()

    def _dialog_start_dir(self, value: str) -> str:
        value = value.strip()
        if value:
            path = Path(value)
            return str(path if path.is_dir() else path.parent)
        return str(Path.home())

    def _browse_merge_root(self) -> None:
        selected = filedialog.askdirectory(
            parent=self.root, title="选择小说根目录",
            initialdir=self._dialog_start_dir(self.merge_root_var.get()),
        )
        if not selected:
            return
        old_output = self.merge_output_var.get().strip()
        self.merge_root_var.set(selected)
        if not old_output:
            self.merge_output_var.set(str(Path(selected) / nm.OUTPUT_DIR_DEFAULT))
        self._save_settings()

    def _browse_merge_output(self) -> None:
        selected = filedialog.askdirectory(
            parent=self.root, title="选择输出文件夹",
            initialdir=self._dialog_start_dir(self.merge_output_var.get()),
        )
        if selected:
            self.merge_output_var.set(selected)
            self._save_settings()

    def _browse_check_folder(self) -> None:
        selected = filedialog.askdirectory(
            parent=self.root, title="选择检测文件夹",
            initialdir=self._dialog_start_dir(self.check_target_var.get()),
        )
        if selected:
            self.check_target_var.set(selected)
            if not self.check_report_var.get().strip():
                self.check_report_var.set(str(Path(selected) / wc.DEFAULT_REPORT_FILE))
            self._save_settings()

    def _browse_video_root(self) -> None:
        selected = filedialog.askdirectory(
            parent=self.root, title="选择视频根目录",
            initialdir=self._dialog_start_dir(self.video_root_var.get()),
        )
        if not selected:
            return
        old_output = self.video_output_var.get().strip()
        self.video_root_var.set(selected)
        if not old_output:
            self.video_output_var.set(str(Path(selected) / vm.DEFAULT_OUTPUT_DIR))
        self._save_settings()

    def _browse_video_output(self) -> None:
        old_output = self.video_output_var.get().strip()
        selected = filedialog.askdirectory(
            parent=self.root, title="选择视频输出文件夹",
            initialdir=self._dialog_start_dir(old_output),
        )
        if selected:
            self.video_output_var.set(selected)
            report = str(self.video_report_var or "").strip()
            if not report or (old_output and Path(report).parent == Path(old_output)):
                self.video_report_var = str(Path(selected) / "视频检测报告.csv")
                self._update_output_infos()
            self._save_settings()

    def _browse_check_file(self) -> None:
        selected = filedialog.askopenfilename(
            parent=self.root, title="选择要检测的 TXT",
            initialdir=self._dialog_start_dir(self.check_target_var.get()),
            filetypes=[("TXT 文件", "*.txt"), ("所有文件", "*.*")],
        )
        if selected:
            self.check_target_var.set(selected)
            if not self.check_report_var.get().strip():
                self.check_report_var.set(str(Path(selected).parent / wc.DEFAULT_REPORT_FILE))
            self._save_settings()

    def _browse_words(self) -> None:
        selected = filedialog.askopenfilename(
            parent=self.root, title="选择违禁词库",
            initialdir=self._dialog_start_dir(self.check_words_var.get()),
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
        )
        if selected:
            self.check_words_var.set(selected)
            self._save_settings()

    def _browse_report(self) -> None:
        current = self.check_report_var.get().strip()
        selected = filedialog.asksaveasfilename(
            parent=self.root, title="保存检测报告",
            initialdir=self._dialog_start_dir(current),
            initialfile=Path(current).name if current else wc.DEFAULT_REPORT_FILE,
            defaultextension=".csv",
            filetypes=[("CSV 文件", "*.csv")],
        )
        if selected:
            self.check_report_var.set(selected)
            self._save_settings()

    def _open_words(self) -> None:
        if not self.check_words_var.get().strip():
            self.show_standard_words_editor()
            return
        try:
            path = self._abs_path(self.check_words_var)
            if not path.exists():
                messagebox.showwarning("词库不存在", f"找不到词库文件：\n{path}", parent=self.root)
                return
            open_path(path)
        except Exception as exc:
            messagebox.showerror("打开词库失败", str(exc), parent=self.root)

    def show_standard_words_editor(self) -> None:
        if self.standard_words_window and self.standard_words_window.winfo_exists():
            self.standard_words_window.lift()
            self.standard_words_window.focus_force()
            return
        path = self.standard_words_path
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("读取失败", f"无法读取标准词库：\n{exc}", parent=self.root)
            return

        win = tk.Toplevel(self.root)
        self.standard_words_window = win
        win.title("编辑标准违禁词 · 小工作箱")
        win.configure(bg=BG)
        win.geometry("920x680")
        win.minsize(720, 500)
        win.transient(self.root)

        head = tk.Frame(win, bg=BG)
        head.pack(fill="x", padx=18, pady=(14, 6))
        tk.Label(head, text="编辑标准违禁词", bg=BG, fg=TEXT,
                 font=(self.font_family, 18, "bold")).pack(anchor="w")
        tk.Label(
            head, text="每行一个词；空行和 # 开头的注释会被忽略。保存后立即生效。",
            bg=BG, fg=MUTED, font=(self.font_family, 9),
        ).pack(anchor="w", pady=(2, 0))
        tk.Label(
            head, text=str(path), bg=BG, fg=ACCENT,
            font=(self.font_family, 8), anchor="w", wraplength=850,
        ).pack(anchor="w", pady=(3, 0))

        card = self._make_card(win, "标准词库内容")
        card.pack(fill="both", expand=True, padx=18, pady=(4, 8))
        card.rowconfigure(0, weight=1)
        card.columnconfigure(0, weight=1)
        editor = ScrolledText(
            card, wrap="word", bg="#0f172a", fg="#e2e8f0",
            insertbackground="#e2e8f0", relief="flat", borderwidth=0,
            highlightthickness=1, highlightbackground="#334155",
            font=("Microsoft YaHei UI", 10), padx=12, pady=10,
        )
        editor.grid(row=0, column=0, sticky="nsew")
        editor.insert("1.0", content)

        actions = tk.Frame(win, bg=BG)
        actions.pack(fill="x", padx=18, pady=(0, 14))
        status = tk.StringVar(value="")
        save_btn = ttk.Button(actions, text="保存", style="Accent.TButton")
        save_btn.pack(side="left")
        use_btn = ttk.Button(actions, text="使用这个标准词库", style="Secondary.TButton")
        use_btn.pack(side="left", padx=8)
        restore_btn = ttk.Button(actions, text="恢复内置默认", style="Secondary.TButton")
        restore_btn.pack(side="left")
        folder_btn = ttk.Button(actions, text="打开所在文件夹", style="Secondary.TButton")
        folder_btn.pack(side="left", padx=8)
        ttk.Button(actions, text="关闭", style="Secondary.TButton",
                   command=win.destroy).pack(side="right")
        tk.Label(actions, textvariable=status, bg=BG, fg=MUTED,
                 font=(self.font_family, 9)).pack(side="right", padx=(0, 12))
        self.standard_words_editor = editor
        self.standard_words_status_var = status
        self.standard_save_btn = save_btn
        self.standard_use_btn = use_btn
        self.standard_restore_btn = restore_btn

        def save() -> None:
            text = editor.get("1.0", "end-1c").rstrip() + "\n"
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            except OSError as exc:
                messagebox.showerror("保存失败", str(exc), parent=win)
                return
            current = self.check_words_var.get().strip()
            if not current or Path(current) == path:
                self.check_words_var.set(str(path))
            self.standard_words_path = path
            self._update_output_infos()
            self._save_settings()
            count = sum(
                1 for line in text.splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            )
            status.set(f"已保存 · {count} 条")
            messagebox.showinfo("已保存", f"标准违禁词已保存：\n{path}\n\n共 {count} 条。",
                                parent=win)

        def use_standard() -> None:
            self.check_words_var.set(str(path))
            self._update_output_infos()
            self._save_settings()
            status.set("已将标准词库设为当前词库")

        def restore_default() -> None:
            source = RESOURCE_DIR / "标准违禁词.txt"
            if not source.exists():
                source = RESOURCE_DIR / "违禁词.txt"
            if not source.exists():
                messagebox.showwarning("没有内置词库", "找不到内置标准词库。", parent=win)
                return
            if not messagebox.askyesno(
                "恢复内置默认", "会用内置标准词库覆盖编辑框内容。\n确定吗？",
                parent=win, default="no", icon="warning",
            ):
                return
            try:
                editor.delete("1.0", "end")
                editor.insert("1.0", source.read_text(encoding="utf-8"))
            except OSError as exc:
                messagebox.showerror("读取失败", str(exc), parent=win)
                return
            status.set("已载入内置默认，点“保存”后生效")

        def open_folder() -> None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                open_path(path.parent)
            except Exception as exc:
                messagebox.showerror("打开失败", str(exc), parent=win)

        save_btn.configure(command=save)
        use_btn.configure(command=use_standard)
        restore_btn.configure(command=restore_default)
        folder_btn.configure(command=open_folder)
        editor.bind("<Control-s>", lambda _e: save())
        win.bind("<Escape>", lambda _e: win.destroy())

        def closed() -> None:
            self.standard_words_window = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", closed)

    def _on_threshold_change(self, value: str) -> None:
        try:
            self.merge_threshold_label.set(f"{float(value):.2f}")
        except ValueError:
            pass

    # ------------------------------------------------------------------
    # 任务线程
    # ------------------------------------------------------------------
    def _emit(self, kind: str, payload: object) -> None:
        self.events.put((kind, payload))

    def _run_job(self, job) -> None:
        try:
            result = job(self._emit)
        except nm.OperationCancelled:
            self._emit("cancelled", None)
        except Exception as exc:  # noqa: BLE001
            self._emit("error", (exc, traceback.format_exc()))
        else:
            self._emit("done", result)

    def _start_task(self, tab: str, job, done_callback) -> None:
        if self.busy:
            return
        self.busy = True
        self.active_tab = tab
        self.cancel_event.clear()
        self._done_callback = done_callback
        self._set_busy(tab)
        self.footer_var.set("任务运行中…")
        threading.Thread(target=self._run_job, args=(job,), daemon=True).start()

    def _poll_events(self) -> None:
        if self._closing:
            return
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "progress":
                    self._handle_progress(payload)  # type: ignore[arg-type]
                elif kind == "done":
                    self.busy = False
                    self.active_tab = None
                    callback = self._done_callback
                    self._done_callback = None
                    if callback:
                        callback(payload)
                    self._set_busy(None)
                    self.footer_var.set("就绪")
                elif kind == "cancelled":
                    tab = self.active_tab
                    self.busy = False
                    self.active_tab = None
                    self._done_callback = None
                    self._set_busy(None)
                    self.footer_var.set("已停止")
                    if tab == "check":
                        self.check_status_var.set("已停止")
                        self._log(self.check_log, "任务已停止。", "warn")
                    elif tab == "video":
                        self.video_status_var.set("已停止")
                        self._log(self.video_log, "任务已停止。", "warn")
                    else:
                        self.merge_status_var.set("已停止")
                        self._log(self.merge_log, "任务已停止。", "warn")
                elif kind == "error":
                    exc, tb = payload  # type: ignore[misc]
                    tab = self.active_tab
                    self.busy = False
                    self.active_tab = None
                    self._done_callback = None
                    self._set_busy(None)
                    self.footer_var.set("任务失败")
                    if tab == "check":
                        self.check_status_var.set("任务失败")
                        self._log(self.check_log, "错误：" + str(exc), "error")
                        if tb:
                            self._log(self.check_log, str(tb).strip().splitlines()[-1], "error")
                    elif tab == "video":
                        self.video_status_var.set("任务失败")
                        self._log(self.video_log, "错误：" + str(exc), "error")
                        if tb:
                            self._log(self.video_log, str(tb).strip().splitlines()[-1], "error")
                    else:
                        self.merge_status_var.set("任务失败")
                        self._log(self.merge_log, "错误：" + str(exc), "error")
                        if tb:
                            self._log(self.merge_log, str(tb).strip().splitlines()[-1], "error")
                    messagebox.showerror("任务失败", str(exc), parent=self.root)
        except queue.Empty:
            pass
        self.root.after(80, self._poll_events)

    def _handle_progress(self, payload: dict) -> None:
        tab = payload.get("tab")
        phase = payload.get("phase", "")
        fraction = float(payload.get("fraction", 0.0))
        message = str(payload.get("message", ""))
        if tab == "merge":
            self.merge_progress.configure(value=fraction * 100)
            self.merge_status_var.set(shorten(message, 64))
            bucket = int(fraction * 20)
            if phase != self._merge_phase:
                self._merge_phase = phase
                self._merge_bucket = bucket
                label = {"scan": "扫描文件", "compare": "比较内容",
                         "write": "写出结果", "done": "完成"}.get(phase, phase)
                self._log(self.merge_log, f"▶ {label}", "phase")
            elif bucket != self._merge_bucket:
                self._merge_bucket = bucket
                self._log(self.merge_log, f"  {message}", "info")
        elif tab == "check":
            self.check_progress.configure(value=fraction * 100)
            self.check_status_var.set(shorten(message, 64))
            bucket = int(fraction * 20)
            if phase == "process":
                if bucket != self._check_bucket:
                    self._check_bucket = bucket
                    self._log(self.check_log, f"  {message}", "info")
            elif bucket != self._check_bucket:
                self._check_bucket = bucket
                self._log(self.check_log, f"  已扫描 {message}", "info")
        elif tab == "video":
            self.video_progress.configure(value=fraction * 100)
            self.video_status_var.set(shorten(message, 64))
            bucket = int(fraction * 20)
            if phase != self._video_phase:
                self._video_phase = phase
                self._video_bucket = bucket
                label = {
                    "scan": "扫描视频", "hash": "完整哈希校验",
                    "compare": "比较标题/时长/分辨率", "write": "复制保留视频",
                    "quarantine": "移入视频隔离区", "done": "完成",
                }.get(phase, phase)
                self._log(self.video_log, f"▶ {label}", "phase")
            elif bucket != self._video_bucket:
                self._video_bucket = bucket
                self._log(self.video_log, f"  {message}", "info")

    def _set_busy(self, active: str | None) -> None:
        self.busy = active is not None
        state = "disabled" if active else "normal"
        self.merge_start_btn.configure(state=state)
        self.merge_preview_btn.configure(state=state)
        self.check_start_btn.configure(state=state)
        self.video_start_btn.configure(state=state)
        self.video_preview_btn.configure(state=state)
        self.video_rename_btn.configure(state=state)
        self.merge_cancel_btn.configure(state="normal" if active == "merge" else "disabled")
        self.check_cancel_btn.configure(state="normal" if active == "check" else "disabled")
        self.video_cancel_btn.configure(state="normal" if active == "video" else "disabled")
        if active == "video":
            for button in (self.video_report_btn, self.video_open_btn,
                           self.video_view_btn, self.video_quarantine_btn,
                           self.video_quarantine_open_btn):
                button.configure(state="disabled")
        else:
            self.video_open_btn.configure(
                state="normal" if self.last_video_output and self.last_video_output.exists() else "disabled")
            self.video_report_btn.configure(
                state="normal" if self.last_video_report and self.last_video_report.exists() else "disabled")
            self._update_video_action_buttons()
        if active == "merge":
            self.merge_check_btn.configure(state="disabled")
            self.check_process_btn.configure(state="disabled")
        elif active == "check":
            self.check_process_btn.configure(state="disabled")
            self.merge_check_btn.configure(
                state="normal" if self.last_output and self.last_output.exists() else "disabled")
        else:
            self.merge_check_btn.configure(
                state="normal" if self.last_output and self.last_output.exists() else "disabled")
            self._update_process_button()

    def _update_video_action_buttons(self) -> None:
        has_outcome = self.video_outcome is not None
        self.video_view_btn.configure(state="normal" if has_outcome else "disabled")
        duplicates = 0
        if self.video_outcome:
            duplicates = len(self.video_outcome.videos) - len(self.video_outcome.groups)
        self.video_quarantine_btn.configure(
            state="normal" if duplicates > 0 and not self.video_quarantined else "disabled")
        self.video_quarantine_open_btn.configure(
            state="normal" if self.last_video_quarantine and self.last_video_quarantine.exists()
            else "disabled")

    def _status_for_active(self, text: str) -> None:
        if self.active_tab == "merge" or (self.active_tab is None and self.merge_progress["value"] > 0):
            self.merge_status_var.set(text)
        if self.active_tab == "check" or (self.active_tab is None and self.check_progress["value"] > 0):
            self.check_status_var.set(text)

    def _log_active(self, text: str, tag: str = "info") -> None:
        if self.active_tab == "check":
            self._log(self.check_log, text, tag)
        elif self.active_tab == "merge":
            self._log(self.merge_log, text, tag)
        else:
            self._log(self.merge_log, text, tag)

    def cancel_current(self) -> None:
        if not self.busy:
            return
        self.cancel_event.set()
        self.footer_var.set("正在停止…")
        if self.active_tab == "merge":
            self.merge_status_var.set("正在停止…")
            self.merge_cancel_btn.configure(state="disabled")
        elif self.active_tab == "check":
            self.check_status_var.set("正在停止…")
            self.check_cancel_btn.configure(state="disabled")
        elif self.active_tab == "video":
            self.video_status_var.set("正在停止…")
            self.video_cancel_btn.configure(state="disabled")

    # ------------------------------------------------------------------
    # 合并
    # ------------------------------------------------------------------
    def start_merge(self, dry_run: bool) -> None:
        if self.busy:
            return
        if not self.merge_root_var.get().strip():
            messagebox.showinfo("请选择目录", "请先选择小说根目录。", parent=self.root)
            return
        if not self.merge_output_var.get().strip():
            messagebox.showinfo("请选择输出目录", "请先选择输出文件夹。", parent=self.root)
            return
        try:
            root = self._abs_path(self.merge_root_var)
            output = self._abs_path(self.merge_output_var)
            threshold = float(self.merge_threshold_var.get())
        except (ValueError, OSError) as exc:
            messagebox.showerror("参数有误", str(exc), parent=self.root)
            return
        self._save_settings()
        if not root.is_dir():
            messagebox.showerror("目录不存在", f"小说根目录不存在：\n{root}", parent=self.root)
            return
        overwrite = self.merge_overwrite_var.get()
        if output.exists() and not overwrite and not dry_run:
            answer = messagebox.askyesno(
                "输出文件夹已存在",
                f"输出文件夹已存在：\n{output}\n\n是否清空并重新生成？\n（只会删除这个输出文件夹）",
                parent=self.root, default="no", icon="warning",
            )
            if not answer:
                return
            overwrite = True
            self.merge_overwrite_var.set(True)

        self._clear_log(self.merge_log)
        self._log(self.merge_log, f"开始{'预览' if dry_run else '合并'}：{root}", "phase")
        self._log(self.merge_log, f"输出：{output}", "info")
        self.merge_progress.configure(value=0)
        self.merge_status_var.set("准备中…")
        self.merge_stat_scanned.set("—")
        self.merge_stat_kept.set("—")
        self.merge_stat_merged.set("—")
        self.merge_stat_time.set("—")
        self.merge_open_btn.configure(state="disabled")
        self.merge_check_btn.configure(state="disabled")
        self.last_output = None
        self._merge_phase = ""
        self._merge_bucket = -1

        recursive = self.merge_recursive_var.get()

        def job(emit):
            def progress(phase: str, fraction: float, message: str) -> None:
                emit("progress", {"tab": "merge", "phase": phase,
                                  "fraction": fraction, "message": message})
            return nm.merge_library(
                root, output,
                recursive=recursive,
                overwrite=overwrite,
                threshold=threshold,
                dry_run=dry_run,
                progress=progress,
                cancel=self.cancel_event.is_set,
            )

        self._start_task("merge", job, self._merge_done)

    def _merge_done(self, outcome: nm.MergeOutcome) -> None:
        merged = sum(len(group.books) - 1 for group in outcome.groups)
        self.merge_stat_scanned.set(f"{outcome.stats.scanned} 个")
        self.merge_stat_kept.set(f"{len(outcome.groups)} 本")
        self.merge_stat_merged.set(f"{merged} 个")
        self.merge_stat_time.set(f"{outcome.elapsed:.1f} 秒")
        self.merge_progress.configure(value=100)
        self.merge_status_var.set("预览完成" if outcome.dry_run else "合并完成")
        self._log(self.merge_log,
                  f"完成：扫描 {outcome.stats.scanned} 个，保留 {len(outcome.groups)} 本，"
                  f"合并 {merged} 个，同名改名 {outcome.renamed} 个。", "ok")
        if outcome.stats.read_errors:
            self._log(self.merge_log,
                      f"读取失败：{len(outcome.stats.read_errors)} 个。", "warn")
        if outcome.stats.ignored:
            self._log(self.merge_log,
                      f"跳过空文件/过短文件：{len(outcome.stats.ignored)} 个。", "warn")
        if not outcome.dry_run:
            self.last_output = outcome.output
            self.merge_output_info_var.set(f"已输出到：{outcome.output}")
            self.merge_open_btn.configure(state="normal")
            self.merge_check_btn.configure(state="normal" if outcome.output.exists() else "disabled")
            self._log(self.merge_log, f"输出目录：{outcome.output}", "ok")
            messagebox.showinfo(
                "合并完成",
                f"扫描：{outcome.stats.scanned} 个\n"
                f"最终保留：{len(outcome.groups)} 本\n"
                f"合并重复：{merged} 个\n\n"
                f"输出：{outcome.output}",
                parent=self.root,
            )
        else:
            self._log(self.merge_log, "当前为预览模式，未写入文件。", "warn")

    def open_merge_output(self) -> None:
        path = self.last_output or self._abs_path(self.merge_output_var)
        try:
            open_path(path)
        except Exception as exc:
            messagebox.showerror("打开失败", str(exc), parent=self.root)

    def check_merge_output(self) -> None:
        if not self.last_output or not self.last_output.exists():
            messagebox.showwarning("还没有输出", "请先完成一次合并。", parent=self.root)
            return
        self.check_target_var.set(str(self.last_output))
        self.check_report_var.set(str(self.last_output / wc.DEFAULT_REPORT_FILE))
        self._save_settings()
        self.show_page("check")
        self.start_check()

    # ------------------------------------------------------------------
    # MP4 视频去重
    # ------------------------------------------------------------------
    def start_video(self, dry_run: bool) -> None:
        if self.busy:
            return
        if not self.video_root_var.get().strip():
            messagebox.showinfo("请选择目录", "请先选择视频根目录。", parent=self.root)
            return
        if not self.video_output_var.get().strip():
            messagebox.showinfo("请选择输出目录", "请先选择视频输出文件夹。", parent=self.root)
            return
        try:
            root = self._abs_path(self.video_root_var)
            output = self._abs_path(self.video_output_var)
        except (ValueError, OSError) as exc:
            messagebox.showerror("参数有误", str(exc), parent=self.root)
            return
        if not str(self.video_report_var or "").strip():
            self.video_report_var = str(output / "视频检测报告.csv")
            self._update_output_infos()
        self._save_settings()
        if not root.is_dir():
            messagebox.showerror("目录不存在", f"视频根目录不存在：\n{root}", parent=self.root)
            return
        overwrite = self.video_overwrite_var.get()
        if output.exists() and not overwrite and not dry_run:
            answer = messagebox.askyesno(
                "输出文件夹已存在",
                f"输出文件夹已存在：\n{output}\n\n是否清空并重新生成？\n"
                "（只会删除这个视频输出文件夹）",
                parent=self.root, default="no", icon="warning",
            )
            if not answer:
                return
            overwrite = True
            self.video_overwrite_var.set(True)

        self._clear_log(self.video_log)
        self._log(self.video_log, f"开始{'检测预览' if dry_run else '去重'}：{root}", "phase")
        self._log(self.video_log, f"输出：{output}", "info")
        self.video_progress.configure(value=0)
        self.video_status_var.set("准备中…")
        self.video_stat_scanned.set("—")
        self.video_stat_kept.set("—")
        self.video_stat_merged.set("—")
        self.video_stat_time.set("—")
        for button in (self.video_open_btn, self.video_report_btn,
                       self.video_view_btn, self.video_quarantine_btn,
                       self.video_quarantine_open_btn):
            button.configure(state="disabled")
        self.last_video_output = None
        self.last_video_report = None
        self.last_video_quarantine = None
        self.video_outcome = None
        self.video_quarantined = False
        self._video_phase = ""
        self._video_bucket = -1
        recursive = self.video_recursive_var.get()
        exact_hash = self.video_hash_var.get()
        report = Path(self.video_report_var)
        summary = report.with_name(report.stem + "_摘要.txt")

        def job(emit):
            def progress(phase: str, fraction: float, message: str) -> None:
                emit("progress", {"tab": "video", "phase": phase,
                                  "fraction": fraction, "message": message})
            return vm.merge_videos(
                root, output,
                recursive=recursive,
                overwrite=overwrite,
                exact_hash=exact_hash,
                dry_run=dry_run,
                report_file=report,
                summary_file=summary,
                progress=progress,
                cancel=self.cancel_event.is_set,
            )

        self._start_task("video", job, self._video_done)

    def _video_done(self, outcome: vm.VideoOutcome) -> None:
        duplicates = len(outcome.videos) - len(outcome.groups)
        self.video_outcome = outcome
        self.video_quarantined = False
        self.video_stat_scanned.set(f"{len(outcome.videos)} 个")
        self.video_stat_kept.set(f"{len(outcome.groups)} 个")
        self.video_stat_merged.set(f"{duplicates} 个")
        self.video_stat_time.set(f"{outcome.elapsed:.1f} 秒")
        self.video_progress.configure(value=100)
        self.video_status_var.set("检测完成" if outcome.dry_run else "去重完成")
        self.last_video_report = outcome.report_file
        self._log(self.video_log,
                  f"完成：扫描 {len(outcome.videos)} 个，保留 {len(outcome.groups)} 个，"
                  f"重复 {duplicates} 个。", "ok")
        if outcome.report_file:
            self._log(self.video_log, f"检测报告：{outcome.report_file}", "ok")
            self.video_report_btn.configure(
                state="normal" if outcome.report_file.exists() else "disabled")
        if outcome.skipped:
            self._log(self.video_log, f"跳过：{len(outcome.skipped)} 个。", "warn")
        self._update_video_action_buttons()
        if not outcome.dry_run:
            self.last_video_output = outcome.output
            self.video_output_info_var.set(f"已输出到：{outcome.output}")
            self.video_open_btn.configure(state="normal" if outcome.output.exists() else "disabled")
            self._log(self.video_log, f"输出目录：{outcome.output}", "ok")
            messagebox.showinfo(
                "MP4 去重完成",
                f"扫描：{len(outcome.videos)} 个\n"
                f"最终保留：{len(outcome.groups)} 个\n"
                f"重复：{duplicates} 个\n"
                f"复制数据：{outcome.copied_bytes / 1024 ** 3:.2f} GB\n\n"
                f"输出：{outcome.output}\n"
                f"报告：{outcome.report_file}",
                parent=self.root,
            )
        else:
            self._log(self.video_log, "当前为检测预览，未复制任何视频。", "warn")
            messagebox.showinfo(
                "MP4 检测完成",
                f"扫描：{len(outcome.videos)} 个\n"
                f"保留：{len(outcome.groups)} 个\n"
                f"重复：{duplicates} 个\n\n"
                f"报告：{outcome.report_file}\n\n"
                "可在“查看检测报告”中查看明细，或点“隔离重复视频”处理重复文件。",
                parent=self.root,
            )

    def open_video_output(self) -> None:
        path = self.last_video_output or self._abs_path(self.video_output_var)
        try:
            open_path(path)
        except Exception as exc:
            messagebox.showerror("打开失败", str(exc), parent=self.root)

    def open_video_report(self) -> None:
        if not self.last_video_report or not self.last_video_report.exists():
            messagebox.showinfo("还没有报告", "请先完成一次 MP4 检测。", parent=self.root)
            return
        try:
            open_path(self.last_video_report)
        except Exception as exc:
            messagebox.showerror("打开失败", str(exc), parent=self.root)

    def show_video_report(self) -> None:
        outcome = self.video_outcome
        if outcome is None:
            messagebox.showinfo("还没有报告", "请先进行一次 MP4 检测。", parent=self.root)
            return
        if self.video_report_window and self.video_report_window.winfo_exists():
            self.video_report_window.lift()
            self.video_report_window.focus_force()
            return

        win = tk.Toplevel(self.root)
        self.video_report_window = win
        win.title("MP4 检测报告 · 小工作箱")
        win.configure(bg=BG)
        win.geometry("1120x660")
        win.minsize(900, 520)
        win.transient(self.root)

        head = tk.Frame(win, bg=BG)
        head.pack(fill="x", padx=18, pady=(14, 6))
        tk.Label(head, text="MP4 检测报告", bg=BG, fg=TEXT,
                 font=(self.font_family, 18, "bold")).pack(anchor="w")
        repeats = len(outcome.videos) - len(outcome.groups)
        tk.Label(
            head,
            text=(f"扫描 {len(outcome.videos)} 个 · 保留 {len(outcome.groups)} 个 · "
                  f"重复 {repeats} 个 · 报告：{outcome.report_file}"),
            bg=BG, fg=MUTED, font=(self.font_family, 9), anchor="w",
            justify="left", wraplength=1050,
        ).pack(anchor="w", pady=(2, 0))

        table_card = self._make_card(win, "文件明细")
        table_card.pack(fill="both", expand=True, padx=18, pady=(4, 8))
        table_card.rowconfigure(0, weight=1)
        table_card.columnconfigure(0, weight=1)
        frame = tk.Frame(table_card, bg=CARD)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        tree = ttk.Treeview(
            frame,
            columns=("group", "status", "file", "resolution", "duration", "size", "similarity", "note"),
            show="headings",
        )
        headings = [
            ("group", "组", 50, 40), ("status", "状态", 70, 55),
            ("file", "文件名", 330, 200), ("resolution", "分辨率", 100, 80),
            ("duration", "时长", 80, 65), ("size", "大小", 90, 70),
            ("similarity", "相似度", 75, 60), ("note", "说明", 280, 160),
        ]
        for key, label, width, minwidth in headings:
            tree.heading(key, text=label)
            tree.column(key, width=width, minwidth=minwidth,
                        anchor="center" if key in {"group", "status", "resolution",
                                                   "duration", "size", "similarity"} else "w",
                        stretch=key in {"file", "note"})
        tree.grid(row=0, column=0, sticky="nsew")
        vsb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        tree.configure(yscrollcommand=vsb.set)
        tree.tag_configure("win", background="#f0fdf4")
        tree.tag_configure("dup", background="#fff7ed")
        for group_no, group in enumerate(outcome.groups, 1):
            for video in group.videos:
                if len(group.videos) == 1:
                    status, note, tag = "唯一", "无重复", "win"
                elif video is group.winner:
                    status, note, tag = "保留", group.reason, "win"
                else:
                    status, note, tag = "重复", "可选择移入隔离区", "dup"
                tree.insert(
                    "", "end",
                    values=(
                        group_no, status, video.path.name,
                        f"{video.meta.width}x{video.meta.height}"
                        if video.meta.width and video.meta.height else "未知",
                        vm._format_duration(video.meta.duration),
                        vm._format_size(video.size),
                        "100%" if video is group.winner else
                        f"{group.similarities.get(video.path, 1.0):.0%}",
                        note,
                    ),
                    tags=(tag,),
                )

        foot = tk.Frame(win, bg=BG)
        foot.pack(fill="x", padx=18, pady=(0, 12))
        ttk.Button(foot, text="打开报告 CSV", style="Secondary.TButton",
                   command=self.open_video_report).pack(side="left")
        ttk.Button(foot, text="关闭", style="Accent.TButton",
                   command=win.destroy).pack(side="right")

        def closed() -> None:
            self.video_report_window = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", closed)

    def quarantine_video_duplicates(self) -> None:
        if self.busy or self.video_outcome is None:
            return
        outcome = self.video_outcome
        duplicates = len(outcome.videos) - len(outcome.groups)
        if duplicates <= 0:
            messagebox.showinfo("没有重复视频", "本次检测没有需要隔离的重复文件。", parent=self.root)
            return
        quarantine = BASE_DIR / f"视频隔离区_{datetime.now():%Y%m%d_%H%M%S}"
        log_file = BASE_DIR / "视频隔离记录.csv"
        answer = messagebox.askyesno(
            "隔离重复视频",
            f"将把 {duplicates} 个重复视频移动到：\n{quarantine}\n\n"
            "保留版本和源目录中的唯一视频不会移动。\n"
            "文件不会被永久删除，可以稍后手动恢复。\n\n确定继续吗？",
            parent=self.root, default="no", icon="warning",
        )
        if not answer:
            return
        self._pending_quarantine = quarantine
        self._log(self.video_log, f"开始隔离 {duplicates} 个重复视频...", "phase")

        def job(emit):
            def progress(index: int, total: int, path: Path) -> None:
                emit("progress", {
                    "tab": "video", "phase": "quarantine",
                    "fraction": index / max(1, total),
                    "message": f"隔离 {index}/{total}：{path.name}",
                })
            return vm.quarantine_duplicates(
                outcome.groups, quarantine,
                log_file=log_file, progress=progress,
            )

        self._start_task("video", job, self._video_quarantine_done)

    def _video_quarantine_done(self, records: list[dict[str, str]]) -> None:
        success = sum(1 for row in records if row["是否成功"] == "已隔离")
        failed = len(records) - success
        quarantine = self._pending_quarantine
        self._pending_quarantine = None
        if quarantine:
            self.last_video_quarantine = quarantine
        self.video_quarantined = success > 0
        self._log(self.video_log,
                  f"隔离完成：成功 {success} 个，失败 {failed} 个。", "ok" if not failed else "warn")
        if quarantine:
            self._log(self.video_log, f"隔离区：{quarantine}", "info")
        self._update_video_action_buttons()
        messagebox.showinfo(
            "隔离完成",
            f"成功隔离 {success} 个，失败 {failed} 个。\n\n"
            f"隔离区：\n{quarantine}",
            parent=self.root,
        )

    def open_video_quarantine(self) -> None:
        if not self.last_video_quarantine or not self.last_video_quarantine.exists():
            messagebox.showinfo("还没有隔离区", "还没有隔离过重复视频。", parent=self.root)
            return
        try:
            open_path(self.last_video_quarantine)
        except Exception as exc:
            messagebox.showerror("打开失败", str(exc), parent=self.root)

    def show_rename_dialog(self) -> None:
        if self.rename_window and self.rename_window.winfo_exists():
            self.rename_window.lift()
            self.rename_window.focus_force()
            return

        win = tk.Toplevel(self.root)
        self.rename_window = win
        win.title("批量清理 MP4 文件名 · 小工作箱")
        win.configure(bg=BG)
        win.geometry("1040x640")
        win.minsize(880, 500)
        win.transient(self.root)

        folder_var = tk.StringVar(value=self.video_root_var.get())
        keyword_var = tk.StringVar(value=fr.DEFAULT_KEYWORD)
        recursive_var = tk.BooleanVar(value=True)
        all_colons_var = tk.BooleanVar(value=True)
        strip_numbers_var = tk.BooleanVar(value=True)
        status_var = tk.StringVar(value="选择文件夹后点“扫描预览”。")
        report_var = tk.StringVar(value="")
        items: list[fr.RenameItem] = []
        self.rename_folder_var = folder_var
        self.rename_keyword_var = keyword_var
        self.rename_recursive_var = recursive_var
        self.rename_all_colons_var = all_colons_var
        self.rename_strip_numbers_var = strip_numbers_var
        self.rename_status_var = status_var
        self.rename_report_var = report_var

        head = tk.Frame(win, bg=BG)
        head.pack(fill="x", padx=18, pady=(14, 6))
        tk.Label(head, text="批量清理 MP4 文件名", bg=BG, fg=TEXT,
                 font=(self.font_family, 18, "bold")).pack(anchor="w")
        tk.Label(head, text="删除文件名中的指定文字，只改名字，不改视频内容。",
                 bg=BG, fg=MUTED, font=(self.font_family, 9)).pack(anchor="w", pady=(2, 0))

        form = self._make_card(win, "目录与关键词")
        form.pack(fill="x", padx=18, pady=(4, 8))
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text="视频文件夹", style="Card.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 10), pady=4)
        ttk.Entry(form, textvariable=folder_var).grid(
            row=0, column=1, sticky="ew", padx=(0, 8), pady=4)
        ttk.Button(form, text="浏览…", style="Secondary.TButton",
                   command=lambda: self._browse_rename_folder(folder_var)).grid(
            row=0, column=2, pady=4)
        ttk.Label(form, text="删除文字", style="Card.TLabel").grid(
            row=1, column=0, sticky="w", padx=(0, 10), pady=4)
        ttk.Entry(form, textvariable=keyword_var).grid(
            row=1, column=1, sticky="ew", padx=(0, 8), pady=4)
        ttk.Checkbutton(form, text="包含子文件夹", variable=recursive_var,
                        style="Card.TCheckbutton").grid(
            row=1, column=2, sticky="w", pady=4)
        ttk.Label(form, text="新建名冲突时自动加 (2)、(3)，不会覆盖已有文件。",
                  style="Hint.TLabel").grid(
            row=2, column=1, columnspan=2, sticky="w", pady=(0, 2))
        ttk.Checkbutton(
            form, text="同时清理所有带冒号的文件（不只“正在播放”）",
            variable=all_colons_var, style="Card.TCheckbutton",
        ).grid(row=3, column=1, columnspan=2, sticky="w", pady=(2, 0))
        ttk.Checkbutton(
            form, text="删除文件名末尾 4 位以上数字编号",
            variable=strip_numbers_var, style="Card.TCheckbutton",
        ).grid(row=4, column=1, columnspan=2, sticky="w", pady=(2, 0))

        actions = tk.Frame(win, bg=BG)
        actions.pack(fill="x", padx=18, pady=(0, 8))
        scan_btn = ttk.Button(actions, text="扫描预览", style="Accent.TButton")
        scan_btn.pack(side="left")
        apply_btn = ttk.Button(actions, text="执行改名", style="Danger.TButton",
                               state="disabled")
        apply_btn.pack(side="left", padx=8)
        report_btn = ttk.Button(actions, text="打开改名报告", style="Secondary.TButton",
                                state="disabled")
        report_btn.pack(side="left")

        table_card = self._make_card(win, "改名预览")
        table_card.pack(fill="both", expand=True, padx=18, pady=(0, 8))
        table_card.rowconfigure(0, weight=1)
        table_card.columnconfigure(0, weight=1)
        frame = tk.Frame(table_card, bg=CARD)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        tree = ttk.Treeview(frame, columns=("status", "old", "new", "dir"),
                            show="headings", height=12)
        tree.heading("status", text="状态")
        tree.heading("old", text="原文件名")
        tree.heading("new", text="新文件名")
        tree.heading("dir", text="所在目录")
        tree.column("status", width=75, minwidth=60, anchor="center", stretch=False)
        tree.column("old", width=390, minwidth=220, anchor="w")
        tree.column("new", width=390, minwidth=220, anchor="w")
        tree.column("dir", width=230, minwidth=140, anchor="w", stretch=True)
        tree.grid(row=0, column=0, sticky="nsew")
        vsb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        tree.configure(yscrollcommand=vsb.set)
        tree.tag_configure("rename", background="#f0fdf4")
        tree.tag_configure("skip", background="#f8fafc")
        tree.tag_configure("fail", background="#fef2f2")
        tk.Label(table_card, textvariable=status_var, bg=CARD, fg=MUTED,
                 font=(self.font_family, 9), anchor="w").grid(
            row=1, column=0, sticky="ew", pady=(5, 0))

        def refresh() -> None:
            nonlocal items
            folder_text = folder_var.get().strip()
            if not folder_text:
                status_var.set("请选择视频文件夹。")
                return
            folder = Path(folder_text).expanduser().resolve()
            if not folder.exists():
                messagebox.showerror("路径不存在", f"文件夹不存在：\n{folder}", parent=win)
                return
            try:
                items = fr.collect_rename_items(
                    [folder], keyword_var.get(), recursive=recursive_var.get(),
                    all_colons=all_colons_var.get(),
                    strip_long_numbers=strip_numbers_var.get(),
                )
            except Exception as exc:
                messagebox.showerror("扫描失败", str(exc), parent=win)
                return
            tree.delete(*tree.get_children())
            for item in items:
                tag = "rename" if item.action == "待改名" else "skip" if item.action == "跳过" else "fail"
                tree.insert("", "end", values=(
                    item.action, item.path.name, item.new_name, str(item.path.parent),
                ), tags=(tag,))
            pending = sum(1 for item in items if item.action == "待改名")
            status_var.set(f"找到 {len(items)} 个可清理的 MP4，可改名 {pending} 个。")
            apply_btn.configure(state="normal" if pending else "disabled")
            base = folder if folder.is_dir() else folder.parent
            report_var.set(str(base / fr.DEFAULT_REPORT))
            report_btn.configure(
                state="normal" if Path(report_var.get()).exists() else "disabled")

        def apply() -> None:
            nonlocal items
            pending = sum(1 for item in items if item.action == "待改名")
            if not pending:
                return
            if not messagebox.askyesno(
                "确认改名",
                f"将修改 {pending} 个 MP4 的文件名，删除“{keyword_var.get()}”。\n\n"
                "只改文件名，不修改视频内容。确定继续吗？",
                parent=win, default="no", icon="warning",
            ):
                return
            records = fr.apply_renames(
                items, report_file=Path(report_var.get()) if report_var.get() else None,
            )
            ok = sum(1 for row in records if row["状态"] == "已改名")
            failed = sum(1 for row in records if row["状态"] == "失败")
            status_var.set(f"改名完成：成功 {ok} 个，失败 {failed} 个。报告：{report_var.get()}")
            report_btn.configure(
                state="normal" if Path(report_var.get()).exists() else "disabled")
            apply_btn.configure(state="disabled")
            items = []
            messagebox.showinfo(
                "改名完成",
                f"成功 {ok} 个，失败 {failed} 个。\n\n报告：\n{report_var.get()}",
                parent=win,
            )

        def open_report() -> None:
            report = Path(report_var.get())
            if report.exists():
                try:
                    open_path(report)
                except Exception as exc:
                    messagebox.showerror("打开失败", str(exc), parent=win)

        self.rename_scan_btn = scan_btn
        self.rename_apply_btn = apply_btn
        self.rename_report_btn = report_btn
        self.rename_tree = tree
        scan_btn.configure(command=refresh)
        apply_btn.configure(command=apply)
        report_btn.configure(command=open_report)
        ttk.Button(actions, text="关闭", style="Secondary.TButton",
                   command=win.destroy).pack(side="right")

        def closed() -> None:
            self.rename_window = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", closed)

    def _browse_rename_folder(self, variable: tk.StringVar) -> None:
        selected = filedialog.askdirectory(parent=self.root, title="选择包含 MP4 的文件夹",
                                           initialdir=self._dialog_start_dir(variable.get()))
        if selected:
            variable.set(selected)

    # ------------------------------------------------------------------
    # 违禁词检测
    # ------------------------------------------------------------------
    def start_check(self) -> None:
        if self.busy:
            return
        if not self.check_target_var.get().strip():
            messagebox.showinfo("请选择对象", "请先选择要检测的 TXT 或文件夹。", parent=self.root)
            return
        if not self.check_words_var.get().strip():
            if self.standard_words_path.exists():
                self.check_words_var.set(str(self.standard_words_path))
            else:
                messagebox.showinfo("请选择词库", "请先选择违禁词库。", parent=self.root)
                return
        if not self.check_report_var.get().strip():
            target_hint = Path(self.check_target_var.get()).expanduser()
            base = target_hint if target_hint.is_dir() else target_hint.parent
            self.check_report_var.set(str(base / wc.DEFAULT_REPORT_FILE))
        try:
            target = self._abs_path(self.check_target_var)
            words = self._abs_path(self.check_words_var)
            report = self._abs_path(self.check_report_var)
            threshold = int(self.check_threshold_var.get())
        except (ValueError, OSError):
            messagebox.showerror("参数有误", "删除阈值必须是整数。", parent=self.root)
            return
        self._save_settings()
        if threshold < 1:
            messagebox.showerror("参数有误", "删除阈值必须大于等于 1。", parent=self.root)
            return
        if not target.exists():
            messagebox.showerror("路径不存在", f"检测对象不存在：\n{target}", parent=self.root)
            return
        if not words.exists():
            messagebox.showerror("词库不存在", f"找不到违禁词库：\n{words}", parent=self.root)
            return
        if target.is_file() and target.suffix.casefold() != ".txt":
            messagebox.showerror("文件类型不对", "请选择 TXT 文件或文件夹。", parent=self.root)
            return

        summary = report.with_name("检测摘要.txt")
        log_file = BASE_DIR / wc.DEFAULT_DELETE_LOG_FILE
        recursive = self.check_recursive_var.get()
        ignore_case = self.check_ignore_case_var.get()

        self.check_result = None
        self.check_rows.clear()
        self._clear_log(self.check_log)
        self._log(self.check_log, f"开始检测：{target}", "phase")
        self._log(self.check_log, f"词库：{words}（{'忽略大小写' if ignore_case else '区分大小写'}）", "info")
        self.check_progress.configure(value=0)
        self.check_status_var.set("准备中…")
        self.check_tree.delete(*self.check_tree.get_children())
        self.check_detail_var.set("选中一行可查看完整路径与行号。")
        self.check_stat_scanned.set("—")
        self.check_stat_matched.set("—")
        self.check_stat_hits.set("—")
        self.check_stat_candidates.set("—")
        self.check_open_btn.configure(state="disabled")
        self.check_process_btn.configure(state="disabled")
        self.check_quarantine_btn.configure(
            state="normal" if self.last_quarantine and self.last_quarantine.exists() else "disabled")
        self._check_bucket = -1

        def job(emit):
            def progress(index: int, total: int, path: Path) -> None:
                emit("progress", {
                    "tab": "check", "phase": "check",
                    "fraction": index / max(1, total),
                    "message": f"{index}/{total}：{path.name}",
                })
            return wc.scan_paths(
                [target], words,
                report_file=report,
                summary_file=summary,
                recursive=recursive,
                ignore_case=ignore_case,
                delete_threshold=threshold,
                progress=progress,
                cancel=self.cancel_event.is_set,
            )

        self._start_task("check", job, self._check_done)

    def _check_done(self, result: wc.CheckResult) -> None:
        self.check_result = result
        self.last_report = result.report
        self._populate_check_tree(result)
        self.check_progress.configure(value=100)
        self.check_status_var.set("检测完成")
        self.check_stat_scanned.set(f"{result.scanned} 个")
        self.check_stat_matched.set(f"{len(result.matched)} 个")
        self.check_stat_hits.set(f"{result.total_hits} 次")
        candidates = result.candidates()
        self.check_stat_candidates.set(f"{len(candidates)} 个")
        self._log(self.check_log,
                  f"完成：扫描 {result.scanned} 个，命中 {len(result.matched)} 个文件，"
                  f"共 {result.total_hits} 次，达到阈值 {len(candidates)} 个。", "ok")
        if result.report:
            self._log(self.check_log, f"报告：{result.report}", "info")
            self.check_open_btn.configure(state="normal" if result.report.exists() else "disabled")
        if result.summary:
            self._log(self.check_log, f"摘要：{result.summary}", "info")
        if result.skipped:
            self._log(self.check_log, f"有 {len(result.skipped)} 个文件读取失败。", "warn")
        self._update_process_button()
        if not result.matched:
            self._log(self.check_log, "未检测到违禁词。", "ok")

    def _populate_check_tree(self, result: wc.CheckResult) -> None:
        children = self.check_tree.get_children()
        if children:
            self.check_tree.delete(*children)
        self.check_rows.clear()
        row = 0
        for file_hits in result.matched:
            for hit in file_hits.hits:
                shown_lines = ", ".join(str(n) for n in hit.lines[:20])
                if len(hit.lines) > 20:
                    shown_lines += " …"
                iid = self.check_tree.insert(
                    "", "end",
                    values=(file_hits.path.name, hit.word, hit.count, shown_lines),
                    tags=("odd" if row % 2 else "even",),
                )
                self.check_rows[iid] = (file_hits, hit)
                row += 1

    def _show_selected_detail(self, _event=None) -> None:
        selection = self.check_tree.selection()
        if not selection:
            return
        item = self.check_rows.get(selection[0])
        if not item:
            return
        file_hits, hit = item
        lines = ", ".join(str(n) for n in hit.lines)
        self.check_detail_var.set(
            f"{file_hits.path}  ·  {hit.word} {hit.count} 次  ·  行号：{lines}"
        )

    def _update_process_button(self) -> None:
        if not hasattr(self, "check_process_btn"):
            return
        if self.busy or not self.check_result:
            self.check_process_btn.configure(state="disabled")
            return
        candidates = self.check_result.candidates()
        if not candidates:
            self.check_process_btn.configure(state="disabled")
            return
        permanent = self.check_permanent_var.get()
        self.check_process_btn.configure(
            state="normal",
            text="永久删除达标文件" if permanent else "隔离达标文件",
            style="Danger.TButton" if permanent else "Accent.TButton",
        )

    def process_hits(self) -> None:
        if self.busy or not self.check_result:
            return
        candidates = self.check_result.candidates()
        if not candidates:
            messagebox.showinfo("没有可处理的文件", "没有文件达到删除阈值。", parent=self.root)
            return
        permanent = self.check_permanent_var.get()
        mode = "delete" if permanent else "quarantine"
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        quarantine = BASE_DIR / f"违禁词隔离区_{timestamp}"
        if permanent:
            if not messagebox.askyesno(
                "永久删除确认",
                f"将永久删除 {len(candidates)} 个 TXT 文件。\n\n"
                "此操作不进入回收站，无法恢复。\n确定继续吗？",
                parent=self.root, default="no", icon="warning",
            ):
                return
            if not messagebox.askyesno(
                "最后一次确认",
                f"真的要永久删除这 {len(candidates)} 个文件吗？",
                parent=self.root, default="no", icon="warning",
            ):
                return
        else:
            if not messagebox.askyesno(
                "隔离确认",
                f"将把 {len(candidates)} 个文件移动到：\n{quarantine}\n\n"
                "文件不会被永久删除，可以稍后手动恢复。\n确定继续吗？",
                parent=self.root, default="yes", icon="question",
            ):
                return

        log_file = BASE_DIR / wc.DEFAULT_DELETE_LOG_FILE
        self.check_process_btn.configure(state="disabled")
        self._log(self.check_log,
                  f"开始{'永久删除' if permanent else '隔离'} {len(candidates)} 个文件...", "phase")

        def job(emit):
            def progress(index: int, total: int, path: Path) -> None:
                emit("progress", {
                    "tab": "check", "phase": "process",
                    "fraction": index / max(1, total),
                    "message": f"处理 {index}/{total}：{path.name}",
                })
            records = wc.process_candidates(
                self.check_result,  # type: ignore[arg-type]
                log_file=log_file,
                mode=mode,
                quarantine_dir=quarantine if mode == "quarantine" else None,
                progress=progress,
            )
            return records, quarantine

        self._start_task("check", job, self._process_done)

    def _process_done(self, payload) -> None:
        records, quarantine = payload
        success = sum(1 for row in records if row["是否成功"] in {"已隔离", "已删除"})
        failed = len(records) - success
        if quarantine and quarantine.exists():
            self.last_quarantine = quarantine
            self.check_quarantine_btn.configure(state="normal")
        self._log(self.check_log,
                  f"处理完成：成功 {success} 个，失败 {failed} 个。", "ok" if not failed else "warn")
        if failed:
            for row in records:
                if row["是否成功"] == "失败":
                    self._log(self.check_log,
                              f"失败：{row['文件名']}：{row['失败原因']}", "error")
        self.check_quarantine_btn.configure(
            state="normal" if self.last_quarantine and self.last_quarantine.exists() else "disabled")
        # 处理之后旧结果已失效，避免误点第二次处理
        self.check_result = None
        self.check_process_btn.configure(state="disabled")
        self.check_tree.delete(*self.check_tree.get_children())
        self.check_rows.clear()
        self.check_detail_var.set("文件已处理；旧结果已清空。")
        self.check_stat_candidates.set("0 个")
        if success:
            messagebox.showinfo(
                "处理完成",
                f"成功 {success} 个，失败 {failed} 个。\n\n"
                + (f"隔离区：\n{quarantine}" if quarantine and quarantine.exists() else "文件已永久删除。"),
                parent=self.root,
            )

    def open_check_report(self) -> None:
        report = self.last_report
        if not report or not report.exists():
            messagebox.showinfo("还没有报告", "请先完成一次检测。", parent=self.root)
            return
        try:
            open_path(report)
        except Exception as exc:
            messagebox.showerror("打开失败", str(exc), parent=self.root)

    def open_quarantine(self) -> None:
        if not self.last_quarantine or not self.last_quarantine.exists():
            messagebox.showinfo("没有隔离区", "还没有隔离过文件。", parent=self.root)
            return
        try:
            open_path(self.last_quarantine)
        except Exception as exc:
            messagebox.showerror("打开失败", str(exc), parent=self.root)

    # ------------------------------------------------------------------
    # 关闭
    # ------------------------------------------------------------------
    def _on_close(self) -> None:
        if self.busy:
            answer = messagebox.askyesno(
                "任务正在运行",
                "当前任务还未结束。退出会中断任务，确定退出吗？",
                parent=self.root, default="no", icon="warning",
            )
            if not answer:
                return
            self.cancel_event.set()
        self._closing = True
        self._save_settings()
        self.root.destroy()


def main() -> int:
    enable_dpi_awareness()
    root = tk.Tk()
    try:
        NovelToolbox(root)
    except Exception as exc:  # noqa: BLE001
        error_log = BASE_DIR / "工具箱错误.log"
        try:
            error_log.write_text(traceback.format_exc(), encoding="utf-8")
        except OSError:
            pass
        messagebox.showerror(
            "启动失败",
            f"{exc}\n\n详细信息已写入：\n{error_log}",
            parent=root,
        )
        root.destroy()
        return 1
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
