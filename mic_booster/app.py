"""Окно MicPro (pywebview → Edge WebView2)."""

from __future__ import annotations

import sys
from pathlib import Path

from . import APP_NAME
from .api import Api


def ui_file() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "mic_booster" / "ui" / "index.html"


def run(minimized: bool = False) -> None:
    import webview

    api = Api()
    html = ui_file().read_text("utf-8").replace('<html lang="ru">', '<html lang="ru" data-app>', 1)
    window = webview.create_window(
        APP_NAME, html=html, js_api=api,
        width=1240, height=820, min_size=(1000, 680),
        frameless=True, easy_drag=False, background_color="#05060a",
        minimized=minimized,
    )
    api._attach(window)
    webview.start()
