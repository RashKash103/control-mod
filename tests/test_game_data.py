"""Checks against the real game files (skipped when Control is not installed).

Set ``CTLMOD_FULL_SCAN=1`` to round-trip every model in the game instead of characters only.
"""

import os

import numpy as np
import pytest

from conftest import needs_game
from ctlmod import geometry, model, texture
from ctlmod.binfbx import BinFbx
from ctlmod.package import game_texture_to_archive_path

pytestmark = [needs_game, pytest.mark.game]

JESSE = "data/objects/characters/intermediate/jesse_suit_publish_physx.binfbx"
SAMPLES = [
    JESSE,
    "data/objects/characters/intermediate/jesse_master_publish_physx.binfbx",  # has a material variant
    "data/objects/characters/intermediate/shared_andro_ragdoll.binfbx",  # rigid skinning
    "data/objects/props/painting_plaque_jesse.binfbx",  # static
]


def test_archives_load(game):
    assert len(game.archives) >= 4
    assert JESSE in game
    assert len(game.models("jesse_*_publish_physx")) >= 12


def test_character_models_roundtrip_bytes(game):
    pattern = "*" if os.environ.get("CTLMOD_FULL_SCAN") else "*/characters/*"
    hits = game.models(pattern)
    assert hits
    bad = []
    for h in hits:
        data = h.archive.read(h.entry)
        try:
            f = BinFbx.parse(data)
            # sanity beyond the byte check: every mesh's buffers are inside the file
            for m in f.meshes + f.shadow_meshes:
                assert m.lod < f.lod_count
                n = m.vertex_count
                assert m.primary_offset + n * geometry.stream_stride(m.attrs, 0) <= len(f.primary)
                assert m.secondary_offset + n * geometry.stream_stride(m.attrs, 1) <= len(f.secondary)
            assert len(f.mesh_material) == len(f.meshes)
            if f.to_bytes() != data:
                bad.append(h.entry.path)
        except Exception as e:  # pragma: no cover - reported below
            bad.append(f"{h.entry.path}: {e}")
    assert not bad, bad[:10]


@pytest.mark.parametrize("path", SAMPLES)
def test_decode_rebuild_fidelity(game, path):
    orig = BinFbx.parse(game.read(path))
    subs = geometry.decode(orig)
    rebuilt = BinFbx.parse(model.rebuild(orig, subs).to_bytes())
    a, b = geometry.decode(orig), geometry.decode(rebuilt)
    assert len(a) == len(b)
    for x, y in zip(a, b):
        assert np.array_equal(x.positions, y.positions)
        assert np.array_equal(x.faces, y.faces)
        assert np.allclose(x.normals, y.normals, atol=1e-4)
        for u, v in zip(x.uvs, y.uvs):
            assert np.array_equal(u, v)
        assert np.array_equal(x.tangents, y.tangents)
        assert np.array_equal(x.islands, y.islands)
        if x.bone_weights is not None:
            assert np.array_equal(x.bone_weights, y.bone_weights)
    assert rebuilt.mesh_material == orig.mesh_material
    assert [v[1] for v in rebuilt.variants] == [v[1] for v in orig.variants]
    assert rebuilt.shadow_material == orig.shadow_material
    assert [(s.lod, s.face_count, s.index_offset) for s in rebuilt.shadow_meshes] == [
        (s.lod, s.face_count, s.index_offset) for s in orig.shadow_meshes
    ]
    assert np.allclose(rebuilt.sphere, orig.sphere, atol=1e-5)
    if model.parse_area_table(orig.trailer):
        _, t0, c0 = model.parse_area_table(orig.trailer)
        _, t1, c1 = model.parse_area_table(rebuilt.trailer)
        assert abs(t0 - t1) < 1e-4 * max(t0, 1) and np.allclose(c0, c1, atol=1e-5)


def test_regenerated_tangents_match_game(game):
    f = BinFbx.parse(game.read(JESSE))
    sub = geometry.decode(f)[3]  # jacket
    t = geometry.compute_tangents(sub.positions, sub.normals, sub.uvs[0], sub.faces)
    agree = (t[:, :3] * sub.tangents[:, :3]).sum(1)
    assert np.median(agree) > 0.97
    assert np.mean(t[:, 3] == sub.tangents[:, 3]) > 0.97


def test_bone_positions_are_plausible(game):
    from ctlmod.package import _blender_bones

    f = BinFbx.parse(game.read(JESSE))
    bones = {b["name"]: b for b in _blender_bones(f)}
    head = np.array(bones["C_head_JNT"]["head"])
    assert 1.4 < head[2] < 1.7  # head height in metres, Blender Z up
    assert bones["L_clavicle_JNT"]["head"][0] > 0 > bones["R_clavicle_JNT"]["head"][0]  # left is +X


def test_character_textures_decode_and_reencode(game):
    f = BinFbx.parse(game.read(JESSE))
    seen = set()
    for m in f.materials:
        for tex in m.textures().values():
            p = game_texture_to_archive_path(tex)
            if p in seen or p not in game:
                continue
            seen.add(p)
            data = game.read(p)
            info = texture.read_info(data)
            if not info.editable:
                continue
            img = texture.decode(data)
            assert img.shape[:2] == (info.height, info.width)
            if info.width <= 512:  # keep the test quick
                again = texture.encode(img, data)
                assert texture.read_info(again).header == info.header
                assert len(again) == len(data)
    assert len(seen) > 10
