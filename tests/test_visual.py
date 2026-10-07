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
