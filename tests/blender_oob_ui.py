"""xvfb-run -a blender --factory-startup --python tests/blender_oob_ui.py -- <forge>   (needs a window: not --background)

The out-of-bounds boundary edited the way a user does it: Edit Boundary, move a corner and extrude a new one with
Blender's own operators, Apply while still in Edit Mode, Save. Checks the saved wall keeps its heights (generic
attributes have no data from Python in Edit Mode; this once turned every height into a 10 m fallback, burying the
wall underground) and has the new corner."""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(REPO, "blender"), REPO, os.path.join(REPO, "vendor", "anvilforge-py", "src")]

import bmesh  # noqa: E402
import bpy  # noqa: E402

import acb_map_editor  # noqa: E402

acb_map_editor.register()
from acb_map_editor import session as S  # noqa: E402
from acbmap import oob as O  # noqa: E402
from acbmap.doc import MapDocument  # noqa: E402
from acbmap.kinds import classify  # noqa: E402

forge = sys.argv[sys.argv.index("--") + 1]
ok = True


def check(c, msg):
    global ok
    print("OOBUI", "OK " if c else "FAIL", msg)
    ok &= bool(c)


def run():
    try:
        s = S.Session(bpy.context.scene, forge)
        s.build(with_visuals=False)
        key = S.parse_key(next(o for o in bpy.context.scene.objects if o.get("acb_part") == "oobwall")["acb_key"])
        before = O.walls(s.doc, key)
        n0 = sum(len(w.corners) for w in before)
        hmin = min(h for w in before for h in w.heights)
        win = bpy.context.window_manager.windows[0]
        area = next(a for a in win.screen.areas if a.type == "VIEW_3D")
        region = next(r for r in area.regions if r.type == "WINDOW")
        with bpy.context.temp_override(window=win, area=area, region=region):
            check(bpy.ops.acb.edit_boundary() == {"FINISHED"}, "Edit Boundary")
            ob = bpy.context.active_object
            bm = bmesh.from_edit_mesh(ob.data)
            bm.verts.ensure_lookup_table()
            end = next(v for v in bm.verts if len(v.link_edges) == 1) if not before[0].closed else bm.verts[3]
            for v in bm.verts:
                v.select = False
            bm.verts[2].select = True
            bmesh.update_edit_mesh(ob.data)
            bpy.ops.transform.translate(value=(4.0, -3.0, 0.0))
            for v in bm.verts:
                v.select = False
            end.select = True
            bmesh.update_edit_mesh(ob.data)
            bpy.ops.mesh.extrude_vertices_move(TRANSFORM_OT_translate={"value": (0.0, -6.0, 0.0)})
            check(bpy.context.mode == "EDIT_MESH", "still in Edit Mode")
            check(bpy.ops.acb.apply() == {"FINISHED"}, "Apply in Edit Mode")
        out = os.path.join(os.path.dirname(s.doc.cache), "edited", "oobui_" + os.path.basename(forge))
        probs = s.save(out)
        d = MapDocument(out)
        e = next(x for x in classify(d) if x.kind == "out_of_bounds")
        after = O.walls(d, e.key)
        hs = [h for w in after for h in w.heights]
        n1 = len({(round(p[0], 2), round(p[1], 2)) for w in after for p in w.corners})   # a junction is in several walls
        check(n1 == n0 + 1, f"one corner added ({n0} -> {n1})")
        check(min(hs) >= hmin - 0.01, f"heights kept (min {min(hs):.1f} m, was {hmin:.1f} m)")
        check(not probs, f"no new structural problems {probs}")
    except Exception:
        import traceback
        traceback.print_exc()
        check(False, "exception")
    print("OOBUI RESULT", "PASS" if ok else "FAIL")
    bpy.ops.wm.quit_blender()


bpy.app.timers.register(run, first_interval=1.0)
