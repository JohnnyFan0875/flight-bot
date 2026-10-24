"""並行執行查詢：隨機間隔、重試、連續失敗時提前結束。"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from .config import RunnerConfig
from .providers.base import ProviderError

log = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class Outcome(Generic[T]):
    task: T
    result: Any = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class Stats:
    """依請求類型（calendar、detail…）統計 ok / failed / empty。"""

    counts: Counter = field(default_factory=Counter)
    aborted: bool = False

    def add(self, kind: str, status: str) -> None:
        self.counts[(kind, status)] += 1

    def total(self, status: str | None = None) -> int:
        return sum(n for (_, s), n in self.counts.items() if status is None or s == status)

    def failure_rate(self) -> float:
        total = self.total()
        return self.total("failed") / total if total else 0.0

    def by_kind(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for (kind, status), n in self.counts.items():
            out.setdefault(kind, {})[status] = n
        return out


class Aborted(Exception):
    pass


class Runner:
    def __init__(self, cfg: RunnerConfig, stats: Stats | None = None, sleep: Callable[[float], None] = time.sleep):
        self.cfg = cfg
        self.stats = stats or Stats()
        self._sleep = sleep
        self._lock = threading.Lock()
        self._consecutive_failures = 0

    def run(
        self,
        kind: str,
        tasks: Sequence[T],
        fn: Callable[[T], Any],
        is_empty: Callable[[Any], bool] = lambda r: not r,
        give_up_after: int | None = None,
        can_abort: bool = True,
    ) -> list[Outcome[T]]:
        """執行所有任務並回傳結果（順序和 tasks 相同）。沒跑到的任務不會出現在結果裡。

        give_up_after：這批任務連續失敗幾次就放棄剩下的（不影響其他批次）。
        can_abort=False：這批的失敗不計入整次執行的連續失敗次數，例如另有備案的日期 API。
        """
        if not tasks:
            return []
        batch = {"failures": 0, "gave_up": False}

        def work(task: T) -> Outcome[T] | None:
            if self.stats.aborted or batch["gave_up"]:
                return None
            outcome = self._attempt(task, fn)
            with self._lock:
                if outcome.ok:
                    batch["failures"] = 0
                    if can_abort:
                        self._consecutive_failures = 0
                    self.stats.add(kind, "empty" if is_empty(outcome.result) else "ok")
                else:
                    batch["failures"] += 1
                    self.stats.add(kind, "failed")
                    log.warning("%s 失敗：%s → %s", kind, task, outcome.error)
                    if give_up_after is not None and batch["failures"] >= give_up_after:
                        if not batch["gave_up"]:
                            log.warning("%s 連續失敗 %d 次，放棄這批剩下的任務", kind, batch["failures"])
                        batch["gave_up"] = True
                    if can_abort:
                        self._consecutive_failures += 1
                        if self._consecutive_failures >= self.cfg.abort_after_consecutive_failures:
                            if not self.stats.aborted:
                                log.error("連續失敗 %d 次，提前結束", self._consecutive_failures)
                            self.stats.aborted = True
            self._sleep(random.uniform(*self.cfg.delay_seconds))
            return outcome

        with ThreadPoolExecutor(max_workers=self.cfg.concurrency) as pool:
            results = list(pool.map(work, tasks))
        return [r for r in results if r is not None]

    def _attempt(self, task: T, fn: Callable[[T], Any]) -> Outcome[T]:
        error = None
        for attempt in range(self.cfg.retries + 1):
            try:
                return Outcome(task, fn(task))
            except ProviderError as e:
                error = str(e)
            except Exception as e:  # 解析時遇到沒預期的格式也算失敗，不要讓整次執行中斷
                log.exception("查詢時發生未預期的錯誤")
                error = f"{type(e).__name__}: {e}"
            if attempt < self.cfg.retries:
                self._sleep(2 ** (attempt + 1) + random.uniform(0, 1))
        return Outcome(task, error=error)
