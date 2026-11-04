"""Shared fixtures. Synthetic data needs nothing; ``game``/``blender`` tests skip when unavailable."""

from __future__ import annotations

import numpy as np
import pytest

from control_mod.binfbx import (
    P_SAMPLER, P_TEXTURE, P_UINT, P_VEC4, BinFbx, Bone, Material, MaterialParam, NLString, VertexAttr,
)  # fmt: skip
from control_mod.config import find_blender
from control_mod.gamedata import find_game_dir
from control_mod.geometry import Submesh, encode

GAME_DIR = find_game_dir()
BLENDER = find_blender()

needs_game = pytest.mark.skipif(GAME_DIR is None, reason="Control game files not found (set CONTROL_MOD_GAME_DIR)")
needs_blender = pytest.mark.skipif(BLENDER is None, reason="Blender not found (set CONTROL_MOD_BLENDER)")

LAYOUT_SKIN8 = [
    VertexAttr(0, 2, 0), VertexAttr(1, 4, 3), VertexAttr(1, 8, 1), VertexAttr(1, 7, 2),
    VertexAttr(0, 13, 5), VertexAttr(0, 13, 5), VertexAttr(0, 5, 6), VertexAttr(0, 5, 6),
]  # fmt: skip
LAYOUT_SKIN4 = [VertexAttr(0, 2, 0), VertexAttr(1, 4, 3), VertexAttr(1, 8, 1), VertexAttr(1, 7, 2), VertexAttr(0, 13, 5), VertexAttr(0, 5, 6)]
LAYOUT_STATIC = [VertexAttr(0, 2, 0), VertexAttr(1, 4, 3), VertexAttr(1, 8, 1), VertexAttr(1, 7, 2)]


def box(center=(0, 1, 0), size=0.2, nbones=3, seed=0) -> dict:
    """A UV-mapped box with split (per-face) vertices, like real game meshes."""
    rng = np.random.default_rng(seed)
    c = np.array(center, np.float32)
    pos, nrm, uv, faces = [], [], [], []
    for axis in range(3):
        for sign in (-1, 1):
            n = np.zeros(3)
            n[axis] = sign
            u = np.roll(n, 1)
            v = np.cross(n, u)
            base = len(pos)
            for du, dv in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
                pos.append(c + (n + du * u + dv * v) * size / 2)
                nrm.append(n)
                uv.append(((du + 1) / 2, (dv + 1) / 2))
            # counter-clockwise when seen from outside in a right-handed frame
            faces += [(base, base + 1, base + 2), (base, base + 2, base + 3)]
    # skin weights depend on position only, so split seam vertices share them (as in game data)
    pos_arr = np.array(pos, np.float32)
    _, corner_id = np.unique(np.round(pos_arr, 5), axis=0, return_inverse=True)
    corner_id = corner_id.ravel()
    k = corner_id.max() + 1
    bi = rng.integers(0, nbones, size=(k, 8))[corner_id]
    bw = (rng.random((k, 8)).astype(np.float32) ** 3)[corner_id]
    bw /= bw.sum(1, keepdims=True)
    return dict(
        positions=np.array(pos, np.float32), normals=np.array(nrm, np.float32), uv=np.array(uv, np.float32),
        faces=np.array(faces, np.int64), bone_indices=bi, bone_weights=bw,
    )  # fmt: skip


def submesh(lod=0, material=0, layout=LAYOUT_SKIN8, seed=0, center=(0, 1, 0)) -> Submesh:
    b = box(center=center, seed=seed)
    skinned = any(a.usage == 13 for a in layout)
    return Submesh(
        lod=lod, material=material, positions=b["positions"], normals=b["normals"], tangents=None,
        uvs=[b["uv"]], colors=[], bone_indices=b["bone_indices"] if skinned else None,
        bone_weights=b["bone_weights"] if skinned else None, faces=b["faces"], layout=list(layout),
    )  # fmt: skip


def material(name="mat_a", shader="standardmaterial", texture="runtimedata\\data_pc\\textures\\test\\a_d.tex") -> Material:
    return Material(
        7, 0x1234_5678_9ABC_40EA, NLString(name), NLString(shader), NLString(f"sourcedata\\materials\\{name}.material"),
        (0, 0, 2, 1, 0, 4),
        [
            MaterialParam(NLString("g_bAlphaTest"), P_UINT, (0,)),
            MaterialParam(NLString("g_vColorMultiplier"), P_VEC4, (1.0, 0.5, 0.25, 1.0)),
            MaterialParam(NLString("g_sColorMap"), P_TEXTURE, NLString(texture)),
            MaterialParam(NLString("g_sColorMap_sampler"), P_SAMPLER, None),
        ],
    )  # fmt: skip


def synthetic_fbx(lods=2, area_table=True) -> BinFbx:
    """A small skinned two-material, multi-LOD model built entirely in code."""
    bones = [
        Bone(NLString("root_JNT"), (1, 0, 0, 0, 1, 0, 0, 0, 1, 0, -1.0, 0), (0, 1, 0, 0.5), -1),
        Bone(NLString("spine_JNT"), (1, 0, 0, 0, 1, 0, 0, 0, 1, 0, -1.2, 0), (0, 1.2, 0, 0.3), 0),
        Bone(NLString("head_JNT"), (0, 1, 0, -1, 0, 0, 0, 0, 1, 1.5, 0, 0), (0, 1.5, 0, 0.2), 1),
    ]
    subs = []
    for lod in range(lods):
        layout = LAYOUT_SKIN8 if lod == 0 else LAYOUT_SKIN4
        subs.append(submesh(lod, 0, layout, seed=lod, center=(0, 1, 0)))
        subs.append(submesh(lod, 1, layout, seed=lod + 10, center=(0, 1.5, 0)))
    enc = encode(subs, bone_total=len(bones))
    trailer = b""
    fbx = BinFbx(
        version=46, index_size=enc.index_size, secondary=enc.secondary, primary=enc.primary, indices=enc.indices,
        bones=bones, unk_a=0, unk_b=0, unk_c=1.0, lod_thresholds=tuple(0.01 * (i + 1) for i in range(lods - 1)),
        unk_d=-1.0, sphere=(0, 1.25, 0, 0.5), bbox=(-0.1, 0.9, -0.1, 0.1, 1.6, 0.1), lod_count=lods,
        materials=[material("mat_a"), material("mat_b", texture="runtimedata\\data_pc\\textures\\test\\b_d.tex")],
        mesh_material=[s.material for s in subs], variants=[(NLString("alt"), [1 - s.material for s in subs])],
        shadow_material=[0] * lods, meshes=enc.meshes, shadow_meshes=[], trailer=trailer,
    )  # fmt: skip
    # shadow mesh per LOD covering both meshes, like the game's grouping
    import copy

    for lod in range(lods):
        first = next(m for m in enc.meshes if m.lod == lod)
        sh = copy.deepcopy(first)
        sh.face_count = sum(m.face_count for m in enc.meshes if m.lod == lod)
        sh.unk2 = 1.0
        fbx.shadow_meshes.append(sh)
    if area_table:
        from control_mod.model import build_area_table

        fbx.trailer = build_area_table(0, subs)
    return fbx


@pytest.fixture
def fbx() -> BinFbx:
    return synthetic_fbx()


@pytest.fixture(scope="session")
def game():
    if GAME_DIR is None:
        pytest.skip("Control game files not found")
    from control_mod.gamedata import GameData

    return GameData(GAME_DIR)
