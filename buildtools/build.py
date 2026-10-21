"""Сборка FPSBooster.exe и MicPro.exe (используется и в GitHub Actions, и в build.bat).

Чтобы антивирусы реже ошибались, exe собирается с иконкой и сведениями о версии,
а в GitHub Actions ещё и со своим загрузчиком PyInstaller, собранным из исходников.
"""

import hashlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import PyInstaller.__main__  # noqa: E402

from buildtools.make_version_info import write  # noqa: E402
from fps_booster import __version__ as fps_version  # noqa: E402
from mic_booster import __version__ as mic_version  # noqa: E402

APPS = [
    {"name": "FPSBooster", "entry": "run.py", "package": "fps_booster", "admin": True,
     "version": fps_version, "description": "FPS Booster - оптимизация Windows для CS2 и Dota 2"},
    {"name": "MicPro", "entry": "run_mic.py", "package": "mic_booster", "admin": False,
     "version": mic_version, "description": "MicPro - обработка голоса микрофона в реальном времени"},
]


def build(app: dict) -> Path:
    version_file = write(app["name"], app["version"], app["description"])
    args = [
        str(ROOT / app["entry"]),
        "--noconfirm", "--clean", "--onefile", "--windowed",
        "--name", app["name"],
        "--icon", str(ROOT / "buildtools" / f"{app['name'].lower()}.ico"),
        "--version-file", str(version_file),
        "--add-data", f"{ROOT / app['package'] / 'ui'}{os.pathsep}{app['package']}/ui",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
    ]
    if app["admin"]:
        args.append("--uac-admin")  # честно просим права администратора манифестом
    PyInstaller.__main__.run(args)
    return ROOT / "dist" / f"{app['name']}.exe"


def main() -> None:
    lines = []
    for app in APPS:
        exe = build(app)
        digest = hashlib.sha256(exe.read_bytes()).hexdigest()
        lines.append(f"{digest}  {exe.name}")
    (ROOT / "dist" / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", "utf-8")
    notes = [
        "**FPSBooster.exe** — больше FPS в CS2 и Dota 2 без потери качества, с настройкой под вашу видеокарту "
        "(NVIDIA, AMD, Intel). Запросит права администратора.",
        "**MicPro.exe** — студийный звук для микрофона (FIFINE AM8 и любых других). Нужен бесплатный VB-Cable.",
        "",
        "Windows 10/11. Программы с открытым кодом, собраны GitHub Actions из этого репозитория и не выходят в интернет.",
        "Если антивирус ругается — это ложное срабатывание на упаковщик PyInstaller; сверьте SHA256 файла:",
        "",
        "```",
        *lines,
        "```",
    ]
    (ROOT / "dist" / "RELEASE_NOTES.md").write_text("\n".join(notes) + "\n", "utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
