"""Edit operations on map entities. An element is addressed by (root uid, child index): child -1 is the root Entity /
EntityGroup itself, child >= 0 an entity inlined in the group's Entities list.

Engine rules these follow (acr-map-port NOTES.md):
- A root is loaded only when a GridCellDataBlock lists it in the first NumberOfObjectsToActivate entries of Objects,
  and it must live in that block's own entry (blockcheck rule).
- Entity.GlobalMatrix is world space for roots and group children alike; component zones/shapes are entity-local.
- New ids come from a reserved range (0xF0xxxxxx is unused by every ACB multi forge), one 64k slice per world.
"""
from __future__ import annotations

import copy

from anvilforge.fastload import Handle, Obj, Ptr, Ref, Root, walk

from .doc import MapDocument, idb, u32
from .geom import matrix_rows, pack_floats
from .kinds import group_children
from .schema import type_name

ID_RANGE_BASE = 0xF0000000


class EditError(Exception):
    pass


# ------------------------------------------------------------------ ids --

def id_base(doc: MapDocument) -> int:
    worlds = doc.uids("World")
    w = worlds[0] if worlds else 0
    return ID_RANGE_BASE | ((((w * 2654435761) & 0xFFFFFFFF) >> 8 & 0xFF) << 16)


def all_ids(doc: MapDocument) -> set[int]:
    """Every object id known in the document: roots plus every sub-object of every decodable root."""
    if getattr(doc, "_all_ids", None) is None:
        ids = set(doc.info)
        for u in doc.info:
            r = doc.root(u)
            if r is not None:
                ids.update(u32(o.id) for o in walk(r.obj))
        ids.discard(0)
        doc._all_ids = ids
    return doc._all_ids


def fresh_ids(doc: MapDocument, n: int) -> list[int]:
    used = all_ids(doc)
    base = id_base(doc)
    i = base
    while True:
        block = list(range(i, i + n))
        if not any(b in used for b in block):
            used.update(block)
            return block
        i += n
        if i + n > base + 0x10000:
            raise EditError("this world's id range is exhausted")


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


def duplicate(doc: MapDocument, key, matrix: bytes | None = None) -> tuple[int, int]:
    """Copy an element next to itself (same block/entry for a root, same group for a child). Returns its key."""
    uid, child = key
    src = element_obj(doc, key)
    new = clone_tree(doc, src)
    if matrix is not None:
        new.fields["GlobalMatrix"] = matrix
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
    refs = [h for h in references_to(doc, ids, skip={uid} | blocks) if not (child >= 0 and h[0] == uid)]
    if refs and not force:
        raise EditError(f"still referenced by {len(refs)} object(s): " +
                        ", ".join(f"{doc.name_of(u)} ({f})" for u, f in refs[:5]))
    if child >= 0:
        g = doc.obj(uid)
        del g.fields["Entities"][child]
        doc.touch(uid)
        return
    for b in blocks:
        o = doc.obj(b)
        objs = o.fields["Objects"]
        n = u32(o.fields["NumberOfObjectsToActivate"])
        keep = [x for x in objs if u32(x.id) != uid]
        if len(keep) != len(objs):
            removed_active = sum(1 for x in objs[:n] if u32(x.id) == uid)
            o.fields["Objects"] = keep
            o.fields["NumberOfObjectsToActivate"] = idb(n - removed_active)
            doc.touch(b)
    doc.remove_root(uid)


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


def collision_template(doc: MapDocument):
    """A plain static collision entity of this map to clone new collision from (only Inert/Guidance/Visual
    components, one shape)."""
    from .kinds import classify
    best = None
    for e in classify(doc, with_children=False):
        if e.kind != "collision":
            continue
        names = set(e.components)
        if names <= {"InertComponent", "GuidanceSystem", "Visual"} and len(inert_components(e.obj)) == 1:
            score = (len(names), len(doc.obj(u32(inert_components(e.obj)[0][1].fields["RigidBody"].fields["Shape"].id))
                                     .fields["Vertices"]))
            if best is None or score < best[0]:
                best = (score, e)
    if best is None:
        raise EditError("no plain collision entity in this map to use as a template")
    return best[1]


def new_collision(doc: MapDocument, matrix: bytes, verts, tris, mats, template_key=None) -> tuple[int, int]:
    """A new static collision entity (no visual, no climb edges) with its own MeshShape, cloned from a plain collision
    entity of the map and placed in the same grid cell block. Returns its key."""
    from .geom import set_mesh_shape_geometry
    t = template_key or collision_template(doc).key
    key = duplicate(doc, t, matrix)
    o = element_obj(doc, key)
    o.fields["Components"] = [p for p in o.fields["Components"]
                              if not (isinstance(p, Ptr) and p.obj is not None and type_name(p.obj.type_hash) == "Visual")]
    strip_guidance(o)
    sid = make_shape_unique(doc, key)
    shape = doc.obj(sid)
    set_mesh_shape_geometry(shape, verts, tris, mats)
    doc.touch(sid)
    doc.touch(key[0])
    return key
