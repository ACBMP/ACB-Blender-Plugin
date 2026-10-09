"""The map's navigation data (NavMeshManager roots), decoded: navmesh triangles, the waypoint network, metalinks.

Layout (RE'd from acbmp_sf.exe, see ac-re-memory acb/maps/acb_navmesh_format.md):
- One NavMeshManager per 32 m grid cell; its Index = row * columns + column, the cell's min corner = origin +
  (column, row) * cell. Columns / origin aren't stored in the forge (NavInfoManager globals); they're fitted from the
  managers' AABVs.
- NavMesh: VertexList (world space, vec4), TriangleVertex (u16, 3 per triangle), Triangles: NeighborTriangleIndex[k]
  is the triangle across edge (v_k, v_k+1), >= 0xffe0 = none in this navmesh; the triangle's custom tail holds per-edge
  metalink lists (edges 0, 1, 2: how a walk crosses into another navmesh) and a list of waypoints (list 3).
- Every element reference inside a manager is "relative": low 4 bits = cell offset in the 3x3 block around the
  manager (4 = itself, 15 = none), the rest = index.
- WayPoint: position quantized in a cell (its own ManagerIndex word, which needn't be the manager holding it; x/y:
  10-bit steps over cell + 2 m starting 1 m before the cell, z: half float), position triangle (relative manager, navmesh, triangle), links (distance * 128 * 4, relative
  waypoint ref); a link with distance field 2 points at a metalink instead.
- Crowd flows: every flow point is a waypoint of its own (no position triangle, one link to the flow's metalink),
  all kept in the metalink's manager even for points in other cells.
  The flow's MetaLink (LinkType 5, NavigationObject = the flow entity) lists the points' triangles (= the flow's
  TriangleArray) and, per point, an ObjectWayPoint: the point's waypoint plus the network waypoints it connects to
  ((index, manager) pairs).
"""
from __future__ import annotations

import math
import struct

from .doc import MapDocument

REL = [(dx, dy) for dy in (-1, 0, 1) for dx in (-1, 0, 1)]   # relative cell offsets, 4 = the manager itself
NONE_REL = 15
CROWD_FLOW_LINK = 5


def h(b) -> int:
    return int.from_bytes(b, "little") if isinstance(b, (bytes, bytearray)) else int(b)


def half(v: int) -> float:
    return struct.unpack("<e", int(v).to_bytes(2, "little"))[0]


def to_half(f: float) -> int:
    return int.from_bytes(struct.pack("<e", f), "little")


class Mesh:
    __slots__ = ("verts", "tris", "nbr", "custom", "obj")

    def __init__(self, nm):
        self.obj = nm
        self.verts = [struct.unpack("<4f", v)[:3] for v in nm.fields["VertexList"]]
        idx = [h(x) for x in nm.fields["TriangleVertex"]]
        self.tris = [tuple(idx[3 * i:3 * i + 3]) for i in range(len(idx) // 3)]
        self.nbr = [[h(x) for x in t.fields["NeighborTriangleIndex"]] for t in nm.fields["Triangles"]]
        self.custom = [t.custom for t in nm.fields["Triangles"]]

    def corners(self, t):
        return [self.verts[i] for i in self.tris[t]]


def _orient(a, b, p) -> float:
    return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])


def inside2d(T, p, eps=1e-4) -> bool:
    d = [_orient(T[k], T[(k + 1) % 3], p) for k in range(3)]
    area = _orient(T[0], T[1], T[2])
    if area < 0:
        d = [-x for x in d]
    return all(x >= -eps * max(1.0, abs(area)) for x in d)


def z_on(T, p) -> float:
    (ax, ay, az), (bx, by, bz), (cx, cy, cz) = T
    det = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
    if abs(det) < 1e-12:
        return (az + bz + cz) / 3
    l1 = ((by - cy) * (p[0] - cx) + (cx - bx) * (p[1] - cy)) / det
    l2 = ((cy - ay) * (p[0] - cx) + (ax - cx) * (p[1] - cy)) / det
    return l1 * az + l2 * bz + (1 - l1 - l2) * cz


class NavData:
    def __init__(self, doc: MapDocument):
        self.doc = doc
        self.mgr = {}
        self.uid_of = {}
        for u in doc.uids("NavMeshManager"):
            o = doc.obj(u)
            if o is None:
                continue
            i = h(o.fields["Index"])
            self.mgr[i] = o
            self.uid_of[i] = u
        self.cols, self.ox, self.oy, self.cell = self._fit_grid()
        self._mesh = {}

    # ------------------------------------------------------------ grid --

    def _fit_grid(self):
        boxes = {}
        for i, o in self.mgr.items():
            bv = o.fields["AABV"].fields
            mn = struct.unpack("<3f", bv["Min"][:12])
            mx = struct.unpack("<3f", bv["Max"][:12])
            boxes[i] = (mn, mx)
        if not boxes:
            return 1, 0.0, 0.0, 32.0
        cell = next(iter(boxes.values()))[1][0] - next(iter(boxes.values()))[0][0]
        for cols in range(1, 1025):
            orgs = {(round(mn[0] - (i % cols) * cell, 3), round(mn[1] - (i // cols) * cell, 3))
                    for i, (mn, _mx) in boxes.items()}
            if len(orgs) == 1:
                ox, oy = orgs.pop()
                return cols, ox, oy, cell
        raise ValueError("navmesh managers don't fit a grid")

    def abs_manager(self, m: int, rel: int):
        if rel >= 9:
            return None
        dx, dy = REL[rel]
        return m + dx + dy * self.cols

    def rel_of(self, m: int, target: int) -> int:
        d = target - m
        for r, (dx, dy) in enumerate(REL):
            if dx + dy * self.cols == d:
                return r
        raise ValueError(f"manager {target} isn't next to {m}")

    def manager_at(self, x, y) -> int:
        c = int(math.floor((x - self.ox) / self.cell))
        r = int(math.floor((y - self.oy) / self.cell))
        return r * self.cols + c

    def cell_min(self, m: int):
        return self.ox + (m % self.cols) * self.cell, self.oy + (m // self.cols) * self.cell

    # ------------------------------------------------------------ elements --

    def mesh(self, m, n) -> Mesh:
        k = (m, n)
        if k not in self._mesh:
            self._mesh[k] = Mesh(self.mgr[m].fields["NavigationMeshes"][n])
        return self._mesh[k]

    def triangle(self, ref):
        """World corners of (manager, navmesh, triangle)."""
        m, n, t = ref
        return self.mesh(m, n).corners(t)

    def waypoints(self, m):
        return self.mgr[m].fields["MyWayPointNetwork"].fields["WayPoints"]

    def waypoint_pos(self, m, i):
        c = self.waypoints(m)[i].custom
        x0, y0 = self.cell_min(c["ManagerIndex"])   # the cell it's quantized in, not the manager holding it
        s = (self.cell + 2) / 1024
        return (x0 - 1 + c["X"] * s, y0 - 1 + c["Y"] * s, half(c["Z"]))

    def quantize(self, p):
        """(cell, X, Y, Z) waypoint words for world position p (quantized in the cell containing it)."""
        m = self.manager_at(p[0], p[1])
        x0, y0 = self.cell_min(m)
        s = (self.cell + 2) / 1024
        qx, qy = round((p[0] - x0 + 1) / s), round((p[1] - y0 + 1) / s)
        return m, min(max(qx, 0), 1023), min(max(qy, 0), 1023), to_half(p[2])

    def waypoint_triangle(self, m, i):
        c = self.waypoints(m)[i].custom
        mm = self.abs_manager(m, c["Flags"] & 0xF)
        if mm is None or c["TriangleIndex"] == 0xFFFF:
            return None
        return (mm, c["Bits"], c["TriangleIndex"])

    def metalink(self, m, i):
        return self.mgr[m].fields["MetaLinks"][i]

    def network_waypoints(self):
        """[(manager, index, position, triangle)] of the ordinary (non-object) waypoints."""
        out = []
        for m in self.mgr:
            for i, w in enumerate(self.waypoints(m)):
                tri = self.waypoint_triangle(m, i)
                if tri is not None and w.fields["Active"] != b"\0":
                    out.append((m, i, self.waypoint_pos(m, i), tri))
        return out

    # ------------------------------------------------------------ queries --

    def _tri_index(self, m):
        """[(minx, miny, maxx, maxy, (m, n, t))] of manager m's triangles, built once."""
        cache = self.__dict__.setdefault("_tri_idx", {})
        if m not in cache:
            out = []
            for n in range(len(self.mgr[m].fields["NavigationMeshes"])):
                ms = self.mesh(m, n)
                for t in range(len(ms.tris)):
                    T = ms.corners(t)
                    out.append((min(v[0] for v in T), min(v[1] for v in T), max(v[0] for v in T),
                                max(v[1] for v in T), (m, n, t)))
            cache[m] = out
        return cache[m]

    def triangle_at(self, p, max_dz=1.0):
        """(manager, navmesh, triangle) under world position p: the triangle containing it in 2D whose surface is
        closest in height (within max_dz); None if p isn't over the navmesh."""
        m0 = self.manager_at(p[0], p[1])
        best, best_dz = None, max_dz
        for dx, dy in REL:
            m = m0 + dx + dy * self.cols
            if m not in self.mgr:
                continue
            for x0, y0, x1, y1, ref in self._tri_index(m):
                if x0 > p[0] or x1 < p[0] or y0 > p[1] or y1 < p[1]:
                    continue
                T = self.triangle(ref)
                if inside2d(T, p):
                    dz = abs(z_on(T, p) - p[2])
                    if dz <= best_dz:
                        best, best_dz = ref, dz
        return best

    def visible_waypoints(self, p, p_tri, radius=60.0, corridor=0.4, limit=32):
        """Network waypoints a flow point at p connects to: those within `radius` that a `corridor`-wide strip from
        p reaches over the navmesh (the line itself and its copies offset sideways by corridor / 2), nearest first,
        at most `limit`. Retail's lists are a pruned subset of this (its extra rule isn't known); every link here is
        a walkable straight line."""
        if not hasattr(self, "_net"):
            self._net = self.network_waypoints()
        cands = sorted((math.dist(p[:2], q[:2]), m, i, q, qt) for m, i, q, qt in self._net
                       if math.dist(p[:2], q[:2]) <= radius and abs(q[2] - p[2]) < 3.0)
        out = []
        r = corridor / 2
        for d, m, i, q, qt in cands:
            if not self.line_clear(p, p_tri, q, qt):
                continue
            if d > 1e-3 and r > 0:
                nx, ny = -(q[1] - p[1]) / d * r, (q[0] - p[0]) / d * r
                ok = True
                for sgn in (-1, 1):
                    a = (p[0] + sgn * nx, p[1] + sgn * ny, p[2])
                    b = (q[0] + sgn * nx, q[1] + sgn * ny, q[2])
                    at, bt = self.triangle_at(a, 1.0), self.triangle_at(b, 1.0)
                    if at is None or bt is None or not self.line_clear(a, at, b, bt):
                        ok = False
                        break
                if not ok:
                    continue
            out.append((m, i))
            if len(out) >= limit:
                break
        return out

    def _edge_links(self, ref, k):
        """Seam metalinks on edge k of a triangle: [(manager, metalink index)]."""
        m, n, t = ref
        c = self.mesh(m, n).custom[t]
        start = sum(c["counts"][:k])
        out = []
        for r in c["data"][start:start + c["counts"][k]]:
            mm = self.abs_manager(m, r & 0xF)
            if mm in self.mgr:
                out.append((mm, r >> 4))
        return out

    def _across(self, ref, k, x, z, seam_types):
        m, n, t = ref
        nb = self.mesh(m, n).nbr[t][k]
        if nb < 0xFFE0:
            return (m, n, nb)
        for mm, li in self._edge_links(ref, k):
            ml = self.metalink(mm, li)
            if h(ml.fields["LinkType"]) not in seam_types:
                continue
            for tr in ml.fields["TriangleList"]:
                cand = (h(tr.fields["ManagerIndex"]), h(tr.fields["NavMeshIndex"]), h(tr.fields["TriangleIndex"]))
                if cand == ref or cand[0] not in self.mgr:
                    continue
                T = self.triangle(cand)
                if inside2d(T, x, eps=1e-2) and abs(z_on(T, x) - z) < 0.6:
                    return cand
        return None

    def line_clear(self, p, p_tri, q, q_tri, seam_types=(0,), max_steps=2000) -> bool:
        """True when the straight line p -> q (2D) stays on the navmesh, walking triangle to triangle from p_tri until
        q_tri (crossing into other navmeshes through seam metalinks, like NavMeshManager::ComputeNextTriangle)."""
        cur = p_tri
        for _ in range(max_steps):
            if cur == q_tri:
                return True
            T = self.triangle(cur)
            best = None
            for k in range(3):
                a, b = T[k], T[(k + 1) % 3]
                d1, d2 = _orient(a, b, p), _orient(a, b, q)
                e1, e2 = _orient(p, q, a), _orient(p, q, b)
                if (e1 > 0) == (e2 > 0) and e1 != 0 and e2 != 0:
                    continue   # the line doesn't pass through this edge
                denom = (d1 - d2)
                if abs(denom) < 1e-12:
                    continue
                s = d1 / denom   # parameter along p -> q
                if s < -1e-6 or s > 1 + 1e-6:
                    continue
                if best is None or s > best[0]:
                    best = (s, k)
            if best is None:
                return False
            s, k = best
            x = (p[0] + (q[0] - p[0]) * s, p[1] + (q[1] - p[1]) * s)
            z = z_on(T, x)
            nxt = self._across(cur, k, x, z, seam_types)
            if nxt is None:
                return False
            cur = nxt
        return False
