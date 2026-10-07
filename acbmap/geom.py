"""Geometry helpers for decoded fields (raw little-endian bytes in the fastload tree).

Entity.GlobalMatrix is 16 floats, row-major with the translation in the last row (D3D row-vector convention), so
the Blender (column-vector) matrix is its transpose."""
from __future__ import annotations

import struct


def floats(b: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(b) // 4}f", b))


def pack_floats(v) -> bytes:
    return struct.pack(f"<{len(v)}f", *v)


def matrix_rows(m: bytes) -> list[list[float]]:
    """GlobalMatrix -> 4 rows as stored (translation in row 3)."""
    f = floats(m)
    return [f[0:4], f[4:8], f[8:12], f[12:16]]


def to_blender(m: bytes) -> list[list[float]]:
    """GlobalMatrix -> Blender-style 4x4 (column vectors, translation in column 3)."""
    r = matrix_rows(m)
    return [[r[j][i] for j in range(4)] for i in range(4)]


def from_blender(bm) -> bytes:
    """Blender-style 4x4 (rows of a mathutils.Matrix or nested lists) -> GlobalMatrix bytes."""
    rows = [list(bm[i]) for i in range(4)]
    return pack_floats([rows[j][i] for i in range(4) for j in range(4)])


def position(m: bytes) -> tuple[float, float, float]:
    return struct.unpack_from("<3f", m, 48)


def set_position(m: bytes, xyz) -> bytes:
    b = bytearray(m)
    struct.pack_into("<3f", b, 48, *xyz)
    return bytes(b)


def mesh_shape_geometry(o) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]], list[int]]:
    """MeshShape -> (vertices, triangles, per-triangle material index)."""
    verts = [struct.unpack("<3f", v) for v in o.fields["Vertices"]]
    idx = [int.from_bytes(i, "little") for i in o.fields["IndicesTriangles"]]
    tris = [tuple(idx[i:i + 3]) for i in range(0, len(idx) - 2, 3)]
    mats = [m[0] for m in o.fields["IndicesMaterial"]]
    return verts, tris, mats


def set_mesh_shape_geometry(o, verts, tris, mats) -> None:
    """Write geometry back to a MeshShape. The MOPP is dropped and its version zeroed, which makes ACB build its
    own at load (MeshShape::UpdateSDKObject, ACBMP.exe 0x016b3c40, trusts a stored MOPP only at version 5)."""
    if len(verts) > 0xFFFF:
        raise ValueError(f"{len(verts)} vertices: MeshShape indices are 16-bit")
    if len(mats) != len(tris):
        raise ValueError("one material index per triangle")
    o.fields["Vertices"] = [struct.pack("<3f", *v) for v in verts]
    o.fields["IndicesTriangles"] = [i.to_bytes(2, "little") for t in tris for i in t]
    o.fields["IndicesMaterial"] = [bytes([m]) for m in mats]
    o.fields["MoppCode"] = []
    o.fields["MoppOffsetAndScale"] = bytes(16)
    o.fields["MoppCodeVersionNumber"] = (0).to_bytes(4, "little")
    lo = [min(v[k] for v in verts) for k in range(3)] if verts else [0.0] * 3
    hi = [max(v[k] for v in verts) for k in range(3)] if verts else [0.0] * 3
    o.fields["MinLocalAABBox"] = pack_floats(lo + [1.0])
    o.fields["MaxLocalAABBox"] = pack_floats(hi + [1.0])
