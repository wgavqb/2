"""Запуск окна приложения (pywebview → Edge WebView2 на Windows)."""

from __future__ import annotations

import sys
from pathlib import Path

from . import APP_NAME
from .api import Api


def ui_file() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "fps_booster" / "ui" / "index.html"


def run() -> None:
    import webview

    api = Api()
    # атрибут data-app говорит странице, что она внутри приложения, а не в браузере (демо)
    html = ui_file().read_text("utf-8").replace('<html lang="ru">', '<html lang="ru" data-app>', 1)
    window = webview.create_window(
        APP_NAME, html=html, js_api=api,
        width=1240, height=820, min_size=(1000, 680),
        frameless=True, easy_drag=False, background_color="#06070b",
    )
    api._attach(window)
    webview.start()
