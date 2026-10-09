"""Player spawn points: entities with a MultiSpawnPlayerComponent {SpawnType, TeamIndex}.

Every retail map has the same three kinds (census 2026-10-09, all 11 maps + Rome):
- Standard (SpawnType 0, TeamIndex 0): free-for-all respawns, no data layer.
- Team (SpawnType 2, TeamIndex 1-4): exactly 4 per team index on every map, no data layer.
- Chest (SpawnType 3, TeamIndex 0): Chest Capture chest positions, filtered on one data layer (0xd41d5cdd,
  Action 0 = loaded while that layer is active); the chest world data lists them (worlddata.sync_chests).
SpawnType 1 (Tutorial) appears once, on Rome only.

A spawn faces along its local +Y (the matrix's second row); its position is the player's feet.
"""
from __future__ import annotations

from anvilforge.fastload import Obj

from . import ops
from .doc import MapDocument, idb, u32
from .kinds import classify, component

STANDARD, TUTORIAL, TEAM, CHEST = 0, 1, 2, 3
TYPE_NAMES = {STANDARD: "Free-for-all", TUTORIAL: "Tutorial", TEAM: "Team", CHEST: "Chest"}
CHEST_LAYER = 0xd41d5cdd   # the layer every retail chest spawn is filtered on
SPAWN_KINDS = ("spawn", "chest_spawn")


def spawn_info(o: Obj) -> tuple[int, int] | None:
    """(SpawnType, TeamIndex) of an entity, None if it isn't a spawn."""
    c = component(o, "MultiSpawnPlayerComponent")
    if c is None:
        return None
    return u32(c.fields["SpawnType"]), u32(c.fields["TeamIndex"])


def label(spawn_type: int, team: int) -> str:
    if spawn_type == TEAM:
        return f"Team {team}"
    return TYPE_NAMES.get(spawn_type, f"Type {spawn_type}")


def spawns(doc: MapDocument) -> list[tuple[tuple[int, int], int, int]]:
    """[(element key, SpawnType, TeamIndex)] for every spawn of the map."""
    out = []
    for e in classify(doc):
        if e.kind in SPAWN_KINDS:
            st, team = spawn_info(e.obj)
            out.append((e.key, st, team))
    return out


def counts(doc: MapDocument) -> dict[str, int]:
    out: dict[str, int] = {}
    for _k, st, team in spawns(doc):
        out[label(st, team)] = out.get(label(st, team), 0) + 1
    return out


def template(doc: MapDocument, spawn_type: int, team: int = 0):
    """Key of an existing root spawn to copy for a new one: same type and team if there is one, else same type,
    else any spawn."""
    best = None
    for key, st, tm in spawns(doc):
        if key[1] >= 0 or ops.owning_block(doc, key[0]) is None:
            continue
        score = (st == spawn_type) * 2 + (tm == team)
        if best is None or score > best[0]:
            best = (score, key)
    return best[1] if best else None


def _layer_actions(o: Obj) -> list:
    return o.fields["DataLayerFilter"].fields["LayerActions"]


def set_spawn(doc: MapDocument, key, spawn_type: int, team: int) -> None:
    """Make an existing spawn a different kind. Team spawns get team 1-4, the others team 0. Turning a spawn into a
    chest (or back) also sets (or clears) the chest data layer; call WorldData.sync_chests afterwards."""
    if spawn_type == TEAM and not 1 <= team <= 4:
        raise ops.EditError("team spawns need a team index 1-4")
    if spawn_type != TEAM:
        team = 0
    o = ops.element_obj(doc, key)
    c = component(o, "MultiSpawnPlayerComponent")
    if c is None:
        raise ops.EditError(f"{doc.name_of(key[0])} isn't a spawn")
    old_type = u32(c.fields["SpawnType"])
    c.fields["SpawnType"] = idb(spawn_type)[:len(c.fields["SpawnType"])]
    c.fields["TeamIndex"] = idb(team)[:len(c.fields["TeamIndex"])]
    if (old_type == CHEST) != (spawn_type == CHEST):
        acts = _layer_actions(o)
        if spawn_type == CHEST:
            acts[:] = _chest_layer_actions(doc)
        else:
            acts[:] = [a for a in acts if u32(a.fields["Layer"].id) != CHEST_LAYER]
    doc.touch(key[0])


def _chest_layer_actions(doc: MapDocument) -> list:
    """A fresh copy of a chest spawn's layer actions (from one of the map's chests, ids renumbered)."""
    for key, st, _t in spawns(doc):
        if st == CHEST:
            acts = _layer_actions(ops.element_obj(doc, key))
            if acts:
                return [ops.clone_tree(doc, a) for a in acts]
    raise ops.EditError("this map has no chest spawn to take the chest data layer from")


def add_spawn(doc: MapDocument, matrix: bytes, spawn_type: int = STANDARD, team: int = 0):
    """A new spawn at `matrix` (Entity.GlobalMatrix bytes), copied from one of the map's own spawns and then set to
    the requested kind. Returns its key."""
    src = template(doc, spawn_type, team)
    if src is None:
        raise ops.EditError("this map has no spawn to copy")
    key = ops.duplicate(doc, src, matrix)
    st, tm = spawn_info(ops.element_obj(doc, key))
    if (st, tm) != (spawn_type, team if spawn_type == TEAM else 0):
        set_spawn(doc, key, spawn_type, team)
    return key
