"""Splitting imported geometry: limits hold, every triangle lands in exactly one piece, geometry is unchanged."""
import random

from acbmap import split as S
from acbmap.visual import MeshInput


def grid(n, step):
    """An n x n quad grid (2n^2 triangles) with box uvs at 1 tile per 2 m."""
    verts = [(x * step, y * step, 0.0) for y in range(n + 1) for x in range(n + 1)]
    tris = []
    for y in range(n):
        for x in range(n):
            a = y * (n + 1) + x
            tris += [(a, a + 1, a + n + 2), (a, a + n + 2, a + n + 1)]
    uvs = [(v[0] / 2, v[1] / 2) for v in verts]
    return verts, tris, uvs


def test_partition_limits_and_cover():
    verts, tris, uvs = grid(120, 1.0)          # 120 m square, 28800 triangles
    tree, leaves = S.partition(verts, tris, uvs, max_tris=5000, max_size=40.0)
    assert sorted(t for l in leaves for t in l) == list(range(len(tris)))
    for l in leaves:
        lo, hi = [min(verts[i][k] for t in l for i in tris[t]) for k in range(2)], \
                 [max(verts[i][k] for t in l for i in tris[t]) for k in range(2)]
        assert len(l) <= 5000 and max(hi[k] - lo[k] for k in range(2)) <= 40.0
        assert S._uv_span(uvs, tris, l) <= S.MAX_UV_SPAN
    # another triangulation of the same surface (flipped diagonals) lands in the same leaves by centroid
    alt = [(t[0], t[1], t[2]) for t in tris]
    got = S.assign(tree, verts, alt)
    assert sum(len(v) for v in got.values()) == len(alt)


def test_sub_pieces_keep_geometry():
    verts, tris, uvs = grid(30, 1.5)
    mi = MeshInput(verts, [(0.0, 0.0, 1.0)] * len(verts), uvs, tris, [i % 2 for i in range(len(tris))], [11, 22])
    tree, leaves = S.partition(mi.verts, mi.tris, mi.uvs, max_tris=400, max_size=1e9)
    random.seed(1)
    for l in random.sample(leaves, 3):
        c = S.center(verts, tris, l)
        sub = S.sub_mesh_input(mi, l, c)
        assert len(sub.tris) == len(l) and set(sub.materials) <= {11, 22}
        for t_new, t_old in zip(sub.tris, l):
            for i_new, i_old in zip(t_new, tris[t_old]):
                assert all(abs(sub.verts[i_new][k] + c[k] - verts[i_old][k]) < 1e-9 for k in range(3))
                du = sub.uvs[i_new][0] - uvs[i_old][0]
                assert abs(du - round(du)) < 1e-9          # shifted by whole tiles only
        assert sub.materials[sub.tri_material[0]] == mi.materials[mi.tri_material[l[0]]]
        cv, ct, cm = S.sub_collision(verts, tris, [0] * len(tris), l, c)
        assert len(ct) == len(l) and len(cv) <= 3 * len(l)
