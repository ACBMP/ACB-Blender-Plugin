"""Map session: the open MapDocument, the Blender objects built for it, and the sync of Blender edits back into
the document.

Every Blender object that stands for something in the forge carries string custom properties (IDProperty ints are
32-bit signed, ids aren't):
  acb_key   "<root uid hex>:<child index>"  -- an element (Entity / EntityGroup root, or a group child)
  acb_kind  element kind (kinds.py)
  acb_part  "" for the element itself; "shape:<n>" collision shape of a multi-shape element; "zone:<path>" a trigger
            zone (path into the element, '/'-separated); "oob:<n>" an out-of-bounds wall section
Collision mesh datablocks carry acb_shape (MeshShape uid hex) and are shared between every entity using the shape,
like in the game. Shift+D on an element creates a new element on save; deleting the Blender object deletes it.
"""
from __future__ import annotations

import hashlib
import math
import os
import struct

import bpy
from mathutils import Matrix, Quaternion, Vector

from acbmap import ops
from acbmap.doc import MapDocument, u32
from acbmap.geom import from_blender, mesh_shape_geometry, set_mesh_shape_geometry, to_blender
from acbmap.kinds import classify, component, components, group_children
from acbmap.schema import type_name
from acbmap.worlddata import WorldData

KIND_COLLECTIONS = {
    "spawn": "Spawns", "chest_spawn": "Chest Spawns", "crowd_flow": "Crowd Flows", "nav_flow": "Crowd Flows",
    "trigger": "Triggers", "out_of_bounds": "Out of Bounds", "collision": "Collision", "group": "Groups",
    "chase_breaker": "Chase Breakers", "elevator": "Interactive", "bench": "Interactive",
    "blend_group": "Interactive", "freerun_magnet": "Parkour", "corner_spin": "Parkour",
    "haystack": "Interactive", "hiding_place": "Interactive", "chase_breaker_door": "Chase Breakers",
}
EMPTY_STYLE = {
    "spawn": ("SINGLE_ARROW", 1.5), "chest_spawn": ("CUBE", 0.6), "crowd_flow": ("PLAIN_AXES", 0.4),
    "trigger": ("PLAIN_AXES", 0.5), "out_of_bounds": ("PLAIN_AXES", 1.0), "group": ("PLAIN_AXES", 0.8),
    "chase_breaker": ("ARROWS", 1.0), "elevator": ("CONE", 1.0), "bench": ("CUBE", 0.5),
    "blend_group": ("SPHERE", 1.0), "freerun_magnet": ("SPHERE", 0.4), "haystack": ("CUBE", 0.8),
}
SKIP_KINDS = {"visual", "other"}      # no shape, no gameplay: not imported in milestone 1
LOCKED_KINDS = {"crowd_flow", "nav_flow"}   # their points carry navmesh triangle refs: moving them breaks the crowd

SESSIONS: dict[str, "Session"] = {}


def keystr(key) -> str:
    return f"{key[0]:08x}:{key[1]}"


def parse_key(s: str):
    a, b = s.split(":")
    return int(a, 16), int(b)


def mat_close(a: bytes, b: bytes, eps=1e-4) -> bool:
    fa = struct.unpack("<16f", a)
    fb = struct.unpack("<16f", b)
    return all(abs(x - y) <= eps * max(1.0, abs(x)) for x, y in zip(fa, fb))


def mesh_hash(me) -> str:
    h = hashlib.sha1()
    for v in me.vertices:
        h.update(struct.pack("<3f", *v.co))
    for p in me.polygons:
        h.update(struct.pack(f"<{len(p.vertices)}I", *p.vertices))
        h.update(struct.pack("<H", p.material_index))
    return h.hexdigest()


def get(scene) -> "Session | None":
    return SESSIONS.get(scene.name)


class Session:
    def __init__(self, scene, forge_path: str, multi_dir: str | None = None):
        self.scene = scene
        self.doc = MapDocument(forge_path)
        self.source = forge_path
        self.multi_dir = multi_dir or os.path.dirname(forge_path)
        self.wd = WorldData(self.doc, self.multi_dir)
        self.objects: dict[str, str] = {}      # acb_key/part -> Blender object name
        self.baseline: dict[str, bytes] = {}   # same key -> matrix / zone / section bytes at last sync
        self.mesh_base: dict[str, str] = {}    # mesh name -> mesh_hash at last sync
        self.vip_paths = self.wd.vip_paths()
        self.flow_names: dict[int, str] = {}   # flow entity uid -> Blender object
        self.id_to_obj: dict[int, str] = {}    # any component/entity id -> Blender object (inspector links)
        self.log: list[str] = []
        scene["acb_forge"] = forge_path
        scene["acb_multi"] = self.multi_dir
        SESSIONS[scene.name] = self

    # ------------------------------------------------------------ build --

    def coll(self, name: str, parent=None):
        root = self.root_coll if parent is None else parent
        full = f"{name} [{self.tag}]"
        c = bpy.data.collections.get(full)
        if c is None:
            c = bpy.data.collections.new(full)
            root.children.link(c)
        return c

    def build(self, with_collision=True, progress=None):
        self.tag = os.path.basename(self.source).replace("DataPC_", "").replace(".forge", "")
        self.root_coll = bpy.data.collections.get(f"ACB {self.tag}")
        if self.root_coll is None:
            self.root_coll = bpy.data.collections.new(f"ACB {self.tag}")
            self.scene.collection.children.link(self.root_coll)
        elements = classify(self.doc)
        roots_obj: dict[int, bpy.types.Object] = {}
        n = len(elements)
        for i, e in enumerate(sorted(elements, key=lambda e: e.child)):   # roots before group children
            if progress and i % 200 == 0:
                progress(i / max(n, 1))
            if e.kind in SKIP_KINDS and e.child < 0 and type_name(e.obj.type_hash) != "EntityGroup":
                continue
            if e.kind == "collision" and not with_collision:
                continue
            ob = self._element_object(e)
            if ob is None:
                continue
            if e.child >= 0 and e.uid in roots_obj:
                ob.parent = roots_obj[e.uid]
                ob.matrix_parent_inverse = roots_obj[e.uid].matrix_world.inverted()
            if e.child < 0:
                roots_obj[e.uid] = ob
        self._build_flow_links()
        self._build_vip_curves()
        self.snapshot()

    def _element_object(self, e):
        k = keystr(e.key)
        shapes = [(i, ic) for i, ic in ops.inert_components(e.obj)]
        coll = self.coll(KIND_COLLECTIONS.get(e.kind, "Other"))
        name = self.doc.name_of(e.uid) if e.child < 0 else f"{self.doc.name_of(e.uid)}/{e.child}"
        mw = Matrix(to_blender(e.obj.fields["GlobalMatrix"]))
        if e.kind == "collision" and len(shapes) == 1:
            me = self._shape_mesh(shapes[0][1])
            if me is None:
                return None
            ob = bpy.data.objects.new(name, me)
            shape_children = []
        else:
            ob = bpy.data.objects.new(name, None)
            style, size = EMPTY_STYLE.get(e.kind, ("PLAIN_AXES", 0.6))
            ob.empty_display_type = style
            ob.empty_display_size = size
            shape_children = shapes
        coll.objects.link(ob)
        ob.matrix_world = mw
        ob["acb_key"] = k
        ob["acb_kind"] = e.kind
        ob["acb_part"] = ""
        self.objects[k] = ob.name
        self.id_to_obj[u32(e.obj.id)] = ob.name
        for _n, c in components(e.obj):
            self.id_to_obj[u32(c.id)] = ob.name
        if e.kind in LOCKED_KINDS:
            ob.lock_location = ob.lock_rotation = ob.lock_scale = (True, True, True)
        if e.kind in ("crowd_flow", "nav_flow"):
            self.flow_names[e.uid] = ob.name
        for si, (_ci, ic) in enumerate(shape_children):
            me = self._shape_mesh(ic)
            if me is None:
                continue
            ch = bpy.data.objects.new(f"{name}:shape{si}", me)
            self.coll("Collision").objects.link(ch)
            ch.parent = ob
            ch["acb_key"] = k
            ch["acb_part"] = f"shape:{si}"
            ch.hide_select = e.kind not in ("collision",)
        self._zones(e, ob, k)
        if e.kind == "out_of_bounds":
            self._oob_sections(e, ob, k)
        return ob

    def _shape_mesh(self, ic):
        sid = u32(ic.fields["RigidBody"].fields["Shape"].id)
        mname = f"ACBShape_{sid:08x}"
        me = bpy.data.meshes.get(mname)
        if me is not None:
            return me
        o = self.doc.obj(sid) if sid in self.doc.info else None
        if o is None or type_name(o.type_hash) != "MeshShape":
            return None
        v, t, m = mesh_shape_geometry(o)
        me = bpy.data.meshes.new(mname)
        me.from_pydata(v, [], t)
        for ref in o.fields["Materials"]:
            mid = u32(ref.id)
            me.materials.append(self._col_material(mid))
        if me.materials:
            me.polygons.foreach_set("material_index", m)
        me.update()
        me["acb_shape"] = f"{sid:08x}"
        self.mesh_base[me.name] = mesh_hash(me)
        return me

    def _col_material(self, mid: int):
        nm = self.doc.name_of(mid) if mid in self.doc.info else f"{mid:08x}"
        mname = f"ACBCol_{nm}"
        mat = bpy.data.materials.get(mname)
        if mat is None:
            mat = bpy.data.materials.new(mname)
            h = (mid * 2654435761) & 0xFFFFFF
            mat.diffuse_color = (0.35 + (h & 0xFF) / 600, 0.35 + (h >> 8 & 0xFF) / 600, 0.35 + (h >> 16) / 600, 1)
            mat["acb_material"] = f"{mid:08x}"
        return mat

    def _zones(self, e, ob, k):
        tc = component(e.obj, "TriggerComponent")
        if tc is None:
            return
        ci = next(i for i, p in enumerate(e.obj.fields["Components"]) if p.obj is tc)
        zones = []
        st = tc.fields.get("Settings")
        if st is not None and hasattr(st, "fields"):
            if st.fields.get("Zone") is not None and st.fields["Zone"].obj is not None:
                zones.append((["Components", ci, "Settings", "Zone"], st.fields["Zone"].obj))
            for j, zp in enumerate(st.fields.get("ZoneList", [])):
                if zp.obj is not None:
                    zones.append((["Components", ci, "Settings", "ZoneList", j], zp.obj))
        for path, zo in zones:
            zt = u32(zo.fields["ZoneType"])
            zob = bpy.data.objects.new(f"{ob.name}:zone", None)
            self.coll("Trigger Zones").objects.link(zob)
            zob.parent = ob
            if zt == 1:
                zob.empty_display_type = "SPHERE"
                c = struct.unpack("<3f", zo.fields["SphereLocalCenter"][:12])
                r = struct.unpack("<f", zo.fields["SphereRadius"])[0]
                zob.matrix_basis = Matrix.Translation(c) @ Matrix.Diagonal((r, r, r, 1))
            else:
                zob.empty_display_type = "CUBE"
                c = struct.unpack("<3f", zo.fields["BoxLocalCenter"][:12])
                he = struct.unpack("<3f", zo.fields["BoxHalfExtents"][:12])
                zob.matrix_basis = Matrix.Translation(c) @ Matrix.Diagonal((*he, 1))
            zob.empty_display_size = 1.0
            zob["acb_key"] = k
            zob["acb_part"] = "zone:" + "/".join(str(p) for p in path)
            self.objects[f"{k}|{zob['acb_part']}"] = zob.name

    def _oob_sections(self, e, ob, k):
        oc = component(e.obj, "OutOfBoundsComponent")
        ci = next(i for i, p in enumerate(e.obj.fields["Components"]) if p.obj is oc)
        me = bpy.data.meshes.get("ACB_OOB_quad")
        if me is None:
            me = bpy.data.meshes.new("ACB_OOB_quad")
            me.from_pydata([(-0.5, 0, 0), (0.5, 0, 0), (0.5, 0, 1), (-0.5, 0, 1)], [], [(0, 1, 2, 3)])
        for j, s in enumerate(oc.fields["Sections"]):
            pos = struct.unpack("<3f", s.fields["GlobalPosition"][:12])
            qx, qy, qz, qw = struct.unpack("<4f", s.fields["GlobalRotation"])
            w, h = struct.unpack("<2f", s.fields["Size"])
            sob = bpy.data.objects.new(f"{ob.name}:oob{j}", me)
            self.coll("Out of Bounds").objects.link(sob)
            sob.matrix_world = (Matrix.Translation(pos) @ Quaternion((qw, qx, qy, qz)).to_matrix().to_4x4()
                                @ Matrix.Diagonal((w, 1, h, 1)))
            sob.display_type = "WIRE"
            sob["acb_key"] = k
            sob["acb_part"] = f"oob:{ci}:{j}"
            self.objects[f"{k}|{sob['acb_part']}"] = sob.name

    def _build_flow_links(self):
        verts, edges, idx = [], [], {}
        for e in classify(self.doc, with_children=False):
            if e.kind != "crowd_flow":
                continue
            cf = component(e.obj, "CrowdFlow")
            m = Matrix(to_blender(e.obj.fields["GlobalMatrix"]))
            pts = [m @ Vector(struct.unpack("<3f", p.fields["FlowPosition"][:12])) for p in cf.fields["NavFlowPointsLocal"]]
            base = len(verts)
            verts += pts
            edges += [(base + i, base + i + 1) for i in range(len(pts) - 1)]
            idx[e.uid] = (base, base + len(pts) - 1)
        me = bpy.data.meshes.new(f"ACB_CrowdFlowLines_{self.tag}")
        me.from_pydata([tuple(v) for v in verts], edges, [])
        ob = bpy.data.objects.new(f"Crowd flow lines [{self.tag}]", me)
        self.coll("Crowd Flows").objects.link(ob)
        ob.hide_select = True

    def _build_vip_curves(self):
        c = self.coll("Escort Paths")
        for ob in list(c.objects):
            bpy.data.objects.remove(ob)
        for pi, path in enumerate(self.vip_paths):
            cu = bpy.data.curves.new(f"ACB_EscortPath_{pi}", "CURVE")
            cu.dimensions = "3D"
            cu.bevel_depth = 0.15
            sp = cu.splines.new("POLY")
            pts = []
            for n in path:
                fo = bpy.data.objects.get(self.flow_names.get(n["flow"], ""))
                if fo is not None:
                    pts.append(fo.matrix_world.translation + Vector((0, 0, 1.0)))
            if not pts:
                continue
            sp.points.add(len(pts) - 1)
            for p, v in zip(sp.points, pts):
                p.co = (*v, 1)
            ob = bpy.data.objects.new(f"Escort path {pi}", cu)
            c.objects.link(ob)
            ob.hide_select = True
            ob.color = (1.0, 0.6 - 0.1 * pi, 0.1, 1)

    def attach(self):
        """Rebind to the ACB objects already in the scene (a reopened .blend). Element baselines come from the
        document, so Blender edits not yet applied before the .blend was saved are still picked up; zone/section
        baselines are taken from the scene as it is."""
        self.tag = os.path.basename(self.source).replace("DataPC_", "").replace(".forge", "")
        self.root_coll = bpy.data.collections.get(f"ACB {self.tag}") or self.scene.collection
        for ob in self.scene.objects:
            if "acb_key" not in ob:
                continue
            k, part = ob["acb_key"], ob.get("acb_part", "")
            if part == "":
                self.objects[k] = ob.name
                key = parse_key(k)
                try:
                    o = ops.element_obj(self.doc, key)
                except Exception:
                    continue
                self.baseline[k] = o.fields["GlobalMatrix"]
                self.id_to_obj[u32(o.id)] = ob.name
                for _n, c in components(o):
                    self.id_to_obj[u32(c.id)] = ob.name
                if ob.get("acb_kind") in ("crowd_flow", "nav_flow"):
                    self.flow_names[key[0]] = ob.name
            elif part.startswith(("zone:", "oob:")):
                self.objects[f"{k}|{part}"] = ob.name
                self.baseline[f"{k}|{part}"] = self._state(ob)
        for me in bpy.data.meshes:
            if "acb_shape" not in me:
                continue
            sid = int(me["acb_shape"], 16)
            o = self.doc.obj(sid) if sid in self.doc.info else None
            if o is None:
                continue
            v, t, m = mesh_shape_geometry(o)
            tmp = bpy.data.meshes.new("acb_tmp")
            tmp.from_pydata(v, [], t)
            tmp.materials.append(None)
            tmp.polygons.foreach_set("material_index", m)
            self.mesh_base[me.name] = mesh_hash(tmp)
            bpy.data.meshes.remove(tmp)

    # ------------------------------------------------------------ sync --

    def snapshot(self):
        bpy.context.view_layer.update()
        for k, name in self.objects.items():
            ob = bpy.data.objects.get(name)
            if ob is not None:
                self.baseline[k] = self._state(ob)
        for me in bpy.data.meshes:
            if "acb_shape" in me:
                self.mesh_base[me.name] = mesh_hash(me)

    def _state(self, ob) -> bytes:
        part = ob.get("acb_part", "")
        if part.startswith("zone:"):
            return struct.pack("<16f", *[x for row in ob.matrix_basis for x in row])
        return from_blender(ob.matrix_world)

    def sync(self) -> list[str]:
        """Push Blender-side changes into the document. Returns a log of what changed."""
        doc, log = self.doc, []
        bpy.context.view_layer.update()   # matrix_world of just-moved objects is stale until the depsgraph runs
        by_key: dict[str, list] = {}
        for ob in self.scene.objects:
            if "acb_key" in ob:
                by_key.setdefault(ob["acb_key"] + ("|" + ob["acb_part"] if ob.get("acb_part") else ""), []).append(ob)
        chests_changed = False
        # deletions (elements only; parts follow their element)
        for k, name in list(self.objects.items()):
            if "|" in k or k in by_key:
                continue
            key = parse_key(k)
            kind = self._kind_of_key(k)
            try:
                ops.delete(doc, key)
                log.append(f"deleted {name}")
                chests_changed |= kind == "chest_spawn"
            except ops.EditError as ex:
                log.append(f"NOT deleted {name}: {ex}")
            del self.objects[k]
            self.baseline.pop(k, None)
        # moves of originals, roots before group children (a group move carries its children; their own matrices
        # are then written exactly)
        items = sorted(((k, obs) for k, obs in by_key.items() if "|" not in k), key=lambda kv: parse_key(kv[0])[1])
        for k, obs in items:
            orig = bpy.data.objects.get(self.objects.get(k, ""))
            if orig is None or orig not in obs:
                continue
            m = from_blender(orig.matrix_world)
            if not mat_close(m, self.baseline.get(k, m)):
                ops.set_matrix(doc, parse_key(k), m)
                log.append(f"moved {orig.name}")
                chests_changed |= orig.get("acb_kind") == "chest_spawn"
        # duplicates (Shift+D copies the custom properties) -> new elements
        for k, obs in items:
            orig_name = self.objects.get(k)
            orig = bpy.data.objects.get(orig_name or "")
            for ob in obs:
                if ob.name == orig_name:
                    continue
                if parse_key(k)[1] >= 0:
                    log.append(f"skipped {ob.name}: duplicating group children isn't supported, duplicate the group")
                    continue
                try:
                    nk = ops.duplicate(doc, parse_key(k), from_blender(ob.matrix_world))
                except ops.EditError as ex:
                    log.append(f"NOT added {ob.name}: {ex}")
                    continue
                ob["acb_key"] = keystr(nk)
                self.objects[keystr(nk)] = ob.name
                if ob.type == "MESH" and orig.type == "MESH" and ob.data != orig.data:
                    # Blender copied the mesh: give the new element its own MeshShape so editing one
                    # doesn't change the other
                    sid = ops.make_shape_unique(doc, nk)
                    ob.data["acb_shape"] = f"{sid:08x}"
                    ob.data.name = f"ACBShape_{sid:08x}"
                log.append(f"added {ob.name} (copy of {orig_name})")
                chests_changed |= ob.get("acb_kind") == "chest_spawn"
        # zones and out-of-bounds sections
        for k, obs in by_key.items():
            if "|" not in k:
                continue
            ob = obs[0]
            st = self._state(ob)
            if self.baseline.get(k) is not None and mat_close(st, self.baseline[k]):
                continue
            ek, part = k.split("|", 1)
            if part.startswith("zone:"):
                self._write_zone(parse_key(ek), part[5:], ob)
                log.append(f"zone {ob.name}")
            elif part.startswith("oob:"):
                self._write_oob(parse_key(ek), part, ob)
                log.append(f"out-of-bounds section {ob.name}")
        # collision geometry
        for me in bpy.data.meshes:
            if "acb_shape" not in me or me.users == 0:
                continue
            h = mesh_hash(me)
            if self.mesh_base.get(me.name) == h:
                continue
            self._write_shape(me)
            log.append(f"collision shape {me.name} ({len(me.vertices)} verts)")
        if chests_changed:
            n = self.wd.sync_chests()
            log.append(f"chest capture world data regenerated ({n} chests)")
        self.snapshot()
        self.log += log
        return log

    def _kind_of_key(self, k):
        try:
            return classify_one(self.doc, parse_key(k))
        except Exception:
            return ""

    def _write_zone(self, key, path, ob):
        o = ops.element_obj(self.doc, key)
        p = [int(x) if x.isdigit() else x for x in path.split("/")]
        from acbmap.fields import get
        zo = get(o, p)
        loc, _rot, sc = ob.matrix_basis.decompose()
        if u32(zo.fields["ZoneType"]) == 1:
            zo.fields["SphereLocalCenter"] = struct.pack("<4f", *loc, 0)
            zo.fields["SphereRadius"] = struct.pack("<f", max(sc))
        else:
            zo.fields["BoxLocalCenter"] = struct.pack("<4f", *loc, 0)
            zo.fields["BoxHalfExtents"] = struct.pack("<4f", *sc, 0)
        self.doc.touch(key[0])

    def _write_oob(self, key, part, ob):
        _t, ci, j = part.split(":")
        o = ops.element_obj(self.doc, key)
        s = o.fields["Components"][int(ci)].obj.fields["Sections"][int(j)]
        loc, rot, sc = ob.matrix_world.decompose()
        n = rot @ Vector((0, 1, 0))
        s.fields["GlobalPosition"] = struct.pack("<4f", *loc, 0)
        s.fields["GlobalRotation"] = struct.pack("<4f", rot.x, rot.y, rot.z, rot.w)
        s.fields["GlobalNormal"] = struct.pack("<4f", *n, 0)
        s.fields["Size"] = struct.pack("<2f", sc.x, sc.z)
        self.doc.touch(key[0])

    def _write_shape(self, me):
        import bmesh
        sid = int(me["acb_shape"], 16)
        bm = bmesh.new()
        bm.from_mesh(me)
        bmesh.ops.triangulate(bm, faces=bm.faces[:])
        verts = [tuple(v.co) for v in bm.verts]
        bm.verts.index_update()
        tris = [tuple(v.index for v in f.verts) for f in bm.faces]
        mats = [f.material_index for f in bm.faces]
        bm.free()
        o = self.doc.obj(sid)
        n_mat = len(o.fields["Materials"])
        mats = [min(m, max(n_mat - 1, 0)) for m in mats]
        set_mesh_shape_geometry(o, verts, tris, mats)
        self.doc.touch(sid)

    # ------------------------------------------------------------ save --

    def save(self, out_path: str) -> list[str]:
        from acbmap.checks import new_problems
        self.sync()
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        rebuilt = self.doc.save(out_path)
        problems = new_problems(MapDocument(out_path), MapDocument(self.source))
        self.last_save = out_path
        self.last_problems = problems
        self.log.append(f"saved {out_path}: {len(rebuilt)} entries rebuilt, {len(problems)} new problems")
        return problems


def classify_one(doc, key):
    from acbmap.kinds import kind_of
    o = ops.element_obj(doc, key)
    return kind_of(o, key[1] < 0 and type_name(o.type_hash) == "EntityGroup")
