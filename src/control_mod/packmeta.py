"""Reader/writer for ``.packmeta``: the per-file metadata the game keeps next to each archive.

The game trusts this metadata over the files themselves: it allocates load buffers from
``r::FileInfoMetadata`` (file size), GPU and ray-tracing buffers from
``rend::MeshMetadata`` (vertex/index buffer sizes, bounding box), streams textures with
``rend::TextureMetadata`` and binds material textures from it as well. A patch archive
that replaces files can ship its own ``.packmeta`` describing the new versions.

Layout (little endian)::

    u32 file_count, u32 1, u32 file_count, u32 names_size
    char[names_size]          NUL-terminated paths (backslashes), in file order
    u32[file_count]           offset of each path (file order)
    u32 file_count
    u64[file_count]           resource ids, ascending (see :func:`resource_id`)
    u32[file_count]           offset of the path for each id
    u32 type_count, Type[]    registry: u32 hash, u32 len, char[len], u32 object count
    sections                  for each registered type, in registry order: the
                              objects of that type for every file that has one, in
                              file order
    type lists                one object per file (type 0xe974fdff): u32 2, u32 name
                              offset, u32 n, n x RefObject(u64 = section_index << 32 | type)

Every object is ``u32 0xd34db33f, u32 version (2), u32 size, u32 type`` + payload +
``u32 0xd34db33f`` footer, where ``size`` counts all of it. ``tests/test_packmeta.py``
re-serialises every ``.packmeta`` in the game byte-identically.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

import cityhash

MAGIC = 0xD34DB33F
TYPE_LIST = 0xE974FDFF
TYPE_REF = 0x35AE54C1
TYPE_RESOURCE_ID = 0x1BFC72D5
FILE_INFO = 0x95E8C0EF
RESOURCE = 0x368B4205
MESH = 0xFE276448
TEXTURE = 0x968A49B7
MESH_BUFFERS = 0xBF008D7D
BOUNDING_BOX = 0xD8763D8B


def normalize(path: str) -> str:
    """``ResourceID::createResourceID``: drop everything up to and including the
    ``data*`` folder, use backslashes, lower case (``data_pc/textures/a.tex`` ->
    ``textures\\a.tex``)."""
    p = path.replace("/", "\\")
    low = p.lower()
    if low.startswith("data"):
        start = 0
    else:
        i = low.find("\\data")
        if i < 0:
            return low
        start = i + 1
    j = p.find("\\", start)
    return p[j + 1 :].lower() if j >= 0 else low


def resource_id(path: str) -> int:
    """The 64-bit id the game files metadata under: CityHash64 of the normalized path."""
    return cityhash.CityHash64(normalize(path).encode("utf-8"))


def game_path(archive_path: str) -> str:
    """Archive path (``data/x/y.binfbx``) -> the spelling used inside .packmeta."""
    return archive_path.replace("/", "\\")


# ----------------------------------------------------------------------------- objects


def make_object(type_hash: int, payload: bytes, version: int = 2) -> bytes:
    return struct.pack("<IIII", MAGIC, version, 16 + len(payload) + 4, type_hash) + payload + struct.pack("<I", MAGIC)


def object_type(obj: bytes) -> int:
    return struct.unpack_from("<I", obj, 12)[0]


def file_info(size: int, crc: int) -> bytes:
    return make_object(FILE_INFO, struct.pack("<IQI", 1, size, crc))


def read_file_info(obj: bytes) -> tuple[int, int]:
    _, size, crc = struct.unpack_from("<IQI", obj, 16)
    return size, crc


def _rid_object(rid: int) -> bytes:
    return make_object(TYPE_RESOURCE_ID, struct.pack("<IQ", 1, rid))


def resource_meta(rid: int, deps: list[int] | tuple[int, ...] = ()) -> bytes:
    """``r::ResourceMetadata``: the file's own id and the ids of resources it depends on
    (for a model: every texture its materials use)."""
    body = struct.pack("<I", 3) + _rid_object(rid) + struct.pack("<I", len(deps)) + b"".join(_rid_object(d) for d in deps)
    return make_object(RESOURCE, body)


def read_resource_meta(obj: bytes) -> tuple[int, list[int]]:
    """(own id, dependency ids) of a ResourceMetadata object."""
    rid = struct.unpack_from("<Q", obj, 20 + 20)[0]
    n = struct.unpack_from("<I", obj, 20 + 32)[0]
    deps = [struct.unpack_from("<Q", obj, 56 + 32 * i + 20)[0] for i in range(n)]
    return rid, deps


def _find_nested(obj: bytes, type_hash: int) -> int:
    """Offset of the first nested object of ``type_hash`` inside ``obj`` (or -1)."""
    needle = struct.pack("<I", type_hash)
    pos = 16
    while True:
        pos = obj.find(needle, pos)
        if pos < 0:
            return -1
        start = pos - 12
        if start >= 0 and struct.unpack_from("<I", obj, start)[0] == MAGIC:
            return start
        pos += 1


def mesh_buffers(obj: bytes) -> tuple[int, int, int, int]:
    """(secondary bytes, primary bytes, index count, index size) of a MeshMetadata."""
    at = _find_nested(obj, MESH_BUFFERS)
    _, sec, prim, icount, isize = struct.unpack_from("<IQQII", obj, at + 16)
    return sec, prim, icount, isize


def mesh_bbox(obj: bytes) -> tuple[float, ...]:
    at = _find_nested(obj, BOUNDING_BOX)
    return struct.unpack_from("<6f", obj, at + 20)


def patch_mesh(obj: bytes, secondary: int, primary: int, index_count: int, index_size: int,
               bbox: tuple[float, ...] | None = None) -> bytes:
    """Copy of a MeshMetadata object describing new buffer sizes (and bounding box)."""
    out = bytearray(obj)
    at = _find_nested(obj, MESH_BUFFERS)
    if at < 0:
        raise ValueError("MeshMetadata without a buffer description")
    ver = struct.unpack_from("<I", out, at + 16)[0]
    struct.pack_into("<IQQII", out, at + 16, ver, secondary, primary, index_count, index_size)
    if bbox is not None:
        bb = _find_nested(obj, BOUNDING_BOX)
        if bb >= 0:
            struct.pack_into("<6f", out, bb + 20, *bbox)
    return bytes(out)


# ----------------------------------------------------------------------------- file


@dataclass
class Entry:
    path: str  # as stored (backslashes)
    objects: dict[int, bytes]  # type hash -> object bytes
    list_flag: int = 2  # first field of the type-list object

    @property
    def rid(self) -> int:
        return resource_id(self.path)


@dataclass
class PackMeta:
    registry: list[tuple[int, str]]
    entries: list[Entry] = field(default_factory=list)

    def find(self, path: str) -> Entry | None:
        key = normalize(path)
        for e in self.entries:
            if normalize(e.path) == key:
                return e
        return None

    # ------------------------------------------------------------ parse
    @classmethod
    def parse(cls, data: bytes) -> "PackMeta":
        cnt, one, cnt2, ssz = struct.unpack_from("<4I", data, 0)
        if one != 1 or cnt != cnt2:
            raise ValueError("not a .packmeta file")
        strs = data[16 : 16 + ssz]
        p = 16 + ssz
        name_offs = struct.unpack_from(f"<{cnt}I", data, p)
        p += 4 * cnt
        n2 = struct.unpack_from("<I", data, p)[0]
        p += 4 + 12 * n2  # ids + id->name offsets (recomputed when writing)
        ntypes = struct.unpack_from("<I", data, p)[0]
        p += 4
        registry = []
        for _ in range(ntypes):
            h, ln = struct.unpack_from("<II", data, p)
            p += 8
            registry.append((h, data[p : p + ln].decode("utf-8")))
            p += ln + 4
        index = {h: i for i, (h, _) in enumerate(registry)}
        sections: dict[int, list[bytes]] = {}
        lists = []
        while p < len(data):
            magic, ver, size, th = struct.unpack_from("<IIII", data, p)
            if magic != MAGIC:
                raise ValueError(f"bad object at {p:#x}")
            obj = data[p : p + size]
            if th == TYPE_LIST:
                lists.append(obj)
            elif th in index:
                sections.setdefault(index[th], []).append(obj)
            else:
                raise ValueError(f"unknown object type {th:#x} at {p:#x}")
            p += size
        if len(lists) != cnt:
            raise ValueError("type lists do not match the file count")
        name_at = {o: strs[o : strs.index(b"\0", o)].decode("utf-8") for o in name_offs}
        entries = []
        for i, lst in enumerate(lists):
            flag, noff, k = struct.unpack_from("<III", lst, 16)
            objs = {}
            for j in range(k):
                ref = struct.unpack_from("<Q", lst, 28 + 32 * j + 20)[0]
                t, pos = ref & 0xFFFFFFFF, ref >> 32
                objs[registry[t][0]] = sections[t][pos]
            entries.append(Entry(name_at[noff], objs, flag))
            if noff != name_offs[i]:
                raise ValueError("type lists are not in file order")
        return cls(registry, entries)

    # ------------------------------------------------------------ write
    def to_bytes(self) -> bytes:
        index = {h: i for i, (h, _) in enumerate(self.registry)}
        names = bytearray()
        name_offs = []
        for e in self.entries:
            name_offs.append(len(names))
            names += e.path.encode("utf-8") + b"\0"
        cnt = len(self.entries)
        out = bytearray(struct.pack("<4I", cnt, 1, cnt, len(names)))
        out += names
        out += struct.pack(f"<{cnt}I", *name_offs)
        ids = sorted((e.rid, off) for e, off in zip(self.entries, name_offs))
        out += struct.pack("<I", cnt)
        out += struct.pack(f"<{cnt}Q", *(r for r, _ in ids))
        out += struct.pack(f"<{cnt}I", *(o for _, o in ids))
        out += struct.pack("<I", len(self.registry))
        for h, name in self.registry:
            raw = name.encode("utf-8")
            count = sum(1 for e in self.entries if h in e.objects)  # objects in this type's section
            out += struct.pack("<II", h, len(raw)) + raw + struct.pack("<I", count)
        refs: list[list[int]] = [[] for _ in self.entries]
        for t, (h, _) in enumerate(self.registry):
            pos = 0
            for i, e in enumerate(self.entries):
                if h in e.objects:
                    out += e.objects[h]
                    refs[i].append(pos << 32 | t)
                    pos += 1
        for e, off, rs in zip(self.entries, name_offs, refs):
            for th in e.objects:
                if th not in index:
                    raise ValueError(f"object type {th:#x} is not registered")
            body = struct.pack("<III", e.list_flag, off, len(rs))
            body += b"".join(make_object(TYPE_REF, struct.pack("<IQ", 1, r)) for r in sorted(rs, key=lambda r: r & 0xFFFFFFFF))
            out += make_object(TYPE_LIST, body)
        return bytes(out)
