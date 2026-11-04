"""Control ``.tex`` textures.

A ``.tex`` file is a plain DirectDraw Surface (DDS). Characters use BC1 (colour),
BC3 (colour + alpha), BC4 (single channel masks), BC5 (normal maps, RG) and BC7.
This module converts between ``.tex`` and RGBA images and re-encodes edited images
with the *same* format and mip count as the texture they replace.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

DDS_MAGIC = b"DDS "
DDSD_MIPMAPCOUNT = 0x20000
DDSD_LINEARSIZE = 0x80000

# canonical format names -> (block bytes, DX10 DXGI format)
BC_FORMATS = {
    "BC1": (8, 71),
    "BC1_SRGB": (8, 72),
    "BC3": (16, 77),
    "BC3_SRGB": (16, 78),
    "BC4": (8, 80),
    "BC5": (16, 83),
    "BC7": (16, 98),
    "BC7_SRGB": (16, 99),
}
_FOURCC = {b"DXT1": "BC1", b"DXT5": "BC3", b"ATI1": "BC4", b"BC4U": "BC4", b"ATI2": "BC5", b"BC5U": "BC5"}
_DXGI = {v[1]: k for k, v in BC_FORMATS.items()}
_DXGI_RGBA = {28: "RGBA8", 29: "RGBA8_SRGB", 87: "BGRA8", 91: "BGRA8_SRGB"}


@dataclass
class DDSInfo:
    width: int
    height: int
    mips: int
    format: str  # canonical name, or "UNKNOWN:<code>"
    dx10: bool
    header: bytes  # raw header incl. magic (and DX10 extension)

    @property
    def editable(self) -> bool:
        return self.format in BC_FORMATS or self.format.startswith(("RGBA8", "BGRA8"))

    @property
    def is_normal_map(self) -> bool:
        return self.format == "BC5"


def read_info(data: bytes) -> DDSInfo:
    if data[:4] != DDS_MAGIC:
        raise ValueError("not a DDS file")
    height, width = struct.unpack_from("<II", data, 12)
    mips = max(1, struct.unpack_from("<I", data, 28)[0])
    fourcc = data[84:88]
    dx10 = fourcc == b"DX10"
    if dx10:
        code = struct.unpack_from("<I", data, 128)[0]
        fmt = _DXGI.get(code) or _DXGI_RGBA.get(code) or f"UNKNOWN:{code}"
        hsize = 148
    else:
        flags = struct.unpack_from("<I", data, 80)[0]
        if fourcc in _FOURCC:
            fmt = _FOURCC[fourcc]
        elif flags & 0x40 and struct.unpack_from("<I", data, 88)[0] == 32:
            fmt = "BGRA8"
        else:
            fmt = f"UNKNOWN:{fourcc!r}"
        hsize = 128
    return DDSInfo(width, height, mips, fmt, dx10, bytes(data[:hsize]))


def _block_bytes(fmt: str) -> int:
    return BC_FORMATS[fmt][0]


def _level_size(fmt: str, w: int, h: int) -> int:
    if fmt in BC_FORMATS:
        return max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * _block_bytes(fmt)
    return w * h * 4


def decode(data: bytes, mip: int = 0) -> np.ndarray:
    """Decode one mip level to an RGBA uint8 array of shape (h, w, 4)."""
    import texture2ddecoder as t2d

    info = read_info(data)
    if not info.editable:
        raise ValueError(f"cannot decode texture format {info.format}")
    off = len(info.header)
    w, h = info.width, info.height
    for _ in range(mip):
        off += _level_size(info.format, w, h)
        w, h = max(1, w // 2), max(1, h // 2)
    size = _level_size(info.format, w, h)
    level = data[off : off + size]
    base = info.format.split("_")[0]
    if base in ("RGBA8", "BGRA8"):
        px = np.frombuffer(level, np.uint8).reshape(h, w, 4)
        return px[..., [2, 1, 0, 3]].copy() if base == "BGRA8" else px.copy()
    pw, ph = max(4, (w + 3) // 4 * 4), max(4, (h + 3) // 4 * 4)
    bgra = np.frombuffer(getattr(t2d, "decode_" + base.lower())(level, pw, ph), np.uint8).reshape(ph, pw, 4)
    rgba = bgra[:h, :w][..., [2, 1, 0, 3]].copy()
    if base == "BC4":
        rgba[..., 1] = rgba[..., 2] = rgba[..., 0]
        rgba[..., 3] = 255
    elif base == "BC5":
        x = rgba[..., 0].astype(np.float32) / 127.5 - 1
        y = rgba[..., 1].astype(np.float32) / 127.5 - 1
        z = np.sqrt(np.clip(1 - x * x - y * y, 0, 1))
        rgba[..., 2] = np.clip(np.round((z + 1) * 127.5), 0, 255).astype(np.uint8)
        rgba[..., 3] = 255
    return rgba


def _mip_chain(rgba: np.ndarray, count: int) -> list[np.ndarray]:
    from PIL import Image

    levels = [rgba]
    img = Image.fromarray(rgba, "RGBA")
    w, h = img.size
    for _ in range(count - 1):
        w, h = max(1, w // 2), max(1, h // 2)
        levels.append(np.asarray(img.resize((w, h), Image.Resampling.BOX)))
    return levels


def _compress(fmt: str, rgba: np.ndarray) -> bytes:
    import etcpak

    base = fmt.split("_")[0]
    h, w = rgba.shape[:2]
    if base in ("RGBA8", "BGRA8"):
        return (rgba[..., [2, 1, 0, 3]] if base == "BGRA8" else rgba).tobytes()
    pw, ph = max(4, (w + 3) // 4 * 4), max(4, (h + 3) // 4 * 4)
    if (pw, ph) != (w, h):
        padded = np.zeros((ph, pw, 4), np.uint8)
        padded[:h, :w] = rgba
        padded[h:, :w] = rgba[-1:, :]
        padded[:, w:] = padded[:, w - 1 : w]
        rgba = padded
    return getattr(etcpak, "compress_" + base.lower())(np.ascontiguousarray(rgba).tobytes(), pw, ph)


def encode(rgba: np.ndarray, like: DDSInfo | bytes, mips: int | None = None) -> bytes:
    """Encode an RGBA image using the format/header style of ``like``.

    The mip count follows the template: a template with a full chain gets a full chain
    for the new size, otherwise the same number of levels is written.
    """
    info = read_info(like) if isinstance(like, (bytes, bytearray)) else like
    if not info.editable:
        raise ValueError(f"cannot encode texture format {info.format}")
    rgba = np.ascontiguousarray(rgba, dtype=np.uint8)
    if rgba.ndim == 2:
        rgba = np.stack([rgba, rgba, rgba, np.full_like(rgba, 255)], axis=-1)
    if rgba.shape[2] == 3:
        rgba = np.concatenate([rgba, np.full(rgba.shape[:2] + (1,), 255, np.uint8)], axis=2)
    h, w = rgba.shape[:2]
    full = int(np.log2(max(info.width, info.height))) + 1
    if mips is None:
        mips = int(np.log2(max(w, h))) + 1 if info.mips >= full else min(info.mips, int(np.log2(max(w, h))) + 1)
    payload = b"".join(_compress(info.format, lvl) for lvl in _mip_chain(rgba, mips))

    hdr = bytearray(info.header)
    struct.pack_into("<II", hdr, 12, h, w)
    flags = struct.unpack_from("<I", hdr, 8)[0]
    if mips > 1:
        flags |= DDSD_MIPMAPCOUNT
    struct.pack_into("<I", hdr, 8, flags)
    # keep the template's pitch convention (the game's tool writes w*h for BC1/BC4)
    old_pitch = struct.unpack_from("<I", hdr, 20)[0]
    old_level = _level_size(info.format, info.width, info.height) if info.format in BC_FORMATS else info.width * 4
    new_level = _level_size(info.format, w, h) if info.format in BC_FORMATS else w * 4
    struct.pack_into("<I", hdr, 20, old_pitch * new_level // old_level if old_level else new_level)
    struct.pack_into("<I", hdr, 28, mips)
    caps = struct.unpack_from("<I", hdr, 108)[0]
    caps = caps | 0x400008 | 0x1000 if mips > 1 else caps | 0x1000
    struct.pack_into("<I", hdr, 108, caps)
    return bytes(hdr) + payload


def to_png(tex: bytes, png_path: str | Path) -> DDSInfo:
    from PIL import Image

    info = read_info(tex)
    Image.fromarray(decode(tex), "RGBA").save(png_path)
    return info


def from_image(image_path: str | Path, like: bytes) -> bytes:
    from PIL import Image

    with Image.open(image_path) as im:
        rgba = np.asarray(im.convert("RGBA"))
    return encode(rgba, like)
