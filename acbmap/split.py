"""Split imported geometry into game-sized pieces.

A whole imported scene (an OBJ of a city) can't be one element: a MeshShape indexes with 16 bits, a visual mesh
holds at most 65535 vertices and a uv span of 16 tiles, and its quantized positions lose precision with size
(1/32768 of the mesh's extent). partition() cuts the triangles with a k-d tree on their centroids until every piece
is within those limits; each piece then becomes its own element, centred on its own origin."""
from __future__ import annotations

from dataclasses import dataclass

from .visual import MAX_UV, MeshInput

MAX_TRIS = 20000          # 3 corners each stays under 65535 vertices, whatever is shared
MAX_SIZE = 64.0           # metres: culling, MOPP size and position precision (2 mm at 64 m)
MAX_UV_SPAN = MAX_UV - 1  # tiles, after the encoder's integer offset


@dataclass
class Node:
    axis: int = -1                 # -1: leaf
    split: float = 0.0
    lo: "Node | None" = None
    hi: "Node | None" = None
    leaf: int = -1


def _centroid(verts, tri):
    a, b, c = (verts[i] for i in tri)
    return tuple((a[k] + b[k] + c[k]) / 3 for k in range(3))


def _uv_span(uvs, tris, idx) -> float:
    if uvs is None or not idx:
        return 0.0
    us = [uvs[i][0] for t in idx for i in tris[t]]
    vs = [uvs[i][1] for t in idx for i in tris[t]]
    return max(max(us) - min(us), max(vs) - min(vs))


def partition(verts, tris, uvs=None, max_tris: int = MAX_TRIS, max_size: float = MAX_SIZE):
    """K-d partition of triangles (uvs: per vertex, for the uv-span limit -- build from the visual MeshInput when
    there is one, then assign() the collision to the same leaves). Returns (tree, leaves), leaves = [triangle
    indices]."""
    cents = [_centroid(verts, t) for t in tris]
    leaves: list[list[int]] = []

    def extent(idx):
        pts = [verts[i] for t in idx for i in tris[t]]
        lo = [min(p[k] for p in pts) for k in range(3)]
        hi = [max(p[k] for p in pts) for k in range(3)]
        return lo, hi

    def build(idx) -> Node:
        lo, hi = extent(idx)
        size = [hi[k] - lo[k] for k in range(3)]
        if len(idx) <= 1 or (len(idx) <= max_tris and max(size) <= max_size and _uv_span(uvs, tris, idx) <= MAX_UV_SPAN):
            leaves.append(idx)
            return Node(leaf=len(leaves) - 1)
        axis = max(range(3), key=lambda k: size[k])
        order = sorted(idx, key=lambda t: cents[t][axis])
        mid = len(order) // 2
        split = cents[order[mid]][axis]
        a = [t for t in order if cents[t][axis] < split]
        b = [t for t in order if cents[t][axis] >= split]
        if not a or not b:    # all centroids equal on that axis: split by count
            a, b = order[:mid], order[mid:]
        return Node(axis, split, build(a), build(b))

    return build(list(range(len(tris)))), leaves


def leaf_of(node: Node, p) -> int:
    while node.axis >= 0:
        node = node.lo if p[node.axis] < node.split else node.hi
    return node.leaf


def assign(node: Node, verts, tris) -> dict[int, list[int]]:
    """Put another triangulation of the same surface into the tree's leaves (by centroid)."""
    out: dict[int, list[int]] = {}
    for ti, t in enumerate(tris):
        out.setdefault(leaf_of(node, _centroid(verts, t)), []).append(ti)
    return out


def center(verts, tris, idx):
    pts = [verts[i] for t in idx for i in tris[t]]
    return tuple((min(p[k] for p in pts) + max(p[k] for p in pts)) / 2 for k in range(3))


def sub_collision(verts, tris, mats, idx, offset):
    """(verts, tris, mats) of the triangles idx, re-indexed and moved by -offset."""
    remap: dict[int, int] = {}
    nv, nt = [], []
    for t in idx:
        tri = []
        for i in tris[t]:
            if i not in remap:
                remap[i] = len(nv)
                nv.append(tuple(verts[i][k] - offset[k] for k in range(3)))
            tri.append(remap[i])
        nt.append(tuple(tri))
    return nv, nt, [mats[t] for t in idx]


def sub_mesh_input(mi: MeshInput, idx, offset) -> MeshInput:
    """The triangles idx of mi as their own MeshInput: re-indexed, moved by -offset, uvs shifted by whole tiles
    toward 0 (textures tile, so this changes nothing visible) and only the materials they use."""
    remap: dict[int, int] = {}
    verts, normals, uvs, tris, tmat = [], [], [], [], []
    used = sorted({mi.tri_material[t] for t in idx})
    mat_index = {m: i for i, m in enumerate(used)}
    for t in idx:
        tri = []
        for i in mi.tris[t]:
            if i not in remap:
                remap[i] = len(verts)
                verts.append(tuple(mi.verts[i][k] - offset[k] for k in range(3)))
                normals.append(mi.normals[i])
                uvs.append(mi.uvs[i])
            tri.append(remap[i])
        tris.append(tuple(tri))
        tmat.append(mat_index[mi.tri_material[t]])
    if uvs:
        du = float(int(min(u for u, _ in uvs) // 1))
        dv = float(int(min(v for _, v in uvs) // 1))
        uvs = [(u - du, v - dv) for u, v in uvs]
    return MeshInput(verts, normals, uvs, tris, tmat, [mi.materials[m] for m in used])
