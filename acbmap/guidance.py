"""Climb edges: an entity's GuidanceSystem component, read and generated.

Layout (inferred from the retail maps; every field below round-trips through fastload):
- GuidanceObjects: one per edge. Index0/Index1 index the points; SubType is GuidanceObjectSubType (1 ledge grab,
  2 beam, 3 ladder, 4 pole, 5 rope, 13 haystack, ...); DecN4Normal0/1 are four int16 each, xyz scaled by 511 and
  a zero w: the two faces meeting at the edge. One is the walkable top, the other the wall or underside; retail
  stores both orders (56% top first), so the game tells them apart itself.
- CompressPoints: int16 x,y,z per point, signed-normalized into the Partitioner's Min/Max box:
  p = min + (q / 32767 + 1) / 2 * (max - min). Entity-local, like the collision shapes.
- Partitioner: a tree over the edges, nodes stored children first with the root last (RootIndex). NodeType 0 is a
  leaf, [Index0, Index1) a range of LeafsIndex (edge indices); NodeTypes 1-3 split, Index0/Index1 child nodes
  (65535 = none), Middle the split value. 95% of retail systems are one leaf, up to 173 edges; that is what the
  generator writes.
- InertComponent.GuidanceSystemPtr links (status 5) to the GuidanceSystem built from that shape; the component
  itself sits inline (status 4) in Entity.Components.

The generator works on one entity's own collision: a ledge is a convex edge where a walkable face (normal z at
least EdgeFilter.SlopeCosAngle) meets a steep face (a wall or an underside), with collinear runs merged. Retail edges
were built by Ubisoft's offline tool over the whole map (some sit on neighbouring geometry, some are trimmed), so
the generator reproduces most of them on the entity's own geometry, not all.
"""
from __future__ import annotations

import copy
import math
import struct
from dataclasses import dataclass

from anvilforge.fastload import Obj, Ptr

from .doc import MapDocument, idb, u32
from .geom import mesh_shape_geometry
from .schema import type_name

LEDGE = 1
SUBTYPE_NAMES = {1: "ledge", 2: "beam", 3: "ladder", 4: "pole", 5: "rope", 6: "surface", 8: "kiosk", 13: "haystack"}
DEFAULT_SLOPE_COS = math.cos(math.radians(45))
WELD = 1e-3            # vertex weld tolerance (m)
MIN_EDGE = 0.05        # shorter generated edges are dropped (m)
MIN_DEPTH = 0.1        # a ledge needs this much walkable surface behind it (m)
MIN_DROP = 0.2         # and this much wall below it, or it's a step (m)
REACH_CAP = 2.0        # depth/drop are measured over connected faces up to this far (m)
STEEP_Z = 0.5          # a face with normal z below this can be the wall side of a ledge
MERGE_COS = 0.999      # collinear and same-normal tolerance when merging edges


@dataclass
class Edge:
    p0: tuple[float, float, float]
    p1: tuple[float, float, float]
    n0: tuple[float, float, float]
    n1: tuple[float, float, float]
    subtype: int = LEDGE
    depth: float = 0.0     # how far the walkable face reaches back from the edge (m)
    drop: float = 0.0      # how far the wall face reaches down from the edge (m)


# ------------------------------------------------------------------ math --

def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _norm(a):
    n = math.sqrt(_dot(a, a))
    return (a[0] / n, a[1] / n, a[2] / n) if n > 1e-12 else (0.0, 0.0, 0.0)


# ------------------------------------------------------------------ read --

def systems(entity: Obj) -> list[Obj]:
    return [p.obj for p in entity.fields.get("Components", [])
            if isinstance(p, Ptr) and p.obj is not None and type_name(p.obj.type_hash) == "GuidanceSystem"]


def points(g: Obj) -> list[tuple[float, float, float]]:
    P = g.fields["Partitioner"].obj
    lo = struct.unpack("<3f", P.fields["Min"])
    hi = struct.unpack("<3f", P.fields["Max"])
    q = [struct.unpack("<h", x)[0] for x in g.fields["CompressPoints"]]
    return [tuple(lo[k] + (q[i + k] / 32767 + 1) / 2 * (hi[k] - lo[k]) for k in range(3))
            for i in range(0, len(q) - 2, 3)]


def _dn4(b: bytes):
    x, y, z, _w = struct.unpack("<4h", b)
    return (x / 511, y / 511, z / 511)


def _pack_dn4(n) -> bytes:
    return struct.pack("<4h", *(max(-511, min(511, round(c * 511))) for c in n), 0)


def edges(g: Obj) -> list[Edge]:
    pts = points(g)
    out = []
    for o in g.fields["GuidanceObjects"]:
        f = o.fields
        i0, i1 = u32(f["Index0"]), u32(f["Index1"])
        if not f["Valid"][0] or i0 >= len(pts) or i1 >= len(pts):
            continue
        out.append(Edge(pts[i0], pts[i1], _dn4(f["DecN4Normal0"]), _dn4(f["DecN4Normal1"]), u32(f["SubType"])))
    return out


def slope_cos(g: Obj | None) -> float:
    if g is None:
        return DEFAULT_SLOPE_COS
    return struct.unpack("<f", g.fields["EdgeFilter"].fields["SlopeCosAngle"])[0]


# ------------------------------------------------------------------ generate --

def generate(verts, tris, slope_cos_: float = DEFAULT_SLOPE_COS, min_depth: float = MIN_DEPTH,
             min_drop: float = MIN_DROP) -> list[Edge]:
    """Ledge edges of a triangle mesh (entity-local). Compared with the retail edges on the retail maps' own
    geometry, the depth/drop filters keep about two thirds of the edges retail has and drop about three quarters
    of those it doesn't (the rest depends on neighbouring geometry, which this doesn't see)."""
    # weld
    key = {}
    remap = []
    wv = []
    for v in verts:
        k = (round(v[0] / WELD), round(v[1] / WELD), round(v[2] / WELD))
        if k not in key:
            key[k] = len(wv)
            wv.append(tuple(v))
        remap.append(key[k])
    faces, normals = [], []
    for t in tris:
        a, b, c = (remap[i] for i in t)
        if a == b or b == c or a == c:
            continue
        n = _norm(_cross(_sub(wv[b], wv[a]), _sub(wv[c], wv[a])))
        if n == (0.0, 0.0, 0.0):
            continue
        faces.append((a, b, c))
        normals.append(n)
    adj: dict[tuple[int, int], list[int]] = {}
    for fi, (a, b, c) in enumerate(faces):
        for u, v in ((a, b), (b, c), (c, a)):
            adj.setdefault((min(u, v), max(u, v)), []).append(fi)

    face_nb: list[list[int]] = [[] for _ in faces]
    for fs in adj.values():
        for a in fs:
            face_nb[a] += [b for b in fs if b != a]

    def spread(f_start, ok, measure):
        """Largest measure() over the faces reachable from f_start through faces with ok(), capped at REACH_CAP."""
        best, seen, stack = 0.0, {f_start}, [f_start]
        while stack and best < REACH_CAP:
            f = stack.pop()
            best = max(best, max(measure(wv[i]) for i in faces[f]))
            for nb in face_nb[f]:
                if nb not in seen and ok(nb):
                    seen.add(nb)
                    stack.append(nb)
        return min(best, REACH_CAP)

    raw = []
    for (u, v), fs in adj.items():
        if len(fs) != 2:
            continue
        f0, f1 = fs
        n0, n1 = normals[f0], normals[f1]
        if n0[2] < n1[2]:
            f0, f1, n0, n1 = f1, f0, n1, n0
        if n0[2] < slope_cos_ or n1[2] > STEEP_Z:
            continue
        # convex: each face's far vertex lies behind the other face's plane
        far0 = next(i for i in faces[f0] if i not in (u, v))
        far1 = next(i for i in faces[f1] if i not in (u, v))
        if _dot(n0, _sub(wv[far1], wv[u])) > -1e-4 or _dot(n1, _sub(wv[far0], wv[u])) > -1e-4:
            continue
        t = _norm(_sub(wv[v], wv[u]))

        def reach(p, u=u, t=t):   # horizontal distance of p from the edge line
            r = _sub(p, wv[u])
            r = (r[0], r[1], 0.0)
            k = _dot(r, t)
            return math.sqrt(max(0.0, _dot(r, r) - k * k))
        top = max(wv[u][2], wv[v][2])
        depth = spread(f0, lambda f: normals[f][2] >= slope_cos_, reach)
        drop = spread(f1, lambda f: normals[f][2] <= STEEP_Z and all(wv[i][2] <= top + 1e-3 for i in faces[f]),
                      lambda p, top=top: top - p[2])
        raw.append(Edge(wv[u], wv[v], n0, n1, LEDGE, depth, drop))
    return [e for e in _merge(raw)
            if math.dist(e.p0, e.p1) >= MIN_EDGE and e.depth >= min_depth and e.drop >= min_drop]


def _merge(raw: list[Edge]) -> list[Edge]:
    """Join edges that continue each other in a straight line with the same face normals."""
    def same(a: Edge, b: Edge) -> bool:
        return _dot(a.n0, b.n0) > MERGE_COS and _dot(a.n1, b.n1) > MERGE_COS
    edges_ = list(raw)
    changed = True
    while changed:
        changed = False
        by_end: dict[tuple, list[int]] = {}
        for i, e in enumerate(edges_):
            for p in (e.p0, e.p1):
                by_end.setdefault(p, []).append(i)
        dead = set()
        for p, ids in by_end.items():
            ids = [i for i in ids if i not in dead]
            if len(ids) != 2:
                continue
            a, b = edges_[ids[0]], edges_[ids[1]]
            if not same(a, b):
                continue
            pa = a.p1 if a.p0 == p else a.p0
            pb = b.p1 if b.p0 == p else b.p0
            if _dot(_norm(_sub(p, pa)), _norm(_sub(pb, p))) < MERGE_COS:
                continue
            edges_[ids[0]] = Edge(pa, pb, a.n0, a.n1, a.subtype, max(a.depth, b.depth), max(a.drop, b.drop))
            dead.add(ids[1])
            changed = True
        edges_ = [e for i, e in enumerate(edges_) if i not in dead]
    return edges_


def build_system(template: Obj, edge_list: list[Edge], new_ids: list[int]) -> Obj:
    """A GuidanceSystem holding edge_list, cloned from a retail one (keeps its EdgeFilter and field layout).
    new_ids: two unused object ids, for the system and its Partitioner (the only sub-objects with ids)."""
    g = copy.deepcopy(template)
    g.id = idb(new_ids[0])
    g.fields["Partitioner"].obj.id = idb(new_ids[1])
    pts: dict[tuple, int] = {}
    order: list[tuple] = []
    for e in edge_list:
        for p in (e.p0, e.p1):
            k = tuple(round(c, 5) for c in p)
            if k not in pts:
                pts[k] = len(order)
                order.append(k)
    lo = [min(p[k] for p in order) for k in range(3)] if order else [0.0] * 3
    hi = [max(p[k] for p in order) for k in range(3)] if order else [0.0] * 3

    def q(p):
        out = []
        for k in range(3):
            span = hi[k] - lo[k]
            v = 0 if span <= 0 else round(((p[k] - lo[k]) / span * 2 - 1) * 32767)
            out.append(struct.pack("<h", max(-32768, min(32767, v))))
        return out
    g.fields["CompressPoints"] = [b for p in order for b in q(p)]
    proto = template.fields["GuidanceObjects"][0] if template.fields["GuidanceObjects"] else None
    if proto is None:
        raise ValueError("template GuidanceSystem has no edges to copy the edge layout from")
    objs = []
    for e in edge_list:
        o = copy.deepcopy(proto)
        o.fields["Valid"] = b"\x01"
        o.fields["SubType"] = e.subtype.to_bytes(4, "little")
        o.fields["Index0"] = pts[tuple(round(c, 5) for c in e.p0)].to_bytes(4, "little")
        o.fields["Index1"] = pts[tuple(round(c, 5) for c in e.p1)].to_bytes(4, "little")
        o.fields["DecN4Normal0"] = _pack_dn4(e.n0)
        o.fields["DecN4Normal1"] = _pack_dn4(e.n1)
        objs.append(o)
    g.fields["GuidanceObjects"] = objs
    P = g.fields["Partitioner"].obj
    P.fields["Min"] = struct.pack("<3f", *lo)
    P.fields["Max"] = struct.pack("<3f", *hi)
    P.fields["LeafsIndex"] = [i.to_bytes(2, "little") for i in range(len(objs))]
    P.fields["RootIndex"] = (0).to_bytes(2, "little")
    leaf = copy.deepcopy(next(n for n in _all_nodes(template) if u32(n.fields["NodeType"]) == 0))
    leaf.fields["Index0"] = (0).to_bytes(2, "little")
    leaf.fields["Index1"] = len(objs).to_bytes(2, "little")
    P.fields["ListNodes"] = [leaf] if objs else []
    return g


def _all_nodes(g: Obj):
    return g.fields["Partitioner"].obj.fields["ListNodes"]


def template_system(doc: MapDocument) -> Obj:
    """A retail GuidanceSystem of this map with a single-leaf partitioner and ledge edges, to clone from."""
    from .kinds import classify
    for e in classify(doc, with_children=False):
        for g in systems(e.obj):
            nodes = _all_nodes(g)
            if (len(nodes) == 1 and g.fields["GuidanceObjects"]
                    and u32(g.fields["GuidanceObjects"][0].fields["SubType"]) == LEDGE):
                return g
    raise ValueError("no retail GuidanceSystem in this map to use as a template")


def regenerate(doc: MapDocument, key, fresh_ids, min_depth: float = MIN_DEPTH, min_drop: float = MIN_DROP) -> int:
    """Replace an element's climb edges with ones generated from its collision shapes (one GuidanceSystem per
    InertComponent with a MeshShape, linked from it). Returns the number of edges; marks the root touched."""
    from .ops import element_obj, inert_components, strip_guidance
    o = element_obj(doc, key)
    old = systems(o)
    tmpl = old[0] if old and old[0].fields["GuidanceObjects"] and len(_all_nodes(old[0])) == 1 else template_system(doc)
    cos_ = slope_cos(old[0] if old else tmpl)
    strip_guidance(o)
    total = 0
    comps = o.fields["Components"]
    for _ci, ic in inert_components(o):
        sid = u32(ic.fields["RigidBody"].fields["Shape"].id)
        if sid not in doc.info or doc.type_of(sid) != "MeshShape":
            continue
        v, t, _m = mesh_shape_geometry(doc.obj(sid))
        el = generate(v, t, cos_, min_depth, min_drop)
        if not el:
            continue
        ids = fresh_ids(doc, 2)
        gid = ids[0]
        g = build_system(tmpl, el, ids)
        # inline component, placed before the InertComponents like retail
        first_inert = next(i for i, p in enumerate(comps) if isinstance(p, Ptr) and p.obj is not None
                           and type_name(p.obj.type_hash) == "InertComponent")
        comps.insert(first_inert, Ptr(4, None, g))
        ic.fields["GuidanceSystemPtr"] = Ptr(5, idb(gid))
        total += len(el)
    doc.touch(key[0])
    return total

