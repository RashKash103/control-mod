"""Reader/writer for Northlight RMDP archives (Control: version 9).

An archive is a pair of files:

* ``<name>.bin``  -- table of contents (header, folder tree, file entries, name table)
* ``<name>.rmdp`` -- raw concatenated file payloads

Layout of the ``.bin`` (little endian, all offsets in bytes)::

    u8   endianness (0 = little)
    u32  version (9 for Control)
    u32  folder count
    u32  file count
    u64  unknown (always 1)
    u32  name table size
    char[8]   path prefix / build tag
    u8[120]   reserved (garbage from the original tool's memory)
    FolderEntry[folder count]   48 bytes each
    FileEntry[file count]       60 bytes each
    char[name table size]       NUL-terminated names; offsets are relative to its start

FolderEntry: u32 name_hash, i64 next_sibling, i64 parent, u32 unk (0xCACACACA),
             i64 name_offset, i64 first_child, i64 first_file
FileEntry:   u32 name_hash, i64 next_file, i64 parent, u32 flags, i64 name_offset,
             u64 data_offset, u64 data_size, u32 data_crc, u64 write_time

Name hashes are zlib CRC32 of the lower-cased entry name. Folder 0 is the unnamed root.
"""

from __future__ import annotations

import os
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

VERSION_CONTROL = 9
HEADER_FMT = "<BIIIQI8s120s"
HEADER_SIZE = struct.calcsize(HEADER_FMT)  # 153
FOLDER_FMT = "<IqqIqqq"
FOLDER_SIZE = struct.calcsize(FOLDER_FMT)  # 48
FILE_FMT = "<IqqIqQQIQ"
FILE_SIZE = struct.calcsize(FILE_FMT)  # 60
FOLDER_UNK = 0xCACACACA


def name_hash(name: str) -> int:
    return zlib.crc32(name.lower().encode("utf-8")) & 0xFFFFFFFF


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
    unk_header: int
    prefix: bytes
    reserved: bytes
    folders: list[FolderEntry]
    entries: list[FileEntry]
    files: dict[str, FileEntry] = field(default_factory=dict)

    @classmethod
    def open(cls, path: str | os.PathLike) -> "Archive":
        """Open by any of ``foo``, ``foo.bin`` or ``foo.rmdp``."""
        base = Path(path)
        if base.suffix.lower() in (".bin", ".rmdp", ".packmeta"):
            base = base.with_suffix("")
        bin_path = base.with_suffix(".bin")
        rmdp_path = base.with_suffix(".rmdp")
        data = bin_path.read_bytes()
        return cls.parse(data, bin_path, rmdp_path)

    @classmethod
    def parse(cls, data: bytes, bin_path: Path, rmdp_path: Path) -> "Archive":
        endian, version, nfolders, nfiles, unk, name_size, prefix, reserved = struct.unpack_from(
            HEADER_FMT, data, 0
        )
        if endian != 0:
            raise ValueError("big endian archives are not supported")
        if version not in (8, VERSION_CONTROL):
            raise ValueError(f"unsupported RMDP version {version}")
        names = data[len(data) - name_size :]

        def name_at(off: int) -> str:
            if off < 0:
                return ""
            end = names.index(b"\0", off)
            return names[off:end].decode("utf-8")

        pos = HEADER_SIZE
        folders = []
        for _ in range(nfolders):
            h, sib, parent, u, noff, child, ffile = struct.unpack_from(FOLDER_FMT, data, pos)
            folders.append(FolderEntry(name_at(noff), h, sib, parent, u, child, ffile))
            pos += FOLDER_SIZE
        entries = []
        for _ in range(nfiles):
            h, nxt, parent, flags, noff, off, size, crc, wt = struct.unpack_from(FILE_FMT, data, pos)
            entries.append(FileEntry(name_at(noff), h, nxt, parent, flags, off, size, crc, wt))
            pos += FILE_SIZE

        arc = cls(bin_path, rmdp_path, version, unk, prefix, reserved, folders, entries)
        folder_paths = arc._folder_paths()
        for e in entries:
            fp = folder_paths[e.parent] if e.parent >= 0 else ""
            e.path = f"{fp}/{e.name}" if fp else e.name
            arc.files[e.path.lower()] = e
        return arc

    def _folder_paths(self) -> list[str]:
        paths: list[str] = [""] * len(self.folders)
        for i, f in enumerate(self.folders):
            parts = []
            j = i
            while j >= 0 and self.folders[j].name:
                parts.append(self.folders[j].name)
                j = self.folders[j].parent
            paths[i] = "/".join(reversed(parts))
        return paths

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


def normalize(path: str) -> str:
    return path.replace("\\", "/").strip("/").lower()


# --------------------------------------------------------------------------- writing


def write_archive(
    out_base: str | os.PathLike,
    files: Iterable[tuple[str, bytes | os.PathLike]],
    template: Archive | None = None,
    write_time: int = 0,
    align: int = 1,
    prefix: bytes | None = None,
) -> Path:
    """Write ``<out_base>.bin`` + ``<out_base>.rmdp`` containing ``files``.

    ``files`` yields ``(archive_path, data_or_source_path)``. Paths use ``/`` and keep
    the original case (e.g. ``data/objects/characters/.../foo.binfbx``). ``template``
    supplies the header prefix/reserved bytes and per-file flags so a patch archive
    looks like the original it overrides.
    """
    out_base = Path(out_base)
    if out_base.suffix.lower() in (".bin", ".rmdp"):
        out_base = out_base.with_suffix("")
    out_base.parent.mkdir(parents=True, exist_ok=True)

    # Build folder tree. Children/files are kept in insertion order of sorted paths.
    items = sorted(((p.replace("\\", "/").strip("/"), d) for p, d in files), key=lambda t: t[0].lower())
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

    flags_for = {}
    if template is not None:
        flags_for = {k: e.flags for k, e in template.files.items()}

    names = bytearray()
    name_off: dict[str, int] = {}

    def intern(n: str) -> int:
        if n not in name_off:
            name_off[n] = len(names)
            names.extend(n.encode("utf-8") + b"\0")
        return name_off[n]

    folder_bytes = bytearray()
    for i, n in enumerate(nodes):
        parent_children = nodes[n.parent].children if n.parent >= 0 else [0]
        k = parent_children.index(i)
        sib = parent_children[k + 1] if k + 1 < len(parent_children) else -1
        noff = intern(n.name) if n.name else -1
        folder_bytes += struct.pack(
            FOLDER_FMT,
            name_hash(n.name) if n.name else 0xFFFFFFFF,
            sib,
            n.parent,
            FOLDER_UNK,
            noff,
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
            nxt = siblings[k + 1] if k + 1 < len(siblings) else -1
            full = "/".join(_path_of(nodes, parent) + [name]).lower()
            file_bytes += struct.pack(
                FILE_FMT,
                name_hash(name),
                nxt,
                parent,
                flags_for.get(full, 0),
                intern(name),
                offset,
                len(data),
                zlib.crc32(data) & 0xFFFFFFFF,
                write_time,
            )
            offset += len(data)

    if prefix is None:
        prefix = template.prefix if template else b"\0" * 8
    prefix = prefix[:8].ljust(8, b"\0")
    reserved = template.reserved if template else b"\0" * 120
    unk = template.unk_header if template else 1
    header = struct.pack(HEADER_FMT, 0, VERSION_CONTROL, len(nodes), len(file_recs), unk, len(names), prefix, reserved)
    out_base.with_suffix(".bin").write_bytes(header + folder_bytes + file_bytes + names)
    return out_base


def _path_of(nodes, idx: int) -> list[str]:
    parts = []
    while idx > 0:
        parts.append(nodes[idx].name)
        idx = nodes[idx].parent
    return list(reversed(parts))

