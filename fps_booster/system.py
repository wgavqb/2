"""Общее ускорение ПК: очистка памяти, временных файлов и автозагрузки."""

from __future__ import annotations

import ctypes
import os
import time
from dataclasses import dataclass
from pathlib import Path

from .winutil import IS_WINDOWS, REG_BINARY, reg_read, reg_values, reg_write

# --- очистка standby-памяти ----------------------------------------------------

_SYSTEM_MEMORY_LIST_INFORMATION = 80
_MEMORY_PURGE_STANDBY_LIST = 4
_SE_PRIVILEGE_ENABLED = 0x2
_ERROR_NOT_ALL_ASSIGNED = 1300


def purge_standby_memory() -> None:
    """Освобождает кэш standby (как RAMMap / ISLC). Убирает микрофризы при нехватке ОЗУ.

    Требуются права администратора.
    """
    if not IS_WINDOWS:
        raise OSError("только для Windows")
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

    class LUID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]

    class TOKEN_PRIVILEGES(ctypes.Structure):
        _fields_ = [("PrivilegeCount", wintypes.DWORD), ("Privileges", LUID_AND_ATTRIBUTES * 1)]

    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.LookupPrivilegeValueW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(LUID)]
    advapi32.AdjustTokenPrivileges.argtypes = [
        wintypes.HANDLE, wintypes.BOOL, ctypes.POINTER(TOKEN_PRIVILEGES),
        wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p,
    ]
    ntdll.NtSetSystemInformation.argtypes = [ctypes.c_int, ctypes.c_void_p, wintypes.ULONG]
    ntdll.NtSetSystemInformation.restype = ctypes.c_long

    token = wintypes.HANDLE()
    TOKEN_ADJUST_PRIVILEGES, TOKEN_QUERY = 0x20, 0x8
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(),
                                     TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        luid = LUID()
        if not advapi32.LookupPrivilegeValueW(None, "SeProfileSingleProcessPrivilege", ctypes.byref(luid)):
            raise ctypes.WinError(ctypes.get_last_error())
        privileges = TOKEN_PRIVILEGES(1, (LUID_AND_ATTRIBUTES * 1)(LUID_AND_ATTRIBUTES(luid, _SE_PRIVILEGE_ENABLED)))
        ok = advapi32.AdjustTokenPrivileges(token, False, ctypes.byref(privileges), 0, None, None)
        if not ok or ctypes.get_last_error() == _ERROR_NOT_ALL_ASSIGNED:
            raise PermissionError("нужны права администратора")
    finally:
        kernel32.CloseHandle(token)

    command = ctypes.c_int(_MEMORY_PURGE_STANDBY_LIST)
    status = ntdll.NtSetSystemInformation(_SYSTEM_MEMORY_LIST_INFORMATION,
                                          ctypes.byref(command), ctypes.sizeof(command))
    if status != 0:
        raise OSError(f"NtSetSystemInformation: NTSTATUS 0x{status & 0xFFFFFFFF:08X}")


# --- временные файлы ------------------------------------------------------------

def temp_dirs() -> list[Path]:
    dirs = []
    for candidate in (os.environ.get("TEMP"), os.environ.get("TMP"),
                      os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Temp") if IS_WINDOWS else None):
        if candidate and Path(candidate).is_dir():
            path = Path(candidate).resolve()
            if path not in dirs:
                dirs.append(path)
    return dirs


def clean_temp(dirs: list[Path] | None = None, min_age_hours: float = 24) -> tuple[int, int]:
    """Удаляет старые временные файлы. Занятые файлы молча пропускаются.

    Возвращает (количество удалённых файлов, освобождено байт).
    """
    cutoff = time.time() - min_age_hours * 3600
    removed = freed = 0
    for root_dir in dirs if dirs is not None else temp_dirs():
        for current, subdirs, files in os.walk(root_dir, topdown=False):
            for name in files:
                path = os.path.join(current, name)
                try:
                    st = os.lstat(path)
                    if st.st_mtime > cutoff:
                        continue
                    os.remove(path)
                except OSError:
                    continue
                removed += 1
                freed += st.st_size
            if current != str(root_dir):
                try:
                    os.rmdir(current)  # только если папка опустела
                except OSError:
                    pass
    return removed, freed


# --- автозагрузка ---------------------------------------------------------------

_RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_WOW = r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Run"
_APPROVED = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved"

# (корень, ключ Run, ключ StartupApproved) — так же, как это делает Диспетчер задач
_STARTUP_SOURCES = (
    ("HKCU", _RUN, _APPROVED + r"\Run"),
    ("HKLM", _RUN, _APPROVED + r"\Run"),
    ("HKLM", _RUN_WOW, _APPROVED + r"\Run32"),
)


@dataclass(frozen=True)
class StartupItem:
    root: str
    approved_key: str
    name: str
    command: str
    enabled: bool


def is_enabled_flag(data: bytes | None) -> bool:
    # 02/06 — включено, 03/07 — выключено (младший бит), нет записи — включено
    return not data or not (data[0] & 1)


def startup_flag(enabled: bool) -> bytes:
    return bytes([0x02 if enabled else 0x03]) + bytes(11)


def list_startup() -> list[StartupItem]:
    if not IS_WINDOWS:
        return []
    items = []
    for root, run_key, approved_key in _STARTUP_SOURCES:
        for name, command, _type in reg_values(root, run_key):
            if not name:
                continue
            info = reg_read(root, approved_key, name)
            data = bytes.fromhex(info["value"]) if info.get("bytes") else None
            items.append(StartupItem(root, approved_key, name, str(command), is_enabled_flag(data)))
    return items


def set_startup_enabled(item: StartupItem, enabled: bool) -> None:
    reg_write(item.root, item.approved_key, item.name, startup_flag(enabled), REG_BINARY)
