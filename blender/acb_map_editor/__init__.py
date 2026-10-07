"""ACB Map Editor: edit Assassin's Creed Brotherhood multiplayer maps in Blender.

Open a map forge (N-panel > ACB), move/duplicate/delete gameplay elements and collision, edit any field in the
inspector, edit Escort paths, then Save (writes an edited forge after structural checks) and Install (replaces the
game's copy, original kept as .orig). The forge work is done by the `acbmap` package (repo root), which uses
anvilforge's engine-rule codec."""

bl_info = {
    "name": "ACB Map Editor",
    "author": "acb2",
    "version": (0, 1, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > ACB",
    "description": "Edit Assassin's Creed Brotherhood MP map forges",
    "category": "Import-Export",
}

import os
import sys

import bpy

HERE = os.path.dirname(os.path.realpath(__file__))
REPO_DIR = os.path.dirname(os.path.dirname(HERE))   # checkout layout: <repo>/blender/acb_map_editor


def _candidates(prefs=None):
    """Where acbmap and anvilforge can come from, first match wins: the add-on preferences, a zip build's bundled
    copies (<add-on>/_vendor), a repo checkout (acbmap at the root, anvilforge as the vendor/anvilforge-py
    submodule)."""
    out = []
    for p in (getattr(prefs, "repo_path", ""), getattr(prefs, "anvilforge_src", "")):
        if p:
            out.append(os.path.expanduser(p))
    out += [os.path.join(HERE, "_vendor"), REPO_DIR, os.path.join(REPO_DIR, "vendor", "anvilforge-py", "src")]
    return out


def ensure_paths(prefs=None):
    for p in reversed(_candidates(prefs)):
        if os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)


class ACBPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__ or __name__

    repo_path: bpy.props.StringProperty(name="acbmap location (optional)", subtype="DIR_PATH",
                                        description="Folder containing the acbmap package; empty = automatic")
    anvilforge_src: bpy.props.StringProperty(name="anvilforge location (optional)", subtype="DIR_PATH",
                                             description="Folder containing the anvilforge package; empty = automatic")
    game_multi: bpy.props.StringProperty(
        name="Game 'multi' folder", subtype="DIR_PATH",
        default=os.path.expanduser("~/Games/assassins-creed-brotherhood/drive_c/Program Files (x86)/Ubisoft/"
                                   "Ubisoft Game Launcher/games/Assassin's Creed Brotherhood/multi"))

    def draw(self, context):
        for p in ("repo_path", "anvilforge_src", "game_multi"):
            self.layout.prop(self, p)


def prefs(context=None):
    context = context or bpy.context
    a = context.preferences.addons.get(__package__ or __name__)
    return a.preferences if a else None


def register():
    bpy.utils.register_class(ACBPreferences)
    ensure_paths(prefs())
    from . import ui
    ui.register()


def unregister():
    from . import ui
    ui.unregister()
    bpy.utils.unregister_class(ACBPreferences)
