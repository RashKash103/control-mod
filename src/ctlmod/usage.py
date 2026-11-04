"""Which models use which textures, game-wide - so modders see side effects.

Textures are shared: every outfit uses the same face and hand textures, many clothes
share fabric detail maps, and ``default*.tex`` is used by thousands of models. Editing
such a texture *in place* changes every model that references it. This module scans
all ``.binfbx`` files once (about 15 s) for the texture paths in their materials and
caches the result, keyed by the archives' build stamps so a game update rebuilds it.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Callable

from .archive import normalize
from .gamedata import GameData

CACHE_VERSION = 1
_TEX_RE = re.compile(re.escape(b"runtimedata\\data_pc\\") + rb"[^\x00]{1,240}?\.tex", re.I)


def _texture_key(game_path: str) -> str:
    """``runtimedata\\data_pc\\x.tex`` -> ``data_pc/x.tex`` (lower case)."""
    p = normalize(game_path)
    return p[len("runtimedata/") :] if p.startswith("runtimedata/") else p


class TextureUsage:
    """``texture archive path -> sorted list of model archive paths`` (all lower case)."""

    def __init__(self, users: dict[str, list[str]]):
        self.users = users

    def models_using(self, texture: str) -> list[str]:
        return self.users.get(_texture_key(texture), [])

    def others(self, texture: str, model: str | set[str]) -> list[str]:
        """Models other than ``model`` that use ``texture``."""
        mine = {normalize(model)} if isinstance(model, str) else {normalize(m) for m in model}
        return [m for m in self.models_using(texture) if m not in mine]

    def search(self, pattern: str) -> dict[str, list[str]]:
        import fnmatch

        pat = pattern.lower().replace("\\", "/")
        if not any(c in pat for c in "*?["):
            pat = f"*{pat}*"
        return {t: m for t, m in sorted(self.users.items()) if fnmatch.fnmatchcase(t, pat)}

    # ------------------------------------------------------------ building / caching
    @classmethod
    def scan(cls, gd: GameData, progress: Callable[[str], None] | None = None) -> "TextureUsage":
        users: dict[str, set[str]] = defaultdict(set)
        for a in gd.archives:
            entries = sorted((e for e in a.entries if e.name.lower().endswith(".binfbx")), key=lambda e: e.offset)
            if not entries:
                continue
            if progress:
                progress(f"  scanning {len(entries)} models in {a.bin_path.stem} ...")
            with open(a.rmdp_path, "rb") as f:
                for e in entries:
                    f.seek(e.offset)
                    for m in set(_TEX_RE.findall(f.read(e.size))):
                        users[_texture_key(m.decode("latin-1"))].add(normalize(e.path))
        # later patches override files; the index is "who references what" so a union is right
        return cls({k: sorted(v) for k, v in users.items()})

    @staticmethod
    def _stamp(gd: GameData) -> list[str]:
        return [f"{a.bin_path.stem}:{a.build_tag}:{a.bin_path.stat().st_size}" for a in gd.archives]

    @classmethod
    def load(cls, gd: GameData, cache_dir: Path, progress: Callable[[str], None] | None = print) -> "TextureUsage":
        cache = cache_dir / "texture_usage.json"
        stamp = cls._stamp(gd)
        if cache.exists():
            try:
                d = json.loads(cache.read_text())
                if d.get("version") == CACHE_VERSION and d.get("stamp") == stamp:
                    return cls(d["users"])
            except (ValueError, KeyError):
                pass
        if progress:
            progress("indexing which models use which textures (one-time, ~15 s) ...")
        usage = cls.scan(gd, progress)
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"version": CACHE_VERSION, "stamp": stamp, "users": usage.users}))
        return usage


def describe(models: list[str], limit: int = 6) -> str:
    """Short human list of model names: ``a, b, c and 12 more``."""
    names = [Path(m).stem for m in models]
    if len(names) <= limit:
        return ", ".join(names)
    return f"{', '.join(names[:limit])} and {len(names) - limit} more"
