import json

import numpy as np

from conftest import submesh
from ctlmod import package


def test_axis_conversion_is_inverse_and_handedness_flip():
    v = np.random.default_rng(0).normal(size=(50, 3)).astype(np.float32)
    assert np.allclose(package.to_game(package.to_blender(v)), v, atol=1e-6)
    assert np.isclose(np.linalg.det(package._C), -1)
    # game up (+Y) is Blender up (+Z); a character facing game +Z faces Blender -Y
    assert np.allclose(package.to_blender([[0, 1, 0]]), [[0, 0, 1]])
    assert np.allclose(package.to_blender([[0, 0, 1]]), [[0, -1, 0]])


def test_texture_path_mapping():
    p = "runtimedata\\data_pc\\textures\\characters\\a_d.tex"
    a = package.game_texture_to_archive_path(p)
    assert a == "data_pc/textures/characters/a_d.tex"
    assert package.archive_to_game_texture_path(a) == p


def test_weld_unweld_roundtrip():
    sub = submesh()
    w = package.weld(sub)
    assert len(w["verts"]) < sub.vertex_count  # box corners are welded across faces
    corners = w["faces"].ravel()
    assert len(w["corner_normals"]) == len(corners)
    back = package.unweld(dict(w), uv_count=1, color_count=0)
    assert len(back["positions"]) == sub.vertex_count  # seams split again
    # same set of (position, normal, uv) vertices
    def rows(pos, nrm, uv):
        return {tuple(np.round(np.concatenate([p, n, u]), 4)) for p, n, u in zip(pos, nrm, uv)}

    assert rows(back["positions"], back["normals"], back["uvs"][0]) == rows(sub.positions, sub.normals, sub.uvs[0])
    # triangles keep their orientation (geometric normal agrees with vertex normals)
    p, f, n = back["positions"], back["faces"], back["normals"]
    gn = np.cross(p[f[:, 1]] - p[f[:, 0]], p[f[:, 2]] - p[f[:, 0]])
    assert ((gn * n[f[:, 0]]).sum(1) > 0).all()


def test_unweld_tolerates_normal_noise():
    sub = submesh()
    w = package.weld(sub)
    noise = np.random.default_rng(2).normal(scale=2e-4, size=w["corner_normals"].shape)
    w["corner_normals"] = (w["corner_normals"] + noise).astype(np.float32)
    back = package.unweld(w, 1, 0)
    assert len(back["positions"]) == sub.vertex_count


def _fake_dump(pkg, meta, geo, materials=None, extra_meshes=()):
    """What the Blender export script would write for an untouched scene."""
    out = pkg / "blender_export"
    out.mkdir()
    meshes, arrays = [], {}
    for i, m in enumerate(meta["meshes"]):
        pre = f"m{i}_"
        for k in geo.files:
            if k.startswith(pre):
                arrays[k] = geo[k]
        names = [b["name"] for b in meta["bones"]]
        meshes.append({"name": m["name"], "lod": m["lod"], "material": meta["materials"][m["material"]]["name"],
                       "source_index": m["source_index"], "uv_count": m["uv_count"], "color_count": 0, "groups": names})
    for em in extra_meshes:
        i = len(meshes)
        for k, v in em["arrays"].items():
            arrays[f"m{i}_{k}"] = v
        meshes.append(em["info"])
    mats = materials or [{"name": m["name"], "ctl_material": m["name"], "textures": {}} for m in meta["materials"]]
    (out / "scene.json").write_text(json.dumps({"meshes": meshes, "materials": mats}))
    np.savez(out / "scene.npz", **arrays)


def test_export_then_build_unchanged(tmp_path, fbx):
    meta = package.export_package(fbx.to_bytes(), "data/objects/test/thing.binfbx", tmp_path / "pkg", None, log=lambda s: None)
    assert len(meta["meshes"]) == 4 and len(meta["bones"]) == 3
    geo = np.load(tmp_path / "pkg" / "geometry.npz")
    _fake_dump(tmp_path / "pkg", meta, geo)
    res = package.build_from_blender(tmp_path / "pkg", log=lambda s: None)
    from ctlmod.binfbx import BinFbx
    from ctlmod.geometry import decode

    out = BinFbx.parse(res.binfbx)
    a, b = decode(fbx), decode(out)
    assert len(a) == len(b)
    for x, y in zip(a, b):
        assert x.vertex_count == y.vertex_count
        assert np.allclose(np.sort(x.positions, axis=0), np.sort(y.positions, axis=0), atol=1e-6)
        assert x.material == y.material
    assert [m.name.text for m in out.materials] == [m.name.text for m in fbx.materials]
    assert res.textures == {}


def test_build_new_material_and_mesh(tmp_path, fbx):
    meta = package.export_package(fbx.to_bytes(), "data/objects/test/thing.binfbx", tmp_path / "pkg", None, log=lambda s: None)
    geo = np.load(tmp_path / "pkg" / "geometry.npz")
    extra = {k[3:]: geo[k] for k in geo.files if k.startswith("m0_")}
    mats = [{"name": m["name"], "ctl_material": m["name"], "textures": {}} for m in meta["materials"]]
    mats.append({"name": "mat_a_copy", "ctl_material": "mat_a", "textures": {}})
    info = {"name": "new_piece", "lod": 0, "material": "mat_a_copy", "source_index": -1, "uv_count": 1,
            "color_count": 0, "groups": [b["name"] for b in meta["bones"]]}
    _fake_dump(tmp_path / "pkg", meta, geo, mats, [{"arrays": extra, "info": info}])
    res = package.build_from_blender(tmp_path / "pkg", log=lambda s: None)
    from ctlmod.binfbx import BinFbx

    out = BinFbx.parse(res.binfbx)
    assert out.materials[-1].name.text == "mat_a_copy"
    assert out.materials[-1].hash != out.materials[0].hash
    assert len(out.meshes) == 5
    assert out.mesh_material.count(2) == 1


def test_materials_with_same_name_are_not_duplicated(tmp_path, fbx):
    """Blender renames the second of two same-named materials to '.001'; it must stay the original."""
    fbx.materials[1].name = fbx.materials[0].name
    meta = package.export_package(fbx.to_bytes(), "data/objects/test/thing.binfbx", tmp_path / "pkg", None, log=lambda s: None)
    geo = np.load(tmp_path / "pkg" / "geometry.npz")
    mats = [
        {"name": "mat_a", "ctl_material": "mat_a", "ctl_index": 0, "original": True, "textures": {}},
        {"name": "mat_a.001", "ctl_material": "mat_a", "ctl_index": 1, "original": True, "textures": {}},
    ]
    _fake_dump(tmp_path / "pkg", meta, geo, mats)
    scene = json.loads((tmp_path / "pkg" / "blender_export" / "scene.json").read_text())
    for m, info in zip(scene["meshes"], meta["meshes"]):
        m["material"] = ["mat_a", "mat_a.001"][info["material"]]
    (tmp_path / "pkg" / "blender_export" / "scene.json").write_text(json.dumps(scene))
    from ctlmod.binfbx import BinFbx

    out = BinFbx.parse(package.build_from_blender(tmp_path / "pkg", log=lambda s: None).binfbx)
    assert len(out.materials) == 2
    assert out.mesh_material == fbx.mesh_material
