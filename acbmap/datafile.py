"""One loose .data forge entry held in memory: dependency table + sub-objects (from acr-map-port convert.py)."""
from __future__ import annotations

import io

from anvilforge.binio import write_string32
from anvilforge.datafile import (DATA_MAGIC, DATA_VERSIONS, _build_toc, _extra_from_header,
                                 _read_inline_dependencies, _write_block_set, _write_inline_dependencies,
                                 derive_uid_and_ext, iter_datafile_subparts)

from .schema import GAME


class DataFile:
    def __init__(self, fname: str, raw: bytes):
        self.fname = fname
        self.deps, self.raw_dep = _read_inline_dependencies(io.BytesIO(raw), GAME)
        self.subs: list[list] = []  # [ext type hash, name, payload]
        for _i, ext, name, payload in iter_datafile_subparts(io.BytesIO(raw), GAME):
            self.subs.append([ext, name, payload])

    @staticmethod
    def uid(payload: bytes) -> int:
        return derive_uid_and_ext(payload, True)[0]

    def build(self) -> bytes:
        version, algorithm, block_size = DATA_VERSIONS[GAME]
        content = bytearray()
        toc = []
        seen = set()
        for ext, name, raw in self.subs:
            uid = self.uid(raw)
            if uid in seen:
                continue
            seen.add(uid)
            nm = "" if name.lower() == "unnamed" else name
            extra = _extra_from_header(raw[:8])
            start = len(content)
            content += ext.to_bytes(4, "little")
            content += (len(raw) - 1 - extra).to_bytes(4, "little", signed=True)
            nb = io.BytesIO()
            write_string32(nb, nm)
            content += nb.getvalue()
            content += raw
            toc.append((uid, len(content) - start))
        out = io.BytesIO()
        _write_inline_dependencies(out, self.deps, self.raw_dep, GAME)
        out.write(DATA_MAGIC.to_bytes(8, "little"))
        _write_block_set(out, _build_toc(toc, True), version, algorithm, block_size)
        out.write(DATA_MAGIC.to_bytes(8, "little"))
        _write_block_set(out, bytes(content), version, algorithm, block_size)
        return out.getvalue()
