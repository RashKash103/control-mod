import struct
import time
import zlib

import pytest

from conftest import needs_game
from ctlmod.archive import (
    DATA_ALIGN, FILE_SIZE, FOLDER_SIZE, FOLDER_UNK, HEADER_SIZE, TAG_OFFSET, Archive, filetime_now, name_hash,
    write_archive,
)  # fmt: skip
from ctlmod.gamedata import CTLMOD_MARKER, is_ctlmod_archive

FILES = {
    "data/objects/characters/a.binfbx": b"A" * 100,
    "data/objects/characters/B.binfbx": b"bb",
    "data/objects/props/c.binfbx": b"",
    "data.x/z.txt": b"sorts after data/ per component",
    "data_pc/textures/x/y/z.tex": bytes(range(256)),
    "root.txt": b"root",
}


def test_struct_sizes():
    assert (HEADER_SIZE, FOLDER_SIZE, FILE_SIZE) == (153, 48, 60)


def test_name_hash_is_crc32_of_lowercase():
    assert name_hash("Data") == zlib.crc32(b"data")


def test_write_and_read_back(tmp_path):
    write_archive(tmp_path / "ep100-001-generic", FILES.items())
    arc = Archive.open(tmp_path / "ep100-001-generic.bin")
    assert arc.version == 9
    assert {e.path for e in arc} == set(FILES)
    for path, data in FILES.items():
        assert arc.read(path) == data
        assert path.upper() in arc  # lookups are case-insensitive
    for i, folder in enumerate(arc.folders):
        if folder.name:
            assert folder.name_hash == name_hash(folder.name)
        j = folder.first_file  # every file reachable from its folder's chain
        while j != -1:
            assert arc.entries[j].parent == i
            j = arc.entries[j].next_file


def test_layout_matches_game_conventions(tmp_path):
    """The engine maps these records straight into memory: the layout must be exact."""
    before = filetime_now()
    write_archive(tmp_path / "p", FILES.items())
    data = (tmp_path / "p.bin").read_bytes()
    _, _, nfolders, nfiles, ndel_folders, ndel_files, name_size = struct.unpack_from("<BIIIIII", data, 0)
    # one empty deletion root after the file table, like every game archive
    assert (ndel_folders, ndel_files) == (1, 0)
    assert len(data) == HEADER_SIZE + FOLDER_SIZE * (nfolders + 1) + FILE_SIZE * nfiles + name_size
    del_root = data[HEADER_SIZE + FOLDER_SIZE * nfolders + FILE_SIZE * nfiles :][:FOLDER_SIZE]
    assert del_root == struct.pack("<IqqIqqq", 0, -1, -1, FOLDER_UNK, -1, -1, -1)
    arc = Archive.open(tmp_path / "p")
    assert len(arc.deleted_folders) == 1 and arc.deleted_entries == []
    root = arc.folders[0]
    assert (root.name_hash, root.parent, root.next_sibling) == (0, -1, -1)
    # pre-order folder list, siblings sorted by name
    for i, f in enumerate(arc.folders[1:], 1):
        assert f.parent < i
    for f in arc.folders:
        kids, k = [], f.first_child
        while k != -1:
            kids.append(arc.folders[k].name.lower())
            k = arc.folders[k].next_sibling
        assert kids == sorted(kids)
    # payloads 32-byte aligned, file padded, fresh build stamp and file times
    assert all(e.offset % DATA_ALIGN == 0 for e in arc.entries)
    assert (tmp_path / "p.rmdp").stat().st_size % DATA_ALIGN == 0
    stamp = int(arc.build_tag[1:], 16)
    assert before <= stamp <= filetime_now()
    assert data[25 : 25 + TAG_OFFSET] == b"\0" * TAG_OFFSET
    assert all(e.write_time == stamp for e in arc.entries)


def test_rejects_duplicate_paths(tmp_path):
    with pytest.raises(ValueError):
        write_archive(tmp_path / "x", [("a/b.txt", b"1"), ("A/B.txt", b"2")])


def test_rejects_truncated_toc(tmp_path):
    write_archive(tmp_path / "x", [("a.txt", b"hello")])
    b = tmp_path / "x.bin"
    b.write_bytes(b.read_bytes()[:-50])
    with pytest.raises(ValueError):
        Archive.open(b)


def test_crc_mismatch_detected(tmp_path):
    write_archive(tmp_path / "x", [("a.txt", b"hello")])
    (tmp_path / "x.rmdp").write_bytes(b"jello")
    with pytest.raises(IOError):
        Archive.open(tmp_path / "x").read("a.txt")


def test_marker_and_template_flags(tmp_path):
    write_archive(tmp_path / "base", [("d/f.bin", b"orig")])
    base = Archive.open(tmp_path / "base")
    base.files["d/f.bin"].flags = 7
    write_archive(tmp_path / "patch", [("d/f.bin", b"new")], template=base, marker=CTLMOD_MARKER)
    assert is_ctlmod_archive(tmp_path / "patch.bin")
    assert not is_ctlmod_archive(tmp_path / "base.bin")
    patch = Archive.open(tmp_path / "patch")
    assert patch.files["d/f.bin"].flags == 7
    # the marker must not touch the bytes the game interprets
    assert (tmp_path / "patch.bin").read_bytes()[25:38] == b"\0" * 13


def test_legacy_marker_still_recognised(tmp_path):
    write_archive(tmp_path / "old", [("a.txt", b"x")])
    b = tmp_path / "old.bin"
    d = bytearray(b.read_bytes())
    d[25:33] = CTLMOD_MARKER  # where ctlmod 0.1 put it
    b.write_bytes(bytes(d))
    assert is_ctlmod_archive(b)


@needs_game
@pytest.mark.game
def test_rebuilding_a_game_archive_is_byte_identical(game, tmp_path):
    """Re-create small original archives from their own content: only the header's
    uninitialised bytes may differ."""
    small = [a for a in game.archives if len(a.entries) <= 3][:4]
    assert small
    for orig in small:
        files = [(e.path, orig.read(e)) for e in orig.entries]
        times = {e.write_time for e in orig.entries}
        assert len(times) == 1
        out = write_archive(tmp_path / orig.bin_path.stem, files, template=orig, write_time=times.pop())
        a, b = orig.bin_path.read_bytes(), out.with_suffix(".bin").read_bytes()
        assert len(a) == len(b)
        assert a[:38] == b[:38] and a[153:] == b[153:], orig.bin_path.stem
        assert out.with_suffix(".rmdp").read_bytes() == orig.rmdp_path.read_bytes()


@needs_game
@pytest.mark.game
def test_all_game_archives_parse_strictly(game):
    for a in game.archives:
        assert len(a.deleted_folders) == 1 and not a.deleted_entries
        assert a.build_tag.startswith("x01")
        assert all(e.offset % DATA_ALIGN == 0 for e in a.entries)
        assert a.folders[0].name_hash == 0


def test_filetime_now_is_plausible():
    years = (filetime_now() - 116444736000000000) / 1e7 / 3600 / 24 / 365.25
    assert abs(years - (time.time() / 3600 / 24 / 365.25)) < 0.01
