"""Мост между HTML-интерфейсом и Python. Публичные методы вызываются из JS
как `pywebview.api.<метод>()`; всё, что начинается с `_`, скрыто от страницы."""

from __future__ import annotations

import json
import threading
import time

import psutil

from . import APP_NAME, __version__
from .backup import Backup, data_dir
from .games import PROFILES, detect_games
from .system import clean_temp, list_startup, purge_standby_memory, set_startup_enabled
from .tweaks import apply_tweaks, build_tweaks, revert_all
from .winutil import is_admin

LAUNCH_NOTES = {
    "-novid": "без заставки при запуске",
    "-nojoy": "не грузить поддержку джойстиков — меньше ОЗУ",
    "+exec autoexec": "гарантированно выполнить autoexec.cfg",
    "-map dota": "заранее загрузить карту — быстрее вход в матч",
}


class Api:
    def __init__(self):
        self._admin = is_admin()
        self._backup = Backup()
        self._games = detect_games()
        self._tweaks = build_tweaks(self._games)
        self._settings_path = data_dir() / "settings.json"
        self._settings = self._load_settings()
        self._logs: list[dict] = []
        self._log_lock = threading.Lock()
        self._busy = threading.Lock()
        self._watcher = None
        self._window = None
        self._startup_cache: list = []
        psutil.cpu_percent(None)  # первый вызов всегда 0 — «прогреваем»

        if not self._admin:
            self._log("Запущено без прав администратора — часть оптимизаций недоступна.", "warn")
        if not self._games:
            self._log("CS2 и Dota 2 не найдены — применятся только оптимизации ПК.", "warn")
        if self._settings.get("watch", True):
            self._start_watch()

    # --- служебное --------------------------------------------------------------

    def _attach(self, window) -> None:
        self._window = window

    def _log(self, message: str, level: str = "info") -> None:
        with self._log_lock:
            self._logs.append({"id": len(self._logs), "t": time.strftime("%H:%M:%S"),
                               "msg": message, "level": level})

    def _log_plain(self, message: str) -> None:
        level = "err" if "✖" in message else "ok" if "✔" in message else "info"
        self._log(message.strip(), level)

    def _load_settings(self) -> dict:
        try:
            return json.loads(self._settings_path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_settings(self) -> None:
        try:
            self._settings_path.write_text(json.dumps(self._settings, indent=2), "utf-8")
        except OSError:
            pass

    def _tweak_info(self) -> list[dict]:
        selected = self._settings.get("selected", {})
        return [{
            "id": t.id, "title": t.title, "description": t.description,
            "category": t.category, "needs_admin": t.needs_admin,
            "needs_reboot": t.needs_reboot, "available": t.available(),
            "locked": t.needs_admin and not self._admin,
            "applied": t.is_applied(self._backup),
            "selected": bool(selected.get(t.id, t.default)) and t.available(),
        } for t in self._tweaks]

    def _start_watch(self) -> None:
        if self._watcher is not None:
            return
        from .watcher import GameWatcher

        self._watcher = GameWatcher([p.process for p in PROFILES], self._log_plain,
                                    on_game_start=self._on_game_start)
        self._watcher.start()

    def _on_game_start(self, _name: str) -> None:
        if self._settings.get("purge_on_start", True) and self._admin:
            try:
                purge_standby_memory()
                self._log("ОЗУ очищена перед игрой", "ok")
            except OSError:
                pass

    # --- вызывается из JS ---------------------------------------------------------

    def get_state(self) -> dict:
        found = {g.profile.key: g for g in self._games}
        mem = psutil.virtual_memory()
        return {
            "app": APP_NAME, "version": __version__, "admin": self._admin,
            "cpu_name": _cpu_name(), "cores": psutil.cpu_count(logical=True),
            "ram_total": mem.total,
            "games": [{
                "key": p.key, "name": p.name, "found": p.key in found,
                "path": str(found[p.key].install_dir) if p.key in found else "",
                "launch": p.launch_options,
                "notes": [[k, v] for k, v in LAUNCH_NOTES.items() if k in p.launch_options],
            } for p in PROFILES],
            "tweaks": self._tweak_info(),
            "settings": {"watch": self._settings.get("watch", True),
                         "purge_on_start": self._settings.get("purge_on_start", True)},
        }

    def poll(self, log_cursor: int = 0) -> dict:
        """Живые данные для дашборда + новые строки журнала."""
        mem = psutil.virtual_memory()
        running = set()
        names = {p.process: p.key for p in PROFILES}
        for proc in psutil.process_iter(["name"]):
            key = names.get((proc.info.get("name") or "").lower())
            if key:
                running.add(key)
        with self._log_lock:
            logs = self._logs[log_cursor:]
        return {
            "cpu": psutil.cpu_percent(None),
            "ram": mem.percent, "ram_used": mem.total - mem.available,
            "running": sorted(running),
            "logs": logs,
        }

    def set_selected(self, tweak_id: str, value: bool) -> None:
        self._settings.setdefault("selected", {})[tweak_id] = bool(value)
        self._save_settings()

    def set_setting(self, name: str, value: bool) -> None:
        if name not in ("watch", "purge_on_start"):
            return
        self._settings[name] = bool(value)
        self._save_settings()
        if name == "watch":
            if value:
                self._start_watch()
                self._log("Авто-режим включён: жду запуска CS2 / Dota 2", "ok")
            elif self._watcher is not None:
                self._watcher.stop()
                self._watcher = None
                self._log("Авто-режим выключен")

    def boost(self, ids: list[str]) -> dict:
        if not self._busy.acquire(blocking=False):
            return {"busy": True}
        try:
            chosen = [t for t in self._tweaks if t.id in set(ids) and t.available()]
            self._log(f"BOOST — оптимизаций: {len(chosen)}", "head")
            ok, errors, reboot = apply_tweaks(chosen, self._backup, self._log_plain)
            if self._admin:
                try:
                    purge_standby_memory()
                    self._log("✔ ОЗУ очищена", "ok")
                except OSError as exc:
                    self._log(f"✖ Очистка ОЗУ: {exc}", "err")
            self._log(f"Готово: {ok} из {len(chosen)}", "ok" if not errors else "warn")
            return {"ok": ok, "total": len(chosen), "errors": errors, "reboot": reboot,
                    "tweaks": self._tweak_info()}
        finally:
            self._busy.release()

    def revert(self) -> dict:
        if not self._busy.acquire(blocking=False):
            return {"busy": True}
        try:
            self._log("Откат всех изменений", "head")
            errors = revert_all(self._backup, self._log_plain)
            self._log("✔ Настройки возвращены" if not errors else "Откат выполнен частично",
                      "ok" if not errors else "warn")
            return {"errors": errors, "tweaks": self._tweak_info()}
        finally:
            self._busy.release()

    def purge_memory(self) -> dict:
        before = psutil.virtual_memory().available
        try:
            purge_standby_memory()
        except PermissionError:
            self._log("✖ Очистка ОЗУ: нужны права администратора", "err")
            return {"ok": False, "error": "Нужны права администратора"}
        except OSError as exc:
            self._log(f"✖ Очистка ОЗУ: {exc}", "err")
            return {"ok": False, "error": str(exc)}
        freed = max(0, psutil.virtual_memory().available - before)
        self._log(f"✔ Standby-кэш очищен (+{freed / 2**20:.0f} МБ свободно)", "ok")
        return {"ok": True, "freed": freed}

    def clean_temp(self) -> dict:
        self._log("Очистка временных файлов старше суток…")
        count, size = clean_temp()
        self._log(f"✔ Удалено файлов: {count}, освобождено {size / 2**20:.1f} МБ", "ok")
        return {"count": count, "size": size}

    def get_startup(self) -> list[dict]:
        try:
            self._startup_cache = list_startup()
        except OSError as exc:
            self._log(f"✖ Автозагрузка: {exc}", "err")
            self._startup_cache = []
        return [{"idx": i, "name": s.name, "command": s.command, "enabled": s.enabled,
                 "locked": s.root == "HKLM" and not self._admin}
                for i, s in enumerate(self._startup_cache)]

    def set_startup(self, idx: int, enabled: bool) -> dict:
        try:
            item = self._startup_cache[idx]
            set_startup_enabled(item, bool(enabled))
        except (IndexError, OSError) as exc:
            self._log(f"✖ Автозагрузка: {exc}", "err")
            return {"ok": False}
        self._log(f"Автозагрузка «{item.name}»: {'вкл.' if enabled else 'выкл.'}", "ok")
        return {"ok": True}

    # окно без рамки — свои кнопки в заголовке
    def minimize(self) -> None:
        if self._window:
            self._window.minimize()

    def close(self) -> None:
        if self._watcher is not None:
            self._watcher.stop()
        if self._window:
            self._window.destroy()


def _cpu_name() -> str:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
            return winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
    except (ImportError, OSError):
        import platform
        return platform.processor() or "CPU"
