"""Mod projects: a folder per mod holding editable models and the built game files.

::

    mods/<id>/
        mod.toml                    name, author, description, models and their options
        models/<model>/             model package (see ctlmod.package) + <model>.blend
        build/files/<archive path>  game files produced by ``ctlmod build``
        build/manifest.json         what the build contains and which archive each file overrides
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import package
from .config import Config, write_toml
from .gamedata import GameData

BLENDER_SCRIPT = Path(__file__).parent / "blender" / "ctl_blender.py"
LOD_MODES = ("authored", "copy", "decimate")
_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def run_blender(cfg: Config, args: list[str], blend: Path | None = None, log: Callable[[str], None] = print) -> None:
    exe = cfg.require_blender()
    cmd = [str(exe), "--background"]
    cmd += [str(blend)] if blend else ["--factory-startup"]
    cmd += ["--python", str(BLENDER_SCRIPT), "--", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    lines = (proc.stdout + proc.stderr).splitlines()
    for ln in lines:
        if ln.startswith("[ctlmod]") and ("warning" in ln or "note" in ln or "exported" in ln or "saved" in ln or "rendered" in ln):
            log("  blender: " + ln[9:])
    if proc.returncode != 0 or any("Traceback" in ln for ln in lines):
        tail = "\n".join(lines[-40:])
        raise RuntimeError(f"Blender failed (exit {proc.returncode}):\n{tail}")


@dataclass
class ModelEntry:
    name: str
    source: str  # archive path of the original .binfbx
    lods: str = "authored"


@dataclass
class Mod:
    root: Path
    id: str
    name: str = ""
    author: str = ""
    version: str = "1.0.0"
    description: str = ""
    models: dict[str, ModelEntry] = field(default_factory=dict)

    # ------------------------------------------------------------- persistence
    @classmethod
    def create(cls, cfg: Config, mod_id: str, name: str = "", author: str = "", description: str = "") -> "Mod":
        if not _ID_RE.match(mod_id):
            raise SystemExit("mod id may only contain letters, digits, '_', '-' and '.'")
        root = cfg.mods_dir / mod_id
        if (root / "mod.toml").exists():
            raise SystemExit(f"mod '{mod_id}' already exists at {root}")
        root.mkdir(parents=True, exist_ok=True)
        mod = cls(root, mod_id, name or mod_id, author, "1.0.0", description)
        mod.save()
        return mod

    @classmethod
    def load(cls, cfg: Config, mod_id: str) -> "Mod":
        root = cfg.mods_dir / mod_id
        p = root / "mod.toml"
        if not p.exists():
            raise SystemExit(f"no mod named '{mod_id}' (looked for {p})")
        d = tomllib.loads(p.read_text(encoding="utf-8"))
        models = {k: ModelEntry(k, v["source"], v.get("lods", "authored")) for k, v in d.get("models", {}).items()}
        return cls(root, d.get("id", mod_id), d.get("name", mod_id), d.get("author", ""), d.get("version", "1.0.0"), d.get("description", ""), models)

    @staticmethod
    def all_ids(cfg: Config) -> list[str]:
        if not cfg.mods_dir.is_dir():
            return []
        return sorted(p.parent.name for p in cfg.mods_dir.glob("*/mod.toml"))

    def save(self) -> None:
        data = {
            "id": self.id,
            "name": self.name,
            "author": self.author,
            "version": self.version,
            "description": self.description,
            "models": {k: {"source": m.source, "lods": m.lods} for k, m in self.models.items()},
        }
        write_toml(self.root / "mod.toml", data, "# ctlmod mod definition")

    # ------------------------------------------------------------- paths
    def model_dir(self, name: str) -> Path:
        return self.root / "models" / name

    def blend_path(self, name: str) -> Path:
        return self.model_dir(name) / f"{name}.blend"

    @property
    def build_dir(self) -> Path:
        return self.root / "build"

    @property
    def manifest_path(self) -> Path:
        return self.build_dir / "manifest.json"

    def manifest(self) -> dict | None:
        return json.loads(self.manifest_path.read_text()) if self.manifest_path.exists() else None

    def resolve_model(self, name: str | None) -> list[str]:
        if name is None:
            return list(self.models)
        hits = [m for m in self.models if m == name] or [m for m in self.models if name.lower() in m.lower()]
        if not hits:
            raise SystemExit(f"mod '{self.id}' has no model matching '{name}'. Models: {', '.join(self.models) or '(none)'}")
        return hits

    # ------------------------------------------------------------- workflow
    def add_model(self, cfg: Config, gd: GameData, source: str, lods: str = "authored", log=print, overwrite: bool = False) -> str:
        loc = gd.locate(source)
        name = Path(loc.entry.path).stem
        if name in self.models and not overwrite:
            raise SystemExit(f"model {name} is already part of mod '{self.id}' (use --force to re-export and discard edits)")
        if lods not in LOD_MODES:
            raise SystemExit(f"--lods must be one of {LOD_MODES}")
        d = self.model_dir(name)
        if d.exists():
            shutil.rmtree(d)
        log(f"exporting {loc.entry.path} from {loc.archive_name} ...")
        meta = package.export_package(gd.read(loc.entry.path), loc.entry.path, d, gd.try_read, log=log)
        log(f"  {len(meta['meshes'])} meshes, {len(meta['materials'])} materials, {len(meta['textures'])} textures, {len(meta['bones'])} bones")
        log("creating Blender file ...")
        run_blender(cfg, ["import", str(d), str(self.blend_path(name))], log=log)
        self.models[name] = ModelEntry(name, loc.entry.path, lods)
        self.save()
        return name

    def build(self, cfg: Config, gd: GameData, log=print) -> dict:
        if not self.models:
            raise SystemExit(f"mod '{self.id}' has no models. Add one with: ctlmod add {self.id} <model>")
        files_dir = self.build_dir / "files"
        if files_dir.exists():
            shutil.rmtree(files_dir)
        files_dir.mkdir(parents=True)
        entries = []
        reports = {}
        for name, m in self.models.items():
            d = self.model_dir(name)
            blend = self.blend_path(name)
            if not blend.exists():
                raise SystemExit(f"{blend} is missing")
            log(f"[{name}] reading Blender file (LODs: {m.lods}) ...")
            run_blender(cfg, ["export", str(d), "--lods", m.lods], blend=blend, log=log)
            res = package.build_from_blender(d, read_texture=gd.try_read, new_texture_root=f"data_pc/textures/ctlmod/{self.id}", log=log)
            for line in res.report:
                log(f"  {line}")
            reports[name] = res.report
            outputs = {m.source: res.binfbx, **res.textures}
            for apath, data in outputs.items():
                if apath in gd:
                    base = gd.locate(apath).archive_name
                    new = False
                else:
                    # new file: put it next to what it was derived from
                    base = _guess_archive(gd, apath, m.source)
                    new = True
                dest = files_dir / apath
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                entries.append({"path": apath, "archive": base, "new": new, "model": name, "size": len(data)})
        manifest = {"id": self.id, "name": self.name, "version": self.version, "files": entries, "reports": reports}
        self.manifest_path.write_text(json.dumps(manifest, indent=1))
        log(f"built {len(entries)} files into {files_dir}")
        return manifest


def _guess_archive(gd: GameData, apath: str, model_source: str) -> str:
    """Archive a brand new file should live in: same episode as the model, pc/generic by type."""
    from .gamedata import split_archive_name

    ep = split_archive_name(gd.locate(model_source).archive_name)[0]
    want = "pc" if apath.lower().startswith("data_pc/") else "generic"
    for a in gd.archives:
        e, _, rest = split_archive_name(a.bin_path.stem)
        if e == ep and rest == want:
            return a.bin_path.stem
    return f"{ep}-000-{want}"
