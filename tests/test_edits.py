"""Headless edit tests on a retail map: every edit is saved, the result reopened and checked."""
import os

import pytest

from acbmap import ops
from acbmap.doc import MapDocument, u32
from acbmap.geom import mesh_shape_geometry, position, set_mesh_shape_geometry, set_position
from acbmap.kinds import classify, component, components, group_children
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


def test_delete_many(tmp_path):
    d = MapDocument(MAP)
    w = WorldData(d, MULTI)
    flow = w.vip_paths()[0][0]["flow"]
    w.set_vip_paths(w.vip_paths())      # the override entry now references the flow entity
    g = next(e for e in classify(d, False) if len(group_children(e.obj)) >= 3)
    names = [u32(c.id) for c in group_children(g.obj)]
    spawn = first(d, "spawn").uid
    done, refused = ops.delete_many(d, [(g.uid, 0), (g.uid, 2), (spawn, -1), (flow, -1)])
    assert set(done) == {(g.uid, 0), (g.uid, 2), (spawn, -1)} and list(refused) == [(flow, -1)]
    d2 = reopen(d, tmp_path)
    left = [u32(c.id) for c in group_children(d2.obj(g.uid))]
    assert left == [names[1]] + names[3:]
    assert spawn not in d2.info and flow in d2.info


def test_compound_member_move_dissolves(tmp_path):
    d = MapDocument(MAP)
    comps = ops.compounds(d)
    if not comps:
        pytest.skip("no compound collision on this map")
    m, members = next(iter(comps.items()))
    idx = ops._sub_index(d)
    key = idx[members[0]]
    o = ops.element_obj(d, key)
    mm = bytearray(o.fields["GlobalMatrix"])
    mm[48:52] = (int.from_bytes(mm[48:52], "little") ^ 1).to_bytes(4, "little")
    ops.set_matrix(d, key, bytes(mm))
    d2 = reopen(d, tmp_path)
    assert m not in d2.info
    from acbmap.checks import check_compounds
    assert check_compounds(d2) == []


def test_clear_scenery(tmp_path):
    d = MapDocument(MAP)
    spawns = count(d, "spawn")
    r = ops.clear_scenery(d)
    # new geometry still has its templates on the cleared map
    cube = [(0, 0, 0), (2, 0, 0), (2, 2, 0), (0, 2, 0), (0, 0, 2), (2, 0, 2), (2, 2, 2), (0, 2, 2)]
    tris = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5), (2, 3, 7),
            (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    m = d.obj(d.uids("Entity")[0]).fields["GlobalMatrix"]
    nk = ops.new_collision(d, set_position(m, (1.0, 2.0, 3.0)), cube, tris, [0] * len(tris))
    d2 = reopen(d, tmp_path)
    assert r["removed"] > 100 and count(d2, "spawn") == spawns and r["templates"]
    # collision left = the new cube, the never-activated templates, and parts of kept gameplay groups (chase-breaker
    # door frames, hay carts)
    from acbmap.kinds import block_membership
    active = block_membership(d2)
    roots = {e.uid for e in classify(d2) if e.kind == "collision" and e.child < 0}
    assert roots - set(r["templates"]) - set(r["hollowed"]) == {nk[0]}
    # what the navmeshes name is hollowed, not removed: no dangling handle in a root acbmap can't rewrite
    pinned = ops.opaque_references(MapDocument(MAP))
    assert r["hollowed"] and all(u in d2.info for u in r["hollowed"])
    gone = set(MapDocument(MAP).info) - set(d2.info)
    assert not gone & pinned
    # parked, not stripped: intact, PARK_DEPTH below (the game crashes loading navmesh-named elements without their
    # visual/collision)
    src = MapDocument(MAP)
    for u in r["hollowed"]:
        before, after = ops.element_obj(src, (u, -1)), ops.element_obj(d2, (u, -1))
        assert position(after.fields["GlobalMatrix"])[2] == pytest.approx(
            position(before.fields["GlobalMatrix"])[2] - ops.PARK_DEPTH, abs=1e-3)
        assert [n for n, _ in components(after)] == [n for n, _ in components(before)]
    assert not set(r["templates"]) & set(active) and nk[0] in active
    assert not ops.compounds(d2)
    from acbmap.checks import new_problems
    assert new_problems(d2, MapDocument(MAP)) == []


def test_delete_many_matches_delete(tmp_path):
    d = MapDocument(MAP)
    sp = [e for e in classify(d) if e.kind == "spawn"][:5]
    n = count(d, "spawn")
    res = ops.delete_many(d, [e.key for e in sp])
    assert all(v is None for v in res.values())
    d2 = reopen(d, tmp_path)
    assert count(d2, "spawn") == n - 5


def test_new_ids_avoid_engine_runtime_range(tmp_path):
    """Objects the engine creates at runtime are numbered from 0xF0000000 (ObjectManager): a forge object there
    never shows up. Copies get editor-range ids, and older saves with runtime-range ids are renumbered."""
    d = MapDocument(MAP)
    sp = first(d, "spawn")
    nk = ops.duplicate(d, sp.key)
    from anvilforge.fastload import walk
    ids = [u32(o.id) for o in walk(ops.element_obj(d, nk))]
    assert all(ops.is_editor_id(i) for i in ids if i)
    # simulate an old save: move the copy into the runtime range, then migrate
    old = {i: 0xF00F0000 + k for k, i in enumerate(sorted(set(ids) - {0}))}
    r = d.root(nk[0])
    ops._remap_ids(r.obj, old)
    for b in d.uids("GridCellDataBlock"):
        o = d.obj(b)
        if any(u32(x.id) == nk[0] for x in o.fields["Objects"]):
            ops._remap_ids(o, old)
            d.touch(b)
    fn, pos = d.where[nk[0]][0]
    d.remove_root(nk[0])
    d.add_root(fn, r, "old_copy", pos)
    assert ops.migrate_runtime_ids(d) == len(old)
    d2 = reopen(d, tmp_path)
    assert not [u for u in d2.info if u >= ops.RUNTIME_ID_BASE]
    assert count(d2, "spawn") == count(MapDocument(MAP), "spawn") + 1
    from acbmap.checks import new_problems
    assert new_problems(d2, MapDocument(MAP)) == []
