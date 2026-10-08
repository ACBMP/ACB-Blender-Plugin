"""Structural checks run on every saved forge before it may be installed. Each one is an engine rule that broke a
real build during the ACR port (acr-map-port NOTES.md); the matching standalone scripts there are aligncheck.py,
blockcheck.py, spawncheck.py and depcheck.py."""
from __future__ import annotations

from collections import Counter

from anvilforge.fastload import Handle, Ptr, Ref, walk
from anvilforge.forge import read_header
from anvilforge.fileset import iter_fileset_entries

from .doc import MapDocument, u32
from .geom import mesh_shape_geometry
from .kinds import block_membership, classify
from .ops import RUNTIME_ID_BASE, is_editor_id


def check_alignment(forge_path: str) -> list[str]:
    """No entry header + dependency table may straddle a 0x8000 streaming chunk (HandleChunkPrefetches reads it in
    place); entries 16-aligned like retail."""
    out = []
    with open(forge_path, "rb") as f:
        for s in range(read_header(f, 25)):
            for e in list(iter_fileset_entries(f, s, True)):
                f.seek(e.offset + 0x1B8)
                n = int.from_bytes(f.read(4), "little")
                end = e.offset + 0x1B8 + 4 + 8 * n - 1
                if e.offset // 0x8000 != end // 0x8000:
                    out.append(f"align: entry {e.name} header straddles a 0x8000 chunk")
                if e.offset % 16:
                    out.append(f"align: entry {e.name} not 16-aligned")
    return out


def check_blocks(doc: MapDocument) -> list[str]:
    """Activated objects of a block must live in the block's own entry, or they are never added to the world."""
    out = []
    for b in doc.uids("GridCellDataBlock"):
        own = doc.entry_of(b)
        if doc.entry_root(own) != b:
            continue
        o = doc.obj(b)
        n = u32(o.fields["NumberOfObjectsToActivate"])
        if n > len(o.fields["Objects"]):
            out.append(f"block: {doc.name_of(b)} activates {n} of {len(o.fields['Objects'])} objects")
        for r in o.fields["Objects"][:n]:
            u = u32(r.id)
            if u not in doc.where:
                out.append(f"block: {doc.name_of(b)} activates missing object {u:#x}")
            elif all(fn != own for fn, _ in doc.where[u]):
                out.append(f"block: {doc.name_of(b)} activates {doc.name_of(u)}, which lives in another entry")
    return out


def check_spawns(doc: MapDocument) -> list[str]:
    act = block_membership(doc)
    return [f"spawn: {doc.name_of(e.uid)} is not activated by any block" for e in classify(doc, with_children=False)
            if e.kind in ("spawn", "chest_spawn") and e.uid not in act]


def check_deps(doc: MapDocument) -> list[str]:
    """Entry dependency tables may only name entries of the same forge (the streaming loader crashed otherwise)."""
    entry_ids = {e.id for e in doc.entries} | {doc.entry_root(fn) for fn in doc.fnames if fn.endswith(".data")}
    entry_ids.discard(None)
    out = []
    for fn in doc.fnames:
        if not fn.endswith(".data"):
            continue
        for d in doc._file(fn).deps:
            if (d.id & 0xFFFFFFFF) not in entry_ids:
                out.append(f"deps: {fn} depends on {d.id & 0xFFFFFFFF:#x}, not an entry of this forge")
    return out


def check_editor_ids(doc: MapDocument) -> list[str]:
    """Objects created by the editor (ops.ID_RANGE_LO..HI): unique, and every link into the range resolves; nothing may
    use the engine's runtime id range (>= 0xF0000000)."""
    owner: dict[int, int] = {}
    out = []
    links = []
    for u in doc.info:
        r = doc.root(u)
        if r is None:
            continue
        seen_here = set()
        for o in walk(r.obj):
            i = u32(o.id)
            if i >= RUNTIME_ID_BASE:
                out.append(f"ids: {doc.name_of(u)} holds {i:#x}, in the engine's runtime id range (never shows up in "
                           "game; reopen the map to renumber it)")
            if is_editor_id(i) and i not in seen_here:
                seen_here.add(i)
                if i in owner and owner[i] != u:
                    out.append(f"ids: {i:#x} used in both {doc.name_of(owner[i])} and {doc.name_of(u)}")
                owner.setdefault(i, u)
            for v in o.fields.values():
                for x in v if isinstance(v, list) else [v]:
                    t = (u32(x.id) if isinstance(x, (Handle,)) or (isinstance(x, Ref) and x.obj is None)
                         else u32(x.link) if isinstance(x, Ptr) and x.obj is None and x.link is not None else None)
                    if t is not None and is_editor_id(t):
                        links.append((u, t))
    known = set(owner) | set(doc.info)
    out += [f"ids: {doc.name_of(u)} links to {t:#x}, which doesn't exist" for u, t in links if t not in known]
    return out


def check_mesh_shapes(doc: MapDocument) -> list[str]:
    out = []
    for u in doc.uids("MeshShape"):
        o = doc.obj(u)
        if o is None:
            continue
        v, t, m = mesh_shape_geometry(o)
        if any(i >= len(v) for tri in t for i in tri):
            out.append(f"collision: {doc.name_of(u)} has triangle indices past its {len(v)} vertices")
        if len(m) != len(t):
            out.append(f"collision: {doc.name_of(u)} has {len(m)} material indices for {len(t)} triangles")
        if m and max(m) >= len(o.fields["Materials"]):
            out.append(f"collision: {doc.name_of(u)} uses material {max(m)} of {len(o.fields['Materials'])}")
        if not o.fields["MoppCode"] and o.fields["MoppCodeVersionNumber"] == (5).to_bytes(4, "little"):
            out.append(f"collision: {doc.name_of(u)} has no MOPP but claims version 5 (ACB would trust it)")
    return out


def check_compounds(doc: MapDocument) -> list[str]:
    """Compound collision (ops.compounds): every member exists, and every InertComponent with IsMerged=1 belongs to a
    compound -- a merged one outside any never adds its rigid body, so it has no collision in game."""
    from .ops import compounds, inert_components
    comps = compounds(doc)
    members = {i for mem in comps.values() for i in mem}
    out = []
    ents = {}
    for e in classify(doc):
        ents[u32(e.obj.id)] = e
        if u32(e.obj.id) not in members and any(ic.fields.get("IsMerged", b"\x00") != b"\x00"
                                                 for _i, ic in inert_components(e.obj)):
            out.append(f"merge: {doc.name_of(e.uid)}{'' if e.child < 0 else f'[{e.child}]'} has IsMerged=1 but no "
                       "compound holds it (no collision in game)")
    for m, mem in comps.items():
        missing = [i for i in mem if i not in ents]
        if missing:
            out.append(f"merge: compound {doc.name_of(m)} lists {len(missing)} missing member(s), e.g. {missing[0]:#x}")
    return out


def run_all(doc: MapDocument) -> list[str]:
    return (check_alignment(doc.path) + check_blocks(doc) + check_spawns(doc) + check_deps(doc)
            + check_editor_ids(doc) + check_mesh_shapes(doc) + check_compounds(doc))


def summary(problems: list[str]) -> Counter:
    return Counter(p.split(":", 1)[0] for p in problems)


def new_problems(out_doc: MapDocument, src_doc: MapDocument) -> list[str]:
    """Problems of a saved forge that its source doesn't already have (retail maps carry a few harmless ones: an
    unaligned first entry, one Alhambra header straddle, San Marco's elevator objects outside their block entry)."""
    base = set(run_all(src_doc))
    return [p for p in run_all(out_doc) if p not in base]
