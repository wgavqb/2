"""Поиск Steam, CS2 и Dota 2 и работа с их autoexec.cfg."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import vdf
from .winutil import IS_WINDOWS, reg_read

BLOCK_BEGIN = "// >>> FPS Booster (auto) - do not edit this block"
BLOCK_END = "// <<< FPS Booster"


@dataclass(frozen=True)
class GameProfile:
    key: str
    name: str
    app_id: int
    folder: str  # папка по умолчанию в steamapps/common
    exe_rel: str
    cfg_rel: str
    process: str
    launch_options: str
    # Только команды, которые НЕ меняют качество картинки
    autoexec: tuple[str, ...]


CS2 = GameProfile(
    key="cs2",
    name="Counter-Strike 2",
    app_id=730,
    folder="Counter-Strike Global Offensive",
    exe_rel="game/bin/win64/cs2.exe",
    cfg_rel="game/csgo/cfg",
    process="cs2.exe",
    launch_options="-novid -nojoy +exec autoexec",
    autoexec=(
        "fps_max 0                // no in-game FPS cap",
        "fps_max_ui 120           // cap FPS in menus: cooler GPU, faster alt-tab",
        "cl_autohelp 0            // disable hint system",
        "gameinstructor_enable 0  // disable game instructor overlay logic",
    ),
)

DOTA2 = GameProfile(
    key="dota2",
    name="Dota 2",
    app_id=570,
    folder="dota 2 beta",
    exe_rel="game/bin/win64/dota2.exe",
    cfg_rel="game/dota/cfg",
    process="dota2.exe",
    launch_options="-novid -nojoy -map dota",
    autoexec=(
        "fps_max 999              // effectively no FPS cap",
        "dota_embers 0            // disable animated main-menu background",
    ),
)

PROFILES = (CS2, DOTA2)


@dataclass(frozen=True)
class InstalledGame:
    profile: GameProfile
    install_dir: Path

    @property
    def exe_path(self) -> Path:
        return self.install_dir / self.profile.exe_rel

    @property
    def cfg_dir(self) -> Path:
        return self.install_dir / self.profile.cfg_rel

    @property
    def autoexec_path(self) -> Path:
        return self.cfg_dir / "autoexec.cfg"


def find_steam_path() -> Path | None:
    candidates: list[str] = []
    if IS_WINDOWS:
        for root, path, name in (
            ("HKCU", r"Software\Valve\Steam", "SteamPath"),
            ("HKLM", r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
            ("HKLM", r"SOFTWARE\Valve\Steam", "InstallPath"),
        ):
            try:
                info = reg_read(root, path, name)
            except OSError:
                continue
            if info.get("exists"):
                candidates.append(str(info["value"]))
        for env in ("ProgramFiles(x86)", "ProgramFiles"):
            if os.environ.get(env):
                candidates.append(os.path.join(os.environ[env], "Steam"))
    else:
        candidates += [str(Path.home() / ".steam/steam"), str(Path.home() / ".local/share/Steam")]
    for candidate in candidates:
        path = Path(candidate)
        if (path / "steamapps").is_dir():
            return path
    return None


def library_paths(steam: Path) -> list[Path]:
    libraries = [steam]
    vdf_file = steam / "steamapps" / "libraryfolders.vdf"
    try:
        data = vdf.parse(vdf_file.read_text("utf-8", errors="replace"))
    except (OSError, vdf.VDFError):
        return libraries
    folders = vdf.get_ci(data, "libraryfolders") or vdf.get_ci(data, "LibraryFolders") or {}
    for key, entry in folders.items():
        if not key.isdigit():
            continue
        # новый формат: {"path": ...}, старый: просто строка с путём
        raw = vdf.get_ci(entry, "path") if isinstance(entry, dict) else entry
        if not raw:
            continue
        path = Path(raw)
        if path not in libraries:
            libraries.append(path)
    return libraries


def find_installed(profile: GameProfile, libraries: list[Path]) -> InstalledGame | None:
    for library in libraries:
        steamapps = library / "steamapps"
        manifest = steamapps / f"appmanifest_{profile.app_id}.acf"
        folder = profile.folder
        if manifest.is_file():
            try:
                state = vdf.get_ci(vdf.parse(manifest.read_text("utf-8", errors="replace")), "AppState", {})
                folder = vdf.get_ci(state, "installdir") or folder
            except vdf.VDFError:
                pass
        install_dir = steamapps / "common" / folder
        if install_dir.is_dir():
            return InstalledGame(profile, install_dir)
    return None


def detect_games(steam: Path | None = None) -> list[InstalledGame]:
    steam = steam or find_steam_path()
    if steam is None:
        return []
    libraries = library_paths(steam)
    return [game for p in PROFILES if (game := find_installed(p, libraries))]


# --- autoexec ---------------------------------------------------------------

def strip_block(text: str) -> str:
    """Убирает наш блок из конфига, остальное оставляет как было."""
    lines = text.splitlines(keepends=True)
    out, inside = [], False
    for line in lines:
        stripped = line.strip()
        if stripped == BLOCK_BEGIN:
            inside = True
            continue
        if inside and stripped == BLOCK_END:
            inside = False
            continue
        if not inside:
            out.append(line)
    return "".join(out)


def merge_block(text: str, commands: tuple[str, ...] | list[str]) -> str:
    """Добавляет (или обновляет) наш блок в конец конфига."""
    base = strip_block(text)
    if base and not base.endswith("\n"):
        base += "\n"
    block = "\n".join([BLOCK_BEGIN, *commands, BLOCK_END]) + "\n"
    return base + block


def apply_autoexec(game: InstalledGame, backup) -> Path:
    path = game.autoexec_path
    existed = path.exists()
    text = path.read_text("utf-8", errors="surrogateescape") if existed else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(merge_block(text, game.profile.autoexec), "utf-8", errors="surrogateescape")
    backup.remember(f"cfg|{path}", {"existed": existed})
    return path


def restore_autoexec(path: Path, original: dict | None) -> None:
    if not path.exists():
        return
    text = strip_block(path.read_text("utf-8", errors="surrogateescape"))
    if not text.strip() and original is not None and not original.get("existed", True):
        path.unlink()
    else:
        path.write_text(text, "utf-8", errors="surrogateescape")
