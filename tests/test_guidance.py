"""Climb edges: the writer reproduces retail systems, the generator finds ledges, generated edges survive a save."""
import math
import os

import pytest

from acbmap import guidance as G, ops
from acbmap.checks import new_problems
from acbmap.doc import MapDocument, u32
from acbmap.kinds import classify

MULTI = os.environ.get("ACB_MULTI", "/home/a/vbox/Assassin's Creed Brotherhood/multi")
MAP = os.path.join(MULTI, "DataPC_AC2MP_MtStMichel_dlc.forge")
needs_map = pytest.mark.skipif(not os.path.exists(MAP), reason="needs the ACB install")

BOX_V = [(x, y, z) for z in (0, 2) for y in (0, 2) for x in (0, 2)]
BOX_T = [(0, 2, 1), (1, 2, 3), (4, 5, 6), (5, 7, 6), (0, 1, 4), (1, 5, 4), (2, 6, 3), (3, 6, 7),
         (0, 4, 2), (2, 4, 6), (1, 3, 5), (3, 7, 5)]


def test_box_has_four_top_ledges():
    el = G.generate(BOX_V, BOX_T)
    assert len(el) == 4
    assert sorted(round(math.dist(e.p0, e.p1), 3) for e in el) == [2.0] * 4
    for e in el:
        assert e.p0[2] == e.p1[2] == 2
        assert e.n0 == (0.0, 0.0, 1.0) and abs(e.n1[2]) < 1e-9
        mid = [(a + b) / 2 for a, b in zip(e.p0, e.p1)]
        assert not all(0 <= m + 0.5 * n <= 2 for m, n in zip(mid[:2], e.n1[:2])), "second normal: the wall's, outward"
        d = [b - a for a, b in zip(e.p0, e.p1)]
        assert G._dot(G._cross(d, e.n0), e.n1) > 0, "runs the way every retail edge does"
        assert e.subtype == G.LEDGE


def test_thin_bar_is_a_pole():
    bar = [(x * 1.0, y * 0.05, z * 0.15 - 0.3) for x, y, z in BOX_V]   # 2 m x 10 cm x 30 cm, like retail poles
    el = G.generate(bar, BOX_T)
    poles = [e for e in el if e.subtype == G.POLE]
    assert len(poles) == 2 and all(round(math.dist(e.p0, e.p1), 3) == 2.0 for e in poles)
    assert poles[0].n1[1] == -poles[1].n1[1] != 0


def test_step_is_not_a_ledge():
    low = [(x, y, z * 0.05) for x, y, z in BOX_V]   # a 10 cm step
    assert G.generate(low, BOX_T) == []


@needs_map
def test_writer_reproduces_retail():
    d = MapDocument(MAP)
    n = 0
    for e in classify(d, with_children=False):
        for g in G.systems(e.obj):
            if len(G._all_nodes(g)) != 1 or not g.fields["GuidanceObjects"]:
                continue
            el = G.edges(g)
            if len(el) != len(g.fields["GuidanceObjects"]):
                continue
            ng = G.build_system(g, el, [0xF0FFFFF0, 0xF0FFFFF1])
            for a, b, x, y in zip(el, G.edges(ng), g.fields["GuidanceObjects"], ng.fields["GuidanceObjects"]):
                assert math.dist(a.p0, b.p0) < 1e-3 and math.dist(a.p1, b.p1) < 1e-3
                assert x.fields == y.fields | {"Index0": x.fields["Index0"], "Index1": x.fields["Index1"]}
            # the stored values themselves, not just what this module decodes them to: the game reads them
            used = {u32(x.fields[k]) for x in g.fields["GuidanceObjects"] for k in ("Index0", "Index1")}
            cp = g.fields["CompressPoints"]
            assert set(b"".join(ng.fields["CompressPoints"])[i:i + 6] for i in range(0, len(ng.fields["CompressPoints"]) * 2, 6)) \
                == {b"".join(cp[3 * i:3 * i + 3]) for i in used}
            n += 1
    assert n > 100


@needs_map
def test_regenerate_survives_save(tmp_path):
    d = MapDocument(MAP)
    target = next(e for e in classify(d, with_children=False)
                  if e.kind == "collision" and G.systems(e.obj) and G.generate(*_shape(d, e)))
    n = G.regenerate(d, target.key, ops.fresh_ids)
    want = [(e.p0, e.p1) for g in G.systems(ops.element_obj(d, target.key)) for e in G.edges(g)]
    nk = ops.new_collision(d, ops.element_obj(d, target.key).fields["GlobalMatrix"],
                           BOX_V, BOX_T, [0] * len(BOX_T))
    assert G.regenerate(d, nk, ops.fresh_ids) == 4
    out = str(tmp_path / os.path.basename(MAP))
    d.save(out)
    r = MapDocument(out)
    got = [(e.p0, e.p1) for g in G.systems(ops.element_obj(r, target.key)) for e in G.edges(g)]
    assert len(got) == len(want) == n
    assert all(math.dist(a, c) < 1e-3 and math.dist(b, d_) < 1e-3 for (a, b), (c, d_) in zip(want, got))
    new = ops.element_obj(r, nk)
    assert len(G.edges(G.systems(new)[0])) == 4
    link = next(ic for _i, ic in ops.inert_components(new)).fields["GuidanceSystemPtr"]
    assert link.status == 5 and link.link == G.systems(new)[0].id
    assert new_problems(r, MapDocument(MAP)) == []


def _shape(d, e):
    from acbmap.doc import u32
    from acbmap.geom import mesh_shape_geometry
    sid = u32(ops.inert_components(e.obj)[0][1].fields["RigidBody"].fields["Shape"].id)
    v, t, _ = mesh_shape_geometry(d.obj(sid))
    return v, t


class StubWorld:
    """WorldCollision stand-in: a floor at floor_z everywhere, other geometry at a fixed distance."""
    def __init__(self, floor_z=None, other=float("inf")):
        self.floor_z, self.other = floor_z, other

    def ground_below(self, p, max_dist=50.0):
        return None if self.floor_z is None or p[2] < self.floor_z else p[2] - self.floor_z

    def distance(self, p, radius, exclude_key=None):
        return self.other


def test_world_filter():
    import struct
    pytest.importorskip("numpy")
    ident = struct.pack("<16f", 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1)
    el = G.generate(BOX_V, BOX_T)                     # ledges at z = 2
    assert len(G.world_filter(el, StubWorld(), ident, None)) == 4
    assert len(G.world_filter(el, StubWorld(floor_z=0.0), ident, None)) == 4          # 2 m drop: fine
    assert G.world_filter(el, StubWorld(floor_z=1.9), ident, None) == []              # a floor 10 cm below
    assert G.world_filter(el, StubWorld(other=0.02), ident, None) == []               # hang space taken


@needs_map
def test_built_systems_are_active():
    """Static buildings' systems are all Active; some elevators' aren't (the game ignores those edges until the
    elevator runs). Edges the editor writes must be active whatever system they were cloned from."""
    d = MapDocument(MAP)
    inactive = [g for e in classify(d, with_children=False) for g in G.systems(e.obj)
                if g.fields["Active"] == b"\x00" and g.fields["GuidanceObjects"]]
    assert inactive, "expected an inactive retail system (elevator) to clone from"
    ng = G.build_system(inactive[0], G.edges(inactive[0]), [0xF0FFFFF0, 0xF0FFFFF1])
    assert ng.fields["Active"] == b"\x01"
    assert G.template_system(d).fields["Active"] == b"\x01"


@needs_map
def test_points_fill_their_box():
    """CompressPoints are a 5 mm grid (offset binary), not normalized into the Partitioner box: decoded so, the box is
    the points' bounds (its Max is sometimes padded, its Min never)."""
    import struct
    d = MapDocument(MAP)
    fit = n = 0
    for e in classify(d, with_children=False):
        for g in G.systems(e.obj):
            pts = G.points(g)
            if not pts:
                continue
            lo = struct.unpack("<3f", g.fields["Partitioner"].obj.fields["Min"])
            fit += all(abs(lo[k] - min(p[k] for p in pts)) < 0.006 for k in range(3))
            n += 1
    assert n > 100 and fit / n > 0.9


@needs_map
def test_retail_edges_run_one_way():
    """Retail edges carry the top's and the wall's outward normals and run so that (p1 - p0) x n0 . n1 > 0."""
    d = MapDocument(MAP)
    n = 0
    for e in classify(d, with_children=False):
        for g in G.systems(e.obj):
            for x in G.edges(g):
                dv = G._sub(x.p1, x.p0)
                if math.hypot(*dv) > 0.05:
                    assert G._dot(G._cross(dv, x.n0), x.n1) > 0
                    n += 1
    assert n > 500
