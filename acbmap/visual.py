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

The fourth bytes of the normal and tangent vary per vertex in most meshes (baked data); meshes where they are
constant mostly use 255, which is what the writer uses.

Textures: CompiledTextureMap.Data is a plain mip chain (largest first), PixelFormat 0 = 32-bit uncompressed,
2/3 = DXT1, 4 = DXT3, 5 = DXT5. TextureSet.Maps slot 0 is the diffuse map, 1 the normal map, 2 the specular map.

Normal maps are tangent space, DirectX convention: the vertex tangent (ubyte4 after the normal) follows +u, and the
bitangent, cross(normal, tangent) times -sign(position w), follows +v as stored (down the texture), so green points
down the image (inverted for Blender). DXT1 normal maps hold x, y, z in RGB; DXT5 and uncompressed ones hold x in
alpha and y in green (DXT5 copies y to blue, red is 255), with z = sqrt(1 - x^2 - y^2).
"""
from __future__ import annotations

import copy
import math
import struct
from dataclasses import dataclass, field

from .doc import MapDocument, idb, u32
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


def normal_map_layout(doc: MapDocument, tex_uid: int) -> str:
    """'rgb' (x, y, z in RGB) or 'ag' (x in alpha, y in green, z derived): see the module docstring."""
    c = doc.obj(tex_uid).fields["CompiledTextureMap"].obj
    return "rgb" if u32(c.fields["PixelFormat"]) in (2, 3) else "ag"


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


# ------------------------------------------------------------------ write --

@dataclass
class MeshInput:
    """Triangles to encode, one entry per vertex corner already split (a vertex has one normal and one uv)."""
    verts: list[tuple[float, float, float]]
    normals: list[tuple[float, float, float]]
    uvs: list[tuple[float, float]]          # Blender convention (V up)
    tris: list[tuple[int, int, int]]
    tri_material: list[int]                 # index into materials
    materials: list[int]                    # Material uids


MAX_UV = 32767 / 2048


def _tangents(mi: MeshInput):
    """Per-vertex tangent (along +u) and the handedness sign to store in position w (see the module docstring)."""
    n = len(mi.verts)
    tan = [[0.0, 0.0, 0.0] for _ in range(n)]
    bit = [[0.0, 0.0, 0.0] for _ in range(n)]
    for a, b, c in mi.tris:
        pa, pb, pc = mi.verts[a], mi.verts[b], mi.verts[c]
        # D3D v (down the texture) = 1 - Blender v
        ua, va = mi.uvs[a][0], 1 - mi.uvs[a][1]
        ub, vb = mi.uvs[b][0], 1 - mi.uvs[b][1]
        uc, vc = mi.uvs[c][0], 1 - mi.uvs[c][1]
        e1 = [pb[k] - pa[k] for k in range(3)]
        e2 = [pc[k] - pa[k] for k in range(3)]
        d1u, d1v, d2u, d2v = ub - ua, vb - va, uc - ua, vc - va
        det = d1u * d2v - d2u * d1v
        if abs(det) < 1e-12:
            continue
        r = 1.0 / det
        su = [(e1[k] * d2v - e2[k] * d1v) * r for k in range(3)]
        sv = [(e2[k] * d1u - e1[k] * d2u) * r for k in range(3)]
        for i in (a, b, c):
            for k in range(3):
                tan[i][k] += su[k]
                bit[i][k] += sv[k]
    out_t, out_s = [], []
    for i in range(n):
        nn = mi.normals[i]
        t = tan[i]
        d = sum(t[k] * nn[k] for k in range(3))
        t = [t[k] - nn[k] * d for k in range(3)]                    # Gram-Schmidt against the normal
        L = math.sqrt(sum(x * x for x in t))
        if L < 1e-9:   # no usable uv gradient: any vector perpendicular to the normal
            ref = (1.0, 0.0, 0.0) if abs(nn[0]) < 0.9 else (0.0, 1.0, 0.0)
            t = [ref[1] * nn[2] - ref[2] * nn[1], ref[2] * nn[0] - ref[0] * nn[2], ref[0] * nn[1] - ref[1] * nn[0]]
            L = math.sqrt(sum(x * x for x in t)) or 1.0
        t = [x / L for x in t]
        c = [nn[1] * t[2] - nn[2] * t[1], nn[2] * t[0] - nn[0] * t[2], nn[0] * t[1] - nn[1] * t[0]]
        # bitangent = cross(N, T) * -sign(w) must follow +v (D3D): w negative when cross(N, T) already does
        out_s.append(-1 if sum(c[k] * bit[i][k] for k in range(3)) >= 0 else 1)
        out_t.append(t)
    return out_t, out_s


def _b(x: float) -> int:
    return max(0, min(255, round((x + 1) * 127.5)))


def encode_static(mi: MeshInput):
    """Vertex format 4 buffers for mi: (vertex bytes, index bytes, primitives [(min index, vertex count, start
    index, triangle count)] in material order, vertex order) -- vertices are regrouped so each material's range is
    contiguous."""
    if not mi.tris:
        raise ValueError("no triangles")
    by_mat: dict[int, list[int]] = {}
    for ti, m in enumerate(mi.tri_material):
        by_mat.setdefault(m, []).append(ti)
    tan, sign = _tangents(mi)
    extent = max(abs(c) for v in mi.verts for c in v)
    w = 8 * max(1, math.ceil(extent + 1e-6))
    if w > 32767:
        raise ValueError("mesh is larger than 4 km")
    scale = (1 << 18) / w
    u_off = math.floor(min(u for u, _ in mi.uvs)) if mi.uvs else 0
    v_off = math.floor(min(1 - v for _, v in mi.uvs)) if mi.uvs else 0
    vb, ib, prims = bytearray(), [], []
    for mat in range(len(mi.materials)):
        tris = by_mat.get(mat, [])
        if not tris:
            continue
        remap: dict[int, int] = {}
        base = len(vb) // 20
        start = len(ib)
        for ti in tris:
            for i in mi.tris[ti]:
                if i not in remap:
                    remap[i] = base + len(remap)
                    x, y, z = (round(c * scale) for c in mi.verts[i])
                    u = (mi.uvs[i][0] - u_off) * 2048
                    v = ((1 - mi.uvs[i][1]) - v_off) * 2048
                    if abs(u) > 32767 or abs(v) > 32767:
                        raise ValueError(f"uv span over {MAX_UV:.0f} tiles")
                    nn, t = mi.normals[i], tan[i]
                    vb += struct.pack("<4h4B4B2h", x, y, z, w * sign[i], _b(nn[0]), _b(nn[1]), _b(nn[2]), 255,
                                      _b(t[0]), _b(t[1]), _b(t[2]), 255, round(u), round(v))
                ib.append(remap[i])
        if base + len(remap) > 0xFFFF:
            raise ValueError(f"{base + len(remap)} vertices: at most 65535 per mesh")
        prims.append((mat, base, len(remap), start, len(tris)))
    return bytes(vb), struct.pack(f"<{len(ib)}H", *ib), prims


def static_mesh_template(doc: MapDocument) -> int:
    """A retail static (VertexFormat 4) Mesh of this map to clone the field layout from."""
    for u in doc.uids("Mesh"):
        o = doc.obj(u)
        cm = getattr(o.fields.get("CompiledMesh"), "obj", None) if o is not None else None
        if cm is None or not cm.fields["InstancingData"]:
            continue
        md = cm.fields["MeshData"]
        if (md.fields["VertexFormat"][0], md.fields["VertexStride"][0]) == (4, 20) and md.fields["StandardPrimitives"] \
                and not o.fields["Bones"] and not o.fields["UseFakeMeshDrawPrimMasking"][0]:
            return u
    raise ValueError("no static mesh in this map to use as a template")


def build_mesh(doc: MapDocument, mi: MeshInput, clone):
    """A new Mesh root holding mi (not yet added to the document). clone(obj) deep-copies with fresh ids."""
    tmpl = static_mesh_template(doc)
    src = doc.root(tmpl)
    o = clone(src.obj)
    vb, ib, prims = encode_static(mi)
    cm = o.fields["CompiledMesh"].obj
    md = cm.fields["MeshData"]
    proto_prim = md.fields["StandardPrimitives"][0]
    proto_inst = cm.fields["InstancingData"][0]
    md.fields["IsIndexBuffer32bit"] = b"\x00"
    md.fields["VertexFormat"] = b"\x04"
    md.fields["VertexStride"] = bytes([20])
    std, shadow, inst = [], [], []
    for k, (mat, base, nv, start, ntri) in enumerate(prims):
        p = copy.deepcopy(proto_prim)
        p.fields.update(MinIndex=base.to_bytes(4, "little"), NumVertices=nv.to_bytes(4, "little"),
                        StartIndex=start.to_bytes(4, "little"), PrimitiveCount=ntri.to_bytes(4, "little"),
                        Type=(1).to_bytes(4, "little"), IsUsingDepthOnlyBuffers=bytes(4))
        std.append(p)
        shadow.append(copy.deepcopy(p))
        d = copy.deepcopy(proto_inst)
        d.fields["ShadowCaster"] = b"\x01"
        d.fields["NumBones"] = b"\x00"
        d.fields["SubMeshIndex"] = bytes([k])
        d.fields["NumVertices"] = nv.to_bytes(2, "little")
        d.fields["Material"] = type(d.fields["Material"])(1, idb(mi.materials[mat]))
        inst.append(d)
    md.fields["StandardPrimitives"] = std
    md.fields["ShadowPrimitives"] = shadow
    md.fields["VertexBufferData"] = [bytes([b]) for b in vb]
    md.fields["IndexBufferData"] = [bytes([b]) for b in ib]
    for k in ("PS3DepthOnlyVertexData", "PS3DepthOnlyIndexData", "EdgeData", "NonEdgeData"):
        md.fields[k] = []
    cm.fields["InstancingData"] = inst
    ref = o.fields["CompiledMeshMaterials"][0]
    o.fields["CompiledMeshMaterials"] = [type(ref)(ref.tag, ref.extra, idb(mi.materials[m])) for m, *_ in prims]
    o.fields["SubMeshes"] = []
    o.fields["Bones"] = []
    for k in ("FakeMeshGridIndex", "FakeMeshGridCount"):
        o.fields[k] = bytes(len(o.fields[k]))
    return type(src)(src.pre_header, src.status, o), [mi.materials[m] for m, *_ in prims]


def default_material(doc: MapDocument) -> int:
    """The map's most used static-mesh material that has a diffuse texture: the fallback for new meshes."""
    import collections
    c = collections.Counter()
    for u in doc.uids("Mesh"):
        o = doc.obj(u)
        if o is None or o.fields["Bones"]:
            continue
        for r in o.fields["CompiledMeshMaterials"]:
            if r.id and u32(r.id) in doc.info:
                c[u32(r.id)] += 1
    for m, _n in c.most_common():
        if material_texture(doc, m, 0) is not None:
            return m
    raise ValueError("no textured material in this map")
