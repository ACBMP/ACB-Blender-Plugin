"""MapDocument: an ACB map forge opened for editing.

The forge is unpacked once into a cache directory (keyed by path, size and mtime) together with a uid index, so
reopening a map is fast. Roots are decoded on demand with the engine-rule codec (anvilforge.fastload); types whose
FastLoad is hand-written (navmesh, animation, FX, shaders) stay opaque bytes. Saving re-encodes only the roots that
were touched, rebuilds only the .data entries that hold them (every copy -- some objects live in several entries) and
repacks with retail-style alignment; every other entry goes out byte-identical.
"""
from __future__ import annotations

import hashlib
import io
import os
import pickle
import shutil
import tempfile
import time
from collections import defaultdict

from anvilforge.binio import leading_index
from anvilforge.datafile import derive_uid_and_ext
from anvilforge.fastload import DecodeError, Root
from anvilforge.fileset import loose_file_name
from anvilforge.forge import LOOSE_EXTENSIONS, repack, unpack

from .datafile import DataFile
from .schema import GAME, codec, type_hash, type_name

CACHE_ROOT = os.path.expanduser("~/.cache/acbmap")
INDEX_VERSION = 2   # 2: entry names decoded 8-bit (latin-1), not lossy UTF-8


def u32(b: bytes) -> int:
    return int.from_bytes(b, "little")


def idb(i: int) -> bytes:
    return (i & 0xFFFFFFFF).to_bytes(4, "little")


class MapDocument:
    def __init__(self, forge_path: str, cache_root: str = CACHE_ROOT):
        self.path = os.path.abspath(forge_path)
        st = os.stat(self.path)
        key = hashlib.sha1(f"{self.path}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:16]
        self.cache = os.path.join(cache_root, f"{os.path.basename(self.path)}-{key}")
        self.src = os.path.join(self.cache, "src")
        self._files: dict[str, DataFile] = {}
        self._roots: dict[int, Root | None] = {}
        self._orig: dict[int, bytes] = {}     # payload each decoded root came from
        self.dirty: set[int] = set()          # uids whose decoded tree must be re-encoded
        self.touched_files: set[str] = set()  # entries rebuilt on save (deps or sub list changed)
        self._load_index()

    # ------------------------------------------------------------ index --

    def _load_index(self) -> None:
        idx_path = os.path.join(self.cache, "index.pkl")
        if os.path.exists(idx_path):
            with open(idx_path, "rb") as f:
                d = pickle.load(f)
            if d.get("v") == INDEX_VERSION:
                self.__dict__.update(d["data"])
                return
        shutil.rmtree(self.cache, ignore_errors=True)
        os.makedirs(self.src)
        entries = unpack(self.path, self.src, GAME)
        fnames = [loose_file_name(i, e) for i, e in enumerate(entries)]
        where: dict[int, list] = defaultdict(list)   # uid -> [(fname, sub index)]
        info: dict[int, tuple] = {}                  # uid -> (type hash, name)
        for fn in fnames:
            if not fn.endswith(".data"):
                continue
            df = self._file(fn)
            for i, (ext, name, payload) in enumerate(df.subs):
                uid = DataFile.uid(payload)
                where[uid].append((fn, i))
                info.setdefault(uid, (ext, name))
        data = {"entries": entries, "fnames": fnames, "where": dict(where), "info": info}
        tmp = idx_path + ".tmp"
        with open(tmp, "wb") as f:
            pickle.dump({"v": INDEX_VERSION, "data": data}, f)
        os.replace(tmp, idx_path)
        self.__dict__.update(data)

    def _file(self, fname: str) -> DataFile:
        df = self._files.get(fname)
        if df is None:
            with open(os.path.join(self.src, fname), "rb") as f:
                df = DataFile(fname, f.read())
            self._files[fname] = df
        return df

    # ------------------------------------------------------------ queries --

    def uids(self, type_name_: str | None = None):
        if type_name_ is None:
            return list(self.info)
        h = type_hash(type_name_)
        return [u for u, (t, _n) in self.info.items() if t == h]

    def type_of(self, uid: int) -> str:
        return type_name(self.info[uid][0])

    def name_of(self, uid: int) -> str:
        v = self.info.get(uid)
        return v[1] if v else f"{uid:#010x}"

    def entry_of(self, uid: int) -> str:
        """The .data entry holding the (first copy of the) root."""
        return self.where[uid][0][0]

    def entry_root(self, fname: str) -> int | None:
        """uid of an entry's own (first) object -- e.g. the GridCellDataBlock of a cell's entry; None if empty."""
        subs = self._file(fname).subs
        return DataFile.uid(subs[0][2]) if subs else None

    def payload(self, uid: int) -> bytes:
        fn, i = self.where[uid][0]
        return self._file(fn).subs[i][2]

    def root(self, uid: int) -> Root | None:
        """Decoded tree, or None when the type uses a custom serializer (opaque)."""
        if uid not in self._roots:
            p = self.payload(uid)
            self._orig[uid] = p
            try:
                self._roots[uid] = codec().decode(p)
            except DecodeError:
                self._roots[uid] = None
        return self._roots[uid]

    def obj(self, uid: int):
        r = self.root(uid)
        return r.obj if r is not None else None

    def decode_types(self, names) -> None:
        for n in names:
            for u in self.uids(n):
                self.root(u)

    # ------------------------------------------------------------ edits --

    def touch(self, uid: int) -> None:
        """Mark a decoded root as modified (its tree is re-encoded on save)."""
        if self.root(uid) is None:
            raise ValueError(f"{uid:#x} is opaque, can't be edited as a tree")
        self.dirty.add(uid)

    def add_root(self, fname: str, root: Root, name: str, position: int | None = None) -> int:
        """Add a new root object to an existing entry (appended, or inserted at `position`)."""
        payload = codec().encode(root)
        uid = DataFile.uid(payload)
        if uid in self.info:
            raise ValueError(f"object id {uid:#x} already used ({self.name_of(uid)})")
        df = self._file(fname)
        sub = [root.obj.type_hash, name, payload]
        if position is None:
            df.subs.append(sub)
        else:
            df.subs.insert(position, sub)
        self._reindex_file(fname)
        self.info[uid] = (root.obj.type_hash, name)
        self._roots[uid] = root
        self.touched_files.add(fname)
        return uid

    def add_entry(self, name: str, roots: list[tuple[Root, str]], deps: list[int] = ()) -> str:
        """A new forge entry (appended after every existing one) holding `roots` [(root, object name)]; its id is the
        first root's. Returns the loose file name."""
        fname = f"{len(self.fnames)}_-_{name}.data"
        df = DataFile.__new__(DataFile)
        from anvilforge.datafile import Dependency
        df.fname, df.deps, df.raw_dep, df.subs = fname, [Dependency(id=d) for d in deps], b"", []
        self._files[fname] = df
        self.fnames.append(fname)
        for r, n in roots:
            payload = codec().encode(r)
            uid = DataFile.uid(payload)
            df.subs.append([r.obj.type_hash, n, payload])
            self.info.setdefault(uid, (r.obj.type_hash, n))
            self._roots.setdefault(uid, r)
        self._reindex_file(fname)
        self.touched_files.add(fname)
        return fname

    def set_entry_roots(self, fname: str, roots: list[tuple[Root, str]]) -> None:
        """Replace every object of an existing entry (keeps its dependency table)."""
        df = self._file(fname)
        old = [DataFile.uid(s[2]) for s in df.subs]
        df.subs = []
        for r, n in roots:
            df.subs.append([r.obj.type_hash, n, codec().encode(r)])
        self._reindex_file(fname)
        for u in old:
            if u not in self.where:
                self.info.pop(u, None)
                self._roots.pop(u, None)
        for (r, n), sub in zip(roots, df.subs):
            uid = u32(r.obj.id)
            self.info.setdefault(uid, (r.obj.type_hash, n))
            # objects whose first copy lives in another entry (the AWD's phantom chest twins) keep that copy as truth
            if self.where[uid][0][0] == fname:
                self._roots[uid] = r
                self._orig[uid] = sub[2]
                self.dirty.discard(uid)
        self.touched_files.add(fname)

    def remove_root(self, uid: int) -> None:
        for fn, _i in list(self.where.get(uid, [])):
            df = self._file(fn)
            df.subs = [s for s in df.subs if DataFile.uid(s[2]) != uid]
            self._reindex_file(fn)
            self.touched_files.add(fn)
        self.where.pop(uid, None)
        self.info.pop(uid, None)
        self._roots.pop(uid, None)
        self.dirty.discard(uid)

    def _reindex_file(self, fname: str) -> None:
        for uid, locs in self.where.items():
            if any(fn == fname for fn, _ in locs):
                self.where[uid] = [l for l in locs if l[0] != fname]
        for i, (_ext, _n, payload) in enumerate(self._file(fname).subs):
            self.where.setdefault(DataFile.uid(payload), []).append((fname, i))
        for uid in [u for u, l in self.where.items() if not l]:
            del self.where[uid]

    def add_dependency(self, fname: str, dep_uid: int) -> None:
        df = self._file(fname)
        if any(d.id == dep_uid for d in df.deps):
            return
        from anvilforge.datafile import Dependency
        df.deps.append(Dependency(id=dep_uid))
        self.touched_files.add(fname)

    def fresh_id(self, base: int | None = None) -> int:
        """An object id unused in this forge (and in every decoded tree's sub-objects)."""
        used = self._used_ids()
        i = base if base is not None else (max(self.info) + 0x100 if self.info else 0x10000000)
        while i in used:
            i += 1
        used.add(i)
        return i

    def _used_ids(self) -> set[int]:
        if not hasattr(self, "_used"):
            from anvilforge.fastload import walk
            used = set(self.info)
            for r in self._roots.values():
                if r is not None:
                    used.update(u32(o.id) for o in walk(r.obj))
            self._used = used
        return self._used

    # ------------------------------------------------------------ save --

    def _flush(self) -> None:
        c = codec()
        for uid in sorted(self.dirty):
            payload = c.encode(self._roots[uid])
            orig = self._orig.get(uid)
            for fn, i in self.where[uid]:
                df = self._file(fn)
                # identical copies follow the edit; differing twins (the phantom chest copies in the chest
                # AdditionalWorldData entry share their map entity's id) are left alone
                if orig is not None and df.subs[i][2] != orig:
                    continue
                if df.subs[i][2] != payload:
                    df.subs[i][2] = payload
                    self.touched_files.add(fn)
            self._orig[uid] = payload
        self.dirty.clear()

    def save(self, out_path: str) -> list[str]:
        """Write the edited forge to out_path; returns the names of the rebuilt entries."""
        self._flush()
        stage = tempfile.mkdtemp(prefix="stage-", dir=self.cache)
        try:
            for fn in os.listdir(self.src):
                if os.path.splitext(fn)[1].lower() not in LOOSE_EXTENSIONS:
                    continue
                dst = os.path.join(stage, fn)
                if fn in self.touched_files:
                    with open(dst, "wb") as f:
                        f.write(self._file(fn).build())
                else:
                    os.symlink(os.path.join(self.src, fn), dst)
            for fn in sorted(self.touched_files - set(os.listdir(self.src)), key=leading_index):
                with open(os.path.join(stage, fn), "wb") as f:
                    f.write(self._file(fn).build())
            tmp = out_path + ".tmp"
            repack(stage, tmp, GAME, original_entries=list(self.entries), align_entries=True)
            os.replace(tmp, out_path)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        return sorted(self.touched_files)
