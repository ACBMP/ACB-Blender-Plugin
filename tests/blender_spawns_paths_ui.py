"""xvfb-run -a blender --factory-startup --enable-event-simulate --python tests/blender_spawns_paths_ui.py -- <forge> [shot.png]

Spawns and Escort paths edited the way a user does it, with simulated clicks in the 3D view: Draw Path (click a start
flow, click a flow further on: the connecting flows are filled in; Ctrl+click marks a VIP spawn + checkpoint;
Enter), Add Spawn (Team 2) at the cursor, Set Spawn Kind (free-for-all -> Chest), Drop to Ground; then Save, and the
saved forge is reopened and checked. Optional: a screenshot of the view."""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(REPO, "blender"), REPO, os.path.join(REPO, "vendor", "anvilforge-py", "src")]

import bpy  # noqa: E402
from bpy_extras.view3d_utils import location_3d_to_region_2d  # noqa: E402
from mathutils import Quaternion, Vector  # noqa: E402

import acb_map_editor  # noqa: E402

acb_map_editor.register()
from acb_map_editor import session as S  # noqa: E402
from acbmap import flows as F  # noqa: E402
from acbmap import spawns as SPW  # noqa: E402
from acbmap.doc import MapDocument  # noqa: E402
from acbmap.worlddata import WorldData  # noqa: E402

args = sys.argv[sys.argv.index("--") + 1:]
forge = args[0]
shot = args[1] if len(args) > 1 else None
ok = True
st = {}


def check(c, msg):
    global ok
    print("SPUI", "OK " if c else "FAIL", msg)
    ok &= bool(c)


def view():
    win = bpy.context.window_manager.windows[0]
    area = next(a for a in win.screen.areas if a.type == "VIEW_3D")
    region = next(r for r in area.regions if r.type == "WINDOW")
    return win, area, region


def click(uid, ctrl=False):
    win, area, region = view()
    f = st["s"].flow_graph[uid]
    a, b = f.points[len(f.points) // 2 - 1], f.points[len(f.points) // 2]
    p = location_3d_to_region_2d(region, area.spaces.active.region_3d, (Vector(a) + Vector(b)) / 2)
    x, y = int(region.x + p.x), int(region.y + p.y)
    win.event_simulate("MOUSEMOVE", "NOTHING", x=x, y=y)
    if ctrl:
        win.event_simulate("LEFT_CTRL", "PRESS", x=x, y=y)
    win.event_simulate("LEFTMOUSE", "PRESS", x=x, y=y)
    win.event_simulate("LEFTMOUSE", "RELEASE", x=x, y=y)
    if ctrl:
        win.event_simulate("LEFT_CTRL", "RELEASE", x=x, y=y)


def step1():
    s = S.Session(bpy.context.scene, forge)
    s.build(with_visuals=True)
    st["s"] = s
    fl = s.flow_graph
    retail = [n["flow"] for n in s.vip_paths[0]]
    a, b = retail[0], retail[min(5, len(retail) - 1)]
    st["a"], st["b"], st["route"] = a, b, F.chain(fl, a, b)
    st["n_paths"] = len(s.vip_paths)
    win, area, region = view()
    sp = area.spaces.active
    sp.shading.type = "SOLID"
    r3d = sp.region_3d
    pts = [Vector(p) for u in st["route"] for p in fl[u].points]
    c = sum(pts, Vector()) / len(pts)
    r3d.view_location = c
    r3d.view_rotation = Quaternion((1, 0, 0, 0))   # top view
    r3d.view_perspective = "ORTHO"
    r3d.view_distance = max(max((p - c).length for p in pts) * 2.4, 30)
    with bpy.context.temp_override(window=win, area=area, region=region):
        check(bpy.ops.acb.path_new() == {"RUNNING_MODAL"}, "New Path starts drawing")
    return 0.5


def step2():
    click(st["a"])
    return 0.3


def step3():
    click(st["b"])
    return 0.3


def step4():
    click(st["b"], ctrl=True)
    return 0.3


def step5():
    win, _a, _r = view()
    win.event_simulate("RET", "PRESS", x=10, y=10)
    win.event_simulate("RET", "RELEASE", x=10, y=10)
    return 0.3


def step6():
    s = st["s"]
    try:
        p = s.vip_paths[-1]
        check(len(s.vip_paths) == st["n_paths"] + 1, "one path added")
        check([n["flow"] for n in p] == st["route"], f"path follows the connected flows ({len(p)} nodes, "
                                                       f"{len(st['route'])} in the route)")
        check(p and p[-1]["spawn"] and p[-1]["checkpoint"] and not p[0]["spawn"], "Ctrl+click marked the end node")
        win, area, region = view()
        with bpy.context.temp_override(window=win, area=area, region=region):
            # spawns: add a Team 2 spawn at the cursor, then a free-for-all one -> Chest, then drop one to the ground
            bpy.context.scene.cursor.location = Vector(s.flow_graph[st["a"]].middle)
            st["counts"] = SPW.counts(s.doc)
            check(bpy.ops.acb.spawn_add(spawn_type=SPW.TEAM, team=2) == {"FINISHED"}, "Add Spawn (Team 2)")
            new = bpy.context.active_object
            check(new is not None and new.users_collection[0].name.startswith("Spawns: Team 2"),
                  "new spawn in the Team 2 collection")
            ffa = next(o for o in bpy.context.scene.objects if o.get("acb_kind") == "spawn"
                       and o.users_collection[0].name.startswith("Spawns: Free-for-all"))
            st["chest_key"] = S.parse_key(ffa["acb_key"])
            for x in bpy.context.selected_objects:
                x.select_set(False)
            ffa.select_set(True)
            bpy.context.view_layer.objects.active = ffa
            check(bpy.ops.acb.spawn_set(spawn_type=SPW.CHEST, team=0) == {"FINISHED"}, "Set Spawn Kind -> Chest")
            ch = bpy.data.objects.get(s.objects[S.keystr(st["chest_key"])])
            check(ch is not None and ch.get("acb_kind") == "chest_spawn" and ch.select_get(),
                  "spawn rebuilt as a chest, still selected")
            drop = next(o for o in bpy.context.scene.objects if o.get("acb_kind") == "spawn"
                        and o.users_collection[0].name.startswith("Spawns: Team 1"))
            z0 = drop.matrix_world.translation.z
            drop.location.z += 6.0
            for x in bpy.context.selected_objects:
                x.select_set(False)
            drop.select_set(True)
            bpy.context.view_layer.update()
            check(bpy.ops.acb.spawn_drop() == {"FINISHED"}, "Drop to Ground")
            bpy.context.view_layer.update()
            st["drop"] = (drop["acb_key"], drop.matrix_world.translation.z)
            check(abs(drop.matrix_world.translation.z - z0) < 1.5, f"dropped near its old height "
                                                                   f"({z0:.2f} -> {drop.matrix_world.translation.z:.2f})")
            if shot:
                bpy.ops.screen.screenshot(filepath=shot)
        out = os.path.join(os.path.dirname(s.doc.cache), "edited", "spui_" + os.path.basename(forge))
        probs = s.save(out)
        check(not probs, f"no new structural problems {probs}")
        d = MapDocument(out)
        c = SPW.counts(d)
        check(c["Team 2"] == st["counts"]["Team 2"] + 1, "saved: Team 2 spawn added")
        check(c["Chest"] == st["counts"]["Chest"] + 1 and c["Free-for-all"] == st["counts"]["Free-for-all"] - 1,
              "saved: one free-for-all spawn became a chest")
        wd = WorldData(d, os.path.dirname(forge))
        check(st["chest_key"][0] in {int.from_bytes(x.id, "little") for x in wd.get(2).obj.fields["chestSpawnPoints"]},
              "saved: chest capture data lists the new chest")
        got = wd.vip_paths()[-1]
        check([n["flow"] for n in got] == st["route"] and got[-1]["spawn"], "saved: drawn Escort path")
    except Exception:
        import traceback
        traceback.print_exc()
        check(False, "exception")
    print("SPUI RESULT", "PASS" if ok else "FAIL")
    bpy.ops.wm.quit_blender()


STEPS = [step1, step2, step3, step4, step5, step6]


def run():
    try:
        nxt = STEPS.pop(0)()
    except Exception:
        import traceback
        traceback.print_exc()
        print("SPUI RESULT FAIL")
        bpy.ops.wm.quit_blender()
        return None
    return nxt if STEPS else None


bpy.app.timers.register(run, first_interval=1.0)
