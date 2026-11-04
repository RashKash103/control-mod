"""Mod manager behaviour against a fake game folder made of synthetic archives."""

import json

import pytest

from control_mod import installer
from control_mod.archive import Archive, write_archive
from control_mod.config import Config, State
from control_mod.gamedata import GameData, is_control_mod_archive, patch_name, split_archive_name
from control_mod.project import Mod


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
        assert is_control_mod_archive(packs / f"{stem}.bin")
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


def test_packmeta_written_for_metadata_builds(env):
    import zlib

    from control_mod import packmeta

    cfg, gd = env
    reg = [(packmeta.FILE_INFO, "r::FileInfoMetadata"), (packmeta.RESOURCE, "r::ResourceMetadata")]
    path = "data/objects/characters/a.binfbx"
    base = packmeta.PackMeta(reg, [packmeta.Entry(packmeta.game_path(path), {
        packmeta.FILE_INFO: packmeta.file_info(2, 0), packmeta.RESOURCE: packmeta.resource_meta(packmeta.resource_id(path))})])
    (gd.pack_dir / "ep100-000-generic.packmeta").write_bytes(base.to_bytes())
    mod = fake_built_mod(cfg, gd, "grown", {path: b"A-much-bigger-file"})
    man = json.loads(mod.manifest_path.read_text())
    st = state_for(cfg)
    installer.install(cfg, gd, st, log=lambda s: None)
    assert not (gd.pack_dir / "ep100-001-generic.packmeta").exists()  # not requested
    man["metadata"] = True
    mod.manifest_path.write_text(json.dumps(man))
    installer.install(cfg, gd, st, log=lambda s: None)
    pm = packmeta.PackMeta.parse((gd.pack_dir / "ep100-001-generic.packmeta").read_bytes())
    e = pm.find(path)
    data = b"A-much-bigger-file"
    assert packmeta.read_file_info(e.objects[packmeta.FILE_INFO]) == (len(data), zlib.crc32(data))
    assert e.objects[packmeta.RESOURCE] == base.entries[0].objects[packmeta.RESOURCE]
    installer.uninstall(cfg, gd, st, log=lambda s: None)
    assert not (gd.pack_dir / "ep100-001-generic.packmeta").exists()


def test_packmeta_new_texture_and_model_dependencies(tmp_path):
    """New textures get the metadata of the texture they derive from; the model's
    dependency list gains every texture its materials reference."""
    from conftest import material, synthetic_fbx
    from control_mod import packmeta

    game = tmp_path / "Control"
    packs = game / "data_packfiles"
    packs.mkdir(parents=True)
    model_path = "data/objects/characters/m.binfbx"
    old_tex = "data_pc/textures/test/a_d.tex"
    new_tex = "data_pc/textures/control-mod/demo/m/new_d.tex"
    fbx = synthetic_fbx()
    write_archive(packs / "ep100-000-generic", [(model_path, fbx.to_bytes())])
    write_archive(packs / "ep100-000-pc", [(old_tex, b"T0")])
    reg = [(packmeta.MESH, "rend::MeshMetadata"), (packmeta.TEXTURE, "rend::TextureMetadata"),
           (packmeta.FILE_INFO, "r::FileInfoMetadata"), (packmeta.RESOURCE, "r::ResourceMetadata")]
    tex_meta = packmeta.make_object(packmeta.TEXTURE, b"texture-description")
    mesh_meta = packmeta.make_object(packmeta.MESH, struct_mesh_payload())
    old_tex_id = packmeta.resource_id(old_tex)
    (packs / "ep100-000-generic.packmeta").write_bytes(packmeta.PackMeta(reg, [packmeta.Entry(packmeta.game_path(model_path), {
        packmeta.MESH: mesh_meta, packmeta.FILE_INFO: packmeta.file_info(1, 0),
        packmeta.RESOURCE: packmeta.resource_meta(packmeta.resource_id(model_path), [old_tex_id])})]).to_bytes())
    (packs / "ep100-000-pc.packmeta").write_bytes(packmeta.PackMeta(reg, [packmeta.Entry(packmeta.game_path(old_tex), {
        packmeta.TEXTURE: tex_meta, packmeta.FILE_INFO: packmeta.file_info(2, 0),
        packmeta.RESOURCE: packmeta.resource_meta(old_tex_id)})]).to_bytes())
    gd = GameData(game)

    # the edited model points one material at the new texture
    fbx.materials[1] = material("mat_b", texture="runtimedata\\data_pc\\textures\\control-mod\\demo\\m\\new_d.tex")
    (tmp_path / "m.binfbx").write_bytes(fbx.to_bytes())
    (tmp_path / "new_d.tex").write_bytes(b"NEW")
    model_meta = packmeta.PackMeta.parse(installer.build_packmeta(gd, "ep100-000-generic", [(model_path, tmp_path / "m.binfbx")]))
    rid, deps = packmeta.read_resource_meta(model_meta.find(model_path).objects[packmeta.RESOURCE])
    assert rid == packmeta.resource_id(model_path)
    assert deps[0] == old_tex_id and packmeta.resource_id(new_tex) in deps
    assert packmeta.mesh_buffers(model_meta.find(model_path).objects[packmeta.MESH])[:2] == (len(fbx.secondary), len(fbx.primary))

    tex_pm = packmeta.PackMeta.parse(installer.build_packmeta(gd, "ep100-000-pc", [(new_tex, tmp_path / "new_d.tex")], {new_tex: old_tex}))
    e = tex_pm.find(new_tex)
    assert e.objects[packmeta.TEXTURE] == tex_meta  # cloned from the original texture
    assert packmeta.read_file_info(e.objects[packmeta.FILE_INFO])[0] == 3
    assert packmeta.read_resource_meta(e.objects[packmeta.RESOURCE]) == (packmeta.resource_id(new_tex), [])


def struct_mesh_payload() -> bytes:
    """A minimal MeshMetadata payload: buffer description + bounding box."""
    import struct

    from control_mod import packmeta

    buffers = packmeta.make_object(packmeta.MESH_BUFFERS, struct.pack("<IQQIII", 3, 0, 0, 0, 2, 0))
    bbox = packmeta.make_object(packmeta.BOUNDING_BOX, struct.pack("<I6f", 1, 0, 0, 0, 1, 1, 1))
    return struct.pack("<I", 21) + buffers + bbox


def test_config_roundtrip(tmp_path):
    cfg = Config(tmp_path, game_dir=tmp_path / "g", blender=tmp_path / 'b "quoted"\\blender.exe', patch_number=4)
    cfg.save()
    again = Config.load(tmp_path)
    assert (again.game_dir, again.blender, again.patch_number) == (cfg.game_dir, cfg.blender, 4)
