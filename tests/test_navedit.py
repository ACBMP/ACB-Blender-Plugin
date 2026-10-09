"""Navmesh editing (navmodel / navedit) on retail maps."""
import glob
import math
import os

import pytest

from acbmap import navedit as NE
from acbmap import ops
from acbmap.doc import MapDocument, codec
from acbmap.flowedit import world_points
from acbmap.navmodel import LEDGE, SEAM, NavModel

MULTI = os.environ.get("ACB_MULTI", "/home/a/vbox/Assassin's Creed Brotherhood/multi")
MAP = os.path.join(MULTI, "DataPC_AC2MP_SanMarco.forge")
pytestmark = pytest.mark.skipif(not os.path.exists(MAP), reason="needs the ACB install")


def quad(x, y, z, r):
    return [((x - r, y - r, z), (x + r, y - r, z), (x + r, y + r, z)), ((x - r, y - r, z), (x + r, y + r, z), (x - r, y + r, z))]


def reloaded_ok(d):
    return NE.check(NavModel(d)) == []


def open_spot(d, M, clear=4.0, away=6.0):
    """A point of flat open ground at least `away` m from every crowd flow point."""
    fpts = [p for f in M.flows for p in world_points(d.obj(f.uid))]
    g = NE.Geo(M)
    for ms in M.meshes():
        for t in ms.tris:
            T = t.corners()
            c = tuple(sum(v[i] for v in T) / 3 for i in range(3))
            if min((math.dist(c[:2], p[:2]) for p in fpts), default=99) < away:
                continue
            if all(g.tri_at((c[0] + dx, c[1] + dy, c[2]), 0.3) for dx in (-clear, 0, clear) for dy in (-clear, 0, clear)):
                return c
    raise AssertionError("no open ground")


@pytest.mark.parametrize("forge", sorted(glob.glob(os.path.join(MULTI, "DataPC_AC2MP_*.forge"))))
def test_model_roundtrips(forge):
    d = MapDocument(forge)
    m = NavModel(d)
    for u in m.store():
        assert codec().encode(d.root(u)) == d.payload(u)


def test_unchanged_triangles_change_nothing():
    d = MapDocument(MAP)
    M = NavModel(d)
    tr = NE.export_triangles(M)
    assert NE.apply_triangles(M, d, [t for t, _ in tr], [m for _, m in tr]) == {"navmeshes_rebuilt": 0}
    assert NE.check(M) == []


def test_block_area_cuts_a_hole():
    d = MapDocument(MAP)
    M = NavModel(d)
    x, y, z = open_spot(d, M)
    st = NE.block_area(M, d, quad(x, y, z, 2) + quad(x, y, z + 2, 2))
    assert st["triangles_changed"] > 0 and not any(k.startswith("links_removed_type") and k != "links_removed_type0"
                                                    for k in st)
    g = NE.Geo(M)
    assert g.tri_at((x, y, z), 0.5) is None
    a, b = (x - 4, y, z), (x + 4, y, z)
    assert not g.line_clear(a, g.tri_at(a, 0.5), b, g.tri_at(b, 0.5))
    assert NE.check(M) == [] and reloaded_ok(d)


def test_add_surface_seams_to_the_ground_around_it():
    d = MapDocument(MAP)
    M = NavModel(d)
    x, y, z = open_spot(d, M)
    NE.block_area(M, d, quad(x, y, z, 2))
    M = NavModel(d)
    n_seams = sum(1 for ln in M.all_links() if ln.type == SEAM)
    st = NE.add_surface(M, d, quad(x, y, z, 2), solid=True)
    assert st["seams_added"] > 0
    g = NE.Geo(M)
    a, b = (x - 3.5, y, z), (x, y, z)
    assert g.tri_at(b, 0.3) is not None and g.line_clear(a, g.tri_at(a, 0.3), b, g.tri_at(b, 0.3))
    assert sum(1 for ln in M.all_links() if ln.type == SEAM) >= n_seams
    assert NE.check(M) == [] and reloaded_ok(d)


def test_extend_past_a_ledge():
    d = MapDocument(MAP)
    M = NavModel(d)
    g = NE.Geo(M)
    for ms in M.meshes():
        for t in ms.tris:
            for k in range(3):
                T = t.corners()
                a, b = T[k], T[(k + 1) % 3]
                if t.nbr[k] != LEDGE or math.dist(a[:2], b[:2]) < 2.5 or abs(a[2] - b[2]) > 0.02:
                    continue
                nx, ny = b[1] - a[1], -(b[0] - a[0])
                L = math.hypot(nx, ny)
                nx, ny = nx / L, ny / L
                c = [(a[i] + b[i]) / 2 for i in range(3)]
                if g.heights((c[0] + nx * 2, c[1] + ny * 2, c[2])) or \
                        M.cell_of(c[0] + nx * 2, c[1] + ny * 2) != M.cell_of(c[0], c[1]):
                    continue
                tris = [T for T, _ in NE.export_triangles(M)]
                owners = [o for _, o in NE.export_triangles(M)]
                a2 = (a[0] + nx * 2, a[1] + ny * 2, a[2])
                b2 = (b[0] + nx * 2, b[1] + ny * 2, b[2])
                tris += [(b, a, a2), (b, a2, b2)]
                owners += [ms, ms]
                try:
                    NE.apply_triangles(M, d, tris, owners)
                except ops.EditError:   # e.g. a flow point on a neighbouring piece: try another ledge
                    M = NavModel(d)
                    g = NE.Geo(M)
                    continue
                g = NE.Geo(M)
                p = (c[0] - nx * 0.2, c[1] - ny * 0.2, c[2])
                q = (c[0] + nx * 1.5, c[1] + ny * 1.5, c[2])
                assert g.tri_at(q, 0.3) is not None
                assert g.line_clear(p, g.tri_at(p, 0.3), q, g.tri_at(q, 0.3))
                assert NE.check(M) == [] and reloaded_ok(d)
                return
    raise AssertionError("no ledge to extend")


def test_deleting_a_navmesh_renumbers_everything():
    d = MapDocument(MAP)
    M = NavModel(d)
    n0 = sum(1 for _ in M.meshes())
    flow_meshes = {t.mesh for f in M.flows for t in f.tris}
    victim = min((ms for ms in M.meshes() if ms not in flow_meshes), key=lambda ms: (ms.mgr.idx, -len(ms.tris)))
    keep = [(T, m) for T, m in NE.export_triangles(M) if m is not victim]
    st = NE.apply_triangles(M, d, [t for t, _ in keep], [m for _, m in keep])
    assert st["navmeshes_removed"] == 1
    M2 = NavModel(d)
    assert sum(1 for _ in M2.meshes()) == n0 - 1 and NE.check(M2) == []


def test_edit_that_strands_a_flow_point_is_refused():
    d = MapDocument(MAP)
    M = NavModel(d)
    before = {u: d.payload(u) for u in d.uids("NavMeshManager")}
    p = world_points(d.obj(M.flows[0].uid))[0]
    with pytest.raises(ops.EditError, match="move those flows first"):
        NE.block_area(M, d, quad(p[0], p[1], p[2], 1))
    M = NavModel(d)
    assert NE.check(M) == []
    for u in before:   # the managers weren't touched
        assert codec().encode(d.root(u)) == before[u]
