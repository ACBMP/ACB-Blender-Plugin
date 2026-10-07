"""The map's static collision in world space, for questions a single entity's shape can't answer (what's below a
ledge, is the space in front of it free). Needs numpy (Blender bundles it); only the climb-edge generator uses it.

Triangles are bucketed in a 2D grid over x/y, so a vertical ray or a proximity query only looks at nearby columns.
Each triangle remembers its element key, so an entity's own geometry can be left out of a query.
"""
from __future__ import annotations

import numpy as np

from .doc import MapDocument, u32
from .geom import matrix_rows, mesh_shape_geometry

CELL = 2.0   # grid cell size (m)


def to_world(matrix: bytes) -> np.ndarray:
    """GlobalMatrix -> 4x4 column-vector matrix (world = M @ [local, 1])."""
    return np.array(matrix_rows(matrix), float).T


class WorldCollision:
    def __init__(self, doc: MapDocument, elements=None):
        from .kinds import classify
        from .ops import inert_components
        tris, owners = [], []
        self.keys: list[tuple[int, int]] = []
        for e in elements if elements is not None else classify(doc):
            M = None
            for _ci, ic in inert_components(e.obj):
                sid = u32(ic.fields["RigidBody"].fields["Shape"].id)
                if sid not in doc.info or doc.type_of(sid) != "MeshShape":
                    continue
                v, t, _m = mesh_shape_geometry(doc.obj(sid))
                if not t:
                    continue
                if M is None:
                    M = to_world(e.obj.fields["GlobalMatrix"])
                    self.keys.append(e.key)
                V = np.asarray(v, float) @ M[:3, :3].T + M[:3, 3]
                T = V[np.asarray(t)]
                tris.append(T)
                owners.append(np.full(len(T), len(self.keys) - 1))
        self.tris = np.concatenate(tris) if tris else np.zeros((0, 3, 3))
        self.owner = np.concatenate(owners) if owners else np.zeros(0, int)
        self.key_index = {k: i for i, k in enumerate(self.keys)}
        lo = self.tris.min(1)[:, :2] if len(self.tris) else np.zeros((0, 2))
        hi = self.tris.max(1)[:, :2] if len(self.tris) else np.zeros((0, 2))
        self.zlo = self.tris[:, :, 2].min(1) if len(self.tris) else np.zeros(0)
        self.zhi = self.tris[:, :, 2].max(1) if len(self.tris) else np.zeros(0)
        self.grid: dict[tuple[int, int], list[int]] = {}
        clo = np.floor(lo / CELL).astype(int)
        chi = np.floor(hi / CELL).astype(int)
        for i in range(len(self.tris)):
            for cx in range(clo[i, 0], chi[i, 0] + 1):
                for cy in range(clo[i, 1], chi[i, 1] + 1):
                    self.grid.setdefault((cx, cy), []).append(i)
        self.grid_arr = {k: np.asarray(v) for k, v in self.grid.items()}

    def _near(self, p, radius=0.0) -> np.ndarray:
        cx0, cy0 = (int(np.floor((p[0] - radius) / CELL)), int(np.floor((p[1] - radius) / CELL)))
        cx1, cy1 = (int(np.floor((p[0] + radius) / CELL)), int(np.floor((p[1] + radius) / CELL)))
        parts = [self.grid_arr[(cx, cy)] for cx in range(cx0, cx1 + 1) for cy in range(cy0, cy1 + 1)
                 if (cx, cy) in self.grid_arr]
        if not parts:
            return np.zeros(0, int)
        return np.unique(np.concatenate(parts)) if len(parts) > 1 else parts[0]

    def ground_below(self, p, max_dist: float = 50.0) -> float | None:
        """Distance straight down from p to the first triangle (any owner), or None."""
        idx = self._near(p)
        if not len(idx):
            return None
        idx = idx[(self.zlo[idx] <= p[2]) & (self.zhi[idx] >= p[2] - max_dist)]
        if not len(idx):
            return None
        T = self.tris[idx]
        a, b, c = T[:, 0], T[:, 1], T[:, 2]
        # 2D barycentric of (p.x, p.y) in each triangle's xy projection
        v0, v1 = b[:, :2] - a[:, :2], c[:, :2] - a[:, :2]
        v2 = np.asarray(p[:2]) - a[:, :2]
        den = v0[:, 0] * v1[:, 1] - v1[:, 0] * v0[:, 1]
        ok = np.abs(den) > 1e-12
        den = np.where(ok, den, 1.0)
        u = (v2[:, 0] * v1[:, 1] - v1[:, 0] * v2[:, 1]) / den
        w = (v0[:, 0] * v2[:, 1] - v2[:, 0] * v0[:, 1]) / den
        inside = ok & (u >= -1e-6) & (w >= -1e-6) & (u + w <= 1 + 1e-6)
        z = a[:, 2] + u * (b[:, 2] - a[:, 2]) + w * (c[:, 2] - a[:, 2])
        d = p[2] - z
        d = d[inside & (d >= -1e-4) & (d <= max_dist)]
        return float(d.min()) if len(d) else None

    def distance(self, p, radius: float, exclude_key=None) -> float:
        """Distance from p to the nearest triangle within radius (other owners than exclude_key), or inf."""
        idx = self._near(p, radius)
        if exclude_key is not None and exclude_key in self.key_index:
            idx = idx[self.owner[idx] != self.key_index[exclude_key]]
        idx = idx[(self.zlo[idx] <= p[2] + radius) & (self.zhi[idx] >= p[2] - radius)]
        if not len(idx):
            return float("inf")
        return float(_point_tri_dist(np.asarray(p, float), self.tris[idx]).min())


def _point_tri_dist(p: np.ndarray, T: np.ndarray) -> np.ndarray:
    """Distance from p to each triangle (n, 3, 3)."""
    a, b, c = T[:, 0], T[:, 1], T[:, 2]
    ab, ac, ap = b - a, c - a, p - a
    d1, d2 = (ab * ap).sum(1), (ac * ap).sum(1)
    bp = p - b
    d3, d4 = (ab * bp).sum(1), (ac * bp).sum(1)
    cp = p - c
    d5, d6 = (ab * cp).sum(1), (ac * cp).sum(1)
    va, vb, vc = d3 * d6 - d5 * d4, d5 * d2 - d1 * d6, d1 * d4 - d3 * d2
    den = va + vb + vc
    den = np.where(np.abs(den) > 1e-18, den, 1.0)
    v, w = vb / den, vc / den
    face = a + ab * v[:, None] + ac * w[:, None]
    inside = (va >= 0) & (vb >= 0) & (vc >= 0)
    best = np.where(inside, np.linalg.norm(face - p, axis=1), np.inf)
    for p0, p1 in ((a, b), (b, c), (c, a)):
        e = p1 - p0
        t = np.clip(((p - p0) * e).sum(1) / np.maximum((e * e).sum(1), 1e-18), 0, 1)
        best = np.minimum(best, np.linalg.norm(p0 + e * t[:, None] - p, axis=1))
    return best
