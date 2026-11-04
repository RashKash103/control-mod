"""Full pipeline through a real Blender (skipped without Blender or the game)."""

import subprocess
from pathlib import Path

import numpy as np
import pytest

from conftest import BLENDER, needs_blender, needs_game
from ctlmod import package
from ctlmod.binfbx import BinFbx
from ctlmod.geometry import decode
from ctlmod.project import BLENDER_SCRIPT

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
    d = tmp_path_factory.mktemp("pkg") / "jesse_suit"
    package.export_package(game.read(MODEL), MODEL, d, game.try_read, log=lambda s: None)
    blender("import", d, d / "model.blend")
    return d


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


def test_edits_new_mesh_and_material(pkg, game, tmp_path):
    blend = tmp_path / "edited.blend"
    blend.write_bytes((pkg / "model.blend").read_bytes())
    run_script(blend, ROOT / "examples" / "demo_edit.py", "--halo", "--red-jacket")
    # the script saved its texture next to the blend; point the package at it
    blender("export", pkg, blend=blend)
    res = package.build_from_blender(pkg, read_texture=game.try_read, log=lambda s: None)
    out = BinFbx.parse(res.binfbx)
    names = [m.name.text for m in out.materials]
    assert "jesse_suit_jacket_red" in names
    assert any(p.endswith("jacket_red_d.tex") for p in res.textures)
    assert len(out.meshes) == 32  # 31 original + halo
    halo = [s for s in decode(out) if s.material == names.index("jesse_suit_accessories") and s.lod == 0]
    assert any(s.positions[:, 1].min() > 1.8 for s in halo)  # a whole mesh above the head (game Y up)


def test_lod_copy_mode(pkg, game):
    blender("export", pkg, "--lods", "copy", blend=pkg / "model.blend")
    res = package.build_from_blender(pkg, read_texture=game.try_read, log=lambda s: None)
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
