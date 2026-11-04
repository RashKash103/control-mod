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


def build_area_table(unk: int, subs: list[Submesh], max_faces: int | None = None) -> bytes:
    """Cumulative surface area of LOD0's triangles (in index-buffer order), used by the
    game to place effects on the surface. ``max_faces`` caps the table length (keeping
    the file size): the extra triangles are then just never chosen."""
    areas = []
    for s in subs:
        if s.lod != 0 or not len(s.faces):
            continue
        p = s.positions.astype(np.float64)
        f = s.faces
        areas.append(0.5 * np.linalg.norm(np.cross(p[f[:, 1]] - p[f[:, 0]], p[f[:, 2]] - p[f[:, 0]]), axis=1))
    a = np.concatenate(areas) if areas else np.zeros(0)
    if max_faces is not None:
        a = a[:max_faces]
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
    match_buffers: bool = False,
) -> BinFbx:
    """Return a new BinFbx with ``subs`` as geometry and ``materials`` as materials.

    ``match_buffers`` keeps the template's index size and pads the vertex and index
    buffers to exactly the template's sizes (raising :class:`SizeBudgetError` if they
    are larger): the game's metadata records those sizes and the renderer allocates GPU
    buffers and ray-tracing data from them.
    """
    if not subs:
        raise ValueError("model has no meshes")
    materials = template.materials if materials is None else materials
    for s in subs:
        if not 0 <= s.material < len(materials):
            raise ValueError(f"submesh refers to material {s.material} but there are {len(materials)}")

    def is_original(s: Submesh) -> bool:
        return 0 <= s.source_index < len(template.meshes)

    # Meshes are grouped by LOD in ascending order, as in every game file: the renderer
    # builds its per-LOD draw tables from that order and crashes otherwise. Within a LOD
    # the original meshes keep their original order and new ones follow.
    order = sorted(range(len(subs)), key=lambda i: (subs[i].lod, not is_original(subs[i]), subs[i].source_index if is_original(subs[i]) else i))
    subs = [subs[i] for i in order]
    try:
        enc = geometry.encode(subs, template.meshes, bone_total=len(template.bones),
                              index_size=template.index_size if match_buffers else None)
    except ValueError as e:
        if match_buffers:
            raise SizeBudgetError(str(e)) from None
        raise
    listing = list(range(len(subs)))
    if match_buffers:
        over = buffer_overrun(template, enc.secondary, enc.primary, enc.indices)
        if over:
            raise SizeBudgetError("; ".join(f"{k} {v:,} bytes over" for k, v in over.items()), over)
        enc.secondary += bytes(len(template.secondary) - len(enc.secondary))
        enc.primary += bytes(len(template.primary) - len(enc.primary))
        enc.indices += bytes(len(template.indices) - len(enc.indices))

    out = copy.copy(template)
    out.primary, out.secondary, out.indices, out.index_size = enc.primary, enc.secondary, enc.indices, enc.index_size
    out.meshes = [enc.meshes[i] for i in listing]
    out.materials = list(materials)
    out.mesh_material = [subs[i].material for i in listing]
    out.variants = []
    for vname, table in template.variants:
        new_table = []
        for s in (subs[i] for i in listing):
            if is_original(s) and s.source_index < len(table) and template.mesh_material[s.source_index] == s.material:
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
    for s, m in zip(subs, enc.meshes):  # index-buffer order, so merged ranges stay contiguous
        if is_original(s):
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
        out.trailer = build_area_table(parsed[0], subs, max_faces=len(parsed[2]) if match_buffers else None)
    problems = layout_problems(out)
    if problems:  # would crash the game's renderer; never write such a file
        raise ValueError("rebuilt model breaks the game's layout rules: " + "; ".join(problems))
    return out


def layout_problems(fbx: BinFbx) -> list[str]:
    """Structural rules every game model follows (the renderer depends on them)."""
    problems = []
    for kind, meshes in (("mesh", fbx.meshes), ("shadow mesh", fbx.shadow_meshes)):
        lods = [m.lod for m in meshes]
        if lods != sorted(lods):
            problems.append(f"{kind} list is not grouped by LOD: {lods}")
        if any(lod >= fbx.lod_count for lod in lods):
            problems.append(f"{kind} refers to a LOD >= lod_count ({fbx.lod_count})")
    missing = sorted(set(range(fbx.lod_count)) - {m.lod for m in fbx.meshes})
    if missing:
        problems.append(f"LOD(s) {missing} have no meshes")
    if len(fbx.mesh_material) != len(fbx.meshes):
        problems.append("material table does not match the mesh list")
    return problems


# ------------------------------------------------------------------ keeping the original structure


class SlotError(ValueError):
    """New geometry that cannot be placed in one of the original mesh slots."""


def _merge(base: Submesh, parts: list[Submesh]) -> Submesh:
    """Concatenate ``parts`` into ``base`` (same LOD and material); returns a new Submesh."""
    all_ = [base, *parts]
    for s in all_:
        if s.tangents is None:
            uv = s.uvs[0] if s.uvs else np.zeros((s.vertex_count, 2), np.float32)
            s.tangents = geometry.compute_tangents(s.positions, s.normals, uv, s.faces)
    out = copy.copy(base)
    offsets = np.cumsum([0] + [s.vertex_count for s in all_[:-1]])
    out.positions = np.concatenate([s.positions for s in all_]).astype(np.float32)
    out.normals = np.concatenate([s.normals for s in all_]).astype(np.float32)
    out.tangents = np.concatenate([s.tangents for s in all_]).astype(np.float32)
    out.faces = np.concatenate([s.faces + o for s, o in zip(all_, offsets)])
    out.uvs = [np.concatenate([(s.uvs[k] if k < len(s.uvs) else s.uvs[0]) for s in all_]) for k in range(len(base.uvs))]
    out.colors = [
        np.concatenate([(s.colors[k] if k < len(s.colors) else np.ones((s.vertex_count, 4), np.float32)) for s in all_])
        for k in range(len(base.colors))
    ]
    if base.bone_indices is not None:
        width = max(s.bone_indices.shape[1] for s in all_)

        def pad(a, fill):
            if a.shape[1] >= width:
                return a
            return np.concatenate([a, np.full((len(a), width - a.shape[1]), fill, a.dtype)], axis=1)

        out.bone_indices = np.concatenate([pad(np.asarray(s.bone_indices), 0) for s in all_])
        out.bone_weights = np.concatenate([pad(np.asarray(s.bone_weights, np.float32), 0) for s in all_])
    if base.islands is not None:
        ids = [base.islands.astype(np.int64)]
        nxt = int(base.islands.max()) + 1 if base.vertex_count else 0
        for s in parts:
            isl = geometry.islands(s.faces, s.vertex_count, s.positions) if s.islands is None else s.islands.astype(np.int64)
            ids.append(isl + nxt)
            nxt += int(isl.max()) + 1 if s.vertex_count else 0
        out.islands = (np.concatenate(ids) % geometry.ISLAND_WRAP).astype(np.int16)
    out.extra = dict(base.extra)
    return out


def _placeholder(o: Submesh) -> Submesh:
    """An invisible stand-in (one degenerate triangle) for a deleted original mesh."""
    p = copy.copy(o)
    p.positions = o.positions[:1].copy()
    p.normals = o.normals[:1].copy()
    p.tangents = None if o.tangents is None else o.tangents[:1].copy()
    p.uvs = [u[:1].copy() for u in o.uvs]
    p.colors = [c[:1].copy() for c in o.colors]
    p.bone_indices = None if o.bone_indices is None else o.bone_indices[:1].copy()
    p.bone_weights = None if o.bone_weights is None else o.bone_weights[:1].copy()
    p.islands = None if o.islands is None else o.islands[:1].copy()
    p.faces = np.zeros((1, 3), np.int64)
    return p


def conform(template: BinFbx, subs: list[Submesh], materials: list[Material] | None = None) -> tuple[list[Submesh], list[str]]:
    """Map edited geometry onto the template's exact mesh slots.

    The game's renderer (its ray-tracing setup, at least) expects each LOD to keep the
    mesh list of the original file, so every original mesh slot is kept:

    * a slot whose mesh came back keeps it;
    * new geometry is merged into the first slot of the same LOD with the same material;
    * a slot whose mesh was deleted gets an invisible placeholder.
    """
    from .geometry import decode

    materials = template.materials if materials is None else materials
    originals = decode(template)
    by_slot: dict[int, Submesh] = {}
    extra: list[Submesh] = []
    for s in subs:
        if 0 <= s.source_index < len(template.meshes) and template.meshes[s.source_index].lod == s.lod and s.source_index not in by_slot:
            by_slot[s.source_index] = s
        else:
            extra.append(s)
    merged_into: dict[int, list[Submesh]] = {}
    keep_original_lods: set[int] = set()
    authored_lods = {template.meshes[i].lod for i in by_slot}
    for s in extra:
        slot = next((i for i, m in enumerate(template.meshes) if m.lod == s.lod and template.mesh_material[i] == s.material), None)
        if slot is None:
            if s.lod not in authored_lods:
                # a LOD made entirely of generated geometry (--lods copy/decimate) whose
                # original uses different materials, e.g. a baked far-distance LOD
                keep_original_lods.add(s.lod)
                continue
            raise SlotError(
                f"new geometry in LOD{s.lod} uses material '{materials[s.material].name.text}', which no "
                f"original LOD{s.lod} mesh uses; the game expects each LOD to keep its original meshes. "
                f"Use one of the materials LOD{s.lod} already has."
            )
        merged_into.setdefault(slot, []).append(s)
    notes: list[str] = [
        f"LOD{lod} keeps its original geometry (it uses materials LOD0 does not have)" for lod in sorted(keep_original_lods)
    ]
    out: list[Submesh] = []
    for i, m in enumerate(template.meshes):
        name = materials[template.mesh_material[i]].name.text
        if m.lod in keep_original_lods:
            out.append(originals[i])
            continue
        base = by_slot.get(i)
        parts = merged_into.get(i, [])
        if base is None and parts:
            base, parts = copy.copy(parts[0]), parts[1:]
        if base is None:
            out.append(_placeholder(originals[i]))
            notes.append(f"LOD{m.lod} mesh '{name}' was deleted (kept as an invisible placeholder)")
            continue
        if parts:
            base = _merge(base, parts)
            notes.append(f"{len(parts)} new part(s) merged into LOD{m.lod} mesh '{name}'")
        base.source_index = i
        base.lod = m.lod
        base.layout = list(m.attrs)
        out.append(base)
    return out, notes


class SizeBudgetError(ValueError):
    """The model does not fit the original's buffer or file sizes."""

    def __init__(self, msg: str, over: dict[str, int] | None = None):
        super().__init__(msg)
        self.over = over or {}


def buffer_overrun(template: BinFbx, secondary: bytes, primary: bytes, indices: bytes) -> dict[str, int]:
    """Bytes by which each rebuilt buffer exceeds the template's (empty if all fit)."""
    over = {}
    for name, new, old in (("secondary vertex buffer", secondary, template.secondary),
                           ("primary vertex buffer", primary, template.primary),
                           ("index buffer", indices, template.indices)):
        if len(new) > len(old):
            over[name] = len(new) - len(old)
    return over


def _as_new(subs: list[Submesh], src_lod: int, dst_lod: int, layout) -> list[Submesh]:
    """Copies of one LOD's meshes, relabelled as new geometry for another LOD."""
    out = []
    for s in subs:
        if s.lod != src_lod:
            continue
        c = copy.copy(s)
        c.lod = dst_lod
        c.source_index = -1
        c.layout = list(layout)
        c.tangents = None if s.tangents is None else s.tangents.copy()
        out.append(c)
    return out


def fit_to_size(
    template: BinFbx,
    subs: list[Submesh],
    materials: list[Material] | None,
    max_size: int,
    keep_slots: bool = True,
) -> tuple[BinFbx, list[str]]:
    """Rebuild a model the game can load in place of the template.

    With ``keep_slots`` (strict builds) the original mesh slots are kept (:func:`conform`)
    and the vertex/index buffers are padded to exactly the original sizes: the game's
    metadata records the file size and the buffer sizes, and the renderer allocates
    from them. If the edited geometry does not fit, LOD1 gets LOD2's geometry (matched
    by material) to make room; if that is not enough, :class:`SizeBudgetError` explains
    by how much.
    """

    def build(s) -> tuple[BinFbx | None, dict[str, int], list[str]]:
        notes_: list[str] = []
        if keep_slots:
            s, notes_ = conform(template, s, materials)
        try:
            o = rebuild(template, s, materials, match_buffers=keep_slots)
        except SizeBudgetError as e:
            return None, e.over or {"geometry": 1}, notes_
        size = len(o.to_bytes())
        return o, ({"file": size - max_size} if size > max_size else {}), notes_

    out, over, notes = build(subs)
    if not over:
        return out, notes
    first_over = over
    lods = sorted({m.lod for m in template.meshes})
    layouts: dict[int, list] = {}
    for m in template.meshes:
        layouts.setdefault(m.lod, m.attrs)
    if len(lods) >= 3:
        trial = [s for s in subs if s.lod != lods[1]] + _as_new(subs, lods[2], lods[1], layouts[lods[1]])
        try:
            out, over, notes = build(trial)
        except SlotError:
            out, over = None, first_over
        if not over:
            notes = [n for n in notes if f"LOD{lods[1]} " not in n]
            desc = ", ".join(f"{k} by {v:,} bytes" for k, v in first_over.items())
            notes.append(f"edits exceeded the original {desc}; LOD{lods[1]} now uses LOD{lods[2]}'s geometry to make room")
            return out, notes
    desc = ", ".join(f"{k} by {v:,} bytes" for k, v in over.items())
    raise SizeBudgetError(
        f"the edited model exceeds the original's {desc}, even with simpler LODs. The game allocates "
        "memory for each model from its original sizes, so it cannot load bigger models yet: remove or "
        "simplify geometry in LOD0 (e.g. a Decimate modifier on new parts), or delete hidden parts.",
        over,
    )


