"""Out-of-bounds walls, edited as connected polylines.

An out-of-bounds element (OutOfBoundsComponent + Visual + InertComponent) stores one boundary three times, all
derived from the same corner list (retail maps, checked on all of them):
- Sections (gameplay, world space): tiles at most 5 m wide, equal widths along each segment. GlobalPosition is the
  tile's centre (base + height / 2), GlobalRotation has Y = GlobalNormal = the inward horizontal normal (toward the
  play area) and X along the wall, Size = (width, height).
- The collision MeshShape (entity-local): a strip, one vertical edge per corner, two triangles per segment. It is the
  only copy whose corners are ordered, so walls are read from it.
- The fog Mesh (VertexFormat 3, entity-local): the same strip, plus a zero-area "fin" at every corner pairing
  vertex copies of alpha 255 and 0 in both windings (the fog shader works with the alpha); u = distance / 10 and
  v = height / 10, shifted by whole tiles.
write_walls() regenerates all three from a list of walls, so moving, adding or removing corners keeps them one
connected piece.
"""
from __future__ import annotations

import copy
import math
import struct
from dataclasses import dataclass

from .doc import MapDocument, u32
from .geom import matrix_rows, mesh_shape_geometry, set_mesh_shape_geometry
from .kinds import component, components

TILE = 5.0      # max section width (retail: 4.8-5.0)
UV_SCALE = 10.0  # metres per texture tile


@dataclass
class Wall:
    corners: list[tuple[float, float, float]]   # world space, wall base
    closed: bool
    heights: list[float]                        # per corner (retail: constant, except a few Pienza/San Donato ones)

    def top(self, i: int):
        p = self.corners[i]
        return (p[0], p[1], p[2] + self.heights[i])


def _world(R, p):
    return tuple(sum(p[k] * R[k][j] for k in range(3)) + R[3][j] for j in range(3))


def _inv_affine(R):
    """Inverse of a row-vector affine matrix (rotation/scale rows 0-2, translation row 3)."""
    a = [R[i][:3] for i in range(3)]
    det = (a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1]) - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
           + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0]))
    inv = [[(a[(j + 1) % 3][(i + 1) % 3] * a[(j + 2) % 3][(i + 2) % 3]
             - a[(j + 1) % 3][(i + 2) % 3] * a[(j + 2) % 3][(i + 1) % 3]) / det for j in range(3)] for i in range(3)]
    t = R[3][:3]
    ti = [-sum(t[k] * inv[k][j] for k in range(3)) for j in range(3)]
    return [inv[0] + [0.0], inv[1] + [0.0], inv[2] + [0.0], ti + [1.0]]


def oob_parts(doc: MapDocument, key):
    """(entity, OutOfBoundsComponent, InertComponent, MeshShape uid, fog Mesh uid or None, Visual component)."""
    from .ops import element_obj, inert_components
    from .visual import entity_meshes
    o = element_obj(doc, key)
    oc = component(o, "OutOfBoundsComponent")
    if oc is None:
        raise ValueError("not an out-of-bounds element")
    ics = inert_components(o)
    sid = u32(ics[0][1].fields["RigidBody"].fields["Shape"].id) if ics else None
    if sid is None or sid not in doc.info or doc.type_of(sid) != "MeshShape":
        raise ValueError("out-of-bounds element without a collision MeshShape")
    meshes = entity_meshes(doc, o)
    vis = component(o, "Visual")
    return o, oc, ics[0][1], sid, (meshes[0] if meshes else None), vis


def walls(doc: MapDocument, key) -> list[Wall]:
    """The element's walls, read from its collision strip: columns (corners) grouped by xy, linked by the strip's
    triangles, walked into chains; a chain whose ends meet is closed."""
    o, _oc, _ic, sid, _m, _v = oob_parts(doc, key)
    R = matrix_rows(o.fields["GlobalMatrix"])
    verts, tris, _ = mesh_shape_geometry(doc.obj(sid))
    W = [_world(R, p) for p in verts]
    col_of, cols = {}, []          # vertex -> column; column = [x, y, zmin, zmax]
    index = {}
    for i, p in enumerate(W):
        k = (round(p[0], 2), round(p[1], 2))
        if k not in index:
            index[k] = len(cols)
            cols.append([p[0], p[1], p[2], p[2]])
        c = cols[index[k]]
        c[2], c[3] = min(c[2], p[2]), max(c[3], p[2])
        col_of[i] = index[k]
    adj: dict[int, set[int]] = {i: set() for i in range(len(cols))}
    for t in tris:
        cs = {col_of[i] for i in t}
        if len(cs) == 2:
            a, b = cs
            adj[a].add(b)
            adj[b].add(a)
    return chains([(c[0], c[1], c[2]) for c in cols], adj, [c[3] - c[2] for c in cols])


def chains(points, adj: dict[int, set[int]], heights) -> list[Wall]:
    """Walls from a corner graph (points: wall bases; adj: neighbours per point; heights per point). Runs between
    corners that don't have exactly two neighbours (ends, T junctions) become open walls; what's left are loops,
    which become closed walls."""
    used: set[frozenset] = set()
    out = []

    def walk(s, first):
        chain, prev, cur = [s], s, first
        used.add(frozenset((s, first)))
        while True:
            chain.append(cur)
            if len(adj[cur]) != 2 or cur == s:
                break
            nxt = next(iter(adj[cur] - {prev}))
            if frozenset((cur, nxt)) in used:
                break
            used.add(frozenset((cur, nxt)))
            prev, cur = cur, nxt
        return chain

    for s in sorted(adj):
        if len(adj[s]) == 2:
            continue
        for n in sorted(adj[s]):
            if frozenset((s, n)) not in used:
                ch = walk(s, n)
                out.append(Wall([tuple(points[c]) for c in ch], False, [heights[c] for c in ch]))
    for s in sorted(adj):
        for n in sorted(adj[s]):
            if frozenset((s, n)) not in used:
                ch = walk(s, n)
                if ch[-1] == ch[0]:
                    ch = ch[:-1]
                out.append(Wall([tuple(points[c]) for c in ch], len(ch) > 2, [heights[c] for c in ch]))
    return out


def _segments(w: Wall):
    n = len(w.corners)
    return [(w.corners[i], w.corners[(i + 1) % n]) for i in range(n if w.closed else n - 1)]


def _seg_heights(w: Wall):
    n = len(w.corners)
    return [(w.heights[i], w.heights[(i + 1) % n]) for i in range(n if w.closed else n - 1)]


def _signed_area(w: Wall) -> float:
    c = w.corners
    return sum(c[i][0] * c[(i + 1) % len(c)][1] - c[(i + 1) % len(c)][0] * c[i][1] for i in range(len(c))) / 2


def _normals(w: Wall, hints=None):
    """Inward unit normal per segment (horizontal). Closed walls: the polygon's inside. Open walls: the side of the
    nearest hint section normal [(position, normal)], else the left side."""
    out = []
    left_inside = _signed_area(w) > 0 if w.closed else None
    for a, b in _segments(w):
        dx, dy = b[0] - a[0], b[1] - a[1]
        L = math.hypot(dx, dy) or 1.0
        left = (-dy / L, dx / L, 0.0)
        if left_inside is not None:
            n = left if left_inside else (-left[0], -left[1], 0.0)
        elif hints:
            m = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            _p, hn = min(hints, key=lambda h: (h[0][0] - m[0]) ** 2 + (h[0][1] - m[1]) ** 2)
            n = left if left[0] * hn[0] + left[1] * hn[1] >= 0 else (-left[0], -left[1], 0.0)
        else:
            n = left
        out.append(n)
    return out


def _quat_from_axes(X, Y, Z):
    """(x, y, z, w) of the rotation whose columns are X, Y, Z."""
    m00, m01, m02 = X[0], Y[0], Z[0]
    m10, m11, m12 = X[1], Y[1], Z[1]
    m20, m21, m22 = X[2], Y[2], Z[2]
    tr = m00 + m11 + m22
    if tr > 0:
        s = math.sqrt(tr + 1.0) * 2
        return ((m21 - m12) / s, (m02 - m20) / s, (m10 - m01) / s, 0.25 * s)
    if m00 > m11 and m00 > m22:
        s = math.sqrt(1.0 + m00 - m11 - m22) * 2
        return (0.25 * s, (m01 + m10) / s, (m02 + m20) / s, (m21 - m12) / s)
    if m11 > m22:
        s = math.sqrt(1.0 + m11 - m00 - m22) * 2
        return ((m01 + m10) / s, 0.25 * s, (m12 + m21) / s, (m02 - m20) / s)
    s = math.sqrt(1.0 + m22 - m00 - m11) * 2
    return ((m02 + m20) / s, (m12 + m21) / s, 0.25 * s, (m10 - m01) / s)


def section_hints(oc):
    out = []
    for s in oc.fields["Sections"]:
        p = struct.unpack("<4f", s.fields["GlobalPosition"])[:3]
        n = struct.unpack("<4f", s.fields["GlobalNormal"])[:3]
        out.append((p, n))
    return out


def build_sections(proto, wall_list: list[Wall], hints=None):
    """Section objects tiling the walls (copies of proto)."""
    out = []
    for w in wall_list:
        for (a, b), n, (ha, hb) in zip(_segments(w), _normals(w, hints), _seg_heights(w)):
            L = math.hypot(b[0] - a[0], b[1] - a[1])
            if L < 1e-4:
                continue
            k = max(1, math.ceil(L / TILE - 1e-6))
            Y = n
            Z = (0.0, 0.0, 1.0)
            X = (Y[1] * Z[2] - Y[2] * Z[1], Y[2] * Z[0] - Y[0] * Z[2], Y[0] * Z[1] - Y[1] * Z[0])
            q = _quat_from_axes(X, Y, Z)
            for i in range(k):
                f = (i + 0.5) / k
                h = ha + (hb - ha) * f
                c = (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f + h / 2)
                s = copy.deepcopy(proto)
                s.fields["GlobalPosition"] = struct.pack("<4f", *c, 0.0)
                s.fields["GlobalRotation"] = struct.pack("<4f", *q)
                s.fields["GlobalNormal"] = struct.pack("<4f", *n, 0.0)
                s.fields["Size"] = struct.pack("<2f", L / k, h)
                out.append(s)
    return out


def fog_mesh_input(wall_list: list[Wall], to_local, material: int, hints=None):
    """The fog mesh (retail pattern, see the module docstring) in the entity's frame (to_local: world -> local)."""
    from .visual import MeshInput
    verts, normals, uvs, tris, colors = [], [], [], [], []

    def vert(p, n, uv, alpha):
        verts.append(p)
        normals.append(n)
        uvs.append(uv)
        colors.append((255, 255, 255, alpha))
        return len(verts) - 1

    loc = to_local
    for w in wall_list:
        segs = _segments(w)
        ns = _normals(w, hints)
        cum = 0.0
        corner_u = []
        for (a, b), n, (ha, hb) in zip(segs, ns, _seg_heights(w)):
            L = math.hypot(b[0] - a[0], b[1] - a[1])
            k = max(1, math.ceil(L / TILE - 1e-6))
            u0 = (cum / UV_SCALE) % 1.0   # whole-tile shift: keeps the mesh's uv span small
            corner_u.append(u0)
            for i in range(k):
                f0, f1 = i / k, (i + 1) / k
                pa = tuple(a[j] + (b[j] - a[j]) * f0 for j in range(3))
                pb = tuple(a[j] + (b[j] - a[j]) * f1 for j in range(3))
                ua, ub = u0 + L * f0 / UV_SCALE, u0 + L * f1 / UV_SCALE
                la, lb = loc(pa), loc(pb)
                hA, hB = ha + (hb - ha) * f0, ha + (hb - ha) * f1
                ta, tb = loc((pa[0], pa[1], pa[2] + hA)), loc((pb[0], pb[1], pb[2] + hB))
                b0 = vert(la, n, (ua, la[2] / UV_SCALE), 255)
                b1 = vert(lb, n, (ub, lb[2] / UV_SCALE), 255)
                t0 = vert(ta, n, (ua, ta[2] / UV_SCALE), 255)
                t1 = vert(tb, n, (ub, tb[2] / UV_SCALE), 255)
                tris += [(t0, b0, t1), (t1, b0, b1)]
            cum += L
        # corner fins: the normal is the average of the adjacent segments' (the bisector)
        for ci, p in enumerate(w.corners):
            idx = [(ci - 1) % len(ns), ci % len(ns)] if w.closed else [i for i in (ci - 1, ci) if 0 <= i < len(ns)]
            nx, ny = sum(ns[i][0] for i in idx), sum(ns[i][1] for i in idx)
            L = math.hypot(nx, ny) or 1.0
            n = (nx / L, ny / L, 0.0)
            u = corner_u[ci] if ci < len(corner_u) else (cum / UV_SCALE) % 1.0
            lb, lt = loc(p), loc(w.top(ci))
            b255, t255 = vert(lb, n, (u, lb[2] / UV_SCALE), 255), vert(lt, n, (u, lt[2] / UV_SCALE), 255)
            b0, t0 = vert(lb, n, (u, lb[2] / UV_SCALE), 0), vert(lt, n, (u, lt[2] / UV_SCALE), 0)
            tris += [(t255, b255, t0), (t0, b255, b0), (t0, b0, t255), (t255, b0, b255)]
    return MeshInput(verts, normals, uvs, tris, [0] * len(tris), [material], colors)


def write_walls(doc: MapDocument, key, wall_list: list[Wall]) -> dict:
    """Regenerate an out-of-bounds element's sections, collision strip and fog mesh from wall_list (world space).
    Returns counts."""
    from .ops import ensure_standalone, make_shape_unique, shape_users
    from .visual import clear_baked_ao, fill_mesh
    wall_list = [w for w in wall_list if len(w.corners) >= 2]
    if not wall_list:
        raise ValueError("an out-of-bounds element needs at least one wall segment")
    o, oc, ic, sid, mesh_uid, vis = oob_parts(doc, key)
    hints = section_hints(oc)
    R = matrix_rows(o.fields["GlobalMatrix"])
    Ri = _inv_affine(R)

    def to_local(p):
        return _world(Ri, p)

    # sections
    oc.fields["Sections"] = build_sections(oc.fields["Sections"][0], wall_list, hints)
    # collision strip
    ensure_standalone(doc, key)
    if len(shape_users(doc, sid)) > 1:
        sid = make_shape_unique(doc, key)
    cv, ct = [], []
    for w in wall_list:
        base = len(cv)
        for i, p in enumerate(w.corners):
            cv.append(to_local(p))
            cv.append(to_local(w.top(i)))
        n = len(w.corners)
        for i in range(n if w.closed else n - 1):
            j = (i + 1) % n
            b0, t0, b1, t1 = base + 2 * i, base + 2 * i + 1, base + 2 * j, base + 2 * j + 1
            ct += [(b0, b1, t1), (b0, t1, t0)]
    shape = doc.obj(sid)
    set_mesh_shape_geometry(shape, cv, ct, [0] * len(ct))
    doc.touch(sid)
    # fog mesh, rewritten in place (same id: the Visual and OutOfBoundsComponent.Visual keep pointing at it)
    if mesh_uid is not None and doc.obj(mesh_uid) is not None:
        mo = doc.obj(mesh_uid)
        mat = u32(mo.fields["CompiledMeshMaterials"][0].id)
        fill_mesh(mo, fog_mesh_input(wall_list, to_local, mat, hints), shadows=None)
        doc.touch(mesh_uid)
        if vis is not None:
            clear_baked_ao(vis)
    # culling bounds
    bv = o.fields.get("BoundingVolume")
    if bv is not None and "Min" in bv.fields:
        bv.fields["Min"] = struct.pack("<3f", *(min(p[k] for p in cv) for k in range(3)))
        bv.fields["Max"] = struct.pack("<3f", *(max(p[k] for p in cv) for k in range(3)))
    doc.touch(key[0])
    return {"walls": len(wall_list), "corners": sum(len(w.corners) for w in wall_list),
            "sections": len(oc.fields["Sections"])}
