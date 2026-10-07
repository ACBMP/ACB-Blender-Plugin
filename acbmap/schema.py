"""ACB schema + codec singletons.

ACB_MP.schema has nameless placeholder props where the engine serializes a field; ACR's definitions of those types
match ACB's FastLoad code (acr-map-port NOTES.md), so the true layout is ACB with placeholders filled from ACR."""
from __future__ import annotations

from functools import lru_cache

from anvilforge.fastload import Codec
from anvilforge.games import Game
from anvilforge.schema import Schema

GAME = Game.BROTHERHOOD


@lru_cache(maxsize=None)
def schema() -> Schema:
    return Schema.load_default(GAME).with_placeholders_filled(Schema.load_default(Game.REVELATIONS))


@lru_cache(maxsize=None)
def codec() -> Codec:
    return Codec(schema())


def type_name(h: int) -> str:
    return schema().name_of(h)


@lru_cache(maxsize=None)
def type_hash(name: str) -> int:
    s = schema()
    return next(h for h in s.types_by_hash if s.name_of(h) == name)


@lru_cache(maxsize=None)
def is_a(h: int, base: str) -> bool:
    """True if type h is `base` or derives from it."""
    s = schema()
    t = s.type_by_hash(h)
    while t is not None:
        if s.name_of(t.type_hash) == base:
            return True
        t = s.type_by_hash(t.base_type_hash)
    return False
