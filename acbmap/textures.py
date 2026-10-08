"""New textures and materials from images.

A map Material binds its textures twice: TextureSet.Maps (slot 0 diffuse, 1 normal, 2 specular) and the
MaterialTemplate's dynamic parameters (Texture refs), so a new material is a clone of a map material whose TextureSet,
both bindings and textures are replaced. Textures are written as DXT1 (PixelFormat 2, the most common retail format;
for normal maps x, y, z in RGB) with a full mip chain down to 1x1 (retail: Data = the plain chain, smallest blocks
padded to 4x4). About half of all retail TextureMaps are stored inside another entry, so new ones go with their
material into the always-loaded grid cell's entry, where any mesh can use them.
"""
from __future__ import annotations

import numpy as np
from anvilforge.fastload import Ref, Root

from .doc import MapDocument, idb, u32

PF_DXT1 = 2
MAX_SIZE = 1024


def mip_chain(img: np.ndarray) -> list[np.ndarray]:
    """img: HxWx4 uint8, power-of-two sides -> [img, img/2, ..., 1x1] (2x2 box filter)."""
    out = [img]
    a = img.astype(np.float32)
    while a.shape[0] > 1 or a.shape[1] > 1:
        h, w = a.shape[:2]
        if h > 1:
            a = (a[0::2] + a[1::2]) / 2
        if w > 1:
            a = (a[:, 0::2] + a[:, 1::2]) / 2
        out.append(np.clip(a + 0.5, 0, 255).astype(np.uint8))
    return out


def _to565(c: np.ndarray) -> np.ndarray:
    """Nearest 5:6:5 levels (truncating would bias every colour up to 8 levels)."""
    c = np.clip(c, 0, 255)
    r, g, b = (np.rint(c[..., k] * m / 255).astype(np.int32) for k, m in ((0, 31), (1, 63), (2, 31)))
    return (r << 11) | (g << 5) | b


def _from565(v: np.ndarray) -> np.ndarray:
    r, g, b = (v >> 11) & 31, (v >> 5) & 63, v & 31
    return np.stack([(r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)], -1).astype(np.float32)


def dxt1(img: np.ndarray) -> bytes:
    """One mip level (HxWx3+ uint8) as DXT1 blocks (opaque 4-colour mode). Endpoints: the block's colours projected on
    their principal axis (extremes); levels under 4x4 are padded by edge replication."""
    h, w = img.shape[:2]
    ph, pw = max(4, -(-h // 4) * 4), max(4, -(-w // 4) * 4)
    a = np.pad(img[..., :3], ((0, ph - h), (0, pw - w), (0, 0)), mode="edge").astype(np.float32)
    blocks = a.reshape(ph // 4, 4, pw // 4, 4, 3).transpose(0, 2, 1, 3, 4).reshape(-1, 16, 3)
    mean = blocks.mean(1, keepdims=True)
    d = blocks - mean
    cov = np.einsum("nki,nkj->nij", d, d)
    axis = np.ones((len(blocks), 3), np.float32)
    for _ in range(8):                                   # power iteration for the principal axis
        axis = np.einsum("nij,nj->ni", cov, axis)
        axis /= np.maximum(np.linalg.norm(axis, axis=1, keepdims=True), 1e-6)
    t = np.einsum("nki,ni->nk", d, axis)
    hi = np.clip(mean[:, 0] + axis * t.max(1, keepdims=True), 0, 255)
    lo = np.clip(mean[:, 0] + axis * t.min(1, keepdims=True), 0, 255)
    c0, c1 = _to565(hi), _to565(lo)
    swap = c0 < c1
    c0, c1 = np.where(swap, c1, c0), np.where(swap, c0, c1)
    p0, p1 = _from565(c0), _from565(c1)
    pal = np.stack([p0, p1, (2 * p0 + p1) / 3, (p0 + 2 * p1) / 3], 1)          # n x 4 x 3
    dist = ((blocks[:, :, None, :] - pal[:, None, :, :]) ** 2).sum(-1)       # n x 16 x 4
    idx = dist.argmin(-1).astype(np.uint32)
    idx[c0 == c1] = 0                                     # equal endpoints = 3-colour mode: index 0 is the colour
    bits = (idx << (2 * np.arange(16, dtype=np.uint32))).sum(1, dtype=np.uint64).astype(np.uint32)
    out = np.zeros(len(blocks), dtype=[("c0", "<u2"), ("c1", "<u2"), ("i", "<u4")])
    out["c0"], out["c1"], out["i"] = c0, c1, bits
    return out.tobytes()


def fit_size(h: int, w: int, max_size: int = MAX_SIZE) -> tuple[int, int]:
    """Power-of-two size (each side the nearest power of two, at most max_size, at least 4)."""
    p = lambda n: int(min(max_size, max(4, 2 ** round(np.log2(max(n, 1))))))   # noqa: E731
    return p(h), p(w)


def texture_template(doc: MapDocument, normal: bool) -> int:
    """A decoded DXT1 TextureMap of this map to clone (a normal map or a colour map)."""
    for t in doc.uids("TextureMap"):
        o = doc.obj(t)
        if o is None:
            continue
        c = o.fields["CompiledTextureMap"].obj
        if u32(c.fields["PixelFormat"]) == PF_DXT1 and u32(c.fields["Depth"]) == 1 and \
                (u32(o.fields["MapType"]) == 1) == normal:
            return t
    raise ValueError("no DXT1 texture in this map to use as a template")


def new_texture(doc: MapDocument, img: np.ndarray, fname: str, name: str, normal: bool = False) -> int:
    """Add a DXT1 TextureMap made from img (HxWx4 uint8, top row first, power-of-two sides) to entry fname."""
    from .ops import clone_tree
    h, w = img.shape[:2]
    if h & (h - 1) or w & (w - 1):
        raise ValueError(f"texture sides must be powers of two, not {w}x{h}")
    t = texture_template(doc, normal)
    r = doc.root(t)
    o = clone_tree(doc, r.obj)
    levels = mip_chain(img)
    data = b"".join(dxt1(m) for m in levels)
    c = o.fields["CompiledTextureMap"].obj
    for x in (o, c):
        x.fields["Width"], x.fields["Height"] = idb(w), idb(h)
        x.fields["NbMipMaps"], x.fields["PixelFormat"] = idb(len(levels)), idb(PF_DXT1)
    c.fields["Data"] = [data[i:i + 1] for i in range(len(data))]
    return doc.add_root(fname, Root(r.pre_header, r.status, o), name)


def material_template(doc: MapDocument) -> int:
    """A map material to clone: opaque (BlendMode 0), diffuse + normal map only, each also bound as a Texture
    parameter of its template; the most used such material wins."""
    from collections import Counter
    from .visual import material_texture
    use = Counter()
    for u in doc.uids("Mesh"):
        o = doc.obj(u)
        if o is None or o.fields["Bones"]:
            continue
        for x in o.fields["CompiledMeshMaterials"]:
            if x.id:
                use[u32(x.id)] += 1
    for m, _n in use.most_common():
        mo = doc.obj(m) if m in doc.info and doc.type_of(m) == "Material" else None
        if mo is None or u32(mo.fields["BlendMode"]) != 0:
            continue
        d, n = material_texture(doc, m, 0), material_texture(doc, m, 1)
        if d is None or n is None or material_texture(doc, m, 2) is not None:
            continue
        bound = {u32(p[3].fields["Texture"].id) for p in mo.dyn or []
                 if hasattr(p[3], "fields") and "Texture" in p[3].fields}
        if bound == {d, n}:
            return m
    raise ValueError("no plain diffuse + normal material in this map to use as a template")


def flat_normal(doc: MapDocument, fname: str) -> int:
    """The shared flat (0, 0, 1) normal map of entry fname, made once."""
    memo = doc.__dict__.setdefault("_flat_normal", {})
    if fname not in memo:
        img = np.empty((64, 64, 4), np.uint8)
        img[:] = (128, 128, 255, 255)
        memo[fname] = new_texture(doc, img, fname, "ACBEdit_FlatNormal", normal=True)
    return memo[fname]


def new_material(doc: MapDocument, img: np.ndarray, name: str, fname: str | None = None) -> int:
    """A new opaque Material showing img (HxWx4 uint8, top row first, power-of-two sides; alpha ignored) with a flat
    normal map, stored with its TextureSet and textures in entry fname (default: the always-loaded grid cell's).
    Returns the Material uid."""
    from .ops import clone_tree, top_block
    from .visual import material_texture
    fname = fname or doc.entry_of(top_block(doc))
    m = material_template(doc)
    mr = doc.root(m)
    ts = u32(mr.obj.fields["TextureSet"].id)
    tr = doc.root(ts)
    old_d, old_n = material_texture(doc, m, 0), material_texture(doc, m, 1)
    tex = new_texture(doc, img, fname, f"{name}_DiffuseMap")
    nrm = flat_normal(doc, fname)
    new_ts = clone_tree(doc, tr.obj)
    new_m = clone_tree(doc, mr.obj)
    mapping = {old_d: tex, old_n: nrm}
    from .ops import _remap_ids
    _remap_ids(new_ts, mapping)
    ts_uid = doc.add_root(fname, Root(tr.pre_header, tr.status, new_ts), f"{name}_Set")
    _remap_ids(new_m, {**mapping, ts: ts_uid})
    new_m.fields["AlphaTestEnabled"] = b"\x00"
    ref = new_m.fields["TextureSet"]
    new_m.fields["TextureSet"] = Ref(ref.tag, ref.extra, idb(ts_uid))
    return doc.add_root(fname, Root(mr.pre_header, mr.status, new_m), name)
