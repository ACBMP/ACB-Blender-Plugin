"""acbmap command line.

    acbmap roundtrip <forge> [--touch TYPE ...]   save with no (or no-op) edits, verify the content is identical
    acbmap dump <forge>                           editable elements per kind
    acbmap check <forge> [--against SRC]          structural checks (only new ones vs SRC)
    acbmap install <forge> | uninstall <name> | status
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import tempfile
import time
from collections import Counter

from anvilforge.datafile import derive_uid_and_ext, iter_datafile_subparts
from anvilforge.forge import unpack

from .doc import MapDocument
from .schema import GAME


def forge_contents(path: str) -> dict[int, list]:
    """uid -> sorted list of payloads (all copies) for every sub-object in a forge."""
    out: dict[int, list] = {}
    with tempfile.TemporaryDirectory() as d:
        unpack(path, d, GAME)
        for fn in os.listdir(d):
            if fn.endswith(".data"):
                with open(os.path.join(d, fn), "rb") as f:
                    for _i, _e, _n, p in iter_datafile_subparts(io.BytesIO(f.read()), GAME):
                        out.setdefault(derive_uid_and_ext(p, True)[0], []).append(p)
    return {k: sorted(v) for k, v in out.items()}


def cmd_roundtrip(a) -> int:
    t = time.time()
    doc = MapDocument(a.forge)
    print(f"open {time.time() - t:.1f}s: {len(doc.info)} objects in {len(doc.fnames)} entries")
    n = 0
    for tn in a.touch or []:
        for u in doc.uids(tn):
            if doc.root(u) is not None:
                doc.touch(u)
                n += 1
    print(f"touched {n} roots")
    out = a.out or os.path.join(doc.cache, "roundtrip.forge")
    t = time.time()
    rebuilt = doc.save(out)
    print(f"save {time.time() - t:.1f}s, rebuilt {len(rebuilt)} entries -> {out}")
    a_c, b_c = forge_contents(a.forge), forge_contents(out)
    diff = [u for u in set(a_c) | set(b_c) if a_c.get(u) != b_c.get(u)]
    print(f"content: {len(a_c)} vs {len(b_c)} objects, {len(diff)} differ")
    for u in diff[:10]:
        print(f"  {u:#x} {doc.name_of(u)} {doc.type_of(u) if u in doc.info else '?'}")
    return 1 if diff else 0


def cmd_dump(a) -> int:
    from .kinds import classify
    doc = MapDocument(a.forge)
    elems = classify(doc)
    c = Counter(e.kind for e in elems)
    for k, v in sorted(c.items()):
        print(f"{v:6d}  {k}")
    if a.verbose:
        for e in elems:
            print(f"{e.kind:16s} {e.uid:#010x} {doc.name_of(e.uid)}  {e.position}")
    return 0


def cmd_check(a) -> int:
    from .checks import new_problems, run_all
    doc = MapDocument(a.forge)
    problems = new_problems(doc, MapDocument(a.against)) if a.against else run_all(doc)
    for p in problems:
        print(p)
    print(f"{len(problems)} problems")
    return 1 if problems else 0


def cmd_install(a) -> int:
    from . import install
    from .checks import new_problems
    if not a.force:
        src = a.source or os.path.join(a.game_multi, os.path.basename(a.forge)) + ".orig"
        if not os.path.exists(src):
            src = os.path.join(a.game_multi, os.path.basename(a.forge))
        problems = new_problems(MapDocument(a.forge), MapDocument(src))
        if problems:
            print("\n".join(problems))
            print(f"refusing to install: {len(problems)} new problems (--force to override)")
            return 1
    print("installed", install.install(a.forge, a.game_multi))
    return 0


def cmd_uninstall(a) -> int:
    from . import install
    print("restored", install.uninstall(a.forge_name, a.game_multi))
    return 0


def cmd_status(a) -> int:
    from . import install
    for n in install.status(a.game_multi):
        print("edited:", n)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="acbmap")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("roundtrip")
    r.add_argument("forge")
    r.add_argument("--out")
    r.add_argument("--touch", nargs="*", help="re-encode every root of these types")
    r.set_defaults(fn=cmd_roundtrip)
    d = sub.add_parser("dump")
    d.add_argument("forge")
    d.add_argument("-v", "--verbose", action="store_true")
    d.set_defaults(fn=cmd_dump)
    c = sub.add_parser("check")
    c.add_argument("forge")
    c.add_argument("--against", help="source forge: report only problems it doesn't have")
    c.set_defaults(fn=cmd_check)
    from .install import GAME_MULTI
    i = sub.add_parser("install", help="install an edited forge over the game's copy (original kept as .orig)")
    i.add_argument("forge")
    i.add_argument("--source", help="forge it was edited from (default: the game's .orig or current copy)")
    i.add_argument("--game-multi", default=GAME_MULTI)
    i.add_argument("--force", action="store_true")
    i.set_defaults(fn=cmd_install)
    u = sub.add_parser("uninstall", help="restore the game's original forge")
    u.add_argument("forge_name")
    u.add_argument("--game-multi", default=GAME_MULTI)
    u.set_defaults(fn=cmd_uninstall)
    st = sub.add_parser("status", help="list maps currently replaced by edited forges")
    st.add_argument("--game-multi", default=GAME_MULTI)
    st.set_defaults(fn=cmd_status)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
