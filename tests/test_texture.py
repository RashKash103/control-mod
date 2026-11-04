import struct

import numpy as np
import pytest

from ctlmod import texture


def make_dds(fmt: str, w: int, h: int, mips: int) -> bytes:
    """Minimal template header in the style of the game's files."""
    hdr = bytearray(128)
    hdr[0:4] = b"DDS "
    struct.pack_into("<IIIIIII", hdr, 4, 124, 0x0A1007, h, w, w * h, 0, mips)
    struct.pack_into("<II", hdr, 76, 32, 4)
    legacy = {"BC1": b"DXT1", "BC3": b"DXT5", "BC5": b"ATI2"}
    if fmt in legacy:
        hdr[84:88] = legacy[fmt]
    else:
        hdr[84:88] = b"DX10"
        hdr += struct.pack("<IIIII", texture.BC_FORMATS[fmt][1], 3, 0, 1, 0)
    struct.pack_into("<I", hdr, 108, 0x401008)
    blank = np.zeros((h, w, 4), np.uint8)
    return texture.encode(blank, texture.DDSInfo(w, h, mips, fmt, fmt not in legacy, bytes(hdr)))


def gradient(w=64, h=32):
    y, x = np.mgrid[0:h, 0:w]
    img = np.zeros((h, w, 4), np.uint8)
    img[..., 0] = x * 255 // (w - 1)
    img[..., 1] = y * 255 // (h - 1)
    img[..., 2] = 128
    img[..., 3] = 255
    return img


@pytest.mark.parametrize("fmt", ["BC1", "BC3", "BC4", "BC5", "BC7"])
def test_encode_decode(fmt):
    tmpl = make_dds(fmt, 16, 16, 5)
    info = texture.read_info(tmpl)
    assert info.format == fmt and info.mips == 5
    img = gradient()
    data = texture.encode(img, tmpl)
    out = texture.read_info(data)
    assert (out.width, out.height, out.format) == (64, 32, fmt)
    assert out.mips == 7  # template had a full chain -> full chain for the new size
    dec = texture.decode(data)
    assert dec.shape == (32, 64, 4)
    channels = {"BC4": [0], "BC5": [0, 1]}.get(fmt, [0, 1, 2])
    err = np.abs(dec[..., channels].astype(int) - img[..., channels].astype(int)).mean()
    assert err < 6, err
    # the payload size matches the mip chain exactly
    expected = len(out.header) + sum(texture._level_size(fmt, max(1, 64 >> i), max(1, 32 >> i)) for i in range(7))
    assert len(data) == expected


def test_non_multiple_of_four_and_small_mips():
    tmpl = make_dds("BC1", 8, 8, 4)
    img = gradient(10, 6)
    data = texture.encode(img, tmpl)
    assert texture.decode(data).shape == (6, 10, 4)
    assert texture.decode(data, mip=3).shape == (1, 1, 4)


def test_partial_mip_chain_preserved():
    tmpl = make_dds("BC1", 64, 64, 1)
    assert texture.read_info(texture.encode(gradient(), tmpl)).mips == 1


def test_png_roundtrip(tmp_path):
    tmpl = make_dds("BC7", 16, 16, 5)
    data = texture.encode(gradient(), tmpl)
    info = texture.to_png(data, tmp_path / "a.png")
    back = texture.from_image(tmp_path / "a.png", data)
    assert texture.read_info(back).format == info.format == "BC7"


def test_not_dds():
    with pytest.raises(ValueError):
        texture.read_info(b"nope" * 40)
