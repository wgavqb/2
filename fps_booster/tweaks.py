"""Оптимизации. Каждая сохраняет исходное состояние и полностью откатывается.

Ни одна из них не снижает настройки графики в играх.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import sys

from . import games as games_mod
from . import nvidia
from .backup import Backup
from .gpu import GPU, supports_hags
from .winutil import IS_WINDOWS, REG_DWORD, REG_SZ, reg_read, reg_restore, reg_write, run

Log = Callable[[str], None]

GUID_RE = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")
ULTIMATE_PERFORMANCE = "e9a42b02-d5df-448d-aa00-03f14749eb61"
HIGH_PERFORMANCE = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"


@dataclass(frozen=True)
class RegValue:
    root: str
    path: str
    name: str
    value: object
    type: int = REG_DWORD

    @property
    def key(self) -> str:
        return f"reg|{self.root}|{self.path}|{self.name}"


class Tweak:
    id = ""
    title = ""
    description = ""
    category = "game"  # "game" — для игр, "gpu" — для видеокарты, "system" — для всего ПК
    default = True
    needs_admin = False
    needs_reboot = False
    unavailable_reason = "игра не найдена"

    def available(self) -> bool:
        return True

    def backup_keys(self) -> list[str]:
        return []

    def is_applied(self, backup: Backup) -> bool:
        return any(key in backup for key in self.backup_keys())

    def apply(self, backup: Backup, log: Log) -> None:
        raise NotImplementedError

    def revert(self, backup: Backup, log: Log) -> None:
        for key in self.backup_keys():
            restore_key(key, backup, log)


class RegistryTweak(Tweak):
    def __init__(self, id, title, description, values, *, category="game",
                 default=True, needs_reboot=False):
        self.id, self.title, self.description = id, title, description
        self.values: list[RegValue] = list(values)
        self.category = category
        self.default = default
        self.needs_reboot = needs_reboot
        self.needs_admin = any(v.root == "HKLM" for v in self.values)

    def available(self) -> bool:
        return bool(self.values)

    def backup_keys(self) -> list[str]:
        return [v.key for v in self.values]

    def apply(self, backup: Backup, log: Log) -> None:
        for v in self.values:
            original = reg_read(v.root, v.path, v.name)
            reg_write(v.root, v.path, v.name, v.value, v.type)
            # запоминаем только после успешной записи, иначе твик считался бы применённым
            backup.remember(v.key, original)


class PowerPlanTweak(Tweak):
    id = "power_plan"
    title = "Схема питания «Максимальная производительность»"
    description = ("Процессор не сбрасывает частоты и не «засыпает» между кадрами — "
                   "меньше просадок и фризов. На ноутбуке играйте от сети.")
    category = "system"

    def backup_keys(self) -> list[str]:
        return ["power|active", "power|created"]

    def is_applied(self, backup: Backup) -> bool:
        return "power|active" in backup

    def apply(self, backup: Backup, log: Log) -> None:
        current = active_power_scheme()
        schemes = set(GUID_RE.findall(run("powercfg", "/list").lower()))
        target = None
        created = backup.get("power|created")
        if created and created in schemes:
            target = created
        elif ULTIMATE_PERFORMANCE in schemes:
            target = ULTIMATE_PERFORMANCE
        else:
            try:
                out = run("powercfg", "/duplicatescheme", ULTIMATE_PERFORMANCE)
                found = GUID_RE.findall(out)
                if found:
                    target = found[-1].lower()
                    backup.set("power|created", target)
            except RuntimeError as exc:
                log(f"  Ultimate Performance недоступна ({exc}), пробую High Performance")
        if target is None and HIGH_PERFORMANCE in schemes:
            target = HIGH_PERFORMANCE
        if target is None:
            raise RuntimeError("в системе нет схемы высокой производительности "
                               "(ноутбук с Modern Standby) — оставлена текущая")
        run("powercfg", "/setactive", target)
        if current:
            backup.remember("power|active", current)


DX_PREFS = r"Software\Microsoft\DirectX\UserGpuPreferences"


class GpuPreferenceTweak(RegistryTweak):
    """Windows «Настройки графики» → нужная видеокарта для exe игры."""

    def __init__(self, installed: list[games_mod.InstalledGame], gpu: GPU | None):
        # 2 — «Высокая производительность» (дискретная), 1 — «Энергосбережение» (встроенная)
        pref = 1 if gpu is not None and gpu.integrated else 2
        values = [RegValue("HKCU", DX_PREFS, str(g.exe_path), f"GpuPreference={pref};", REG_SZ)
                  for g in installed]
        target = gpu.name if gpu else "мощной видеокарте"
        super().__init__(
            "gpu_pref",
            f"Игры всегда на {target}" if gpu else "Игры всегда на мощной видеокарте",
            "CS2 и Dota 2 запускаются строго на выбранной карте. Особенно важно для ноутбуков "
            "с двумя GPU: игра не уйдёт случайно на слабую встроенную графику.",
            values, category="gpu",
        )


def merge_dx_settings(current: str, updates: dict[str, str]) -> str:
    """'VRROptimizeEnable=0;' + {'SwapEffectUpgradeEnable': '1'} → обе настройки, порядок сохраняется."""
    pairs: dict[str, str] = {}
    for item in (current or "").split(";"):
        if "=" in item:
            key, value = item.split("=", 1)
            pairs[key.strip()] = value.strip()
    pairs.update(updates)
    return "".join(f"{k}={v};" for k, v in pairs.items())


class WindowedOptimizationTweak(RegistryTweak):
    """Windows 11 → «Оптимизации для игр в окне»: современная цепочка кадров (flip model)."""

    unavailable_reason = "нужна Windows 11"

    def __init__(self):
        super().__init__(
            "windowed_opt",
            "Оптимизация игр в окне (Windows 11)",
            "Для режима «Окно без рамки»: меньше задержка и выше FPS, работает как полноэкранный. "
            "Картинку не меняет. Подходит для всех видеокарт.",
            [RegValue("HKCU", DX_PREFS, "DirectXUserGlobalSettings", "", REG_SZ)], category="gpu",
        )

    def available(self) -> bool:
        return not IS_WINDOWS or sys.getwindowsversion().build >= 22000

    def apply(self, backup: Backup, log: Log) -> None:
        v = self.values[0]
        original = reg_read(v.root, v.path, v.name)
        current = str(original["value"]) if original.get("exists") else ""
        reg_write(v.root, v.path, v.name, merge_dx_settings(current, {"SwapEffectUpgradeEnable": "1"}), REG_SZ)
        backup.remember(v.key, original)


class NvidiaProfileTweak(Tweak):
    id = "nvidia_profile"
    title = "Драйвер NVIDIA: максимальная производительность для CS2 и Dota 2"
    description = ("Как в «Панели управления NVIDIA», только автоматически и лишь для этих игр: "
                   "электропитание — «Максимальная производительность», режим низкой задержки — «Вкл.». "
                   "Карта не сбрасывает частоты, меньше input lag. Графика не меняется.")
    category = "gpu"
    needs_admin = True
    unavailable_reason = "только для видеокарт NVIDIA"

    def __init__(self, gpu: GPU | None):
        self.gpu = gpu

    def available(self) -> bool:
        return self.gpu is not None and self.gpu.vendor == "nvidia" and (nvidia.available() or not IS_WINDOWS)

    def backup_keys(self) -> list[str]:
        return [f"nv|{p.process}" for p in games_mod.PROFILES]

    def apply(self, backup: Backup, log: Log) -> None:
        for profile in games_mod.PROFILES:
            info = nvidia.apply_game(profile.process)
            # при повторном BOOST исходные значения в бэкапе не перезаписываются
            backup.remember(f"nv|{profile.process}", info)
            log(f"  {profile.name}: профиль драйвера NVIDIA обновлён")


class AutoexecTweak(Tweak):
    id = "autoexec"
    title = "Безопасный autoexec.cfg для CS2 / Dota 2"
    description = ("Снимает лимит FPS в игре, ограничивает FPS в меню, отключает лишнюю "
                   "анимацию меню. Графику не трогает. Ваш конфиг сохраняется.")

    def __init__(self, installed: list[games_mod.InstalledGame]):
        self.games = list(installed)

    def available(self) -> bool:
        return bool(self.games)

    def backup_keys(self) -> list[str]:
        return [f"cfg|{g.autoexec_path}" for g in self.games]

    def apply(self, backup: Backup, log: Log) -> None:
        for game in self.games:
            path = games_mod.apply_autoexec(game, backup)
            log(f"  {game.profile.name}: {path}")


def build_tweaks(installed: list[games_mod.InstalledGame], gpu: GPU | None = None) -> list[Tweak]:
    """Набор оптимизаций под найденные игры и выбранную видеокарту."""
    mmcss = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile"
    hags_ok = gpu is not None and supports_hags(gpu)
    return [
        GpuPreferenceTweak(installed, gpu),
        NvidiaProfileTweak(gpu),
        WindowedOptimizationTweak(),
        RegistryTweak(
            "hags", "Аппаратное планирование GPU (HAGS)",
            ("Видеокарта сама управляет своей памятью: ниже задержка и нагрузка на процессор. "
             + ("Ваша карта поддерживает — рекомендуем. " if hags_ok else
                "Для старых карт не рекомендуется. ")
             + "Нужна перезагрузка."),
            [RegValue("HKLM", r"SYSTEM\CurrentControlSet\Control\GraphicsDrivers", "HwSchMode", 2)],
            category="gpu", default=hags_ok, needs_reboot=True,
        ),
        AutoexecTweak(installed),
        RegistryTweak(
            "game_mode", "Игровой режим Windows",
            "Windows отдаёт игре приоритет по CPU/GPU и не ставит обновления во время игры.",
            [RegValue("HKCU", r"Software\Microsoft\GameBar", "AutoGameModeEnabled", 1),
             RegValue("HKCU", r"Software\Microsoft\GameBar", "AllowAutoGameMode", 1)],
        ),
        RegistryTweak(
            "game_dvr", "Отключить фоновую запись Xbox Game DVR",
            "Фоновая запись геймплея постоянно ест GPU и диск. Оверлей Game Bar останется.",
            [RegValue("HKCU", r"System\GameConfigStore", "GameDVR_Enabled", 0),
             RegValue("HKCU", r"Software\Microsoft\Windows\CurrentVersion\GameDVR", "AppCaptureEnabled", 0)],
        ),
        RegistryTweak(
            "mmcss", "Приоритет игр в планировщике Windows",
            "Повышает приоритет игровых потоков (MMCSS) и убирает ограничение сети "
            "для мультимедиа — ровнее фреймтайм и пинг.",
            [RegValue("HKLM", mmcss, "SystemResponsiveness", 10),
             RegValue("HKLM", mmcss, "NetworkThrottlingIndex", 0xFFFFFFFF),
             RegValue("HKLM", mmcss + r"\Tasks\Games", "GPU Priority", 8),
             RegValue("HKLM", mmcss + r"\Tasks\Games", "Priority", 6),
             RegValue("HKLM", mmcss + r"\Tasks\Games", "Scheduling Category", "High", REG_SZ),
             RegValue("HKLM", mmcss + r"\Tasks\Games", "SFIO Priority", "High", REG_SZ)],
            category="system",
        ),
        PowerPlanTweak(),
        RegistryTweak(
            "power_throttling", "Отключить Power Throttling",
            "Windows перестаёт урезать частоты «фоновым» процессам (Steam, Discord, "
            "античит). Полезно для ноутбуков; от батареи тратит больше заряда.",
            [RegValue("HKLM", r"SYSTEM\CurrentControlSet\Control\Power\PowerThrottling",
                      "PowerThrottlingOff", 1)],
            category="system",
        ),
        RegistryTweak(
            "background_apps", "Запретить фоновую работу приложений Store",
            "Приложения из Microsoft Store (Почта, Погода, Xbox и т.п.) не работают в фоне "
            "и не едят CPU/ОЗУ. Уведомления от них могут приходить позже.",
            [RegValue("HKCU", r"Software\Microsoft\Windows\CurrentVersion\BackgroundAccessApplications",
                      "GlobalUserDisabled", 1),
             RegValue("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Search",
                      "BackgroundAppGlobalToggle", 0)],
            category="system",
        ),
        RegistryTweak(
            "ui_fast", "Быстрый интерфейс Windows",
            "Меню открываются без задержки, окна сворачиваются без анимации. "
            "Внешний вид Windows почти не меняется. Действует после перезахода.",
            [RegValue("HKCU", r"Control Panel\Desktop", "MenuShowDelay", "0", REG_SZ),
             RegValue("HKCU", r"Control Panel\Desktop\WindowMetrics", "MinAnimate", "0", REG_SZ),
             RegValue("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
                      "TaskbarAnimations", 0)],
            category="system", needs_reboot=True,
        ),
        RegistryTweak(
            "transparency", "Отключить прозрачность Windows",
            "Немного разгружает видеокарту на рабочем столе. Панель задач и меню станут "
            "непрозрачными (на игры не влияет).",
            [RegValue("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
                      "EnableTransparency", 0)],
            category="system", default=False,
        ),
    ]


# --- откат ----------------------------------------------------------------------

def active_power_scheme() -> str | None:
    found = GUID_RE.findall(run("powercfg", "/getactivescheme"))
    return found[0].lower() if found else None


def restore_key(key: str, backup: Backup, log: Log) -> None:
    if key not in backup:
        return
    original = backup.get(key)
    kind = key.split("|", 1)[0]
    if kind == "reg":
        _, root, path, name = key.split("|", 3)
        reg_restore(root, path, name, original)
    elif kind == "cfg":
        games_mod.restore_autoexec(Path(key[len("cfg|"):]), original)
    elif kind == "nv":
        if nvidia.available():  # драйвер NVIDIA удалён — откатывать нечего
            nvidia.revert_game(key[len("nv|"):], original or {})
    elif key == "power|active":
        if original:
            run("powercfg", "/setactive", original)
        created = backup.get("power|created")
        if created and created != original:
            run("powercfg", "/delete", created, check=False)
        backup.forget("power|created")
    elif key == "power|created":
        if original and original != active_power_scheme():
            run("powercfg", "/delete", original, check=False)
    backup.forget(key)


def apply_tweaks(tweaks: list[Tweak], backup: Backup, log: Log) -> tuple[int, list[str], bool]:
    """Применяет твики. Возвращает (успешно, [ошибки], нужна_перезагрузка)."""
    ok, errors, reboot = 0, [], False
    for tweak in tweaks:
        if not tweak.available():
            continue
        log(f"▶ {tweak.title}")
        try:
            tweak.apply(backup, log)
        except PermissionError:
            errors.append(f"{tweak.title}: нужны права администратора")
            log("  ✖ нужны права администратора")
            continue
        except Exception as exc:  # noqa: BLE001 — один сбой не должен ломать остальные
            errors.append(f"{tweak.title}: {exc}")
            log(f"  ✖ {exc}")
            continue
        ok += 1
        reboot = reboot or tweak.needs_reboot
        log("  ✔ готово")
    return ok, errors, reboot


def revert_all(backup: Backup, log: Log) -> list[str]:
    """Откатывает всё, что когда-либо было изменено (по файлу бэкапа)."""
    errors = []
    keys = backup.keys()
    keys.sort(key=lambda k: k != "power|active")  # сначала вернуть схему питания
    for key in keys:
        try:
            restore_key(key, backup, log)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{key}: {exc}")
            log(f"  ✖ {key}: {exc}")
    return errors
