"""Проверяет, что загрузчик PyInstaller собран из исходников, а не взят готовым.

Готовый загрузчик из официального пакета одинаков у тысяч программ (в том числе
вредоносных), поэтому антивирусы его подозревают. Сравниваем хеш с официальным.
"""

import hashlib
import sys
from pathlib import Path

import PyInstaller

# SHA256 runw.exe из официального pyinstaller-6.22.3-py3-none-win_amd64.whl
OFFICIAL = {"2291f269c3a3804fde1079462239e09a8b32fffbeeaa8f628a531faf5be77d41"}

runw = Path(PyInstaller.__file__).parent / "bootloader" / "Windows-64bit-intel" / "runw.exe"
digest = hashlib.sha256(runw.read_bytes()).hexdigest()
print(f"bootloader {runw.name}: {digest}")
if digest in OFFICIAL:
    sys.exit("Загрузчик совпадает с официальным — он не был собран из исходников")
print("OK: собственный загрузчик")
