"""Shared-texture detection: usage index, build/install reporting."""

import json

from conftest import material, synthetic_fbx
from control_mod import installer
from control_mod.archive import write_archive
from control_mod.config import Config, State
from control_mod.gamedata import GameData
from control_mod.project import Mod
from control_mod.usage import TextureUsage, describe

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


# ------------------------------------------------------------------ which models the game uses


def test_model_refs_find_resource_ids_at_any_alignment(tmp_path):
    from control_mod.packmeta import resource_id
    from control_mod.usage import ModelRefs, describe_refs

    gd = make_game(tmp_path)
    rid = lambda m: resource_id(f"data/objects/characters/{m}.binfbx").to_bytes(8, "little")
    write_archive(gd.pack_dir / "ep101-000-generic", [
        ("data/worlds/gameworld/layers/hub_npc_a/entities.bin", b"\x01\x02\x03" + rid("outfit_a") + b"\x00" * 5),
        ("data/worlds/gameworld/episodes/x/tasks.bin", b"\x00" * 8 + rid("outfit_a") + rid("outfit_b")),
        ("data/text/strings.txt", rid("outfit_c")),  # not a type that places models
    ])
    refs = ModelRefs.scan(GameData(gd.game_dir))
    assert refs.files_using("data/objects/characters/outfit_a.binfbx") == [
        "data/worlds/gameworld/episodes/x/tasks.bin", "data/worlds/gameworld/layers/hub_npc_a/entities.bin"]
    assert refs.files_using(r"DATA\objects\characters\outfit_b.binfbx") == ["data/worlds/gameworld/episodes/x/tasks.bin"]
    assert refs.files_using("data/objects/characters/outfit_c.binfbx") == []
    assert describe_refs(refs.files_using("data/objects/characters/outfit_a.binfbx")) == "used by 2 file(s): x, hub_npc_a"
    assert "probably unused" in describe_refs([])
    calls = []
    ModelRefs.load(GameData(gd.game_dir), tmp_path / "state", progress=calls.append)
    calls.clear()
    ModelRefs.load(GameData(gd.game_dir), tmp_path / "state", progress=calls.append)
    assert not calls  # cached


def test_remove_deletes_and_uninstalls(tmp_path, monkeypatch):
    import argparse

    from control_mod import cli

    gd = make_game(tmp_path)
    cfg = Config(tmp_path / "ws", game_dir=gd.game_dir)
    for mod_id in ("keep", "gone"):
        mod = Mod.create(cfg, mod_id)
        files = mod.build_dir / "files" / "data/objects/characters"
        files.mkdir(parents=True)
        (files / "outfit_b.binfbx").write_bytes(model_bytes(FACE))
        mod.manifest_path.write_text(json.dumps({"id": mod_id, "files": [
            {"path": "data/objects/characters/outfit_b.binfbx", "archive": "ep100-000-generic", "new": False,
             "model": "outfit_b"}]}))
    st = cli._state(cfg)
    installer.install(cfg, gd, st, log=lambda s: None)
    assert sorted(State.load(cfg).installed["mods"]) == ["gone", "keep"]

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    try:
        cli.cmd_remove(cfg, argparse.Namespace(mod="gone", yes=False))
        raise AssertionError("removed without confirmation")
    except SystemExit as e:
        assert "--yes" in str(e)
    assert (cfg.mods_dir / "gone").exists()

    cli.cmd_remove(cfg, argparse.Namespace(mod="gone", yes=True))
    assert not (cfg.mods_dir / "gone").exists()
    st = State.load(cfg)
    assert st.order == ["keep"] and "gone" not in st.enabled
    assert st.installed["mods"] == ["keep"]  # patch archives rebuilt without it
