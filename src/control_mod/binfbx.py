"""Parser/writer for Control ``.binfbx`` mesh files (version 46).

The format was reverse engineered from the retail files; fields whose meaning is not
known are preserved verbatim so that ``parse(data).to_bytes() == data`` for every
model in the game (see ``tests/test_game_roundtrip.py``).

File layout (little endian)::

    u32 version (46)
    u32 secondary_size        # "stream 1": normals / tangents / uvs / colours
    u32 primary_size          # "stream 0": positions / skinning
    u32 index_count
    u32 index_size            # 2 or 4
    u8  secondary[secondary_size]
    u8  primary[primary_size]
    u8  indices[index_count * index_size]
    u32 bone_count
    Bone[bone_count]          # str name, f32[12] inverse bind 3x4, f32[4] sphere, i32 parent
    u32 unk_a, u32 unk_b, f32 unk_c
    u32 n, f32[n] lod_thresholds?, f32 unk_d
    f32[4] bounding sphere, f32[6] bounding box
    u32 lod_count
    u32 material_count, Material[material_count]
    u32 n, u32[n] mesh_material      # material index used by each mesh
    u32 variant_count, Variant[...]  # str name, u32[n] alternate mesh_material table
    u32 n, u32[n] shadow_material    # material index used by each shadow mesh
    u32 mesh_count, Mesh[mesh_count]
    u32 shadow_count, Mesh[shadow_count]
    u8  trailer[...]                 # kept verbatim

Strings are ``u32 length`` + bytes (usually including a trailing NUL).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any

VERSION = 46


class Reader:
    def __init__(self, data: bytes, pos: int = 0):
        self.data = data
        self.pos = pos

    def unpack(self, fmt: str) -> tuple:
        fmt = "<" + fmt
        v = struct.unpack_from(fmt, self.data, self.pos)
        self.pos += struct.calcsize(fmt)
        return v

    def one(self, fmt: str) -> Any:
        return self.unpack(fmt)[0]

    def u32(self) -> int:
        return self.one("I")

    def bytes(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise EOFError(f"read past end at {self.pos:#x}")
        b = self.data[self.pos : self.pos + n]
        self.pos += n
        return b

    def string(self) -> "NLString":
        n = self.u32()
        if n > 1 << 20:
            raise ValueError(f"implausible string length {n} at {self.pos - 4:#x}")
        return NLString.from_raw(self.bytes(n))


class Writer:
    def __init__(self) -> None:
        self.parts: list[bytes] = []

    def pack(self, fmt: str, *v) -> None:
        self.parts.append(struct.pack("<" + fmt, *v))

    def u32(self, v: int) -> None:
        self.pack("I", v)

    def raw(self, b: bytes) -> None:
        self.parts.append(bytes(b))

    def string(self, s: "NLString | str") -> None:
        if isinstance(s, str):
            s = NLString(s)
        raw = s.raw()
        self.u32(len(raw))
        self.raw(raw)

    def getvalue(self) -> bytes:
        return b"".join(self.parts)


@dataclass
class NLString:
    """A length-prefixed string that remembers whether it carried a trailing NUL."""

    text: str
    nul: bool = True

    @classmethod
    def from_raw(cls, raw: bytes) -> "NLString":
        nul = raw.endswith(b"\0")
        return cls(raw[:-1].decode("latin-1") if nul else raw.decode("latin-1"), nul)

    def raw(self) -> bytes:
        return self.text.encode("latin-1") + (b"\0" if self.nul else b"")

    def __str__(self) -> str:
        return self.text

    def __eq__(self, other) -> bool:
        if isinstance(other, str):
            return self.text == other
        return isinstance(other, NLString) and (self.text, self.nul) == (other.text, other.nul)

    def __hash__(self) -> int:
        return hash(self.text)


# ----------------------------------------------------------------------------- bones


@dataclass
class Bone:
    name: NLString
    inv_bind: tuple[float, ...]  # 12 floats: 3x3 rotation (row major) + translation
    sphere: tuple[float, float, float, float]
    parent: int


# ------------------------------------------------------------------------- materials

# Material parameter value types
P_FLOAT, P_VEC2, P_VEC3, P_VEC4 = 0, 1, 2, 3
P_TEXTURE_A, P_SAMPLER, P_TEXTURE = 7, 8, 9
P_UINT = 12
P_INT = 16
_PARAM_FMT = {P_FLOAT: "f", P_VEC2: "2f", P_VEC3: "3f", P_VEC4: "4f", P_UINT: "I", P_INT: "i"}


@dataclass
class MaterialParam:
    name: NLString
    type: int
    value: Any  # tuple of numbers, NLString for textures, None for samplers

    @property
    def is_texture(self) -> bool:
        return self.type in (P_TEXTURE, P_TEXTURE_A)


@dataclass
class Material:
    version: int
    hash: int
    name: NLString
    shader: NLString
    source: NLString
    flags: tuple[int, ...]  # 6 x u32 (render flags, blend mode, cull mode, ...)
    params: list[MaterialParam]

    def textures(self) -> dict[str, str]:
        return {str(p.name): str(p.value) for p in self.params if p.is_texture}

    def param(self, name: str) -> MaterialParam | None:
        for p in self.params:
            if p.name == name:
                return p
        return None


# ----------------------------------------------------------------------------- meshes

# Vertex attribute usage codes
U_POSITION = 2
U_NORMAL = 4
U_BONE_WEIGHT = 5
U_TEXCOORD = 7
U_TANGENT = 8
U_BONE_INDEX = 13


@dataclass
class VertexAttr:
    stream: int  # 0 = primary buffer, 1 = secondary buffer
    usage: int
    format: int
    pad: int = 0


@dataclass
class Mesh:
    lod: int
    vertex_count: int
    face_count: int
    secondary_offset: int  # byte offset into secondary buffer
    primary_offset: int  # byte offset into primary buffer
    index_offset: int  # in indices (not bytes)
    unk0: int
    sphere: tuple[float, ...]
    bbox: tuple[float, ...]
    unk1: int
    attrs: list[VertexAttr]
    bone_count: int  # number of bones in the file the mesh is skinned against (0 = static)
    unk2: float
    unk3: int
    unk4: float


# ----------------------------------------------------------------------------- file


@dataclass
class BinFbx:
    version: int
    index_size: int
    secondary: bytes
    primary: bytes
    indices: bytes
    bones: list[Bone]
    unk_a: int
    unk_b: int
    unk_c: float
    lod_thresholds: tuple[float, ...]
    unk_d: float
    sphere: tuple[float, ...]
    bbox: tuple[float, ...]
    lod_count: int
    materials: list[Material]
    mesh_material: list[int]
    variants: list[tuple[NLString, list[int]]]
    shadow_material: list[int]
    meshes: list[Mesh]
    shadow_meshes: list[Mesh]
    trailer: bytes = field(default=b"")

    # ---------------------------------------------------------------- parse
    @classmethod
    def parse(cls, data: bytes) -> "BinFbx":
        r = Reader(data)
        version, ssize, psize, icount, isize = r.unpack("5I")
        if version != VERSION:
            raise ValueError(f"unsupported binfbx version {version} (expected {VERSION})")
        if isize not in (2, 4):
            raise ValueError(f"bad index size {isize}")
        secondary = r.bytes(ssize)
        primary = r.bytes(psize)
        indices = r.bytes(icount * isize)

        bones = []
        for _ in range(r.u32()):
            name = r.string()
            vals = r.unpack("16fi")
            bones.append(Bone(name, vals[:12], vals[12:16], vals[16]))

        unk_a, unk_b, unk_c = r.unpack("IIf")
        n = r.u32()
        lod_thresholds = r.unpack(f"{n}f") if n else ()
        unk_d = r.one("f")
        sphere = r.unpack("4f")
        bbox = r.unpack("6f")
        lod_count = r.u32()

        materials = [cls._read_material(r) for _ in range(r.u32())]
        mesh_material = list(r.unpack(f"{r.u32()}I"))
        variants = []
        for _ in range(r.u32()):
            vname = r.string()
            variants.append((vname, list(r.unpack(f"{len(mesh_material)}I"))))
        shadow_material = list(r.unpack(f"{r.u32()}I"))
        meshes = [cls._read_mesh(r) for _ in range(r.u32())]
        shadow_meshes = [cls._read_mesh(r) for _ in range(r.u32())]
        trailer = data[r.pos :]
        return cls(
            version, isize, secondary, primary, indices, bones, unk_a, unk_b, unk_c,
            lod_thresholds, unk_d, sphere, bbox, lod_count, materials, mesh_material,
            variants, shadow_material, meshes, shadow_meshes, trailer,
        )  # fmt: skip

    @staticmethod
    def _read_material(r: Reader) -> Material:
        version = r.u32()
        h = r.one("Q")
        name, shader, source = r.string(), r.string(), r.string()
        flags = r.unpack("6I")
        params = []
        for _ in range(r.u32()):
            pname = r.string()
            t = r.u32()
            if t in _PARAM_FMT:
                val: Any = r.unpack(_PARAM_FMT[t])
            elif t in (P_TEXTURE, P_TEXTURE_A):
                val = r.string()
            elif t == P_SAMPLER:
                val = None
            else:
                raise ValueError(f"unknown material param type {t} for {pname} at {r.pos:#x}")
            params.append(MaterialParam(pname, t, val))
        return Material(version, h, name, shader, source, flags, params)

    @staticmethod
    def _read_mesh(r: Reader) -> Mesh:
        lod, vcount, fcount, soff, poff, ioff, unk0 = r.unpack("7I")
        sphere = r.unpack("4f")
        bbox = r.unpack("6f")
        unk1 = r.u32()
        attrs = [VertexAttr(*r.unpack("4B")) for _ in range(r.one("B"))]
        bone_count, unk2, unk3, unk4 = r.unpack("IfBf")
        return Mesh(lod, vcount, fcount, soff, poff, ioff, unk0, sphere, bbox, unk1, attrs, bone_count, unk2, unk3, unk4)

    # ---------------------------------------------------------------- write
    def to_bytes(self) -> bytes:
        w = Writer()
        isize = self.index_size
        w.pack("5I", self.version, len(self.secondary), len(self.primary), len(self.indices) // isize, isize)
        w.raw(self.secondary)
        w.raw(self.primary)
        w.raw(self.indices)
        w.u32(len(self.bones))
        for b in self.bones:
            w.string(b.name)
            w.pack("16fi", *b.inv_bind, *b.sphere, b.parent)
        w.pack("IIf", self.unk_a, self.unk_b, self.unk_c)
        w.u32(len(self.lod_thresholds))
        w.pack(f"{len(self.lod_thresholds)}f", *self.lod_thresholds)
        w.pack("f", self.unk_d)
        w.pack("4f", *self.sphere)
        w.pack("6f", *self.bbox)
        w.u32(self.lod_count)
        w.u32(len(self.materials))
        for m in self.materials:
            w.u32(m.version)
            w.pack("Q", m.hash)
            w.string(m.name)
            w.string(m.shader)
            w.string(m.source)
            w.pack("6I", *m.flags)
            w.u32(len(m.params))
            for p in m.params:
                w.string(p.name)
                w.u32(p.type)
                if p.type in _PARAM_FMT:
                    w.pack(_PARAM_FMT[p.type], *p.value)
                elif p.type in (P_TEXTURE, P_TEXTURE_A):
                    w.string(p.value)
        w.u32(len(self.mesh_material))
        w.pack(f"{len(self.mesh_material)}I", *self.mesh_material)
        w.u32(len(self.variants))
        for vname, table in self.variants:
            w.string(vname)
            w.pack(f"{len(table)}I", *table)
        w.u32(len(self.shadow_material))
        w.pack(f"{len(self.shadow_material)}I", *self.shadow_material)
        for lst in (self.meshes, self.shadow_meshes):
            w.u32(len(lst))
            for m in lst:
                w.pack("7I", m.lod, m.vertex_count, m.face_count, m.secondary_offset, m.primary_offset, m.index_offset, m.unk0)
                w.pack("4f", *m.sphere)
                w.pack("6f", *m.bbox)
                w.u32(m.unk1)
                w.pack("B", len(m.attrs))
                for a in m.attrs:
                    w.pack("4B", a.stream, a.usage, a.format, a.pad)
                w.pack("IfBf", m.bone_count, m.unk2, m.unk3, m.unk4)
        w.raw(self.trailer)
        return w.getvalue()
