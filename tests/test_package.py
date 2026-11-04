import json

import pytest

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
    # the game ignores new materials: strict builds refuse with an explanation ...
    with pytest.raises(package.BuildError, match="new copy of 'mat_a'"):
        package.build_from_blender(tmp_path / "pkg", log=lambda s: None)
    # ... experimental builds still produce them (for future metadata support)
    res = package.build_from_blender(tmp_path / "pkg", log=lambda s: None, strict=False)
    from ctlmod.binfbx import BinFbx

    out = BinFbx.parse(res.binfbx)
    assert out.materials[-1].name.text == "mat_a_copy"
    assert out.materials[-1].hash != out.materials[0].hash
    assert len(out.meshes) == 5
    assert out.mesh_material.count(2) == 1
    # the new LOD0 mesh follows the original LOD0 meshes; the list stays grouped by LOD
    assert out.mesh_material.index(2) == sum(1 for m in fbx.meshes if m.lod == 0)
    assert [m.lod for m in out.meshes] == sorted(m.lod for m in out.meshes)


# ------------------------------------------------------------------ strict texture rules


@pytest.fixture
def tex_pkg(tmp_path, fbx):
    """A package whose textures exist (16x16 BC1), plus an untouched Blender dump."""
    from test_texture import make_dds

    tex = make_dds("BC1", 16, 16, 5)
    meta = package.export_package(fbx.to_bytes(), "data/objects/test/thing.binfbx", tmp_path / "pkg", lambda p: tex, log=lambda s: None)
    geo = np.load(tmp_path / "pkg" / "geometry.npz")
    pkg = tmp_path / "pkg"

    def dump(textures_by_material=None):
        mats = []
        for i, m in enumerate(meta["materials"]):
            # like Blender: every image node lists the PNG it shows, then apply overrides
            tex_nodes = {param: str(pkg / meta["textures"][ap]["png"]) for param, ap in m["textures"].items()
                         if meta["textures"][ap].get("png")}
            tex_nodes.update((textures_by_material or {}).get(m["name"], {}))
            mats.append({"name": m["name"], "ctl_material": m["name"], "ctl_index": i, "original": True, "textures": tex_nodes})
        if (pkg / "blender_export").exists():
            import shutil

            shutil.rmtree(pkg / "blender_export")
        _fake_dump(pkg, meta, geo, mats)

    return pkg, meta, tex, dump


def _png(path, size, color):
    from PIL import Image

    Image.new("RGBA", size, color).save(path)


def test_in_place_texture_edit_is_allowed(tex_pkg):
    pkg, meta, tex, dump = tex_pkg
    _png(pkg / "textures" / "a_d.png", (16, 16), (255, 0, 0, 255))
    dump()
    res = package.build_from_blender(pkg, read_texture=lambda p: tex, log=lambda s: None)
    assert list(res.textures) == ["data_pc/textures/test/a_d.tex"]
    assert len(res.textures["data_pc/textures/test/a_d.tex"]) == len(tex)  # same size as the original


def test_resized_texture_is_refused(tex_pkg):
    pkg, meta, tex, dump = tex_pkg
    _png(pkg / "textures" / "a_d.png", (32, 32), (255, 0, 0, 255))
    dump()
    with pytest.raises(package.BuildError, match="must stay 16x16"):
        package.build_from_blender(pkg, read_texture=lambda p: tex, log=lambda s: None)


def test_texture_swap_and_new_image_are_refused(tex_pkg, tmp_path):
    pkg, meta, tex, dump = tex_pkg
    _png(tmp_path / "brand_new.png", (16, 16), (0, 255, 0, 255))
    dump({
        "mat_a": {"g_sColorMap": str(pkg / "textures" / "b_d.png")},  # another existing texture
        "mat_b": {"g_sColorMap": str(tmp_path / "brand_new.png")},  # a new file
    })
    with pytest.raises(package.BuildError) as e:
        package.build_from_blender(pkg, read_texture=lambda p: tex, log=lambda s: None)
    msg = str(e.value)
    assert "2 change(s)" in msg and "switched to b_d.tex" in msg and "new image brand_new.png" in msg
    assert "edit the original texture" in msg.lower() or "paint over the original" in msg.lower()


def test_conform_keeps_original_mesh_slots(fbx):
    from conftest import LAYOUT_SKIN8, submesh
    from ctlmod import model
    from ctlmod.geometry import decode

    subs = decode(fbx)
    slots = [(m.lod, mat) for m, mat in zip(fbx.meshes, fbx.mesh_material)]
    # add a new LOD0 part with material 1 and delete the original LOD0 material-0 mesh
    new = submesh(0, 1, LAYOUT_SKIN8, seed=77, center=(0, 3, 0))
    deleted = next(s for s in subs if s.lod == 0 and s.material == 0)
    edited = [s for s in subs if s is not deleted] + [new]
    out, notes = model.conform(fbx, edited)
    assert [(s.lod, s.material) for s in out] == slots  # same slots, same order
    assert any("merged into LOD0 mesh 'mat_b'" in n for n in notes)
    assert any("deleted" in n for n in notes)
    lod0_b = next(s for s in out if s.lod == 0 and s.material == 1)
    assert lod0_b.vertex_count == next(s for s in subs if s.lod == 0 and s.material == 1).vertex_count + new.vertex_count
    placeholder = next(s for s in out if s.lod == 0 and s.material == 0)
    assert placeholder.faces.tolist() == [[0, 0, 0]]
    built = model.rebuild(fbx, out)
    assert model.layout_problems(built) == []
    assert [(m.lod, mat) for m, mat in zip(built.meshes, built.mesh_material)] == slots


def test_conform_refuses_unknown_material_but_keeps_generated_lods(fbx):
    from conftest import LAYOUT_SKIN4, material, submesh
    from ctlmod import model
    from ctlmod.geometry import decode

    mats = list(fbx.materials) + [material("lod_baked")]
    subs = decode(fbx)
    # hand-authored LOD1 containing a part with a material LOD1 never had: refused
    stray = submesh(1, 2, LAYOUT_SKIN4, seed=5)
    with pytest.raises(model.SlotError, match="lod_baked"):
        model.conform(fbx, subs + [stray], mats)
    # LOD1 made only of generated geometry (copy/decimate) with such a material: keep original
    generated = [s for s in subs if s.lod == 0] + [stray]
    out, notes = model.conform(fbx, generated, mats)
    assert any("LOD1 keeps its original geometry" in n for n in notes)
    lod1 = [s for s in out if s.lod == 1]
    orig1 = [s for s in subs if s.lod == 1]
    assert [s.vertex_count for s in lod1] == [s.vertex_count for s in orig1]


def test_fit_to_size_reuses_coarser_lods(fbx):
    from conftest import LAYOUT_SKIN4, submesh, synthetic_fbx
    from ctlmod import model
    from ctlmod.binfbx import BinFbx
    from ctlmod.geometry import decode

    # template: LOD1 is much heavier than LOD2 (as in real models)
    base = synthetic_fbx(lods=3)
    subs = decode(base)
    heavy = submesh(1, 0, LAYOUT_SKIN4, seed=5)
    for i in range(40):
        piece = submesh(1, 0, LAYOUT_SKIN4, seed=6 + i, center=(i * 0.3, 1, 0))
        subs.append(piece)
    del heavy
    template = BinFbx.parse(model.rebuild(base, subs).to_bytes())
    budget = len(template.to_bytes())

    edited = decode(template)
    for i in range(6):  # new LOD0 parts that do not fit on their own
        edited.append(submesh(0, 1, edited[0].layout, seed=100 + i, center=(0, 2 + i * 0.3, 0)))
    assert len(model.rebuild(template, edited).to_bytes()) > budget
    out, notes = model.fit_to_size(template, edited, None, budget)
    assert len(out.to_bytes()) == budget  # exactly the original size (area table capped)
    assert any("LOD1 now uses LOD2" in n for n in notes)
    # vertex/index buffers padded to exactly the original sizes, same index size
    assert (len(out.secondary), len(out.primary), len(out.indices), out.index_size) == (
        len(template.secondary), len(template.primary), len(template.indices), template.index_size)
    _, _, cdf = model.parse_area_table(out.trailer)
    assert len(cdf) == len(model.parse_area_table(template.trailer)[2]) and abs(cdf[-1] - 1) < 1e-6
    # the mesh slots of every LOD are exactly the original ones
    assert [(m.lod, mat) for m, mat in zip(out.meshes, out.mesh_material)] == [
        (m.lod, mat) for m, mat in zip(template.meshes, template.mesh_material)]
    # LOD1 holds LOD2's geometry (merged into its slots) plus degenerate placeholders
    lod1 = sum(m.face_count for m in out.meshes if m.lod == 1)
    lod2 = sum(m.face_count for m in out.meshes if m.lod == 2)
    placeholders = sum(1 for m in out.meshes if m.lod == 1 and m.face_count == 1)
    assert lod1 == lod2 + placeholders

    for i in range(400):  # far too much new geometry: refused with a clear message
        edited.append(submesh(0, 1, edited[0].layout, seed=1000 + i))
    with pytest.raises(model.SizeBudgetError, match="exceeds the original's"):
        model.fit_to_size(template, edited, None, budget)


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
