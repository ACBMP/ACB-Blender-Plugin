"""blender --background --factory-startup --python tests/blender_switch_map.py -- <forge A> <forge B>

Switching maps: open A, start a new file (the panel must not show A any more: every new file's scene is called
"Scene" again), open B, open A over B in the same scene (B is closed first), Close Map (the scene is left with no ACB
objects, collections, data or properties)."""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(REPO, "blender"), REPO, os.path.join(REPO, "vendor", "anvilforge-py", "src")]

import bpy  # noqa: E402

import acb_map_editor  # noqa: E402

acb_map_editor.register()
from acb_map_editor import session as S  # noqa: E402

a, b = sys.argv[sys.argv.index("--") + 1:][:2]
ok = True


def check(c, msg):
    global ok
    print("SWITCH", "OK " if c else "FAIL", msg)
    ok &= bool(c)


def acb_data():
    return (sum("acb_key" in o for o in bpy.data.objects),
            sum(c.name.startswith("ACB ") for c in bpy.data.collections),
            sum(d.name.startswith(("ACB", "Escort")) for d in (*bpy.data.meshes, *bpy.data.curves, *bpy.data.materials)))


def open_map(path):
    return bpy.ops.acb.open_map(filepath=path, with_visuals=False)


check(open_map(a) == {"FINISHED"}, "open A")
check(S.get(bpy.context.scene) is not None and S.get(bpy.context.scene).source == a, "panel shows A")
bpy.ops.wm.read_homefile(use_empty=True)
check(S.get(bpy.context.scene) is None, f"new file: no map (scene {bpy.context.scene.name!r})")
check(open_map(b) == {"FINISHED"} and S.get(bpy.context.scene).source == b, "open B in the new file")
nb = acb_data()
check(open_map(a) == {"FINISHED"} and S.get(bpy.context.scene).source == a, "open A over B")
check(not any(c.name.startswith(f"ACB {os.path.basename(b).replace('DataPC_', '').replace('.forge', '')}")
              for c in bpy.data.collections), "B's collections gone")
check(len(S.SESSIONS) == 1, "one open map")
check(bpy.ops.acb.close_map() == {"FINISHED"}, "Close Map")
check(S.get(bpy.context.scene) is None, "no map after closing")
check(acb_data() == (0, 0, 0), f"no ACB objects/collections/data left {acb_data()} (B had {nb})")
check(not any(k.startswith("acb_") for k in bpy.context.scene.keys()), "scene properties cleared")
check(open_map(b) == {"FINISHED"} and acb_data()[0] == nb[0], "B opens again afterwards, same object count")
print("SWITCH RESULT", "PASS" if ok else "FAIL")
