"""Panels and operators (View3D > Sidebar > ACB)."""
from __future__ import annotations

import os
import traceback

import bpy
from mathutils import Matrix

from . import ensure_paths, prefs

ensure_paths(prefs())

from acbmap import fields, ops  # noqa: E402
from acbmap import guidance as G  # noqa: E402
from acbmap.doc import MapDocument, u32  # noqa: E402
from acbmap.geom import from_blender  # noqa: E402
from acbmap.kinds import Element, classify, components, kind_of  # noqa: E402
from acbmap.schema import type_name  # noqa: E402

from . import session as S  # noqa: E402

EXPANDED: dict[str, set] = {}   # acb_key -> expanded inspector paths


def sess(context):
    return S.get(context.scene)


def active_element(context):
    ob = context.active_object
    if ob is None or "acb_key" not in ob:
        return None, None
    return ob, S.parse_key(ob["acb_key"])


def edited_path(s) -> str:
    return os.path.join(os.path.dirname(s.doc.cache), "edited", os.path.basename(s.source))


def _report_log(op, lines, limit=8):
    for ln in lines[:limit]:
        op.report({"INFO"}, ln)
    if len(lines) > limit:
        op.report({"INFO"}, f"... and {len(lines) - limit} more")


# ---------------------------------------------------------------- map --

class ACB_OT_open_map(bpy.types.Operator):
    """Open an ACB map forge for editing"""
    bl_idname = "acb.open_map"
    bl_label = "Open ACB Map"
    filepath: bpy.props.StringProperty(subtype="FILE_PATH")
    filter_glob: bpy.props.StringProperty(default="*.forge", options={"HIDDEN"})
    with_collision: bpy.props.BoolProperty(name="Collision geometry", default=True)
    with_visuals: bpy.props.BoolProperty(name="Visual meshes", default=True,
                                         description="Show the map's textured meshes (most detailed LOD)")

    def invoke(self, context, event):
        if not self.filepath:
            p = prefs(context)
            self.filepath = (getattr(p, "game_multi", "") or "") + os.sep
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        wm = context.window_manager
        wm.progress_begin(0, 1)
        try:
            s = S.Session(context.scene, self.filepath)
            s.build(self.with_collision, self.with_visuals, progress=wm.progress_update)
        except Exception as ex:
            traceback.print_exc()
            self.report({"ERROR"}, f"open failed: {ex}")
            return {"CANCELLED"}
        finally:
            wm.progress_end()
        n = len(s.objects)
        if s.migrated:
            self.report({"WARNING"}, f"renumbered {s.migrated} object ids an earlier editor version put in the "
                                     "engine's runtime range (those objects never showed in game); save to keep it")
        self.report({"INFO"}, f"{os.path.basename(self.filepath)}: {n} editable objects")
        return {"FINISHED"}


class ACB_OT_toggle_collision(bpy.types.Operator):
    """Show or hide the collision meshes (hidden while visual meshes are shown; unhide them to edit collision)"""
    bl_idname = "acb.toggle_collision"
    bl_label = "Show Collision"

    def execute(self, context):
        s = sess(context)
        s.set_collision_visible(not s.collision_visible())
        return {"FINISHED"}


class ACB_OT_reconnect(bpy.types.Operator):
    """Reopen the forge behind this scene's ACB objects (after reopening the .blend)"""
    bl_idname = "acb.reconnect"
    bl_label = "Reconnect"

    def execute(self, context):
        sc = context.scene
        src = sc.get("acb_forge")
        if not src or not os.path.exists(src):
            self.report({"ERROR"}, "scene has no ACB map")
            return {"CANCELLED"}
        s = S.Session(sc, sc.get("acb_saved") or src, sc.get("acb_multi"))
        s.source = src
        s.attach()
        self.report({"INFO"}, f"reconnected: {len(s.objects)} objects")
        return {"FINISHED"}


class ACB_OT_apply(bpy.types.Operator):
    """Push Blender edits (moves, Shift+D copies, deletions, zones, collision) into the open map"""
    bl_idname = "acb.apply"
    bl_label = "Apply Edits"

    def execute(self, context):
        s = sess(context)
        log = s.sync()
        _report_log(self, log or ["no changes"])
        return {"FINISHED"}


class ACB_OT_save(bpy.types.Operator):
    """Write the edited forge and run the structural checks against the original"""
    bl_idname = "acb.save"
    bl_label = "Save Forge"

    def execute(self, context):
        s = sess(context)
        out = edited_path(s)
        try:
            problems = s.save(out)
        except Exception as ex:
            traceback.print_exc()
            self.report({"ERROR"}, f"save failed: {ex}")
            return {"CANCELLED"}
        context.scene["acb_saved"] = out
        if problems:
            _report_log(self, problems)
            self.report({"WARNING"}, f"saved with {len(problems)} new problems -- not installable")
        else:
            self.report({"INFO"}, f"saved {out}, checks passed")
        return {"FINISHED"}


class ACB_OT_install(bpy.types.Operator):
    """Replace the game's copy of this map with the saved forge (the original is kept as .orig)"""
    bl_idname = "acb.install"
    bl_label = "Install into Game"

    @classmethod
    def poll(cls, context):
        s = sess(context)
        return s is not None and getattr(s, "last_save", None) and not s.last_problems

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        from acbmap import install
        s = sess(context)
        try:
            dst = install.install(s.last_save, prefs(context).game_multi, os.path.basename(s.source))
        except Exception as ex:
            self.report({"ERROR"}, str(ex))
            return {"CANCELLED"}
        self.report({"INFO"}, f"installed {dst} (every player needs this file)")
        return {"FINISHED"}


class ACB_OT_uninstall(bpy.types.Operator):
    """Restore the game's original copy of this map"""
    bl_idname = "acb.uninstall"
    bl_label = "Restore Original"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        from acbmap import install
        s = sess(context)
        try:
            dst = install.uninstall(os.path.basename(s.source), prefs(context).game_multi)
        except Exception as ex:
            self.report({"ERROR"}, str(ex))
            return {"CANCELLED"}
        self.report({"INFO"}, f"restored {dst}")
        return {"FINISHED"}


# ---------------------------------------------------------------- add --

def _template_items(self, context):
    s = sess(context)
    if s is None:
        return [("", "(no map open)", "")]
    if not hasattr(s, "_templates"):
        seen = {}
        for e in classify(s.doc, with_children=False):
            if e.kind in ("visual", "other", "collision", "crowd_flow", "nav_flow") or e.kind in seen:
                continue
            if e.block is None:
                continue
            seen[e.kind] = e.key
        s._templates = seen
    return [(S.keystr(k), kind.replace("_", " ").title(), f"copy of {s.doc.name_of(k[0])}")
            for kind, k in sorted(s._templates.items())]


class ACB_OT_add_element(bpy.types.Operator):
    """Add a gameplay element at the 3D cursor, copied from one of this map's own"""
    bl_idname = "acb.add_element"
    bl_label = "Add Element"
    bl_property = "template"
    template: bpy.props.EnumProperty(name="Element", items=_template_items)

    def invoke(self, context, event):
        context.window_manager.invoke_search_popup(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        s = sess(context)
        s.sync()
        key = S.parse_key(self.template)
        src = ops.element_obj(s.doc, key)
        m = Matrix(S.to_blender(src.fields["GlobalMatrix"]))
        m.translation = context.scene.cursor.location
        try:
            nk = ops.duplicate(s.doc, key, from_blender(m))
        except ops.EditError as ex:
            self.report({"ERROR"}, str(ex))
            return {"CANCELLED"}
        o = ops.element_obj(s.doc, nk)
        e = Element(kind_of(o, type_name(o.type_hash) == "EntityGroup"), nk[0], o, -1,
                    [n for n, _ in components(o)], ops.owning_block(s.doc, nk[0]))
        ob = s._element_object(e)
        if type_name(o.type_hash) == "EntityGroup":
            from acbmap.kinds import group_children
            for i, c in enumerate(group_children(o)):
                ce = Element(kind_of(c), nk[0], c, i, [n for n, _ in components(c)], e.block)
                cob = s._element_object(ce)
                if cob is not None:
                    cob.parent = ob
                    cob.matrix_parent_inverse = ob.matrix_world.inverted()
        if e.kind == "chest_spawn":
            s.wd.sync_chests()
        s.snapshot()
        for x in context.selected_objects:
            x.select_set(False)
        if ob is not None:
            ob.select_set(True)
            context.view_layer.objects.active = ob
        self.report({"INFO"}, f"added {ob.name if ob else nk}")
        return {"FINISHED"}


def _import_meshes(op, context, collision: bool, visible: bool):
    """Shared body of Mesh to Collision / Mesh to Scenery: split pieces, then one climb-edge pass."""
    s = sess(context)
    s.sync()
    made = []   # (key, blender name)
    for ob in [o for o in context.selected_objects if o.type == "MESH" and "acb_key" not in o]:
        try:
            keys = s.import_mesh(ob, collision=collision, visible=visible, max_size=op.max_size,
                                 surface=getattr(op, "surface", "auto"))
        except (ops.EditError, ValueError) as ex:
            op.report({"ERROR"}, f"{ob.name}: {ex}")
            continue
        base = ob.name
        bpy.data.objects.remove(ob)
        suffix = "_col" if collision else "_vis"
        made += [(k, base + suffix if len(keys) == 1 else f"{base}{suffix}{i:03d}") for i, k in enumerate(keys)]
    edges = 0
    if collision and getattr(op, "climb", False) and made:
        world = s.world_collision()   # once, with every new piece in it
        for k, _n in made:
            try:
                edges += G.regenerate(s.doc, k, ops.fresh_ids, world=world)
            except ValueError as ex:
                op.report({"WARNING"}, f"no climb edges: {ex}")
    for k, name in made:
        s.show_new_element(k, name)
    s.snapshot()
    return made, edges


class ACB_OT_new_collision(bpy.types.Operator):
    """Turn the selected plain mesh objects into ACB static collision, visible in game (map materials on slots that
    use them, ACBMat_*), with generated climb edges. Big meshes are split into pieces"""
    bl_idname = "acb.new_collision"
    bl_label = "Mesh to Collision"
    bl_options = {"REGISTER", "UNDO"}
    climb: bpy.props.BoolProperty(name="Climb edges", default=True, description="Generate ledges from the mesh")
    visible: bpy.props.BoolProperty(name="Visible mesh", default=True,
                                    description="Also write the mesh as the object's visible geometry")
    surface: bpy.props.EnumProperty(
        name="Surface", default="auto",
        items=[("auto", "Auto", "The object's acb_surface property, else from the mesh: mostly up-facing = ground, "
                                "some up-facing area = roof, else wall"),
               ("ground", "Ground", "A floor: walked on normally, crowd may spawn (IsGround)"),
               ("roof", "Roof", "A building to climb and run across (IsRoof)"),
               ("wall", "Wall", "Neither")])
    max_size: bpy.props.FloatProperty(name="Max piece size", default=64.0, min=4.0, max=1000.0, unit="LENGTH",
                                      description="Meshes larger than this (or over 20000 triangles, or a 15-tile "
                                                  "uv span) are split into pieces of at most this size")

    def execute(self, context):
        made, edges = _import_meshes(self, context, True, self.visible)
        self.report({"INFO"}, f"{len(made)} collision object(s) created, {edges} climb edges")
        return {"FINISHED"}


class ACB_OT_new_scenery(bpy.types.Operator):
    """Turn the selected plain mesh objects into visible scenery without collision (map materials on slots that use
    them, ACBMat_*). Big meshes are split into pieces"""
    bl_idname = "acb.new_scenery"
    bl_label = "Mesh to Scenery"
    bl_options = {"REGISTER", "UNDO"}
    max_size: bpy.props.FloatProperty(name="Max piece size", default=64.0, min=4.0, max=1000.0, unit="LENGTH")

    def execute(self, context):
        made, _ = _import_meshes(self, context, False, True)
        self.report({"INFO"}, f"{len(made)} scenery object(s) created")
        return {"FINISHED"}


class ACB_OT_clear_scenery(bpy.types.Operator):
    """Start a new map from this one: remove every visible mesh and static collision element, keeping spawns,
    chests, interactive objects, zones and out-of-bounds. Then import your geometry (File > Import) and use Mesh to
    Collision. NPCs keep walking the old navmesh"""
    bl_idname = "acb.clear_scenery"
    bl_label = "Clear Scenery"
    bl_options = {"REGISTER"}

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        s = sess(context)
        context.window.cursor_set("WAIT")
        try:
            r = s.clear_scenery()
        finally:
            context.window.cursor_set("DEFAULT")
        self.report({"INFO"}, f"removed {r['removed']} objects ({r['compounds_dissolved']} merged-collision groups "
                              f"split up); {r['kept_referenced']} kept because something links to them")
        return {"FINISHED"}


class ACB_OT_replace_visual(bpy.types.Operator):
    """Make the selected plain mesh the visible geometry of the active ACB element (replacing its own; collision and
    climb edges stay as they are). The plain mesh is consumed"""
    bl_idname = "acb.replace_visual"
    bl_label = "Replace Visual"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = sess(context)
        target, key = active_element(context)
        src = [o for o in context.selected_objects if o.type == "MESH" and "acb_key" not in o]
        if target is None or len(src) != 1:
            self.report({"ERROR"}, "select one plain mesh, then the ACB element (active)")
            return {"CANCELLED"}
        s.sync()
        el = bpy.data.objects.get(s.objects.get(target["acb_key"], "")) or target
        to_entity = el.matrix_world.inverted() @ src[0].matrix_world
        try:
            ops.set_visual(s.doc, key, s.mesh_input(src[0], to_entity))
        except (ops.EditError, ValueError) as ex:
            self.report({"ERROR"}, str(ex))
            return {"CANCELLED"}
        if s.replace_element_visual(key) is None:
            self.report({"WARNING"}, "written; reopen the map to see it on this element")
        bpy.data.objects.remove(src[0])
        s.snapshot()
        self.report({"INFO"}, f"{el.name}: new visual mesh")
        return {"FINISHED"}


class ACB_OT_edit_boundary(bpy.types.Operator):
    """Edit the out-of-bounds boundary: selects its wall object and enters Edit Mode (G moves corners, E extends
    from a selected corner, subdivide adds one, X > Dissolve Vertices removes one); Apply regenerates the wall"""
    bl_idname = "acb.edit_boundary"
    bl_label = "Edit Boundary"
    bl_options = {"REGISTER"}

    def execute(self, context):
        s = sess(context)
        walls = [o for o in context.scene.objects if o.get("acb_part") == "oobwall"]
        if not walls:
            self.report({"ERROR"}, "this map has no out-of-bounds wall")
            return {"CANCELLED"}
        if context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        for o in context.selected_objects:
            o.select_set(False)
        wob = walls[0]
        if s is not None:
            s.set_collection_visible("Out of Bounds", True)
        wob.hide_set(False)
        wob.hide_select = False
        wob.select_set(True)
        context.view_layer.objects.active = wob
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_mode(type="VERT")
        self._top_view(context)
        self.report({"INFO"}, "boundary in Edit Mode (top view, X-ray): click corners, G move, E extend, "
                              "X > Dissolve Vertices remove; then Apply")
        return {"FINISHED"}

    @staticmethod
    def _top_view(context):
        """X-ray on (corners behind the wall surface or inside the ground stay clickable), top view, framed on the
        whole boundary, nothing selected."""
        area = context.area if context.area and context.area.type == "VIEW_3D" else next(
            (a for a in context.screen.areas if a.type == "VIEW_3D"), None) if context.screen else None
        if area is None:
            return
        region = next((r for r in area.regions if r.type == "WINDOW"), None)
        space = area.spaces.active
        space.shading.show_xray = True
        space.shading.show_xray_wireframe = True
        with context.temp_override(area=area, region=region, space_data=space):
            bpy.ops.view3d.view_axis(type="TOP")
            bpy.ops.mesh.select_all(action="SELECT")
            bpy.ops.view3d.view_selected()
            bpy.ops.mesh.select_all(action="DESELECT")


class ACB_OT_make_unique(bpy.types.Operator):
    """Give the active collision object its own copy of a shape shared with other objects"""
    bl_idname = "acb.make_unique"
    bl_label = "Make Shape Unique"

    def execute(self, context):
        s = sess(context)
        ob, key = active_element(context)
        if ob is None or ob.type != "MESH" or "acb_shape" not in ob.data:
            self.report({"ERROR"}, "select a collision mesh (unhide the Collision collection to reach it)")
            return {"CANCELLED"}
        s.sync()
        part = ob.get("acb_part", "")
        idx = int(part.split(":")[1]) if part.startswith("shape:") else 0
        sid = ops.make_shape_unique(s.doc, key, idx)
        me = ob.data.copy()
        me.name = f"ACBShape_{sid:08x}"
        me["acb_shape"] = f"{sid:08x}"
        ob.data = me
        s.mesh_base[me.name] = S.mesh_hash(me)
        self.report({"INFO"}, f"{ob.name} now has its own shape {sid:#x}")
        return {"FINISHED"}


class ACB_OT_generate_climb(bpy.types.Operator):
    """Generate climb edges (ledges) for the selected elements from their own collision, replacing any they had.
    Retail edges also come from neighbouring geometry, so regenerating a retail object can lose some"""
    bl_idname = "acb.generate_climb"
    bl_label = "Generate Climb Edges"
    bl_options = {"REGISTER", "UNDO"}
    min_depth: bpy.props.FloatProperty(name="Min ledge depth", default=G.MIN_DEPTH, min=0.0, unit="LENGTH",
                                       description="Walkable surface needed behind a ledge")
    min_drop: bpy.props.FloatProperty(name="Min wall drop", default=G.MIN_DROP, min=0.0, unit="LENGTH",
                                      description="Wall height needed below a ledge (lower edges are steps)")
    use_world: bpy.props.BoolProperty(name="Use surrounding geometry", default=True,
                                      description="Drop ledges whose hanging space is taken by another object or "
                                                  "that sit less than the min drop above any floor")

    def execute(self, context):
        s = sess(context)
        s.sync()
        world = s.world_collision() if self.use_world else None
        done, total = 0, 0
        seen = set()
        for ob in context.selected_objects:
            if "acb_key" not in ob or ob["acb_key"] in seen:
                continue
            seen.add(ob["acb_key"])
            key = S.parse_key(ob["acb_key"])
            el = s.objects.get(ob["acb_key"])
            eob = bpy.data.objects.get(el) if el else ob
            try:
                n = G.regenerate(s.doc, key, ops.fresh_ids, self.min_depth, self.min_drop, world)
            except (ops.EditError, ValueError) as ex:
                self.report({"ERROR"}, f"{ob.name}: {ex}")
                continue
            s.climb_object(ops.element_obj(s.doc, key), eob, ob["acb_key"])
            done += 1
            total += n
        s.set_collection_visible("Climb Edges", True)
        self.report({"INFO"}, f"{total} climb edges on {done} element(s)")
        return {"FINISHED"}


class ACB_OT_toggle_climb(bpy.types.Operator):
    """Show or hide the climb edges (ledges the player can grab)"""
    bl_idname = "acb.toggle_climb"
    bl_label = "Show Climb Edges"

    def execute(self, context):
        s = sess(context)
        s.set_collection_visible("Climb Edges", not s.collection_visible("Climb Edges"))
        return {"FINISHED"}


class ACB_OT_strip_guidance(bpy.types.Operator):
    """Remove the active element's climb edges (GuidanceSystem); do this after reshaping collision that had them"""
    bl_idname = "acb.strip_guidance"
    bl_label = "Remove Climb Edges"

    def execute(self, context):
        s = sess(context)
        ob, key = active_element(context)
        if ob is None:
            return {"CANCELLED"}
        o = ops.element_obj(s.doc, key)
        if ops.strip_guidance(o):
            s.doc.touch(key[0])
            eob = bpy.data.objects.get(s.objects.get(ob["acb_key"], ""))
            if eob is not None:
                s.climb_object(o, eob, ob["acb_key"])
            self.report({"INFO"}, "climb edges removed")
        else:
            self.report({"INFO"}, "no climb edges on this element")
        return {"FINISHED"}


# ---------------------------------------------------------------- escort --

def _apply_paths(s):
    s.wd.set_vip_paths(s.vip_paths)
    s._build_vip_curves()


class ACB_OT_path_new(bpy.types.Operator):
    """New Escort path from the selected crowd flows"""
    bl_idname = "acb.path_new"
    bl_label = "New Path"

    def execute(self, context):
        s = sess(context)
        s.vip_paths.append([])
        context.scene.acb_path_index = len(s.vip_paths) - 1
        return bpy.ops.acb.path_add_selected()


class ACB_OT_path_delete(bpy.types.Operator):
    bl_idname = "acb.path_delete"
    bl_label = "Delete Path"

    def execute(self, context):
        s = sess(context)
        i = context.scene.acb_path_index
        if 0 <= i < len(s.vip_paths):
            del s.vip_paths[i]
            _apply_paths(s)
        context.scene.acb_path_index = max(0, i - 1)
        return {"FINISHED"}


class ACB_OT_path_add_selected(bpy.types.Operator):
    """Append the selected crowd-flow points (active one last) to the current Escort path"""
    bl_idname = "acb.path_add_selected"
    bl_label = "Append Selected Flows"

    def execute(self, context):
        s = sess(context)
        i = context.scene.acb_path_index
        if not 0 <= i < len(s.vip_paths):
            return {"CANCELLED"}
        act = context.active_object
        sel = [o for o in context.selected_objects if o.get("acb_kind") in ("crowd_flow", "nav_flow") and o != act]
        sel.sort(key=lambda o: o.name)
        if act is not None and act.get("acb_kind") in ("crowd_flow", "nav_flow"):
            sel.append(act)
        if not sel:
            self.report({"ERROR"}, "select crowd-flow points (Crowd Flows collection)")
            return {"CANCELLED"}
        for o in sel:
            s.vip_paths[i].append({"flow": S.parse_key(o["acb_key"])[0], "spawn": False, "checkpoint": False})
        _apply_paths(s)
        return {"FINISHED"}


class ACB_OT_path_node(bpy.types.Operator):
    bl_idname = "acb.path_node"
    bl_label = "Path Node"
    index: bpy.props.IntProperty()
    action: bpy.props.StringProperty()   # remove / up / down / spawn / checkpoint / select

    def execute(self, context):
        s = sess(context)
        p = s.vip_paths[context.scene.acb_path_index]
        j = self.index
        if self.action == "remove":
            del p[j]
        elif self.action == "up" and j > 0:
            p[j - 1], p[j] = p[j], p[j - 1]
        elif self.action == "down" and j < len(p) - 1:
            p[j + 1], p[j] = p[j], p[j + 1]
        elif self.action in ("spawn", "checkpoint"):
            p[j][self.action] = not p[j][self.action]
        elif self.action == "select":
            ob = bpy.data.objects.get(s.flow_names.get(p[j]["flow"], ""))
            if ob:
                for x in context.selected_objects:
                    x.select_set(False)
                ob.select_set(True)
                context.view_layer.objects.active = ob
            return {"FINISHED"}
        _apply_paths(s)
        return {"FINISHED"}


# ---------------------------------------------------------------- inspector --

class ACB_OT_toggle(bpy.types.Operator):
    bl_idname = "acb.toggle"
    bl_label = "Expand"
    path: bpy.props.StringProperty()

    def execute(self, context):
        ob, key = active_element(context)
        ex = EXPANDED.setdefault(ob["acb_key"], set())
        p = tuple(_path(self.path))
        ex.symmetric_difference_update({p})
        return {"FINISHED"}


def _path(s: str):
    return [int(x) if x.lstrip("-").isdigit() else x for x in s.split("/") if x != ""]


class ACB_OT_edit_field(bpy.types.Operator):
    """Edit this field"""
    bl_idname = "acb.edit_field"
    bl_label = "Edit Field"
    path: bpy.props.StringProperty()
    value: bpy.props.StringProperty(name="Value")

    def invoke(self, context, event):
        s = sess(context)
        ob, key = active_element(context)
        o = ops.element_obj(s.doc, key)
        k, ev = fields.kind_at(o, _path(self.path))
        self.value = fields.format_value(k, fields.get(o, _path(self.path)), ev)
        if ev:
            self.value = self.value.split(" (")[0]
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        s = sess(context)
        ob, key = active_element(context)
        o = ops.element_obj(s.doc, key)
        k, ev = fields.kind_at(o, _path(self.path))
        self.layout.label(text=self.path.replace("/", " > "))
        self.layout.prop(self, "value")
        if ev:
            self.layout.label(text="one of: " + ", ".join(n for _v, n in ev)[:200])

    def execute(self, context):
        s = sess(context)
        ob, key = active_element(context)
        o = ops.element_obj(s.doc, key)
        p = _path(self.path)
        k, ev = fields.kind_at(o, p)
        try:
            raw = fields.parse_value(k, self.value, ev)
            if p == ["GlobalMatrix"]:
                ops.set_matrix(s.doc, key, raw)
                ob.matrix_world = Matrix(S.to_blender(raw))
                s.baseline[ob["acb_key"]] = raw
            else:
                ops.set_field(s.doc, key, p, raw)
        except Exception as ex:
            self.report({"ERROR"}, f"{self.path}: {ex}")
            return {"CANCELLED"}
        if ob.get("acb_kind") == "chest_spawn":
            s.wd.sync_chests()
        return {"FINISHED"}


class ACB_OT_select_link(bpy.types.Operator):
    """Select the object this link points at"""
    bl_idname = "acb.select_link"
    bl_label = "Go To"
    target: bpy.props.StringProperty()

    def execute(self, context):
        s = sess(context)
        t = int(self.target, 16)
        name = s.id_to_obj.get(t)
        ob = bpy.data.objects.get(name or "")
        if ob is None:
            where = s.doc.name_of(t) if t in s.doc.info else "outside this map forge (or not imported)"
            self.report({"INFO"}, f"{t:#010x}: {where}")
            return {"CANCELLED"}
        for x in context.selected_objects:
            x.select_set(False)
        ob.select_set(True)
        context.view_layer.objects.active = ob
        return {"FINISHED"}


# ---------------------------------------------------------------- panels --

class ACBPanel:
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "ACB"


class ACB_PT_map(ACBPanel, bpy.types.Panel):
    bl_label = "ACB Map"

    def draw(self, context):
        lay = self.layout
        s = sess(context)
        if s is None:
            lay.operator("acb.open_map", icon="FILEBROWSER")
            if context.scene.get("acb_forge"):
                lay.operator("acb.reconnect", icon="LINKED")
            return
        lay.label(text=os.path.basename(s.source), icon="WORLD")
        col = lay.column(align=True)
        col.operator("acb.apply", icon="CHECKMARK")
        col.operator("acb.save", icon="FILE_TICK")
        row = col.row(align=True)
        row.operator("acb.install", icon="IMPORT")
        row.operator("acb.uninstall", icon="LOOP_BACK")
        if getattr(s, "last_save", None):
            box = lay.box()
            box.label(text=f"saved: {os.path.basename(s.last_save)}")
            if s.last_problems:
                box.label(text=f"{len(s.last_problems)} new problems:", icon="ERROR")
                for p in s.last_problems[:6]:
                    box.label(text=p[:90])
            else:
                box.label(text="checks passed", icon="CHECKMARK")
        if s.log:
            box = lay.box()
            for ln in s.log[-6:]:
                box.label(text=ln[:90])


class ACB_PT_tools(ACBPanel, bpy.types.Panel):
    bl_label = "Add / Collision / Climbing"
    bl_parent_id = "ACB_PT_map"

    @classmethod
    def poll(cls, context):
        return sess(context) is not None

    def draw(self, context):
        col = self.layout.column(align=True)
        col.operator("acb.add_element", icon="ADD")
        col.label(text="Shift+D copies an element, X deletes it (on Apply)")
        col.separator()
        vis = s.collision_visible() if (s := sess(context)) is not None else True
        col.operator("acb.toggle_collision", text="Hide Collision" if vis else "Show Collision",
                     icon="HIDE_OFF" if vis else "HIDE_ON")
        col.operator("acb.edit_boundary", icon="EDITMODE_HLT")
        col.operator("acb.clear_scenery", icon="TRASH")
        col.operator("acb.new_collision", icon="MESH_CUBE")
        col.operator("acb.new_scenery", icon="SCENE_DATA")
        col.operator("acb.replace_visual", icon="MOD_MESHDEFORM")
        col.operator("acb.make_unique", icon="DUPLICATE")
        col.separator()
        cv = s.collection_visible("Climb Edges") if s is not None else False
        col.operator("acb.toggle_climb", text="Hide Climb Edges" if cv else "Show Climb Edges",
                     icon="HIDE_OFF" if cv else "HIDE_ON")
        col.operator("acb.generate_climb", icon="MOD_EDGESPLIT")
        col.operator("acb.strip_guidance", icon="X")


class ACB_PT_escort(ACBPanel, bpy.types.Panel):
    bl_label = "Escort Paths"
    bl_parent_id = "ACB_PT_map"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        return sess(context) is not None

    def draw(self, context):
        s = sess(context)
        lay = self.layout
        if 7 not in s.wd.modes:
            lay.label(text="this world has no Escort data")
            return
        row = lay.row(align=True)
        row.prop(context.scene, "acb_path_index", text=f"Path (of {len(s.vip_paths)})")
        row.operator("acb.path_new", text="", icon="ADD")
        row.operator("acb.path_delete", text="", icon="REMOVE")
        i = context.scene.acb_path_index
        if not 0 <= i < len(s.vip_paths):
            return
        lay.operator("acb.path_add_selected", icon="PLUS")
        for j, n in enumerate(s.vip_paths[i]):
            r = lay.row(align=True)
            nm = s.doc.name_of(n["flow"])
            op = r.operator("acb.path_node", text=f"{j}: {nm}", emboss=False)
            op.index, op.action = j, "select"
            for act, icon, on in (("spawn", "OUTLINER_OB_ARMATURE", n["spawn"]),
                                  ("checkpoint", "BOOKMARKS", n["checkpoint"])):
                op = r.operator("acb.path_node", text="", icon=icon, depress=on)
                op.index, op.action = j, act
            for act, icon in (("up", "TRIA_UP"), ("down", "TRIA_DOWN"), ("remove", "X")):
                op = r.operator("acb.path_node", text="", icon=icon)
                op.index, op.action = j, act


class ACB_PT_inspector(ACBPanel, bpy.types.Panel):
    bl_label = "Inspector"
    bl_parent_id = "ACB_PT_map"

    @classmethod
    def poll(cls, context):
        return sess(context) is not None

    def draw(self, context):
        s = sess(context)
        ob, key = active_element(context)
        lay = self.layout
        if ob is None:
            lay.label(text="select an ACB object")
            return
        try:
            o = ops.element_obj(s.doc, key)
        except Exception as ex:
            lay.label(text=str(ex))
            return
        lay.label(text=f"{ob.get('acb_kind', '')}  {key[0]:#010x}" + (f" child {key[1]}" if key[1] >= 0 else ""))
        if ob.get("acb_kind") in S.LOCKED_KINDS:
            lay.label(text="position locked (navmesh-bound)", icon="LOCKED")
        ex = EXPANDED.setdefault(ob["acb_key"], {("Components",)})
        col = lay.column(align=True)
        for r in fields.rows(o, ex)[:400]:
            row = col.row(align=True)
            row.separator(factor=1.5 * r.depth)
            p = "/".join(str(x) for x in r.path)
            if r.expandable:
                op = row.operator("acb.toggle", text=r.label, emboss=False,
                                  icon="DISCLOSURE_TRI_DOWN" if tuple(r.path) in ex else "DISCLOSURE_TRI_RIGHT")
                op.path = p
            elif r.editable:
                row.label(text=r.label)
                op = row.operator("acb.edit_field", text=r.text[:40])
                op.path = p
            elif r.link:
                row.label(text=r.label)
                op = row.operator("acb.select_link", text=r.text, icon="FORWARD")
                op.target = f"{r.link:08x}"
            else:
                row.label(text=f"{r.label}: {r.text[:50]}")


CLASSES = (ACB_OT_open_map, ACB_OT_toggle_collision, ACB_OT_reconnect, ACB_OT_apply, ACB_OT_save, ACB_OT_install, ACB_OT_uninstall,
           ACB_OT_add_element, ACB_OT_new_collision, ACB_OT_new_scenery, ACB_OT_clear_scenery, ACB_OT_edit_boundary, ACB_OT_replace_visual, ACB_OT_make_unique, ACB_OT_generate_climb, ACB_OT_toggle_climb,
           ACB_OT_strip_guidance,
           ACB_OT_path_new, ACB_OT_path_delete, ACB_OT_path_add_selected, ACB_OT_path_node,
           ACB_OT_toggle, ACB_OT_edit_field, ACB_OT_select_link,
           ACB_PT_map, ACB_PT_tools, ACB_PT_escort, ACB_PT_inspector)


def register():
    bpy.types.Scene.acb_path_index = bpy.props.IntProperty(name="Path", min=0, default=0)
    for c in CLASSES:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
    del bpy.types.Scene.acb_path_index
