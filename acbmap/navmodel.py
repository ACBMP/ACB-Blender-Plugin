"""The map's navigation data as linked objects, for structural edits (navedit.py).

navmesh.py reads the NavMeshManagers in place, by index. Editing the navmesh itself adds and removes triangles,
navmeshes, metalinks and waypoints, which renumbers them -- and every reference to them is an index: relative refs
inside the managers (triangle edge lists, waypoint links, waypoint triangles), absolute refs in metalinks
(TriangleList, ObjectWayPoints), DirectConnectionSets, and the CrowdFlow entities outside (MetaLinkRef,
TriangleArray, WayPointArray). NavModel turns all of them into object references on load and back into indices on
store(), so an edit only rearranges objects. Storing an unedited model rewrites every manager byte-identically.

Layout notes (see navmesh.py and ac-re-memory acb/maps/acb_navmesh_format.md):
- a triangle's custom tail: counts of four lists, then the lists: metalinks on edges 0/1/2 (relative refs) and the
  waypoints of the triangle (relative refs) -- list 3 is where pathfinding enters the waypoint network;
- NeighborTriangleIndex[k] (edge v_k -> v_k+1): a triangle of the same navmesh, or 0xffe3 (seamed to another navmesh
  by a LinkType-0 metalink on that edge), 0xffe2 (ledge), 0xffe4 (wall);
- a metalink's TriangleList is side A (NextNavMeshIndexStart entries) then side B; SafePositions name an edge of a
  TriangleList entry (TriangleIndex = index in that list, 255 = none);
- DirectConnectionSets: per navmesh, the navmeshes reachable through seams (its seam-connected component, itself
  included; 0xff when it has no seams).
"""
from __future__ import annotations

import copy
import struct

from .doc import MapDocument
from .kinds import classify, component
from .navmesh import NONE_REL, REL, h

SEAM, JUMP, FLOW, CLIMB, DROP = 0, 1, 5, 6, 7
LEDGE, SEAMED, WALL = 0xFFE2, 0xFFE3, 0xFFE4
METALINK_DIST = 2   # a waypoint link with this distance word points at a metalink


def _put(obj, key, v):
    """Set an integer field, keeping its stored width."""
    obj.fields[key] = int(v).to_bytes(len(obj.fields[key]), "little")


def _resize(lst, n, make):
    del lst[n:]
    while len(lst) < n:
        lst.append(make())
    return lst


class Tri:
    __slots__ = ("mesh", "v", "nbr", "links", "wps", "obj", "i")

    def __init__(self, mesh, v, nbr, obj=None):
        self.mesh, self.v, self.nbr, self.obj = mesh, tuple(v), list(nbr), obj
        self.links = [[], [], []]   # metalinks on each edge
        self.wps = []               # list 3
        self.i = -1

    def corners(self):
        vs = self.mesh.verts
        return [vs[k] for k in self.v]


class Mesh:
    __slots__ = ("mgr", "obj", "verts", "tris", "dirty", "set0", "i")

    def __init__(self, mgr, obj):
        self.mgr, self.obj = mgr, obj
        self.verts, self.tris = [], []
        self.dirty = False    # geometry rebuilt: vertices / triangles / MOPP / AABV get rewritten
        self.set0 = None      # DirectConnectionSet as loaded (list of meshes, stored order), None = 0xff
        self.i = -1


class Link:
    __slots__ = ("mgr", "obj", "type", "tris", "nnis", "owps", "i")

    def __init__(self, mgr, obj):
        self.mgr, self.obj = mgr, obj
        self.type = h(obj.fields["LinkType"])
        self.tris = []     # TriangleList (side A = first nnis)
        self.nnis = h(obj.fields["NextNavMeshIndexStart"])
        self.owps = []     # [(ObjectWayPoint obj, WayPoint, [network WayPoint])]
        self.i = -1

    def sides(self):
        return self.tris[:self.nnis], self.tris[self.nnis:]


class WayPoint:
    __slots__ = ("mgr", "obj", "tri", "cell", "X", "Y", "Z", "links", "hi", "i")

    def __init__(self, mgr, obj):
        self.mgr, self.obj = mgr, obj
        c = obj.custom
        self.cell, self.X, self.Y, self.Z = c["ManagerIndex"], c["X"], c["Y"], c["Z"]
        self.hi = c["Flags"] >> 4
        self.tri = None
        self.links = []    # [(distance word, WayPoint | Link)]
        self.i = -1

    @property
    def active(self):
        return self.obj.fields["Active"] != b"\0"


class Manager:
    __slots__ = ("idx", "uid", "obj", "meshes", "links", "wps", "sets_dirty")

    def __init__(self, idx, uid, obj):
        self.idx, self.uid, self.obj = idx, uid, obj
        self.meshes, self.links, self.wps = [], [], []
        self.sets_dirty = False


class Flow:
    __slots__ = ("uid", "cf", "link", "tris", "wps")

    def __init__(self, uid, cf):
        self.uid, self.cf = uid, cf
        self.link, self.tris, self.wps = None, [], []


class NavModel:
    def __init__(self, doc: MapDocument, nav=None):
        from .navmesh import NavData
        self.doc = doc
        nd = nav if nav is not None else NavData(doc)
        self.cols, self.ox, self.oy, self.cell = nd.cols, nd.ox, nd.oy, nd.cell
        self.mgr = {i: Manager(i, nd.uid_of[i], o) for i, o in sorted(nd.mgr.items())}
        self._load()

    # ------------------------------------------------------------ grid --

    def abs_manager(self, m, rel):
        if rel >= 9:
            return None
        dx, dy = REL[rel]
        return m + dx + dy * self.cols

    def rel_of(self, m, target):
        """Relative ref code of manager `target` seen from `m` (None: not in m's 3x3 block)."""
        d = target - m
        for r, (dx, dy) in enumerate(REL):
            if dx + dy * self.cols == d and 0 <= m % self.cols + dx < self.cols:
                return r
        return None

    def cell_of(self, x, y):
        import math
        return int(math.floor((y - self.oy) / self.cell)) * self.cols + int(math.floor((x - self.ox) / self.cell))

    def cell_min(self, m):
        return self.ox + (m % self.cols) * self.cell, self.oy + (m // self.cols) * self.cell

    def wp_pos(self, w):
        from .navmesh import half
        x0, y0 = self.cell_min(w.cell)
        s = (self.cell + 2) / 1024
        return (x0 - 1 + w.X * s, y0 - 1 + w.Y * s, half(w.Z))

    def set_wp_pos(self, w, p):
        from .navmesh import to_half
        m = self.cell_of(p[0], p[1])
        x0, y0 = self.cell_min(m)
        s = (self.cell + 2) / 1024
        w.cell = m
        w.X = min(max(round((p[0] - x0 + 1) / s), 0), 1023)
        w.Y = min(max(round((p[1] - y0 + 1) / s), 0), 1023)
        w.Z = to_half(p[2])

    # ------------------------------------------------------------ load --

    def _load(self):
        M = self.mgr
        for m in M.values():
            nav = m.obj.fields["NavigationMeshes"]
            for nm in nav:
                ms = Mesh(m, nm)
                ms.verts = [struct.unpack("<3f", v[:12]) for v in nm.fields["VertexList"]]
                idx = [h(x) for x in nm.fields["TriangleVertex"]]
                for t, to in enumerate(nm.fields["Triangles"]):
                    ms.tris.append(Tri(ms, idx[3 * t:3 * t + 3], [h(x) for x in to.fields["NeighborTriangleIndex"]], to))
                m.meshes.append(ms)
            m.links = [Link(m, o) for o in m.obj.fields["MetaLinks"]]
            m.wps = [WayPoint(m, o) for o in m.obj.fields["MyWayPointNetwork"].fields["WayPoints"]]
        tri = lambda mi, n, t: M[mi].meshes[n].tris[t]   # noqa: E731
        for m in M.values():
            for ms in m.meshes:
                for t in ms.tris:
                    t.nbr = [ms.tris[x] if x < 0xFFE0 else x for x in t.nbr]
                    c = t.obj.custom
                    pos = 0
                    for k in range(4):
                        refs = c["data"][pos:pos + c["counts"][k]]
                        pos += c["counts"][k]
                        for r in refs:
                            mm = M[self.abs_manager(m.idx, r & 0xF)]
                            if k < 3:
                                t.links[k].append(mm.links[r >> 4])
                            else:
                                t.wps.append(mm.wps[r >> 4])
            for ln in m.links:
                ln.tris = [tri(h(r.fields["ManagerIndex"]), h(r.fields["NavMeshIndex"]), h(r.fields["TriangleIndex"]))
                           for r in ln.obj.fields["TriangleList"]]
                for ow in ln.obj.fields["ObjectWayPoints"]:
                    r = ow.fields["WayPoint"]
                    w = M[h(r.fields["ManagerIndex"])].wps[h(r.fields["WayPointIndex"])]
                    ln.owps.append((ow, w, [M[mi].wps[wi] for wi, mi in ow.custom]))
            for w in m.wps:
                c = w.obj.custom
                rel = c["Flags"] & 0xF
                if rel != NONE_REL and c["TriangleIndex"] != 0xFFFF:
                    w.tri = tri(self.abs_manager(m.idx, rel), c["Bits"], c["TriangleIndex"])
                for dist, r in c["Links"]:
                    mm = M[self.abs_manager(m.idx, r & 0xF)]
                    w.links.append((dist, mm.links[r >> 4] if dist == METALINK_DIST else mm.wps[r >> 4]))
            # DirectConnectionSets as loaded
            refs = [(h(r.fields["ManagerIndex"]), h(r.fields["NavMeshIndex"])) for r in m.obj.fields["DirectConnectionSets"]]
            start = [h(x) for x in m.obj.fields["DirectConnectionSetIndex"]]
            size = [h(x) for x in m.obj.fields["DirectConnectionSetSize"]]
            for ms in m.meshes:
                k = h(ms.obj.fields["DirectConnectionIndex"])
                if k != 0xFF:
                    ms.set0 = [M[a].meshes[b] for a, b in refs[start[k]:start[k] + size[k]]]
        self.flows = []
        for e in classify(self.doc, with_children=False):
            if e.kind not in ("crowd_flow", "nav_flow"):
                continue
            cf = component(e.obj, "CrowdFlow") or component(e.obj, "NavFlow")
            if cf is None or "MetaLinkRef" not in cf.fields:
                continue
            f = Flow(e.uid, cf)
            r = cf.fields["MetaLinkRef"]
            mi = h(r.fields["ManagerIndex"])
            if mi in M:
                f.link = M[mi].links[h(r.fields["MetaLinkIndex"])]
            f.tris = [tri(h(t.fields["ManagerIndex"]), h(t.fields["NavMeshIndex"]), h(t.fields["TriangleIndex"]))
                      for t in cf.fields["TriangleArray"]]
            f.wps = [M[h(w.fields["ManagerIndex"])].wps[h(w.fields["WayPointIndex"])] for w in cf.fields["WayPointArray"]]
            self.flows.append(f)

    # ------------------------------------------------------------ queries --

    def meshes(self):
        for m in self.mgr.values():
            yield from m.meshes

    def all_links(self):
        for m in self.mgr.values():
            yield from m.links

    def all_wps(self):
        for m in self.mgr.values():
            yield from m.wps

    def seam_components(self):
        """{mesh: frozenset of meshes reachable through seams} for every mesh with at least one seam."""
        adj = {}
        for ln in self.all_links():
            if ln.type != SEAM:
                continue
            a, b = ln.sides()
            for x in {t.mesh for t in a}:
                for y in {t.mesh for t in b}:   # a seam can join a navmesh to itself: it still gets a set
                    adj.setdefault(x, set()).add(y)
                    adj.setdefault(y, set()).add(x)
        comp = {}
        for s in adj:
            if s in comp:
                continue
            seen, todo = {s}, [s]
            while todo:
                for y in adj[todo.pop()]:
                    if y not in seen:
                        seen.add(y)
                        todo.append(y)
            fs = frozenset(seen)
            for x in seen:
                comp[x] = fs
        return comp

    # ------------------------------------------------------------ store --

    def _ref(self, holder, target_mgr, index, what):
        r = self.rel_of(holder.idx, target_mgr.idx)
        if r is None:
            raise ValueError(f"{what}: manager {target_mgr.idx} isn't next to {holder.idx}")
        if index >= 0x1000:
            raise ValueError(f"{what}: index {index} doesn't fit a relative ref")
        return r | (index << 4)

    def store(self):
        """Write the model back into the manager objects and the flow entities; returns the uids changed (all
        managers, the flows whose refs moved)."""
        for m in self.mgr.values():
            for n, ms in enumerate(m.meshes):
                ms.i = n
                for t, tr in enumerate(ms.tris):
                    tr.i = t
            for i, ln in enumerate(m.links):
                ln.i = i
            for i, w in enumerate(m.wps):
                w.i = i
        comp = self.seam_components()
        for m in self.mgr.values():
            self._store_manager(m, comp)
        touched = {m.uid for m in self.mgr.values()}
        for f in self.flows:
            if self._store_flow(f):
                touched.add(f.uid)
        return touched

    def _store_manager(self, m, comp):
        mo = m.obj
        mo.fields["NavigationMeshes"] = [ms.obj for ms in m.meshes]
        for ms in m.meshes:
            nm = ms.obj
            if ms.dirty:
                self._store_geometry(ms)
            for tr in ms.tris:
                to = tr.obj
                _put(to, "TriangleIndex", tr.i)
                nb = to.fields["NeighborTriangleIndex"]
                for k in range(3):
                    x = tr.nbr[k]
                    nb[k] = int(x.i if isinstance(x, Tri) else x).to_bytes(len(nb[k]), "little")
                lists = [[self._ref(m, ln.mgr, ln.i, "triangle edge metalink") for ln in tr.links[k]] for k in range(3)]
                lists.append([self._ref(m, w.mgr, w.i, "triangle waypoint") for w in tr.wps])
                if any(len(x) > 255 for x in lists):
                    raise ValueError("a triangle list is too long")
                to.custom = {"counts": [len(x) for x in lists], "data": [r for x in lists for r in x]}
            nm.fields["Triangles"] = [tr.obj for tr in ms.tris]
        self._store_sets(m, comp)
        mo.fields["MetaLinks"] = [ln.obj for ln in m.links]
        for ln in m.links:
            lo = ln.obj
            tl = lo.fields["TriangleList"]
            tmpl = tl[0] if tl else None
            if tmpl is None:
                tmpl = _any_tri_ref(self)
            _resize(tl, len(ln.tris), lambda: copy.deepcopy(tmpl))
            for r, tr in zip(tl, ln.tris):
                _put(r, "TriangleIndex", tr.i)
                _put(r, "NavMeshIndex", tr.mesh.i)
                _put(r, "ManagerIndex", tr.mesh.mgr.idx)
            _put(lo, "NextNavMeshIndexStart", ln.nnis)
            lo.fields["ObjectWayPoints"] = [ow for ow, _w, _c in ln.owps]
            for ow, w, conns in ln.owps:
                _put(ow.fields["WayPoint"], "WayPointIndex", w.i)
                _put(ow.fields["WayPoint"], "ManagerIndex", w.mgr.idx)
                ow.custom = [(c.i, c.mgr.idx) for c in conns]
        net = mo.fields["MyWayPointNetwork"].fields
        net["WayPoints"] = [w.obj for w in m.wps]
        for w in m.wps:
            c = w.obj.custom
            if w.tri is None:
                rel, bits, ti = NONE_REL, c["Bits"] if c["Flags"] & 0xF == NONE_REL else 0xFFF, 0xFFFF
            else:
                rel = self.rel_of(m.idx, w.tri.mesh.mgr.idx)
                if rel is None:
                    raise ValueError(f"waypoint {w.i} of manager {m.idx}: its triangle isn't in a neighbouring cell")
                bits, ti = w.tri.mesh.i, w.tri.i
            links = []
            for dist, x in w.links:
                links.append((dist, self._ref(m, x.mgr, x.i, "waypoint link")))
            w.obj.custom = dict(c, Links=links, Flags=(w.hi << 4) | rel, Bits=bits, TriangleIndex=ti,
                                X=w.X, Y=w.Y, Z=w.Z, ManagerIndex=w.cell)

    def _store_geometry(self, ms):
        nm = ms.obj
        vt = nm.fields["VertexList"]
        nm.fields["VertexList"] = [struct.pack("<4f", *v, 0.0) for v in ms.verts]
        w = len(nm.fields["TriangleVertex"][0]) if nm.fields["TriangleVertex"] else 2
        nm.fields["TriangleVertex"] = [int(k).to_bytes(w, "little") for tr in ms.tris for k in tr.v]
        nm.fields["MoppCode"] = []   # NavMesh::BuildMopp compiles one at load when it's empty
        bv = nm.fields["AABV"].fields
        mn = [min(v[i] for v in ms.verts) for i in range(3)]
        mx = [max(v[i] for v in ms.verts) for i in range(3)]
        bv["Min"] = struct.pack("<3f", *mn) + bv["Min"][12:]
        bv["Max"] = struct.pack("<3f", *mx) + bv["Max"][12:]
        del vt

    def _store_sets(self, m, comp):
        """DirectConnectionSets: kept as loaded while every navmesh's set still is its seam component."""
        mo = m.obj
        same = all((ms.set0 is None and ms not in comp) or
                   (ms.set0 is not None and ms in comp and set(ms.set0) == comp[ms]) for ms in m.meshes)
        if same and not m.sets_dirty:
            refs = mo.fields["DirectConnectionSets"]
            start = [h(x) for x in mo.fields["DirectConnectionSetIndex"]]
            done = set()
            for ms in m.meshes:
                k = h(ms.obj.fields["DirectConnectionIndex"])
                if k == 0xFF or k in done:
                    continue
                done.add(k)
                for r, x in zip(refs[start[k]:], ms.set0):
                    _put(r, "NavMeshIndex", x.i)
                    _put(r, "ManagerIndex", x.mgr.idx)
            return
        order, sets = {}, []
        for ms in m.meshes:
            c = comp.get(ms)
            if c is None:
                _put(ms.obj, "DirectConnectionIndex", 0xFF)
                continue
            if c not in order:
                if len(order) >= 0xFF:
                    raise ValueError(f"manager {m.idx} has too many connection sets")
                order[c] = len(sets)
                old = ms.set0 if ms.set0 is not None and set(ms.set0) == c else None
                sets.append(old or sorted(c, key=lambda x: (x.mgr.idx, x.i)))
            _put(ms.obj, "DirectConnectionIndex", order[c])
        refs, starts, sizes = mo.fields["DirectConnectionSets"], [], []
        tmpl = refs[0] if refs else _any_set_ref(self)
        flat = [x for s in sets for x in s]
        _resize(refs, len(flat), lambda: copy.deepcopy(tmpl))
        for r, x in zip(refs, flat):
            _put(r, "NavMeshIndex", x.i)
            _put(r, "ManagerIndex", x.mgr.idx)
        pos = 0
        for s in sets:
            starts.append(pos)
            sizes.append(len(s))
            pos += len(s)
        w = 2
        mo.fields["DirectConnectionSetIndex"] = [v.to_bytes(w, "little") for v in starts]
        mo.fields["DirectConnectionSetSize"] = [v.to_bytes(w, "little") for v in sizes]
        for ms in m.meshes:
            ms.set0 = None if comp.get(ms) is None else sets[order[comp[ms]]]
        m.sets_dirty = False

    def _store_flow(self, f) -> bool:
        cf = f.cf
        before = (bytes(cf.fields["MetaLinkRef"].fields["ManagerIndex"]),
                  [tuple(t.fields.values()) for t in cf.fields["TriangleArray"]],
                  [tuple(w.fields.values()) for w in cf.fields["WayPointArray"]],
                  bytes(cf.fields["MetaLinkRef"].fields["MetaLinkIndex"]))
        if f.link is not None:
            _put(cf.fields["MetaLinkRef"], "ManagerIndex", f.link.mgr.idx)
            _put(cf.fields["MetaLinkRef"], "MetaLinkIndex", f.link.i)
        for r, tr in zip(cf.fields["TriangleArray"], f.tris):
            _put(r, "TriangleIndex", tr.i)
            _put(r, "NavMeshIndex", tr.mesh.i)
            _put(r, "ManagerIndex", tr.mesh.mgr.idx)
        for r, w in zip(cf.fields["WayPointArray"], f.wps):
            _put(r, "WayPointIndex", w.i)
            _put(r, "ManagerIndex", w.mgr.idx)
        after = (bytes(cf.fields["MetaLinkRef"].fields["ManagerIndex"]),
                 [tuple(t.fields.values()) for t in cf.fields["TriangleArray"]],
                 [tuple(w.fields.values()) for w in cf.fields["WayPointArray"]],
                 bytes(cf.fields["MetaLinkRef"].fields["MetaLinkIndex"]))
        return before != after


def _any_tri_ref(model):
    for ln in model.all_links():
        if ln.obj.fields["TriangleList"]:
            return ln.obj.fields["TriangleList"][0]
    raise ValueError("no NavMeshTriangleRef to copy")


def _any_set_ref(model):
    for m in model.mgr.values():
        if m.obj.fields["DirectConnectionSets"]:
            return m.obj.fields["DirectConnectionSets"][0]
    raise ValueError("no NavMeshRef to copy")
