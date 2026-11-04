"""Mod manager: compose enabled mods into patch archives inside the game folder.

Control loads every ``*.rmdp`` in ``data_packfiles``; an archive named
``ep100-001-generic`` overrides files of ``ep100-000-generic``. Instead of shipping one
patch per mod (which would collide when two mods touch the same archive), control-mod merges
all *enabled* mods - in load order, later mods winning - into a single patch per
original archive. Re-running ``install`` after enabling/disabling mods rebuilds them.

Archives written by control-mod carry a marker in their header, so ``uninstall`` only ever
deletes files control-mod created and original game files are never modified.
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .archive import write_archive
from .config import Config, State
from .gamedata import CONTROL_MOD_MARKER, GameData, is_control_mod_archive, patch_name, split_archive_name
from .project import Mod


@dataclass
class Plan:
    files: dict[str, tuple[str, Path, str]] = field(default_factory=dict)  # lower path -> (path, source file, mod id)
    archives: dict[str, list[str]] = field(default_factory=dict)  # patch stem -> [lower paths]
    base_of: dict[str, str] = field(default_factory=dict)  # patch stem -> base stem
    conflicts: list[tuple[str, str, str]] = field(default_factory=list)  # (path, loser, winner)
    skipped: list[str] = field(default_factory=list)  # enabled mods without a build


def installed_archives(pack_dir: Path) -> list[Path]:
    return sorted(b for b in pack_dir.glob("*.bin") if is_control_mod_archive(b))


def make_plan(cfg: Config, gd: GameData, state: State) -> Plan:
    plan = Plan()
    used_names = {b.stem.lower() for b in gd.pack_dir.glob("*.bin") if not is_control_mod_archive(b)}
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
                number += 1  # a non-control-mod mod already uses this slot
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


def shared_impact(cfg: Config, state: State) -> dict[str, set[str]]:
    """Enabled mod -> other models its shared-texture edits also change (from manifests)."""
    out: dict[str, set[str]] = {}
    for mod_id in state.order:
        if not state.enabled.get(mod_id, True):
            continue
        man = Mod.load(cfg, mod_id).manifest() or {}
        affected = {o for f in man.get("files", []) for o in f.get("also_affects") or []}
        if affected:
            out[mod_id] = affected
    return out


def build_packmeta(gd: GameData, base_stem: str, files: list[tuple[str, Path]], derived: dict[str, str] | None = None) -> bytes:
    """Metadata for a patch archive: the base archive's entries for each file, with the
    file size/CRC and (for models) buffer sizes, bounding box and texture dependencies of
    the new version. New textures get the metadata of the texture they were encoded like
    (``derived``: new path -> original path), with their own id and file info."""
    import zlib

    from . import packmeta
    from .binfbx import BinFbx
    from .package import game_texture_to_archive_path

    derived = {k.lower(): v for k, v in (derived or {}).items()}
    cache: dict[str, packmeta.PackMeta] = {}

    def meta_of(stem: str) -> packmeta.PackMeta:
        if stem not in cache:
            cache[stem] = packmeta.PackMeta.parse((gd.pack_dir / f"{stem}.packmeta").read_bytes())
        return cache[stem]

    def original_entry(path: str):
        return meta_of(gd.locate(path).archive_name).find(path) if path in gd else None

    base = meta_of(base_stem)
    out = packmeta.PackMeta(base.registry)
    for path, src in files:
        data = Path(src).read_bytes()
        orig = base.find(path) or original_entry(path)
        rid = packmeta.resource_id(path)
        if orig:
            objects = dict(orig.objects)
        else:
            objects = {packmeta.RESOURCE: packmeta.resource_meta(rid)}
            like = derived.get(path.lower())
            like_entry = original_entry(like) if like else None
            if like_entry and packmeta.TEXTURE in like_entry.objects:
                objects[packmeta.TEXTURE] = like_entry.objects[packmeta.TEXTURE]
        objects[packmeta.FILE_INFO] = packmeta.file_info(len(data), zlib.crc32(data) & 0xFFFFFFFF)
        if packmeta.MESH in objects and path.lower().endswith(".binfbx"):
            f = BinFbx.parse(data)
            objects[packmeta.MESH] = packmeta.patch_mesh(
                objects[packmeta.MESH], len(f.secondary), len(f.primary), len(f.indices) // f.index_size, f.index_size, f.bbox
            )
            # every texture the materials use must be a dependency (the game preloads them)
            own, deps = packmeta.read_resource_meta(objects[packmeta.RESOURCE])
            for m in f.materials:
                for tex in m.textures().values():
                    t = packmeta.resource_id(game_texture_to_archive_path(tex))
                    if t not in deps:
                        deps.append(t)
            objects[packmeta.RESOURCE] = packmeta.resource_meta(own, deps)
        entry = packmeta.Entry(packmeta.game_path(orig.path if orig else path), objects)
        out.entries.append(entry)
    return out.to_bytes()


def _derived(cfg: Config, plan: "Plan", keys: list[str]) -> dict[str, str]:
    """New file -> original it was derived from, from the manifests of the mods involved."""
    out = {}
    for m in {plan.files[k][2] for k in keys}:
        for f in (Mod.load(cfg, m).manifest() or {}).get("files", []):
            if f.get("derived_from"):
                out[f["path"]] = f["derived_from"]
    return out


def _wants_metadata(cfg: Config, plan: "Plan", keys: list[str]) -> bool:
    mods = {plan.files[k][2] for k in keys}
    return any((Mod.load(cfg, m).manifest() or {}).get("metadata") for m in mods)


def install(cfg: Config, gd: GameData, state: State, dry_run: bool = False, log=print, metadata: bool = False) -> Plan:
    """Write patch archives for all enabled, built mods. A ``.packmeta`` describing the
    patched files is written for mods built with metadata (the default) or when ``metadata`` is set."""
    plan = make_plan(cfg, gd, state)
    for path, loser, winner in plan.conflicts:
        log(f"conflict: {path} is changed by '{loser}' and '{winner}' - using '{winner}' (later in load order)")
    for m in plan.skipped:
        log(f"skipping '{m}': not built yet (run: control-mod build {m})")
    for mod_id, affected in shared_impact(cfg, state).items():
        log(f"note: '{mod_id}' edits shared textures - this also changes {len(affected)} other model(s) "
            f"(details: control-mod impact {mod_id})")
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
        write_archive(out, files, template=base_arc, marker=CONTROL_MOD_MARKER)
        written.append(stem)
        extra = ""
        if (metadata or _wants_metadata(cfg, plan, keys)) and (gd.pack_dir / f"{plan.base_of[stem]}.packmeta").exists():
            out.with_suffix(".packmeta").write_bytes(build_packmeta(gd, plan.base_of[stem], files, _derived(cfg, plan, keys)))
            extra = " + .packmeta"
        log(f"wrote {stem}.bin/.rmdp{extra} ({len(files)} files, overrides {plan.base_of[stem]})")
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
    """Zip a built mod (mod.toml + build/) so others can install it with ``control-mod unpack``."""
    mod = Mod.load(cfg, mod_id)
    if not mod.manifest():
        raise SystemExit(f"build the mod first: control-mod build {mod_id}")
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
            raise SystemExit("not a control-mod mod archive (expected a single top-level folder)")
        mod_id = tops.pop()
        if f"{mod_id}/mod.toml" not in names or f"{mod_id}/build/manifest.json" not in names:
            raise SystemExit("not a control-mod mod archive (mod.toml or build/manifest.json missing)")
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
