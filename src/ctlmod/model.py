"""High level rebuild of a ``.binfbx`` from edited submeshes.

Everything that is not geometry (skeleton, LOD thresholds, unknown header values) is
taken from the original file (the *template*). Derived data is regenerated:

* vertex/index buffers and per-mesh records (``geometry.encode``)
* shadow meshes: the original grouping of meshes is kept where possible
* material variant tables
* bounding volumes
* the trailing surface-area table used by the engine to spawn effects on the surface
"""

from __future__ import annotations

import copy
import struct

import numpy as np

from . import geometry
from .binfbx import BinFbx, Material, Mesh
from .geometry import Submesh


def shadow_groups(fbx: BinFbx) -> dict[int, int]:
    """Map mesh index -> index of the shadow mesh whose index range contains it."""
    out: dict[int, int] = {}
    for si, sh in enumerate(fbx.shadow_meshes):
        lo, hi = sh.index_offset, sh.index_offset + 3 * sh.face_count
        for mi, m in enumerate(fbx.meshes):
            if m.lod == sh.lod and m.index_offset >= lo and m.index_offset + 3 * m.face_count <= hi and m.face_count:
                out[mi] = si
    return out


def parse_area_table(trailer: bytes) -> tuple[int, float, np.ndarray] | None:
    if len(trailer) < 12:
        return None
    unk, total, n = struct.unpack_from("<IfI", trailer, 0)
    if len(trailer) != 12 + 4 * n:
        return None
    return unk, total, np.frombuffer(trailer, "<f4", count=n, offset=12)


def build_area_table(unk: int, subs: list[Submesh]) -> bytes:
    areas = []
    for s in subs:
        if s.lod != 0 or not len(s.faces):
            continue
        p = s.positions.astype(np.float64)
        f = s.faces
        areas.append(0.5 * np.linalg.norm(np.cross(p[f[:, 1]] - p[f[:, 0]], p[f[:, 2]] - p[f[:, 0]]), axis=1))
    a = np.concatenate(areas) if areas else np.zeros(0)
    a = a.astype(np.float32)
    c = np.cumsum(a, dtype=np.float32)
    total = float(c[-1]) if len(c) else 0.0
    cdf = (c / np.float32(total)) if total > 0 else np.zeros(len(a), np.float32)
    return struct.pack("<IfI", unk, total, len(a)) + cdf.astype("<f4").tobytes()


def rebuild(
    template: BinFbx,
    subs: list[Submesh],
    materials: list[Material] | None = None,
    new_meshes_cast_shadows: bool = True,
) -> BinFbx:
    """Return a new BinFbx with ``subs`` as geometry and ``materials`` as materials."""
    if not subs:
        raise ValueError("model has no meshes")
    materials = template.materials if materials is None else materials
    for s in subs:
        if not 0 <= s.material < len(materials):
            raise ValueError(f"submesh refers to material {s.material} but there are {len(materials)}")
    order = sorted(range(len(subs)), key=lambda i: subs[i].lod)  # stable: keeps order within a LOD
    subs = [subs[i] for i in order]
    enc = geometry.encode(subs, template.meshes, bone_total=len(template.bones))

    out = copy.copy(template)
    out.primary, out.secondary, out.indices, out.index_size = enc.primary, enc.secondary, enc.indices, enc.index_size
    out.meshes = enc.meshes
    out.materials = list(materials)
    out.mesh_material = [s.material for s in subs]
    out.variants = []
    for vname, table in template.variants:
        new_table = []
        for s in subs:
            if 0 <= s.source_index < len(table) and template.mesh_material[s.source_index] == s.material:
                new_table.append(table[s.source_index])
            else:
                new_table.append(s.material)
        out.variants.append((vname, new_table))
    out.lod_count = max(template.lod_count, max(s.lod for s in subs) + 1)

    # shadow meshes ------------------------------------------------------------
    groups = shadow_groups(template)
    shadow_meshes: list[Mesh] = []
    shadow_material: list[int] = []
    prev_key = None
    for s, m in zip(subs, enc.meshes):
        if 0 <= s.source_index < len(template.meshes):
            g = groups.get(s.source_index)
            if g is None:
                prev_key = None
                continue  # original mesh did not cast shadows
            key = ("orig", m.lod, g)
            mat = template.shadow_material[g] if g < len(template.shadow_material) else s.material
        else:
            if not new_meshes_cast_shadows:
                prev_key = None
                continue
            key = ("new", id(s))
            mat = s.material
        if key == prev_key and shadow_meshes[-1].index_offset + 3 * shadow_meshes[-1].face_count == m.index_offset:
            # the game keeps the first member's bounds/metadata for merged shadow meshes
            shadow_meshes[-1].face_count += m.face_count
        else:
            sh = copy.deepcopy(m)
            sh.unk2 = 1.0
            shadow_meshes.append(sh)
            shadow_material.append(mat)
        prev_key = key
    out.shadow_meshes = shadow_meshes
    out.shadow_material = shadow_material

    # bounds ---------------------------------------------------------------------
    lod0 = [s for s in subs if s.lod == 0] or subs
    pts = np.concatenate([s.positions for s in lod0]).astype(np.float64)
    lo, hi = pts.min(0), pts.max(0)
    c = (lo + hi) / 2
    out.sphere = (*map(float, c), float(np.linalg.norm(pts - c, axis=1).max()))
    out.bbox = (*map(float, lo), *map(float, hi))

    # trailer --------------------------------------------------------------------
    parsed = parse_area_table(template.trailer)
    if parsed is not None:
        out.trailer = build_area_table(parsed[0], subs)
    return out
