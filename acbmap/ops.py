"""Edit operations on map entities. An element is addressed by (root uid, child index): child -1 is the root Entity /
EntityGroup itself, child >= 0 an entity inlined in the group's Entities list.

Engine rules these follow (acr-map-port NOTES.md):
- A root is loaded only when a GridCellDataBlock lists it in the first NumberOfObjectsToActivate entries of Objects,
  and it must live in that block's own entry (blockcheck rule).
- Entity.GlobalMatrix is world space for roots and group children alike; component zones/shapes are entity-local.
- New ids come from 0xE9600000-0xEAA00000, a run of 64k slices no ACB multi forge (retail, skins, extra, the ACFE
  ports) uses, one slice per world. NOT 0xF0000000 and up: the engine numbers runtime-created objects from there
  (scimitar::ObjectManager: transient ids from 0xF0000000, local ids from 0xF8000000), and a forge object whose id
  a runtime object already took never shows up in game.
"""
from __future__ import annotations

import copy

from anvilforge.fastload import Handle, Obj, Ptr, Ref, Root, walk

from .datafile import DataFile
from .doc import MapDocument, idb, u32
from .geom import matrix_rows, pack_floats
from .kinds import group_children
from .schema import type_name

ID_RANGE_LO, ID_RANGE_HI = 0xE9600000, 0xEAA00000   # free in every multi forge (census 2026-10-08)
ID_RANGE_BASE = ID_RANGE_LO
RUNTIME_ID_BASE = 0xF0000000                         # ObjectManager transient/local ids: never use in a forge


def is_editor_id(i: int) -> bool:
    return ID_RANGE_LO <= i < ID_RANGE_HI


class EditError(Exception):
    pass


# ------------------------------------------------------------------ ids --

def id_base(doc: MapDocument) -> int:
    worlds = doc.uids("World")
    w = worlds[0] if worlds else 0
    n = (ID_RANGE_HI - ID_RANGE_LO) >> 16
    return ID_RANGE_LO + ((((w * 2654435761) & 0xFFFFFFFF) >> 8) % n << 16)


def all_ids(doc: MapDocument) -> set[int]:
    """Every object id fresh_ids could hand out that the document already uses: every root id, plus each id in this
    world's editor range (id_base) found in any root. Unedited roots are scanned as raw bytes for the range's 2-byte
    prefix (a superset: a stray match only reserves an id), so nothing has to be decoded; edited roots, whose stored
    bytes are stale, are walked. Cached; fresh_ids adds what it hands out."""
    if getattr(doc, "_all_ids", None) is None:
        hi = (id_base(doc) >> 16).to_bytes(2, "little")
        ids = set(doc.info)
        for u in doc.info:
            if u in doc.dirty:
                r = doc.root(u)
                if r is not None:
                    ids.update(u32(o.id) for o in walk(r.obj))
                continue
            b = doc.payload(u)
            p = b.find(hi, 2)
            while p >= 0:
                ids.add(u32(b[p - 2:p + 2]))
                p = b.find(hi, p + 1)
        ids.discard(0)
        doc._all_ids = ids
    return doc._all_ids


def fresh_ids(doc: MapDocument, n: int) -> list[int]:
    used = all_ids(doc)
    base = id_base(doc)
    i = max(base, getattr(doc, "_id_cursor", base))   # ids below the cursor are taken: big imports stay linear
    while True:
        block = list(range(i, i + n))
        if not any(b in used for b in block):
            used.update(block)
            doc._id_cursor = i + n
            return block
        i += n
        if i + n > base + 0x10000:
            raise EditError("this world's id range is exhausted")


def migrate_runtime_ids(doc: MapDocument) -> int:
    """Renumber objects an earlier editor version gave ids in the engine's runtime range (>= 0xF0000000) into the
    editor range, with every link to them. Returns the number of ids changed."""
    roots = [u for u in doc.info if doc.root(u) is not None]
    bad = set()
    for u in roots:
        for o in walk(doc.obj(u)):
            if u32(o.id) >= RUNTIME_ID_BASE:
                bad.add(u32(o.id))
    if not bad:
        return 0
    mapping = dict(zip(sorted(bad), fresh_ids(doc, len(bad))))
    for u in roots:
        r = doc.root(u)
        if not (linked_ids(r.obj) & bad or any(u32(o.id) in bad for o in walk(r.obj))):
            continue
        _remap_ids(r.obj, mapping)
        if u in mapping:   # the root itself: re-add under its new id, in place
            fn = doc.entry_of(u)
            pos = next(i for f, i in doc.where[u] if f == fn)
            name = doc.name_of(u)
            doc.remove_root(u)
            doc.add_root(fn, r, name, pos)
        else:
            doc.touch(u)
    if hasattr(doc, "_compounds"):
        doc._compounds = None
    return len(mapping)


# ------------------------------------------------------------------ access --

def element_obj(doc: MapDocument, key) -> Obj:
    uid, child = key
    o = doc.obj(uid)
    if o is None:
        raise EditError(f"{uid:#x} is not decodable")
    if child < 0:
        return o
    return group_children(o)[child]


def owning_block(doc: MapDocument, uid: int) -> int | None:
    """GridCellDataBlock that activates root `uid` -- preferably the one owning the entry the root lives in."""
    fn = doc.entry_of(uid)
    own = doc.entry_root(fn)
    if own is not None and doc.type_of(own) == "GridCellDataBlock":
        o = doc.obj(own)
        if any(u32(r.id) == uid for r in o.fields["Objects"]):
            return own
    for b in doc.uids("GridCellDataBlock"):
        if any(u32(r.id) == uid for r in doc.obj(b).fields["Objects"]):
            return b
    return None


# ------------------------------------------------------------------ compound collision --
#
# Most retail static collision is merged per area: an Entity with a MultiInertComponent holds a MultiMeshShape listing
# member entities (ContainedEntities/ContainedShapes) plus one MOPP compiled over all of them, and each member's
# InertComponent has IsMerged=1. At load (MultiInertComponent::OnAddToWorld) the compound copies every member's
# shape at its current matrix and uses the stored MOPP as is (RebuildMopp only runs at editor time); a merged
# InertComponent never adds its own rigid body (InertComponent::AddToWorldInternal checks +0x105 = IsMerged). So a
# moved or reshaped member collides wrongly, a deleted one leaves a dangling handle, and a copy that keeps IsMerged=1
# has no collision at all. Editing a member therefore dissolves its compound: the compound entity goes, and its
# members become standalone (IsMerged=0), each with its own rigid body and MeshShape MOPP.

def _sub_index(doc: MapDocument) -> dict[int, tuple[int, int]]:
    """entity id -> element key, for roots and group children."""
    from .kinds import classify
    return {u32(e.obj.id): e.key for e in classify(doc)}


def compounds(doc: MapDocument) -> dict[int, list[int]]:
    """compound entity uid -> member entity ids (cached; dissolve_compound keeps it current)."""
    if getattr(doc, "_compounds", None) is None:
        out = {}
        for u in doc.uids("Entity"):
            o = doc.obj(u)
            if o is None:
                continue
            for p in o.fields.get("Components", []):
                c = getattr(p, "obj", None)
                if c is not None and type_name(c.type_hash) == "MultiInertComponent":
                    for ms in walk(c):
                        if type_name(ms.type_hash) == "MultiMeshShape":
                            out.setdefault(u, []).extend(u32(h.id) for h in ms.fields["ContainedEntities"])
        doc._compounds = out
    return doc._compounds


def compound_of(doc: MapDocument, entity_id: int) -> int | None:
    return next((m for m, mem in compounds(doc).items() if entity_id in mem), None)


def unmerge(o: Obj) -> bool:
    """IsMerged=0 on every InertComponent of an entity (it then adds its own rigid body)."""
    changed = False
    for _i, ic in inert_components(o):
        if ic.fields.get("IsMerged", b"\x00") != b"\x00":
            ic.fields["IsMerged"] = b"\x00"
            changed = True
    return changed


def dissolve_compound(doc: MapDocument, multi_uid: int, remove: bool = True) -> int:
    """Make a compound's members standalone collision and (remove=True) delete the compound entity. Returns the
    number of members unmerged."""
    members = compounds(doc).pop(multi_uid, [])
    idx = _sub_index(doc)
    n = 0
    for m in members:
        k = idx.get(m)
        if k is None:
            continue
        if unmerge(element_obj(doc, k)):
            doc.touch(k[0])
            n += 1
    if remove and multi_uid in doc.info:
        _remove_from_blocks(doc, {multi_uid})
        doc.remove_root(multi_uid)
    return n


def ensure_standalone(doc: MapDocument, key) -> int | None:
    """Before moving/reshaping/deleting an element: dissolve the compound(s) holding it or (for a group) any of its
    children. Returns a dissolved compound uid, if any."""
    o = element_obj(doc, key)
    ids = {u32(o.id)}
    if key[1] < 0 and type_name(o.type_hash) == "EntityGroup":
        ids |= {u32(c.id) for c in group_children(o)}
    done = None
    for m in [m for m, mem in compounds(doc).items() if ids & set(mem)]:
        dissolve_compound(doc, m)
        done = m
    return done


def _remove_from_blocks(doc: MapDocument, gone: set[int]) -> None:
    for b in doc.uids("GridCellDataBlock"):
        o = doc.obj(b)
        objs = o.fields["Objects"]
        if not any(u32(x.id) in gone for x in objs):
            continue
        n = u32(o.fields["NumberOfObjectsToActivate"])
        o.fields["NumberOfObjectsToActivate"] = idb(n - sum(1 for x in objs[:n] if u32(x.id) in gone))
        o.fields["Objects"] = [x for x in objs if u32(x.id) not in gone]
        doc.touch(b)


# ------------------------------------------------------------------ transform --

def _mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def _inv(m):
    """General 4x4 inverse (Gauss-Jordan)."""
    n = 4
    a = [list(map(float, row)) + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(m)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(a[r][c]))
        if abs(a[p][c]) < 1e-12:
            raise EditError("singular matrix")
        a[c], a[p] = a[p], a[c]
        f = a[c][c]
        a[c] = [x / f for x in a[c]]
        for r in range(n):
            if r != c and a[r][c]:
                g = a[r][c]
                a[r] = [x - g * y for x, y in zip(a[r], a[c])]
    return [row[n:] for row in a]


def set_matrix(doc: MapDocument, key, m: bytes) -> None:
    """Set an element's GlobalMatrix. Moving a group root carries its children along (they store world matrices)."""
    uid, child = key
    o = element_obj(doc, key)
    old = o.fields["GlobalMatrix"]
    if old == m:
        return
    ensure_standalone(doc, key)
    o.fields["GlobalMatrix"] = m
    if child < 0 and type_name(o.type_hash) == "EntityGroup":
        # row-vector convention: world = local * M  ->  new_child = child * inv(old) * new
        delta = _mul(_inv(matrix_rows(old)), matrix_rows(m))
        for c in group_children(o):
            nm = _mul(matrix_rows(c.fields["GlobalMatrix"]), delta)
            c.fields["GlobalMatrix"] = pack_floats([x for row in nm for x in row])
    doc.touch(uid)


def set_field(doc: MapDocument, key, path: list, value) -> None:
    """Set a field inside an element by path: names for object fields, ints for list indices, e.g.
    ["Components", 0, "SpawnType"] (Ptr/Ref are followed to their inline object). `value` must have the field's
    stored representation (raw bytes for primitives)."""
    o = element_obj(doc, key)
    cur = o
    for p in path[:-1]:
        cur = cur.fields[p] if isinstance(cur, Obj) else cur[p]
        if isinstance(cur, (Ptr, Ref)):
            cur = cur.obj
    last = path[-1]
    if isinstance(cur, Obj):
        old = cur.fields[last]
        if isinstance(old, bytes) and isinstance(value, bytes) and len(old) != len(value):
            raise EditError(f"{last}: expected {len(old)} bytes, got {len(value)}")
        cur.fields[last] = value
    else:
        cur[last] = value
    doc.touch(key[0])


# ------------------------------------------------------------------ duplicate --

def _remap_ids(obj: Obj, mapping: dict[int, int]) -> None:
    def fix(v):
        if isinstance(v, Obj):
            i = u32(v.id)
            if i in mapping:
                v.id = idb(mapping[i])
            for k, x in v.fields.items():
                v.fields[k] = fix(x)
            if v.dyn:
                v.dyn = [(a, b, c, fix(d)) for a, b, c, d in v.dyn]
            return v
        if isinstance(v, Ptr):
            if v.obj is not None:
                fix(v.obj)
            elif v.link is not None and u32(v.link) in mapping:
                v.link = idb(mapping[u32(v.link)])
            return v
        if isinstance(v, Ref):
            if v.obj is not None:
                fix(v.obj)
            if u32(v.id) in mapping:
                v.id = idb(mapping[u32(v.id)])
            return v
        if isinstance(v, Handle):
            if u32(v.id) in mapping:
                return Handle(v.tag, idb(mapping[u32(v.id)]))
            return v
        if isinstance(v, list):
            return [fix(x) for x in v]
        return v
    fix(obj)


def clone_tree(doc: MapDocument, obj: Obj) -> Obj:
    """Deep copy with every internal object id (and the links/handles pointing at them) renumbered."""
    new = copy.deepcopy(obj)
    old = sorted({u32(o.id) for o in walk(new)} - {0})
    mapping = dict(zip(old, fresh_ids(doc, len(old))))
    _remap_ids(new, mapping)
    return new


def duplicate(doc: MapDocument, key, matrix: bytes | None = None, block: int | None = None) -> tuple[int, int]:
    """Copy an element next to itself (same block/entry for a root, same group for a child), or into `block`'s entry
    (a GridCellDataBlock uid; roots only). Returns its key."""
    uid, child = key
    src = element_obj(doc, key)
    new = clone_tree(doc, src)
    if matrix is not None:
        new.fields["GlobalMatrix"] = matrix
    unmerge(new)    # a copy isn't in its source's compound: merged, it would have no collision
    for c in group_children(new) if type_name(new.type_hash) == "EntityGroup" else ():
        unmerge(c)
    if block is not None and child < 0 and block != owning_block(doc, uid):
        fn = doc.entry_of(block)
        if doc.entry_root(fn) != block:
            raise EditError(f"{doc.name_of(block)} doesn't own its entry")
        src_root = doc.root(uid)
        localize_links(doc, new, fn)
        nuid = doc.add_root(fn, Root(src_root.pre_header, src_root.status, new), _copy_name(doc, uid))
        copy_deps(doc, doc.entry_of(uid), fn, new)
        activate(doc, block, [nuid])
        return (nuid, -1)
    if child >= 0:
        g = doc.obj(uid)
        g.fields["Entities"].append(Ref(0, g.fields["Entities"][child].extra, new.id, new))
        doc.touch(uid)
        return (uid, len(g.fields["Entities"]) - 1)
    blk = owning_block(doc, uid)
    if blk is None:
        raise EditError(f"{doc.name_of(uid)} isn't activated by any grid cell / layer block")
    fn = doc.entry_of(uid)
    src_root = doc.root(uid)
    pos = next(i for f, i in doc.where[uid] if f == fn)
    nuid = doc.add_root(fn, Root(src_root.pre_header, src_root.status, new), _copy_name(doc, uid), pos + 1)
    if doc.entry_of(blk) != fn:
        raise EditError("source root does not live in its block's entry")
    activate(doc, blk, [nuid])
    return (nuid, -1)


def linked_ids(o: Obj) -> set[int]:
    """Ids a tree links to (handles, unresolved refs and pointer links), excluding 0."""
    out = set()
    for x in walk(o):
        for v in list(x.fields.values()) + [d[3] for d in x.dyn or []]:
            for y in v if isinstance(v, list) else [v]:
                if isinstance(y, Handle) or (isinstance(y, Ref) and y.obj is None):
                    out.add(u32(y.id))
                elif isinstance(y, Ptr) and y.obj is None and y.link is not None:
                    out.add(u32(y.link))
    out.discard(0)
    return out


def always_loaded_entries(doc: MapDocument) -> set[str]:
    """Entries loaded whatever the player's position: the World's and the whole-map grid cell's."""
    out = {doc.entry_of(w) for w in doc.uids("World")}
    try:
        out.add(doc.entry_of(top_block(doc)))
    except EditError:
        pass
    return out


def localize_links(doc: MapDocument, o: Obj, dst_fn: str) -> int:
    """Make everything tree `o` (about to be stored in entry dst_fn, or touched by the caller) links to loadable
    wherever dst_fn is. Entries load by id, but roots stored inside another entry (Materials and TextureSets live in
    grid-cell entries, with no dependency from their users) exist only while that entry is loaded: each such root
    not in dst_fn or an always-loaded entry is copied into dst_fn (fresh ids, once per entry) and the link
    redirected. Returns the number of copies made."""
    entries = {e.id & 0xFFFFFFFF for e in doc.entries}
    ok = always_loaded_entries(doc) | {dst_fn}
    memo = doc.__dict__.setdefault("_localized", {})
    made = 0

    def fix(t: Obj) -> None:
        nonlocal made
        mapping = {}
        for i in linked_ids(t):
            if i in entries or i not in doc.where or any(fn in ok for fn, _ in doc.where[i]):
                continue
            if (i, dst_fn) not in memo:
                r = doc.root(i)
                if r is None:
                    continue    # opaque: can't renumber it
                new = Root(r.pre_header, r.status, clone_tree(doc, r.obj))
                memo[(i, dst_fn)] = u32(new.obj.id)
                fix(new.obj)
                doc.add_root(dst_fn, new, f"{doc.name_of(i)}_l{u32(new.obj.id) & 0xFFFF:04x}")
                made += 1
            mapping[i] = memo[(i, dst_fn)]
        if mapping:
            _remap_ids(t, mapping)

    fix(o)
    return made


def _entry_ids_of(doc: MapDocument, ids) -> dict[int, str]:
    """entry id -> entry file name, for the entries holding the given root ids."""
    out = {}
    for i in ids:
        if i in doc.where:
            fn = doc.where[i][0][0]
            e = doc.entry_root(fn)
            if e is not None:
                out[e] = fn
    return out


def copy_deps(doc: MapDocument, src_fn: str, dst_fn: str, o: Obj) -> None:
    """A root moved/copied from entry src_fn into dst_fn takes along the dependencies src_fn listed for what it links
    to (retail tables list only part of what an entry references, so nothing is added that src_fn didn't have)."""
    have = {d.id & 0xFFFFFFFF for d in doc._file(src_fn).deps}
    for e, fn in _entry_ids_of(doc, linked_ids(o)).items():
        if e in have and fn != dst_fn:
            doc.add_dependency(dst_fn, e)


def top_block(doc: MapDocument) -> int:
    """The GridCellDataBlock of the grid's last cell, which covers the whole map and is always loaded (the level-0
    cells stream in only within the LoadingRangeTable radius of the World's anchor)."""
    best = None
    for b in doc.uids("GridCellDataBlock"):
        n = doc.name_of(b)
        if n.startswith("Cell") and n.endswith("_DataBlock") and n[4:-10].isdigit():
            if best is None or int(n[4:-10]) > best[0]:
                best = (int(n[4:-10]), b)
    if best is None:
        raise EditError("no grid cell blocks in this map")
    return best[1]


def _copy_name(doc: MapDocument, uid: int) -> str:
    base = doc.name_of(uid)
    names = {n for _t, n in doc.info.values()}
    i = 1
    while f"{base}_copy{i}" in names:
        i += 1
    return f"{base}_copy{i}"


def activate(doc: MapDocument, blk: int, ids) -> None:
    """Insert roots into a block's activated prefix (appending would leave them inactive)."""
    o = doc.obj(blk)
    objs = o.fields["Objects"]
    n = u32(o.fields["NumberOfObjectsToActivate"])
    have = {u32(x.id) for x in objs[:n]}
    add = [i for i in dict.fromkeys(ids) if i not in have]
    rest = [x for x in objs[n:] if u32(x.id) not in set(add)]
    o.fields["Objects"] = objs[:n] + [Ref(1, 0, idb(i)) for i in add] + rest
    o.fields["NumberOfObjectsToActivate"] = idb(n + len(add))
    doc.touch(blk)


# ------------------------------------------------------------------ delete --

def references_to(doc: MapDocument, ids: set[int], skip: set[int] = frozenset()) -> list[tuple[int, str]]:
    """Decoded roots (outside `skip`) that link to any of `ids` -> [(root uid, field)]."""
    hits = []
    for u in doc.info:
        if u in skip:
            continue
        r = doc.root(u)
        if r is None:
            continue
        for o in walk(r.obj):
            for k, v in o.fields.items():
                vals = v if isinstance(v, list) else [v]
                for x in vals:
                    t = None
                    if isinstance(x, Handle):
                        t = u32(x.id)
                    elif isinstance(x, Ptr) and x.obj is None and x.link is not None:
                        t = u32(x.link)
                    elif isinstance(x, Ref) and x.obj is None:
                        t = u32(x.id)
                    if t in ids and t != 0:
                        hits.append((u, f"{type_name(o.type_hash)}.{k}"))
    return hits


def delete(doc: MapDocument, key, force: bool = False) -> None:
    uid, child = key
    target = element_obj(doc, key)
    ids = {u32(o.id) for o in walk(target)} - {0}
    blocks = {b for b in doc.uids("GridCellDataBlock")}
    comps = {m for m, mem in compounds(doc).items() if ids & set(mem)}
    refs = [h for h in references_to(doc, ids, skip={uid} | blocks | comps) if not (child >= 0 and h[0] == uid)]
    if refs and not force:
        raise EditError(f"still referenced by {len(refs)} object(s): " +
                        ", ".join(f"{doc.name_of(u)} ({f})" for u, f in refs[:5]))
    for m in comps:
        dissolve_compound(doc, m)
    if child >= 0:
        g = doc.obj(uid)
        del g.fields["Entities"][child]
        doc.touch(uid)
        return
    _remove_from_blocks(doc, {uid})
    doc.remove_root(uid)


def delete_many(doc: MapDocument, keys) -> dict:
    """delete() for many elements with one reference scan (a scan per element is quadratic on a whole map). An
    element something outside the batch links to is refused, as delete() would. Returns {key: None (deleted) or
    the refusal reason}."""
    keys = list(dict.fromkeys(keys))
    owner: dict[int, tuple] = {}
    for k in keys:
        for o in walk(element_obj(doc, k)):
            owner[u32(o.id)] = k
    owner.pop(0, None)
    roots = {k[0] for k in keys if k[1] < 0}
    blocks = set(doc.uids("GridCellDataBlock"))
    comps = compounds(doc)
    refused: dict[tuple, str] = {}
    for u in doc.info:
        if u in roots or u in blocks or u in comps:
            continue
        r = doc.root(u)
        if r is None:
            continue
        for i in linked_ids(r.obj) & owner.keys():
            k = owner[i]
            if k[0] != u:   # a group child may be linked from its own group
                refused.setdefault(k, f"still referenced by {doc.name_of(u)}")
    out = {k: refused.get(k) for k in keys}
    go = [k for k in keys if out[k] is None]
    ids = {i for i, k in owner.items() if out[k] is None}
    for m in [m for m, mem in comps.items() if ids & set(mem)]:
        dissolve_compound(doc, m)
    for k in sorted((k for k in go if k[1] >= 0), key=lambda k: -k[1]):   # later children first: indices shift
        del doc.obj(k[0]).fields["Entities"][k[1]]
        doc.touch(k[0])
    gone = {k[0] for k in go if k[1] < 0}
    _remove_from_blocks(doc, gone)
    doc.remove_roots(gone)
    return out


def opaque_references(doc: MapDocument) -> set[int]:
    """Ids named in the raw bytes of roots acbmap can't decode (NavMeshManager: each navmesh's source entity, and the
    benches, elevators and crowd flows it links). Those links can't be edited, so what they name must not go.
    A superset (every 4-byte window that is a known root id), cached."""
    if getattr(doc, "_opaque_refs", None) is None:
        import numpy as np
        known = np.fromiter(doc.info, dtype=np.uint32)
        found = set()
        for u in doc.info:
            if doc.root(u) is not None:
                continue
            b = doc.payload(u)
            for k in range(4):
                n = (len(b) - k) // 4
                if n > 0:
                    v = np.frombuffer(b, dtype="<u4", count=n, offset=k)
                    found.update(int(x) for x in np.intersect1d(v, known))
        found.discard(0)
        doc._opaque_refs = found
    return doc._opaque_refs


HOLLOW_COMPONENTS = {"Visual", "InertComponent", "MultiInertComponent", "GuidanceSystem"}


COLLAPSED_SHAPE = ([(0.0, 0.0, -500.0), (0.01, 0.0, -500.0), (0.0, 0.01, -500.0)], [(0, 1, 2)], [0])


PARK_DEPTH = 1000.0   # parked elements sit this far below where they were


def hollow(doc: MapDocument, uid: int, keep_collision: bool = True, mode: str = "park") -> None:
    """Get an element that something unchangeable still names (a navmesh's source entity, a bench it links) out of
    the way without deleting it. Modes (in-game 2026-10-08: removing Visual + collision + climb edges from them
    crashed the load; leaving them intact loaded):
      park     the element (a group with its children) moved PARK_DEPTH m down, untouched otherwise
      visual   Visual and GuidanceSystem removed, collision left as is
      shape    each MeshShape collapsed to a 1 cm triangle 500 m below, Visual kept
      strip    Visual and GuidanceSystem removed; collision collapsed (keep_collision) or removed"""
    from .geom import position, set_mesh_shape_geometry, set_position
    o = doc.obj(uid)
    is_group = type_name(o.type_hash) == "EntityGroup"
    for x in [o] + (group_children(o) if is_group else []):
        unmerge(x)   # its compound (if any) goes with the cleared scenery
    if mode == "park":
        m = o.fields["GlobalMatrix"]
        x, y, z = position(m)
        set_matrix(doc, (uid, -1), set_position(m, (x, y, z - PARK_DEPTH)))
        doc.touch(uid)
        return
    for ci, x in [(-1, o)] + (list(enumerate(group_children(o))) if is_group else []):
        if mode in ("visual", "strip"):
            drop = {"Visual", "GuidanceSystem"}
            if mode == "strip" and not keep_collision:
                drop |= {"InertComponent", "MultiInertComponent"}
            x.fields["Components"] = [p for p in x.fields.get("Components", [])
                                      if not (isinstance(p, Ptr) and p.obj is not None
                                              and type_name(p.obj.type_hash) in drop)]
        if mode == "shape" or (mode == "strip" and keep_collision):
            for k, (_i, ic) in enumerate(inert_components(x)):
                sid = u32(ic.fields["RigidBody"].fields["Shape"].id)
                if sid not in doc.info or doc.type_of(sid) != "MeshShape":
                    continue
                nsid = make_shape_unique(doc, (uid, ci), k)
                set_mesh_shape_geometry(doc.obj(nsid), *COLLAPSED_SHAPE)
                doc.touch(nsid)
    doc.touch(uid)


SCENERY_KINDS = {"visual", "collision"}


def clear_scenery(doc: MapDocument, kinds=SCENERY_KINDS, keep_collision: bool = True,
                  blank_fakes: bool = False, hollow_pinned: bool = True, prune_deps: bool = True,
                  hollow_mode: str = "park", keep=frozenset()) -> dict:
    """Start a new map from this one: remove every element of `kinds` (default: visible geometry and static
    collision) and every group made only of them, keeping gameplay (spawns, chests, benches, chase breakers, zones,
    out-of-bounds, crowd flows...). Elements something else still links to are kept, and so are elements no grid
    block lists (runtime templates: effects, pickups, crowd groups). The removed roots' entries
    lose the dependencies only they needed. blank_fakes: the World's FakeEntities (merged far-LOD stand-ins of the old
    buildings, drawn for cells that aren't loaded) draw nothing (zero-length index spans; untested in game, so off by
    default). The navmesh is not touched.
    The few elements new geometry is cloned from (template_roots) are kept but taken out of every grid block, so the
    game never adds them; Mesh to Collision/Scenery keep working on the cleared map. Elements an undecodable root
    names (opaque_references: the navmeshes' source entities, the benches and elevators they link) are hollowed
    instead of removed (a dangling handle there can't be fixed); keep_collision: see hollow().
    Returns counts: removed, kept_referenced, compounds_dissolved, deps_dropped, and the hollowed and template uids."""
    from .kinds import classify, kind_of
    # elements no grid block lists are templates the game spawns at runtime (effects, pickups, crowd groups:
    # GFX_Elevator_Lever, AC2MP_PickupEntity...), not scenery: left alone (stripping them crashed the load)
    listed = {u32(r.id) for b in doc.uids("GridCellDataBlock") for r in doc.obj(b).fields["Objects"]}
    roots = {}
    for e in classify(doc, with_children=False):
        if e.uid not in listed or e.uid in keep:   # keep: roots the caller wants left alone
            continue
        if e.kind in kinds:
            roots[e.uid] = e
        elif e.kind == "group":
            ch = group_children(e.obj)
            if ch and all(kind_of(c) in kinds for c in ch):
                roots[e.uid] = e
    blocks = set(doc.uids("GridCellDataBlock"))
    comps = compounds(doc)
    # keep whatever something outside the removal set (and the compounds, handled below) links to
    sub_owner = {}
    for u in roots:
        for o in walk(doc.obj(u)):
            sub_owner[u32(o.id)] = u
    sub_owner.pop(0, None)
    kept = {u for u, _f in references_to(doc, set(sub_owner), skip=set(roots) | blocks | set(comps))}
    keep_roots = set()
    for u in kept:
        r = doc.root(u)
        for i in linked_ids(r.obj) if r is not None else ():
            if i in sub_owner:
                keep_roots.add(sub_owner[i])
    gone = set(roots) - keep_roots
    # named by an undecodable root (a navmesh's source entity, a bench it links...): hollowed, not removed
    pinned = opaque_references(doc)
    hollowed = {u for u in gone if any(u32(o.id) in pinned for o in walk(doc.obj(u)))}
    gone -= hollowed
    if not hollow_pinned:   # diagnostic: what the navmesh names stays as it is
        hollowed = set()
    # what new collision/scenery/climb edges are cloned from stays, but in no block: never added to the world
    templates = set(template_roots(doc, exclude=hollowed)) & gone
    gone -= templates
    # compounds: one whose members all go goes too; one that keeps some (benches...) is dissolved
    member_root = {u32(e.obj.id): e.uid for e in classify(doc)}
    dissolved = 0
    for m, mem in list(comps.items()):
        hit = [i for i in mem if member_root.get(i) in gone | hollowed]
        if not hit:
            continue
        if len(hit) == len(mem):
            comps.pop(m)
            gone.add(m)
        else:
            dissolve_compound(doc, m, remove=False)
            gone.add(m)
            dissolved += 1

    # dependency pruning: what the removed roots needed minus what the rest of their entries still use
    by_entry: dict[str, set[int]] = {}
    for u in gone:
        by_entry.setdefault(doc.entry_of(u), set()).add(u)
    dropped = 0
    for fn, us in (by_entry.items() if prune_deps else ()):
        lost, still = set(), set()
        for sub in doc._file(fn).subs:
            v = DataFile.uid(sub[2])
            r = doc.root(v)
            if r is None:
                continue
            (lost if v in us else still).update(_entry_ids_of(doc, linked_ids(r.obj)))
        drop = lost - still
        df = doc._file(fn)
        n = len(df.deps)
        df.deps = [d for d in df.deps if (d.id & 0xFFFFFFFF) not in drop]
        if len(df.deps) != n:
            dropped += n - len(df.deps)
            doc.touched_files.add(fn)

    _remove_from_blocks(doc, gone | templates)
    doc.remove_roots(gone)
    for u in hollowed:
        hollow(doc, u, keep_collision, hollow_mode)
    for t in templates:
        if unmerge(doc.obj(t)):
            doc.touch(t)

    for f in doc.uids("FakeEntities") if blank_fakes else []:
        fo = doc.obj(f)
        if fo is None:
            continue
        for fe in fo.fields["FakeEntities"]:
            for sm in fe.fields["SubMeshSpans"]:
                for sp in sm.fields["Spans"]:
                    sp.fields["NbIndex"] = idb(0)
        doc.touch(f)
    if hasattr(doc, "_all_ids"):
        doc._all_ids = None
    return {"removed": len(gone), "hollowed": sorted(hollowed),
            "kept_referenced": len(set(roots) - gone - templates - hollowed), "templates": sorted(templates),
            "compounds_dissolved": dissolved, "deps_dropped": dropped}


# ------------------------------------------------------------------ collision --

def inert_components(o: Obj) -> list[tuple[int, Obj]]:
    """[(index in Components, InertComponent)]."""
    return [(i, p.obj) for i, p in enumerate(o.fields.get("Components", []))
            if isinstance(p, Ptr) and p.obj is not None and type_name(p.obj.type_hash) == "InertComponent"]


def shape_users(doc: MapDocument, shape_uid: int) -> list[tuple[int, int]]:
    """Element keys whose collision uses MeshShape `shape_uid`."""
    from .kinds import classify
    users = []
    for e in classify(doc):
        for _i, ic in inert_components(e.obj):
            if u32(ic.fields["RigidBody"].fields["Shape"].id) == shape_uid:
                users.append(e.key)
    return users


def make_shape_unique(doc: MapDocument, key, inert_index: int = 0) -> int:
    """Give one element its own copy of a shared MeshShape (stored in the element's entry). Returns the new uid."""
    ensure_standalone(doc, key)
    o = element_obj(doc, key)
    _ci, ic = inert_components(o)[inert_index]
    ref = ic.fields["RigidBody"].fields["Shape"]
    src = u32(ref.id)
    r = doc.root(src)
    if r is None:
        raise EditError("shape isn't decodable")
    nid = fresh_ids(doc, 1)[0]
    new = copy.deepcopy(r)
    new.obj.id = idb(nid)
    localize_links(doc, new.obj, doc.entry_of(key[0]))
    doc.add_root(doc.entry_of(key[0]), new, f"{doc.name_of(src)}_u{nid & 0xFFFF:04x}")
    ic.fields["RigidBody"].fields["Shape"] = Ref(ref.tag, ref.extra, idb(nid))
    doc.touch(key[0])
    return nid


def strip_guidance(o: Obj) -> bool:
    """Drop an entity's GuidanceSystem (precomputed climb edges) -- needed when its collision no longer matches."""
    comps = o.fields["Components"]
    keep = [p for p in comps if not (isinstance(p, Ptr) and p.obj is not None and
                                     type_name(p.obj.type_hash) == "GuidanceSystem")]
    if len(keep) == len(comps):
        return False
    o.fields["Components"] = keep
    for _i, ic in inert_components(o):
        if "GuidanceSystemPtr" in ic.fields:
            ic.fields["GuidanceSystemPtr"] = Ptr(3)
    return True


def collision_template(doc: MapDocument, exclude=frozenset()):
    """A plain static collision entity of this map to clone new collision from (only Inert/Guidance/Visual
    components, one shape)."""
    from .kinds import classify
    best = None
    for e in classify(doc, with_children=False):
        if e.kind != "collision" or e.uid in exclude:
            continue
        names = set(e.components)
        if names <= {"InertComponent", "GuidanceSystem", "Visual"} and len(inert_components(e.obj)) == 1:
            ic = inert_components(e.obj)[0][1]
            shape = doc.obj(u32(ic.fields["RigidBody"].fields["Shape"].id))
            if shape is None or "Vertices" not in shape.fields:
                continue
            mats = [u32(m.id) for m in shape.fields.get("Materials", [])]
            # a clone takes its template's flags and (until replaced) material along: prefer plain solid stone that
            # needs no copying (a FuzzyZone corner made new ground behave like a ledge)
            score = (any("Fuzzy" in doc.name_of(m) for m in mats if m in doc.info),
                     ic.fields.get("IsMerged", b"\x00") != b"\x00", len(names), len(shape.fields["Vertices"]))
            if best is None or score < best[0]:
                best = (score, e)
    if best is None:
        raise EditError("no plain collision entity in this map to use as a template")
    return best[1]


def template_roots(doc: MapDocument, exclude=frozenset()) -> list[int]:
    """Root uids new geometry is cloned from: the collision template, the element holding the visual template and
    the one holding a climb-edge template (acbmap.guidance.template_system)."""
    from .guidance import systems, template_system
    from .kinds import classify, components
    out = []
    try:
        out.append(collision_template(doc, exclude).uid)
    except EditError:
        pass
    try:
        vt = visual_template(doc, exclude)
        gt = template_system(doc, exclude)
    except (EditError, ValueError):
        vt = gt = None
    for e in classify(doc):
        if vt is not None and any(c is vt for _n, c in components(e.obj)):
            out.append(e.uid)
            vt = None
        if gt is not None and any(g is gt for g in systems(e.obj)):
            out.append(e.uid)
            gt = None
    return list(dict.fromkeys(out))


SURFACES = ("ground", "roof", "wall")
SMALL_EXTENT, MEDIUM_EXTENT = 2.0, 6.5   # retail: IsSmallObject up to 3.3 m (median 1.8), IsMediumObject 1-6.5 m


def collision_material(doc: MapDocument, name: str = "Stone_Clean") -> int:
    """A CollisionMaterial for new collision: `name`, preferably the copy in the always-loaded cell (nothing to copy
    along), else the map's most used non-fuzzy one."""
    from collections import Counter
    top = doc.entry_of(top_block(doc))
    cms = doc.uids("CollisionMaterial")
    named = sorted((doc.entry_of(u) != top, u) for u in cms if doc.name_of(u) == name)
    if named:
        return named[0][1]
    use = Counter()
    for s_ in doc.uids("MeshShape"):
        o = doc.obj(s_)
        for m in o.fields.get("Materials", []) if o is not None else []:
            if u32(m.id) in doc.info and "Fuzzy" not in doc.name_of(u32(m.id)):
                use[u32(m.id)] += 1
    if not use:
        raise EditError("no collision material in this map")
    return use.most_common(1)[0][0]


def surface_of(verts, tris) -> str:
    """ground (mostly up-facing: a floor), roof (some up-facing area: a building to run across) or wall."""
    up = total = 0.0
    for a, b, c in tris:
        pa, pb, pc = verts[a], verts[b], verts[c]
        e1 = [pb[k] - pa[k] for k in range(3)]
        e2 = [pc[k] - pa[k] for k in range(3)]
        n = (e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0])
        area = (n[0] ** 2 + n[1] ** 2 + n[2] ** 2) ** 0.5
        total += area
        if area and n[2] / area > 0.7:
            up += area
    f = up / total if total else 0.0
    return "ground" if f >= 0.6 else "roof" if f >= 0.1 else "wall"


def set_surface(o: Obj, surface: str) -> None:
    """Retail flags: floors and stairs IsGround (crowd may spawn), roofs and what one runs across up high IsRoof,
    walls neither."""
    if surface not in SURFACES:
        raise EditError(f"surface must be one of {SURFACES}")
    for _i, ic in inert_components(o):
        ic.fields["IsGround"] = b"\x01" if surface == "ground" else b"\x00"
        ic.fields["IsRoof"] = b"\x01" if surface == "roof" else b"\x00"
        nav = ic.fields.get("GPSurfaceNavType")
        if nav is not None:
            nav.fields["GameplaySurfaceNavType_CrowdSpawn"] = b"\x01" if surface == "ground" else b"\x00"


def fit_new_element(o: Obj, pts=None) -> None:
    """Flags a new element must not inherit from its template: no far-LOD stand-in cell (FakeCellIndex -1; a cell's
    stand-in replaced the element from afar) and size class from its bounds (a 'medium' 150 m ground piece was
    culled early). pts: entity-local points to set the bounds from first."""
    import struct
    bv = o.fields.get("BoundingVolume")
    if bv is not None and pts:
        bv.fields["Min"] = struct.pack("<3f", *(min(p[k] for p in pts) for k in range(3)))
        bv.fields["Max"] = struct.pack("<3f", *(max(p[k] for p in pts) for k in range(3)))
        bv.fields["Type"] = (0).to_bytes(4, "little")
    if "FakeCellIndex" in o.fields:
        o.fields["FakeCellIndex"] = (-1).to_bytes(len(o.fields["FakeCellIndex"]), "little", signed=True)
    if bv is not None:
        lo, hi = struct.unpack("<3f", bv.fields["Min"]), struct.unpack("<3f", bv.fields["Max"])
        ext = max(hi[k] - lo[k] for k in range(3))
        o.fields["IsSmallObject"] = b"\x01" if ext <= SMALL_EXTENT else b"\x00"
        o.fields["IsMediumObject"] = b"\x01" if SMALL_EXTENT < ext <= MEDIUM_EXTENT else b"\x00"


def new_collision(doc: MapDocument, matrix: bytes, verts, tris, mats, template_key=None,
                  block: int | None = None, surface: str = "auto", material: int | None = None) -> tuple[int, int]:
    """A new static collision entity (no visual, no climb edges) with its own MeshShape, cloned from a plain collision
    entity of the map, in `block` (default: the always-loaded whole-map cell, so it exists wherever it is placed).
    The shape uses one collision material (default: collision_material(), Stone_Clean) and the element gets the
    surface flags of `surface` (ground / roof / wall; auto: surface_of the geometry). Returns its key."""
    from .geom import set_mesh_shape_geometry
    t = template_key or collision_template(doc).key
    key = duplicate(doc, t, matrix, block if block is not None else top_block(doc))
    o = element_obj(doc, key)
    o.fields["Components"] = [p for p in o.fields["Components"]
                              if not (isinstance(p, Ptr) and p.obj is not None and type_name(p.obj.type_hash) == "Visual")]
    strip_guidance(o)
    sid = make_shape_unique(doc, key)
    shape = doc.obj(sid)
    m0 = shape.fields["Materials"][0]
    shape.fields["Materials"] = [Ref(m0.tag, m0.extra, idb(material or collision_material(doc)))]
    set_mesh_shape_geometry(shape, verts, tris, [0] * len(tris))
    set_surface(o, surface_of(verts, tris) if surface == "auto" else surface)
    fit_new_element(o, list(verts))
    doc.touch(sid)
    doc.touch(key[0])
    return key


# ------------------------------------------------------------------ visual meshes --

def visual_template(doc: MapDocument, exclude=frozenset()) -> Obj:
    """A retail Visual component that shows a Mesh directly (InstanceData = MeshInstanceData), to clone."""
    from .kinds import classify, components
    for e in classify(doc):
        if e.uid in exclude:
            continue
        for n, c in components(e.obj):
            if n != "Visual":
                continue
            idata = c.fields.get("InstanceData")
            if (isinstance(idata, Ptr) and idata.obj is not None and type_name(idata.obj.type_hash) == "MeshInstanceData"
                    and idata.obj.fields["MaterialInfos"]):
                return c
    raise EditError("no Visual with a direct mesh in this map to use as a template")


def set_visual(doc: MapDocument, key, mesh_input) -> int:
    """Give an element a visual mesh built from mesh_input (acbmap.visual.MeshInput, entity-local), replacing the
    Visual components it had. The new Mesh root goes into the element's entry. Returns the Mesh uid."""
    import struct
    from . import visual as V
    o = element_obj(doc, key)
    root, mats = V.build_mesh(doc, mesh_input, lambda x: clone_tree(doc, x))
    fn = doc.entry_of(key[0])
    localize_links(doc, root.obj, fn)   # the map materials it uses must load wherever this entry does
    mesh_uid = doc.add_root(fn, root, f"{doc.name_of(key[0])}_Mesh{u32(root.obj.id) & 0xFFFF:04x}")

    comp = clone_tree(doc, visual_template(doc))
    tag, extra = comp.fields["Object"].tag, comp.fields["Object"].extra
    comp.fields["Object"] = Ref(tag, extra, idb(mesh_uid))
    idata = comp.fields["InstanceData"].obj
    idata.fields["Mesh"] = Ptr(1, idb(mesh_uid))
    cmi = getattr(idata.fields.get("CompiledMeshInstance"), "obj", None)
    if cmi is not None:   # no baked ambient occlusion: retail instances without it carry an empty buffer
        cmi.fields["HasAmbientOcclusion"] = b"\x00"
        cmi.fields["VertexBuffer"] = []
        cmi.fields["VertexFormat"] = b"\x00"
        cmi.fields["MeshHash"] = bytes(len(cmi.fields["MeshHash"]))
    proto = idata.fields["MaterialInfos"][0]
    infos = []
    for m in mats:
        mi = copy.deepcopy(proto)
        mi.fields["GraphicObjectInstance"] = Ptr(2, idata.id)
        mi.fields["MeshMaterial"] = Handle(0, idb(m))
        r = mi.fields["InstanceMaterial"]
        mi.fields["InstanceMaterial"] = Ref(r.tag, r.extra, idb(m))
        infos.append(mi)
    idata.fields["MaterialInfos"] = infos
    localize_links(doc, comp, fn)
    status = next((p.status for p in o.fields["Components"] if isinstance(p, Ptr) and p.obj is not None), 4)
    o.fields["Components"] = [Ptr(status, None, comp)] + [
        p for p in o.fields["Components"]
        if not (isinstance(p, Ptr) and p.obj is not None and type_name(p.obj.type_hash) == "Visual")]

    # culling bounds (entity-local): the new mesh together with the collision
    pts = list(mesh_input.verts)
    from .geom import mesh_shape_geometry
    for _i, ic in inert_components(o):
        sid = u32(ic.fields["RigidBody"].fields["Shape"].id)
        if sid in doc.info and doc.type_of(sid) == "MeshShape":
            pts += mesh_shape_geometry(doc.obj(sid))[0]
    bv = o.fields.get("BoundingVolume")
    if bv is not None and pts:
        bv.fields["Min"] = struct.pack("<3f", *(min(p[k] for p in pts) for k in range(3)))
        bv.fields["Max"] = struct.pack("<3f", *(max(p[k] for p in pts) for k in range(3)))
        bv.fields["Type"] = (0).to_bytes(4, "little")
    if is_editor_id(key[0]):   # bounds changed: an editor-made element's size class follows
        fit_new_element(o)
    doc.touch(key[0])
    return mesh_uid


def new_scenery(doc: MapDocument, matrix: bytes, mesh_input, template_key=None,
                block: int | None = None) -> tuple[int, int]:
    """A new visual-only element (no collision, no climb edges) showing mesh_input, in `block` (default: the
    always-loaded whole-map cell). Returns its key."""
    t = template_key or collision_template(doc).key
    key = duplicate(doc, t, matrix, block if block is not None else top_block(doc))
    o = element_obj(doc, key)
    o.fields["Components"] = [p for p in o.fields["Components"]
                              if not (isinstance(p, Ptr) and p.obj is not None
                                      and type_name(p.obj.type_hash) in ("InertComponent", "GuidanceSystem"))]
    set_visual(doc, key, mesh_input)
    fit_new_element(o)
    return key
