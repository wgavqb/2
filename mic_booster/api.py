"""Мост HTML-интерфейса и звукового движка (pywebview js_api)."""

from __future__ import annotations

import json
import os
import sys
import threading
import webbrowser
from pathlib import Path

from . import APP_NAME, __version__
from .dsp import PRESETS, Settings
from .engine import AudioEngine

VBCABLE_URL = "https://vb-audio.com/Cable/"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def data_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    path = Path(base) / "MicPro"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _find(devices: list[dict], *needles: str) -> dict | None:
    for dev in devices:
        name = dev["name"].lower()
        if any(n in name for n in needles):
            return dev
    return None


class Api:
    def __init__(self):
        self._engine = AudioEngine()
        self._path = data_dir() / "settings.json"
        self._cfg = self._load()
        self._engine.settings = Settings.from_dict(self._cfg.get("dsp", PRESETS["stream"]))
        self._engine.low_latency = bool(self._cfg.get("low_latency", False))
        self._engine.monitor_volume = float(self._cfg.get("monitor_volume", 0.8))
        self._window = None
        self._visible = True
        self._devices = {"inputs": [], "outputs": []}
        self._lock = threading.Lock()
        self.refresh_devices()

    # --- служебное --------------------------------------------------------------------

    def _attach(self, window) -> None:
        self._window = window

    def _load(self) -> dict:
        try:
            return json.loads(self._path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        self._cfg["dsp"] = self._engine.settings.to_dict()
        try:
            self._path.write_text(json.dumps(self._cfg, ensure_ascii=False, indent=2), "utf-8")
        except OSError:
            pass

    def _by_name(self, kind: str, name: str | None) -> dict | None:
        if not name:
            return None
        return next((d for d in self._devices[kind] if d["name"] == name), None)

    def _selection(self) -> dict:
        ins, outs = self._devices["inputs"], self._devices["outputs"]
        mic = (self._by_name("inputs", self._cfg.get("input"))
               or _find(ins, "fifine", "am8")
               or next((d for d in ins if d["id"] == self._devices.get("default_in")), None)
               or (ins[0] if ins else None))
        cable = self._by_name("outputs", self._cfg.get("output")) or _find(outs, "cable input", "vb-audio")
        monitor = self._by_name("outputs", self._cfg.get("monitor")) if self._cfg.get("monitor_on") else None
        return {"input": mic, "output": cable, "monitor": monitor}

    # --- вызывается из JS -------------------------------------------------------------------

    def refresh_devices(self) -> dict:
        try:
            self._devices = AudioEngine.devices()
        except Exception as exc:  # noqa: BLE001 — нет PortAudio / звуковой карты
            self._devices = {"inputs": [], "outputs": [], "error": str(exc)}
        return self._devices

    def get_state(self) -> dict:
        sel = self._selection()
        outs = self._devices["outputs"]
        return {
            "app": APP_NAME, "version": __version__,
            "devices": self._devices,
            "selected": {k: (v["name"] if v else None) for k, v in sel.items()},
            "monitor_on": bool(self._cfg.get("monitor_on", False)),
            "monitor_volume": self._engine.monitor_volume,
            "am8_found": _find(self._devices["inputs"], "fifine", "am8") is not None,
            "vbcable_found": _find(outs, "cable input", "vb-audio") is not None,
            "settings": self._engine.settings.to_dict(),
            "presets": list(PRESETS),
            "preset": self._cfg.get("preset", "stream"),
            "low_latency": self._engine.low_latency,
            "autostart": self._autostart_enabled(),
            "running": self._engine.running,
            "was_running": bool(self._cfg.get("was_running", False)),
        }

    def start(self) -> dict:
        with self._lock:
            sel = self._selection()
            if not sel["input"]:
                return {"ok": False, "error": "Микрофон не найден"}
            if not sel["output"] and not sel["monitor"]:
                return {"ok": False, "error": "Не выбран вывод: установите VB-Cable или включите прослушку"}
            try:
                self._engine.start(sel["input"]["id"],
                                   sel["output"]["id"] if sel["output"] else None,
                                   sel["monitor"]["id"] if sel["monitor"] else None)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": str(exc)}
            self._cfg["was_running"] = True
            self._save()
            return {"ok": True, "latency_ms": self._engine.latency_ms}

    def stop(self) -> dict:
        with self._lock:
            self._engine.stop()
            self._cfg["was_running"] = False
            self._save()
            return {"ok": True}

    def _restart_if_running(self) -> dict | None:
        if self._engine.running:
            return self.start()
        return None

    def set_device(self, kind: str, name: str | None) -> dict | None:
        if kind not in ("input", "output", "monitor"):
            return None
        self._cfg[kind] = name
        self._save()
        return self._restart_if_running()

    def set_monitor(self, on: bool, volume: float | None = None) -> dict | None:
        if volume is not None:
            self._engine.monitor_volume = max(0.0, min(1.5, float(volume)))
            self._cfg["monitor_volume"] = self._engine.monitor_volume
        changed = bool(on) != bool(self._cfg.get("monitor_on", False))
        self._cfg["monitor_on"] = bool(on)
        if on and not self._cfg.get("monitor"):
            default = next((d for d in self._devices["outputs"]
                            if d["id"] == self._devices.get("default_out")), None)
            if default and "cable" not in default["name"].lower():
                self._cfg["monitor"] = default["name"]
        self._save()
        return self._restart_if_running() if changed else None

    def set_param(self, name: str, value) -> None:
        self._engine.settings.update({name: value})
        self._engine.apply(self._engine.settings)
        self._cfg["preset"] = "custom"
        self._save()

    def apply_preset(self, name: str) -> dict:
        preset = PRESETS.get(name)
        if preset:
            s = self._engine.settings
            s.update({**preset, "bypass": s.bypass})
            self._engine.apply(s)
            self._cfg["preset"] = name
            self._save()
        return self._engine.settings.to_dict()

    def set_bypass(self, on: bool) -> None:
        self._engine.settings.bypass = bool(on)
        self._engine.apply(self._engine.settings)

    def set_low_latency(self, on: bool) -> dict | None:
        self._engine.low_latency = bool(on)
        self._cfg["low_latency"] = bool(on)
        self._save()
        return self._restart_if_running()

    def set_visible(self, visible: bool) -> None:
        self._visible = bool(visible)

    def poll(self) -> dict:
        # в свёрнутом окне не считаем спектр — программа почти не тратит CPU
        return self._engine.snapshot(with_spectrum=self._visible)

    def open_url(self, url: str) -> None:
        if url.startswith("https://"):
            webbrowser.open(url)

    def open_sound_settings(self) -> None:
        if sys.platform == "win32":
            os.startfile("ms-settings:sound")  # noqa: S606

    # --- автозапуск ---------------------------------------------------------------

    def _autostart_enabled(self) -> bool:
        if sys.platform != "win32":
            return False
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                winreg.QueryValueEx(key, APP_NAME)
            return True
        except OSError:
            return False

    def set_autostart(self, on: bool) -> bool:
        if sys.platform != "win32":
            return False
        import winreg
        exe = sys.executable if getattr(sys, "frozen", False) else None
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if on and exe:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, f'"{exe}" --minimized')
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except OSError:
                    pass
        return self._autostart_enabled()

    # --- окно ---------------------------------------------------------------------

    def minimize(self) -> None:
        if self._window:
            self._window.minimize()

    def close(self) -> None:
        self._engine.stop()
        if self._window:
            self._window.destroy()
