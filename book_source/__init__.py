# -*- coding: utf-8 -*-
"""书源引擎。

设计思路参考 legado（阅读 3.0 / with-MD3）：
程序本体只负责调度、UI、缓存、书架、任务队列；每个站点怎么抓、怎么解析，
全部放在独立的「书源」JSON 配置里。

本包不依赖任何第三方库，仅使用 Python 标准库。
"""

from __future__ import annotations

__version__ = "1.0.0"

__all__ = [
    "http",
    "js",
    "dom",
    "rules",
    "source",
    "engine",
    "store",
    "tasks",
]
