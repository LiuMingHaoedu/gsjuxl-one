# -*- coding: utf-8 -*-
"""后台任务队列。

UI 只负责提交任务和轮询事件；搜索、取目录、下载缓存都不阻塞 Tk 主线程。
"""

from __future__ import annotations

import queue
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

__all__ = ["Task", "TaskQueue"]


@dataclass
class Task:
    task_id: str
    name: str
    fn: Callable[..., Any]
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    status: str = "pending"
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    error: str = ""
    result: Any = None


class TaskQueue:
    """固定工作线程数的任务队列。

    事件格式：``("started", task)`` / ``("done", task)`` / ``("error", task)``。
    UI 用 ``poll()`` 从队列里取事件。
    """

    def __init__(self, max_workers: int = 4, name: str = "book-source") -> None:
        self.max_workers = max(1, int(max_workers))
        self.name = name
        self.events: "queue.Queue[tuple[str, Task]]" = queue.Queue()
        self._tasks: dict[str, Task] = {}
        self._queue: "queue.Queue[Task | None]" = queue.Queue()
        self._lock = threading.RLock()
        self._counter = 0
        self._workers: list[threading.Thread] = []
        self._closed = False
        for index in range(self.max_workers):
            thread = threading.Thread(
                target=self._worker,
                name=f"{name}-{index}",
                daemon=True,
            )
            thread.start()
            self._workers.append(thread)

    # ------------------------------------------------------------------
    def submit(self, name: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> str:
        with self._lock:
            self._counter += 1
            task_id = f"t{self._counter}"
            task = Task(task_id=task_id, name=name, fn=fn, args=args, kwargs=kwargs)
            self._tasks[task_id] = task
        self._queue.put(task)
        return task_id

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            task.cancel_event.set()
            if task.status == "pending":
                task.status = "cancelled"
            return True

    def cancel_all(self) -> None:
        with self._lock:
            for task in self._tasks.values():
                task.cancel_event.set()
                if task.status == "pending":
                    task.status = "cancelled"

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def poll(self) -> list[tuple[str, Task]]:
        items: list[tuple[str, Task]] = []
        while True:
            try:
                items.append(self.events.get_nowait())
            except queue.Empty:
                break
        return items

    def pending_count(self) -> int:
        with self._lock:
            return sum(1 for task in self._tasks.values() if task.status in ("pending", "running"))

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
        for _ in self._workers:
            self._queue.put(None)
        for thread in self._workers:
            thread.join(timeout=1.0)

    # ------------------------------------------------------------------
    def _worker(self) -> None:
        while True:
            task = self._queue.get()
            if task is None:
                return
            if task.cancel_event.is_set():
                task.status = "cancelled"
                continue
            task.status = "running"
            task.started_at = time.time()
            self.events.put(("started", task))
            try:
                task.result = task.fn(*task.args, **task.kwargs)
                task.status = "done"
                self.events.put(("done", task))
            except Exception as exc:
                task.status = "error"
                task.error = "".join(
                    traceback.format_exception_only(type(exc), exc)
                ).strip()
                self.events.put(("error", task))
            finally:
                task.finished_at = time.time()
