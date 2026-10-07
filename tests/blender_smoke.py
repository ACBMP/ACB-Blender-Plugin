"""blender --background --factory-startup --python tests/blender_smoke.py -- <forge>

Drives the add-on the way a user would: open a map, move a spawn, Shift+D a spawn and a chest, delete a spawn,
reshape a collision mesh and a trigger zone, save; then reopens the saved forge headless and checks every edit."""
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(REPO, "blender"), REPO, os.path.join(REPO, "vendor", "anvilforge-py", "src")]

import bpy  # noqa: E402

import acb_map_editor  # noqa: E402

acb_map_editor.register()
from acb_map_editor import session as S  # noqa: E402
from acbmap.doc import MapDocument  # noqa: E402
from acbmap.geom import position, mesh_shape_geometry  # noqa: E402
from acbmap.kinds import classify  # noqa: E402

forge = sys.argv[sys.argv.index("--") + 1]
t = time.time()
s = S.Session(bpy.context.scene, forge)
s.build()
print(f"SMOKE built {len(s.objects)} objects in {time.time() - t:.1f}s")
objs = [o for o in bpy.context.scene.objects if "acb_key" in o and not o.get("acb_part")]
kinds = {}
for o in objs:
    kinds.setdefault(o["acb_kind"], []).append(o)
print("SMOKE kinds", {k: len(v) for k, v in sorted(kinds.items())})

def copy(ob):
    c = ob.copy()
    if ob.data is not None:
        c.data = ob.data.copy()
    for col in ob.users_collection:
        col.objects.link(c)
    return c

sp = kinds["spawn"]
n_spawn = len(sp)
sp[0].location.z += 3.0
dup = copy(sp[1]); dup.location.x += 5
bpy.context.view_layer.update()
moved_key, moved_z = sp[0]["acb_key"], sp[0].matrix_world.translation.z
dup_xyz = tuple(dup.matrix_world.translation)
deleted_key = sp[2]["acb_key"]
bpy.data.objects.remove(sp[2])
chest = copy(kinds["chest_spawn"][0]); chest.location.y += 4
col = next(o for o in kinds["collision"] if o.type == "MESH" and o.data.users == 1)
col.data.vertices[0].co.z += 0.5
shape = int(col.data["acb_shape"], 16)
zone = next(o for o in bpy.context.scene.objects if o.get("acb_part", "").startswith("zone:"))
zone.scale *= 1.5
log = s.sync()
print("SMOKE log", log)
out = os.path.join(os.path.dirname(s.doc.cache), "edited", "smoke_" + os.path.basename(forge))
probs = s.save(out)
print("SMOKE problems", probs)
d = MapDocument(out)
els = classify(d)
spawns = [e for e in els if e.kind == "spawn"]
ok = True
def check(c, msg):
    global ok
    print("SMOKE", "OK " if c else "FAIL", msg)
    ok &= bool(c)
check(len(spawns) == n_spawn, f"spawn count {len(spawns)} == {n_spawn} (+1 copy -1 deleted)")
mk = S.parse_key(moved_key)
check(abs(position(d.obj(mk[0]).fields["GlobalMatrix"])[2] - moved_z) < 1e-3, "moved spawn z")
check(S.parse_key(deleted_key)[0] not in d.info, "deleted spawn gone")
check(any(all(abs(a - b) < 1e-3 for a, b in zip(e.position, dup_xyz)) for e in spawns), "duplicated spawn present")
check(any(l.startswith("moved ") for l in log), "move was detected")
check(not any("out-of-bounds" in l for l in log), "untouched out-of-bounds sections not rewritten")
check(sum(l.startswith("moved ") for l in log) == 1, "only the moved element is moved")
check(abs(mesh_shape_geometry(d.obj(shape))[0][0][2] - col.data.vertices[0].co.z) < 1e-4, "collision vertex")
from acbmap.worlddata import WorldData
w = WorldData(d, os.path.dirname(forge))
check(len(w.get(2).obj.fields["chestSpawnPoints"]) == len([e for e in els if e.kind == "chest_spawn"]), "chest data")
check(not probs, "no new structural problems")
print("SMOKE RESULT", "PASS" if ok else "FAIL")
