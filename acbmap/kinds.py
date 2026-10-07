"""Classify a map's entities into editable element kinds.

An element is an Entity root, an EntityGroup root, or a child entity inlined in a group (EntityGroup.Entities, Ref
tag 0). Kinds come from components and from EntityDescriptor (DescriptorType 3 = Object, ObjectSubType names the
gameplay object: haystack, hiding place, bench, corner spin, ...; enum names from the schema).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from anvilforge.fastload import Obj, Ptr, Ref

from .doc import MapDocument, u32
from .geom import position
from .schema import schema, type_hash, type_name

OBJECT_SUBTYPES = {
    1: "weapon", 2: "ladder", 3: "pole", 4: "rope", 5: "breakable", 6: "kiosk", 7: "scaffold", 8: "haystack",
    9: "hiding_place", 10: "boat", 11: "horse_hitch", 12: "wagon_cart", 15: "glider", 16: "treasure_chest",
    17: "water_well", 18: "bench", 19: "smoke_bomb", 20: "corner_spin", 21: "spring_jump", 22: "money",
    23: "lute", 24: "chase_breaker_door",
}

# component -> kind, in priority order (first match wins)
COMPONENT_KINDS = [
    ("MultiSpawnPlayerComponent", "spawn"),
    ("OutOfBoundsComponent", "out_of_bounds"),
    ("ElevatorComponent", "elevator"),
    ("RestObjectAttributeComponent", "bench"),
    ("GameplayCoordinatorComponent", "blend_group"),
    ("FreeRunTargetingMagnetComponent", "freerun_magnet"),
    ("CrowdFlow", "crowd_flow"),
    ("NavFlow", "nav_flow"),
    ("TriggerComponent", "trigger"),
    ("InertComponent", "collision"),
    ("Visual", "visual"),
]

GAMEPLAY_KINDS = {"spawn", "chest_spawn", "out_of_bounds", "elevator", "bench", "blend_group", "freerun_magnet",
                  "crowd_flow", "nav_flow", "trigger", "chase_breaker"} | set(OBJECT_SUBTYPES.values())


@dataclass
class Element:
    kind: str
    uid: int                      # root object id (Entity or EntityGroup)
    obj: Obj                      # the entity itself (the group child for child elements)
    child: int = -1               # index in the group's Entities, -1 for a root
    components: list[str] = field(default_factory=list)
    block: int | None = None      # GridCellDataBlock uid that activates the root, if any

    @property
    def position(self):
        return tuple(round(c, 3) for c in position(self.obj.fields["GlobalMatrix"]))

    @property
    def key(self) -> tuple[int, int]:
        return (self.uid, self.child)


def components(o: Obj) -> list[tuple[str, Obj]]:
    out = []
    for p in o.fields.get("Components", []):
        if isinstance(p, Ptr) and p.obj is not None:
            out.append((type_name(p.obj.type_hash), p.obj))
    return out


def component(o: Obj, name: str) -> Obj | None:
    return next((c for n, c in components(o) if n == name), None)


def group_children(g: Obj) -> list[Obj]:
    return [e.obj for e in g.fields.get("Entities", []) if isinstance(e, Ref) and e.obj is not None]


def kind_of(o: Obj, is_group: bool = False) -> str:
    comps = components(o)
    names = {n for n, _ in comps}
    if "MultiSpawnPlayerComponent" in names:
        msp = dict(comps)["MultiSpawnPlayerComponent"]
        return "chest_spawn" if u32(msp.fields["SpawnType"]) == 3 else "spawn"
    if is_group and "Scene" in names and "TriggerComponent" in names and any(
            _object_subtype(c) == 24 for c in group_children(o)):
        return "chase_breaker"
    st = _object_subtype(o)
    if st in OBJECT_SUBTYPES:
        return OBJECT_SUBTYPES[st]
    for cname, kind in COMPONENT_KINDS:
        if cname in names:
            return kind
    return "group" if is_group else "other"


def _object_subtype(o: Obj) -> int | None:
    ed = o.fields.get("EntityDescriptor")
    if not isinstance(ed, Obj) or u32(ed.fields["DescriptorType"]) != 3:
        return None
    return u32(ed.fields["SubDescriptorType"])  # serialized slot of ObjectSubType


def block_membership(doc: MapDocument) -> dict[int, int]:
    """root uid -> GridCellDataBlock uid that activates it (first NumberOfObjectsToActivate entries)."""
    out = {}
    for b in doc.uids("GridCellDataBlock"):
        o = doc.obj(b)
        n = u32(o.fields["NumberOfObjectsToActivate"])
        for r in o.fields["Objects"][:n]:
            out.setdefault(u32(r.id), b)
    return out


def classify(doc: MapDocument, with_children: bool = True) -> list[Element]:
    blocks = block_membership(doc)
    out = []
    for u in doc.uids("Entity"):
        o = doc.obj(u)
        if o is None:
            continue
        out.append(Element(kind_of(o), u, o, -1, [n for n, _ in components(o)], blocks.get(u)))
    for u in doc.uids("EntityGroup"):
        g = doc.obj(u)
        if g is None:
            continue
        out.append(Element(kind_of(g, True), u, g, -1, [n for n, _ in components(g)], blocks.get(u)))
        if with_children:
            for i, c in enumerate(group_children(g)):
                out.append(Element(kind_of(c), u, c, i, [n for n, _ in components(c)], blocks.get(u)))
    return out


def enum_name(owner_type: str, field_name: str, value: int) -> str | None:
    """Schema enum name for a field's raw value (e.g. EntityDescriptor.ObjectSubType 8 -> ..._HayStack)."""
    s = schema()
    t = s.type_by_hash(type_hash(owner_type))
    while t is not None:
        for p in t.properties:
            if s.name_of(p.name_hash) == field_name and p.object_hash:
                return s.enum_value_name(p.object_hash, value)
        t = s.type_by_hash(t.base_type_hash)
    return None
