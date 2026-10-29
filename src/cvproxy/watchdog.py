"""Event-loop stall watchdog: logs what is blocking the loop while it happens."""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import traceback

logger = logging.getLogger(__name__)


class LoopWatchdog:
    """Pings the event loop from a thread; logs the loop thread's stack on a stall.

    A stall is the loop failing to run a scheduled callback within *threshold*
    seconds.  The stack is captured once per stall, while it is still blocked,
    and the total stall time is logged when the loop catches up.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        threshold: float = 10.0,
        interval: float = 1.0,
    ) -> None:
        self._loop = loop
        self._threshold = threshold
        self._interval = interval
        self._loop_thread_id = threading.get_ident()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="loop-watchdog", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            seen = threading.Event()
            started = time.monotonic()
            try:
                self._loop.call_soon_threadsafe(seen.set)
            except RuntimeError:  # loop closed
                return
            if seen.wait(self._threshold):
                continue
            frame = sys._current_frames().get(self._loop_thread_id)
            stack = "".join(traceback.format_stack(frame)) if frame else "(no frame)"
            logger.warning(
                "Event loop blocked for over %.0fs; loop thread is at:\n%s",
                self._threshold,
                stack,
            )
            while not seen.wait(self._interval):
                if self._stop.is_set():
                    return
            logger.warning("Event loop stall ended after %.1fs", time.monotonic() - started)
