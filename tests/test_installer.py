"""Mod manager behaviour against a fake game folder made of synthetic archives."""

import json

import pytest

from ctlmod import installer
from ctlmod.archive import Archive, write_archive
from ctlmod.config import Config, State
from ctlmod.gamedata import GameData, is_ctlmod_archive, patch_name, split_archive_name
from ctlmod.project import Mod


@pytest.fixture
def env(tmp_path):
    game = tmp_path / "Control"
    packs = game / "data_packfiles"
    packs.mkdir(parents=True)
    write_archive(packs / "ep100-000-generic", [("data/objects/characters/a.binfbx", b"A0"), ("data/objects/characters/b.binfbx", b"B0")])
    write_archive(packs / "ep100-000-pc", [("data_pc/textures/a_d.tex", b"T0")])
    write_archive(packs / "ep101-000-generic", [("data/objects/characters/dlc.binfbx", b"D0")])
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = Config(ws, game_dir=game)
    cfg.save()
    return cfg, GameData(game)


def fake_built_mod(cfg, gd, mod_id, files: dict[str, bytes]):
    mod = Mod.create(cfg, mod_id)
    entries = []
    for path, data in files.items():
        dest = mod.build_dir / "files" / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        arc = gd.locate(path).archive_name if path in gd else "ep100-000-pc"
        entries.append({"path": path, "archive": arc, "new": path not in gd, "model": "x", "size": len(data)})
    mod.manifest_path.write_text(json.dumps({"id": mod_id, "files": entries}))
    return mod


def state_for(cfg):
    st = State.load(cfg)
    st.sync(Mod.all_ids(cfg))
    return st


def test_archive_names():
    assert split_archive_name("ep100-000-generic-en") == ("ep100", 0, "generic-en")
    assert patch_name("ep101-000-pc", 3) == "ep101-003-pc"
    assert split_archive_name("random") is None


def test_install_routes_files_to_matching_patches(env):
    cfg, gd = env
    fake_built_mod(cfg, gd, "m1", {"data/objects/characters/a.binfbx": b"A1", "data_pc/textures/a_d.tex": b"T1",
                                   "data/objects/characters/dlc.binfbx": b"D1"})
    installer.install(cfg, gd, state_for(cfg), log=lambda s: None)
    packs = gd.pack_dir
    for stem, path, data in [("ep100-001-generic", "data/objects/characters/a.binfbx", b"A1"),
                             ("ep100-001-pc", "data_pc/textures/a_d.tex", b"T1"),
                             ("ep101-001-generic", "data/objects/characters/dlc.binfbx", b"D1")]:
        assert is_ctlmod_archive(packs / f"{stem}.bin")
        assert Archive.open(packs / stem).read(path) == data
    # the original archives are untouched and GameData still serves originals
    assert GameData(gd.game_dir).read("data/objects/characters/a.binfbx") == b"A0"


def test_load_order_conflicts_and_disable(env):
    cfg, gd = env
    fake_built_mod(cfg, gd, "low", {"data/objects/characters/a.binfbx": b"LOW", "data/objects/characters/b.binfbx": b"B_LOW"})
    fake_built_mod(cfg, gd, "high", {"data/objects/characters/a.binfbx": b"HIGH"})
    st = state_for(cfg)
    assert st.order == ["high", "low"]  # alphabetical on first sync
    st.order = ["low", "high"]
    plan = installer.install(cfg, gd, st, log=lambda s: None)
    assert plan.conflicts == [("data/objects/characters/a.binfbx", "low", "high")]
    arc = Archive.open(gd.pack_dir / "ep100-001-generic")
    assert arc.read("data/objects/characters/a.binfbx") == b"HIGH"
    assert arc.read("data/objects/characters/b.binfbx") == b"B_LOW"
    # disable the winner: the other mod's version comes back
    st.enabled["high"] = False
    installer.install(cfg, gd, st, log=lambda s: None)
    assert Archive.open(gd.pack_dir / "ep100-001-generic").read("data/objects/characters/a.binfbx") == b"LOW"
    # disable everything: no archives remain
    st.enabled["low"] = False
    installer.install(cfg, gd, st, log=lambda s: None)
    assert installer.installed_archives(gd.pack_dir) == []


def test_skips_slots_used_by_other_mods(env):
    cfg, gd = env
    write_archive(gd.pack_dir / "ep100-001-generic", [("data/objects/characters/b.binfbx", b"OTHER")])
    gd2 = GameData(gd.game_dir)
    fake_built_mod(cfg, gd2, "m", {"data/objects/characters/a.binfbx": b"A1"})
    installer.install(cfg, gd2, state_for(cfg), log=lambda s: None)
    assert (gd.pack_dir / "ep100-002-generic.bin").exists()
    installer.uninstall(cfg, gd2, state_for(cfg), log=lambda s: None)
    assert (gd.pack_dir / "ep100-001-generic.bin").exists()  # never deletes foreign archives
    assert not (gd.pack_dir / "ep100-002-generic.bin").exists()


def test_unbuilt_mods_are_skipped(env):
    cfg, gd = env
    Mod.create(cfg, "empty")
    plan = installer.install(cfg, gd, state_for(cfg), log=lambda s: None)
    assert plan.skipped == ["empty"] and not plan.archives


def test_pack_and_unpack(env, tmp_path):
    cfg, gd = env
    fake_built_mod(cfg, gd, "share", {"data/objects/characters/a.binfbx": b"S"})
    z = installer.pack_mod(cfg, "share", tmp_path / "share.zip", log=lambda s: None)
    other = Config(tmp_path / "ws2", game_dir=gd.game_dir)
    other.workspace.mkdir()
    assert installer.unpack_mod(other, z, log=lambda s: None) == "share"
    assert Mod.load(other, "share").manifest()["files"][0]["path"] == "data/objects/characters/a.binfbx"
    with pytest.raises(SystemExit):
        installer.unpack_mod(other, z, log=lambda s: None)


def test_export_loose(env, tmp_path):
    cfg, gd = env
    fake_built_mod(cfg, gd, "m", {"data_pc/textures/a_d.tex": b"T1"})
    n = installer.export_loose(cfg, gd, state_for(cfg), tmp_path / "loose", log=lambda s: None)
    assert n == 1 and (tmp_path / "loose" / "data_pc/textures/a_d.tex").read_bytes() == b"T1"


def test_config_roundtrip(tmp_path):
    cfg = Config(tmp_path, game_dir=tmp_path / "g", blender=tmp_path / 'b "quoted"\\blender.exe', patch_number=4)
    cfg.save()
    again = Config.load(tmp_path)
    assert (again.game_dir, again.blender, again.patch_number) == (cfg.game_dir, cfg.blender, 4)
