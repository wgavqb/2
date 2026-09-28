import os
import time
from pathlib import Path

import pytest

from fps_booster import vdf
from fps_booster.backup import Backup
from fps_booster.games import (
    BLOCK_BEGIN, BLOCK_END, CS2, DOTA2, InstalledGame, apply_autoexec, detect_games,
    library_paths, merge_block, restore_autoexec, strip_block,
)
from fps_booster.system import clean_temp, is_enabled_flag, startup_flag
from fps_booster.tweaks import build_tweaks, restore_key

LIBRARYFOLDERS = r'''
"libraryfolders"
{
	"0"
	{
		"path"		"C:\\Program Files (x86)\\Steam"
		"apps" { "228980" "1" }
	}
	"1"
	{
		"path"		"D:\\SteamLibrary"
		"apps" { "730" "1" }
	}
}
'''


def test_vdf_parse_nested_and_escapes():
    data = vdf.parse(LIBRARYFOLDERS)
    assert data["libraryfolders"]["1"]["path"] == r"D:\SteamLibrary"
    assert data["libraryfolders"]["0"]["apps"] == {"228980": "1"}


def test_vdf_comments_and_conditionals():
    data = vdf.parse('// c\n"a" { "b" "1" [$WIN32] "c" "2" }')
    assert data == {"a": {"b": "1", "c": "2"}}


def test_vdf_errors():
    with pytest.raises(vdf.VDFError):
        vdf.parse('"a" { "b" "1"')
    with pytest.raises(vdf.VDFError):
        vdf.parse('"a" "1" }')


def make_steam(tmp_path: Path) -> Path:
    steam = tmp_path / "Steam"
    lib2 = tmp_path / "Lib2"
    (steam / "steamapps" / "common" / "dota 2 beta" / "game" / "dota" / "cfg").mkdir(parents=True)
    (lib2 / "steamapps" / "common" / "Counter-Strike Global Offensive" / "game" / "csgo" / "cfg").mkdir(parents=True)
    (steam / "steamapps" / "libraryfolders.vdf").write_text(
        '"libraryfolders" { "0" { "path" "%s" } "1" { "path" "%s" } }'
        % (str(steam).replace("\\", "\\\\"), str(lib2).replace("\\", "\\\\")))
    (lib2 / "steamapps" / "appmanifest_730.acf").write_text(
        '"AppState" { "appid" "730" "installdir" "Counter-Strike Global Offensive" }')
    return steam


def test_detect_games(tmp_path):
    steam = make_steam(tmp_path)
    assert library_paths(steam) == [steam, tmp_path / "Lib2"]
    games = {g.profile.key: g for g in detect_games(steam)}
    assert games["cs2"].install_dir == tmp_path / "Lib2/steamapps/common/Counter-Strike Global Offensive"
    assert games["dota2"].exe_path.name == "dota2.exe"


def test_merge_and_strip_block_preserve_user_config():
    user = "bind f5 screenshot\nsensitivity 1.2"
    merged = merge_block(user, ["fps_max 0"])
    assert merged.startswith("bind f5 screenshot\nsensitivity 1.2\n")
    assert BLOCK_BEGIN in merged and BLOCK_END in merged
    # повторное применение не дублирует блок
    again = merge_block(merged, ["fps_max 0"])
    assert again.count(BLOCK_BEGIN) == 1
    assert strip_block(again) == "bind f5 screenshot\nsensitivity 1.2\n"


def test_autoexec_apply_and_restore(tmp_path):
    install = tmp_path / "cs2"
    game = InstalledGame(CS2, install)
    backup = Backup(tmp_path / "b.json")
    apply_autoexec(game, backup)
    assert "fps_max 0" in game.autoexec_path.read_text()
    restore_key(f"cfg|{game.autoexec_path}", backup, print)
    assert not game.autoexec_path.exists()  # файла не было — удаляем

    dota = InstalledGame(DOTA2, tmp_path / "dota")
    dota.cfg_dir.mkdir(parents=True)
    dota.autoexec_path.write_text("dota_camera_distance 1134\n")
    apply_autoexec(dota, backup)
    restore_autoexec(dota.autoexec_path, backup.get(f"cfg|{dota.autoexec_path}"))
    assert dota.autoexec_path.read_text() == "dota_camera_distance 1134\n"


def test_backup_keeps_first_original(tmp_path):
    b = Backup(tmp_path / "b.json")
    b.remember("k", 1)
    b.remember("k", 2)
    assert Backup(tmp_path / "b.json").get("k") == 1
    b.forget("k")
    assert "k" not in Backup(tmp_path / "b.json")


def test_startup_flags():
    assert is_enabled_flag(None)
    assert is_enabled_flag(bytes([2]) + bytes(11))
    assert not is_enabled_flag(bytes([3]) + bytes(11))
    assert is_enabled_flag(startup_flag(True)) and not is_enabled_flag(startup_flag(False))


def test_clean_temp_only_old_files(tmp_path):
    old, new = tmp_path / "old.tmp", tmp_path / "sub" / "new.tmp"
    new.parent.mkdir()
    old.write_bytes(b"x" * 100)
    new.write_bytes(b"y")
    past = time.time() - 3 * 86400
    os.utime(old, (past, past))
    count, size = clean_temp([tmp_path])
    assert (count, size) == (1, 100)
    assert new.exists() and not old.exists()


def test_build_tweaks_without_games():
    tweaks = {t.id: t for t in build_tweaks([])}
    assert not tweaks["gpu_pref"].available()
    assert not tweaks["autoexec"].available()
    assert tweaks["mmcss"].needs_admin and not tweaks["game_mode"].needs_admin
    assert len(tweaks) == len(build_tweaks([]))  # уникальные id
