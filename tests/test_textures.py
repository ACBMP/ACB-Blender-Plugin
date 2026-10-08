"""New textures and materials from images."""
import os

import numpy as np
import pytest

from acbmap import textures as T
from acbmap import visual as V
from acbmap.doc import MapDocument, u32

MULTI = os.environ.get("ACB_MULTI", "/home/a/vbox/Assassin's Creed Brotherhood/multi")
MAP = os.path.join(MULTI, "DataPC_AC2MP_MtStMichel_dlc.forge")
needs_map = pytest.mark.skipif(not os.path.exists(MAP), reason="needs the ACB install")


def undxt1(data: bytes, w: int, h: int) -> np.ndarray:
    bw, bh = max(1, (w + 3) // 4), max(1, (h + 3) // 4)
    b = np.frombuffer(data[:bw * bh * 8], dtype=[("c0", "<u2"), ("c1", "<u2"), ("i", "<u4")])
    p0, p1 = T._from565(b["c0"].astype(np.int32)), T._from565(b["c1"].astype(np.int32))
    four = (b["c0"] > b["c1"])[:, None]
    pal = np.stack([p0, p1, np.where(four, (2 * p0 + p1) / 3, (p0 + p1) / 2),
                    np.where(four, (p0 + 2 * p1) / 3, 0)], 1)
    idx = (b["i"][:, None] >> (2 * np.arange(16, dtype=np.uint32))) & 3
    px = np.take_along_axis(pal, idx[:, :, None].astype(np.int64), 1)        # n x 16 x 3
    img = px.reshape(bh, bw, 4, 4, 3).transpose(0, 2, 1, 3, 4).reshape(bh * 4, bw * 4, 3)
    return img[:h, :w]


def test_dxt1_roundtrip():
    y, x = np.mgrid[0:64, 0:128]
    img = np.zeros((64, 128, 4), np.uint8)
    img[..., 0] = x * 2
    img[..., 1] = y * 4
    img[..., 2] = 128 + 60 * np.sin(x / 7.0)
    img[..., 3] = 255
    err = np.abs(undxt1(T.dxt1(img), 128, 64) - img[..., :3].astype(np.float32))
    assert err.mean() < 4 and err.max() < 40


def test_mip_chain_size():
    img = np.zeros((32, 64, 4), np.uint8)
    levels = T.mip_chain(img)
    assert [m.shape[:2] for m in levels][-1] == (1, 1) and len(levels) == 7
    assert sum(len(T.dxt1(m)) for m in levels) == sum(max(1, (m.shape[1] + 3) // 4) * max(1, (m.shape[0] + 3) // 4) * 8
                                                     for m in levels)


@needs_map
def test_new_material(tmp_path):
    from acbmap import ops
    d = MapDocument(MAP)
    img = np.zeros((128, 256, 4), np.uint8)
    img[..., 0], img[..., 3] = 200, 255          # red
    m = T.new_material(d, img, "Test_Red")
    out = str(tmp_path / os.path.basename(MAP))
    d.save(out)
    d2 = MapDocument(out)
    tex, nrm = V.material_texture(d2, m, 0), V.material_texture(d2, m, 1)
    assert tex is not None and nrm is not None and d2.name_of(tex) == "Test_Red_DiffuseMap"
    c = d2.obj(tex).fields["CompiledTextureMap"].obj
    assert (u32(c.fields["Width"]), u32(c.fields["Height"]), u32(c.fields["NbMipMaps"])) == (256, 128, 9)
    px = undxt1(b"".join(c.fields["Data"]), 256, 128)
    assert abs(px[..., 0].mean() - 200) < 4 and px[..., 1].max() < 8
    bound = {u32(p[3].fields["Texture"].id) for p in d2.obj(m).dyn if hasattr(p[3], "fields") and "Texture" in p[3].fields}
    assert bound == {tex, nrm}
    top = d2.entry_of(ops.top_block(d2))
    assert {d2.entry_of(x) for x in (m, tex, nrm, u32(d2.obj(m).fields["TextureSet"].id))} == {top}
    assert V.texture_dds(d2, tex) is not None
    from acbmap.checks import new_problems
    assert new_problems(d2, MapDocument(MAP)) == []
