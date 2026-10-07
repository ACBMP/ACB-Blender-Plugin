"""Visual mesh / texture decoding on a retail map (read-only)."""
import os
import struct

import pytest

from acbmap import ops, visual as V
from acbmap.doc import MapDocument, u32
from acbmap.geom import mesh_shape_geometry
from acbmap.kinds import classify

MULTI = os.environ.get("ACB_MULTI", "/home/a/vbox/Assassin's Creed Brotherhood/multi")
MAP = os.path.join(MULTI, "DataPC_AC2MP_MtStMichel_dlc.forge")
pytestmark = pytest.mark.skipif(not os.path.exists(MAP), reason="needs the ACB install")


@pytest.fixture(scope="module")
def doc():
    return MapDocument(MAP)


def bbox(vs):
    return [min(v[k] for v in vs) for k in range(3)], [max(v[k] for v in vs) for k in range(3)]


def test_static_meshes_decode(doc):
    meshes = {m for e in classify(doc) for m in V.entity_meshes(doc, e.obj)}
    decoded = [g for g in (V.mesh_geometry(doc, m) for m in meshes) if g is not None]
    assert len(decoded) > 150
    for g in decoded:
        assert len(g.verts) == len(g.normals) == len(g.uvs)
        assert len(g.tris) == len(g.tri_material) and g.tris
        assert max(max(t) for t in g.tris) < len(g.verts)
        assert max(g.tri_material) < len(g.materials)


def test_visual_matches_collision(doc):
    """Dequantized visual geometry sits where the entity's collision is (same frame, same scale)."""
    deltas = []
    for e in classify(doc, with_children=False):
        shapes = list(ops.inert_components(e.obj))
        vis = V.entity_meshes(doc, e.obj)
        if len(shapes) != 1 or len(vis) != 1:
            continue
        sid = u32(shapes[0][1].fields["RigidBody"].fields["Shape"].id)
        g = V.mesh_geometry(doc, vis[0])
        if g is None or sid not in doc.info or doc.type_of(sid) != "MeshShape":
            continue
        (vlo, vhi), (clo, chi) = bbox(g.verts), bbox(mesh_shape_geometry(doc.obj(sid))[0])
        deltas += [abs(a - b) for a, b in zip(vlo + vhi, clo + chi)]
    # collision is a simplified hull, so single entities differ; a wrong scale or frame would shift them all
    deltas.sort()
    assert len(deltas) > 300
    assert deltas[len(deltas) // 2] < 0.1


def test_textures_are_dds(doc):
    n = 0
    for t in doc.uids("TextureMap")[:40]:
        b = V.texture_dds(doc, t)
        if b is None:
            continue
        magic, size, _flags, h, w = struct.unpack_from("<4s4I", b)
        c = doc.obj(t).fields["CompiledTextureMap"].obj
        assert magic == b"DDS " and size == 124
        assert (w, h) == (u32(c.fields["Width"]), u32(c.fields["Height"]))
        n += 1
    assert n > 20


def test_skinned_meshes_skipped(doc):
    skinned = [m for m in doc.uids("Mesh")
               if doc.obj(m).fields["CompiledMesh"].obj.fields["MeshData"].fields["VertexFormat"][0] == 0]
    assert skinned and all(V.mesh_geometry(doc, m) is None for m in skinned)


def box_input(materials):
    """A 2 m box split per face (4 corners each, own normal and uvs), two materials."""
    from acbmap.visual import MeshInput
    faces = [((0, 0, 1), [(0, 0, 2), (2, 0, 2), (2, 2, 2), (0, 2, 2)]),
             ((0, 0, -1), [(0, 0, 0), (0, 2, 0), (2, 2, 0), (2, 0, 0)]),
             ((1, 0, 0), [(2, 0, 0), (2, 2, 0), (2, 2, 2), (2, 0, 2)]),
             ((-1, 0, 0), [(0, 0, 0), (0, 0, 2), (0, 2, 2), (0, 2, 0)]),
             ((0, 1, 0), [(0, 2, 0), (0, 2, 2), (2, 2, 2), (2, 2, 0)]),
             ((0, -1, 0), [(0, 0, 0), (2, 0, 0), (2, 0, 2), (0, 0, 2)])]
    v, n, uv, t, tm = [], [], [], [], []
    for fi, (nn, quad) in enumerate(faces):
        b = len(v)
        v += quad
        n += [nn] * 4
        uv += [(0, 0), (1, 0), (1, 1), (0, 1)]
        t += [(b, b + 1, b + 2), (b, b + 2, b + 3)]
        tm += [fi % 2] * 2
    return MeshInput(v, n, uv, t, tm, materials)


def test_written_mesh_round_trips(tmp_path):
    import math
    from acbmap.checks import new_problems
    from acbmap.schema import type_name
    doc = MapDocument(MAP)   # edited: not the shared read-only fixture
    mats = [m for m in doc.uids("Material")][:2]
    src = box_input(mats)
    tmpl = next(e for e in classify(doc, with_children=False) if e.kind == "collision")
    m = tmpl.obj.fields["GlobalMatrix"]
    key = ops.new_collision(doc, m, src.verts, [t for t in src.tris], [0] * len(src.tris))
    ops.set_visual(doc, key, src)
    skey = ops.new_scenery(doc, m, src)
    out = tmp_path / os.path.basename(MAP)
    doc.save(str(out))
    r = MapDocument(str(out))
    for k in (key, skey):
        o = ops.element_obj(r, k)
        ms = V.entity_meshes(r, o)
        assert len(ms) == 1
        g = V.mesh_geometry(r, ms[0])
        assert len(g.tris) == 12 and sorted(g.materials) == sorted(mats)
        assert all(min(math.dist(p, q) for q in src.verts) < 1e-3 for p in g.verts)
        assert all(min(math.dist(nn, q) for q in src.normals) < 0.01 for nn in g.normals)
        assert all(abs(u - round(u)) < 1e-3 and abs(v - round(v)) < 1e-3 for u, v in g.uvs)
        bv = o.fields["BoundingVolume"].fields
        assert struct.unpack("<3f", bv["Max"]) == (2.0, 2.0, 2.0)
    assert not [c for c in ops.element_obj(r, skey).fields["Components"]
                if c.obj is not None and type_name(c.obj.type_hash) in ("InertComponent", "GuidanceSystem")]
    assert new_problems(r, MapDocument(MAP)) == []
