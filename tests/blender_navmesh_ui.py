"""xvfb-run -a blender --factory-startup --python tests/blender_navmesh_ui.py -- <forge>   (needs a window)

Navmesh editing the way a user does it: Edit Navmesh, delete faces in Edit Mode and Apply (that ground stops being
walkable); extrude a ledge edge outwards and Apply (new walkable ground, seamed to the old); Block Area with a cube;
Add Walkable with a plane; a block over a crowd flow point is refused and leaves the map as it was; Save, reopen and
check the navigation data is consistent and the edits are still there."""
import math
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(REPO, "blender"), REPO, os.path.join(REPO, "vendor", "anvilforge-py", "src")]

import bmesh  # noqa: E402
import bpy  # noqa: E402
from mathutils import Vector  # noqa: E402

import acb_map_editor  # noqa: E402

acb_map_editor.register()
from acb_map_editor import session as S  # noqa: E402
from acbmap import navedit as NE  # noqa: E402
from acbmap.doc import MapDocument  # noqa: E402
from acbmap.flowedit import world_points  # noqa: E402
from acbmap.navmodel import LEDGE, NavModel  # noqa: E402

forge = sys.argv[sys.argv.index("--") + 1]
ok = True


def check(c, msg):
    global ok
    print("NAVUI", "OK " if c else "FAIL", msg)
    ok &= bool(c)


def walkable(model, p, dz=0.4):
    return NE.Geo(model).tri_at(p, dz) is not None


def open_sites(s):
    """Points on flat open ground well away from crowd flow points, and a straight ledge edge with void beyond."""
    M = s.navmodel
    fpts = [p for f in M.flows for p in world_points(s.doc.obj(f.uid))]
    g = NE.Geo(M)
    far = lambda p, r: min((math.dist(p[:2], q[:2]) for q in fpts), default=99) > r  # noqa: E731
    flat, ledge = [], None
    for ms in M.meshes():
        for t in ms.tris:
            T = t.corners()
            c = tuple(sum(v[i] for v in T) / 3 for i in range(3))
            if (len(flat) < 40 and far(c, 7) and
                    all(g.tri_at((c[0] + dx, c[1] + dy, c[2]), 0.2) for dx in (-3, 0, 3) for dy in (-3, 0, 3))):
                if all(math.dist(c[:2], f[:2]) > 15 for f in flat):
                    flat.append(c)
            for k in range(3):
                if ledge is not None or t.nbr[k] != LEDGE:
                    continue
                a, b = T[k], T[(k + 1) % 3]
                if math.dist(a[:2], b[:2]) < 2.5 or abs(a[2] - b[2]) > 0.02:
                    continue
                m = [(a[i] + b[i]) / 2 for i in range(3)]
                nx, ny = b[1] - a[1], -(b[0] - a[0])
                L = math.hypot(nx, ny)
                nx, ny = nx / L, ny / L
                out = (m[0] + nx * 2, m[1] + ny * 2, m[2])
                if (g.heights(out) or g.heights((m[0] + nx * 5, m[1] + ny * 5, m[2])) or not far(m, 8) or
                        M.cell_of(out[0], out[1]) not in M.mgr or
                        M.cell_of(m[0] + nx * 3, m[1] + ny * 3) != M.cell_of(m[0], m[1])):
                    continue
                ledge = (a, b, (nx, ny), m)
    return flat, ledge, fpts


def edit_navmesh(s, fn):
    bpy.ops.acb.navmesh_edit()
    ob = bpy.data.objects.get(f"Navmesh [{s.tag}]")
    check(ob.mode == "EDIT", "Edit Navmesh enters Edit Mode on the navmesh")
    bm = bmesh.from_edit_mesh(ob.data)
    fn(bm)
    bmesh.update_edit_mesh(ob.data)
    return ob


def run():
    try:
        s = S.Session(bpy.context.scene, forge)
        s.build(with_visuals=False)
        flat, ledge, fpts = open_sites(s)
        check(len(flat) >= 3 and ledge is not None, f"found test sites ({len(flat)} open spots, ledge {ledge is not None})")
        hole, cube_at, _spare = flat[0], flat[1], flat[2]

        # 1. delete faces around `hole`
        def delete(bm):
            gone = [f for f in bm.faces if math.dist(f.calc_center_median()[:2], hole[:2]) < 1.5
                    and abs(f.calc_center_median()[2] - hole[2]) < 1]
            bmesh.ops.delete(bm, geom=gone, context="FACES")
        edit_navmesh(s, delete)
        check(s.navmesh_changed(), "deleting faces counts as a pending navmesh edit")
        r = bpy.ops.acb.navmesh_apply()
        ob = bpy.data.objects.get(f"Navmesh [{s.tag}]")
        check(r == {"FINISHED"} and ob.mode == "OBJECT" and ob.hide_select, f"Apply Navmesh {r}, back in Object Mode")
        check(not s.navmesh_changed(), "the navmesh object matches the map after Apply")
        check(not walkable(s.navmodel, hole), "the deleted spot isn't walkable")
        check(walkable(s.navmodel, (hole[0] + 3, hole[1], hole[2])), "ground next to it still is")
        check(NE.check(s.navmodel) == [], "navigation data consistent")

        # 2. extrude a ledge edge outwards by 2 m
        a, b, (nx, ny), m = ledge

        def extrude(bm):
            for e in bm.edges:
                e.select = False
            ka, kb = NE._key(a), NE._key(b)
            es = [e for e in bm.edges if {NE._key(tuple(v.co)) for v in e.verts} == {ka, kb}]
            check(len(es) == 1, "found the ledge edge in the edit mesh")
            res = bmesh.ops.extrude_edge_only(bm, edges=es)
            vs = [x for x in res["geom"] if isinstance(x, bmesh.types.BMVert)]
            bmesh.ops.translate(bm, verts=vs, vec=Vector((nx * 2, ny * 2, 0)))
        edit_navmesh(s, extrude)
        log = s.sync()
        check(any("navmesh (edited faces)" in x and "NOT" not in x for x in log), f"Apply Edits applies it: {log[-1:]}")
        out = (m[0] + nx * 1.5, m[1] + ny * 1.5, m[2])
        M = s.navmodel
        check(walkable(M, out), "the extruded strip is walkable")
        g = NE.Geo(M)
        inside = (m[0] - nx * 0.3, m[1] - ny * 0.3, m[2])
        ti, to = g.tri_at(inside, 0.4), g.tri_at(out, 0.4)
        check(ti is not None and to is not None and g.line_clear(inside, ti, out, to),
              "a straight walk from the old ground onto the new strip stays on the navmesh")
        _leave()

        # 3. Block Area with a cube
        bpy.ops.mesh.primitive_cube_add(size=3, location=(cube_at[0], cube_at[1], cube_at[2] + 1.5))
        cube = bpy.context.active_object
        r = bpy.ops.acb.navmesh_block()
        check(r == {"FINISHED"}, f"Block Area {r}")
        check(not walkable(s.navmodel, cube_at), "the cube's footprint isn't walkable")
        check(walkable(s.navmodel, (cube_at[0] + 2.5, cube_at[1], cube_at[2])), "around the cube still is")
        bpy.data.objects.remove(cube)

        # 4. Add Walkable: a raised 4 x 4 m platform over the blocked spot from step 1 (not solid: a walkway)
        bpy.ops.mesh.primitive_plane_add(size=4, location=(hole[0], hole[1], hole[2] + 0.0))
        plane = bpy.context.active_object
        bpy.context.scene.acb_nav_solid = True
        r = bpy.ops.acb.navmesh_add(solid=True)
        check(r == {"FINISHED"}, f"Add Walkable {r}")
        check(walkable(s.navmodel, hole), "the plane made the deleted spot walkable again")
        g = NE.Geo(s.navmodel)
        p, q = (hole[0] - 2.5, hole[1], hole[2]), (hole[0], hole[1], hole[2])
        tp, tq = g.tri_at(p, 0.4), g.tri_at(q, 0.4)
        check(tp is not None and tq is not None and g.line_clear(p, tp, q, tq), "and joined to the ground around it")
        bpy.data.objects.remove(plane)

        # 5. a block over a crowd flow point is refused
        before = {u: s.doc.payload(u) for u in s.doc.uids("NavMeshManager")}
        fp = fpts[len(fpts) // 3]
        bpy.ops.mesh.primitive_cube_add(size=1, location=(fp[0], fp[1], fp[2] + 0.5))
        r = bpy.ops.acb.navmesh_block()
        check(r == {"CANCELLED"}, f"blocking a crowd flow point is refused ({r})")
        check(walkable(s.navmodel, fp, 1.0), "the flow point is still on the navmesh")
        bpy.data.objects.remove(bpy.context.active_object)
        check(not s.navmesh_changed(), "the navmesh object was restored")
        del before

        # 6. save, reopen
        out_path = os.path.join(os.path.dirname(s.doc.cache), "edited", "navui_" + os.path.basename(forge))
        problems = s.save(out_path)
        check(not problems, f"saved, checks: {problems[:3]}")
        d2 = MapDocument(out_path)
        M2 = NavModel(d2)
        check(NE.check(M2) == [], "reopened: navigation data consistent")
        check(not walkable(M2, cube_at) and walkable(M2, out) and walkable(M2, hole), "reopened: the edits are there")
    except Exception:
        import traceback
        traceback.print_exc()
        check(False, "exception")
    print("NAVUI RESULT", "PASS" if ok else "FAIL")
    sys.stdout.flush()
    os._exit(0 if ok else 1)


def _leave():
    if bpy.context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")


bpy.app.timers.register(run, first_interval=0.5)
