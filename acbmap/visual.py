"""Visual meshes and their textures, read-only.

Entity -> Visual component -> Object: a LODSelector (LODDescs[0].Object is the most detailed Mesh) or a Mesh directly.
Mesh.CompiledMesh.MeshData holds the GPU buffers; each StandardPrimitive is a triangle list over the shared vertex
buffer with absolute indices, drawn with the material at the same index in Mesh.CompiledMeshMaterials.

Static vertex layouts (VertexFormat, stride), inferred from the retail maps:
  4 (20 bytes): short4 position | ubyte4 normal | ubyte4 tangent | short2 uv
  3 (24 bytes): short4 position | ubyte4 normal | ubyte4 tangent | d3dcolor | short2 uv
Position = xyz * |w| / 2^18, in the entity's frame: w is one value per mesh (its sign varies per vertex), and the
result matches each entity's collision MeshShape to within centimetres. Normal component = (b - 127.5) / 127.5, and
agrees with the stored triangle winding. UV = short / 2048 with V pointing down (D3D). VertexFormat 0 (32 bytes) is
skinned (crowd, characters) and isn't decoded.

Textures: CompiledTextureMap.Data is a plain mip chain (largest first), PixelFormat 0 = 32-bit uncompressed,
2/3 = DXT1, 4 = DXT3, 5 = DXT5. TextureSet.Maps slot 0 is the diffuse map, 1 the normal map, 2 the specular map.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

from .doc import MapDocument, u32
from .kinds import components

POS_SCALE = 1.0 / (1 << 18)
UV_SCALE = 1.0 / 2048
STATIC_FORMATS = {4: 20, 3: 24}   # VertexFormat -> stride; uv is the last 4 bytes
DDS_FOURCC = {2: b"DXT1", 3: b"DXT1", 4: b"DXT3", 5: b"DXT5"}


@dataclass
class MeshGeometry:
    verts: list[tuple[float, float, float]]
    normals: list[tuple[float, float, float]]
    uvs: list[tuple[float, float]]          # per vertex, V already flipped up (Blender convention)
    tris: list[tuple[int, int, int]]
    tri_material: list[int]                 # index into materials, per triangle
    materials: list[int] = field(default_factory=list)   # Material uids (0 = none)


def lod0_mesh(doc: MapDocument, target: int) -> int | None:
    """The most detailed Mesh behind a Visual.Object target (a LODSelector or a Mesh), or None."""
    seen = 0
    while target in doc.info and seen < 4:
        t = doc.type_of(target)
        if t == "Mesh":
            return target
        if t != "LODSelector":
            return None
        descs = doc.obj(target).fields["LODDescs"]
        if not descs:
            return None
        target = u32(descs[0].fields["Object"].id)
        seen += 1
    return None


def entity_meshes(doc: MapDocument, entity) -> list[int]:
    """LOD0 Mesh uids of an entity's Visual components."""
    out = []
    for name, c in components(entity):
        if name != "Visual":
            continue
        ref = c.fields.get("Object")
        if ref is None or not getattr(ref, "id", None):
            continue
        m = lod0_mesh(doc, u32(ref.id))
        if m is not None:
            out.append(m)
    return out


def mesh_geometry(doc: MapDocument, mesh_uid: int) -> MeshGeometry | None:
    """Decoded triangles of a static Mesh, or None for skinned/unknown vertex formats."""
    o = doc.obj(mesh_uid)
    if o is None:
        return None
    cm = o.fields["CompiledMesh"]
    cm = getattr(cm, "obj", None)
    if cm is None:
        return None
    md = cm.fields["MeshData"]
    fmt, stride = md.fields["VertexFormat"][0], md.fields["VertexStride"][0]
    if STATIC_FORMATS.get(fmt) != stride:
        return None
    vb = b"".join(md.fields["VertexBufferData"])
    ib = b"".join(md.fields["IndexBufferData"])
    n = len(vb) // stride
    if n == 0:
        return None
    if md.fields["IsIndexBuffer32bit"][0]:
        idx = struct.unpack(f"<{len(ib) // 4}I", ib)
    else:
        idx = struct.unpack(f"<{len(ib) // 2}H", ib)

    verts, normals, uvs = [], [], []
    uv_off = stride - 4
    for i in range(0, n * stride, stride):
        x, y, z, w = struct.unpack_from("<4h", vb, i)
        s = abs(w) * POS_SCALE
        verts.append((x * s, y * s, z * s))
        nx, ny, nz = vb[i + 8], vb[i + 9], vb[i + 10]
        normals.append(((nx - 127.5) / 127.5, (ny - 127.5) / 127.5, (nz - 127.5) / 127.5))
        u, v = struct.unpack_from("<2h", vb, i + uv_off)
        uvs.append((u * UV_SCALE, 1.0 - v * UV_SCALE))

    tris, tri_mat = [], []
    for pi, p in enumerate(md.fields["StandardPrimitives"]):
        if u32(p.fields["Type"]) != 1:       # 1 = triangle list, the only type in the retail maps
            continue
        start, count = u32(p.fields["StartIndex"]), u32(p.fields["PrimitiveCount"])
        for t in range(start, start + 3 * count, 3):
            a, b, c = idx[t:t + 3]
            if a >= n or b >= n or c >= n or a == b or b == c or a == c:
                continue
            tris.append((a, b, c))
            tri_mat.append(pi)
    mats = [u32(r.id) if r.id else 0 for r in o.fields["CompiledMeshMaterials"]]
    return MeshGeometry(verts, normals, uvs, tris, tri_mat, mats)


def material_texture(doc: MapDocument, material_uid: int, slot: int = 0) -> int | None:
    """TextureMap uid in a Material's TextureSet slot (0 diffuse, 1 normal, 2 specular), or None."""
    if material_uid not in doc.info or doc.type_of(material_uid) != "Material":
        return None
    m = doc.obj(material_uid)
    ts = u32(m.fields["TextureSet"].id) if m is not None and m.fields["TextureSet"].id else 0
    if ts not in doc.info:
        return None
    maps = doc.obj(ts).fields["Maps"]
    if slot >= len(maps) or not maps[slot].id:
        return None
    t = u32(maps[slot].id)
    return t if t in doc.info and doc.type_of(t) == "TextureMap" else None


def material_flags(doc: MapDocument, material_uid: int) -> dict:
    """The render flags the viewport cares about."""
    m = doc.obj(material_uid) if material_uid in doc.info else None
    if m is None:
        return {}
    f = m.fields
    return {"alpha_test": bool(f["AlphaTestEnabled"][0]), "two_sided": bool(f["TwoSided"][0]),
            "blend_mode": u32(f["BlendMode"])}


def texture_dds(doc: MapDocument, tex_uid: int) -> bytes | None:
    """A TextureMap as a DDS file (any loader, Blender included, reads it), or None for unsupported formats."""
    o = doc.obj(tex_uid)
    c = getattr(o.fields.get("CompiledTextureMap"), "obj", None) if o is not None else None
    if c is None:
        return None
    w, h, depth, mips, pf = (u32(c.fields[k]) for k in ("Width", "Height", "Depth", "NbMipMaps", "PixelFormat"))
    data = b"".join(c.fields["Data"])
    if depth != 1 or w == 0 or h == 0:
        return None
    mips = max(mips, 1)
    if pf in DDS_FOURCC:
        bpb = 8 if DDS_FOURCC[pf] == b"DXT1" else 16
        size = lambda w_, h_: max(1, (w_ + 3) // 4) * max(1, (h_ + 3) // 4) * bpb   # noqa: E731
        pixfmt = struct.pack("<II4s5I", 32, 0x4, DDS_FOURCC[pf], 0, 0, 0, 0, 0)
        flags, pitch = 0x1 | 0x2 | 0x4 | 0x1000 | 0x80000, size(w, h)
    elif pf == 0:
        size = lambda w_, h_: max(1, w_) * max(1, h_) * 4   # noqa: E731
        pixfmt = struct.pack("<II4s5I", 32, 0x41, b"\0\0\0\0", 32, 0xFF0000, 0xFF00, 0xFF, 0xFF000000)
        flags, pitch = 0x1 | 0x2 | 0x4 | 0x1000 | 0x8, w * 4
    else:
        return None
    need, ww, hh = 0, w, h
    for _ in range(mips):
        need += size(ww, hh)
        ww, hh = max(1, ww // 2), max(1, hh // 2)
    if len(data) < need:
        # not a plain mip chain: keep only the levels that are there
        mips, need, ww, hh = 0, 0, w, h
        while need + size(ww, hh) <= len(data):
            need += size(ww, hh)
            mips += 1
            ww, hh = max(1, ww // 2), max(1, hh // 2)
        if mips == 0:
            return None
    if mips > 1:
        flags |= 0x20000
    caps = 0x1000 | (0x400008 if mips > 1 else 0)
    header = struct.pack("<4s7I44x", b"DDS ", 124, flags, h, w, pitch, 0, mips) + pixfmt + \
        struct.pack("<5I", caps, 0, 0, 0, 0)
    return header + data[:need]
