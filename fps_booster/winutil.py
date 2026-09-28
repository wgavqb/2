"""Обёртки над реестром, командами и правами администратора Windows."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    import winreg
else:  # модуль импортируется и на Linux, чтобы работали тесты
    winreg = None

REG_SZ = 1
REG_BINARY = 3
REG_DWORD = 4

CREATE_NO_WINDOW = 0x08000000


def _root(name: str):
    return {"HKCU": winreg.HKEY_CURRENT_USER, "HKLM": winreg.HKEY_LOCAL_MACHINE}[name]


def _view() -> int:
    # 64-битное представление реестра, даже если сборка 32-битная
    return winreg.KEY_WOW64_64KEY


def reg_read(root: str, path: str, name: str) -> dict:
    """Возвращает {"exists": False} или {"exists": True, "value": ..., "type": int}."""
    try:
        with winreg.OpenKey(_root(root), path, 0, winreg.KEY_READ | _view()) as key:
            value, value_type = winreg.QueryValueEx(key, name)
    except FileNotFoundError:
        return {"exists": False}
    if isinstance(value, bytes):
        return {"exists": True, "value": value.hex(), "type": value_type, "bytes": True}
    return {"exists": True, "value": value, "type": value_type}


def reg_write(root: str, path: str, name: str, value, value_type: int) -> None:
    with winreg.CreateKeyEx(_root(root), path, 0, winreg.KEY_SET_VALUE | _view()) as key:
        winreg.SetValueEx(key, name, 0, value_type, value)


def reg_delete(root: str, path: str, name: str) -> None:
    try:
        with winreg.OpenKey(_root(root), path, 0, winreg.KEY_SET_VALUE | _view()) as key:
            winreg.DeleteValue(key, name)
    except FileNotFoundError:
        pass


def reg_restore(root: str, path: str, name: str, original: dict) -> None:
    """Возвращает значение, сохранённое через reg_read."""
    if not original or not original.get("exists"):
        reg_delete(root, path, name)
        return
    value = original["value"]
    if original.get("bytes"):
        value = bytes.fromhex(value)
    reg_write(root, path, name, value, original["type"])


def reg_values(root: str, path: str) -> list[tuple[str, object, int]]:
    """Все значения ключа: [(имя, значение, тип)]."""
    result = []
    try:
        with winreg.OpenKey(_root(root), path, 0, winreg.KEY_READ | _view()) as key:
            i = 0
            while True:
                try:
                    result.append(winreg.EnumValue(key, i))
                except OSError:
                    break
                i += 1
    except FileNotFoundError:
        pass
    return result


def run(*args: str, check: bool = True) -> str:
    """Запуск консольной утилиты без мигающего окна."""
    proc = subprocess.run(
        list(args),
        capture_output=True,
        creationflags=CREATE_NO_WINDOW if IS_WINDOWS else 0,
    )
    # вывод powercfg локализован и в OEM-кодировке — нам нужны только GUID
    out = (proc.stdout or b"").decode("cp866" if IS_WINDOWS else "utf-8", errors="ignore")
    if check and proc.returncode != 0:
        err = (proc.stderr or b"").decode("cp866" if IS_WINDOWS else "utf-8", errors="ignore")
        raise RuntimeError(f"{' '.join(args)}: {(err or out).strip() or proc.returncode}")
    return out


def is_admin() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def relaunch_as_admin() -> bool:
    """Перезапускает приложение с запросом UAC. True — если новый процесс запущен."""
    if not IS_WINDOWS:
        return False
    if getattr(sys, "frozen", False):
        params = subprocess.list2cmdline(sys.argv[1:])
    else:
        params = subprocess.list2cmdline(["-m", "fps_booster", *sys.argv[1:]])
    workdir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, workdir, 1)
    return rc > 32
