"""Editing where NPCs can walk: the navmesh triangles and everything derived from them.

The edit is a new set of walkable triangles. apply_triangles() takes every triangle of the map (world space), each
optionally tagged with the navmesh it came from (export_triangles() gives the current ones), and rebuilds only the
navmeshes whose triangles changed:
- triangles are clipped at the 32 m cell borders (a navmesh lives in one cell's NavMeshManager) and welded;
  untagged triangles join the navmesh they touch in their cell, or become a new navmesh;
- inside a navmesh, neighbours are the shared edges; boundary edges get a seam (LinkType-0 metalink, held by the
  lower manager, side A = its navmesh) wherever another navmesh's boundary edge lies along them, and otherwise the
  code of the old edge they lie on, or a new one: wall (0xffe4) where walkable ground was removed or rises beyond,
  ledge (0xffe2) where it drops away;
- jump / climb / drop metalinks keep the rebuilt triangles that still have their edge (SafePositions moved along),
  and are dropped when a side loses every triangle; seams touching a rebuilt navmesh are made anew;
- crowd flows: points on rebuilt triangles are re-found on the new navmesh (an error when a point is no longer over
  it -- move the flow first) and get new network connections;
- waypoints on rebuilt triangles are re-found or removed; obstacle corners of rebuilt navmeshes (the boundary turning
  away from the walkable side) get new waypoints just off the corner; waypoint links crossing a rebuilt area are
  re-checked, and the new / moved waypoints are linked to the waypoints they see (symmetric, nearest first);
  every rebuilt triangle lists its own waypoints and the nearest it sees (list 3, pathfinding's way in);
- DirectConnectionSets follow the seams (navmodel), MoppCode is left empty (NavMesh::BuildMopp compiles it at load),
  AABVs are recomputed.
Navmeshes nothing touched keep their bytes, apart from the indices that moved.

block_area() and add_surface() build such an edit from a footprint: cut the walkable triangles under it (and, for
add_surface, add the new surface, which seams to the cut edges around it).
"""
from __future__ import annotations

import copy
import math
from collections import defaultdict

from . import ops
from .navmodel import (FLOW, LEDGE, METALINK_DIST, SEAM, SEAMED, WALL, Link, Mesh, NavModel,
                       Tri, WayPoint)
from .navmesh import REL, inside2d, z_on

KEY = 1e-3          # positions closer than this (m) are the same vertex
MIN_AREA = 1e-4     # m^2: smaller triangles are dropped
SEAM_TOL = 0.02     # m: how far off another edge's line an edge may lie and still seam to it
SEAM_DZ = 0.3       # m: height difference along a seam
CORNER_TURN = 0.2   # radians: boundary turns sharper than this get a corner waypoint
CORNER_OFFSET = 0.35
CORNER_SPACING = 0.75
LINK_RADIUS = 40.0
LINK_LIMIT = 10
LIST3_RADIUS = 30.0
LIST3_LIMIT = 12


def _key(p):
    return (round(p[0] / KEY), round(p[1] / KEY), round(p[2] / KEY))


def _area2(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _orient(a, b, p):
    return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])


def _poly_area(poly):
    return sum(_area2(poly[0], poly[i], poly[i + 1]) for i in range(1, len(poly) - 1)) / 2


def _clip(poly, a, b, keep_left=True):
    """The part of convex polygon `poly` (3D points, z linear) left (or right) of the line a -> b."""
    out = []
    n = len(poly)
    for i in range(n):
        p, q = poly[i], poly[(i + 1) % n]
        dp, dq = _orient(a, b, p), _orient(a, b, q)
        if not keep_left:
            dp, dq = -dp, -dq
        if dp >= 0:
            out.append(p)
        if (dp >= 0) != (dq >= 0) and dp != dq:
            s = dp / (dp - dq)
            out.append(tuple(p[j] + (q[j] - p[j]) * s for j in range(3)))
    return out


def _fan(poly):
    return [(poly[0], poly[i], poly[i + 1]) for i in range(1, len(poly) - 1)]


def _ccw(t):
    return t if _area2(*t) > 0 else (t[0], t[2], t[1])


def subtract_convex(poly, convex):
    """Convex polygon `poly` minus convex polygon `convex` (CCW, 2D used): a list of convex pieces."""
    if _poly_area(convex) < 0:
        convex = convex[::-1]
    out, cur = [], list(poly)
    for i in range(len(convex)):
        a, b = convex[i], convex[(i + 1) % len(convex)]
        outside = _clip(cur, a, b, keep_left=False)
        if len(outside) >= 3 and abs(_poly_area(outside)) > MIN_AREA:
            out.append(outside)
        cur = _clip(cur, a, b, keep_left=True)
        if len(cur) < 3 or abs(_poly_area(cur)) <= MIN_AREA:
            return out
    return out   # what's left in `cur` is inside `convex`: removed


def intersect_convex(poly, convex):
    if _poly_area(convex) < 0:
        convex = convex[::-1]
    cur = list(poly)
    for i in range(len(convex)):
        cur = _clip(cur, convex[i], convex[(i + 1) % len(convex)])
        if len(cur) < 3:
            return []
    return cur


def _seg_dist2d(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    s = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2))
    return math.hypot(p[0] - a[0] - dx * s, p[1] - a[1] - dy * s), s


def _overlap(a, b, c, d, dz=SEAM_DZ):
    """Edges a-b and c-d on one line (within SEAM_TOL, heights within dz): their overlap length, else 0."""
    L = math.dist(a[:2], b[:2])
    if L < 1e-6:
        return 0.0
    ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
    for p in (c, d):
        if abs((p[0] - a[0]) * uy - (p[1] - a[1]) * ux) > SEAM_TOL:
            return 0.0
    L2 = math.dist(c[:2], d[:2])
    if L2 < 1e-6:
        return 0.0
    for p in (a, b):
        vx, vy = (d[0] - c[0]) / L2, (d[1] - c[1]) / L2
        if abs((p[0] - c[0]) * vy - (p[1] - c[1]) * vx) > SEAM_TOL:
            return 0.0
    tc = (c[0] - a[0]) * ux + (c[1] - a[1]) * uy
    td = (d[0] - a[0]) * ux + (d[1] - a[1]) * uy
    lo, hi = max(0.0, min(tc, td)), min(L, max(tc, td))
    if hi - lo <= 0.05:
        return 0.0
    mid = (lo + hi) / 2
    za = a[2] + (b[2] - a[2]) * mid / L
    tm = (mid - tc) / (td - tc) if td != tc else 0.0
    zc = c[2] + (d[2] - c[2]) * tm
    return hi - lo if abs(za - zc) <= dz else 0.0


class Geo:
    """Spatial queries over the model's current triangles."""

    def __init__(self, model: NavModel):
        self.model = model
        self.idx = {}
        for m in model.mgr.values():
            lst = []
            for ms in m.meshes:
                for t in ms.tris:
                    T = t.corners()
                    lst.append((min(v[0] for v in T), min(v[1] for v in T), max(v[0] for v in T),
                                max(v[1] for v in T), t))
            self.idx[m.idx] = lst

    def near(self, x, y):
        m0 = self.model.cell_of(x, y)
        for dx, dy in REL:
            m = m0 + dx + dy * self.model.cols
            for x0, y0, x1, y1, t in self.idx.get(m, ()):
                if x0 - 1e-4 <= x <= x1 + 1e-4 and y0 - 1e-4 <= y <= y1 + 1e-4:
                    yield t

    def tri_at(self, p, max_dz=1.0):
        best, bdz = None, max_dz
        for t in self.near(p[0], p[1]):
            T = t.corners()
            if inside2d(T, p):
                dz = abs(z_on(T, p) - p[2])
                if dz <= bdz:
                    best, bdz = t, dz
        return best

    def heights(self, p):
        return [z_on(t.corners(), p) for t in self.near(p[0], p[1]) if inside2d(t.corners(), p)]

    def _across(self, t, k, x, z):
        nb = t.nbr[k]
        if isinstance(nb, Tri):
            return nb
        T = t.corners()
        a, b = T[k], T[(k + 1) % 3]
        best = None
        for ln in t.links[k]:
            if ln.type != SEAM:
                continue
            for c in ln.tris:
                if c is t:
                    continue
                C = c.corners()
                if not inside2d(C, x, eps=1e-2) or abs(z_on(C, x) - z) >= 0.6:
                    continue
                # the triangle across: its middle lies right of the edge (outside t, which is CCW)
                side = _orient(a, b, tuple(sum(v[i] for v in C) / 3 for i in range(2)))
                if side < 0 and (best is None or side < best[0]):
                    best = (side, c)
        return best[1] if best else None

    def line_clear(self, p, pt, q, qt):
        """The straight line p -> q stays on the navmesh (walking triangles, crossing seams) from pt to qt."""
        cur, s_in, seen = pt, -1e-6, set()
        for _ in range(200 + int(math.dist(p[:2], q[:2]) * 20)):
            if cur is qt:
                return True
            if id(cur) in seen:   # bounced back (a T-junction or overlapping pieces): not a clean walk
                return False
            seen.add(id(cur))
            T = cur.corners()
            best = None
            for k in range(3):
                a, b = T[k], T[(k + 1) % 3]
                e1, e2 = _orient(p, q, a), _orient(p, q, b)
                if (e1 > 0) == (e2 > 0) and e1 != 0 and e2 != 0:
                    continue
                d1, d2 = _orient(a, b, p), _orient(a, b, q)
                if abs(d1 - d2) < 1e-12:
                    continue
                t = d1 / (d1 - d2)
                if s_in - 1e-6 <= t <= 1 + 1e-6 and (best is None or t > best[0]):
                    best = (t, k)
            if best is None or best[0] >= 1 - 1e-9 and cur is not qt and inside2d(T, q, eps=1e-4):
                return False   # q is in this triangle, but it isn't qt (another level)
            t, k = best
            x = (p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t)
            nxt = self._across(cur, k, x, z_on(T, x))
            if nxt is None:
                return False
            cur, s_in = nxt, t
        return False

    def visible(self, p, pt, cands, radius, limit, corridor=0.0, exclude=()):
        """Waypoints of `cands` within `radius` the line from p (on triangle pt) reaches, nearest first."""
        out = []
        r = corridor / 2
        for d, w, q in sorted(((math.dist(p[:2], q[:2]), w, q) for w, q in cands
                               if abs(q[0] - p[0]) <= radius and abs(q[1] - p[1]) <= radius
                               and math.dist(p[:2], q[:2]) <= radius and abs(q[2] - p[2]) < 3.0 and w not in exclude),
                              key=lambda e: e[0]):
            if not self.line_clear(p, pt, q, w.tri):
                continue
            if r > 0 and d > 1e-3:
                nx, ny = -(q[1] - p[1]) / d * r, (q[0] - p[0]) / d * r
                ok = True
                for sg in (-1, 1):
                    a = (p[0] + sg * nx, p[1] + sg * ny, p[2])
                    b = (q[0] + sg * nx, q[1] + sg * ny, q[2])
                    at, bt = self.tri_at(a), self.tri_at(b)
                    if at is None or bt is None or not self.line_clear(a, at, b, bt):
                        ok = False
                        break
                if not ok:
                    continue
            out.append(w)
            if len(out) >= limit:
                break
        return out


def export_triangles(model: NavModel):
    """[(corners, mesh)] for every navmesh triangle of the map."""
    return [(tuple(t.corners()), ms) for ms in model.meshes() for t in ms.tris]


def _tri_key(T):
    return frozenset(_key(p) for p in T)


def _cells_of(model, T, slack=0.01):
    """Cells (row, column) triangle T overlaps; retail triangles poke up to ~1 mm past a border, so `slack` m over
    the line doesn't count."""
    e = slack / model.cell
    x0, x1 = min(p[0] for p in T), max(p[0] for p in T)
    y0, y1 = min(p[1] for p in T), max(p[1] for p in T)
    c0 = math.floor((x0 - model.ox) / model.cell + e)
    c1 = math.floor((x1 - model.ox) / model.cell - e)
    r0 = math.floor((y0 - model.oy) / model.cell + e)
    r1 = math.floor((y1 - model.oy) / model.cell - e)
    return [(r, c) for r in range(r0, max(r0, r1) + 1) for c in range(c0, max(c0, c1) + 1)]


def _pieces(model, triangles, owners):
    """Clip to cells: {manager index: [(corners, owner mesh or None)]}."""
    out = defaultdict(list)
    for T, own in zip(triangles, owners):
        T = tuple(tuple(map(float, p[:3])) for p in T)
        if abs(_area2(*T)) / 2 < MIN_AREA:
            continue
        cells = _cells_of(model, T)
        if len(cells) == 1:
            r, c = cells[0]
            out[r * model.cols + c].append((_ccw(T), own))
            continue
        for r, c in cells:
            x0, y0 = model.ox + c * model.cell, model.oy + r * model.cell
            sq = [(x0, y0, 0), (x0 + model.cell, y0, 0), (x0 + model.cell, y0 + model.cell, 0), (x0, y0 + model.cell, 0)]
            poly = intersect_convex(list(_ccw(T)), sq)
            if len(poly) >= 3 and abs(_poly_area(poly)) > MIN_AREA:
                for t in _fan(poly):
                    if abs(_area2(*t)) / 2 > MIN_AREA:
                        out[r * model.cols + c].append((_ccw(t), own))
    return out


class _Edit:
    def __init__(self, model: NavModel, doc):
        self.model, self.doc = model, doc
        self.stats = defaultdict(int)
        self.gone = set()            # ids of the replaced Tri objects (every triangle of a rebuilt navmesh)
        self.oldc = {}               # id(old Tri) -> its corners (the navmesh's vertex list is replaced)
        self.succ = {}               # id(old Tri) -> the new Tri with the same corners (unchanged triangle)
        self.fresh = set()           # ids of new Tris that aren't an old triangle (the changed area)
        self.old = {}                # mesh -> its old Tri list
        self.removed_links = set()
        self.removed_wps = set()
        self.lost_seam = []          # (tri, k) of kept navmeshes whose seam was removed
        self.changed_wps = set()
        self.safe_updates = []       # (SafePosition obj, list index, edge, s): written once the edit succeeded

    def oc(self, t):
        return self.oldc.get(id(t)) or t.corners()

    # ------------------------------------------------------------ geometry --

    def assign(self, pieces):
        """{mesh: [corners]} for the navmeshes to rebuild (new navmeshes created as needed)."""
        M = self.model
        new = {}
        for cell, plist in pieces.items():
            if cell not in M.mgr:
                raise ops.EditError(f"there's no navmesh cell at {M.cell_min(cell)} (the map's navigation grid "
                                    f"doesn't reach there)")
            mgr = M.mgr[cell]
            mine = set(map(id, mgr.meshes))
            own = [p[1] if p[1] is not None and id(p[1]) in mine else None for p in plist]
            # untagged pieces join the navmesh they share an edge with (nearest through other untagged pieces)
            edges = defaultdict(list)
            for i, (T, _o) in enumerate(plist):
                ks = [_key(p) for p in T]
                for k in range(3):
                    edges[frozenset((ks[k], ks[(k + 1) % 3]))].append(i)
            nbrs = defaultdict(set)
            for lst in edges.values():
                if len(lst) < 16:
                    for i in lst:
                        nbrs[i].update(j for j in lst if j != i)
            todo = [i for i, o in enumerate(own) if o is not None]
            while todo:
                nxt = []
                for i in todo:
                    for j in nbrs[i]:
                        if own[j] is None:
                            own[j] = own[i]
                            nxt.append(j)
                todo = nxt
            for i in range(len(plist)):   # what's left: new navmeshes, one per connected group
                if own[i] is not None:
                    continue
                ms = self._new_mesh(mgr)
                own[i] = ms
                todo = [i]
                while todo:
                    nxt = []
                    for a in todo:
                        for j in nbrs[a]:
                            if own[j] is None:
                                own[j] = ms
                                nxt.append(j)
                    todo = nxt
            for (T, _o), ms in zip(plist, own):
                new.setdefault(ms, []).append(T)
        changed = {}
        for ms in list(M.meshes()):
            got = new.get(ms, [])
            if not ms.tris:   # a navmesh made by this edit
                changed[ms] = got
                continue
            old = sorted(map(sorted, (_tri_key(t.corners()) for t in ms.tris)))
            cur = sorted(map(sorted, (_tri_key(T) for T in got)))
            if old != cur:
                changed[ms] = got
        return changed

    def _new_mesh(self, mgr):
        src = mgr.meshes[0].obj if mgr.meshes else next(self.model.meshes()).obj
        obj = ops.clone_tree(self.doc, src)
        for k, v in (("Beam", 0), ("Narrow", 0), ("Active", 1), ("DirectConnectionIndex", 0xFF)):
            obj.fields[k] = int(v).to_bytes(len(obj.fields[k]), "little")
        obj.fields["Triangles"] = []
        ms = Mesh(mgr, obj)
        ms.dirty = True
        mgr.meshes.append(ms)
        self.stats["navmeshes_added"] += 1
        return ms

    def rebuild(self, ms, tris):
        """New vertices / triangles for `ms`. Triangles with an old triangle's corners take its place: its object,
        corner order, neighbours (between two such), edge codes and jump / climb / drop links; the rest are fresh,
        joined to whatever shares their edges."""
        olds = list(ms.tris)
        self.old[ms] = olds
        for t in olds:
            self.gone.add(id(t))
            self.oldc[id(t)] = t.corners()
        reuse = {}
        for t in olds:
            reuse.setdefault(_tri_key(self.oldc[id(t)]), []).append(t)
        tmpl = olds[0].obj if olds else _any_tri_obj(self.model)
        vid, verts, out = {}, [], []
        src = {}
        for T in tris:
            cand = reuse.get(_tri_key(T))
            o = cand.pop() if cand else None
            if o is not None:
                T = self.oldc[id(o)]   # the old corner order (edge k stays edge k)
            ix = []
            for p in T:
                k = _key(p)
                if k not in vid:
                    vid[k] = len(verts)
                    verts.append(p)
                ix.append(vid[k])
            if len(set(ix)) < 3:
                continue
            nt = Tri(ms, ix, [None, None, None], o.obj if o is not None else copy.deepcopy(tmpl))
            if o is not None:
                src[id(nt)] = o
                self.succ[id(o)] = nt
            else:
                self.fresh.add(id(nt))
            out.append(nt)
        edge = {}
        for t in out:
            for k in range(3):
                edge.setdefault(frozenset((t.v[k], t.v[(k + 1) % 3])), []).append((t, k))
        for lst in edge.values():
            for i in range(len(lst)):
                a, ka = lst[i]
                for b, kb in lst[i + 1:]:
                    if a is b or a.nbr[ka] is not None or b.nbr[kb] is not None:
                        continue
                    sa, sb = src.get(id(a)), src.get(id(b))
                    if sa is not None and sb is not None and sa.nbr[ka] is not sb:
                        continue   # retail kept these apart (e.g. a ledge over a zero gap): so do we
                    a.nbr[ka], b.nbr[kb] = b, a
        for t in out:   # unchanged triangles: their edges' links (retail has some on inner edges too) and codes
            o = src.get(id(t))
            if o is None:
                continue
            for k in range(3):
                t.links[k] = [ln for ln in o.links[k] if ln.type != SEAM]
                if t.nbr[k] is None and not isinstance(o.nbr[k], Tri):
                    t.nbr[k] = None if o.nbr[k] == SEAMED else o.nbr[k]
            t.wps = list(o.wps)
        ms.verts, ms.tris, ms.dirty = verts, out, True
        self.stats["triangles"] += len(out)
        self.stats["triangles_changed"] += sum(1 for t in out if id(t) in self.fresh)

    # ------------------------------------------------------------ links --

    def boundary(self, meshes, fresh_only=False):
        return [(t, k) for ms in meshes for t in ms.tris for k in range(3)
                if not isinstance(t.nbr[k], Tri) and (not fresh_only or t.nbr[k] is None)]

    def remap_links(self, rebuilt):
        """Seams on rebuilt triangles go (made anew later); jump / climb / drop links follow unchanged triangles
        and move to the changed triangles that still have their edge."""
        by_mgr = defaultdict(list)
        for t, k in self.boundary(rebuilt, fresh_only=True):
            if id(t) in self.fresh:
                by_mgr[t.mesh.mgr.idx].append((t, k))
        for ln in list(self.model.all_links()):
            if ln.type == FLOW or not any(id(t) in self.gone for t in ln.tris):
                continue
            if ln.type == SEAM:
                for t in ln.tris:
                    if id(t) not in self.gone:
                        for k in range(3):
                            if ln in t.links[k]:
                                self.lost_seam.append((t, k))
                self._drop_link(ln)
                continue
            self._move_link(ln, by_mgr)

    def _move_link(self, ln, by_mgr):
        old_list = list(ln.tris)
        repl = {}
        for t in old_list:
            if id(t) not in self.gone or id(t) in repl:
                continue
            if id(t) in self.succ:
                repl[id(t)] = [self.succ[id(t)]]
                continue
            got = []
            T = self.oc(t)
            ks = [k for k in range(3) if ln in t.links[k]]
            for k in ks:
                a, b = T[k], T[(k + 1) % 3]
                for m in _block(self.model, t.mesh.mgr.idx):
                    for nt, nk in by_mgr.get(m, ()):
                        C = nt.corners()
                        c, d = C[nk], C[(nk + 1) % 3]
                        if (b[0] - a[0]) * (d[0] - c[0]) + (b[1] - a[1]) * (d[1] - c[1]) <= 0:
                            continue   # the other side of the edge (walkable to the right of it)
                        if _overlap(a, b, c, d, dz=0.05) > 0:   # the same surface
                            if ln not in nt.links[nk]:
                                nt.links[nk].append(ln)
                            if nt not in got:
                                got.append(nt)
            if not ks:   # listed without an edge (e.g. the landing area): whatever covers its middle
                c = tuple(sum(p[i] for p in T) / 3 for i in range(3))
                nt = Geo1.get(self.model).tri_at(c, 1.0)
                if nt is not None:
                    got.append(nt)
            repl[id(t)] = got
        new_list = []
        nnis = 0
        for i, t in enumerate(old_list):
            for nt in (repl[id(t)] if id(t) in self.gone else [t]):
                if nt not in new_list:
                    new_list.append(nt)
                    nnis += i < ln.nnis
        a_new, b_new = new_list[:nnis], new_list[nnis:]
        if not b_new or (ln.nnis > 0 and not a_new):
            self._drop_link(ln)
            return
        # SafePositions: (edge, list index, s/63 along the edge) -- carried to the new list
        for sp in ln.obj.fields["SafePositions"]:
            e = sp.fields["EdgeIndex"][0]
            ti = sp.fields["TriangleIndex"][0]
            if ti == 0xFF or ti >= len(old_list):
                continue
            t = old_list[ti]
            nt = self.succ.get(id(t)) if id(t) in self.gone else t
            if nt is not None and nt in new_list:
                self.safe_updates.append((sp, new_list.index(nt), None, None))
                continue
            if e > 2:
                self._drop_link(ln)
                return
            T = self.oc(t)
            s = sp.fields["FixedSFactor"][0] / 63
            P = tuple(T[e][j] + (T[(e + 1) % 3][j] - T[e][j]) * s for j in range(3))
            hit = None
            for ni, nt in enumerate(new_list):
                C = nt.corners()
                for k in range(3):
                    if ln in nt.links[k]:
                        dd, ss = _seg_dist2d(P, C[k], C[(k + 1) % 3])
                        if dd < 0.05:
                            hit = (ni, k, ss)
                            break
                if hit:
                    break
            if hit is None:
                self._drop_link(ln)
                return
            self.safe_updates.append((sp, hit[0], hit[1], max(0, min(63, round(hit[2] * 63)))))
        if new_list != [self.succ.get(id(t), t) for t in old_list]:
            self.stats["links_moved"] += 1
        ln.tris, ln.nnis = new_list, nnis

    def _drop_link(self, ln):
        if ln in self.removed_links:
            return
        self.removed_links.add(ln)
        ln.mgr.links.remove(ln)
        self.stats[f"links_removed_type{ln.type}"] += 1

    def make_seams(self, rebuilt):
        """Seams for the rebuilt edges without one: every other boundary edge lying along them."""
        M = self.model
        mine = self.boundary(rebuilt, fresh_only=True)
        if not mine:
            return
        cand = defaultdict(list)
        for ms in M.meshes():
            for t in ms.tris:
                for k in range(3):
                    if not isinstance(t.nbr[k], Tri):
                        cand[ms.mgr.idx].append((t, k))
        pairs = defaultdict(list)
        done = set()
        for t, k in mine:
            T = t.corners()
            a, b = T[k], T[(k + 1) % 3]
            for m in _block(M, t.mesh.mgr.idx):
                for u, j in cand.get(m, ()):
                    if u is t and j == k:
                        continue
                    if (id(u), j, id(t), k) in done:
                        continue
                    if any(ln.type != SEAM and ln in u.links[j] for ln in t.links[k]):
                        continue   # retail joins these with a step / jump link, not a seam
                    C = u.corners()
                    if _overlap(a, b, C[j], C[(j + 1) % 3]) > 0:
                        done.add((id(t), k, id(u), j))
                        key = (t.mesh, u.mesh) if _mesh_order(t.mesh) <= _mesh_order(u.mesh) else (u.mesh, t.mesh)
                        pairs[key].append(((t, k), (u, j)))
        tmpl = next((ln.obj for ln in M.all_links() if ln.type == SEAM), None)
        if pairs and tmpl is None:
            raise ops.EditError("this map has no seam metalink to copy")
        for (ma, mb), lst in pairs.items():
            holder = ma.mgr if ma.mgr.idx <= mb.mgr.idx else mb.mgr
            first = ma if ma.mgr is holder else mb
            ln = Link(holder, copy.deepcopy(tmpl))
            ln.type = SEAM
            sa, sb = [], []
            for (t, k), (u, j) in lst:
                if ma is mb:
                    pairs_ = (((t, k), sa), ((u, j), sb))
                else:
                    pairs_ = (((t, k), sa if t.mesh is first else sb), ((u, j), sa if u.mesh is first else sb))
                for (tri, kk), side in pairs_:
                    if tri not in sa and tri not in sb:
                        side.append(tri)
                    if ln not in tri.links[kk]:
                        tri.links[kk].append(ln)
                    tri.nbr[kk] = SEAMED
            if not sa or not sb:
                continue
            ln.tris, ln.nnis = sa + sb, len(sa)
            holder.links.append(ln)
            self.stats["seams_added"] += 1

    def edge_codes(self, rebuilt):
        """Codes for changed boundary edges without a seam, and kept edges that lost theirs."""
        old_edges = defaultdict(list)
        for ms, olds in self.old.items():
            for t in olds:
                T = self.oc(t)
                for k in range(3):
                    if not isinstance(t.nbr[k], Tri):
                        old_edges[ms.mgr.idx].append((T[k], T[(k + 1) % 3], t.nbr[k]))
        oldgeo = _OldGeo(self)
        geo = Geo1.get(self.model)
        for t, k in self.boundary(rebuilt, fresh_only=True):
            T = t.corners()
            a, b = T[k], T[(k + 1) % 3]
            code = None
            if id(t) in self.fresh:
                for oa, ob, oc in old_edges.get(t.mesh.mgr.idx, ()):
                    if oc != SEAMED and _overlap(a, b, oa, ob) > 0:
                        code = oc
                        break
            if code is None:
                code = self._probe(a, b, geo, oldgeo)
            t.nbr[k] = code
        for t, k in self.lost_seam:
            if id(t) in self.gone:
                continue
            if not any(ln.type == SEAM and ln not in self.removed_links for ln in t.links[k]):
                t.nbr[k] = WALL   # the ground beyond was taken away: an obstacle now

    @staticmethod
    def _probe(a, b, geo, oldgeo):
        mx, my, mz = ((a[i] + b[i]) / 2 for i in range(3))
        ex, ey = b[0] - a[0], b[1] - a[1]
        L = math.hypot(ex, ey) or 1.0
        nx, ny = ey / L, -ex / L          # right of a -> b: outside a CCW triangle
        p = (mx + nx * 0.6, my + ny * 0.6, mz)
        if any(abs(z - mz) < 0.5 for z in oldgeo.heights(p)):
            return WALL   # walkable before: the edit put an obstacle there
        if any(z - mz > -0.5 for z in geo.heights(p)):
            return WALL
        return LEDGE

    # ------------------------------------------------------------ flows --

    def remap_flows(self):
        from .flowedit import world_points
        geo = Geo1.get(self.model)
        bad, moved = [], {}
        for f in self.model.flows:
            idx = [i for i, t in enumerate(f.tris) if id(t) in self.gone]
            if not idx:
                continue
            pts = None
            for i in idx:
                nt = self.succ.get(id(f.tris[i]))
                if nt is None:
                    if pts is None:
                        pts = world_points(self.doc.obj(f.uid))
                    nt = geo.tri_at(pts[i], 1.5)
                    if nt is None:
                        bad.append((f.uid, i))
                        continue
                    moved.setdefault(f, []).append((i, pts[i]))
                f.tris[i] = nt
                if f.link is not None and i < len(f.link.tris):
                    f.link.tris[i] = nt
        if bad:
            names = sorted({self.doc.name_of(u) for u, _i in bad})
            raise ops.EditError("crowd flow points would be off the navmesh: " + ", ".join(names[:6]) +
                                (" ..." if len(names) > 6 else "") + " -- move those flows first")
        self.flow_points = moved
        self.stats["flow_points_moved"] = sum(len(v) for v in moved.values())

    # ------------------------------------------------------------ waypoints --

    def remap_waypoints(self):
        M = self.model
        geo = Geo1.get(M)
        for w in list(M.all_wps()):
            if w.tri is None or id(w.tri) not in self.gone:
                continue
            nt = self.succ.get(id(w.tri))
            if nt is None:
                nt = geo.tri_at(M.wp_pos(w), 1.0)
                if nt is None:
                    self.removed_wps.add(w)
                    continue
                self.changed_wps.add(w)
            w.tri = nt
        self.stats["waypoints_removed"] = len(self.removed_wps)

    def corner_waypoints(self, rebuilt):
        """Waypoints just off the obstacle corners of the changed area."""
        M = self.model
        geo = Geo1.get(M)
        grid = defaultdict(list)
        for w in M.all_wps():
            if w.tri is not None:
                p = M.wp_pos(w)
                grid[(int(p[0] // 4), int(p[1] // 4))].append(p)
        tmpl = next((w.obj for w in M.all_wps() if w.tri is not None), None)
        if tmpl is None:
            return
        for ms in rebuilt:
            out_e, in_e = defaultdict(list), defaultdict(list)
            touched = set()
            for t in ms.tris:
                for k in range(3):
                    if t.nbr[k] in (LEDGE, WALL):
                        a, b = t.v[k], t.v[(k + 1) % 3]
                        out_e[a].append(b)
                        in_e[b].append(a)
                        if id(t) in self.fresh:
                            touched.update((a, b))
            for v in touched:
                outs = out_e.get(v, ())
                if len(outs) != 1 or len(in_e[v]) != 1:
                    continue
                A, V, B = ms.verts[in_e[v][0]], ms.verts[v], ms.verts[outs[0]]
                d1 = (V[0] - A[0], V[1] - A[1])
                d2 = (B[0] - V[0], B[1] - V[1])
                l1, l2 = math.hypot(*d1), math.hypot(*d2)
                if l1 < 1e-6 or l2 < 1e-6:
                    continue
                cross = d1[0] * d2[1] - d1[1] * d2[0]
                turn = math.atan2(cross, d1[0] * d2[0] + d1[1] * d2[1])
                if turn > -CORNER_TURN:   # walkable on the left: a right turn is an obstacle corner
                    continue
                n1 = (-d1[1] / l1, d1[0] / l1)
                n2 = (-d2[1] / l2, d2[0] / l2)
                bx, by = n1[0] + n2[0], n1[1] + n2[1]
                bl = math.hypot(bx, by)
                if bl < 1e-6:
                    continue
                bx, by = bx / bl, by / bl
                off = CORNER_OFFSET / max(bx * n1[0] + by * n1[1], 0.25)
                p = (V[0] + bx * off, V[1] + by * off, V[2])
                t = geo.tri_at(p, 1.0)
                if t is None:
                    continue
                p = (p[0], p[1], z_on(t.corners(), p))
                g = (int(p[0] // 4), int(p[1] // 4))
                if any(math.dist(p, q) < CORNER_SPACING for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                       for q in grid[(g[0] + dx, g[1] + dy)]):
                    continue
                obj = copy.deepcopy(tmpl)
                obj.fields["Active"] = b"\x01"
                obj.fields["JumpPoint"] = b"\x00"
                obj.custom = dict(obj.custom, Links=[], Flags=4)
                w = WayPoint(t.mesh.mgr, obj)
                w.hi = 0
                w.tri = t
                M.set_wp_pos(w, p)
                t.mesh.mgr.wps.append(w)
                grid[g].append(p)
                self.changed_wps.add(w)
                self.stats["waypoints_added"] += 1

    def purge(self):
        """Drop every reference to removed links / waypoints, and empty navmeshes."""
        M = self.model
        rl, rw = self.removed_links, self.removed_wps
        for w in rw:
            w.mgr.wps.remove(w)
        for ms in M.meshes():
            for t in ms.tris:
                for k in range(3):
                    if any(ln in rl for ln in t.links[k]):
                        t.links[k] = [ln for ln in t.links[k] if ln not in rl]
                if any(w in rw for w in t.wps):
                    t.wps = [w for w in t.wps if w not in rw]
        for w in M.all_wps():
            if any(x in rl or x in rw for _d, x in w.links):
                before = len(w.links)
                w.links = [(d, x) for d, x in w.links if x not in rl and x not in rw]
                if len(w.links) < before and w.tri is not None:
                    self.changed_wps.add(w)   # lost a neighbour: look for others
        for ln in M.all_links():
            if ln.owps:
                ln.owps = [(o, w, [c for c in cs if c not in rw]) for o, w, cs in ln.owps if w not in rw]
        for ms in [ms for ms in M.meshes() if not ms.tris]:
            ms.mgr.meshes.remove(ms)
            self.stats["navmeshes_removed"] += 1

    def relink(self, rebuilt, boxes):
        """Cut waypoint links over removed ground, link new / moved waypoints, list 3 of changed triangles, and the
        connections of flow points on changed triangles."""
        M = self.model
        geo = Geo1.get(M)
        net = [(w, M.wp_pos(w)) for w in M.all_wps() if w.tri is not None and w.active]
        pos = dict(net)
        oldgeo = _OldGeo(self)

        def crosses(p, q):
            x0, x1, y0, y1 = min(p[0], q[0]), max(p[0], q[0]), min(p[1], q[1]), max(p[1], q[1])
            return any(not (x1 < bx0 or x0 > bx1 or y1 < by0 or y0 > by1) for bx0, by0, bx1, by1 in boxes)

        def over_removed(p, q):
            # retail links aren't all straight walks over the navmesh (stairs, steps): cut one only where it runs
            # over ground this edit took away
            n = max(2, int(math.dist(p[:2], q[:2]) / 0.5))
            for i in range(1, n):
                s = i / n
                x = tuple(p[j] + (q[j] - p[j]) * s for j in range(3))
                if not any(bx0 <= x[0] <= bx1 and by0 <= x[1] <= by1 for bx0, by0, bx1, by1 in boxes):
                    continue
                if any(abs(z - x[2]) < 1.0 for z in oldgeo.heights(x)) and \
                        not any(abs(z - x[2]) < 1.0 for z in geo.heights(x)):
                    return True
            return False

        cut = {}
        for w, p in net:
            for _d, x in w.links:
                if isinstance(x, WayPoint) and x in pos and (id(x), id(w)) not in cut and crosses(p, pos[x]):
                    cut[(id(w), id(x))] = over_removed(p, pos[x])
        for w, _p in net:
            if any(cut.get((id(w), id(x))) or cut.get((id(x), id(w))) for _d, x in w.links):
                w.links = [(d, x) for d, x in w.links if not (cut.get((id(w), id(x))) or cut.get((id(x), id(w))))]
                self.changed_wps.add(w)
        self.stats["links_cut"] = sum(1 for v in cut.values() if v)
        for w in sorted(self.changed_wps, key=lambda w: (w.mgr.idx, M.wp_pos(w))):
            p = pos.get(w)
            if p is None:
                continue
            have = {id(x) for _d, x in w.links}
            cands = [(x, q) for x, q in net if x is not w and id(x) not in have and
                     M.rel_of(w.mgr.idx, x.mgr.idx) is not None and M.rel_of(x.mgr.idx, w.mgr.idx) is not None]
            for x in geo.visible(p, w.tri, cands, LINK_RADIUS, LINK_LIMIT):
                if len(w.links) >= 255 or len(x.links) >= 255:
                    break
                d = max(4, round(math.dist(p, pos[x]) * 128) * 4)
                w.links.append((d, x))
                x.links.append((d, w))
                self.stats["links_added"] += 1
        for ms in rebuilt:
            for t in ms.tris:
                if id(t) not in self.fresh:
                    continue
                T = t.corners()
                c = tuple(sum(v[i] for v in T) / 3 for i in range(3))
                own = [w for w, _p in net if w.tri is t]
                cands = [(x, q) for x, q in net if x.tri is not t and M.rel_of(ms.mgr.idx, x.mgr.idx) is not None]
                t.wps = own + geo.visible(c, t, cands, LIST3_RADIUS, max(0, LIST3_LIMIT - len(own)))
        for f, lst in getattr(self, "flow_points", {}).items():
            if f.link is None:
                continue
            for i, p in lst:
                if i < len(f.link.owps):
                    o, w, _c = f.link.owps[i]
                    f.link.owps[i] = (o, w, geo.visible(p, f.tris[i], net, 60.0, 32, corridor=0.4))


def _mesh_order(ms):
    return (ms.mgr.idx, ms.mgr.meshes.index(ms))


def _block(model, m):
    return [m + dx + dy * model.cols for dx, dy in REL if model.rel_of(m, m + dx + dy * model.cols) is not None]


class Geo1:
    """The Geo of a model's current triangles, kept on the model (rebuilt when an edit changes them)."""

    @staticmethod
    def get(model):
        g = getattr(model, "_geo", None)
        if g is None:
            g = model._geo = Geo(model)
        return g

    @staticmethod
    def reset(model):
        model._geo = None


class _OldGeo:
    """The triangles an edit took away (old triangles nothing unchanged replaced), for "was this walkable?"."""

    def __init__(self, ed):
        self.cells = defaultdict(list)
        for olds in ed.old.values():
            for t in olds:
                if id(t) in ed.succ:
                    continue
                T = ed.oc(t)
                x0, x1 = min(p[0] for p in T), max(p[0] for p in T)
                y0, y1 = min(p[1] for p in T), max(p[1] for p in T)
                for gx in range(int(x0 // 4), int(x1 // 4) + 1):
                    for gy in range(int(y0 // 4), int(y1 // 4) + 1):
                        self.cells[(gx, gy)].append(T)

    def heights(self, p):
        return [z_on(T, p) for T in self.cells.get((int(p[0] // 4), int(p[1] // 4)), ()) if inside2d(T, p)]


def _any_tri_obj(model):
    for ms in model.meshes():
        if ms.tris:
            return ms.tris[0].obj
    raise ops.EditError("this map has no navmesh triangle to copy")


def _seam_obj(model):
    raise ops.EditError("this map has no seam metalink to copy")


def apply_triangles(model: NavModel, doc, triangles, owners=None) -> dict:
    """Make the map's walkable triangles `triangles` (world corners), `owners` = the navmesh each came from (None:
    new). Returns stats. Raises ops.EditError with the document untouched -- but the model is spoiled: load a new
    NavModel after an error."""
    if owners is None:
        owners = [None] * len(triangles)
    ed = _Edit(model, doc)
    Geo1.reset(model)
    changed = ed.assign(_pieces(model, triangles, owners))
    if not changed:
        return {"navmeshes_rebuilt": 0}
    for ms, tris in changed.items():
        ed.rebuild(ms, tris)
    boxes = []   # the changed area: fresh triangles, and old ones nothing replaced
    for T in [t.corners() for ms in changed for t in ms.tris if id(t) in ed.fresh] + \
             [ed.oc(t) for olds in ed.old.values() for t in olds if id(t) not in ed.succ]:
        boxes.append((min(p[0] for p in T) - 0.5, min(p[1] for p in T) - 0.5,
                      max(p[0] for p in T) + 0.5, max(p[1] for p in T) + 0.5))
    rebuilt = [ms for ms in changed if ms.tris]
    ed.stats["navmeshes_rebuilt"] = len(changed)
    Geo1.reset(model)
    ed.remap_links(rebuilt)
    ed.make_seams(rebuilt)
    ed.edge_codes(rebuilt)
    ed.remap_flows()
    ed.remap_waypoints()
    ed.purge()
    Geo1.reset(model)
    ed.corner_waypoints(rebuilt)
    ed.relink(rebuilt, boxes)
    Geo1.reset(model)
    problems = check(model)
    if problems:
        raise ops.EditError("navmesh edit left broken data: " + "; ".join(problems[:5]))
    for sp, ti, e, sf in ed.safe_updates:
        sp.fields["TriangleIndex"] = bytes([ti])
        if e is not None:
            sp.fields["EdgeIndex"] = bytes([e])
            sp.fields["FixedSFactor"] = bytes([sf])
    for u in model.store():
        doc.touch(u)
    return dict(ed.stats)


# ---------------------------------------------------------------- footprints --

def _cut(model, polys, keep):
    """Every triangle with the parts inside the convex 2D polygons `polys` removed where keep(T, piece) is false."""
    tris, owners = [], []
    for T, ms in export_triangles(model):
        pieces = [list(T)]
        for poly in polys:
            x0, x1 = min(p[0] for p in poly), max(p[0] for p in poly)
            y0, y1 = min(p[1] for p in poly), max(p[1] for p in poly)
            nxt = []
            for pc in pieces:
                if (max(p[0] for p in pc) < x0 or min(p[0] for p in pc) > x1 or
                        max(p[1] for p in pc) < y0 or min(p[1] for p in pc) > y1):
                    nxt.append(pc)
                    continue
                inside = intersect_convex(pc, poly)
                if len(inside) < 3 or abs(_poly_area(inside)) <= MIN_AREA or keep(inside, poly):
                    nxt.append(pc)
                    continue
                nxt += subtract_convex(pc, poly)
            pieces = nxt
        if len(pieces) == 1 and pieces[0] == list(T):
            tris.append(T)
            owners.append(ms)
            continue
        for pc in pieces:
            for t in _fan(pc):
                tris.append(t)
                owners.append(ms)
    return tris, owners


def footprint(triangles):
    """Convex 2D pieces (with their 3D corners) of a surface's triangles that face up or down."""
    out = []
    for T in triangles:
        T = [tuple(map(float, p[:3])) for p in T]
        if abs(_area2(*T)) / 2 > MIN_AREA:
            out.append(list(_ccw(tuple(T))))
    return out


def block_area(model: NavModel, doc, triangles, below=1.0, above=0.3) -> dict:
    """Remove the walkable ground under an obstacle: every navmesh part inside the footprint of `triangles` (an
    object's faces, world space) whose height is within the obstacle's height range (from `below` m under its
    lowest point to `above` m over its highest)."""
    polys = footprint(triangles)
    if not polys:
        raise ops.EditError("the object has no faces seen from above")
    z0 = min(p[2] for T in triangles for p in T) - below
    z1 = max(p[2] for T in triangles for p in T) + above
    tris, owners = _cut(model, polys, keep=lambda piece, poly: not any(z0 <= p[2] <= z1 for p in piece))
    return apply_triangles(model, doc, tris, owners)


def add_surface(model: NavModel, doc, triangles, solid=True, max_slope=45.0) -> dict:
    """Make the faces `triangles` (world space) walkable: their upward faces become navmesh, the navmesh they
    cover at their height is replaced (so the two seam together along its edges), and with `solid` the navmesh
    under them (down to 3 m) goes too -- NPCs won't walk through a solid block."""
    cmax = math.cos(math.radians(max_slope))
    up = []
    for T in triangles:
        T = [tuple(map(float, p[:3])) for p in T]
        ux, uy, uz = (T[1][i] - T[0][i] for i in range(3))
        vx, vy, vz = (T[2][i] - T[0][i] for i in range(3))
        n = (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)
        L = math.sqrt(sum(c * c for c in n))
        if L > 1e-9 and n[2] / L >= cmax:
            up.append(tuple(T))
    if not up:
        raise ops.EditError(f"the object has no faces facing up (within {max_slope:.0f} degrees of flat)")
    polys = [list(_ccw(T)) for T in up]

    def keep(piece, poly):
        # the cut polygon's plane: z of the new surface at a point
        A, B, C = poly[0], poly[1], poly[2]
        for p in piece:
            zs = z_on((A, B, C), p)
            if zs - (3.0 if solid else 0.6) <= p[2] <= zs + 0.6:
                return False
        return True

    tris, owners = _cut(model, polys, keep)
    tris += up
    owners += [None] * len(up)
    return apply_triangles(model, doc, tris, owners)


# ---------------------------------------------------------------- checks --

def check(model: NavModel) -> list[str]:
    """Structural problems of the model (empty = consistent)."""
    out = []
    M = model
    live_links = {id(ln) for ln in M.all_links()}
    live_wps = {id(w) for w in M.all_wps()}
    live_tris = set()
    for ms in M.meshes():
        for t in ms.tris:
            live_tris.add(id(t))
    for ms in M.meshes():
        if not ms.tris:
            out.append(f"empty navmesh in manager {ms.mgr.idx}")
        for t in ms.tris:
            for k in range(3):
                nb = t.nbr[k]
                if isinstance(nb, Tri):
                    if nb.mesh is not ms or t not in nb.nbr:
                        out.append(f"triangle neighbour not reciprocal in manager {ms.mgr.idx}")
                elif nb not in (LEDGE, SEAMED, WALL):
                    out.append(f"edge code {nb!r} in manager {ms.mgr.idx}")
                elif nb == SEAMED and not any(ln.type == SEAM for ln in t.links[k]):
                    out.append(f"seamed edge without a seam in manager {ms.mgr.idx}")
                for ln in t.links[k]:
                    if id(ln) not in live_links:
                        out.append("edge list names a removed metalink")
                    elif M.rel_of(ms.mgr.idx, ln.mgr.idx) is None:
                        out.append("edge metalink outside the 3x3 block")
            for w in t.wps:
                if id(w) not in live_wps:
                    out.append("triangle lists a removed waypoint")
    for ln in M.all_links():
        for t in ln.tris:
            if id(t) not in live_tris:
                out.append(f"metalink (type {ln.type}) in manager {ln.mgr.idx} names a replaced triangle")
                break
        for _o, w, cs in ln.owps:
            if id(w) not in live_wps or any(id(c) not in live_wps for c in cs):
                out.append("object waypoint names a removed waypoint")
    for w in M.all_wps():
        if w.tri is not None and id(w.tri) not in live_tris:
            out.append(f"waypoint in manager {w.mgr.idx} on a replaced triangle")
        for d, x in w.links:
            if id(x) not in (live_links if d == METALINK_DIST else live_wps):
                out.append("waypoint link to a removed element")
    for f in M.flows:
        if f.link is not None and id(f.link) not in live_links:
            out.append("crowd flow metalink removed")
        if any(id(t) not in live_tris for t in f.tris):
            out.append("crowd flow names a replaced triangle")
    out += edge_link_problems(model)
    return sorted(set(out))


def edge_link_problems(model: NavModel) -> list[str]:
    """Jump / climb / drop metalinks whose listed triangles don't name them on any edge, though retail's did.
    (Retail lists a few landing triangles without an edge, so only a link that names none of its triangles counts.)"""
    out = []
    for ln in model.all_links():
        if ln.type in (SEAM, FLOW) or not ln.tris:
            continue
        if not any(ln in t.links[k] for t in ln.tris for k in range(3)):
            out.append(f"metalink type {ln.type} in manager {ln.mgr.idx} is on no triangle edge")
    return out
