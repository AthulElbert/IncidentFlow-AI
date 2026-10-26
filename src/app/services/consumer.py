"""Runs a bus consumer on a background thread: call `run_once` every `interval` seconds until stopped.

A failing handler leaves its event uncommitted (the bus redelivers it), so the loop logs and tries again
on the next tick instead of dying.
"""
import logging
import threading
from typing import Callable

logger = logging.getLogger("app.consumer")


class ConsumerLoop:
    def __init__(self, name: str, run_once: Callable[[], int], interval_seconds: float = 2.0) -> None:
        self.name = name
        self.run_once = run_once
        self.interval = max(0.1, interval_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"consumer-{self.name}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                logger.exception("consumer-failed", extra={"consumer": self.name})
            self._stop.wait(self.interval)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())
