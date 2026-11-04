import struct
import zlib

import pytest

from ctlmod.archive import FILE_SIZE, FOLDER_SIZE, HEADER_SIZE, Archive, name_hash, write_archive
from ctlmod.gamedata import CTLMOD_MARKER, is_ctlmod_archive


def test_struct_sizes():
    assert (HEADER_SIZE, FOLDER_SIZE, FILE_SIZE) == (153, 48, 60)


def test_name_hash_is_crc32_of_lowercase():
    assert name_hash("Data") == zlib.crc32(b"data")


def test_write_and_read_back(tmp_path):
    files = {
        "data/objects/characters/a.binfbx": b"A" * 100,
        "data/objects/characters/B.binfbx": b"bb",
        "data/objects/props/c.binfbx": b"",
        "data_pc/textures/x/y/z.tex": bytes(range(256)),
        "root.txt": b"root",
    }
    write_archive(tmp_path / "ep100-001-generic", files.items())
    arc = Archive.open(tmp_path / "ep100-001-generic.bin")
    assert arc.version == 9
    assert {e.path for e in arc} == set(files)
    for path, data in files.items():
        assert arc.read(path) == data
        assert path.upper() in arc  # lookups are case-insensitive
    for f in arc.folders:
        if f.name:
            assert f.name_hash == name_hash(f.name)
    # folder tree links are consistent: every file reachable from its folder's chain
    for i, folder in enumerate(arc.folders):
        j = folder.first_file
        while j != -1:
            assert arc.entries[j].parent == i
            j = arc.entries[j].next_file


def test_rejects_duplicate_paths(tmp_path):
    with pytest.raises(ValueError):
        write_archive(tmp_path / "x", [("a/b.txt", b"1"), ("A/B.txt", b"2")])


def test_crc_mismatch_detected(tmp_path):
    write_archive(tmp_path / "x", [("a.txt", b"hello")])
    rmdp = tmp_path / "x.rmdp"
    rmdp.write_bytes(b"jello")
    with pytest.raises(IOError):
        Archive.open(tmp_path / "x").read("a.txt")


def test_marker_and_template_flags(tmp_path):
    write_archive(tmp_path / "base", [("d/f.bin", b"orig")])
    base = Archive.open(tmp_path / "base")
    base.files["d/f.bin"].flags = 7
    write_archive(tmp_path / "patch", [("d/f.bin", b"new")], template=base, prefix=CTLMOD_MARKER)
    assert is_ctlmod_archive(tmp_path / "patch.bin")
    assert not is_ctlmod_archive(tmp_path / "base.bin")
    patch = Archive.open(tmp_path / "patch")
    assert patch.files["d/f.bin"].flags == 7
    assert struct.unpack_from("<I", (tmp_path / "patch.bin").read_bytes(), 1)[0] == 9
