"""Headless edit tests on a retail map: every edit is saved, the result reopened and checked."""
import os

import pytest

from acbmap import ops
from acbmap.doc import MapDocument, u32
from acbmap.geom import mesh_shape_geometry, position, set_mesh_shape_geometry, set_position
from acbmap.kinds import classify, component, group_children
from acbmap.worlddata import WorldData

MULTI = os.environ.get("ACB_MULTI", "/home/a/vbox/Assassin's Creed Brotherhood/multi")
MAP = os.path.join(MULTI, "DataPC_AC2MP_MtStMichel_dlc.forge")
pytestmark = pytest.mark.skipif(not os.path.exists(MAP), reason="needs the ACB install")


def reopen(doc, tmp_path, tag="out"):
    d = tmp_path / tag
    d.mkdir()
    out = str(d / os.path.basename(MAP))
    doc.save(out)
    return MapDocument(out)


def first(doc, kind):
    return next(e for e in classify(doc) if e.kind == kind)


def count(doc, kind):
    return sum(e.kind == kind for e in classify(doc))


def test_noop_save_identical(tmp_path):
    from acbmap.cli import forge_contents
    d = MapDocument(MAP)
    out = str(tmp_path / "rt.forge")
    assert d.save(out) == []
    assert forge_contents(out) == forge_contents(MAP)


def test_move_spawn(tmp_path):
    d = MapDocument(MAP)
    e = first(d, "spawn")
    ops.set_matrix(d, e.key, set_position(e.obj.fields["GlobalMatrix"], (1.5, 2.5, 30.0)))
    assert position(reopen(d, tmp_path).obj(e.uid).fields["GlobalMatrix"]) == (1.5, 2.5, 30.0)


def test_add_and_delete_spawn(tmp_path):
    d = MapDocument(MAP)
    n0 = count(d, "spawn")
    e = first(d, "spawn")
    new = ops.duplicate(d, e.key, set_position(e.obj.fields["GlobalMatrix"], (0.0, 0.0, 50.0)))
    d2 = reopen(d, tmp_path, "add")
    assert count(d2, "spawn") == n0 + 1
    ne = next(k for k in classify(d2) if k.uid == new[0])
    assert ne.block is not None                            # activated
    assert d2.entry_of(new[0]) == d2.entry_of(ne.block)    # lives in its block's own entry
    assert component(ne.obj, "MultiSpawnPlayerComponent").id != component(e.obj, "MultiSpawnPlayerComponent").id
    ops.delete(d2, new)
    assert count(reopen(d2, tmp_path, "del"), "spawn") == n0


def test_spawn_field(tmp_path):
    d = MapDocument(MAP)
    e = first(d, "spawn")
    msp = component(e.obj, "MultiSpawnPlayerComponent")
    i = next(j for j, p in enumerate(e.obj.fields["Components"]) if p.obj is msp)
    ops.set_field(d, e.key, ["Components", i, "TeamIndex"], b"\x01")
    assert component(reopen(d, tmp_path).obj(e.uid), "MultiSpawnPlayerComponent").fields["TeamIndex"] == b"\x01"


def test_move_group_moves_children(tmp_path):
    d = MapDocument(MAP)
    g = first(d, "chase_breaker")
    before = [position(c.fields["GlobalMatrix"]) for c in group_children(g.obj)]
    x, y, z = position(g.obj.fields["GlobalMatrix"])
    ops.set_matrix(d, g.key, set_position(g.obj.fields["GlobalMatrix"], (x + 10, y, z)))
    after = [position(c.fields["GlobalMatrix"]) for c in group_children(reopen(d, tmp_path).obj(g.uid))]
    assert len(after) == len(before)
    for a, b in zip(before, after):
        assert b == pytest.approx((a[0] + 10, a[1], a[2]), abs=1e-3)


def test_collision_vertex_edit(tmp_path):
    d = MapDocument(MAP)
    sid = d.uids("MeshShape")[0]
    o = d.obj(sid)
    v, t, m = mesh_shape_geometry(o)
    v[0] = (v[0][0], v[0][1], v[0][2] + 1.0)
    set_mesh_shape_geometry(o, v, t, m)
    d.touch(sid)
    o2 = reopen(d, tmp_path).obj(sid)
    v2, t2, m2 = mesh_shape_geometry(o2)
    assert v2[0] == pytest.approx(v[0]) and t2 == t and m2 == m
    assert o2.fields["MoppCodeVersionNumber"] == b"\0\0\0\0"


def test_escort_and_chests(tmp_path):
    d = MapDocument(MAP)
    w = WorldData(d, MULTI)
    paths = w.vip_paths()
    paths[0] = paths[0][:-1]
    w.set_vip_paths(paths)
    chest = first(d, "chest_spawn")
    ops.duplicate(d, chest.key, set_position(chest.obj.fields["GlobalMatrix"], (5.0, 5.0, 5.0)))
    n = w.sync_chests()
    w2 = WorldData(reopen(d, tmp_path), MULTI)
    assert [len(p) for p in w2.vip_paths()] == [len(p) for p in paths]
    assert len(w2.get(2).obj.fields["chestSpawnPoints"]) == n == count(d, "chest_spawn")


def test_delete_refuses_referenced(tmp_path):
    d = MapDocument(MAP)
    w = WorldData(d, MULTI)
    flow = w.vip_paths()[0][0]["flow"]
    w.set_vip_paths(w.vip_paths())      # the override entry now references the flow entity
    with pytest.raises(ops.EditError):
        ops.delete(d, (flow, -1))
