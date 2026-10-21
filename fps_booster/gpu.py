"""Определение видеокарт (NVIDIA, AMD, Intel) и сведения о драйвере.

Список карт берётся из DirectX (DXGI): там только реально установленные
адаптеры с честным объёмом видеопамяти. Версию и дату драйвера дочитываем
из реестра класса видеоадаптеров.
"""

from __future__ import annotations

import ctypes
import os
import re
from dataclasses import asdict, dataclass
from datetime import date

from .winutil import IS_WINDOWS, reg_subkeys, reg_values

VENDORS = {0x10DE: "nvidia", 0x1002: "amd", 0x1022: "amd", 0x8086: "intel"}
SOFTWARE_VENDORS = {0x1414}  # Microsoft Basic Render Driver и прочие программные адаптеры
DISPLAY_CLASS = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"

VENDOR_INFO = {
    "nvidia": {
        "name": "NVIDIA",
        "driver_url": "https://www.nvidia.com/ru-ru/drivers/",
        "panel": "Панель управления NVIDIA",
        "tips": [
            "В CS2: Настройки → Видео → NVIDIA Reflex — «Включено + Boost».",
            "Панель NVIDIA → Параметры 3D → Размер кэша шейдеров — «Без ограничений»: меньше фризов при первой загрузке карт.",
            "Если монитор с G-SYNC: включите G-SYNC и V-Sync в панели NVIDIA, а в игре V-Sync выключите.",
            "Не включайте DLSS «Производительность» — это уже снижение качества. Режим «Качество» или DLAA картинку не портит.",
        ],
    },
    "amd": {
        "name": "AMD",
        "driver_url": "https://www.amd.com/ru/support/download/drivers.html",
        "panel": "AMD Software: Adrenalin Edition",
        "tips": [
            "Adrenalin → Игры → Графика: Radeon Anti-Lag — «Вкл.», Radeon Chill — «Выкл.».",
            "Radeon Boost — «Выкл.»: он понижает разрешение в движении, это потеря качества.",
            "Кэширование шейдеров — «Оптимизировано AMD», размер кэша не ограничивайте.",
            "Включите Smart Access Memory (Resizable BAR) в BIOS — до +10% FPS на RX 6000/7000/9000.",
            "Если монитор с FreeSync: включите FreeSync, а «Ожидание вертикальной синхронизации» — «Выкл., если не указано приложением».",
        ],
    },
    "intel": {
        "name": "Intel",
        "driver_url": "https://www.intel.com/content/www/us/en/support/detect.html",
        "panel": "Intel Graphics Software",
        "tips": [
            "Видеокарты Arc: обязательно включите Resizable BAR (ReBAR) в BIOS — без него FPS ниже на 20–40%.",
            "Intel Graphics Software → Графика → Режим низкой задержки — «Вкл.».",
            "Встроенная графика: две планки оперативной памяти (двухканальный режим) дают до +50% FPS.",
            "Встроенная графика: в BIOS выделите видеопамяти побольше (UMA Frame Buffer / DVMT) — меньше подтормаживаний.",
        ],
    },
    "other": {"name": "Видеокарта", "driver_url": "", "panel": "", "tips": []},
}

# Программы-«панели» производителей (что найдётся — то и откроем)
PANELS = {
    "nvidia": [
        r"%ProgramFiles%\NVIDIA Corporation\NVIDIA app\CEF\NVIDIA app.exe",
        r"%ProgramFiles%\NVIDIA Corporation\Control Panel Client\nvcplui.exe",
        r"shell:AppsFolder\NVIDIACorp.NVIDIAControlPanel_56jybvy8sckqj!NVIDIACorp.NVIDIAControlPanel",
    ],
    "amd": [r"%ProgramFiles%\AMD\CNext\CNext\RadeonSoftware.exe"],
    "intel": [
        r"%ProgramFiles%\Intel\Intel Graphics Software\IntelGraphicsSoftware.exe",
        r"shell:AppsFolder\AppUp.IntelArcSoftware_8j3eq9eme6ctt!App",
        r"shell:AppsFolder\AppUp.IntelGraphicsExperience_8j3eq9eme6ctt!App",
    ],
}


@dataclass
class GPU:
    id: str               # "10de:2684:<luid>" — стабильный ключ для выбора в интерфейсе
    name: str
    vendor: str           # nvidia | amd | intel | other
    vendor_id: int
    device_id: int
    vram: int             # байт выделенной видеопамяти
    integrated: bool
    driver_version: str = ""
    driver_date: str = ""  # ISO, YYYY-MM-DD

    @property
    def driver_age_days(self) -> int | None:
        if not self.driver_date:
            return None
        try:
            return (date.today() - date.fromisoformat(self.driver_date)).days
        except ValueError:
            return None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["vendor_name"] = VENDOR_INFO[self.vendor]["name"]
        data["driver_age_days"] = self.driver_age_days
        data["driver_display"] = display_driver_version(self.vendor, self.driver_version)
        data["supports_hags"] = supports_hags(self)
        return data


# --- чистые функции (покрыты тестами) ------------------------------------------

def vendor_of(vendor_id: int) -> str:
    return VENDORS.get(vendor_id, "other")


_AMD_IGPU = re.compile(r"Radeon\(TM\) Graphics|Radeon Graphics|Vega \d|\b\d{3}M\b", re.I)
_INTEL_DGPU = re.compile(r"Arc.*\b[AB]\d{3}\b", re.I)


def is_integrated(vendor: str, name: str, vram: int) -> bool:
    if vendor == "intel":
        return not _INTEL_DGPU.search(name)
    if vendor == "amd":
        return bool(_AMD_IGPU.search(name)) or vram < 1 << 30
    if vendor == "nvidia":
        return False
    return vram < 1 << 30


def parse_driver_date(raw: str) -> str:
    """'10-8-2025' (так пишет Windows) → '2025-10-08'."""
    m = re.fullmatch(r"\s*(\d{1,2})-(\d{1,2})-(\d{4})\s*", raw or "")
    if not m:
        return ""
    month, day, year = (int(x) for x in m.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return ""


def display_driver_version(vendor: str, raw: str) -> str:
    """У NVIDIA версия Windows '32.0.15.6094' — это драйвер 560.94."""
    parts = (raw or "").split(".")
    if vendor == "nvidia" and len(parts) == 4 and all(p.isdigit() for p in parts):
        num = (int(parts[2]) % 10) * 10000 + int(parts[3])
        return f"{num // 100}.{num % 100:02d}"
    return raw or ""


def supports_hags(gpu: GPU) -> bool:
    """Аппаратное планирование GPU стабильно на NVIDIA GTX 10xx+, AMD RX 5000+, Intel Arc/Xe."""
    name = gpu.name.upper()
    if gpu.vendor == "nvidia":
        if "RTX" in name:
            return True
        m = re.search(r"GTX\s*(\d{3,4})", name)
        return bool(m) and int(m.group(1)) >= 1000
    if gpu.vendor == "amd":
        m = re.search(r"RX\s*(\d{4})", name)
        return bool(m) and int(m.group(1)) >= 5000
    if gpu.vendor == "intel":
        return "ARC" in name or "IRIS XE" in name
    return False


def pick_best(gpus: list[GPU]) -> GPU | None:
    """Для игр: дискретная карта с наибольшей видеопамятью, иначе встроенная."""
    if not gpus:
        return None
    return max(gpus, key=lambda g: (not g.integrated, g.vram))


# --- DXGI ------------------------------------------------------------------------

class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", ctypes.c_uint32), ("HighPart", ctypes.c_int32)]


class _DXGI_ADAPTER_DESC1(ctypes.Structure):
    _fields_ = [
        ("Description", ctypes.c_wchar * 128),
        ("VendorId", ctypes.c_uint),
        ("DeviceId", ctypes.c_uint),
        ("SubSysId", ctypes.c_uint),
        ("Revision", ctypes.c_uint),
        ("DedicatedVideoMemory", ctypes.c_size_t),
        ("DedicatedSystemMemory", ctypes.c_size_t),
        ("SharedSystemMemory", ctypes.c_size_t),
        ("AdapterLuid", _LUID),
        ("Flags", ctypes.c_uint),
    ]


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]


# IID_IDXGIFactory1 {770aae78-f26f-4dba-a829-253c83d1b387}
_IID_FACTORY1 = _GUID(0x770AAE78, 0xF26F, 0x4DBA, (ctypes.c_ubyte * 8)(0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87))
_DXGI_ADAPTER_FLAG_SOFTWARE = 2
_DXGI_ERROR_NOT_FOUND = 0x887A0002


def _vcall(obj: ctypes.c_void_p, index: int, restype, *args):
    """Вызов метода COM-объекта по номеру в таблице виртуальных функций."""
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *[type(a) for a in args])
    return proto(vtbl[index])(obj, *args)


def _dxgi_adapters() -> list[dict]:
    dxgi = ctypes.WinDLL("dxgi")
    factory = ctypes.c_void_p()
    hr = dxgi.CreateDXGIFactory1(ctypes.byref(_IID_FACTORY1), ctypes.byref(factory))
    if hr != 0 or not factory:
        raise OSError(f"CreateDXGIFactory1: 0x{hr & 0xFFFFFFFF:08X}")
    adapters = []
    try:
        for i in range(16):
            adapter = ctypes.c_void_p()
            # IDXGIFactory1::EnumAdapters1 — 12-й метод
            hr = _vcall(factory, 12, ctypes.c_long, ctypes.c_uint(i), ctypes.pointer(adapter))
            if hr & 0xFFFFFFFF == _DXGI_ERROR_NOT_FOUND or hr != 0:
                break
            try:
                desc = _DXGI_ADAPTER_DESC1()
                # IDXGIAdapter1::GetDesc1 — 10-й метод
                if _vcall(adapter, 10, ctypes.c_long, ctypes.pointer(desc)) == 0:
                    adapters.append({
                        "name": desc.Description.strip(),
                        "vendor_id": desc.VendorId, "device_id": desc.DeviceId,
                        "vram": int(desc.DedicatedVideoMemory),
                        "luid": f"{desc.AdapterLuid.HighPart & 0xFFFFFFFF:08x}{desc.AdapterLuid.LowPart:08x}",
                        "software": bool(desc.Flags & _DXGI_ADAPTER_FLAG_SOFTWARE),
                    })
            finally:
                _vcall(adapter, 2, ctypes.c_ulong)  # Release
    finally:
        _vcall(factory, 2, ctypes.c_ulong)
    return adapters


def _driver_info() -> dict[tuple[int, int], tuple[str, str]]:
    """(vendor_id, device_id) → (версия драйвера, дата). Берём самую свежую установку."""
    info: dict[tuple[int, int], tuple[str, str]] = {}
    for sub in reg_subkeys("HKLM", DISPLAY_CLASS):
        if not sub.isdigit():
            continue
        values = {name: value for name, value, _t in reg_values("HKLM", rf"{DISPLAY_CLASS}\{sub}")}
        m = re.search(r"ven_([0-9a-f]{4})&dev_([0-9a-f]{4})", str(values.get("MatchingDeviceId", "")), re.I)
        if not m:
            continue
        key = (int(m.group(1), 16), int(m.group(2), 16))
        entry = (str(values.get("DriverVersion", "")), parse_driver_date(str(values.get("DriverDate", ""))))
        if key not in info or entry[1] > info[key][1]:
            info[key] = entry
    return info


def list_gpus() -> list[GPU]:
    if not IS_WINDOWS:
        return []
    try:
        adapters = _dxgi_adapters()
    except OSError:
        return []
    try:
        drivers = _driver_info()
    except OSError:
        drivers = {}
    gpus, seen = [], set()
    for a in adapters:
        if a["software"] or a["vendor_id"] in SOFTWARE_VENDORS:
            continue
        ident = f"{a['vendor_id']:04x}:{a['device_id']:04x}:{a['luid']}"
        if ident in seen:
            continue
        seen.add(ident)
        vendor = vendor_of(a["vendor_id"])
        version, ddate = drivers.get((a["vendor_id"], a["device_id"]), ("", ""))
        gpus.append(GPU(ident, a["name"], vendor, a["vendor_id"], a["device_id"], a["vram"],
                        is_integrated(vendor, a["name"], a["vram"]), version, ddate))
    gpus.sort(key=lambda g: (g.integrated, -g.vram))
    return gpus


def _store_app_installed(target: str) -> bool:
    """shell:AppsFolder\\Family_hash!App → установлен ли пакет Store (иначе Windows покажет ошибку)."""
    family = target.split("\\")[-1].split("!")[0].split("_")[0].lower()
    packages = reg_subkeys("HKCU", r"Software\Classes\ActivatableClasses\Package")
    return any(p.lower().startswith(family + "_") for p in packages)


def open_panel(vendor: str) -> bool:
    """Открывает фирменную программу производителя видеокарты, если она установлена."""
    if not IS_WINDOWS:
        return False
    for candidate in PANELS.get(vendor, []):
        target = os.path.expandvars(candidate)
        if target.startswith("shell:") and _store_app_installed(target) or os.path.isfile(target):
            try:
                os.startfile(target)  # noqa: S606
                return True
            except OSError:
                continue
    return False
