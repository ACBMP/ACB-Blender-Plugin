"""Navigation data (NavMeshManager) decoding and crowd flow editing on retail maps."""
import math
import os

import pytest

from acbmap import flowedit as FE
from acbmap import ops
from acbmap.checks import new_problems
from acbmap.doc import MapDocument, codec
from acbmap.kinds import classify, component
from acbmap.navmesh import NavData, h, inside2d

MULTI = os.environ.get("ACB_MULTI", "/home/a/vbox/Assassin's Creed Brotherhood/multi")
MAP = os.path.join(MULTI, "DataPC_AC2MP_SanMarco.forge")
pytestmark = pytest.mark.skipif(not os.path.exists(MAP), reason="needs the ACB install")


def flow_state(d, uid):
    nd = NavData(d)
    o = d.obj(uid)
    cf = component(o, "CrowdFlow")
    r = cf.fields["MetaLinkRef"]
    ml = nd.metalink(h(r.fields["ManagerIndex"]), h(r.fields["MetaLinkIndex"]))
    tri = lambda t: (h(t.fields["ManagerIndex"]), h(t.fields["NavMeshIndex"]), h(t.fields["TriangleIndex"]))  # noqa
    wpr = lambda w: (h(w.fields["ManagerIndex"]), h(w.fields["WayPointIndex"]))  # noqa
    pts = FE.world_points(o)
    tris = [tri(t) for t in cf.fields["TriangleArray"]]
    wps = [wpr(w) for w in cf.fields["WayPointArray"]]
    assert tris == [tri(t) for t in ml.fields["TriangleList"]]
    assert wps == [wpr(w.fields["WayPoint"]) for w in ml.fields["ObjectWayPoints"]]
    for p, t, w in zip(pts, tris, wps):
        assert inside2d(nd.triangle(t), p, 1e-3)
        assert math.dist(p, nd.waypoint_pos(*w)) < 0.06
        assert nd.waypoints(w[0])[w[1]].fields["Active"] == b"\x01"
    return pts, ml


def test_managers_decode_and_roundtrip():
    d = MapDocument(MAP)
    for u in d.uids("NavMeshManager"):
        p = d.payload(u)
        assert codec().encode(codec().decode(p)) == p


def test_retail_flows_match_navigation_data():
    d = MapDocument(MAP)
    nd = NavData(d)
    assert (nd.cols, nd.ox, nd.oy, nd.cell) == (16, -256.0, -256.0, 32.0)
    for e in classify(d, with_children=False):
        if e.kind == "crowd_flow":
            flow_state(d, e.uid)


def longest_flow(d):
    return max((e.uid for e in classify(d, with_children=False) if e.kind == "crowd_flow"),
               key=lambda u: len(component(d.obj(u), "CrowdFlow").fields["NavFlowPointsLocal"]))


def test_same_points_change_nothing(tmp_path):
    from acbmap.cli import forge_contents
    d = MapDocument(MAP)
    uid = longest_flow(d)
    st = FE.set_flow_points(d, NavData(d), uid, FE.world_points(d.obj(uid)))
    assert st["moved"] == 0
    out = str(tmp_path / "same.forge")
    d.save(out)
    assert forge_contents(out) == forge_contents(MAP)


def test_move_insert_delete(tmp_path):
    d = MapDocument(MAP)
    uid = longest_flow(d)
    pts = FE.world_points(d.obj(uid))
    k = len(pts) // 2
    a, b = pts[k - 1], pts[k + 1]
    L = math.hypot(b[0] - a[0], b[1] - a[1])
    moved = (pts[k][0] - (b[1] - a[1]) / L * 1.5, pts[k][1] + (b[0] - a[0]) / L * 1.5, pts[k][2])
    mid = tuple((x + y) / 2 for x, y in zip(pts[1], pts[2]))
    new = pts[:2] + [mid] + pts[2:k] + [moved] + pts[k + 1:-1]
    st = FE.set_flow_points(d, NavData(d), uid, new)
    assert st == {"points": len(pts), "moved": 2, "new_waypoints": 0, "deactivated": 0}
    out = str(tmp_path / os.path.basename(MAP))
    d.save(out)
    d2 = MapDocument(out)
    got, ml = flow_state(d2, uid)
    assert all(math.dist(g, n) < 0.05 for g, n in zip(got, new))   # heights snapped to the navmesh
    assert 0 < len(ml.fields["ObjectWayPoints"][2].custom) <= 32
    assert new_problems(d2, MapDocument(MAP)) == []


def test_longer_flow_gets_new_waypoints(tmp_path):
    d = MapDocument(MAP)
    uid = longest_flow(d)
    pts = FE.world_points(d.obj(uid))
    new = pts[:1] + [tuple((x + y) / 2 for x, y in zip(pts[i], pts[i + 1])) for i in range(2)] + pts[1:]
    st = FE.set_flow_points(d, NavData(d), uid, new)
    assert st["new_waypoints"] == 2
    out = str(tmp_path / os.path.basename(MAP))
    d.save(out)
    got, _ml = flow_state(MapDocument(out), uid)
    assert len(got) == len(pts) + 2


def test_off_navmesh_rejected():
    d = MapDocument(MAP)
    uid = longest_flow(d)
    pts = FE.world_points(d.obj(uid))
    with pytest.raises(ops.EditError, match="navmesh"):
        FE.set_flow_points(d, NavData(d), uid, [(pts[0][0], pts[0][1], pts[0][2] + 40)] + pts[1:])
