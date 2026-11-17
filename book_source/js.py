# -*- coding: utf-8 -*-
"""JS 引擎层：用 SpiderMonkey（pythonmonkey）执行书源里的 ``@js:`` 规则。

legado 用 Rhino；桌面版直接嵌入 SpiderMonkey，语法覆盖更全，并且可以把
Python 侧的 ``java.ajax`` 等宿主函数同步注入 JS，行为与 Rhino 基本一致。

依赖：``pythonmonkey``（可选安装）。未安装时只有 ``@js:`` 规则不可用，
其它规则不受影响。
"""

from __future__ import annotations

import json
import queue
import threading
from typing import Any, Callable

__all__ = ["JSError", "JSContext", "javascript_available", "install_hint"]

_RUNNER_SOURCE = r"""
globalThis.__bs_eval = function (code, scope) {
    try {
        var out;
        with (scope) {
            var __value = eval(code);
        }
        if (__value !== undefined) {
            out = __value;
        } else if (typeof result !== "undefined") {
            out = result;
        } else {
            out = scope.result;
        }
        if (out === undefined) out = null;
        try {
            return JSON.stringify({ ok: true, v: out });
        } catch (e) {
            return JSON.stringify({ ok: true, v: String(out) });
        }
    } catch (e) {
        var message = (e && (e.message || e.toString())) || "JS 执行错误";
        return JSON.stringify({ ok: false, e: String(message) });
    }
};
"""


class JSError(Exception):
    """书源 JS 执行失败。"""


_runner: Any = None
_pm: Any = None
_engine_lock = threading.RLock()

# SpiderMonkey 运行时不保证跨线程安全。所有 JS 求值都放到同一个后台线程，
# 调用方同步等待结果；这样搜索多个书源时不会踩踏运行时。
_job_queue: "queue.Queue[tuple[str, dict[str, Any], dict[str, Any], threading.Event] | None]" = queue.Queue()
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()


def install_hint() -> str:
    return "书源 JS 规则需要 pythonmonkey，请运行：python -m pip install pythonmonkey"


def javascript_available() -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec("pythonmonkey") is not None
    except (ImportError, ValueError):
        return False


def _ensure_runner() -> Any:
    global _runner, _pm
    if _runner is not None:
        return _runner
    try:
        import pythonmonkey as pm
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise JSError(install_hint()) from exc
    _pm = pm
    pm.eval(_RUNNER_SOURCE)
    _runner = pm.eval("__bs_eval")
    return _runner


class JSContext:
    """一个可复用的 JS 执行上下文。

    SpiderMonkey 运行时不保证跨线程安全，所以所有求值都在同一个后台线程串行
    执行；调用线程同步等待结果。这样多书源并发搜索时也不会互相踩踏。
    """

    def __init__(self, logger: Callable[[str], None] | None = None) -> None:
        self.logger = logger

    def eval(self, code: str, extra: dict[str, Any] | None = None) -> Any:
        if not code or not code.strip():
            return None
        scope = self._prepare_scope(extra)
        if threading.current_thread() is _worker:
            return _eval_sync(code, scope)
        _ensure_worker()
        box: dict[str, Any] = {}
        done = threading.Event()
        _job_queue.put((code, scope, box, done))
        if not done.wait(timeout=90):
            raise JSError("JS 执行超时（90 秒）")
        error = box.get("error")
        if error is not None:
            if isinstance(error, JSError):
                raise error
            raise JSError(f"JS 执行失败：{error}")
        return box.get("value")

    def _prepare_scope(self, extra: dict[str, Any] | None) -> dict[str, Any]:
        scope: dict[str, Any] = dict(extra or {})
        # 不把文件系统/进程能力直接暴露给普通规则；书源本身仍是可执行配置，
        # 只应导入可信来源。
        for name in ("pythonmonkey", "require", "process", "os", "sys"):
            scope.setdefault(name, None)
        scope.setdefault("console", _console(self))
        scope.setdefault("print", lambda *args: self._log(" ".join(str(arg) for arg in args)))
        return scope

    # 供宿主桥接使用
    def call(self, function: Any, *args: Any) -> Any:
        return function(*args)

    def _log(self, message: str) -> None:
        if self.logger:
            try:
                self.logger(message)
            except Exception:
                pass

    def _console(self, level: str) -> Callable[..., None]:
        def emit(*args: Any) -> None:
            self._log(f"[console.{level}] " + " ".join(_stringify(arg) for arg in args))

        return emit


def _ensure_worker() -> None:
    global _worker
    if _worker is not None and _worker.is_alive():
        return
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(
                target=_worker_loop, name="book-source-js", daemon=True
            )
            _worker.start()


def _worker_loop() -> None:
    while True:
        job = _job_queue.get()
        if job is None:
            return
        code, scope, box, done = job
        try:
            box["value"] = _eval_sync(code, scope)
        except BaseException as exc:  # 交给调用线程处理
            box["error"] = exc
        finally:
            done.set()


def _eval_sync(code: str, scope: dict[str, Any]) -> Any:
    runner = _ensure_runner()
    with _engine_lock:
        try:
            raw = runner(code, scope)
        except Exception as exc:  # SpiderMonkey 内部异常
            raise JSError(f"JS 执行失败：{exc}") from exc
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise JSError(f"JS 返回值不是 JSON：{raw!r}") from exc
    if not payload.get("ok"):
        raise JSError(str(payload.get("e") or "JS 执行错误"))
    return payload.get("v")


def _console(context: JSContext) -> dict[str, Callable[..., None]]:
    return {
        "log": context._console("log"),
        "info": context._console("info"),
        "warn": context._console("warn"),
        "error": context._console("error"),
        "debug": context._console("debug"),
    }


def _stringify(value: Any) -> str:
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)
    return str(value)
