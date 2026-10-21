"""Генерирует файлы ресурсов версии Windows для PyInstaller (--version-file).

Exe без сведений о версии, издателе и описании антивирусы оценивают как
подозрительный, поэтому заполняем их честно.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fps_booster import __version__ as fps_version  # noqa: E402
from mic_booster import __version__ as mic_version  # noqa: E402

TEMPLATE = """# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(filevers={tup}, prodvers={tup}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('041904B0', [
      StringStruct('CompanyName', 'wgavqb'),
      StringStruct('FileDescription', {description!r}),
      StringStruct('FileVersion', {version!r}),
      StringStruct('InternalName', {name!r}),
      StringStruct('LegalCopyright', 'Open source: https://github.com/wgavqb/2'),
      StringStruct('OriginalFilename', {exe!r}),
      StringStruct('ProductName', {name!r}),
      StringStruct('ProductVersion', {version!r})])]),
    VarFileInfo([VarStruct('Translation', [0x0419, 1200])])
  ]
)
"""


def write(name: str, version: str, description: str) -> Path:
    parts = [int(p) for p in version.split(".")][:4]
    parts += [0] * (4 - len(parts))
    out = ROOT / "build" / f"{name.lower()}_version.txt"
    out.parent.mkdir(exist_ok=True)
    out.write_text(TEMPLATE.format(tup=tuple(parts), version=version, name=name,
                                   exe=f"{name}.exe", description=description), "utf-8")
    return out


if __name__ == "__main__":
    print(write("FPSBooster", fps_version, "FPS Booster - оптимизация Windows для CS2 и Dota 2"))
    print(write("MicPro", mic_version, "MicPro - обработка голоса микрофона в реальном времени"))
