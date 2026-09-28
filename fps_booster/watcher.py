"""Следит за запуском игр и выдаёт им высокий приоритет процессора."""

from __future__ import annotations

import threading
from typing import Callable

import psutil


class GameWatcher(threading.Thread):
    def __init__(self, process_names: list[str], log: Callable[[str], None],
                 on_game_start: Callable[[str], None] | None = None, interval: float = 3.0):
        super().__init__(daemon=True, name="GameWatcher")
        self.names = {n.lower() for n in process_names}
        self.log = log
        self.on_game_start = on_game_start
        self.interval = interval
        self._stop_event = threading.Event()
        self._boosted: set[int] = set()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            self.scan()
            self._stop_event.wait(self.interval)

    def scan(self) -> None:
        alive = set()
        for proc in psutil.process_iter(["name"]):
            name = (proc.info.get("name") or "").lower()
            if name not in self.names:
                continue
            alive.add(proc.pid)
            if proc.pid in self._boosted:
                continue
            try:
                # HIGH, а не REALTIME: realtime может подвесить ввод и звук
                proc.nice(psutil.HIGH_PRIORITY_CLASS)
            except (psutil.AccessDenied, psutil.NoSuchProcess) as exc:
                self.log(f"Не удалось повысить приоритет {name}: {exc}")
            else:
                self.log(f"🎮 {name} запущен — приоритет CPU: высокий")
                if self.on_game_start:
                    self.on_game_start(name)
            self._boosted.add(proc.pid)
        self._boosted &= alive
