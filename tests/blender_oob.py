"""blender --background --factory-startup --python tests/blender_oob.py -- <forge>

The out-of-bounds boundary as one editable wall: move a corner, add one (subdivide a segment), dissolve one, Apply
and Save; reopen headless and check the walls, sections, fog mesh and structural checks."""
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
from acbmap import oob as O  # noqa: E402
from acbmap import visual as V  # noqa: E402
from acbmap.doc import MapDocument  # noqa: E402
from acbmap.kinds import classify, component  # noqa: E402

forge = sys.argv[sys.argv.index("--") + 1]
s = S.Session(bpy.context.scene, forge)
s.build()
ok = True


def check(c, msg):
    global ok
    print("OOB", "OK " if c else "FAIL", msg)
    ok &= bool(c)


walls = [o for o in bpy.context.scene.objects if o.get("acb_part") == "oobwall"]
check(len(walls) == 1 and walls[0].type == "MESH" and not walls[0].data.polygons, "one polyline wall object")
wob = walls[0]
n0 = len(wob.data.vertices)
check(s.sync() == [] or not any("out-of-bounds" in l for l in s.sync()), "untouched wall not rewritten")
key = S.parse_key(wob["acb_key"])
fog_before = V.mesh_geometry(s.doc, O.oob_parts(s.doc, key)[4])

bm = bmesh.new()
bm.from_mesh(wob.data)
bm.verts.ensure_lookup_table()
M, Mi = wob.matrix_world, wob.matrix_world.inverted()
moved_world = M @ bm.verts[2].co + Vector((5, -3, 0))
bm.verts[2].co = Mi @ moved_world
bm.edges.ensure_lookup_table()
e = next(e for e in bm.edges if 0 not in [v.index for v in e.verts] and 2 not in [v.index for v in e.verts])
res = bmesh.ops.subdivide_edges(bm, edges=[e], cuts=1)
newv = next(g for g in res["geom_inner"] if isinstance(g, bmesh.types.BMVert))
added_world = M @ newv.co + Vector((0, 0, 0))
dv = next(v for v in bm.verts if v is not newv and v.index not in (0, 2) and len(v.link_edges) == 2
          and all(o.index not in (2,) for ed in v.link_edges for o in ed.verts if o is not v))
bmesh.ops.dissolve_verts(bm, verts=[dv])
bm.to_mesh(wob.data)
bm.free()
hl = wob.data.attributes["acb_height"]
check(all(d.value > 1 for d in hl.data), "every corner (the added one too) has a height")

log = s.sync()
print("OOB log", [l for l in log if "out-of-bounds" in l])
check(any(l.startswith("out-of-bounds wall") for l in log), "wall edit detected and written")
fog_obj = next((c for c in s.scene.objects if c.get("acb_visual_of") == wob["acb_key"]), None)
out = os.path.join(os.path.dirname(s.doc.cache), "edited", "oob_" + os.path.basename(forge))
probs = s.save(out)
d = MapDocument(out)
e2 = next(x for x in classify(d) if x.kind == "out_of_bounds")
back = O.walls(d, e2.key)
corners = [p for w in back for p in w.corners]
check(len(corners) == n0, f"corner count {len(corners)} == {n0} (+1 added -1 dissolved)")
check(any(math.dist(p, moved_world) < 0.02 for p in corners), "moved corner is where it was dragged")
check(any(math.dist(p, added_world) < 0.02 for p in corners), "added corner present")
oc = component(e2.obj, "OutOfBoundsComponent")
check(len(oc.fields["Sections"]) > 10, f"{len(oc.fields['Sections'])} sections")
fog = V.mesh_geometry(d, V.entity_meshes(d, e2.obj)[0])
check(fog is not None and fog_before is not None and fog.verts != fog_before.verts, "fog mesh regenerated")
check(fog_obj is not None and len(fog_obj.data.vertices) == len(fog.verts), "fog wireframe shows the new mesh")
check(not probs, f"no new structural problems {probs}")
print("OOB RESULT", "PASS" if ok else "FAIL")
