"""Map session: the open MapDocument, the Blender objects built for it, and the sync of Blender edits back into
the document.

Every Blender object that stands for something in the forge carries string custom properties (IDProperty ints are
32-bit signed, ids aren't):
  acb_key   "<root uid hex>:<child index>"  -- an element (Entity / EntityGroup root, or a group child)
  acb_kind  element kind (kinds.py)
  acb_part  "" for the element itself; "shape:<n>" collision shape of a multi-shape element (or of any element shown
            by its visual mesh); "zone:<path>" a trigger zone (path into the element, '/'-separated); "oobwall" the
            out-of-bounds boundary as one polyline mesh (vertices = wall base corners in world space, per-vertex
            height in the "acb_height" attribute, drawn as a wall by a Screw modifier); editing it regenerates the
            element's sections, collision strip and fog mesh (acbmap.oob)
Collision mesh datablocks carry acb_shape (MeshShape uid hex) and are shared between every entity using the shape,
like in the game; visual mesh datablocks carry acb_visual (Mesh uid hex) and are shared the same way, but are
display-only. An element with a visual mesh is that mesh (so clicking the scenery selects it), with its collision as
child objects in the Collision collection, which is hidden while visuals are shown. Shift+D on an element creates a
new element on save; deleting the Blender object deletes it.
"""
from __future__ import annotations

import hashlib
import math
import os
import struct

import bpy
from mathutils import Matrix, Quaternion, Vector

from acbmap import guidance as G
from acbmap import oob as OOB
from acbmap import ops
from acbmap import split as SP
from acbmap import visual as V
from acbmap.doc import MapDocument, u32
from acbmap.geom import from_blender, mesh_shape_geometry, set_mesh_shape_geometry, to_blender
from acbmap.kinds import Element, classify, component, components, group_children
from acbmap.schema import type_name
from acbmap.worlddata import WorldData

KIND_COLLECTIONS = {
    "spawn": "Spawns", "chest_spawn": "Chest Spawns", "crowd_flow": "Crowd Flows", "nav_flow": "Crowd Flows",
    "trigger": "Triggers", "out_of_bounds": "Out of Bounds", "collision": "Collision", "group": "Groups",
    "chase_breaker": "Chase Breakers", "elevator": "Interactive", "bench": "Interactive",
    "blend_group": "Interactive", "freerun_magnet": "Parkour", "corner_spin": "Parkour",
    "haystack": "Interactive", "hiding_place": "Interactive", "chase_breaker_door": "Chase Breakers",
    "visual": "Scenery", "other": "Scenery",
}
EMPTY_STYLE = {
    "spawn": ("SINGLE_ARROW", 1.5), "chest_spawn": ("CUBE", 0.6), "crowd_flow": ("PLAIN_AXES", 0.4),
    "trigger": ("PLAIN_AXES", 0.5), "out_of_bounds": ("PLAIN_AXES", 1.0), "group": ("PLAIN_AXES", 0.8),
    "chase_breaker": ("ARROWS", 1.0), "elevator": ("CONE", 1.0), "bench": ("CUBE", 0.5),
    "blend_group": ("SPHERE", 1.0), "freerun_magnet": ("SPHERE", 0.4), "haystack": ("CUBE", 0.8),
}
SKIP_KINDS = {"visual", "other"}      # no shape, no gameplay: imported only when they have a visual mesh
WIRE_VISUAL_KINDS = {"out_of_bounds"}   # visual = the boundary fog wall: a wireframe child, the element stays an empty
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


def show_textures():
    """Solid-mode 3D views colour by texture, so the scenery reads without switching to Material Preview."""
    for win in getattr(bpy.context.window_manager, "windows", []):
        for area in win.screen.areas:
            if area.type == "VIEW_3D":
                for sp in area.spaces:
                    if sp.type == "VIEW_3D":
                        sp.shading.color_type = "TEXTURE"


def normal_decode_group():
    """Node group turning an ACB normal map sample into a Blender (OpenGL) tangent-space colour: picks x from red or
    alpha, inverts green (the maps are DirectX style) and rebuilds z for two-channel maps."""
    g = bpy.data.node_groups.get("ACB Normal Decode")
    if g is not None:
        return g
    g = bpy.data.node_groups.new("ACB Normal Decode", "ShaderNodeTree")
    g.interface.new_socket("Color", in_out="INPUT", socket_type="NodeSocketColor")
    g.interface.new_socket("Alpha", in_out="INPUT", socket_type="NodeSocketFloat")
    s = g.interface.new_socket("Alpha in X", in_out="INPUT", socket_type="NodeSocketFloat")
    s.min_value, s.max_value = 0.0, 1.0
    g.interface.new_socket("Color", in_out="OUTPUT", socket_type="NodeSocketColor")
    n, ln = g.nodes, g.links
    gin, gout = n.new("NodeGroupInput"), n.new("NodeGroupOutput")
    sep = n.new("ShaderNodeSeparateColor")
    ln.new(gin.outputs["Color"], sep.inputs["Color"])

    def math(op, a, b=None):
        """A Math node; operands are sockets or constants."""
        m = n.new("ShaderNodeMath")
        m.operation = op
        for i, x in enumerate((a, b)):
            if isinstance(x, (int, float)):
                m.inputs[i].default_value = x
            elif x is not None:
                ln.new(x, m.inputs[i])
        return m.outputs[0]

    def to_color(c):   # [-1, 1] -> [0, 1]
        return math("ADD", math("MULTIPLY", c, 0.5), 0.5)
    mix = n.new("ShaderNodeMix")              # x channel: red (rgb maps) or alpha (two-channel maps)
    mix.data_type = "FLOAT"
    ln.new(gin.outputs["Alpha in X"], mix.inputs["Factor"])
    ln.new(sep.outputs["Red"], mix.inputs["A"])
    ln.new(gin.outputs["Alpha"], mix.inputs["B"])
    xs = mix.outputs["Result"]
    x = math("SUBTRACT", math("MULTIPLY", xs, 2.0), 1.0)
    y = math("SUBTRACT", 1.0, math("MULTIPLY", sep.outputs["Green"], 2.0))         # DirectX -> OpenGL: -y
    zd = math("SQRT", math("MAXIMUM", math("SUBTRACT", math("SUBTRACT", 1.0, math("MULTIPLY", x, x)),
                                             math("MULTIPLY", y, y)), 0.0))
    zs = math("SUBTRACT", math("MULTIPLY", sep.outputs["Blue"], 2.0), 1.0)
    zmix = n.new("ShaderNodeMix")
    zmix.data_type = "FLOAT"
    ln.new(gin.outputs["Alpha in X"], zmix.inputs["Factor"])
    ln.new(zs, zmix.inputs["A"])
    ln.new(zd, zmix.inputs["B"])
    comb = n.new("ShaderNodeCombineColor")
    ln.new(to_color(x), comb.inputs["Red"])
    ln.new(to_color(y), comb.inputs["Green"])
    ln.new(to_color(zmix.outputs["Result"]), comb.inputs["Blue"])
    ln.new(comb.outputs["Color"], gout.inputs["Color"])
    return g


def get(scene) -> "Session | None":
    return SESSIONS.get(scene.name)


class Session:
    # Blender structs are looked up by name on every use: a stored Scene/Collection reference goes stale ("StructRNA
    # of type Scene has been removed") whenever Blender rebuilds its data, e.g. on undo.
    @property
    def scene(self):
        sc = bpy.data.scenes.get(self.scene_name)
        if sc is None:
            raise RuntimeError(f"scene {self.scene_name!r} no longer exists; reopen the map")
        return sc

    @scene.setter
    def scene(self, sc):
        self.scene_name = sc.name

    @property
    def root_coll(self):
        c = bpy.data.collections.get(self.root_coll_name) if self.root_coll_name else None
        return c if c is not None else self.scene.collection

    @root_coll.setter
    def root_coll(self, c):
        self.root_coll_name = None if c is None or c == self.scene.collection else c.name

    def __init__(self, scene, forge_path: str, multi_dir: str | None = None):
        self.scene = scene
        self.root_coll_name = None
        self.doc = MapDocument(forge_path)
        self.migrated = 0
        if any(u >= ops.RUNTIME_ID_BASE for u in self.doc.info):   # saved by an editor version before the fix
            self.migrated = ops.migrate_runtime_ids(self.doc)
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

    def build(self, with_collision=True, with_visuals=True, progress=None):
        self.with_collision = with_collision
        self.with_visuals = with_visuals
        self.tag = base = os.path.basename(self.source).replace("DataPC_", "").replace(".forge", "")
        i = 1
        while True:   # collection names are global: a map open in another scene gets its own set here
            c = bpy.data.collections.get(f"ACB {self.tag}")
            if c is None or c.name in self.scene.collection.children:
                break
            self.tag = f"{base} {self.scene.name}" + (f" {i}" if i > 1 else "")
            i += 1
        self.scene["acb_tag"] = self.tag
        rc = bpy.data.collections.get(f"ACB {self.tag}")
        if rc is None:
            rc = bpy.data.collections.new(f"ACB {self.tag}")
            self.scene.collection.children.link(rc)
        self.root_coll = rc
        elements = classify(self.doc)
        roots_obj: dict[int, bpy.types.Object] = {}
        n = len(elements)
        for i, e in enumerate(sorted(elements, key=lambda e: e.child)):   # roots before group children
            if progress and i % 200 == 0:
                progress(i / max(n, 1))
            has_visual = with_visuals and bool(V.entity_meshes(self.doc, e.obj))
            if (e.kind in SKIP_KINDS and e.child < 0 and type_name(e.obj.type_hash) != "EntityGroup"
                    and not has_visual):
                continue
            if e.kind == "collision" and not with_collision and not has_visual:
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
        if with_visuals:
            self.set_collision_visible(False)
            show_textures()
        self.set_collection_visible("Climb Edges", False)
        self.snapshot()

    def set_collision_visible(self, visible: bool):
        """Show or hide the Collision collection (hidden by default while visual meshes are shown)."""
        self.set_collection_visible("Collision", visible)

    def collision_visible(self) -> bool:
        return self.collection_visible("Collision")

    def set_collection_visible(self, coll: str, visible: bool):
        name = f"{coll} [{self.tag}]"

        def find(lc):
            if lc is None:   # a scene that was never made active (background runs)
                return None
            if lc.collection.name == name:
                return lc
            for c in lc.children:
                r = find(c)
                if r is not None:
                    return r
            return None
        for vl in self.scene.view_layers:
            lc = find(vl.layer_collection)
            if lc is not None:
                lc.hide_viewport = not visible

    def collection_visible(self, coll: str) -> bool:
        lc = bpy.context.view_layer.layer_collection
        stack = [lc]
        while stack:
            c = stack.pop()
            if c.collection.name == f"{coll} [{self.tag}]":
                return not c.hide_viewport
            stack += list(c.children)
        return True

    def _element_object(self, e, name: str | None = None):
        k = keystr(e.key)
        shapes = [(i, ic) for i, ic in ops.inert_components(e.obj)]
        if not getattr(self, "with_collision", True):
            shapes = []
        visuals = []
        if getattr(self, "with_visuals", False):
            visuals = [m for m in (self._visual_mesh(u) for u in V.entity_meshes(self.doc, e.obj)) if m is not None]
        coll_name = KIND_COLLECTIONS.get(e.kind, "Other")
        if visuals and coll_name == "Collision":
            coll_name = "Scenery"   # the Collision collection gets hidden; the scenery itself must stay visible
        coll = self.coll(coll_name)
        if name is None:
            name = self.doc.name_of(e.uid) if e.child < 0 else f"{self.doc.name_of(e.uid)}/{e.child}"
        mw = Matrix(to_blender(e.obj.fields["GlobalMatrix"]))
        wire_visuals = []
        if e.kind in WIRE_VISUAL_KINDS:
            wire_visuals, visuals = visuals, []
        if visuals:
            ob = bpy.data.objects.new(name, visuals[0])
            shape_children = shapes
        elif e.kind == "collision" and len(shapes) == 1:
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
            ch.hide_select = e.kind not in ("collision", "visual", "other")
        for vi, me in enumerate(visuals[1:], 1):
            # further Visual components: display-only children (no acb_key, so they're never taken for elements)
            ch = bpy.data.objects.new(f"{name}:visual{vi}", me)
            coll.objects.link(ch)
            ch.parent = ob
            ch.hide_select = True
            ch["acb_visual_of"] = k
        for vi, me in enumerate(wire_visuals):
            ch = bpy.data.objects.new(f"{name}:visual{vi}", me)
            coll.objects.link(ch)
            ch.parent = ob
            ch.hide_select = True
            ch.display_type = "WIRE"
            ch["acb_visual_of"] = k
        self._zones(e, ob, k)
        self.climb_object(e.obj, ob, k)
        if e.kind == "out_of_bounds":
            self._oob_wall(e, ob, k)
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

    def default_material(self) -> int:
        if getattr(self, "_default_mat", None) is None:
            self._default_mat = V.default_material(self.doc)
        return self._default_mat

    def material_from_image(self, mat) -> int | None:
        """A Blender material whose Base Color comes from an Image Texture becomes a new map material showing that
        image (power-of-two sized, at most 1024), once per image: the material is tagged acb_vis_material."""
        import numpy as np
        from acbmap import textures as TX
        if not mat.use_nodes:
            return None
        img = next((n.image for n in mat.node_tree.nodes if n.type == "TEX_IMAGE" and n.image is not None
                    and any(l.to_socket.name in ("Base Color", "Color") for o in n.outputs for l in o.links)), None)
        if img is None or img.size[0] == 0:
            return None
        memo = self.__dict__.setdefault("_image_mats", {})
        if img.name not in memo:
            w, h = img.size
            th, tw = TX.fit_size(h, w)
            src = img
            if (tw, th) != (w, h):
                src = img.copy()
                src.scale(tw, th)
            px = np.empty(tw * th * 4, np.float32)
            src.pixels.foreach_get(px)
            if src is not img:
                bpy.data.images.remove(src)
            rgba = (np.clip(px.reshape(th, tw, 4)[::-1], 0, 1) * 255 + 0.5).astype(np.uint8)   # top row first
            name = "ACBEdit_" + "".join(c if c.isalnum() else "_" for c in os.path.splitext(img.name)[0])
            memo[img.name] = TX.new_material(self.doc, rgba, name)
        mat["acb_vis_material"] = f"{memo[img.name]:08x}"
        return memo[img.name]

    def mesh_input(self, ob, to_entity: Matrix) -> "V.MeshInput":
        """A Blender mesh object as encoder input in an entity's frame (to_entity: object-local -> entity-local).
        Slots holding one of the map's materials (ACBMat_*) keep it; others get the map's most used textured
        material. Without a UV map, faces get box-projected uvs (one tile per 2 m)."""
        me = ob.data
        me.calc_loop_triangles()
        nmat = to_entity.to_3x3().inverted().transposed()
        corner = me.corner_normals if hasattr(me, "corner_normals") else None
        uvl = me.uv_layers.active
        slot_mat = []
        for slot in ob.material_slots:
            m = slot.material
            if m is not None and "acb_vis_material" not in m:
                self.material_from_image(m)
            slot_mat.append(int(m["acb_vis_material"], 16) if m is not None and "acb_vis_material" in m
                            else self.default_material())
        if not slot_mat:
            slot_mat = [self.default_material()]
        mats = list(dict.fromkeys(slot_mat))
        index: dict[tuple, int] = {}
        verts, normals, uvs, tris, tri_mat = [], [], [], [], []
        for lt in me.loop_triangles:
            fn = (nmat @ lt.normal).normalized()
            axis = max(range(3), key=lambda k: abs(fn[k]))
            tri = []
            for li, vi in zip(lt.loops, lt.vertices):
                p = to_entity @ me.vertices[vi].co
                n = (nmat @ (corner[li].vector if corner is not None else me.loops[li].normal)).normalized()
                if uvl is not None:
                    uv = tuple(uvl.data[li].uv)
                else:
                    a, b = [k for k in range(3) if k != axis]
                    uv = (p[a] / 2.0, p[b] / 2.0)
                k = (vi, round(uv[0], 5), round(uv[1], 5), round(n.x, 3), round(n.y, 3), round(n.z, 3))
                if k not in index:
                    index[k] = len(verts)
                    verts.append(tuple(p))
                    normals.append(tuple(n))
                    uvs.append(uv)
                tri.append(index[k])
            tris.append(tuple(tri))
            sm = slot_mat[min(lt.material_index, len(slot_mat) - 1)]
            tri_mat.append(mats.index(sm))
        return V.MeshInput(verts, normals, uvs, tris, tri_mat, mats)

    @staticmethod
    def collision_geometry(ob):
        """A mesh object's triangles for a MeshShape: object-local with the object's scale baked in."""
        import bmesh
        bm = bmesh.new()
        bm.from_mesh(ob.data)
        bmesh.ops.triangulate(bm, faces=bm.faces[:])
        bm.verts.index_update()
        sc = ob.matrix_world.to_scale()
        verts = [tuple(v.co[i] * sc[i] for i in range(3)) for v in bm.verts]
        tris = [tuple(v.index for v in f.verts) for f in bm.faces]
        bm.free()
        return verts, tris

    def import_mesh(self, ob, collision=True, visible=True, max_size=SP.MAX_SIZE):
        """Turn a plain mesh object into new elements, split into game-sized pieces (acbmap.split), each centred on
        its own origin and placed in the always-loaded cell. collision: static collision (MeshShape), visible: a
        visual mesh. Returns the new element keys (climb edges are left to the caller, who can then build the
        world collision once for all of them)."""
        loc, rot, sc = ob.matrix_world.decompose()
        base = Matrix.Translation(loc) @ rot.to_matrix().to_4x4()
        mi = self.mesh_input(ob, Matrix.Diagonal((*sc, 1))) if visible else None
        cv, ct = self.collision_geometry(ob) if collision else (None, None)
        if mi is not None:
            tree, leaves = SP.partition(mi.verts, mi.tris, mi.uvs, max_size=max_size)
            col = SP.assign(tree, cv, ct) if collision else {}
        else:
            tree, leaves = SP.partition(cv, ct, max_size=max_size)
            col = dict(enumerate(leaves))
        keys = []
        for li, leaf in enumerate(leaves):
            vis = leaf if mi is not None else None
            cidx = col.get(li, [])
            c = SP.center(mi.verts, mi.tris, vis) if vis else SP.center(cv, ct, cidx)
            m = from_blender(base @ Matrix.Translation(c))
            if cidx:
                v, t, mm = SP.sub_collision(cv, ct, [0] * len(ct), cidx, c)
                if len(v) > 0xFFFF:
                    raise ValueError(f"{ob.name}: a piece has {len(v)} collision vertices (at most 65535)")
                nk = ops.new_collision(self.doc, m, v, t, mm)
                if vis:
                    ops.set_visual(self.doc, nk, SP.sub_mesh_input(mi, vis, c))
            elif vis:
                nk = ops.new_scenery(self.doc, m, SP.sub_mesh_input(mi, vis, c))
            else:
                continue
            keys.append(nk)
        return keys

    def show_new_element(self, key, name: str):
        """The Blender object of an element created in the document."""
        o = ops.element_obj(self.doc, key)
        names = [n for n, _ in components(o)]
        kind = "collision" if "InertComponent" in names else "visual"
        e = Element(kind, key[0], o, key[1], names, ops.owning_block(self.doc, key[0]))
        return self._element_object(e, name)   # named at creation: the session maps keys to object names

    def clear_scenery(self, kinds=ops.SCENERY_KINDS, **kw) -> dict:
        """ops.clear_scenery, then drop the Blender objects of what it removed."""
        self.sync()
        res = ops.clear_scenery(self.doc, kinds, **kw)
        gone = [k for k in self.objects if parse_key(k.split("|")[0])[0] not in self.doc.info]
        for k in gone:
            ob = bpy.data.objects.get(self.objects.pop(k))
            self.baseline.pop(k, None)
            if ob is not None:
                for ch in list(ob.children_recursive):
                    bpy.data.objects.remove(ch)
                bpy.data.objects.remove(ob)
        for me in [m for m in bpy.data.meshes if m.users == 0 and ("acb_shape" in m or m.name.startswith("ACB"))]:
            bpy.data.meshes.remove(me)
        for u in res["hollowed"]:   # still in the map (the navmesh names them) but shown/solid no more
            ob = bpy.data.objects.get(self.objects.get(keystr((u, -1)), ""))
            for x in [ob] + list(ob.children_recursive) if ob is not None else []:
                x["acb_hollow"] = True
                x.hide_set(True, view_layer=self.scene.view_layers[0])
                x.hide_render = True
        for t in res["templates"]:   # kept for cloning only, never added to the world: out of the way
            ob = bpy.data.objects.get(self.objects.get(keystr((t, -1)), ""))
            if ob is not None:
                ob["acb_template"] = True
                ob.hide_set(True, view_layer=self.scene.view_layers[0])   # this scene may not be the active one
                ob.hide_render = True
        self.snapshot()
        res["blender_objects"] = len(gone)
        return res

    def replace_element_visual(self, key):
        """Show an element's (new) visual mesh on its Blender object."""
        ob = bpy.data.objects.get(self.objects.get(keystr(key), ""))
        o = ops.element_obj(self.doc, key)
        meshes = [m for m in (self._visual_mesh(u) for u in V.entity_meshes(self.doc, o)) if m is not None]
        if ob is not None and ob.type == "MESH" and meshes and "acb_shape" not in ob.data:
            ob.data = meshes[0]
            return ob
        return None

    def world_collision(self):
        """The map's collision in world space as the document has it now (built per call: edits move things)."""
        try:
            from acbmap.world import WorldCollision
        except ImportError:   # no numpy: the generator falls back to the entity's own geometry
            return None
        return WorldCollision(self.doc)

    def climb_object(self, entity, ob, k):
        """(Re)build the line object showing an element's climb edges (entity-local, so parented with no offset)."""
        old = bpy.data.objects.get(f"{ob.name}:climb")
        if old is not None and old.get("acb_climb_of") == k:
            me = old.data
            bpy.data.objects.remove(old)
            if me.users == 0:
                bpy.data.meshes.remove(me)
        edges = [e for g in G.systems(entity) for e in G.edges(g)]
        if not edges:
            return None
        me = bpy.data.meshes.new(f"ACBClimb_{k}")
        me.from_pydata([p for e in edges for p in (e.p0, e.p1)], [(2 * i, 2 * i + 1) for i in range(len(edges))], [])
        ch = bpy.data.objects.new(f"{ob.name}:climb", me)
        self.coll("Climb Edges").objects.link(ch)
        ch.parent = ob
        ch.hide_select = True
        ch.show_in_front = True
        ch.color = (1.0, 0.75, 0.1, 1.0)
        ch["acb_climb_of"] = k
        return ch

    def _visual_mesh(self, uid: int):
        """The Blender mesh of a visual Mesh (LOD0), built once and shared by every entity showing it."""
        mname = f"ACBVis_{uid:08x}"
        me = bpy.data.meshes.get(mname)
        if me is not None:
            return me
        if uid in getattr(self, "_no_visual", ()):
            return None
        g = V.mesh_geometry(self.doc, uid)
        if g is None or not g.tris:
            self.__dict__.setdefault("_no_visual", set()).add(uid)
            return None
        me = bpy.data.meshes.new(mname)
        me.from_pydata(g.verts, [], g.tris)
        uv = me.uv_layers.new(name="UVMap")
        lv = [0] * len(me.loops)
        me.loops.foreach_get("vertex_index", lv)
        uv.data.foreach_set("uv", [c for i in lv for c in g.uvs[i]])
        for mid in g.materials:
            me.materials.append(self._vis_material(mid) if mid else None)
        if len(g.materials) > 1:
            me.polygons.foreach_set("material_index", g.tri_material)
        me.shade_smooth()
        me.normals_split_custom_set_from_vertices(g.normals)
        me.update()
        me["acb_visual"] = f"{uid:08x}"
        return me

    def _vis_material(self, mid: int):
        nm = self.doc.name_of(mid) if mid in self.doc.info else f"{mid:08x}"
        mat = bpy.data.materials.get(f"ACBMat_{nm}")
        if mat is not None:
            return mat
        mat = bpy.data.materials.new(f"ACBMat_{nm}")
        mat["acb_vis_material"] = f"{mid:08x}"
        mat.use_nodes = True
        nt = mat.node_tree
        bsdf = next((n for n in nt.nodes if n.type == "BSDF_PRINCIPLED"), None)
        img = self._texture_image(V.material_texture(self.doc, mid, 0))
        flags = V.material_flags(self.doc, mid)
        if bsdf is not None:
            bsdf.inputs["Roughness"].default_value = 0.8
        if img is not None and bsdf is not None:
            tex = nt.nodes.new("ShaderNodeTexImage")
            tex.image = img
            tex.location = (-320, 260)
            nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
            if flags.get("alpha_test") or flags.get("blend_mode"):
                nt.links.new(tex.outputs["Alpha"], bsdf.inputs["Alpha"])
                if hasattr(mat, "surface_render_method"):
                    mat.surface_render_method = "DITHERED"
                else:
                    mat.blend_method = "CLIP"
        nid = V.material_texture(self.doc, mid, 1)
        nimg = self._texture_image(nid, non_color=True)
        if nimg is not None and bsdf is not None:
            tex = nt.nodes.new("ShaderNodeTexImage")
            tex.image = nimg
            tex.location = (-620, -200)
            dec = nt.nodes.new("ShaderNodeGroup")
            dec.node_tree = normal_decode_group()
            dec.location = (-320, -200)
            dec.inputs["Alpha in X"].default_value = 1.0 if V.normal_map_layout(self.doc, nid) == "ag" else 0.0
            nm = nt.nodes.new("ShaderNodeNormalMap")
            nm.location = (-140, -200)
            nt.links.new(tex.outputs["Color"], dec.inputs["Color"])
            nt.links.new(tex.outputs["Alpha"], dec.inputs["Alpha"])
            nt.links.new(dec.outputs["Color"], nm.inputs["Color"])
            nt.links.new(nm.outputs["Normal"], bsdf.inputs["Normal"])
        mat.use_backface_culling = not flags.get("two_sided", False)
        return mat

    def _texture_image(self, tid: int | None, non_color: bool = False):
        if tid is None:
            return None
        nm = f"ACBTex_{self.doc.name_of(tid)}"
        img = bpy.data.images.get(nm)
        if img is not None:
            return img
        d = os.path.join(self.doc.cache, "tex")
        path = os.path.join(d, f"{tid:08x}.dds")
        if not os.path.exists(path):
            data = V.texture_dds(self.doc, tid)
            if data is None:
                return None
            os.makedirs(d, exist_ok=True)
            with open(path + ".tmp", "wb") as f:
                f.write(data)
            os.replace(path + ".tmp", path)
        try:
            img = bpy.data.images.load(path)
        except RuntimeError:
            return None
        img.name = nm
        if non_color:
            img.colorspace_settings.name = "Non-Color"
            img.alpha_mode = "CHANNEL_PACKED"   # alpha carries data (x) in two-channel normal maps
        return img

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

    def _oob_wall(self, e, ob, k):
        """The element's boundary as one editable polyline (see the module docstring)."""
        try:
            ws = OOB.walls(self.doc, e.key)
        except ValueError:
            return None
        verts, edges, heights = [], [], []
        for w in ws:
            base, n = len(verts), len(w.corners)
            verts += w.corners
            heights += w.heights
            edges += [(base + i, base + (i + 1) % n) for i in range(n if w.closed else n - 1)]
        me = bpy.data.meshes.new(f"ACB_OOBWall_{k}")
        me.from_pydata(verts, edges, [])
        attr = me.attributes.new("acb_height", "FLOAT", "POINT")
        attr.data.foreach_set("value", heights)
        wob = bpy.data.objects.new(f"{ob.name}:wall", me)
        self.coll("Out of Bounds").objects.link(wob)
        wob.parent = ob
        wob.matrix_parent_inverse = ob.matrix_world.inverted()   # vertices stay in world space
        mod = wob.modifiers.new("Wall height (display)", "SCREW")
        mod.angle = 0.0
        mod.screw_offset = sorted(heights)[len(heights) // 2] if heights else 10.0
        mod.steps = mod.render_steps = 1
        mod.axis = "Z"
        mod.use_normal_calculate = True
        wob.show_wire = True
        wob.color = (1.0, 0.2, 0.2, 0.6)
        wob["acb_key"] = k
        wob["acb_part"] = "oobwall"
        self.objects[f"{k}|oobwall"] = wob.name
        return wob

    @staticmethod
    def wall_geometry(wob):
        """(world-space corner points, per-corner heights or None, edges) of a wall object. In Edit Mode the mesh's
        generic attributes have no data from Python (len 0, even after update_from_editmode), so everything is
        read from the edit BMesh there."""
        import bmesh
        me = wob.data
        M = wob.matrix_world
        if wob.mode == "EDIT":
            bm = bmesh.from_edit_mesh(me)
            bm.verts.index_update()
            layer = bm.verts.layers.float.get("acb_height")
            pts = [tuple(M @ v.co) for v in bm.verts]
            hs = [v[layer] for v in bm.verts] if layer is not None else None
            edges = [tuple(v.index for v in e.verts) for e in bm.edges]
            return pts, hs, edges
        attr = me.attributes.get("acb_height")
        pts = [tuple(M @ v.co) for v in me.vertices]
        hs = [d.value for d in attr.data] if attr is not None and len(attr.data) == len(pts) else None
        return pts, hs, [tuple(e.vertices) for e in me.edges]

    @classmethod
    def wall_state(cls, wob) -> bytes:
        """What a wall object's edit changes: world-space corners, edges and heights."""
        pts, hs, edges = cls.wall_geometry(wob)
        flat = [x for p in pts for x in p]
        hs = hs or []
        return (struct.pack(f"<{len(flat)}f", *flat) + struct.pack(f"<{len(hs)}f", *hs)
                + struct.pack(f"<{2 * len(edges)}I", *sorted(i for e in edges for i in e)))

    @classmethod
    def walls_from_object(cls, wob, current: "list[OOB.Wall]" = ()) -> "list[OOB.Wall]":
        """A wall object's polyline as walls (corners in world space). A corner without a usable height (attribute
        missing, or 0) takes the height of the nearest corner of `current` (the walls as the document has them);
        nothing is ever made up."""
        pts, hs, edges = cls.wall_geometry(wob)
        ref = [(p, h) for w in current for p, h in zip(w.corners, w.heights)]
        if hs is None:
            hs = [0.0] * len(pts)
        known = [(p, h) for p, h in zip(pts, hs) if h > 0.01] or ref
        if not known:
            raise ValueError("the wall has no heights (acb_height attribute) and nothing to take them from")

        def nearest(p):
            return min(known, key=lambda q: (q[0][0] - p[0]) ** 2 + (q[0][1] - p[1]) ** 2)[1]
        hs = [h if h > 0.01 else nearest(p) for p, h in zip(pts, hs)]
        adj = {i: set() for i in range(len(pts))}
        for a, b in edges:
            if a != b:
                adj[a].add(b)
                adj[b].add(a)
        return OOB.chains(pts, adj, hs)

    def _refresh_visual(self, uid: int):
        """Rebuild the Blender mesh of a visual Mesh the document changed (every object showing it follows)."""
        old = bpy.data.meshes.get(f"ACBVis_{uid:08x}")
        if old is not None:
            old.name = f"{old.name}.old"
        new = self._visual_mesh(uid)
        if old is not None:
            if new is not None:
                old.user_remap(new)
            bpy.data.meshes.remove(old)

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
        self.tag = self.scene.get("acb_tag") or os.path.basename(self.source).replace("DataPC_", "").replace(".forge", "")
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
            elif part.startswith("zone:") or part == "oobwall":
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
        if part == "oobwall":
            return self.wall_state(ob)
        if part.startswith("zone:"):
            return struct.pack("<16f", *[x for row in ob.matrix_basis for x in row])
        return from_blender(ob.matrix_world)

    def sync(self) -> list[str]:
        """Push Blender-side changes into the document. Returns a log of what changed."""
        doc, log = self.doc, []
        for ob in self.scene.objects:      # edit-mode changes reach ob.data only when flushed
            if ob.mode == "EDIT" and ob.type == "MESH":
                ob.update_from_editmode()
        bpy.context.view_layer.update()   # matrix_world of just-moved objects is stale until the depsgraph runs
        by_key: dict[str, list] = {}
        for ob in self.scene.objects:
            if "acb_key" in ob:
                by_key.setdefault(ob["acb_key"] + ("|" + ob["acb_part"] if ob.get("acb_part") else ""), []).append(ob)
        chests_changed = False
        # deletions (elements only; parts follow their element)
        removed = [k for k in self.objects if "|" not in k and k not in by_key]
        if removed:
            kinds = {k: self._kind_of_key(k) for k in removed}
            res = ops.delete_many(doc, [parse_key(k) for k in removed])
            for k in removed:
                why = res[parse_key(k)]
                name = self.objects.pop(k)
                self.baseline.pop(k, None)
                if why is None:
                    log.append(f"deleted {name}")
                    chests_changed |= kinds[k] == "chest_spawn"
                else:
                    log.append(f"NOT deleted {name}: {why}")
            self._renumber_children([parse_key(k) for k in removed
                                     if res[parse_key(k)] is None and parse_key(k)[1] >= 0])
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
                if ob.type == "MESH" and orig.type == "MESH" and ob.data != orig.data and "acb_shape" in orig.data:
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
            ek, part = k.split("|", 1)
            if part == "oobwall":
                if self.baseline.get(k) == st:
                    continue
                try:
                    r = OOB.write_walls(doc, parse_key(ek), self.walls_from_object(ob, OOB.walls(doc, parse_key(ek))))
                except ValueError as ex:
                    log.append(f"NOT changed {ob.name}: {ex}")
                    continue
                mesh_uid = OOB.oob_parts(doc, parse_key(ek))[4]
                if mesh_uid is not None:
                    self._refresh_visual(mesh_uid)
                log.append(f"out-of-bounds wall {ob.name}: {r['corners']} corners, {r['sections']} sections")
                continue
            if self.baseline.get(k) is not None and mat_close(st, self.baseline[k]):
                continue
            if part.startswith("zone:"):
                self._write_zone(parse_key(ek), part[5:], ob)
                log.append(f"zone {ob.name}")
        # collision geometry
        for me in bpy.data.meshes:
            if "acb_shape" not in me or me.users == 0:
                continue
            h = mesh_hash(me)
            if self.mesh_base.get(me.name) == h:
                continue
            self._write_shape(me)
            log.append(f"collision shape {me.name} ({len(me.vertices)} verts)")
            stale = self._climb_users(int(me["acb_shape"], 16))
            if stale:
                log.append(f"climb edges now stale on {', '.join(stale[:3])}{' ...' if len(stale) > 3 else ''}: "
                           "Generate or Remove Climb Edges")
        if chests_changed:
            n = self.wd.sync_chests()
            log.append(f"chest capture world data regenerated ({n} chests)")
        self.snapshot()
        self.log += log
        return log

    def _climb_users(self, sid: int) -> list[str]:
        """Blender names of elements that have climb edges and use MeshShape sid."""
        out = []
        for k, name in self.objects.items():
            if "|" in k:
                continue
            try:
                o = ops.element_obj(self.doc, parse_key(k))
            except Exception:
                continue
            if G.systems(o) and any(u32(ic.fields["RigidBody"].fields["Shape"].id) == sid
                                    for _i, ic in ops.inert_components(o)):
                out.append(name)
        return out

    def _renumber_children(self, deleted):
        """After group children were deleted, the later children of those groups moved down: rekey their objects
        (and parts) to the new indices."""
        by_group: dict[int, list[int]] = {}
        for g, i in deleted:
            by_group.setdefault(g, []).append(i)
        if not by_group:
            return

        def new_key(k: str) -> str:
            ek, sep, part = k.partition("|")
            g, i = parse_key(ek)
            if g not in by_group or i < 0:
                return k
            return keystr((g, i - sum(1 for d in by_group[g] if d < i))) + sep + part

        self.objects = {new_key(k): v for k, v in self.objects.items()}
        self.baseline = {new_key(k): v for k, v in self.baseline.items()}
        for ob in self.scene.objects:
            if "acb_key" in ob and parse_key(ob["acb_key"])[0] in by_group:
                ob["acb_key"] = new_key(ob["acb_key"])

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
        for k in ops.shape_users(self.doc, sid):   # compound members collide through the compound's stale MOPP
            ops.ensure_standalone(self.doc, k)
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
