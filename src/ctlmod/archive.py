"""Reader/writer for Northlight RMDP archives (Control: version 9).

An archive is a pair of files:

* ``<name>.bin``  -- table of contents (header, folder trees, file entries, name table)
* ``<name>.rmdp`` -- raw concatenated file payloads

Layout of the ``.bin`` (little endian, all offsets in bytes)::

    u8   endianness (0 = little)
    u32  version (9 for Control)
    u32  folder count
    u32  file count
    u32  deletion folder count   (1 in every game archive: an empty root)
    u32  deletion file count     (0)
    u32  name table size
    u8[128]   build tag area: 13 zero bytes, then "x%016x" (build time as a Windows
              FILETIME) + NUL at byte 38; the rest is uninitialised memory
    FolderEntry[folder count]            48 bytes each
    FileEntry[file count]                60 bytes each
    FolderEntry[deletion folder count]   a second tree listing deleted paths
    FileEntry[deletion file count]
    char[name table size]                NUL-terminated names; offsets relative to its start

FolderEntry: u32 name_hash, i64 next_sibling, i64 parent, u32 0xCACACACA,
             i64 name_offset, i64 first_child, i64 first_file
FileEntry:   u32 name_hash, i64 next_file, i64 parent, u32 flags, i64 name_offset,
             u64 data_offset, u64 data_size, u32 data_crc, u64 write_time (FILETIME)

Name hashes are zlib CRC32 of the lower-cased name; the unnamed root folder has hash 0.
Siblings are sorted by name. Payloads in the ``.rmdp`` start on 32-byte boundaries.

The engine loads these records straight into memory and turns indices into pointers,
so a missing deletion tree or a wrong count makes it follow garbage pointers.
"""

from __future__ import annotations

import os
import struct
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

VERSION_CONTROL = 9
HEADER_FMT = "<BIIIIII128s"
HEADER_SIZE = struct.calcsize(HEADER_FMT)  # 153
FOLDER_FMT = "<IqqIqqq"
FOLDER_SIZE = struct.calcsize(FOLDER_FMT)  # 48
FILE_FMT = "<IqqIqQQIQ"
FILE_SIZE = struct.calcsize(FILE_FMT)  # 60
FOLDER_UNK = 0xCACACACA
DATA_ALIGN = 32
TAG_OFFSET = 13  # inside the 128-byte tag area (absolute byte 38)
MARKER_OFFSET = 96  # where ctlmod puts its marker, in the area the game leaves as garbage
FILETIME_EPOCH = 116444736000000000  # 1601-01-01 -> 1970-01-01 in 100 ns units


def name_hash(name: str) -> int:
    return zlib.crc32(name.lower().encode("utf-8")) & 0xFFFFFFFF


def filetime_now() -> int:
    return FILETIME_EPOCH + time.time_ns() // 100


@dataclass
class FolderEntry:
    name: str
    name_hash: int
    next_sibling: int
    parent: int
    unk: int
    first_child: int
    first_file: int


@dataclass
class FileEntry:
    name: str
    name_hash: int
    next_file: int
    parent: int
    flags: int
    offset: int
    size: int
    crc: int
    write_time: int
    path: str = ""


@dataclass
class Archive:
    """An opened archive. ``files`` maps lower-case forward-slash paths to entries."""

    bin_path: Path
    rmdp_path: Path
    version: int
    tag_area: bytes
    folders: list[FolderEntry]
    entries: list[FileEntry]
    deleted_folders: list[FolderEntry] = field(default_factory=list)
    deleted_entries: list[FileEntry] = field(default_factory=list)
    files: dict[str, FileEntry] = field(default_factory=dict)

    @property
    def build_tag(self) -> str:
        """The ``x<FILETIME hex>`` build stamp, or '' if absent."""
        raw = self.tag_area[TAG_OFFSET:].split(b"\0", 1)[0]
        return raw.decode("latin-1") if raw[:1] == b"x" else ""

    @classmethod
    def open(cls, path: str | os.PathLike) -> "Archive":
        """Open by any of ``foo``, ``foo.bin`` or ``foo.rmdp``."""
        base = Path(path)
        if base.suffix.lower() in (".bin", ".rmdp", ".packmeta"):
            base = base.with_suffix("")
        bin_path = base.with_suffix(".bin")
        rmdp_path = base.with_suffix(".rmdp")
        return cls.parse(bin_path.read_bytes(), bin_path, rmdp_path)

    @classmethod
    def parse(cls, data: bytes, bin_path: Path, rmdp_path: Path) -> "Archive":
        endian, version, nfolders, nfiles, ndel_folders, ndel_files, name_size, tag = struct.unpack_from(HEADER_FMT, data, 0)
        if endian != 0:
            raise ValueError("big endian archives are not supported")
        if version not in (8, VERSION_CONTROL):
            raise ValueError(f"unsupported RMDP version {version}")
        expected = HEADER_SIZE + FOLDER_SIZE * (nfolders + ndel_folders) + FILE_SIZE * (nfiles + ndel_files) + name_size
        if expected != len(data):
            raise ValueError(f"{bin_path}: size {len(data)} does not match its header ({expected})")
        names = data[len(data) - name_size :]

        def name_at(off: int) -> str:
            if off < 0:
                return ""
            end = names.index(b"\0", off)
            return names[off:end].decode("utf-8")

        pos = HEADER_SIZE

        def folders_(n):
            nonlocal pos
            out = []
            for _ in range(n):
                h, sib, parent, u, noff, child, ffile = struct.unpack_from(FOLDER_FMT, data, pos)
                out.append(FolderEntry(name_at(noff), h, sib, parent, u, child, ffile))
                pos += FOLDER_SIZE
            return out

        def files_(n):
            nonlocal pos
            out = []
            for _ in range(n):
                h, nxt, parent, flags, noff, off, size, crc, wt = struct.unpack_from(FILE_FMT, data, pos)
                out.append(FileEntry(name_at(noff), h, nxt, parent, flags, off, size, crc, wt))
                pos += FILE_SIZE
            return out

        folders = folders_(nfolders)
        entries = files_(nfiles)
        deleted_folders = folders_(ndel_folders)
        deleted_entries = files_(ndel_files)
        arc = cls(bin_path, rmdp_path, version, tag, folders, entries, deleted_folders, deleted_entries)
        folder_paths = _folder_paths(folders)
        for e in entries:
            fp = folder_paths[e.parent] if e.parent >= 0 else ""
            e.path = f"{fp}/{e.name}" if fp else e.name
            arc.files[e.path.lower()] = e
        return arc

    def __contains__(self, path: str) -> bool:
        return normalize(path) in self.files

    def __iter__(self) -> Iterator[FileEntry]:
        return iter(self.entries)

    def get(self, path: str) -> FileEntry:
        return self.files[normalize(path)]

    def read(self, entry: FileEntry | str, verify: bool = True) -> bytes:
        if isinstance(entry, str):
            entry = self.get(entry)
        with open(self.rmdp_path, "rb") as f:
            f.seek(entry.offset)
            data = f.read(entry.size)
        if verify and (zlib.crc32(data) & 0xFFFFFFFF) != entry.crc:
            raise IOError(f"CRC mismatch for {entry.path}")
        return data

    def glob(self, pattern: str) -> list[FileEntry]:
        import fnmatch

        pat = normalize(pattern)
        return [e for p, e in self.files.items() if fnmatch.fnmatchcase(p, pat)]


def _folder_paths(folders: list[FolderEntry]) -> list[str]:
    paths: list[str] = [""] * len(folders)
    for i in range(len(folders)):
        parts = []
        j = i
        while j >= 0 and folders[j].name:
            parts.append(folders[j].name)
            j = folders[j].parent
        paths[i] = "/".join(reversed(parts))
    return paths


def normalize(path: str) -> str:
    return path.replace("\\", "/").strip("/").lower()


# --------------------------------------------------------------------------- writing


def make_tag_area(build_time: int, marker: bytes = b"") -> bytes:
    area = bytearray(128)
    tag = f"x{build_time:016x}".encode() + b"\0"
    area[TAG_OFFSET : TAG_OFFSET + len(tag)] = tag
    if marker:
        area[MARKER_OFFSET : MARKER_OFFSET + len(marker)] = marker
    return bytes(area)


def write_archive(
    out_base: str | os.PathLike,
    files: Iterable[tuple[str, bytes | os.PathLike]],
    template: Archive | None = None,
    write_time: int | None = None,
    align: int = DATA_ALIGN,
    marker: bytes = b"",
) -> Path:
    """Write ``<out_base>.bin`` + ``<out_base>.rmdp`` containing ``files``.

    ``files`` yields ``(archive_path, data_or_source_path)``; paths use ``/`` and keep
    their case. ``template`` supplies per-file flags. The archive gets a fresh build
    stamp and file times (now, unless ``write_time`` is given) so it is newer than the
    archives it overrides. ``marker`` is stored in the unused part of the header.
    """
    out_base = Path(out_base)
    if out_base.suffix.lower() in (".bin", ".rmdp"):
        out_base = out_base.with_suffix("")
    out_base.parent.mkdir(parents=True, exist_ok=True)
    now = filetime_now() if write_time is None else write_time

    # sort per path component, like the game's archives (siblings in name order)
    items = sorted(((p.replace("\\", "/").strip("/"), d) for p, d in files), key=lambda t: t[0].lower().split("/"))
    seen: set[str] = set()
    for p, _ in items:
        if p.lower() in seen:
            raise ValueError(f"duplicate path {p}")
        seen.add(p.lower())

    class _Node:
        def __init__(self, name: str, parent: int):
            self.name = name
            self.parent = parent
            self.children: list[int] = []
            self.files: list[int] = []

    nodes = [_Node("", -1)]
    index: dict[str, int] = {"": 0}

    def folder_for(parts: list[str]) -> int:
        key = ""
        cur = 0
        for part in parts:
            key = f"{key}/{part.lower()}"
            if key not in index:
                nodes.append(_Node(part, cur))
                index[key] = len(nodes) - 1
                nodes[cur].children.append(index[key])
            cur = index[key]
        return cur

    file_recs = []  # (name, parent folder, data)
    for p, d in items:
        parts = p.split("/")
        parent = folder_for(parts[:-1])
        nodes[parent].files.append(len(file_recs))
        file_recs.append((parts[-1], parent, d))

    # Folders were created in path order, which is a pre-order walk with name-sorted
    # siblings - the same order as the game's archives.
    flags_for = {k: e.flags for k, e in template.files.items()} if template is not None else {}
    names = bytearray()

    def add_name(n: str) -> int:
        off = len(names)
        names.extend(n.encode("utf-8") + b"\0")
        return off

    folder_bytes = bytearray()
    for i, n in enumerate(nodes):
        siblings = nodes[n.parent].children if n.parent >= 0 else [0]
        k = siblings.index(i)
        folder_bytes += struct.pack(
            FOLDER_FMT,
            name_hash(n.name) if n.name else 0,
            siblings[k + 1] if k + 1 < len(siblings) else -1,
            n.parent,
            FOLDER_UNK,
            add_name(n.name) if n.name else -1,
            n.children[0] if n.children else -1,
            n.files[0] if n.files else -1,
        )

    file_bytes = bytearray()
    offset = 0
    with open(out_base.with_suffix(".rmdp"), "wb") as rmdp:
        for i, (name, parent, d) in enumerate(file_recs):
            data = d if isinstance(d, (bytes, bytearray)) else Path(d).read_bytes()
            if align > 1 and offset % align:
                pad = align - offset % align
                rmdp.write(b"\0" * pad)
                offset += pad
            rmdp.write(data)
            siblings = nodes[parent].files
            k = siblings.index(i)
            full = "/".join(_path_of(nodes, parent) + [name]).lower()
            file_bytes += struct.pack(
                FILE_FMT,
                name_hash(name),
                siblings[k + 1] if k + 1 < len(siblings) else -1,
                parent,
                flags_for.get(full, 0),
                add_name(name),
                offset,
                len(data),
                zlib.crc32(data) & 0xFFFFFFFF,
                now,
            )
            offset += len(data)
        if align > 1 and offset % align:
            rmdp.write(b"\0" * (align - offset % align))

    # empty deletion tree: just its root, exactly as in every game archive
    deletion_root = struct.pack(FOLDER_FMT, 0, -1, -1, FOLDER_UNK, -1, -1, -1)
    header = struct.pack(
        HEADER_FMT, 0, VERSION_CONTROL, len(nodes), len(file_recs), 1, 0, len(names), make_tag_area(now, marker)
    )
    out_base.with_suffix(".bin").write_bytes(header + folder_bytes + file_bytes + deletion_root + names)
    return out_base


def _path_of(nodes, idx: int) -> list[str]:
    parts = []
    while idx > 0:
        parts.append(nodes[idx].name)
        idx = nodes[idx].parent
    return list(reversed(parts))
