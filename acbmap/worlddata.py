"""Per-world game-mode data: Escort (TeamVIP) paths and Chest Capture points.

ACB finds them through the AdditionalWorldDataDLCElement table of the skins DLC packages ({world id -> [(game mode,
AdditionalWorldData id)]}; mode 2 ChestCapture, 6 Assassinate, 7 TeamVIP) and loads the object by id through
LoadOnDemandManager, which probes the map forge before the skins forges. So an edited copy shipped in the MAP forge
under the same id overrides the skins one; the skins forges are never written (acr-map-port NOTES.md, "Test 9").

- TeamVIP: TeamVIPPaths -> TeamVIPNavflowPath.Path -> nodes {NavFlow handle (a CrowdFlow entity of the map),
  IsSpawnPoint, IsCheckpoint}. Shipped as a one-object, dependency-free entry like retail's.
- ChestCapture: chestSpawnPoints = Refs to the map's SpawnType-3 spawn entities; the entry also carries phantom twins
  of those entities (IsPhantom=1, inline component pointers status 0 + flag 1 -- reproduces retail byte for byte).
  The editor regenerates it from the map's current chest spawns on every save.
"""
from __future__ import annotations

import copy
import os

from anvilforge.fastload import Handle, Obj, Ptr, Ref, Root, walk

from .doc import MapDocument, idb, u32
from .kinds import classify
from .schema import type_hash, type_name

SKINS = ("DataPC_skins_0002_00000004_dlc.forge", "DataPC_skins_0001_00000002_dlc.forge",
         "DataPC_skins_0000_00000001_dlc.forge")
MODE_CHEST, MODE_ASSASSINATE, MODE_TEAMVIP = 2, 6, 7


class WorldData:
    def __init__(self, doc: MapDocument, multi_dir: str | None = None):
        self.doc = doc
        self.multi_dir = multi_dir or os.path.dirname(doc.path)
        worlds = doc.uids("World")
        self.world = worlds[0] if worlds else None
        self.modes: dict[int, int] = {}
        self._skins: list[MapDocument] = []
        for fg in SKINS:
            p = os.path.join(self.multi_dir, fg)
            if not os.path.exists(p):
                continue
            s = MapDocument(p)
            self._skins.append(s)
            if not self.modes:
                self.modes = self._holder_modes(s)

    def _holder_modes(self, s: MapDocument) -> dict[int, int]:
        for u in s.uids("ContentPackage"):
            o = s.obj(u)
            if o is None:
                continue
            for x in walk(o):
                if type_name(x.type_hash) == "AdditionalWorldDataHolder" and u32(x.fields["AssociatedWorld"]) == self.world:
                    return {u32(pm.fields["AssociatedGameModeID"]): u32(pm.fields["AdditionalWorldData"].id)
                            for pm in x.fields["PerGameModeData"]}
        return {}

    def get(self, mode: int) -> Root | None:
        """The mode's AdditionalWorldData root: the map forge's override if it has one, else a copy of the skins'."""
        aid = self.modes.get(mode)
        if aid is None:
            return None
        if aid in self.doc.info:
            return self.doc.root(aid)
        for s in self._skins:
            if aid in s.info:
                return copy.deepcopy(s.root(aid))
        return None

    # ------------------------------------------------------------ escort --

    def vip_paths(self) -> list[list[dict]]:
        """[[{flow: CrowdFlow entity uid, spawn: bool, checkpoint: bool}]] per path."""
        r = self.get(MODE_TEAMVIP)
        if r is None:
            return []
        return [[{"flow": u32(n.fields["NavFlow"].id), "spawn": n.fields["IsSpawnPoint"] != b"\0",
                  "checkpoint": n.fields["IsCheckpoint"] != b"\0"} for n in p.fields["Path"]]
                for p in r.obj.fields["TeamVIPPaths"]]

    def set_vip_paths(self, paths: list[list[dict]]) -> None:
        """Write Escort paths (same shape as vip_paths()) into the map forge's override entry."""
        aid = self.modes.get(MODE_TEAMVIP)
        if aid is None:
            raise ValueError("this world has no Escort (TeamVIP) row in the skins world-data table")
        flows = {e.uid for e in classify(self.doc, with_children=False) if e.kind in ("crowd_flow", "nav_flow")}
        bad = [n["flow"] for p in paths for n in p if n["flow"] not in flows]
        if bad:
            raise ValueError(f"path nodes must be crowd/nav flow entities of this map: {[hex(b) for b in bad]}")
        base = self.get(MODE_TEAMVIP)
        old = base.obj.fields["TeamVIPPaths"] if base is not None else []
        flag = old[0].flag if old else None
        used = {u32(o.id) for o in (walk(base.obj) if base is not None else [])}
        nxt = [aid + 1]

        def fresh():
            while nxt[0] in used or nxt[0] in self.doc.info:
                nxt[0] += 1
            used.add(nxt[0])
            return nxt[0]

        out = []
        for i, p in enumerate(paths):
            # keep existing path/node ids so an unchanged path re-encodes byte-identically; fresh ids only for additions
            old_nodes = old[i].fields["Path"] if i < len(old) else []
            pid = u32(old[i].id) if i < len(old) else fresh()
            nodes = [Obj(type_hash("TeamVIPNavflowPathNode"),
                         old_nodes[j].id if j < len(old_nodes) else idb(fresh()),
                         {"NavFlow": Handle(0, idb(n["flow"])), "IsSpawnPoint": b"\1" if n["spawn"] else b"\0",
                          "IsCheckpoint": b"\1" if n["checkpoint"] else b"\0"}) for j, n in enumerate(p)]
            out.append(Obj(type_hash("TeamVIPNavflowPath"), idb(pid), {"Path": nodes}, flag))
        root = Root(b"", 0, Obj(type_hash("AdditionalWorldData_TeamVIP"), idb(aid), {"TeamVIPPaths": out}, flag=1))
        self._put(aid, [(root, "Unnamed")], "AdditionalWorldData_TeamVIP")

    # ------------------------------------------------------------ chests --

    def sync_chests(self) -> int:
        """Regenerate the Chest Capture world data from the map's SpawnType-3 spawn entities. Returns the count."""
        aid = self.modes.get(MODE_CHEST)
        if aid is None:
            return 0
        chests = sorted((self.doc.name_of(e.uid), e.uid) for e in classify(self.doc, with_children=False)
                        if e.kind == "chest_spawn" and e.child < 0)
        twins = []
        for name, uid in chests:
            r = copy.deepcopy(self.doc.root(uid))
            r.obj.fields["IsPhantom"] = b"\x01"
            comps = []
            for x in r.obj.fields["Components"]:
                if isinstance(x, Ptr) and x.obj is not None:
                    x.obj.flag = 1
                    comps.append(Ptr(0, obj=x.obj))
                else:
                    comps.append(x)
            r.obj.fields["Components"] = comps
            twins.append((r, name))
        root = Root(b"", 0, Obj(type_hash("AdditionalWorldData_ChestCapture"), idb(aid),
                                {"chestSpawnPoints": [Ref(1, 0, idb(u)) for _n, u in chests]}, flag=1))
        self._put(aid, [(root, "Unnamed")] + twins, "AdditionalWorldData_ChestCapture")
        return len(chests)

    def _put(self, aid: int, roots, name: str) -> None:
        if aid in self.doc.info and self.doc.type_of(aid).startswith("AdditionalWorldData"):
            self.doc.set_entry_roots(self.doc.entry_of(aid), roots)
        else:
            self.doc.add_entry(name, roots)
