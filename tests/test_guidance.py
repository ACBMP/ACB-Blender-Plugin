"""Climb edges: the writer reproduces retail systems, the generator finds ledges, generated edges survive a save."""
import math
import os

import pytest

from acbmap import guidance as G, ops
from acbmap.checks import new_problems
from acbmap.doc import MapDocument
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
