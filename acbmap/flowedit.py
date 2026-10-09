"""Editing crowd flows (the lines crowd NPCs and Escort VIPs walk) together with the navigation data behind them.

A CrowdFlow's points are mirrored in its map's NavMeshManagers (navmesh.py): each point is a waypoint of its own
(quantized position, one link to the flow's metalink), and the flow's MetaLink lists every point's navmesh triangle
and, per point, the network waypoints NPCs use to get on and off the flow. set_flow_points rewrites all of it for a
new list of points:
- every point is put on the navmesh (its height snapped to the surface; a point not over the navmesh is an error);
- its triangle goes into the flow's TriangleArray and the metalink's TriangleList;
- waypoints are reused by index and moved (each is quantized in the cell its point is in, while staying in the
  metalink's manager, as retail does), new points get new waypoints appended there, and the waypoints of removed
  points are deactivated (indices elsewhere stay valid);
- points that moved (or are new) get their network connections recomputed (NavData.visible_waypoints); untouched
  points keep retail's lists.
Nothing else in the navigation data refers to crowd-flow metalinks (triangles' per-edge metalink lists only hold the
other link types), so the navmesh itself is left as is.
"""
from __future__ import annotations

import math
import struct

from . import ops
from .doc import MapDocument
from .geom import matrix_rows
from .navmesh import NavData, h, z_on
from .flows import flow_component

MOVED = 0.01   # metres: a point closer than this to where it was keeps its connections


def _u16(v) -> bytes:
    return int(v).to_bytes(2, "little")


def _ref(obj, **vals):
    for k, v in vals.items():
        obj.fields[k] = _u16(v)
    return obj


def world_points(o) -> list[tuple]:
    cf = flow_component(o)
    rows = matrix_rows(o.fields["GlobalMatrix"])
    out = []
    for fp in cf.fields["NavFlowPointsLocal"]:
        p = struct.unpack("<3f", fp.fields["FlowPosition"][:12])
        out.append(tuple(p[0] * rows[0][i] + p[1] * rows[1][i] + p[2] * rows[2][i] + rows[3][i] for i in range(3)))
    return out


def _local(rows_inv, p):
    return tuple(p[0] * rows_inv[0][i] + p[1] * rows_inv[1][i] + p[2] * rows_inv[2][i] + rows_inv[3][i]
                 for i in range(3))


def _clone(doc, obj):
    return ops.clone_tree(doc, obj)


def set_flow_points(doc: MapDocument, nav: NavData, uid: int, points, max_dz: float = 1.5) -> dict:
    """Make crowd flow `uid` (a CrowdFlow/NavFlow entity root) follow world `points` (at least 2). Returns
    {"points", "moved", "new_waypoints", "deactivated"}. Raises ops.EditError when a point isn't over the navmesh."""
    o = doc.obj(uid)
    cf = flow_component(o) if o is not None else None
    if cf is None:
        raise ops.EditError(f"{uid:#x} isn't a crowd flow")
    points = [tuple(map(float, p[:3])) for p in points]
    if len(points) < 2:
        raise ops.EditError("a crowd flow needs at least 2 points")
    mlr = cf.fields["MetaLinkRef"]
    mm, mi = h(mlr.fields["ManagerIndex"]), h(mlr.fields["MetaLinkIndex"])
    if mm not in nav.mgr:
        raise ops.EditError(f"the flow's metalink manager {mm} isn't in this map")
    ml = nav.metalink(mm, mi)
    old_pts = world_points(o)
    old_wps = [(h(w.fields["ManagerIndex"]), h(w.fields["WayPointIndex"])) for w in cf.fields["WayPointArray"]]
    old_ows = ml.fields["ObjectWayPoints"]
    old_tris = [(h(t.fields["ManagerIndex"]), h(t.fields["NavMeshIndex"]), h(t.fields["TriangleIndex"]))
                for t in cf.fields["TriangleArray"]]
    # each new point that sits where an old one was keeps that point's waypoint, triangle and connections
    src, free = [], set(range(len(old_pts)))
    for p in points:
        j = min(free, key=lambda j: math.dist(old_pts[j], p), default=None)
        if j is not None and math.dist(old_pts[j], p) <= MOVED:
            free.discard(j)
            src.append(j)
        else:
            src.append(None)
    same = [j is not None for j in src]
    spare = sorted(free)   # waypoints of old points nothing kept: reused for moved / new points first

    # 1. every point onto the navmesh (untouched points stay exactly as they are)
    tris, snapped = [], []
    for k, p in enumerate(points):
        if same[k]:
            tris.append(old_tris[src[k]])
            snapped.append(old_pts[src[k]])
            continue
        t = nav.triangle_at(p, max_dz)
        if t is None:
            raise ops.EditError(f"point {k} ({p[0]:.1f}, {p[1]:.1f}, {p[2]:.1f}) isn't on the navmesh "
                                f"(NPCs can only walk where the map's navmesh is)")
        tris.append(t)
        snapped.append((p[0], p[1], z_on(nav.triangle(t), p)))

    touched = {mm}
    stats = {"points": len(snapped), "moved": 0, "new_waypoints": 0, "deactivated": 0}
    wps, conns = [], []
    template = nav.waypoints(old_wps[0][0])[old_wps[0][1]]
    for k, p in enumerate(snapped):
        if same[k]:
            wps.append(old_wps[src[k]])
            conns.append(list(old_ows[src[k]].custom))
            continue
        cell, qx, qy, qz = nav.quantize(p)
        if cell not in nav.mgr:
            raise ops.EditError(f"point {k} is in a navmesh cell this map doesn't have ({cell})")
        if spare:
            wm, wi = old_wps[spare.pop(0)]
        else:   # like retail, a flow's waypoints all live in its metalink's manager, linked to the metalink
            w = ops.clone_tree(doc, template)
            w.custom = dict(template.custom, Links=[(2, 4 | (mi << 4))])
            w.fields["Active"] = b"\x01"
            wm, wi = mm, len(nav.waypoints(mm))
            if wi >= 0x1000:
                raise ops.EditError(f"navmesh cell {mm} has no room for another waypoint")
            nav.waypoints(mm).append(w)
            stats["new_waypoints"] += 1
        c = nav.waypoints(wm)[wi].custom
        c["ManagerIndex"], c["X"], c["Y"], c["Z"] = cell, qx, qy, qz
        touched.add(wm)
        wps.append((wm, wi))
        stats["moved"] += 1
        conns.append([(i, m) for m, i in nav.visible_waypoints(p, tris[k])])
    for j in spare:   # removed points: their waypoints stay (indices elsewhere), inactive
        wm, wi = old_wps[j]
        nav.waypoints(wm)[wi].fields["Active"] = b"\x00"
        touched.add(wm)
        stats["deactivated"] += 1

    # 2. the flow's arrays and the metalink's, element objects reused by index (new ones cloned with fresh ids)
    def resize(lst, n):
        while len(lst) > n:
            lst.pop()
        while len(lst) < n:
            lst.append(_clone(doc, lst[-1]))
            if lst[-1].custom is not None:
                lst[-1].custom = list(lst[-1].custom)
        return lst

    n = len(snapped)
    inv = ops._inv(matrix_rows(o.fields["GlobalMatrix"]))
    old_local = [fp.fields["FlowPosition"] for fp in cf.fields["NavFlowPointsLocal"]]
    for k, (fp, p) in enumerate(zip(resize(cf.fields["NavFlowPointsLocal"], n), snapped)):
        if same[k]:
            fp.fields["FlowPosition"] = old_local[src[k]]   # exact old bytes
            continue
        w = struct.unpack("<f", fp.fields["FlowPosition"][12:16])[0]
        fp.fields["FlowPosition"] = struct.pack("<4f", *_local(inv, p), w)
    for lst in (cf.fields["TriangleArray"], ml.fields["TriangleList"]):
        for r, (m, nm, t) in zip(resize(lst, n), tris):
            _ref(r, TriangleIndex=t, NavMeshIndex=nm, ManagerIndex=m)
    for r, (m, i) in zip(resize(cf.fields["WayPointArray"], n), wps):
        _ref(r, WayPointIndex=i, ManagerIndex=m)
    for ow, (m, i), cn in zip(resize(ml.fields["ObjectWayPoints"], n), wps, conns):
        _ref(ow.fields["WayPoint"], WayPointIndex=i, ManagerIndex=m)
        ow.custom = cn
    doc.touch(uid)
    for m in touched:
        doc.touch(nav.uid_of[m])
    return stats
