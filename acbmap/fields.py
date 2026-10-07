"""Schema-driven view of a decoded object for the generic inspector: list the serialized fields of any object with
readable values, and parse edited text back into the stored representation.

Paths are lists of field names and list indices, as ops.set_field takes them; pointers/references to inline objects
are followed transparently."""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

from anvilforge.fastload import SERIALIZED, Handle, Obj, Ptr, Ref
from anvilforge.schema import Kind

from .schema import codec, schema, type_name

_FMT = {Kind.BOOL: "?", Kind.CHAR: "b", Kind.INT8: "b", Kind.UINT8: "B", Kind.INT16: "h", Kind.UINT16: "H",
        Kind.INT32: "i", Kind.UINT32: "I", Kind.INT64: "q", Kind.UINT64: "Q", Kind.FLOAT: "f",
        Kind.VECTOR2: "2f", Kind.VECTOR3: "3f", Kind.VECTOR4: "4f", Kind.QUATERNION: "4f", Kind.MATRIX3X3: "9f",
        Kind.MATRIX4X4: "16f"}


@dataclass
class Row:
    path: list
    label: str
    kind: int
    text: str
    depth: int
    editable: bool = False
    expandable: bool = False      # an object (or list of objects) that can be expanded
    link: int | None = None       # target id of a link/handle/reference
    enum: list[tuple[int, str]] = field(default_factory=list)


def props(type_hash: int) -> list:
    """[(name, Property)] serialized fields of a type, base class first."""
    S = schema()
    return [(S.name_of(p.name_hash), p) for _c, ps in codec().levels(type_hash) for p in ps]


def enum_values(prop) -> list[tuple[int, str]]:
    S = schema()
    e = S.global_enums_by_hash.get(prop.object_hash)
    if e is None:
        for t in S.types_by_hash.values():
            e = next((x for x in t.enums if x.name_hash == prop.object_hash), None)
            if e is not None:
                break
    if e is None:
        return []
    return [(v.value, S.name_of(v.name_hash).split("_", 1)[-1] if "_" in S.name_of(v.name_hash)
             else S.name_of(v.name_hash)) for v in e.values]


def format_value(kind: int, b: bytes, enum=None) -> str:
    if kind == Kind.ENUM:
        v = int.from_bytes(b, "little")
        name = dict(enum or []).get(v)
        return f"{name} ({v})" if name else str(v)
    if kind == Kind.OBJECT_ID:
        return f"{int.from_bytes(b, 'little'):#010x}"
    f = _FMT.get(kind)
    if f is None:
        return b.hex()
    vals = struct.unpack("<" + f, b)
    if kind == Kind.BOOL:
        return "true" if b != b"\0" else "false"
    if kind in (Kind.FLOAT, Kind.VECTOR2, Kind.VECTOR3, Kind.VECTOR4, Kind.QUATERNION, Kind.MATRIX3X3,
                Kind.MATRIX4X4):
        return ", ".join(f"{v:.6g}" for v in vals)
    return ", ".join(str(v) for v in vals)


def parse_value(kind: int, text: str, enum=None) -> bytes:
    text = text.strip()
    if kind == Kind.BOOL:
        return b"\1" if text.lower() in ("1", "true", "yes", "on") else b"\0"
    if kind == Kind.ENUM:
        for v, n in enum or []:
            if text.lower() in (n.lower(), str(v)):
                return v.to_bytes(4, "little")
        return int(text.split()[0], 0).to_bytes(4, "little")
    if kind == Kind.OBJECT_ID:
        return int(text, 0).to_bytes(4, "little")
    f = _FMT.get(kind)
    if f is None:
        raise ValueError(f"kind {kind} isn't editable as text")
    parts = [p for p in text.replace(",", " ").split() if p]
    conv = float if f[-1] == "f" else (lambda s: int(s, 0))
    return struct.pack("<" + f, *[conv(p) for p in parts])


def _link_of(v) -> int | None:
    if isinstance(v, Handle):
        return int.from_bytes(v.id, "little")
    if isinstance(v, Ptr) and v.obj is None and v.link is not None:
        return int.from_bytes(v.link, "little")
    if isinstance(v, Ref) and v.obj is None:
        return int.from_bytes(v.id, "little")
    return None


def _inline(v):
    if isinstance(v, Obj):
        return v
    if isinstance(v, (Ptr, Ref)) and v.obj is not None:
        return v.obj
    return None


def rows(obj: Obj, expanded: set[tuple], path: tuple = (), depth: int = 0, max_list: int = 64) -> list[Row]:
    """Rows for `obj`'s fields; objects whose path is in `expanded` are opened recursively."""
    out = []
    for name, p in props(obj.type_hash):
        v = obj.fields.get(name)
        fp = path + (name,)
        k = p.kind
        if k in (Kind.STATIC_ARRAY, Kind.BIG_ARRAY, Kind.SMALL_ARRAY):
            items = v or []
            sub = _inline(items[0]) if items else None
            if sub is not None or (items and _link_of(items[0]) is not None):
                out.append(Row(list(fp), f"{name} [{len(items)}]", k, "", depth, expandable=True))
                if fp in expanded:
                    for i, it in enumerate(items[:max_list]):
                        io = _inline(it)
                        ip = fp + (i,)
                        if io is not None:
                            out.append(Row(list(ip), f"[{i}] {type_name(io.type_hash)}", Kind.OBJECT, "", depth + 1,
                                           expandable=True))
                            if ip in expanded:
                                out += rows(io, expanded, ip, depth + 2)
                        else:
                            t = _link_of(it)
                            out.append(Row(list(ip), f"[{i}]", Kind.REFERENCE, f"-> {t:#010x}", depth + 1, link=t))
            else:
                ek = p.elem_kind
                text = f"{len(items)} x {Kind(ek).name.lower() if ek in Kind._value2member_map_ else ek}"
                if 0 < len(items) <= 4 and ek in _FMT:
                    text = "; ".join(format_value(ek, x) for x in items)
                out.append(Row(list(fp), name, k, text, depth))
            continue
        io = _inline(v)
        if io is not None:
            out.append(Row(list(fp), f"{name}: {type_name(io.type_hash)}", Kind.OBJECT, "", depth, expandable=True))
            if fp in expanded:
                out += rows(io, expanded, fp, depth + 1)
            continue
        t = _link_of(v)
        if t is not None or isinstance(v, (Ptr, Ref, Handle)):
            out.append(Row(list(fp), name, k, "null" if not t else f"-> {t:#010x}", depth, link=t or None))
            continue
        if isinstance(v, bytes) and k in (Kind.STRING, Kind.LSTRING):
            txt = v.decode("utf-16-le" if k == Kind.LSTRING else "latin-1", "replace")
            out.append(Row(list(fp), name, k, txt, depth))
            continue
        if isinstance(v, bytes):
            ev = enum_values(p) if k == Kind.ENUM else []
            out.append(Row(list(fp), name, k, format_value(k, v, ev), depth,
                           editable=k in _FMT or k in (Kind.ENUM, Kind.OBJECT_ID), enum=ev))
    return out


def get(obj: Obj, path) -> object:
    cur = obj
    for p in path:
        cur = cur.fields[p] if isinstance(cur, Obj) else cur[p]
        if isinstance(cur, (Ptr, Ref)) and cur.obj is not None:
            cur = cur.obj
    return cur


def kind_at(obj: Obj, path) -> tuple[int, list]:
    """(Kind, enum values) of the primitive field at `path`."""
    parent = get(obj, path[:-1]) if len(path) > 1 else obj
    if not isinstance(parent, Obj):
        raise ValueError("list elements are edited through their object")
    p = dict(props(parent.type_hash))[path[-1]]
    return p.kind, (enum_values(p) if p.kind == Kind.ENUM else [])
