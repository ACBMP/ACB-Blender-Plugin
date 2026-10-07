"""Install an edited map forge over the game's copy (every player needs the same file).

The game's original is kept once as `<forge>.orig` (never overwritten by later installs), and uninstall restores it.
A forge is only installed if it passes the structural checks against its source."""
from __future__ import annotations

import os
import shutil

GAME_MULTI = os.path.expanduser(
    "~/Games/assassins-creed-brotherhood/drive_c/Program Files (x86)/Ubisoft/Ubisoft Game Launcher/games/"
    "Assassin's Creed Brotherhood/multi")


def install(edited: str, game_multi: str = GAME_MULTI, name: str | None = None) -> str:
    dst = os.path.join(game_multi, name or os.path.basename(edited))
    if not os.path.exists(dst):
        raise FileNotFoundError(f"{dst}: no such map in the game folder (install only replaces existing maps)")
    orig = dst + ".orig"
    if not os.path.exists(orig):
        shutil.copy2(dst, orig)
    tmp = dst + ".installing"
    shutil.copyfile(edited, tmp)
    os.replace(tmp, dst)
    return dst


def uninstall(forge_name: str, game_multi: str = GAME_MULTI) -> str:
    dst = os.path.join(game_multi, forge_name)
    orig = dst + ".orig"
    if not os.path.exists(orig):
        raise FileNotFoundError(f"{orig}: nothing to restore")
    os.replace(orig, dst)
    return dst


def status(game_multi: str = GAME_MULTI) -> list[str]:
    return sorted(f[:-5] for f in os.listdir(game_multi) if f.endswith(".forge.orig"))
