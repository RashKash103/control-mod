"""Full pipeline through a real Blender (skipped without Blender or the game)."""

import subprocess
from pathlib import Path

import numpy as np
import pytest

from conftest import BLENDER, needs_blender, needs_game
from control_mod import package
from control_mod.binfbx import BinFbx
from control_mod.geometry import decode
from control_mod.project import BLENDER_SCRIPT, TOOLS_SCRIPT

pytestmark = [needs_game, needs_blender, pytest.mark.blender]
ROOT = Path(__file__).resolve().parents[1]
MODEL = "data/objects/characters/intermediate/jesse_suit_publish_physx.binfbx"


def blender(*args, blend=None):
    cmd = [str(BLENDER), "--background"] + ([str(blend)] if blend else ["--factory-startup"])
    cmd += ["--python", str(BLENDER_SCRIPT), "--", *map(str, args)]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert p.returncode == 0 and "Traceback" not in p.stdout + p.stderr, (p.stdout + p.stderr)[-3000:]
    return p.stdout


def run_script(blend, script, *args):
    cmd = [str(BLENDER), "--background", str(blend), "--python", str(script), "--", *map(str, args)]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert p.returncode == 0 and "Traceback" not in p.stdout + p.stderr, (p.stdout + p.stderr)[-3000:]


@pytest.fixture(scope="module")
def pkg(tmp_path_factory, game):
    from control_mod.usage import TextureUsage

    d = tmp_path_factory.mktemp("pkg") / "jesse_suit"
    usage = TextureUsage.load(game, ROOT / ".control-mod", progress=None)
    package.export_package(game.read(MODEL), MODEL, d, game.try_read, log=lambda s: None,
                           shared_with=lambda t: usage.others(t, MODEL))
    blender("import", d, d / "model.blend")
    return d


def test_blend_flags_shared_textures(pkg):
    script = (
        "import bpy\n"
        "labels = [n.label for m in bpy.data.materials if m.node_tree for n in m.node_tree.nodes if n.get('ctl_shared_with')]\n"
        "print('SHARED_NODES', len(labels))\n"
        "print('SHARED_IMAGES', sum(1 for i in bpy.data.images if i.name.startswith('SHARED(')))\n"
        "print('README', 'control-mod_READ_ME' in bpy.data.texts and 'SHARED TEXTURES' in bpy.data.texts['control-mod_READ_ME'].as_string())\n"
    )
    cmd = [str(BLENDER), "--background", str(pkg / "model.blend"), "--python-expr", script]
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    counts = {ln.split()[0]: ln.split()[1] for ln in out.splitlines() if ln.startswith(("SHARED_", "README"))}
    assert int(counts["SHARED_NODES"]) > 10 and int(counts["SHARED_IMAGES"]) > 10  # face, hands, defaults ...
    assert counts["README"] == "True"


def test_unedited_roundtrip_through_blender(pkg, game):
    blender("export", pkg, blend=pkg / "model.blend")
    res = package.build_from_blender(pkg, read_texture=game.try_read, log=lambda s: None)
    assert res.textures == {}
    orig, new = decode(BinFbx.parse(game.read(MODEL))), decode(BinFbx.parse(res.binfbx))
    assert len(orig) == len(new)
    for a, b in zip(orig, new):
        assert a.lod == b.lod and a.material == b.material
        assert len(a.faces) == len(b.faces)
        assert abs(a.vertex_count - b.vertex_count) <= max(8, a.vertex_count // 100)
        # same surface: every rebuilt vertex position exists in the original
        sa = {tuple(p) for p in np.round(a.positions, 5)}
        assert all(tuple(p) in sa for p in np.round(b.positions, 5))
        assert np.allclose(b.bone_weights.sum(1), 1, atol=1e-5)


def test_new_mesh_fits_the_size_budget(pkg, game, tmp_path):
    blend = tmp_path / "halo.blend"
    blend.write_bytes((pkg / "model.blend").read_bytes())
    run_script(blend, ROOT / "examples" / "demo_edit.py", "--halo", "jesse_suit_accessories")
    blender("export", pkg, blend=blend)
    res = package.build_from_blender(pkg, read_texture=game.try_read, log=lambda s: None)
    out = BinFbx.parse(res.binfbx)
    assert len(res.binfbx) <= len(game.read(MODEL))  # what the game's metadata allows
    assert any("LOD1 now uses LOD2" in r for r in res.report)  # made room by reusing a coarser LOD
    # buffer sizes (recorded in the game's metadata) are exactly the original ones
    orig_fbx = BinFbx.parse(game.read(MODEL))
    assert (len(out.secondary), len(out.primary), len(out.indices), out.index_size) == (
        len(orig_fbx.secondary), len(orig_fbx.primary), len(orig_fbx.indices), orig_fbx.index_size)
    orig = BinFbx.parse(game.read(MODEL))
    # same mesh slots as the original: the halo is merged into the accessories mesh
    assert [(m.lod, mat) for m, mat in zip(out.meshes, out.mesh_material)] == [
        (m.lod, mat) for m, mat in zip(orig.meshes, orig.mesh_material)]
    assert any("merged into LOD0 mesh 'jesse_suit_accessories'" in r for r in res.report)
    names = [m.name.text for m in out.materials]
    acc = names.index("jesse_suit_accessories")
    lod0_acc = next(s for s in decode(out) if s.lod == 0 and s.material == acc)
    assert (lod0_acc.positions[:, 1] > 1.8).sum() > 100  # the halo's vertices, above the head


def test_new_material_and_texture_are_refused(pkg, game, tmp_path):
    blend = tmp_path / "red.blend"
    blend.write_bytes((pkg / "model.blend").read_bytes())
    run_script(blend, ROOT / "examples" / "demo_edit.py", "--red-jacket")
    blender("export", pkg, blend=blend)
    with pytest.raises(package.BuildError) as e:
        package.build_from_blender(pkg, read_texture=game.try_read, log=lambda s: None)
    assert "jesse_suit_jacket_red" in str(e.value) and "new image" in str(e.value)
    res = package.build_from_blender(pkg, read_texture=game.try_read, log=lambda s: None, strict=False)
    assert "jesse_suit_jacket_red" in [m.name.text for m in BinFbx.parse(res.binfbx).materials]


def test_metadata_mode_allows_new_materials_and_textures(pkg, game, tmp_path):
    """The default build mode (with generated .packmeta) - confirmed in game."""
    blend = pkg / "new_assets.blend"  # next to textures/, so image sizes can be read
    blend.write_bytes((pkg / "model.blend").read_bytes())
    run_script(blend, ROOT / "examples" / "demo_edit.py", "--halo", "jesse_suit_accessories",
               "--halo-copy", "jesse_suit_accessories", "gold", "--new-texture", "jesse_suit_jacket", "blue")
    blender("export", pkg, blend=blend)
    res = package.build_from_blender(pkg, read_texture=game.try_read, log=lambda s: None, size_budget=False)
    out = BinFbx.parse(res.binfbx)
    names = [m.name.text for m in out.materials]
    assert "jesse_suit_accessories_gold" in names
    orig = BinFbx.parse(game.read(MODEL))
    assert len(out.meshes) == len(orig.meshes) + 1  # the halo is an extra mesh with its own material
    assert [m.lod for m in out.meshes] == sorted(m.lod for m in out.meshes)
    new_textures = {p: like for p, like in res.derived.items()}
    assert len(new_textures) == 2 and all(p.startswith("data_pc/textures/control-mod/") for p in new_textures)
    jacket = out.materials[names.index("jesse_suit_jacket")].param("g_sColorMap").value
    assert jacket.nul and jacket.text.endswith("jesse_suit_jacket_blue_d.tex")
    # new textures are encoded exactly like (same size as) the texture they replace
    for p, like in new_textures.items():
        assert len(res.textures[p]) == len(game.read(like))


def test_lod_copy_mode(pkg, game):
    blender("export", pkg, "--lods", "copy", blend=pkg / "model.blend")
    # copies of LOD0 in every LOD cannot fit the original size (the baked LOD3 keeps its
    # original geometry, LOD1-2 get LOD0's): strict builds refuse, experimental ones allow
    with pytest.raises(package.BuildError, match="exceeds the original's .*vertex buffer"):
        package.build_from_blender(pkg, read_texture=game.try_read, log=lambda s: None)
    res = package.build_from_blender(pkg, read_texture=game.try_read, log=lambda s: None, strict=False)
    out = BinFbx.parse(res.binfbx)
    lod0 = sum(m.face_count for m in out.meshes if m.lod == 0)
    for lod in range(1, out.lod_count):
        assert sum(m.face_count for m in out.meshes if m.lod == lod) == lod0
    # LOD3 keeps its 4-influence vertex layout
    assert {m.unk0 for m in out.meshes if m.lod == 3} == {4}


def test_render_preview(pkg, tmp_path):
    out = tmp_path / "front.png"
    blender("render", out, blend=pkg / "model.blend")
    assert out.exists() and out.stat().st_size > 10_000


# ------------------------------------------------------------------ control-mod tools (adopt / check)

MAKE_GLB = """
import bpy, sys
out = sys.argv[sys.argv.index("--") + 1]
for o in list(bpy.data.objects):
    bpy.data.objects.remove(o)
bpy.ops.object.armature_add(location=(0, 0, 0))
rig = bpy.context.active_object
rig.name = "SomeoneElsesRig"
bpy.ops.mesh.primitive_uv_sphere_add(radius=0.12, location=(0, -0.12, 1.35))
part = bpy.context.active_object
part.name = "chest_piece"
img = bpy.data.images.new("chest_piece_albedo", 37, 23)
img.pixels.foreach_set([0.2, 0.9, 0.3, 1.0] * (37 * 23))
img.pack()
mat = bpy.data.materials.new("Chest Piece")
mat.use_nodes = True
tex = mat.node_tree.nodes.new("ShaderNodeTexImage")
tex.image = img
bsdf = mat.node_tree.nodes["Principled BSDF"]
mat.node_tree.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
bsdf.inputs["Roughness"].default_value = 0.3
part.data.materials.append(mat)
part.vertex_groups.new(name="Bone").add(range(len(part.data.vertices)), 1.0, "REPLACE")
part.parent = rig
part.modifiers.new("Armature", "ARMATURE").object = rig
bpy.ops.export_scene.gltf(filepath=out, export_format="GLB")
"""


def tools(blend, *args, ok=(0,)):
    cmd = [str(BLENDER), "--background", str(blend), "--python", str(TOOLS_SCRIPT), "--", *map(str, args)]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = p.stdout + p.stderr
    assert p.returncode in ok and "Traceback" not in out, out[-3000:]
    return p.returncode, out


def test_tools_embedded_in_new_blends(pkg):
    script = "import bpy; t = bpy.data.texts.get('control_mod_tools.py'); print('TOOLS', bool(t and t.use_module))"
    cmd = [str(BLENDER), "--background", str(pkg / "model.blend"), "--python-expr", script]
    assert "TOOLS True" in subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace").stdout


def test_adopt_glb_then_build(pkg, game, tmp_path):
    glb = tmp_path / "part.glb"
    maker = tmp_path / "make_glb.py"
    maker.write_text(MAKE_GLB)
    p = subprocess.run([str(BLENDER), "--background", "--factory-startup", "--python", str(maker), "--", str(glb)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert glb.exists(), (p.stdout + p.stderr)[-2000:]
    blend = pkg / "adopt.blend"  # next to textures/
    blend.write_bytes((pkg / "model.blend").read_bytes())
    _, out = tools(blend, "adopt", "--import", glb, "--save")
    assert "material chest_piece: copy of" in out and "weights copied" in out
    code, out = tools(blend, "check", ok=(0, 2))
    assert code == 0, out[-3000:]
    assert "chest_piece" not in out  # no warnings about the adopted part (e.g. unweighted vertices)

    blender("export", pkg, blend=blend)
    res = package.build_from_blender(pkg, read_texture=game.try_read, log=lambda s: None, size_budget=False)
    out_fbx = BinFbx.parse(res.binfbx)
    names = [m.name.text for m in out_fbx.materials]
    assert "chest_piece" in names
    orig = BinFbx.parse(game.read(MODEL))
    assert len(out_fbx.meshes) == len(orig.meshes) + 1
    # every slot of the copied material got a new texture with the replaced texture's size
    assert res.derived and all(len(res.textures[p]) == len(game.read(like)) for p, like in res.derived.items())
    part = next(s for s in decode(out_fbx) if s.material == names.index("chest_piece") and s.lod == 0)
    assert np.allclose(part.bone_weights.sum(1), 1, atol=1e-3)
    assert len(np.unique(part.bone_indices[part.bone_weights > 0.05])) > 1  # weighted to the torso, not one bone
    green = next(p for p in res.derived if p.endswith("_g_scolormap.tex"))
    assert green


def test_check_reports_problems(pkg, tmp_path):
    blend = pkg / "bad.blend"
    blend.write_bytes((pkg / "model.blend").read_bytes())
    script = (
        "import bpy\n"
        "bpy.ops.mesh.primitive_cube_add(size=0.1)\n"
        "bpy.context.active_object.data.materials.append(bpy.data.materials.new('plain'))\n"
        "bpy.ops.wm.save_mainfile()\n"
    )
    subprocess.run([str(BLENDER), "--background", str(blend), "--python-expr", script], capture_output=True)
    code, out = tools(blend, "check", ok=(2,))
    assert "material 'plain' is not a game material" in out
    assert "not in a LOD collection" in out and "no bone weights" in out
