"""Crowd flows as a graph, for Escort (TeamVIP) path editing.

A CrowdFlow entity is a polyline (NavFlowPointsLocal, entity-local) that NPCs walk; its ends connect to other flows
(HeadConnectingNavFlows / TailConnectingNavFlows, NavFlow entity handles; links aren't always listed on both
sides). An Escort path is an ordered list of flows (worlddata.vip_paths): in every retail path consecutive nodes are
connected flows, except one gap on Siena, and every path is a closed loop: its last flow connects back to its first
(which isn't repeated at the end). So a path is edited as a route through this graph: pick a start and an end flow
and the chain between them is filled in, and the loop is closed through the chain from the last flow to the first.

The flows' own points stay read-only: each point also carries a navmesh triangle and waypoint index into the opaque
NavMeshManager, which the editor can't rebuild.
"""
from __future__ import annotations

import heapq
import math
import struct

from .doc import MapDocument, u32
from .geom import matrix_rows
from .kinds import classify, component


class Flow:
    __slots__ = ("uid", "name", "points", "links")

    def __init__(self, uid, name, points, links):
        self.uid = uid
        self.name = name
        self.points = points   # world space
        self.links = links     # flow uids connected at either end

    @property
    def length(self) -> float:
        return sum(math.dist(a, b) for a, b in zip(self.points, self.points[1:]))

    @property
    def middle(self):
        return self.points[len(self.points) // 2] if self.points else (0.0, 0.0, 0.0)


def _world(rows, p):
    return tuple(p[0] * rows[0][i] + p[1] * rows[1][i] + p[2] * rows[2][i] + rows[3][i] for i in range(3))


def flow_component(o):
    return component(o, "CrowdFlow") or component(o, "NavFlow")


def flows(doc: MapDocument) -> dict[int, Flow]:
    """Every crowd/nav flow root of the map, with links made symmetric."""
    out: dict[int, Flow] = {}
    for e in classify(doc, with_children=False):
        if e.kind not in ("crowd_flow", "nav_flow"):
            continue
        cf = flow_component(e.obj)
        if cf is None:
            continue
        rows = matrix_rows(e.obj.fields["GlobalMatrix"])
        pts = [_world(rows, struct.unpack("<3f", p.fields["FlowPosition"][:12])) for p in cf.fields["NavFlowPointsLocal"]]
        links = {u32(h.id) for f in ("HeadConnectingNavFlows", "TailConnectingNavFlows") for h in cf.fields.get(f, [])}
        out[e.uid] = Flow(e.uid, doc.name_of(e.uid), pts, links)
    for f in out.values():
        f.links &= out.keys()
    for f in list(out.values()):
        for o in f.links:
            out[o].links.add(f.uid)
    return out


def connected(fl: dict[int, Flow], a: int, b: int) -> bool:
    return a in fl and b in fl[a].links


def chain(fl: dict[int, Flow], a: int, b: int) -> list[int] | None:
    """Shortest walk (by flow length) from flow a to flow b through connected flows, both ends included; None if b
    can't be reached from a."""
    if a not in fl or b not in fl:
        return None
    dist, prev = {a: 0.0}, {}
    q = [(0.0, a)]
    while q:
        d, u = heapq.heappop(q)
        if u == b:
            break
        if d > dist[u]:
            continue
        for v in fl[u].links:
            nd = d + fl[v].length
            if nd < dist.get(v, math.inf):
                dist[v], prev[v] = nd, u
                heapq.heappush(q, (nd, v))
    if b not in dist:
        return None
    out = [b]
    while out[-1] != a:
        out.append(prev[out[-1]])
    return out[::-1]


def extend(fl: dict[int, Flow], path: list[int], target: int) -> tuple[list[int], bool]:
    """Flows to append to `path` to reach `target`: the connecting chain (without the path's last node), or just
    [target] when it can't be reached. Returns (flows, connected)."""
    if not path:
        return [target], True
    c = chain(fl, path[-1], target)
    if c is None:
        return [target], False
    return c[1:], True


def closing(fl: dict[int, Flow], path: list[int]) -> list[int] | None:
    """Flows to append so the path's last flow connects back to its first (closing the loop): [] when it already
    does, None when the first flow can't be reached from the last."""
    if len(path) < 2 or connected(fl, path[-1], path[0]):
        return []
    c = chain(fl, path[-1], path[0])
    return None if c is None else c[1:-1]


def gaps(fl: dict[int, Flow], path: list[int]) -> list[int]:
    """Indices i where node i and the next one aren't connected flows; the last node's next is the first (the path
    is a loop)."""
    n = len(path)
    if n < 2:
        return []
    return [i for i in range(n) if not connected(fl, path[i], path[(i + 1) % n])]


def oriented(fl: dict[int, Flow], path: list[int]) -> list[list[tuple]]:
    """Each node's points in walking order: every flow is turned so that it ends at the end nearest the next node
    (the last node's next is the first: paths are loops)."""
    out = []
    for i, u in enumerate(path):
        pts = list(fl[u].points) if u in fl else []
        if len(pts) >= 2:
            j = (i + 1) % len(path)
            if j != i and path[j] in fl and fl[path[j]].points:
                nxt = fl[path[j]].points
                ends = (nxt[0], nxt[-1])
                if min(math.dist(pts[0], e) for e in ends) < min(math.dist(pts[-1], e) for e in ends):
                    pts.reverse()
            elif out and out[-1]:
                if math.dist(pts[-1], out[-1][-1]) < math.dist(pts[0], out[-1][-1]):
                    pts.reverse()
        out.append(pts)
    return out


def path_points(fl: dict[int, Flow], path: list[int]) -> list[tuple]:
    """The whole path as one polyline along its flows, back to its start (a loop)."""
    out = []
    for pts in oriented(fl, path):
        out += pts
    if len(path) > 1 and out:
        out.append(out[0])
    return out
