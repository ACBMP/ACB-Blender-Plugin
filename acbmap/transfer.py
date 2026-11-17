"""Copy an element from another map: a hiding haystack, a chase breaker, any Entity / EntityGroup root, with
everything it needs (meshes, materials, textures, shapes, skeleton, animation, effects).

How retail stores what an element uses (San Donato's haystacks, Alhambra's flower stacks, 2026-11-17):
- Mesh, TextureMap and MaterialTemplate live in entries of their own.
- Everything else (Material, TextureSet, LODSelector, shapes, Skeleton, Animation, FX and the FX's Entity +
  ParticleSystem) is a root stored inside the entry of the level-1 grid cell over the element's cell
  (ops.area_block), the same root in each level-1 cell that has such an element; that cell's block names it in
  Objects after the activated ones, and activates the FX entities.
The copy does the same in the target map: resources go to the element's level-1 cell, byte for byte and under their
own ids (FX and Animation are opaque here: they can't be renumbered; retail ids are the same asset in every map),
own-entry ones as new entries listed in that cell's dependency table. A resource the target map already has is
kept, and stored in that cell too if it isn't there or in an always-loaded entry. The element itself gets fresh ids
(its ResetData included), goes to the grid cell of its position, and gets a map marker if it had one.

Links inside opaque roots are found by scanning their bytes for ids of the source map: a superset (a chance match
only copies one root too many).
"""
from __future__ import annotations

from anvilforge.fastload import Ref, Root

from . import ops
from .datafile import DataFile
from .doc import MapDocument, idb, u32
from .kinds import group_children
from .schema import codec, type_name

ELEMENT_TYPES = ("Entity", "EntityGroup")
NEVER = ("GridCellDataBlock", "World", "NavMeshManager", "NavMesh")   # never part of an element (chance matches)


def _src_index(src: MapDocument) -> dict:
    """Cached per source map: ids that are entries of their own, ids named by a block, ids a block activates."""
    idx = src.__dict__.get("_transfer_index")
    if idx is None:
        listed, active = set(), set()
        for b in src.uids("GridCellDataBlock"):
            o = src.obj(b)
            if o is None:
                continue
            objs = [u32(r.id) for r in o.fields["Objects"]]
            listed.update(objs)
            active.update(objs[:u32(o.fields["NumberOfObjectsToActivate"])])
        ids = {u for u in src.info if src.type_of(u) not in NEVER}
        idx = src._transfer_index = {"entries": {e.id & 0xFFFFFFFF for e in src.entries} & ids, "listed": listed,
                                     "active": active, "ids": ids}
    return idx


def links_of(src: MapDocument, uid: int) -> set[int]:
    """Roots of `src` that root `uid` links to (an opaque root: every 4 bytes that are a root id of the map)."""
    ids = _src_index(src)["ids"]
    r = src.root(uid)
    if r is not None:
        return {i for i in ops.linked_ids(r.obj) if i in ids and i != uid}
    b = src.payload(uid)
    return {i for i in (u32(b[p:p + 4]) for p in range(len(b) - 3)) if i in ids and i != uid}


def resources(src: MapDocument, o) -> list[int]:
    """Every root of `src` the tree `o` needs, transitively; what a whole entry carries (its other roots, its
    dependency table) included."""
    idx = _src_index(src)
    todo = sorted(i for i in ops.linked_ids(o) if i in idx["ids"])
    out, seen = [], set()
    while todo:
        u = todo.pop()
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
        more = links_of(src, u)
        if u in idx["entries"]:
            df = src._file(src.entry_of(u))
            more |= {DataFile.uid(s[2]) for s in df.subs} | {d.id & 0xFFFFFFFF for d in df.deps}
        todo += sorted(i for i in more if i in idx["ids"] and i not in seen)
    return out


def _payload(doc: MapDocument, uid: int) -> bytes:
    return codec().encode(doc._roots[uid]) if uid in doc.dirty else doc.payload(uid)


def _entry_ids(doc: MapDocument) -> set[int]:
    return {e.id & 0xFFFFFFFF for e in doc.entries} | {doc.entry_root(fn) for fn in doc.fnames[len(doc.entries):]}


def import_resources(doc: MapDocument, src: MapDocument, ids, area: int | None = None) -> dict:
    """Make roots `ids` of `src` (see resources) available to the elements under level-1 cell block `area`
    (default: the whole-map cell). Returns counts: entries and roots added, roots the map already had."""
    idx = _src_index(src)
    area = ops.top_block(doc) if area is None else area
    area_fn = doc.entry_of(area)
    if doc.entry_root(area_fn) != area:
        raise ops.EditError(f"{doc.name_of(area)} doesn't own its entry")
    here = ops.always_loaded_entries(doc) | {area_fn}
    own = _entry_ids(doc)
    stats = {"entries": 0, "roots": 0, "kept": 0}
    ids = [u for u in ids if src.type_of(u) not in NEVER]
    whole = {u for u in ids if u in idx["entries"]}
    name_tail, activate = [], []

    def store(u, type_hash_, name, payload, from_src):
        doc.add_raw(area_fn, type_hash_, name, payload)
        if from_src and u in idx["active"] and src.type_of(u) in ELEMENT_TYPES:
            activate.append(u)      # an effect's entity
        else:
            name_tail.append(u)

    for u in ids:
        if u in doc.info:
            if doc.info[u][0] != src.info[u][0]:
                raise ops.EditError(f"id {u:#x} is {src.type_of(u)} {src.name_of(u)!r} in the source map but "
                                    f"{doc.type_of(u)} {doc.name_of(u)!r} here")
            if u in own:
                doc.add_dependency(area_fn, u)
            elif not any(fn in here for fn, _ in doc.where[u]):
                store(u, doc.info[u][0], doc.info[u][1], _payload(doc, u), u in getattr(doc, "_transferred", ()))
            stats["kept"] += 1
            continue
        if u in idx["entries"]:
            sfn = src.entry_of(u)
            df = src._file(sfn)
            name = sfn.split("_-_", 1)[1].rsplit(".data", 1)[0]
            doc.add_raw_entry(name, [tuple(x) for x in df.subs], [d.id for d in df.deps])
            for x in df.subs:
                ops.reserve_ids(doc, x[2])
            doc.add_dependency(area_fn, u)
            own.add(u)
            stats["entries"] += 1
        elif all(src.entry_root(fn) in whole for fn, _ in src.where[u]):
            continue        # stored only inside entries that are copied whole
        else:
            p = src.payload(u)
            store(u, src.info[u][0], src.info[u][1], p, True)
            ops.reserve_ids(doc, p)
            doc.__dict__.setdefault("_transferred", set()).add(u)
            stats["roots"] += 1
    if activate:
        ops.activate(doc, area, activate)
    if name_tail:
        o = doc.obj(area)
        have = {u32(r.id) for r in o.fields["Objects"]}
        o.fields["Objects"] = o.fields["Objects"] + [Ref(1, 0, idb(i)) for i in name_tail if i not in have]
        doc.touch(area)
    return stats


def copy_element(doc: MapDocument, src: MapDocument, uid: int, matrix: bytes | None = None,
                 block: int | None = None, name: str | None = None) -> tuple[tuple[int, int], dict]:
    """Copy root element `uid` (an Entity or EntityGroup) of map `src` into `doc`, at `matrix` (default: where it
    was), into the grid cell of that position (or `block`). Returns (its key, import_resources' counts)."""
    if src.type_of(uid) not in ELEMENT_TYPES:
        raise ops.EditError(f"{src.name_of(uid)} is a {src.type_of(uid)}, not an element")
    sroot = src.root(uid)
    if sroot is None:
        raise ops.EditError(f"{uid:#x} is not decodable")
    new = ops.clone_tree(doc, sroot.obj)
    members = [new] + (group_children(new) if type_name(new.type_hash) == "EntityGroup" else [])
    for o in members:
        ops.unmerge(o)      # not in its source's compound
        if "FakeCellIndex" in o.fields:    # the source map's far stand-in cell
            o.fields["FakeCellIndex"] = (-1).to_bytes(len(o.fields["FakeCellIndex"]), "little", signed=True)
    if matrix is not None:
        ops.place(new, matrix)
    blk = block if block is not None else ops._block_at(doc, new.fields["GlobalMatrix"])
    fn = doc.entry_of(blk)
    if doc.entry_root(fn) != blk:
        raise ops.EditError(f"{doc.name_of(blk)} doesn't own its entry")
    stats = import_resources(doc, src, resources(src, sroot.obj), ops.area_block(doc, blk))
    if name is None:
        base, names, i = src.name_of(uid), {n for _t, n in doc.info.values()}, 1
        while f"{base}_copy{i}" in names:
            i += 1
        name = f"{base}_copy{i}"
    nuid = doc.add_root(fn, Root(sroot.pre_header, sroot.status, new), name)
    ops.activate(doc, blk, [nuid])
    doc.touch(nuid)
    mk = ops.marker_of(src, uid)    # benches and hiding stacks have a map marker
    stats["marker"] = int(mk is not None and ops.add_marker(doc, mk[2], nuid, new.fields["GlobalMatrix"]))
    if hasattr(doc, "_compounds"):
        doc._compounds = None
    return (nuid, -1), stats


def find(src: MapDocument, name: str) -> int:
    """The first element root of `src` whose name starts with `name`."""
    for t in ELEMENT_TYPES:
        for u in sorted(src.uids(t), key=src.name_of):
            if src.name_of(u).startswith(name):
                return u
    raise ops.EditError(f"no element named {name}* in {src.path}")
