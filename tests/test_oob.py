"""Out-of-bounds walls: read as polylines, regenerated to match retail, edited corners survive a save."""
import math
import os
import struct

import pytest

from acbmap import oob as O
from acbmap import visual as V
from acbmap.checks import new_problems
from acbmap.doc import MapDocument
from acbmap.kinds import classify, component

MULTI = os.environ.get("ACB_MULTI", "/home/a/vbox/Assassin's Creed Brotherhood/multi")
MAP = os.path.join(MULTI, "DataPC_AC2MP_Siena.forge")
pytestmark = pytest.mark.skipif(not os.path.exists(MAP), reason="needs the ACB install")


def oob(doc):
    return next(e for e in classify(doc) if e.kind == "out_of_bounds")


def sections(doc, e):
    oc = component(e.obj, "OutOfBoundsComponent")
    return [(struct.unpack("<4f", s.fields["GlobalPosition"])[:3], struct.unpack("<4f", s.fields["GlobalNormal"])[:3],
             struct.unpack("<2f", s.fields["Size"])) for s in oc.fields["Sections"]]


def test_regenerate_matches_retail():
    d = MapDocument(MAP)
    e = oob(d)
    oc = component(e.obj, "OutOfBoundsComponent")
    retail = sections(d, e)
    new = O.build_sections(oc.fields["Sections"][0], O.walls(d, e.key), O.section_hints(oc))
    assert len(new) == len(retail)
    got = [(struct.unpack("<4f", s.fields["GlobalPosition"])[:3], struct.unpack("<4f", s.fields["GlobalNormal"])[:3])
           for s in new]
    for p, n, _sz in retail:
        q = min(got, key=lambda g: math.dist(g[0], p))
        assert math.dist(q[0], p) < 0.05 and sum(a * b for a, b in zip(q[1], n)) > 0.99


def test_edit_corners_and_save(tmp_path):
    d = MapDocument(MAP)
    e = oob(d)
    ws = O.walls(d, e.key)
    w = ws[0]
    x, y, z = w.corners[3]
    w.corners[3] = (x + 6.0, y - 4.0, z)                         # move a corner
    a, b = w.corners[5], w.corners[6]
    w.corners.insert(6, ((a[0] + b[0]) / 2 + 3, (a[1] + b[1]) / 2, a[2]))   # add one
    w.heights.insert(6, w.heights[5])
    del w.corners[10], w.heights[10]                               # remove one
    res = O.write_walls(d, e.key, ws)
    out = str(tmp_path / os.path.basename(MAP))
    d.save(out)
    d2 = MapDocument(out)
    e2 = oob(d2)
    back = O.walls(d2, e2.key)[0]
    assert len(back.corners) == len(w.corners) and back.closed == w.closed
    assert all(math.dist(p, q) < 0.02 for p, q in zip(back.corners, w.corners))
    assert len(sections(d2, e2)) == res["sections"]
    g = V.mesh_geometry(d2, V.entity_meshes(d2, e2.obj)[0])
    assert g is not None and len(g.verts) > 4 * len(w.corners)
    assert new_problems(d2, MapDocument(MAP)) == []
