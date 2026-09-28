"""Точка входа: без аргументов — окно приложения, с аргументами — консольный режим."""

from __future__ import annotations

import argparse
import sys

from . import APP_NAME, __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fps_booster", description=f"{APP_NAME} {__version__}")
    parser.add_argument("--list", action="store_true", help="показать найденные игры и оптимизации")
    parser.add_argument("--apply", nargs="*", metavar="ID", help="применить (без ID — выбранные по умолчанию)")
    parser.add_argument("--revert", action="store_true", help="откатить все изменения")
    parser.add_argument("--no-admin", action="store_true", help="не запрашивать права администратора")
    args = parser.parse_args(argv)
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # консоль Windows может не знать «✔»

    from .winutil import IS_WINDOWS, is_admin, relaunch_as_admin

    if IS_WINDOWS and not args.no_admin and not is_admin() and relaunch_as_admin():
        return 0  # запущена копия с правами администратора

    if not (args.list or args.apply is not None or args.revert):
        from .app import run
        run()
        return 0

    from .backup import Backup
    from .games import detect_games
    from .tweaks import apply_tweaks, build_tweaks, revert_all

    backup = Backup()
    games = detect_games()
    tweaks = build_tweaks(games)
    if args.list:
        for game in games:
            print(f"[игра] {game.profile.name}: {game.install_dir}")
        for t in tweaks:
            mark = "✔" if t.is_applied(backup) else " "
            print(f"[{mark}] {t.id:<18} {t.title}{'' if t.available() else ' (недоступно)'}")
    if args.revert:
        errors = revert_all(backup, print)
        return 1 if errors else 0
    if args.apply is not None:
        chosen = [t for t in tweaks if (t.id in args.apply if args.apply else t.default)]
        _ok, errors, reboot = apply_tweaks(chosen, backup, print)
        if reboot:
            print("Часть изменений заработает после перезагрузки.")
        return 1 if errors else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
