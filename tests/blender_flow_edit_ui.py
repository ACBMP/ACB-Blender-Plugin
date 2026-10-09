"""xvfb-run -a blender --factory-startup --python tests/blender_flow_edit_ui.py -- <forge>   (needs a window)

Crowd flow (NPC path) points edited the way a user does it: Edit Flow Points, then in Edit Mode move a point,
subdivide between two points, delete one, Apply while still in Edit Mode; a point moved off the navmesh is rejected
and the line restored; Save, reopen and check the navigation data agrees with the flow (every point on the triangle it
names, the metalink mirroring the flow's triangles and waypoints, waypoints at the points)."""
import math
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(REPO, "blender"), REPO, os.path.join(REPO, "vendor", "anvilforge-py", "src")]

import bpy  # noqa: E402
from mathutils import Vector  # noqa: E402

import acb_map_editor  # noqa: E402

acb_map_editor.register()
from acb_map_editor import session as S  # noqa: E402
from acbmap import flowedit as FE  # noqa: E402
from acbmap.doc import MapDocument  # noqa: E402
from acbmap.kinds import component  # noqa: E402
from acbmap.navmesh import NavData, h, inside2d  # noqa: E402

forge = sys.argv[sys.argv.index("--") + 1]
ok = True


def check(c, msg):
    global ok
    print("FLOWUI", "OK " if c else "FAIL", msg)
    ok &= bool(c)


def consistent(d, uid):
    nd = NavData(d)
    o = d.obj(uid)
    cf = component(o, "CrowdFlow")
    pts = FE.world_points(o)
    r = cf.fields["MetaLinkRef"]
    ml = nd.metalink(h(r.fields["ManagerIndex"]), h(r.fields["MetaLinkIndex"]))
    tri = lambda t: (h(t.fields["ManagerIndex"]), h(t.fields["NavMeshIndex"]), h(t.fields["TriangleIndex"]))  # noqa
    tris = [tri(t) for t in cf.fields["TriangleArray"]]
    wps = [(h(w.fields["ManagerIndex"]), h(w.fields["WayPointIndex"])) for w in cf.fields["WayPointArray"]]
    return (len(pts) == len(tris) == len(wps) == len(ml.fields["ObjectWayPoints"])
            and all(inside2d(nd.triangle(t), p, 1e-3) for t, p in zip(tris, pts))
            and tris == [tri(t) for t in ml.fields["TriangleList"]]
            and wps == [(h(w.fields["WayPoint"].fields["ManagerIndex"]), h(w.fields["WayPoint"].fields["WayPointIndex"]))
                        for w in ml.fields["ObjectWayPoints"]]
            and max(math.dist(p, nd.waypoint_pos(*w)) for p, w in zip(pts, wps)) < 0.06), len(pts)


def run():
    try:
        s = S.Session(bpy.context.scene, forge)
        s.build(with_visuals=False)
        flows = [o for o in bpy.context.scene.objects if o.get("acb_kind") == "crowd_flow" and o.type == "CURVE"]
        ob = max(flows, key=lambda o: len(o.data.splines[0].points))
        uid = S.parse_key(ob["acb_key"])[0]
        n0 = len(ob.data.splines[0].points)
        win = bpy.context.window_manager.windows[0]
        area = next(a for a in win.screen.areas if a.type == "VIEW_3D")
        region = next(r for r in area.regions if r.type == "WINDOW")
        with bpy.context.temp_override(window=win, area=area, region=region):
            for x in bpy.context.selected_objects:
                x.select_set(False)
            bpy.context.view_layer.objects.active = ob
            check(bpy.ops.acb.edit_flow() == {"FINISHED"} and bpy.context.mode == "EDIT_CURVE", "Edit Flow Points")
            pts = ob.data.splines[0].points
            k = n0 // 2
            a, b = Vector(pts[k - 1].co[:3]), Vector(pts[k + 1].co[:3])
            side = Vector((-(b - a).y, (b - a).x, 0)).normalized()
            for p in pts:
                p.select = False
            pts[k].select = True
            bpy.ops.transform.translate(value=tuple(side * 1.5))
            for p in pts:
                p.select = False
            pts[1].select = pts[2].select = True
            bpy.ops.curve.subdivide()
            pts = ob.data.splines[0].points
            for p in pts:
                p.select = False
            pts[-1].select = True
            bpy.ops.curve.delete(type="VERT")
            check(bpy.ops.acb.apply() == {"FINISHED"}, "Apply in Edit Mode")
            check(any("crowd flow" in ln and "2 moved or new" in ln for ln in s.log), f"flow rewritten: {s.log[-3:]}")
            c, n = consistent(s.doc, uid)
            check(c and n == n0, f"navigation data consistent ({n} points, was {n0})")
            check(len(ob.data.splines[0].points) == n0, "line redrawn from the document")
            # a point far off the navmesh: rejected, line restored
            before = FE.world_points(s.doc.obj(uid))
            bpy.ops.object.mode_set(mode="EDIT")
            pts = ob.data.splines[0].points
            for p in pts:
                p.select = False
            pts[0].select = True
            bpy.ops.transform.translate(value=(0, 0, 40.0))
            bpy.ops.acb.apply()
            check(any("NOT changed" in ln and "navmesh" in ln for ln in s.log[-3:]), f"off-navmesh rejected: {s.log[-1]}")
            check(FE.world_points(s.doc.obj(uid)) == before, "document unchanged by the rejected edit")
            first = ob.matrix_world @ Vector(ob.data.splines[0].points[0].co[:3])
            check((first - Vector(before[0])).length < 0.01, "line restored")
            bpy.ops.object.mode_set(mode="OBJECT")
        out = os.path.join(os.path.dirname(s.doc.cache), "edited", "flowui_" + os.path.basename(forge))
        probs = s.save(out)
        check(not probs, f"no new structural problems {probs}")
        c, n = consistent(MapDocument(out), uid)
        check(c and n == n0, "saved forge: navigation data consistent")
    except Exception:
        import traceback
        traceback.print_exc()
        check(False, "exception")
    print("FLOWUI RESULT", "PASS" if ok else "FAIL")
    bpy.ops.wm.quit_blender()


bpy.app.timers.register(run, first_interval=1.0)
