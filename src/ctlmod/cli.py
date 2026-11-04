"""``ctlmod`` command line interface. Run ``ctlmod --help`` or ``ctlmod <command> --help``."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from .config import Config, State, find_blender


def _gd(cfg: Config):
    from .gamedata import GameData

    return GameData(cfg.require_game())


def _state(cfg: Config) -> State:
    from .project import Mod

    st = State.load(cfg)
    st.sync(Mod.all_ids(cfg))
    return st


def _pick_model(gd, pattern: str) -> str:
    if pattern.lower().endswith(".binfbx") and pattern in gd:
        return gd.locate(pattern).entry.path
    hits = gd.models(pattern)
    exact = [h for h in hits if Path(h.entry.path).stem.lower() == pattern.lower()]
    if len(exact) == 1:
        return exact[0].entry.path
    if len(hits) == 1:
        return hits[0].entry.path
    if not hits:
        raise SystemExit(f"no model matches '{pattern}'. Try: ctlmod find {pattern}")
    listing = "\n  ".join(h.entry.path for h in hits[:30])
    raise SystemExit(f"'{pattern}' matches {len(hits)} models; be more specific:\n  {listing}")


# ------------------------------------------------------------------------ commands


def cmd_setup(cfg: Config, a) -> None:
    from .gamedata import GameData, find_game_dir

    if a.game:
        cfg.game_dir = Path(a.game)
    elif not cfg.game_dir:
        cfg.game_dir = find_game_dir()
    if a.blender:
        cfg.blender = Path(a.blender)
    elif not cfg.blender:
        cfg.blender = find_blender()
    if a.patch_number:
        cfg.patch_number = a.patch_number
    cfg.save()
    print(f"workspace : {cfg.workspace}")
    print(f"game      : {cfg.game_dir or 'NOT FOUND - pass --game'}")
    print(f"blender   : {cfg.blender or 'NOT FOUND - pass --blender'}")
    if cfg.game_dir:
        gd = GameData(cfg.game_dir)
        print(f"archives  : {len(gd.archives)} original archives, {len(gd.index)} files, {len(gd.models())} models")
    cfg.mods_dir.mkdir(exist_ok=True)


def cmd_find(cfg: Config, a) -> None:
    gd = _gd(cfg)
    if a.files:
        hits = gd.glob(a.pattern if any(c in a.pattern for c in "*?") else f"*{a.pattern}*")
        for h in hits[: a.limit]:
            print(f"{h.entry.path}  ({h.entry.size:,} bytes, {h.archive_name})")
        print(f"{len(hits)} files")
        return
    hits = gd.models(a.pattern)
    if a.characters:
        hits = [h for h in hits if "/characters/" in h.entry.path.lower()]
    for h in hits[: a.limit]:
        print(f"{Path(h.entry.path).stem:<55} {h.entry.size / 1e6:7.2f} MB  {h.entry.path}")
    more = f" (showing {a.limit}; use --limit)" if len(hits) > a.limit else ""
    print(f"{len(hits)} models{more}")


def cmd_info(cfg: Config, a) -> None:
    from .binfbx import BinFbx
    from .package import game_texture_to_archive_path

    gd = _gd(cfg)
    path = _pick_model(gd, a.model)
    loc = gd.locate(path)
    f = BinFbx.parse(gd.read(path))
    print(f"{path}  [{loc.archive_name}]")
    print(f"bones: {len(f.bones)}  LODs: {f.lod_count}  meshes: {len(f.meshes)}  shadow meshes: {len(f.shadow_meshes)}")
    if f.variants:
        print("material variants:", ", ".join(v[0].text for v in f.variants))
    print("meshes:")
    for i, m in enumerate(f.meshes):
        print(f"  [{i:2d}] LOD{m.lod} {f.materials[f.mesh_material[i]].name.text:<40} {m.face_count:7d} tris  weights/vertex: {m.unk0}")
    print("materials:")
    for i, m in enumerate(f.materials):
        print(f"  [{i:2d}] {m.name.text}  (shader: {m.shader.text})")
        if a.textures:
            for param, tex in m.textures().items():
                p = game_texture_to_archive_path(tex)
                where = gd.locate(p).archive_name if p in gd else "MISSING"
                print(f"        {param:<28} {p}  [{where}]")


def cmd_extract(cfg: Config, a) -> None:
    gd = _gd(cfg)
    hits = gd.glob(a.pattern)
    out = Path(a.output)
    for h in hits:
        dest = out / h.entry.path
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = h.archive.read(h.entry)
        if a.png and dest.suffix == ".tex":
            from . import texture

            try:
                texture.to_png(data, dest.with_suffix(".png"))
                continue
            except Exception as e:
                print(f"warning: {h.entry.path}: {e}; writing raw file")
        dest.write_bytes(data)
    print(f"extracted {len(hits)} files to {out}")


def cmd_new(cfg: Config, a) -> None:
    from .project import Mod

    mod = Mod.create(cfg, a.mod, a.name or "", a.author or "", a.description or "")
    st = _state(cfg)
    st.save()
    print(f"created mod '{mod.id}' at {mod.root}")
    print(f"next: ctlmod add {mod.id} <model>   (find models with: ctlmod find jesse)")


def cmd_add(cfg: Config, a) -> None:
    from .project import Mod

    gd = _gd(cfg)
    mod = Mod.load(cfg, a.mod)
    path = _pick_model(gd, a.model)
    name = mod.add_model(cfg, gd, path, a.lods, overwrite=a.force)
    print(f"\nadded {name}. Edit it in Blender:\n  {mod.blend_path(name)}\n(or run: ctlmod open {mod.id} {name})")


def cmd_open(cfg: Config, a) -> None:
    from .project import Mod

    mod = Mod.load(cfg, a.mod)
    names = mod.resolve_model(a.model)
    blend = mod.blend_path(names[0])
    exe = cfg.require_blender()
    print(f"opening {blend}")
    subprocess.Popen([str(exe), str(blend)])


def cmd_build(cfg: Config, a) -> None:
    from .project import Mod

    gd = _gd(cfg)
    mod = Mod.load(cfg, a.mod)
    if a.lods:
        for m in mod.models.values():
            m.lods = a.lods
        mod.save()
    mod.build(cfg, gd)
    if a.install:
        cmd_install(cfg, argparse.Namespace(dry_run=False))


def cmd_render(cfg: Config, a) -> None:
    from .package import export_package
    from .project import Mod, run_blender

    mod = Mod.load(cfg, a.mod)
    out_dir = mod.root / "renders"
    out_dir.mkdir(exist_ok=True)
    for name in mod.resolve_model(a.model):
        blend = mod.blend_path(name)
        tag = ""
        if a.built:
            # re-import the *built* binfbx to check what the game will get
            gd = _gd(cfg)
            built = mod.build_dir / "files" / mod.models[name].source
            if not built.exists():
                raise SystemExit("not built yet: ctlmod build " + mod.id)
            tmp = mod.root / ".preview" / name
            textures_dir = mod.build_dir / "files"

            def reader(p, _gd=gd):
                f = textures_dir / p
                return f.read_bytes() if f.exists() else _gd.try_read(p)

            export_package(built.read_bytes(), mod.models[name].source, tmp, reader, log=lambda s: None)
            blend = tmp / "preview.blend"
            run_blender(cfg, ["import", str(tmp), str(blend)])
            tag = "_built"
        out = out_dir / f"{name}{tag}_{a.view}_lod{a.lod}.png"
        run_blender(cfg, ["render", str(out), "--view", a.view, "--lod", str(a.lod)], blend=blend)
        print(f"rendered {out}")


def cmd_list(cfg: Config, a) -> None:
    from .project import Mod

    st = _state(cfg)
    st.save()
    if not st.order:
        print("no mods yet. Create one with: ctlmod new <id>")
        return
    installed = set(st.installed.get("mods", []))
    print(f"{'#':>2}  {'on':<3} {'mod':<28} {'built':<6} {'installed':<9} models")
    for i, m in enumerate(st.order):
        mod = Mod.load(cfg, m)
        on = "yes" if st.enabled.get(m, True) else "no"
        built = "yes" if mod.manifest() else "no"
        print(f"{i + 1:>2}  {on:<3} {m:<28} {built:<6} {'yes' if m in installed else 'no':<9} {', '.join(mod.models) or '-'}")
    print("\nLater mods win when two mods change the same file. Change order with: ctlmod order <mod> <position>")


def cmd_enable(cfg: Config, a, value: bool) -> None:
    st = _state(cfg)
    for m in a.mods:
        if m not in st.order:
            raise SystemExit(f"unknown mod '{m}'")
        st.enabled[m] = value
    st.save()
    print(("enabled " if value else "disabled ") + ", ".join(a.mods) + ". Run 'ctlmod install' to apply.")


def cmd_order(cfg: Config, a) -> None:
    st = _state(cfg)
    if a.mod not in st.order:
        raise SystemExit(f"unknown mod '{a.mod}'")
    st.order.remove(a.mod)
    st.order.insert(max(0, min(len(st.order), a.position - 1)), a.mod)
    st.save()
    print("load order: " + " -> ".join(st.order))


def cmd_install(cfg: Config, a) -> None:
    from . import installer

    gd = _gd(cfg)
    installer.install(cfg, gd, _state(cfg), dry_run=a.dry_run)


def cmd_uninstall(cfg: Config, a) -> None:
    from . import installer

    removed = installer.uninstall(cfg, _gd(cfg), _state(cfg))
    print(f"removed {len(removed)} archive(s); the game is back to its original files" if removed else "nothing installed")


def cmd_status(cfg: Config, a) -> None:
    from . import installer

    gd = _gd(cfg)
    st = _state(cfg)
    arcs = installer.installed_archives(gd.pack_dir)
    print(f"game: {gd.game_dir}")
    print("installed patch archives: " + (", ".join(b.stem for b in arcs) or "none"))
    if st.installed:
        print("installed mods: " + (", ".join(st.installed.get("mods", [])) or "none"))
        print(f"files overridden: {len(st.installed.get('files', {}))}")
    plan = installer.make_plan(cfg, gd, st)
    current = {k: v[2] for k, v in plan.files.items()}
    was = {k.lower(): v for k, v in st.installed.get("files", {}).items()}
    if current != was:
        print("NOTE: enabled mods/builds differ from what is installed - run 'ctlmod install'")
    for path, loser, winner in plan.conflicts:
        print(f"conflict: {path}: '{winner}' overrides '{loser}'")


def cmd_loose(cfg: Config, a) -> None:
    from . import installer

    installer.export_loose(cfg, _gd(cfg), _state(cfg), Path(a.output))


def cmd_pack(cfg: Config, a) -> None:
    from . import installer

    installer.pack_mod(cfg, a.mod, Path(a.output) if a.output else None, include_sources=a.sources)


def cmd_unpack(cfg: Config, a) -> None:
    from . import installer

    mod_id = installer.unpack_mod(cfg, Path(a.zip), force=a.force)
    st = _state(cfg)
    st.save()
    print(f"'{mod_id}' added (enabled). Run 'ctlmod install' to apply.")


def save_dirs(steam_root: Path | None = None) -> list[Path]:
    """Known Control save locations that exist (Steam Cloud folders, Epic/GOG local folder)."""
    import os

    steam = steam_root or Path(r"C:\Program Files (x86)\Steam")
    found = sorted((steam / "userdata").glob("*/870780"))
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Remedy" / "Control"
    if local.is_dir():
        found.append(local)
    return found


def cmd_backup_saves(cfg: Config, a) -> None:
    """Copy save folders into <workspace>/backups/saves-<timestamp>/ (read-only on the source)."""
    import shutil
    import time

    dirs = [Path(a.source)] if a.source else save_dirs()
    if not dirs:
        raise SystemExit("no Control save folder found; pass --source <folder>")
    dest = cfg.workspace / "backups" / time.strftime("saves-%Y%m%d-%H%M%S")
    for d in dirs:
        target = dest / "_".join(d.parts[-2:]).replace(":", "")
        shutil.copytree(d, target)
        n = sum(1 for p in target.rglob("*") if p.is_file())
        print(f"backed up {d} -> {target} ({n} files)")
    print("To restore: quit the game (and pause Steam Cloud sync), then copy the files back.")


def cmd_selftest(cfg: Config, a) -> None:
    """Parse every model in the game and check it re-serialises byte-identically."""
    from .binfbx import BinFbx

    gd = _gd(cfg)
    hits = gd.models(a.pattern)
    ok = bad = 0
    for h in hits:
        data = h.archive.read(h.entry)
        try:
            same = BinFbx.parse(data).to_bytes() == data
        except Exception as e:
            same = False
            print(f"FAIL {h.entry.path}: {e}")
        else:
            if not same:
                print(f"MISMATCH {h.entry.path}")
        ok += same
        bad += not same
    print(f"{ok} ok, {bad} failed")
    if bad:
        sys.exit(1)


# ------------------------------------------------------------------------ parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ctlmod", description="Control: Ultimate Edition model/outfit modding toolkit")
    p.add_argument("--workspace", help="workspace folder (default: nearest folder with ctlmod.toml)")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="<command>")

    s = sub.add_parser("setup", help="configure game and Blender paths")
    s.add_argument("--game", help="Control install folder (contains data_packfiles)")
    s.add_argument("--blender", help="path to blender.exe")
    s.add_argument("--patch-number", type=int, help="patch slot to install into (default 1 -> ep100-001-*)")
    s.set_defaults(func=cmd_setup)

    s = sub.add_parser("find", help="search models (or any file with --files)")
    s.add_argument("pattern", nargs="?", default="*", help="substring or glob, e.g. jesse or *civilian*")
    s.add_argument("--characters", action="store_true", help="only models under objects/characters")
    s.add_argument("--files", action="store_true", help="search all files, not just models")
    s.add_argument("--limit", type=int, default=200)
    s.set_defaults(func=cmd_find)

    s = sub.add_parser("info", help="show meshes, materials and textures of a model")
    s.add_argument("model")
    s.add_argument("--textures", action="store_true", help="list texture files per material")
    s.set_defaults(func=cmd_info)

    s = sub.add_parser("extract", help="extract raw game files matching a glob")
    s.add_argument("pattern", help="e.g. data_pc/textures/characters/andro/jesse/*")
    s.add_argument("-o", "--output", default="work/extracted")
    s.add_argument("--png", action="store_true", help="convert .tex textures to .png")
    s.set_defaults(func=cmd_extract)

    s = sub.add_parser("new", help="create a new mod project")
    s.add_argument("mod", help="mod id (folder name), e.g. jesse_red_suit")
    s.add_argument("--name")
    s.add_argument("--author")
    s.add_argument("--description")
    s.set_defaults(func=cmd_new)

    s = sub.add_parser("add", help="add a model to a mod and create its .blend")
    s.add_argument("mod")
    s.add_argument("model", help="model name/pattern or archive path, e.g. jesse_suit_publish_physx")
    s.add_argument("--lods", choices=["authored", "copy", "decimate"], default="authored",
                   help="how LOD1+ are produced at build time (default: as authored in the .blend)")
    s.add_argument("--force", action="store_true", help="re-export, discarding existing edits")
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("open", help="open a mod's model in Blender")
    s.add_argument("mod")
    s.add_argument("model", nargs="?")
    s.set_defaults(func=cmd_open)

    s = sub.add_parser("build", help="turn the mod's .blend files into game files")
    s.add_argument("mod")
    s.add_argument("--lods", choices=["authored", "copy", "decimate"], help="override LOD mode for all models")
    s.add_argument("--install", action="store_true", help="install enabled mods afterwards")
    s.set_defaults(func=cmd_build)

    s = sub.add_parser("render", help="render a preview image of a mod's model")
    s.add_argument("mod")
    s.add_argument("model", nargs="?")
    s.add_argument("--view", choices=["front", "back", "side", "face"], default="front")
    s.add_argument("--lod", type=int, default=0)
    s.add_argument("--built", action="store_true", help="render the built game file instead of the .blend")
    s.set_defaults(func=cmd_render)

    s = sub.add_parser("list", help="list mods, load order and status")
    s.set_defaults(func=cmd_list)
    s = sub.add_parser("enable", help="enable mods")
    s.add_argument("mods", nargs="+")
    s.set_defaults(func=lambda c, a: cmd_enable(c, a, True))
    s = sub.add_parser("disable", help="disable mods")
    s.add_argument("mods", nargs="+")
    s.set_defaults(func=lambda c, a: cmd_enable(c, a, False))
    s = sub.add_parser("order", help="move a mod to a load-order position (1 = lowest priority)")
    s.add_argument("mod")
    s.add_argument("position", type=int)
    s.set_defaults(func=cmd_order)

    s = sub.add_parser("install", help="install all enabled, built mods into the game")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_install)
    s = sub.add_parser("uninstall", help="remove everything ctlmod installed")
    s.set_defaults(func=cmd_uninstall)
    s = sub.add_parser("status", help="show what is installed and pending")
    s.set_defaults(func=cmd_status)
    s = sub.add_parser("export-loose", help="write merged mod files as a folder tree (for loose-file loaders)")
    s.add_argument("output")
    s.set_defaults(func=cmd_loose)

    s = sub.add_parser("pack", help="zip a built mod for sharing")
    s.add_argument("mod")
    s.add_argument("-o", "--output")
    s.add_argument("--sources", action="store_true", help="include .blend and model packages")
    s.set_defaults(func=cmd_pack)
    s = sub.add_parser("unpack", help="add a mod from a zip made by 'ctlmod pack'")
    s.add_argument("zip")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_unpack)

    s = sub.add_parser("backup-saves", help="copy your Control save games into <workspace>/backups")
    s.add_argument("--source", help="save folder to back up (default: auto-detect)")
    s.set_defaults(func=cmd_backup_saves)

    s = sub.add_parser("selftest", help="verify the model parser against every model in the game")
    s.add_argument("pattern", nargs="?", default="*")
    s.set_defaults(func=cmd_selftest)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    cfg = Config.load(Path(args.workspace) if args.workspace else None)
    try:
        args.func(cfg, args)
    except KeyboardInterrupt:
        sys.exit(130)
    except RuntimeError as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
