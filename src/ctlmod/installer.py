"""Mod manager: compose enabled mods into patch archives inside the game folder.

Control loads every ``*.rmdp`` in ``data_packfiles``; an archive named
``ep100-001-generic`` overrides files of ``ep100-000-generic``. Instead of shipping one
patch per mod (which would collide when two mods touch the same archive), ctlmod merges
all *enabled* mods - in load order, later mods winning - into a single patch per
original archive. Re-running ``install`` after enabling/disabling mods rebuilds them.

Archives written by ctlmod carry a marker in their header, so ``uninstall`` only ever
deletes files ctlmod created and original game files are never modified.
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .archive import write_archive
from .config import Config, State
from .gamedata import CTLMOD_MARKER, GameData, is_ctlmod_archive, patch_name, split_archive_name
from .project import Mod


@dataclass
class Plan:
    files: dict[str, tuple[str, Path, str]] = field(default_factory=dict)  # lower path -> (path, source file, mod id)
    archives: dict[str, list[str]] = field(default_factory=dict)  # patch stem -> [lower paths]
    base_of: dict[str, str] = field(default_factory=dict)  # patch stem -> base stem
    conflicts: list[tuple[str, str, str]] = field(default_factory=list)  # (path, loser, winner)
    skipped: list[str] = field(default_factory=list)  # enabled mods without a build


def installed_archives(pack_dir: Path) -> list[Path]:
    return sorted(b for b in pack_dir.glob("*.bin") if is_ctlmod_archive(b))


def make_plan(cfg: Config, gd: GameData, state: State) -> Plan:
    plan = Plan()
    used_names = {b.stem.lower() for b in gd.pack_dir.glob("*.bin") if not is_ctlmod_archive(b)}
    for mod_id in state.order:
        if not state.enabled.get(mod_id, True):
            continue
        mod = Mod.load(cfg, mod_id)
        man = mod.manifest()
        if not man:
            plan.skipped.append(mod_id)
            continue
        for f in man["files"]:
            key = f["path"].lower()
            src = mod.build_dir / "files" / f["path"]
            if key in plan.files and plan.files[key][2] != mod_id:
                plan.conflicts.append((f["path"], plan.files[key][2], mod_id))
            base = f["archive"]
            if split_archive_name(base) is None:
                raise SystemExit(f"{mod_id}: unexpected archive name {base}")
            number = cfg.patch_number
            while patch_name(base, number).lower() in used_names:
                number += 1  # a non-ctlmod mod already uses this slot
            stem = patch_name(base, number)
            # a later mod overriding an earlier file: drop it from the earlier archive
            if key in plan.files:
                for lst in plan.archives.values():
                    if key in lst:
                        lst.remove(key)
            plan.files[key] = (f["path"], src, mod_id)
            plan.archives.setdefault(stem, []).append(key)
            plan.base_of[stem] = base
    plan.archives = {k: v for k, v in plan.archives.items() if v}
    return plan


def install(cfg: Config, gd: GameData, state: State, dry_run: bool = False, log=print) -> Plan:
    plan = make_plan(cfg, gd, state)
    for path, loser, winner in plan.conflicts:
        log(f"conflict: {path} is changed by '{loser}' and '{winner}' - using '{winner}' (later in load order)")
    for m in plan.skipped:
        log(f"skipping '{m}': not built yet (run: ctlmod build {m})")
    if dry_run:
        for stem, keys in plan.archives.items():
            log(f"would write {stem} ({len(keys)} files, overrides {plan.base_of[stem]})")
        return plan
    removed = uninstall(cfg, gd, state, log=lambda s: None)
    if removed:
        log(f"removed {len(removed)} previously installed patch archive(s)")
    written = []
    for stem, keys in sorted(plan.archives.items()):
        base_arc = next((a for a in gd.archives if a.bin_path.stem == plan.base_of[stem]), None)
        files = [(plan.files[k][0], plan.files[k][1]) for k in sorted(keys)]
        out = gd.pack_dir / stem
        write_archive(out, files, template=base_arc, prefix=CTLMOD_MARKER)
        written.append(stem)
        log(f"wrote {stem}.bin/.rmdp ({len(files)} files, overrides {plan.base_of[stem]})")
    state.installed = {
        "archives": written,
        "mods": [m for m in state.order if state.enabled.get(m, True) and m not in plan.skipped],
        "files": {plan.files[k][0]: plan.files[k][2] for k in plan.files},
    }
    state.save()
    if not written:
        log("nothing to install (no enabled, built mods)")
    return plan


def uninstall(cfg: Config, gd: GameData, state: State, log=print) -> list[str]:
    removed = []
    for b in installed_archives(gd.pack_dir):
        for ext in (".bin", ".rmdp", ".packmeta"):
            p = b.with_suffix(ext)
            if p.exists():
                p.unlink()
        removed.append(b.stem)
        log(f"removed {b.stem}")
    state.installed = {}
    state.save()
    return removed


def export_loose(cfg: Config, gd: GameData, state: State, out_dir: Path, log=print) -> int:
    """Write the merged files as a plain folder tree (for loose-file loaders)."""
    plan = make_plan(cfg, gd, state)
    for path, src, _ in plan.files.values():
        dest = out_dir / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(src.read_bytes())
    log(f"wrote {len(plan.files)} files to {out_dir}")
    return len(plan.files)


# ------------------------------------------------------------------------ sharing


def pack_mod(cfg: Config, mod_id: str, out: Path | None = None, include_sources: bool = False, log=print) -> Path:
    """Zip a built mod (mod.toml + build/) so others can install it with ``ctlmod unpack``."""
    mod = Mod.load(cfg, mod_id)
    if not mod.manifest():
        raise SystemExit(f"build the mod first: ctlmod build {mod_id}")
    out = out or (cfg.workspace / "dist" / f"{mod_id}-{mod.version}.zip")
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(mod.root / "mod.toml", f"{mod_id}/mod.toml")
        for p in sorted(mod.build_dir.rglob("*")):
            if p.is_file():
                z.write(p, f"{mod_id}/{p.relative_to(mod.root).as_posix()}")
        if include_sources:
            for p in sorted((mod.root / "models").rglob("*")):
                if p.is_file() and "blender_export" not in p.parts and p.suffix != ".blend1":
                    z.write(p, f"{mod_id}/{p.relative_to(mod.root).as_posix()}")
    log(f"packed {out}")
    return out


def unpack_mod(cfg: Config, zip_path: Path, force: bool = False, log=print) -> str:
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        tops = {n.split("/", 1)[0] for n in names}
        if len(tops) != 1:
            raise SystemExit("not a ctlmod mod archive (expected a single top-level folder)")
        mod_id = tops.pop()
        if f"{mod_id}/mod.toml" not in names or f"{mod_id}/build/manifest.json" not in names:
            raise SystemExit("not a ctlmod mod archive (mod.toml or build/manifest.json missing)")
        dest = cfg.mods_dir / mod_id
        if dest.exists() and not force:
            raise SystemExit(f"mod '{mod_id}' already exists (use --force to replace it)")
        root = cfg.mods_dir.resolve()
        for n in names:
            target = (cfg.mods_dir / n).resolve()
            if root not in target.parents:
                raise SystemExit(f"refusing unsafe path in archive: {n}")
        z.extractall(cfg.mods_dir)
    json.loads((cfg.mods_dir / mod_id / "build" / "manifest.json").read_text())
    log(f"unpacked mod '{mod_id}' into {cfg.mods_dir / mod_id}")
    return mod_id
