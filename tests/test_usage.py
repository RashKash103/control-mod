"""Shared-texture detection: usage index, build/install reporting."""

import json

from conftest import material, synthetic_fbx
from ctlmod import installer
from ctlmod.archive import write_archive
from ctlmod.config import Config, State
from ctlmod.gamedata import GameData
from ctlmod.project import Mod
from ctlmod.usage import TextureUsage, describe

FACE = "runtimedata\\data_pc\\textures\\test\\face_d.tex"
SUIT = "runtimedata\\data_pc\\textures\\test\\suit_d.tex"


def model_bytes(*textures):
    f = synthetic_fbx()
    f.materials = [material(f"m{i}", texture=t) for i, t in enumerate(textures)]
    f.mesh_material = [i % len(textures) for i in range(len(f.meshes))]
    f.variants = []
    return f.to_bytes()


def make_game(tmp_path):
    packs = tmp_path / "Control" / "data_packfiles"
    packs.mkdir(parents=True)
    write_archive(packs / "ep100-000-generic", [
        ("data/objects/characters/outfit_a.binfbx", model_bytes(FACE, SUIT)),
        ("data/objects/characters/outfit_b.binfbx", model_bytes(FACE)),
        ("data/objects/characters/outfit_c.binfbx", model_bytes(FACE)),
    ])
    write_archive(packs / "ep100-000-pc", [("data_pc/textures/test/face_d.tex", b"F"), ("data_pc/textures/test/suit_d.tex", b"S")])
    return GameData(tmp_path / "Control")


def test_index_finds_shared_textures(tmp_path):
    gd = make_game(tmp_path)
    usage = TextureUsage.scan(gd)
    face = "data_pc/textures/test/face_d.tex"
    assert usage.models_using(face) == [f"data/objects/characters/outfit_{x}.binfbx" for x in "abc"]
    assert usage.others(face, "data/objects/characters/outfit_a.binfbx") == [
        "data/objects/characters/outfit_b.binfbx", "data/objects/characters/outfit_c.binfbx"]
    # game-style path (backslashes, runtimedata prefix, any case) resolves the same
    assert usage.models_using(FACE.upper()) == usage.models_using(face)
    assert usage.others("data_pc/textures/test/suit_d.tex", "data/objects/characters/outfit_a.binfbx") == []
    assert set(usage.search("face")) == {face}
    assert describe(["a/x.binfbx", "b/y.binfbx"]) == "x, y"
    assert describe([f"m{i}.binfbx" for i in range(10)], 3) == "m0, m1, m2 and 7 more"


def test_index_is_cached_and_refreshed_after_game_update(tmp_path):
    gd = make_game(tmp_path)
    cache = tmp_path / "state"
    calls = []
    TextureUsage.load(gd, cache, progress=calls.append)
    assert calls  # built once
    calls.clear()
    TextureUsage.load(GameData(gd.game_dir), cache, progress=calls.append)
    assert not calls  # served from cache
    # a "game update" (new archive contents) invalidates the cache
    write_archive(gd.pack_dir / "ep100-000-generic", [("data/objects/characters/outfit_a.binfbx", model_bytes(SUIT))])
    u = TextureUsage.load(GameData(gd.game_dir), cache, progress=calls.append)
    assert calls and u.models_using("data_pc/textures/test/face_d.tex") == []


def test_install_reports_shared_impact(tmp_path):
    gd = make_game(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = Config(ws, game_dir=gd.game_dir)
    mod = Mod.create(cfg, "face_paint")
    dest = mod.build_dir / "files" / "data_pc/textures/test/face_d.tex"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"F2")
    others = ["data/objects/characters/outfit_b.binfbx", "data/objects/characters/outfit_c.binfbx"]
    mod.manifest_path.write_text(json.dumps({"id": "face_paint", "files": [
        {"path": "data_pc/textures/test/face_d.tex", "archive": "ep100-000-pc", "new": False, "also_affects": others}]}))
    st = State.load(cfg)
    st.sync(Mod.all_ids(cfg))
    assert installer.shared_impact(cfg, st) == {"face_paint": set(others)}
    lines = []
    installer.install(cfg, gd, st, dry_run=True, log=lines.append)
    assert any("edits shared textures" in ln and "2 other model(s)" in ln for ln in lines)
