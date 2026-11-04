"""Vertex/index buffer decoding and encoding for ``BinFbx`` meshes.

Meshes of one LOD share a vertex block; each mesh is a range of the index buffer.
Decoding produces one :class:`Submesh` per mesh with *compacted* vertices (only the
vertices it references), which is what an editor wants. Encoding does the reverse:
it packs any number of submeshes into fresh buffers, one shared block per LOD.

Attribute encodings (verified against geometry over every model in the game):

=========  ======  ==================================================================
usage      format  encoding
=========  ======  ==================================================================
2 pos      0       f32 x3
8 normal   1       snorm16 x4: xyz * 32766, w = mesh island id (see below)
4 tangent  3       unorm8 x4 stored B,G,R,A; xyz*2-1, A=255 -> bitangent sign +1
4 colour   4       unorm8 x4 RGBA (vertex colour)
7 uv       2       s16 x2, value / 4096
13 bones   5       u16 x4 bone indices
15 bone    5       u32 single rigid bone index
5 weights  6       unorm8 x4
5 weights  5       unorm8 x4 (rare variant, same layout)
=========  ======  ==================================================================

Bitangent = cross(normal, tangent) * sign. Positions are model space, Y up, metres.

The normal's w component holds a small integer that numbers the connected pieces
("islands") of a LOD in order, wrapping at :data:`ISLAND_WRAP` (hair cards get
0..79). It is preserved when present and regenerated for new geometry.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .binfbx import (
    U_BONE_INDEX,
    U_BONE_WEIGHT,
    U_NORMAL,
    U_POSITION,
    U_TANGENT,
    U_TEXCOORD,
    BinFbx,
    Mesh,
    VertexAttr,
)

U_RIGID_BONE = 15
F_FLOAT3, F_SNORM16X4, F_UV16, F_TANGENT8, F_COLOR8, F_BONES, F_WEIGHT8 = 0, 1, 2, 3, 4, 5, 6

UV_SCALE = 4096.0
NORMAL_SCALE = 32766.0
ISLAND_WRAP = 80


def attr_size(a: VertexAttr) -> int:
    if a.format == F_BONES:
        return 8 if a.usage == U_BONE_INDEX else 4
    try:
        return {F_FLOAT3: 12, F_SNORM16X4: 8, F_UV16: 4, F_TANGENT8: 4, F_COLOR8: 4, F_WEIGHT8: 4}[a.format]
    except KeyError:
        raise ValueError(f"unknown vertex format {a.format} (usage {a.usage})") from None


def stream_stride(attrs: list[VertexAttr], stream: int) -> int:
    return sum(attr_size(a) for a in attrs if a.stream == stream)


def _semantic(a: VertexAttr) -> str:
    if a.usage == U_POSITION:
        return "position"
    if a.usage == U_NORMAL and a.format == F_TANGENT8:
        return "tangent"
    if a.usage == U_NORMAL and a.format == F_COLOR8:
        return "color"
    if a.usage == U_TANGENT:  # the game's code 8 holds the normal
        return "normal"
    if a.usage == U_TEXCOORD:
        return "uv"
    if a.usage == U_BONE_INDEX:
        return "bones"
    if a.usage == U_RIGID_BONE:
        return "rigid_bone"
    if a.usage == U_BONE_WEIGHT:
        return "weights"
    raise ValueError(f"unknown vertex attribute usage={a.usage} format={a.format}")


@dataclass
class Submesh:
    """Editable geometry of one mesh (one material, one LOD)."""

    lod: int
    material: int
    positions: np.ndarray  # (n,3) f32
    normals: np.ndarray  # (n,3) f32
    tangents: np.ndarray | None  # (n,4) f32, w = bitangent sign
    uvs: list[np.ndarray]  # each (n,2) f32, game convention (v down)
    colors: list[np.ndarray]  # each (n,4) f32 0..1
    bone_indices: np.ndarray | None  # (n,k) int
    bone_weights: np.ndarray | None  # (n,k) f32, rows sum to 1
    faces: np.ndarray  # (f,3) int, local vertex indices
    islands: np.ndarray | None = None  # (n,) int16 normal.w values, None = regenerate
    source_index: int = -1  # index into BinFbx.meshes this came from (-1 = new)
    layout: list[VertexAttr] | None = None  # attribute layout to use when encoding
    extra: dict = field(default_factory=dict)  # opaque per-mesh fields carried through

    @property
    def vertex_count(self) -> int:
        return len(self.positions)


def _decode_block(fbx: BinFbx, mesh: Mesh) -> dict[str, list[np.ndarray]]:
    """Decode the whole shared vertex block a mesh points into."""
    n = mesh.vertex_count
    out: dict[str, list[np.ndarray]] = {}
    for stream, buf, base in ((0, fbx.primary, mesh.primary_offset), (1, fbx.secondary, mesh.secondary_offset)):
        stride = stream_stride(mesh.attrs, stream)
        if stride == 0:
            continue
        raw = np.frombuffer(buf, np.uint8, count=n * stride, offset=base).reshape(n, stride)
        off = 0
        for a in mesh.attrs:
            if a.stream != stream:
                continue
            size = attr_size(a)
            col = np.ascontiguousarray(raw[:, off : off + size])
            off += size
            sem = _semantic(a)
            if sem == "position":
                v = col.view("<f4").reshape(n, 3).astype(np.float32)
            elif sem == "normal":
                q = col.view("<i2").reshape(n, 4)
                out.setdefault("island", []).append(q[:, 3].copy())
                v = q[:, :3].astype(np.float32) / NORMAL_SCALE
            elif sem == "tangent":
                xyz = col[:, [2, 1, 0]].astype(np.float32) / 255.0 * 2.0 - 1.0
                sign = np.where(col[:, 3] > 127, 1.0, -1.0).astype(np.float32)
                v = np.concatenate([xyz, sign[:, None]], axis=1)
            elif sem == "color":
                v = col.astype(np.float32) / 255.0
            elif sem == "uv":
                v = col.view("<i2").reshape(n, 2).astype(np.float32) / UV_SCALE
            elif sem == "bones":
                v = col.view("<u2").reshape(n, 4).astype(np.int32)
            elif sem == "rigid_bone":
                v = col.view("<u4").reshape(n, 1).astype(np.int32)
            elif sem == "weights":
                v = col.astype(np.float32) / 255.0
            out.setdefault(sem, []).append(v)
    return out


def decode(fbx: BinFbx) -> list[Submesh]:
    """Decode every (non-shadow) mesh of the file into compact submeshes."""
    isize = fbx.index_size
    all_idx = np.frombuffer(fbx.indices, "<u2" if isize == 2 else "<u4")
    cache: dict[tuple, dict] = {}
    subs = []
    for mi, mesh in enumerate(fbx.meshes):
        key = (mesh.primary_offset, mesh.secondary_offset, mesh.vertex_count)
        if key not in cache:
            cache[key] = _decode_block(fbx, mesh)
        block = cache[key]
        tri = all_idx[mesh.index_offset : mesh.index_offset + 3 * mesh.face_count].astype(np.int64)
        used, local = np.unique(tri, return_inverse=True)
        faces = local.reshape(-1, 3)

        def take(name: str) -> list[np.ndarray]:
            return [a[used] for a in block.get(name, [])]

        bones = take("bones") + take("rigid_bone")
        weights = take("weights")
        bi = np.concatenate(bones, axis=1) if bones else None
        bw = np.concatenate(weights, axis=1) if weights else None
        if bi is not None and bw is None:  # rigid skinning
            bw = np.ones(bi.shape, np.float32)
        if bw is not None:
            s = bw.sum(axis=1, keepdims=True)
            bw = np.divide(bw, s, out=np.zeros_like(bw), where=s > 0)
        tangents = take("tangent")
        subs.append(
            Submesh(
                lod=mesh.lod,
                material=fbx.mesh_material[mi] if mi < len(fbx.mesh_material) else 0,
                positions=take("position")[0],
                normals=take("normal")[0] if "normal" in block else np.zeros((len(used), 3), np.float32),
                tangents=tangents[0] if tangents else None,
                uvs=take("uv"),
                colors=take("color"),
                bone_indices=bi,
                bone_weights=bw,
                faces=faces,
                islands=take("island")[0] if "island" in block else None,
                source_index=mi,
                layout=list(mesh.attrs),
                extra={"unk1": mesh.unk1, "unk2": mesh.unk2, "unk3": mesh.unk3},
            )
        )
    return subs


# ----------------------------------------------------------------------------- encoding


def compute_tangents(pos: np.ndarray, nrm: np.ndarray, uv: np.ndarray, faces: np.ndarray) -> np.ndarray:
    """Per-vertex tangents (xyz + bitangent sign) from UV layer 0, game convention."""
    f = faces.astype(np.int64)
    v0, v1, v2 = pos[f[:, 0]], pos[f[:, 1]], pos[f[:, 2]]
    w0, w1, w2 = uv[f[:, 0]], uv[f[:, 1]], uv[f[:, 2]]
    e1, e2 = v1 - v0, v2 - v0
    d1, d2 = w1 - w0, w2 - w0
    det = d1[:, 0] * d2[:, 1] - d2[:, 0] * d1[:, 1]
    r = np.where(np.abs(det) > 1e-20, 1.0 / np.where(det == 0, 1, det), 0.0)[:, None]
    ft = (e1 * d2[:, 1:2] - e2 * d1[:, 1:2]) * r
    fb = (e2 * d1[:, 0:1] - e1 * d2[:, 0:1]) * r
    n = len(pos)
    t = np.zeros((n, 3))
    b = np.zeros((n, 3))
    for k in range(3):
        np.add.at(t, f[:, k], ft)
        np.add.at(b, f[:, k], fb)
    nn = nrm.astype(np.float64)
    t -= nn * (t * nn).sum(1, keepdims=True)
    ln = np.linalg.norm(t, axis=1, keepdims=True)
    # fall back to any perpendicular vector where UVs are degenerate
    fallback = np.cross(nn, np.where(np.abs(nn[:, :1]) < 0.9, [[1.0, 0, 0]], [[0, 1.0, 0]]))
    t = np.where(ln > 1e-12, t / np.maximum(ln, 1e-12), fallback / np.maximum(np.linalg.norm(fallback, axis=1, keepdims=True), 1e-12))
    sign = np.where((np.cross(nn, t) * b).sum(1) < 0, -1.0, 1.0)
    return np.concatenate([t, sign[:, None]], axis=1).astype(np.float32)


def islands(faces: np.ndarray, n: int, positions: np.ndarray | None = None) -> np.ndarray:
    """Connected-component number of each vertex, components ordered by first vertex.

    Vertices sharing a position (split by UV/normal seams) count as connected.
    """
    parent = np.arange(n)
    if positions is not None and n:
        _, weld = np.unique(np.round(positions.astype(np.float64), 5), axis=0, return_inverse=True)
        weld = weld.ravel()
        first = np.full(weld.max() + 1, -1)
        for i, k in enumerate(weld.tolist()):
            if first[k] < 0:
                first[k] = i
            else:
                parent[i] = first[k]

    def find(x: int) -> int:
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    for a, b, c in faces.tolist():
        ra, rb, rc = find(a), find(b), find(c)
        parent[rb] = ra
        parent[find(rc)] = ra
    roots = np.array([find(i) for i in range(n)])
    _, first = np.unique(roots, return_index=True)
    order = {r: k for k, r in enumerate(roots[np.sort(first)])}
    return np.array([order[r] for r in roots], np.int64)


def quantize_weights(idx: np.ndarray, w: np.ndarray, slots: int) -> tuple[np.ndarray, np.ndarray]:
    """Keep the ``slots`` largest influences, renormalise and quantise to u8 summing to 255."""
    n = len(w)
    if w.shape[1] < slots:
        pad = slots - w.shape[1]
        w = np.concatenate([w, np.zeros((n, pad), w.dtype)], axis=1)
        idx = np.concatenate([idx, np.zeros((n, pad), idx.dtype)], axis=1)
    order = np.argsort(-w, axis=1, kind="stable")[:, :slots]
    w = np.take_along_axis(w, order, axis=1).astype(np.float64)
    idx = np.take_along_axis(idx, order, axis=1)
    s = w.sum(1, keepdims=True)
    w = np.where(s > 0, w / np.where(s == 0, 1, s), 0.0)
    w[s[:, 0] == 0, 0] = 1.0
    q = np.floor(w * 255.0).astype(np.int64)
    # distribute rounding remainder to the largest fractional parts
    rem = 255 - q.sum(1)
    frac = w * 255.0 - q
    rank = np.argsort(-frac, axis=1, kind="stable")
    for k in range(slots):
        sel = rem > k
        q[sel, rank[sel, k]] += 1
    return idx.astype(np.int64), q.astype(np.uint8)


def _encode_attr(a: VertexAttr, sub: Submesh, counters: dict, weights_q, n: int) -> np.ndarray:
    sem = _semantic(a)
    k = counters.get(sem, 0)
    counters[sem] = k + 1
    if sem == "position":
        return sub.positions.astype("<f4").view(np.uint8).reshape(n, 12)
    if sem == "normal":
        nv = sub.normals.astype(np.float64)
        ln = np.linalg.norm(nv, axis=1, keepdims=True)
        nv = np.where(np.abs(ln - 1.0) > 1e-3, nv / np.maximum(ln, 1e-12), nv)
        q = np.zeros((n, 4), "<i2")
        q[:, :3] = np.clip(np.round(nv * NORMAL_SCALE), -32766, 32766)
        q[:, 3] = sub.islands
        return q.view(np.uint8).reshape(n, 8)
    if sem == "tangent":
        t = sub.tangents
        xyz = np.clip(np.round((t[:, :3] + 1.0) * 0.5 * 255.0), 0, 255).astype(np.uint8)
        out = np.empty((n, 4), np.uint8)
        out[:, 0], out[:, 1], out[:, 2] = xyz[:, 2], xyz[:, 1], xyz[:, 0]
        out[:, 3] = np.where(t[:, 3] >= 0, 255, 0)
        return out
    if sem == "color":
        c = sub.colors[k] if k < len(sub.colors) else np.ones((n, 4), np.float32)
        return np.clip(np.round(c * 255.0), 0, 255).astype(np.uint8)
    if sem == "uv":
        uv = sub.uvs[k] if k < len(sub.uvs) else (sub.uvs[0] if sub.uvs else np.zeros((n, 2), np.float32))
        q = np.clip(np.round(uv * UV_SCALE), -32768, 32767).astype("<i2")
        return q.view(np.uint8).reshape(n, 4)
    qi, qw = weights_q
    if sem == "bones":
        return qi[:, 4 * k : 4 * k + 4].astype("<u2").view(np.uint8).reshape(n, 8)
    if sem == "rigid_bone":
        return qi[:, :1].astype("<u4").view(np.uint8).reshape(n, 4)
    if sem == "weights":
        return qw[:, 4 * k : 4 * k + 4]
    raise AssertionError(sem)


def _layout_slots(layout: list[VertexAttr]) -> int:
    nb = sum(1 for a in layout if a.usage == U_BONE_INDEX)
    if nb:
        return 4 * nb
    return 1 if any(a.usage == U_RIGID_BONE for a in layout) else 0


@dataclass
class EncodedBuffers:
    primary: bytes
    secondary: bytes
    indices: bytes
    index_size: int
    meshes: list[Mesh]


def encode(subs: list[Submesh], template_meshes: list[Mesh] | None = None, bone_total: int = 0) -> EncodedBuffers:
    """Pack submeshes into new vertex/index buffers.

    Submeshes are grouped by LOD (in order of first appearance); every LOD gets one
    shared vertex block, as in the original files. All submeshes of a LOD must use the
    same attribute layout (the first one's layout wins; others are converted).
    """
    template_meshes = template_meshes or []
    lods: dict[int, list[Submesh]] = {}
    for s in subs:
        lods.setdefault(s.lod, []).append(s)

    prim_parts, sec_parts, idx_parts = [], [], []
    prim_off = sec_off = idx_off = 0
    meshes: list[Mesh] = []
    big = any(len(v) > 65535 for v in lods.values()) or max(
        (sum(s.vertex_count for s in v) for v in lods.values()), default=0
    ) > 65535
    index_size = 4 if big else 2

    for lod in sorted(lods):
        group = lods[lod]
        layout = group[0].layout
        if layout is None:
            raise ValueError("submesh has no attribute layout")
        slots = _layout_slots(layout)
        block_n = sum(s.vertex_count for s in group)
        s0, s1 = stream_stride(layout, 0), stream_stride(layout, 1)
        prim = np.zeros((block_n, s0), np.uint8)
        sec = np.zeros((block_n, s1), np.uint8)
        base = 0
        block_meshes = []
        lod_used: set[int] = set()
        island_base = 0
        for s in group:
            n = s.vertex_count
            if s.islands is None:
                ids = islands(s.faces, n, s.positions) if n else np.zeros(0, np.int64)
                s.islands = ((ids + island_base) % ISLAND_WRAP).astype(np.int16)
                island_base += int(ids.max()) + 1 if n else 0
            else:
                island_base = int(s.islands.max()) + 1 if n else island_base
            if s.tangents is None:
                s.tangents = compute_tangents(s.positions, s.normals, s.uvs[0] if s.uvs else np.zeros((n, 2)), s.faces)
            if slots:
                if s.bone_indices is None:
                    raise ValueError(f"LOD {lod} is skinned but a submesh has no bone weights")
                wq = quantize_weights(s.bone_indices, s.bone_weights, slots)
            else:
                wq = (None, None)
            counters: dict[str, int] = {}
            o0 = o1 = 0
            for a in layout:
                col = _encode_attr(a, s, counters, wq, n)
                if a.stream == 0:
                    prim[base : base + n, o0 : o0 + col.shape[1]] = col
                    o0 += col.shape[1]
                else:
                    sec[base : base + n, o1 : o1 + col.shape[1]] = col
                    o1 += col.shape[1]
            tri = (s.faces.astype(np.int64) + base).ravel()
            idx_parts.append(tri.astype("<u2" if index_size == 2 else "<u4").tobytes())
            tmpl = template_meshes[s.source_index] if 0 <= s.source_index < len(template_meshes) else None
            lo, hi = s.positions.min(0), s.positions.max(0)
            center = (lo + hi) / 2
            radius = float(np.linalg.norm(s.positions - center, axis=1).max()) if n else 0.0
            if slots and n:
                used_bones = set(np.unique(wq[0][wq[1] > 0]).tolist())
            else:
                used_bones = set()
            e = np.linalg.norm(s.positions[s.faces[:, [1, 2, 0]]] - s.positions[s.faces], axis=2) if len(s.faces) else np.zeros(1)
            block_meshes.append(
                Mesh(
                    lod=lod,
                    vertex_count=block_n,
                    face_count=len(s.faces),
                    secondary_offset=sec_off,
                    primary_offset=prim_off,
                    index_offset=idx_off,
                    unk0=max(slots, 1),
                    sphere=(*map(float, center), radius),
                    bbox=(*map(float, lo), *map(float, hi)),
                    unk1=s.extra.get("unk1", tmpl.unk1 if tmpl else 1),
                    attrs=list(layout),
                    bone_count=0,
                    unk2=s.extra.get("unk2", tmpl.unk2 if tmpl else 1.0),
                    unk3=s.extra.get("unk3", tmpl.unk3 if tmpl else 0),
                    unk4=float(e.max()),  # longest edge
                )
            )
            lod_used |= used_bones
            idx_off += 3 * len(s.faces)
            base += n
        # Every mesh of a LOD reports how many bones the LOD needs. The game files use
        # the full bone count for LOD0 and (about) the number of distinct bones used for
        # lower LODs; we never report fewer than the template did.
        if slots:
            if lod == 0 and bone_total:
                lod_bones = bone_total
            else:
                tmpl_counts = [template_meshes[s.source_index].bone_count for s in group
                               if 0 <= s.source_index < len(template_meshes) and template_meshes[s.source_index].lod == lod]
                lod_bones = max([len(lod_used), *tmpl_counts])
                if bone_total:
                    lod_bones = min(lod_bones, bone_total)
            for m in block_meshes:
                m.bone_count = lod_bones
        meshes.extend(block_meshes)
        prim_parts.append(prim.tobytes())
        sec_parts.append(sec.tobytes())
        prim_off += prim.nbytes
        sec_off += sec.nbytes
    return EncodedBuffers(b"".join(prim_parts), b"".join(sec_parts), b"".join(idx_parts), index_size, meshes)
