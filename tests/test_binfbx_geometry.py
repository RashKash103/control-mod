import numpy as np
import pytest

from conftest import LAYOUT_SKIN4, LAYOUT_STATIC, submesh, synthetic_fbx
from ctlmod import geometry, model
from ctlmod.binfbx import BinFbx
from ctlmod.geometry import compute_tangents, decode, encode, islands, quantize_weights


def test_serialize_roundtrip(fbx):
    data = fbx.to_bytes()
    again = BinFbx.parse(data)
    assert again.to_bytes() == data
    assert [m.name.text for m in again.materials] == ["mat_a", "mat_b"]
    assert again.variants[0][0].text == "alt"
    assert len(again.meshes) == 4 and len(again.shadow_meshes) == 2


def test_rejects_wrong_version(fbx):
    data = bytearray(fbx.to_bytes())
    data[0] = 43
    with pytest.raises(ValueError):
        BinFbx.parse(bytes(data))


def test_decode_matches_encoded_input():
    sub = submesh()
    orig_pos, orig_nrm, orig_uv = sub.positions.copy(), sub.normals.copy(), sub.uvs[0].copy()
    orig_bi, orig_bw = sub.bone_indices.copy(), sub.bone_weights.copy()
    enc = encode([sub], bone_total=3)
    f = synthetic_fbx()
    f.primary, f.secondary, f.indices, f.index_size, f.meshes = enc.primary, enc.secondary, enc.indices, enc.index_size, enc.meshes
    f.mesh_material, f.variants = [0], []
    (d,) = decode(f)
    assert np.array_equal(d.positions, orig_pos)
    assert np.allclose(d.normals, orig_nrm, atol=1e-4)
    assert np.allclose(d.uvs[0], orig_uv, atol=1 / 4096)
    assert np.array_equal(d.faces, sub.faces)
    # weights survive 8-bit quantisation (top 8, sum to one)
    qi, qw = quantize_weights(orig_bi, orig_bw, 8)
    assert np.allclose(d.bone_weights, qw / 255.0, atol=1e-6)
    assert np.array_equal(np.where(qw > 0, d.bone_indices, 0), np.where(qw > 0, qi, 0))
    # tangents are unit length and perpendicular to normals
    assert np.allclose(np.linalg.norm(d.tangents[:, :3], axis=1), 1, atol=0.02)
    assert np.abs((d.tangents[:, :3] * d.normals).sum(1)).max() < 0.02


def test_reencode_is_idempotent(fbx):
    subs = decode(fbx)
    enc = encode(subs, fbx.meshes, bone_total=len(fbx.bones))
    assert enc.primary == fbx.primary
    assert enc.secondary == fbx.secondary
    assert enc.indices == fbx.indices


def test_quantize_weights_sum_to_255():
    rng = np.random.default_rng(1)
    w = rng.random((500, 12)).astype(np.float32)
    idx = rng.integers(0, 50, (500, 12))
    qi, qw = quantize_weights(idx, w, 4)
    assert qw.shape == (500, 4)
    assert (qw.astype(int).sum(1) == 255).all()
    assert (np.diff(qw.astype(int), axis=1) <= 1).all()  # (nearly) sorted by influence
    zero_i, zero_w = quantize_weights(np.zeros((1, 2), int), np.zeros((1, 2), np.float32), 4)
    assert zero_w[0].sum() == 255  # unweighted vertices fall back to the first slot


def test_tangent_sign_follows_uv_mirroring():
    sub = submesh(layout=LAYOUT_STATIC)
    t = compute_tangents(sub.positions, sub.normals, sub.uvs[0], sub.faces)
    mirrored = sub.uvs[0].copy()
    mirrored[:, 0] = 1 - mirrored[:, 0]
    tm = compute_tangents(sub.positions, sub.normals, mirrored, sub.faces)
    assert np.all(t[:, 3] == -tm[:, 3])


def test_islands_count_connected_pieces():
    a = submesh(layout=LAYOUT_STATIC)
    faces = np.concatenate([a.faces, a.faces + len(a.positions)])
    pos = np.concatenate([a.positions, a.positions + 5])
    ids = islands(faces, len(pos), pos)
    assert set(ids[: len(a.positions)]) == {0} and set(ids[len(a.positions) :]) == {1}


def test_static_layout_and_large_index_buffer():
    big = submesh(layout=LAYOUT_STATIC)
    reps = 70000 // len(big.positions) + 1
    big.positions = np.tile(big.positions, (reps, 1))
    big.normals = np.tile(big.normals, (reps, 1))
    big.uvs = [np.tile(big.uvs[0], (reps, 1))]
    big.faces = np.concatenate([big.faces + i * 24 for i in range(reps)])
    enc = encode([big])
    assert enc.index_size == 4
    assert enc.meshes[0].unk0 == 1 and enc.meshes[0].bone_count == 0


def test_rebuild_with_edits(fbx):
    subs = decode(fbx)
    # move one mesh, drop another, add a new one with a different material
    subs[0].positions = subs[0].positions + np.float32(0.05)
    del subs[1]
    new = submesh(0, 1, subs[0].layout, seed=99, center=(0.3, 1.0, 0))
    subs.append(new)
    out = model.rebuild(fbx, subs)
    again = BinFbx.parse(out.to_bytes())
    assert len(again.meshes) == len(subs)
    # original meshes keep their relative order and come first; the new mesh is appended
    # meshes stay grouped by LOD (the renderer requires it); within LOD0 the original
    # meshes keep their order and the new one follows them
    assert model.layout_problems(again) == []
    lods = [m.lod for m in again.meshes]
    assert lods == sorted(lods)
    originals = sorted((s for s in subs if s.source_index >= 0), key=lambda s: s.source_index)
    n0 = sum(1 for s in originals if s.lod == 0)
    expected = [s.material for s in originals if s.lod == 0] + [1] + [s.material for s in originals if s.lod > 0]
    assert again.mesh_material == expected
    assert again.meshes[n0].lod == 0
    assert all(len(t) == len(again.meshes) for _, t in again.variants)
    # every mesh is covered by exactly one shadow range of its LOD
    groups = model.shadow_groups(again)
    assert set(groups) == set(range(len(again.meshes)))
    # area table regenerated for LOD0 faces
    unk, total, cdf = model.parse_area_table(again.trailer)
    n_lod0 = sum(m.face_count for m in again.meshes if m.lod == 0)
    assert len(cdf) == n_lod0 and abs(cdf[-1] - 1) < 1e-5 and total > 0
    lo, hi = np.array(again.bbox[:3]), np.array(again.bbox[3:])
    assert (hi > lo).all()


def test_rebuild_rejects_bad_material(fbx):
    subs = decode(fbx)
    subs[0].material = 99
    with pytest.raises(ValueError):
        model.rebuild(fbx, subs)


def test_mixed_layouts_per_lod_convert_to_lod_layout():
    a = submesh(0, 0, LAYOUT_SKIN4)
    b = submesh(0, 0, LAYOUT_SKIN4, seed=3)
    enc = geometry.encode([a, b], bone_total=3)
    assert len({(m.primary_offset, m.vertex_count) for m in enc.meshes}) == 1  # one shared block
    assert enc.meshes[1].index_offset == 3 * len(a.faces)
