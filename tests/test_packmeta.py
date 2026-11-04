import glob
import struct
import zlib

import pytest

from conftest import GAME_DIR, needs_game
from control_mod import packmeta
from control_mod.packmeta import PackMeta


def test_normalize_and_resource_id():
    assert packmeta.normalize("data_pc/textures/A/B.tex") == "textures\\a\\b.tex"
    assert packmeta.normalize("data\\objects\\x.binfbx") == "objects\\x.binfbx"
    assert packmeta.normalize("runtimedata\\data_pc\\textures\\x.tex") == "textures\\x.tex"
    # value checked against ep100-000-pc.packmeta
    assert packmeta.resource_id("data_pc\\textures\\characters\\andro\\female_guard\\hands\\andro_female_guard_hands_s.tex") == 0x000C1144F40AE322


def test_build_and_parse_synthetic():
    reg = [(packmeta.FILE_INFO, "r::FileInfoMetadata"), (packmeta.RESOURCE, "r::ResourceMetadata")]
    pm = PackMeta(reg, [
        packmeta.Entry("data\\a.bin", {packmeta.FILE_INFO: packmeta.file_info(10, 0xAB), packmeta.RESOURCE: packmeta.resource_meta(packmeta.resource_id("data\\a.bin"))}),
        packmeta.Entry("data\\b.bin", {packmeta.FILE_INFO: packmeta.file_info(20, 0xCD)}),
    ])
    data = pm.to_bytes()
    again = PackMeta.parse(data)
    assert again.to_bytes() == data
    assert packmeta.read_file_info(again.find("data/b.bin").objects[packmeta.FILE_INFO]) == (20, 0xCD)
    assert packmeta.RESOURCE not in again.find("data/b.bin").objects


@needs_game
@pytest.mark.game
@pytest.mark.parametrize("path", sorted(glob.glob(f"{GAME_DIR}/data_packfiles/*-000-*.packmeta")) if GAME_DIR else [])
def test_game_packmeta_roundtrip(path):
    data = open(path, "rb").read()
    pm = PackMeta.parse(data)
    assert pm.to_bytes() == data


@needs_game
@pytest.mark.game
def test_metadata_describes_game_files(game):
    """FileInfo and MeshMetadata agree with the files they describe."""
    from control_mod.binfbx import BinFbx

    pm = PackMeta.parse((game.pack_dir / "ep100-000-generic.packmeta").read_bytes())
    path = "data/objects/characters/intermediate/jesse_civilian_publish_physx.binfbx"
    e = pm.find(path)
    data = game.read(path)
    assert packmeta.read_file_info(e.objects[packmeta.FILE_INFO]) == (len(data), zlib.crc32(data) & 0xFFFFFFFF)
    f = BinFbx.parse(data)
    assert packmeta.mesh_buffers(e.objects[packmeta.MESH]) == (len(f.secondary), len(f.primary), len(f.indices) // f.index_size, f.index_size)
    assert packmeta.mesh_bbox(e.objects[packmeta.MESH]) == pytest.approx(f.bbox, abs=1e-5)
    patched = packmeta.patch_mesh(e.objects[packmeta.MESH], 1, 2, 3, 4, (0, 0, 0, 1, 1, 1))
    assert packmeta.mesh_buffers(patched) == (1, 2, 3, 4) and len(patched) == len(e.objects[packmeta.MESH])
    assert struct.unpack_from("<I", patched, 12)[0] == packmeta.MESH
