"""可注入时钟：锁的到期判断只依赖这里提供的当前时间。

生产环境使用 SystemClock；测试注入 FakeClock 以精确控制锁过期。
"""

from __future__ import annotations

import time


class SystemClock:
    def now(self) -> float:
        return time.time()


class FakeClock:
    """测试用手动时钟。"""

    def __init__(self, now: float = 1_000_000.0):
        self._now = now

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds
