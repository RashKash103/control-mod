"""Access to the game's packfiles as one virtual file system."""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from .archive import MARKER_OFFSET, Archive, FileEntry, normalize

CONTROL_MOD_MARKER = b"ctrl-mod"  # stored in the unused part of the header of archives we create
LEGACY_MARKERS = (b"ctlmod\0\0",)  # written by earlier versions (when the tool was called ctlmod)
_NAME_RE = re.compile(r"^(?P<ep>[a-z]+\d+)-(?P<patch>\d{3})-(?P<rest>.+)$", re.I)

DEFAULT_GAME_DIRS = [
    r"C:\Program Files (x86)\Steam\steamapps\common\Control",
    r"C:\Program Files\Epic Games\Control",
    r"C:\Program Files (x86)\GOG Galaxy\Games\Control",
]


def split_archive_name(stem: str) -> tuple[str, int, str] | None:
    """``ep100-000-generic-en`` -> ``("ep100", 0, "generic-en")``."""
    m = _NAME_RE.match(stem)
    if not m:
        return None
    return m["ep"], int(m["patch"]), m["rest"]


def patch_name(base_stem: str, number: int) -> str:
    ep, _, rest = split_archive_name(base_stem)
    return f"{ep}-{number:03d}-{rest}"


def is_control_mod_archive(bin_path: Path) -> bool:
    try:
        with open(bin_path, "rb") as f:
            head = f.read(153)
        # current location, and the header prefix used by the first version (still uninstallable)
        markers = (CONTROL_MOD_MARKER, *LEGACY_MARKERS)
        return head[25 + MARKER_OFFSET : 33 + MARKER_OFFSET] in markers or head[25:33] in markers
    except OSError:
        return False


@dataclass
class Located:
    archive: Archive
    entry: FileEntry

    @property
    def archive_name(self) -> str:
        return self.archive.bin_path.stem


class GameData:
    def __init__(self, game_dir: str | os.PathLike):
        self.game_dir = Path(game_dir)
        self.pack_dir = self.game_dir / "data_packfiles"
        if not self.pack_dir.is_dir():
            raise FileNotFoundError(f"{self.pack_dir} not found - is the game directory right?")

    @cached_property
    def archives(self) -> list[Archive]:
        """Original (non-control-mod) archives, lowest patch number first."""
        out = []
        for b in sorted(self.pack_dir.glob("*.bin")):
            if not b.with_suffix(".rmdp").exists() or is_control_mod_archive(b):
                continue
            if split_archive_name(b.stem) is None:
                continue
            out.append(Archive.open(b))
        out.sort(key=lambda a: (split_archive_name(a.bin_path.stem)[1], a.bin_path.stem))
        return out

    @cached_property
    def index(self) -> dict[str, Located]:
        """Path -> location; higher patch numbers override lower ones."""
        idx: dict[str, Located] = {}
        for a in self.archives:
            for key, e in a.files.items():
                idx[key] = Located(a, e)
        return idx

    def __contains__(self, path: str) -> bool:
        return normalize(path) in self.index

    def locate(self, path: str) -> Located:
        try:
            return self.index[normalize(path)]
        except KeyError:
            raise FileNotFoundError(f"{path} is not in any game archive") from None

    def read(self, path: str) -> bytes:
        loc = self.locate(path)
        return loc.archive.read(loc.entry)

    def try_read(self, path: str) -> bytes | None:
        try:
            return self.read(path)
        except FileNotFoundError:
            return None

    def glob(self, pattern: str) -> list[Located]:
        pat = normalize(pattern)
        return [loc for p, loc in sorted(self.index.items()) if fnmatch.fnmatchcase(p, pat)]

    def models(self, pattern: str = "*") -> list[Located]:
        """Every ``.binfbx`` whose path or file name matches ``pattern`` (glob, case-insensitive)."""
        pat = pattern.lower()
        if not any(ch in pat for ch in "*?["):
            pat = f"*{pat}*"
        out = []
        for p, loc in sorted(self.index.items()):
            name = p.rsplit("/", 1)[-1]
            if p.endswith(".binfbx") and any(fnmatch.fnmatchcase(c, pat) for c in (p, name, name[: -len(".binfbx")])):
                out.append(loc)
        return out


def find_game_dir() -> Path | None:
    env = os.environ.get("CONTROL_MOD_GAME_DIR")
    for c in ([env] if env else []) + DEFAULT_GAME_DIRS:
        if c and (Path(c) / "data_packfiles").is_dir():
            return Path(c)
    return None
