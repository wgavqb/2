"""Хранилище исходных значений, чтобы любое изменение можно было откатить."""

from __future__ import annotations

import json
import os
from pathlib import Path


def data_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    path = Path(base) / "FPSBooster"
    path.mkdir(parents=True, exist_ok=True)
    return path


class Backup:
    """JSON-файл вида {ключ: исходное значение}.

    `remember` сохраняет значение только при первом вызове — повторное
    применение твика не должно затирать настоящее исходное состояние.
    """

    def __init__(self, path: str | os.PathLike | None = None):
        self.path = Path(path) if path else data_dir() / "backup.json"
        self.data: dict = self._load()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), "utf-8")
        os.replace(tmp, self.path)

    def remember(self, key: str, value) -> None:
        if key not in self.data:
            self.data[key] = value
            self.save()

    def set(self, key: str, value) -> None:
        self.data[key] = value
        self.save()

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def forget(self, key: str) -> None:
        if key in self.data:
            del self.data[key]
            self.save()

    def keys(self) -> list[str]:
        return list(self.data)

    def __contains__(self, key: str) -> bool:
        return key in self.data
