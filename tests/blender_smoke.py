"""blender --background --factory-startup --python tests/blender_smoke.py -- <forge>

Drives the add-on the way a user would: open a map, move a spawn, Shift+D a spawn and a chest, delete a spawn,
reshape a collision mesh and a trigger zone, move and Shift+D a piece of scenery (shown by its visual mesh), generate
climb edges on an element, turn plain meshes into visible collision and scenery and swap a visual, save; then
reopens the saved forge headless and checks every edit."""
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(REPO, "blender"), REPO, os.path.join(REPO, "vendor", "anvilforge-py", "src")]

import bpy  # noqa: E402
from mathutils import Vector  # noqa: E402

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
vis = [o for o in objs if o.type == "MESH" and "acb_visual" in o.data]

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
col = next(o for o in bpy.context.scene.objects
           if o.type == "MESH" and "acb_shape" in o.data and o.data.users == 1 and "acb_key" in o)
col.data.vertices[0].co.z += 0.5
shape = int(col.data["acb_shape"], 16)
zone = next(o for o in bpy.context.scene.objects if o.get("acb_part", "").startswith("zone:"))
zone.scale *= 1.5
print("SMOKE visual elements", len(vis), "meshes", sum("acb_visual" in m for m in bpy.data.meshes),
      "textures", sum(i.name.startswith("ACBTex_") for i in bpy.data.images))
scen = next(o for o in vis if o["acb_kind"] == "visual" and S.parse_key(o["acb_key"])[1] < 0)
scen.location.x += 2.0
scen_dup = copy(next(o for o in vis if o["acb_kind"] == "collision" and S.parse_key(o["acb_key"])[1] < 0))
scen_dup.location.y += 7.0
bpy.context.view_layer.update()
scen_key, scen_x = scen["acb_key"], scen.matrix_world.translation.x
log = s.sync()
climb_el = next(o for o in vis if o["acb_kind"] == "collision" and S.parse_key(o["acb_key"])[1] < 0
                and o.name != scen_dup.name and o["acb_key"] != scen_dup["acb_key"]
                and any(c.get("acb_part", "").startswith("shape:") for c in o.children))
for o in bpy.context.selected_objects:
    o.select_set(False)
climb_el.select_set(True)
bpy.context.view_layer.objects.active = climb_el
res = bpy.ops.acb.generate_climb()
climb_key = climb_el["acb_key"]

def plain(op, loc, **kw):
    for o in bpy.context.selected_objects:
        o.select_set(False)
    op(location=loc, **kw)
    ob = bpy.context.active_object
    ob.select_set(True)
    return ob
base = sp[0].matrix_world.translation
acb_mat = next(m for m in bpy.data.materials if "acb_vis_material" in m)
cube = plain(bpy.ops.mesh.primitive_cube_add, base + Vector((0, 0, 8)), size=2)
cube.data.materials.append(acb_mat)
cube_name = cube.name
res_col = bpy.ops.acb.new_collision()
vis_col = bpy.context.scene.objects.get(cube_name + "_col")
cube2 = plain(bpy.ops.mesh.primitive_cube_add, base + Vector((4, 0, 8)), size=1)
cube2_name = cube2.name
res_scn = bpy.ops.acb.new_scenery()
scn = bpy.context.scene.objects.get(cube2_name + "_vis")
cyl = plain(bpy.ops.mesh.primitive_cylinder_add, scn.matrix_world.translation, radius=0.5, depth=3)
scn.select_set(True)
bpy.context.view_layer.objects.active = scn
res_rep = bpy.ops.acb.replace_visual()
print("SMOKE visual ops", res_col, res_scn, res_rep, vis_col and vis_col.type, scn.data.name)
# a big imported surface: 150 m, ~80k triangles -> split into pieces
grid = plain(bpy.ops.mesh.primitive_grid_add, base + Vector((0, 0, 30)), x_subdivisions=200, y_subdivisions=200,
             size=150)
grid_name, grid_tris = grid.name, 2 * len(grid.data.polygons)
res_big = bpy.ops.acb.new_collision(climb=False)
pieces = [o for o in bpy.context.scene.objects
          if o.name.startswith(grid_name + "_col") and "acb_key" in o and not o.get("acb_part")]
print("SMOKE big import", res_big, len(pieces), "pieces")
col_key, scn_key = vis_col["acb_key"], scn["acb_key"]
climb_drawn = bpy.data.objects.get(f"{climb_el.name}:climb")
print("SMOKE generate_climb", res, "drawn", climb_drawn is not None)
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
check(abs(position(d.obj(S.parse_key(scen_key)[0]).fields["GlobalMatrix"])[0] - scen_x) < 1e-3, "moved scenery x")
check(S.parse_key(scen_dup["acb_key"])[0] in d.info, "duplicated scenery present")
check(len(vis) > 100 and all(len(o.data.polygons) for o in vis), "visual meshes built")
check(not any("out-of-bounds" in l for l in log), "untouched out-of-bounds sections not rewritten")
check(sum(l.startswith("moved ") for l in log) == 2, "only the moved elements are moved")
check(abs(mesh_shape_geometry(d.obj(shape))[0][0][2] - col.data.vertices[0].co.z) < 1e-4, "collision vertex")
from acbmap.worlddata import WorldData
w = WorldData(d, os.path.dirname(forge))
check(len(w.get(2).obj.fields["chestSpawnPoints"]) == len([e for e in els if e.kind == "chest_spawn"]), "chest data")
from acbmap import guidance as G
from acbmap import ops as O
ce = G.systems(O.element_obj(d, S.parse_key(climb_key)))
n_climb = sum(len(G.edges(g)) for g in ce)
check(res == {"FINISHED"} and (n_climb == 0 or (climb_drawn is not None and len(climb_drawn.data.edges) == n_climb)),
      f"generated climb edges saved and drawn ({n_climb})")
from acbmap import visual as Vis
gc = Vis.mesh_geometry(d, Vis.entity_meshes(d, O.element_obj(d, S.parse_key(col_key)))[0])
gs = Vis.mesh_geometry(d, Vis.entity_meshes(d, O.element_obj(d, S.parse_key(scn_key)))[0])
check(res_col == res_scn == res_rep == {"FINISHED"}, "mesh to collision / scenery / replace visual ran")
chosen = int(acb_mat["acb_vis_material"], 16)
check(gc is not None and len(gc.tris) == 12 and len(gc.materials) == 1
      and (gc.materials[0] == chosen or d.name_of(gc.materials[0]).startswith(d.name_of(chosen) + "_l")),
      "visible collision cube written with the chosen map material (or its always-loaded copy)")
top = O.top_block(d)
big = [S.parse_key(o["acb_key"]) for o in pieces]
check(res_big == {"FINISHED"} and len(big) >= 9 and all(O.owning_block(d, k[0]) == top for k in big),
      f"big mesh split into {len(big)} pieces, all in the always-loaded cell")
def shape_tris(k):
    ic = O.inert_components(O.element_obj(d, k))[0][1]
    return len(mesh_shape_geometry(d.obj(int.from_bytes(ic.fields["RigidBody"].fields["Shape"].id, "little")))[1])
n_big = sum(shape_tris(k) for k in big)
check(n_big == grid_tris, f"pieces hold every collision triangle ({n_big} of {grid_tris})")
check(all(ic.fields["IsMerged"] == b"\x00" for k in big + [S.parse_key(col_key)]
          for _i, ic in O.inert_components(O.element_obj(d, k))), "new collision is not merged")
check(gs is not None and len(gs.tris) > 12 and max(v[2] for v in gs.verts) > 1.4, "scenery visual replaced by cylinder")
check(not O.inert_components(O.element_obj(d, S.parse_key(scn_key))), "scenery has no collision")
check(not probs, "no new structural problems")

# a new map from this one: clear the scenery (fresh scene, collision-only view for speed)
sc2 = bpy.data.scenes.new("clear")
s2 = S.Session(sc2, forge)
s2.build(with_visuals=False)
n_before = {k: sum(1 for e in classify(s2.doc, False) if e.kind == k) for k in ("spawn", "chest_spawn", "bench")}
r = s2.clear_scenery()
out2 = os.path.join(os.path.dirname(s.doc.cache), "edited", "smoke_clear_" + os.path.basename(forge))
probs2 = s2.save(out2)
d2 = MapDocument(out2)
els2 = classify(d2, False)
print("SMOKE clear", r, "problems", probs2)
from acbmap.kinds import block_membership
active2 = block_membership(d2)
left_roots = {e.uid for e in els2 if e.kind == "collision" and e.child < 0}
check(r["removed"] > 100 and left_roots <= set(r["templates"]) | set(r["hollowed"]) and not O.compounds(d2)
      and not set(r["templates"]) & set(active2),
      "clear scenery removed the collision roots (left: templates, never activated; hollowed ones the navmesh "
      "names) and every compound")
check({k: sum(1 for e in els2 if e.kind == k) for k in n_before} == n_before, f"gameplay kept {n_before}")
check(not [o for o in sc2.objects if o.get("acb_kind") == "collision" and not o.get("acb_part")
           and not o.get("acb_template") and not o.get("acb_hollow") and o.parent is None],
      "Blender collision objects removed (left: hidden templates, parts of kept gameplay groups)")
check(not probs2, "no new structural problems after clearing")
print("SMOKE RESULT", "PASS" if ok else "FAIL")
