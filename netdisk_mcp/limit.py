"""转存和创建分享之间的间隔。生产上短时间连续写网盘会先被禁分享。"""

from __future__ import annotations

import os
import threading
import time


class Pace:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        try:
            gap = float(os.getenv("NETDISK_MIN_INTERVAL_SECONDS", "3"))
        except ValueError:
            gap = 3.0
        gap = max(gap, 0.0)
        with self._lock:
            now = time.monotonic()
            if now < self._next:
                time.sleep(self._next - now)
            self._next = time.monotonic() + gap


PACE = Pace()
