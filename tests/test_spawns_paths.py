"""Spawn kinds and Escort path routing on a retail map: edits are saved, reopened and checked."""
import os

import pytest

from acbmap import flows as F
from acbmap import ops
from acbmap import spawns as SPW
from acbmap.doc import MapDocument, u32
from acbmap.geom import position
from acbmap.worlddata import WorldData

MULTI = os.environ.get("ACB_MULTI", "/home/a/vbox/Assassin's Creed Brotherhood/multi")
MAP = os.path.join(MULTI, "DataPC_AC2MP_MtStMichel_dlc.forge")
pytestmark = pytest.mark.skipif(not os.path.exists(MAP), reason="needs the ACB install")

IDENT_AT = (1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 12.5, -3.25, 20.0, 1)


def reopen(doc, tmp_path, tag="out"):
    d = tmp_path / tag
    d.mkdir()
    out = str(d / os.path.basename(MAP))
    doc.save(out)
    return MapDocument(out)


def mat(xyz):
    import struct
    return struct.pack("<16f", *IDENT_AT[:12], *xyz, 1)


def kinds_at(doc, xyz):
    return [(st, team) for key, st, team in SPW.spawns(doc)
            if all(abs(a - b) < 1e-3 for a, b in zip(position(ops.element_obj(doc, key).fields["GlobalMatrix"]), xyz))]


def chest_ids(doc):
    wd = WorldData(doc, MULTI)
    r = wd.get(2)
    return {u32(x.id) for x in r.obj.fields["chestSpawnPoints"]}


def test_add_team_spawn(tmp_path):
    d = MapDocument(MAP)
    before = SPW.counts(d)
    key = SPW.add_spawn(d, mat((12.5, -3.25, 20.0)), SPW.TEAM, 3)
    assert ops.owning_block(d, key[0]) is not None
    d2 = reopen(d, tmp_path)
    assert SPW.counts(d2)["Team 3"] == before["Team 3"] + 1
    assert kinds_at(d2, (12.5, -3.25, 20.0)) == [(SPW.TEAM, 3)]


def test_spawn_to_chest_and_back(tmp_path):
    d = MapDocument(MAP)
    key = next(k for k, st, _t in SPW.spawns(d) if st == SPW.STANDARD)
    n_chest = SPW.counts(d)["Chest"]
    SPW.set_spawn(d, key, SPW.CHEST, 0)
    acts = ops.element_obj(d, key).fields["DataLayerFilter"].fields["LayerActions"]
    assert [u32(a.fields["Layer"].id) for a in acts] == [SPW.CHEST_LAYER]
    WorldData(d).sync_chests()
    d2 = reopen(d, tmp_path, "a")
    assert SPW.counts(d2)["Chest"] == n_chest + 1
    assert key[0] in chest_ids(d2)
    SPW.set_spawn(d2, key, SPW.TEAM, 2)
    assert ops.element_obj(d2, key).fields["DataLayerFilter"].fields["LayerActions"] == []
    WorldData(d2, MULTI).sync_chests()
    d3 = reopen(d2, tmp_path, "b")
    assert SPW.counts(d3)["Chest"] == n_chest
    assert key[0] not in chest_ids(d3)
    assert SPW.spawn_info(ops.element_obj(d3, key)) == (SPW.TEAM, 2)


def test_team_index_range():
    d = MapDocument(MAP)
    key = SPW.spawns(d)[0][0]
    with pytest.raises(ops.EditError):
        SPW.set_spawn(d, key, SPW.TEAM, 5)


def test_retail_paths_are_flow_chains():
    d = MapDocument(MAP)
    fl = F.flows(d)
    for p in WorldData(d).vip_paths():
        u = [n["flow"] for n in p]
        assert F.gaps(fl, u) == []
        rebuilt = [u[0]]
        for t in u[1:]:
            add, ok = F.extend(fl, rebuilt, t)
            assert ok
            rebuilt += add
        assert rebuilt == u
        assert F.closing(fl, u) == []   # every retail path is a loop: last flow connects back to the first
        pts = F.path_points(fl, u)
        assert len(pts) == sum(len(fl[x].points) for x in u) + 1 and pts[-1] == pts[0]


def test_open_path_closes_through_flows():
    d = MapDocument(MAP)
    fl = F.flows(d)
    u = [n["flow"] for n in WorldData(d).vip_paths()[0]]
    cut = u[:-3]
    assert F.gaps(fl, cut) == [len(cut) - 1]   # only the closing step is broken
    add = F.closing(fl, cut)
    assert add and len(add) <= 3
    assert F.gaps(fl, cut + add) == [] and F.closing(fl, cut + add) == []


def test_routed_path_saves(tmp_path):
    d = MapDocument(MAP)
    fl = F.flows(d)
    wd = WorldData(d)
    paths = wd.vip_paths()
    first = paths[0][0]["flow"]
    far = max(fl, key=lambda u: (F.chain(fl, first, u) is not None, len(F.chain(fl, first, u) or [])))
    route = F.chain(fl, first, far)
    assert route and len(route) > 2 and F.gaps(fl, route) in ([], [len(route) - 1])
    route += F.closing(fl, route)
    assert F.gaps(fl, route) == []
    paths.append([{"flow": u, "spawn": i == 0, "checkpoint": i == 0} for i, u in enumerate(route)])
    wd.set_vip_paths(paths)
    d2 = reopen(d, tmp_path)
    got = WorldData(d2, MULTI).vip_paths()
    assert [n["flow"] for n in got[-1]] == route
    assert got[-1][0]["spawn"] and not got[-1][1]["spawn"]
