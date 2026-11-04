"""Model packages: the editable, Blender-friendly form of a ``.binfbx``.

A package is a folder::

    model.json        metadata: bones, materials, textures, meshes (see ``export_package``)
    geometry.npz      per-mesh arrays in *Blender space* (welded vertices + per-corner data)
    template.binfbx   the original file; everything not editable is taken from it
    textures/*.png    decoded textures referenced by the materials

Blender never sees game conventions: this module converts coordinates
(game: left-handed, Y up -> Blender: right-handed, Z up, character facing -Y),
UV origin (top-left -> bottom-left) and triangle winding in both directions.

The Blender side writes ``blender_export/`` (``scene.json`` + ``scene.npz``) which
:func:`build_from_blender` turns back into a ``.binfbx`` and changed textures.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from . import model, texture
from .binfbx import P_TEXTURE, P_TEXTURE_A, BinFbx, Material, NLString
from .geometry import Submesh, decode

FORMAT_VERSION = 1

# game (x, y, z) -> blender (-x, -z, y)
_C = np.array([[-1.0, 0, 0], [0, 0, -1.0], [0, 1.0, 0]])
_C_INV = np.linalg.inv(_C)


def to_blender(v: np.ndarray) -> np.ndarray:
    return (np.asarray(v, np.float64) @ _C.T).astype(np.float32)


def to_game(v: np.ndarray) -> np.ndarray:
    return (np.asarray(v, np.float64) @ _C_INV.T).astype(np.float32)


def game_texture_to_archive_path(p: str) -> str:
    """``runtimedata\\data_pc\\textures\\x.tex`` -> ``data_pc/textures/x.tex``."""
    p = p.replace("\\", "/")
    if p.lower().startswith("runtimedata/"):
        p = p[len("runtimedata/") :]
    return p


def archive_to_game_texture_path(p: str) -> str:
    return "runtimedata\\" + p.replace("/", "\\")


def bone_world_matrices(fbx: BinFbx) -> list[np.ndarray]:
    """Rest matrices (4x4, column vectors) of every bone in game space."""
    out = []
    for b in fbx.bones:
        m = np.array(b.inv_bind[:9], np.float64).reshape(3, 3)
        t = np.array(b.inv_bind[9:12], np.float64)
        w = np.eye(4)
        w[:3, :3] = m
        w[:3, 3] = -m @ t
        out.append(w)
    return out


def _blender_bones(fbx: BinFbx) -> list[dict]:
    mats = bone_world_matrices(fbx)
    c4, ci4 = np.eye(4), np.eye(4)
    c4[:3, :3], ci4[:3, :3] = _C, _C_INV
    heads = [to_blender(m[:3, 3]) for m in mats]
    children: dict[int, list[int]] = {}
    for i, b in enumerate(fbx.bones):
        children.setdefault(b.parent, []).append(i)
    out = []
    for i, b in enumerate(fbx.bones):
        mb = c4 @ mats[i] @ ci4
        head = heads[i].astype(np.float64)
        tail = None
        kids = [k for k in children.get(i, []) if np.linalg.norm(heads[k] - head) > 1e-3]
        if len(kids) == 1 or (kids and "spine" in b.name.text.lower()):
            tail = heads[kids[0]]
        elif kids:
            tail = np.mean([heads[k] for k in kids], axis=0)
            if np.linalg.norm(tail - head) < 1e-3:
                tail = None
        if tail is None:
            if b.parent >= 0 and np.linalg.norm(head - heads[b.parent]) > 1e-3:
                d = head - heads[b.parent]
                tail = head + d / np.linalg.norm(d) * min(0.05, float(np.linalg.norm(d)))
            else:
                tail = head + np.array([0, 0, 0.05])
        out.append(
            {
                "name": b.name.text,
                "parent": b.parent,
                "head": [float(x) for x in head],
                "tail": [float(x) for x in tail],
                "matrix": [[float(x) for x in row] for row in mb],
            }
        )
    return out


# ------------------------------------------------------------------------- welding


def weld(sub: Submesh) -> dict[str, np.ndarray]:
    """Merge split vertices (same position + skin) and move split data to corners."""
    pos = to_blender(sub.positions)
    keys = [pos.view(np.int32)]
    if sub.bone_indices is not None:
        w = np.round(sub.bone_weights * 1000).astype(np.int32)
        keys += [np.where(w > 0, sub.bone_indices, -1).astype(np.int32), w]
    key = np.ascontiguousarray(np.concatenate(keys, axis=1))
    _, first, inv = np.unique(key.view([("", key.dtype)] * key.shape[1]).ravel(), return_index=True, return_inverse=True)
    inv = inv.ravel()
    # keep original vertex order of first occurrences for stable, readable meshes
    order = np.argsort(first)
    remap = np.empty_like(order)
    remap[order] = np.arange(len(order))
    vmap = remap[inv]
    rep = first[order]

    faces = sub.faces[:, [0, 2, 1]]  # flip winding for the handedness change
    wf = vmap[faces]
    # Welding must not create degenerate or duplicate triangles (Blender rejects them,
    # e.g. double-sided hair cards): such faces keep private copies of their vertices.
    bad = (wf[:, 0] == wf[:, 1]) | (wf[:, 1] == wf[:, 2]) | (wf[:, 0] == wf[:, 2])
    srt = np.sort(wf, axis=1)
    _, first_face = np.unique(srt, axis=0, return_index=True)
    dup = np.ones(len(wf), bool)
    dup[first_face] = False
    bad |= dup
    src_of_vertex = rep
    if bad.any():
        extra = faces[bad].ravel()  # original split vertex indices
        wf[bad] = (len(rep) + np.arange(len(extra))).reshape(-1, 3)
        src_of_vertex = np.concatenate([rep, extra])
    corners = faces.ravel()
    out: dict[str, np.ndarray] = {
        "verts": pos[src_of_vertex],
        "faces": wf.astype(np.int32),
        "corner_normals": to_blender(sub.normals[corners]),
    }
    for k, uv in enumerate(sub.uvs):
        c = uv[corners].astype(np.float32).copy()
        c[:, 1] = 1.0 - c[:, 1]
        out[f"corner_uv{k}"] = c
    for k, col in enumerate(sub.colors):
        out[f"corner_color{k}"] = col[corners].astype(np.float32)
    if sub.bone_indices is not None:
        out["bone_indices"] = sub.bone_indices[src_of_vertex].astype(np.int32)
        out["bone_weights"] = sub.bone_weights[src_of_vertex].astype(np.float32)
    if sub.islands is not None:
        out["islands"] = sub.islands[src_of_vertex].astype(np.int32)
    return out


def smooth_corner_normals(verts: np.ndarray, faces: np.ndarray) -> np.ndarray:
    """Area-weighted smooth vertex normals, expanded to corners."""
    p = np.asarray(verts, np.float64)
    fn = np.cross(p[faces[:, 1]] - p[faces[:, 0]], p[faces[:, 2]] - p[faces[:, 0]])
    vn = np.zeros_like(p)
    for k in range(3):
        np.add.at(vn, faces[:, k], fn)
    return vn[faces.ravel()]


def unweld(arrays: dict[str, np.ndarray], uv_count: int, color_count: int, recompute_normals: bool = False) -> dict[str, np.ndarray]:
    """Inverse of :func:`weld` for data coming back from Blender (Blender space in)."""
    faces = np.asarray(arrays["faces"], np.int64)
    corners = faces.ravel()
    n_corner = len(corners)
    if recompute_normals:
        normals = smooth_corner_normals(arrays["verts"], faces)
    else:
        normals = np.asarray(arrays["corner_normals"], np.float64).reshape(n_corner, 3)
    # degenerate normals (e.g. from collapsed triangles) fall back to the face normal
    ln = np.linalg.norm(normals, axis=1)
    if (~(ln > 1e-8)).any():
        p = np.asarray(arrays["verts"], np.float64)
        fn = np.cross(p[faces[:, 1]] - p[faces[:, 0]], p[faces[:, 2]] - p[faces[:, 0]])
        fn = np.repeat(fn, 3, axis=0)
        fl = np.linalg.norm(fn, axis=1, keepdims=True)
        fn = np.where(fl > 1e-20, fn / np.maximum(fl, 1e-30), [[0.0, 0.0, 1.0]])
        normals = np.where((ln > 1e-8)[:, None], normals, fn)
    normals = normals / np.linalg.norm(normals, axis=1, keepdims=True)
    uvs = [np.asarray(arrays[f"corner_uv{k}"], np.float64).reshape(n_corner, 2) for k in range(uv_count) if f"corner_uv{k}" in arrays]
    cols = [np.asarray(arrays[f"corner_color{k}"], np.float64).reshape(n_corner, 4) for k in range(color_count) if f"corner_color{k}" in arrays]
    # group corners by vertex + attributes at game precision ...
    parts = [corners[:, None].astype(np.float64)]
    parts += [np.round(u * 4096) for u in uvs] + [np.round(c * 255) for c in cols]
    key = np.ascontiguousarray(np.concatenate(parts, axis=1))
    _, inv = np.unique(key.view([("", key.dtype)] * key.shape[1]).ravel(), return_inverse=True)
    labels = inv.ravel().astype(np.int64)
    # ... then split groups whose normals differ by more than ~2.5 degrees. Blender's
    # custom normals carry small per-corner noise, so exact comparison over-splits.
    # Each pass moves corners that disagree with their group's first corner into a new
    # group, so it terminates after at most (distinct normals per vertex) passes.
    while n_corner:
        firsts = np.full(labels.max() + 1, -1, np.int64)
        firsts[labels[::-1]] = np.arange(n_corner)[::-1]
        bad = (normals * normals[firsts[labels]]).sum(1) < 0.999
        bad[firsts] = False  # a group's reference corner always stays
        if not bad.any():
            break
        labels = np.unique(np.where(bad, labels.max() + 1 + labels, labels), return_inverse=True)[1].ravel()
    _, first, inv = np.unique(labels, return_index=True, return_inverse=True)
    inv = inv.ravel()
    order = np.argsort(first)
    remap = np.empty_like(order)
    remap[order] = np.arange(len(order))
    new_idx = remap[inv]
    src = first[order]  # corner index representing each new vertex
    vsrc = corners[src]
    out = {
        "positions": to_game(np.asarray(arrays["verts"])[vsrc]),
        "normals": to_game(normals[src]),
        "faces": new_idx.reshape(-1, 3)[:, [0, 2, 1]],
        "uvs": [],
        "colors": [c[src].astype(np.float32) for c in cols],
    }
    for u in uvs:
        g = u[src].astype(np.float32).copy()
        g[:, 1] = 1.0 - g[:, 1]
        out["uvs"].append(g)
    if "bone_indices" in arrays:
        out["bone_indices"] = np.asarray(arrays["bone_indices"])[vsrc]
        out["bone_weights"] = np.asarray(arrays["bone_weights"])[vsrc]
    if "islands" in arrays:
        out["islands"] = np.asarray(arrays["islands"])[vsrc].astype(np.int16)
    return out


# ------------------------------------------------------------------------- export

TextureReader = Callable[[str], bytes | None]  # archive path -> .tex bytes (or None)


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def material_info(m: Material) -> dict:
    params = []
    for p in m.params:
        v = p.value
        if isinstance(v, NLString):
            v = v.text
        elif isinstance(v, tuple):
            v = list(v)
        params.append({"name": p.name.text, "type": p.type, "value": v})
    return {"name": m.name.text, "shader": m.shader.text, "source": m.source.text, "params": params}


def export_package(
    fbx_bytes: bytes,
    source_path: str,
    out_dir: str | Path,
    read_texture: TextureReader | None = None,
    log: Callable[[str], None] = print,
    shared_with: Callable[[str], list[str]] | None = None,
) -> dict:
    """Write a model package for one ``.binfbx``. Returns the model.json content.

    ``shared_with(texture archive path)`` lists *other* models using a texture; it is
    stored per texture so the Blender file can flag shared textures.
    """
    out = Path(out_dir)
    (out / "textures").mkdir(parents=True, exist_ok=True)
    (out / "template.binfbx").write_bytes(fbx_bytes)
    fbx = BinFbx.parse(fbx_bytes)
    subs = decode(fbx)

    textures: dict[str, dict] = {}
    materials = []
    for mi, m in enumerate(fbx.materials):
        info = material_info(m)
        info["index"] = mi
        info["textures"] = {}
        for p in m.params:
            if p.type not in (P_TEXTURE, P_TEXTURE_A) or not p.value.text:
                continue
            apath = game_texture_to_archive_path(p.value.text)
            if apath not in textures:
                rec = {"archive_path": apath, "png": None}
                if shared_with is not None:
                    rec["shared_with"] = shared_with(apath)
                data = read_texture(apath) if read_texture else None
                if data is not None:
                    try:
                        dinfo = texture.read_info(data)
                        rec.update(format=dinfo.format, width=dinfo.width, height=dinfo.height)
                        if dinfo.editable:
                            png = out / "textures" / (_safe(Path(apath).stem) + ".png")
                            k = 1
                            while png.exists() and any(t.get("png") == f"textures/{png.name}" for t in textures.values()):
                                png = out / "textures" / f"{_safe(Path(apath).stem)}_{k}.png"
                                k += 1
                            texture.to_png(data, png)
                            rec["png"] = f"textures/{png.name}"
                            rec["sha256"] = _sha(png)
                    except Exception as e:  # keep going; texture simply stays untouched
                        log(f"  warning: could not convert {apath}: {e}")
                else:
                    log(f"  warning: texture not found in game archives: {apath}")
                textures[apath] = rec
            info["textures"][p.name.text] = apath
        materials.append(info)

    arrays: dict[str, np.ndarray] = {}
    meshes = []
    per_lod: dict[int, int] = {}
    for i, s in enumerate(subs):
        k = per_lod.get(s.lod, 0)
        per_lod[s.lod] = k + 1
        mname = fbx.materials[s.material].name.text
        name = f"LOD{s.lod}_{k:02d}_{mname}"
        for key, val in weld(s).items():
            arrays[f"m{i}_{key}"] = val
        meshes.append(
            {
                "name": name,
                "lod": s.lod,
                "material": s.material,
                "source_index": s.source_index,
                "uv_count": len(s.uvs),
                "color_count": len(s.colors),
                "skinned": s.bone_indices is not None,
                "vertex_count": s.vertex_count,
                "face_count": int(len(s.faces)),
            }
        )
    np.savez_compressed(out / "geometry.npz", **arrays)

    meta = {
        "format": FORMAT_VERSION,
        "source_path": source_path,
        "name": Path(source_path).stem,
        "lod_count": fbx.lod_count,
        "bones": _blender_bones(fbx),
        "materials": materials,
        "textures": textures,
        "meshes": meshes,
        "variants": [v[0].text for v in fbx.variants],
    }
    (out / "model.json").write_text(json.dumps(meta, indent=1))
    return meta


# ------------------------------------------------------------------------- build


class BuildError(ValueError):
    """An edit the game cannot load; the message explains what to do instead."""


# Why some edits are refused (confirmed in game, see README "What the game accepts"):
WHY_TEXTURE_BINDING = (
    "the game binds textures from its own metadata (.packmeta), not from the model file, "
    "so changing which texture a material uses has no effect in game"
)
WHY_NEW_FILES = "the game has no metadata for brand-new files, so it cannot load them"


@dataclass
class BuildResult:
    binfbx: bytes
    textures: dict[str, bytes] = field(default_factory=dict)  # archive path -> .tex bytes
    report: list[str] = field(default_factory=list)
    derived: dict[str, str] = field(default_factory=dict)  # new texture -> texture it was encoded like


def _load_blender_dump(dump_dir: Path) -> tuple[dict, dict[str, np.ndarray]]:
    scene = json.loads((dump_dir / "scene.json").read_text())
    with np.load(dump_dir / "scene.npz") as z:
        arrays = {k: z[k] for k in z.files}
    return scene, arrays


def build_from_blender(
    pkg_dir: str | Path,
    dump_dir: str | Path | None = None,
    read_texture: TextureReader | None = None,
    new_texture_root: str = "data_pc/textures/control-mod",
    log: Callable[[str], None] = print,
    strict: bool = True,
    size_budget: bool = True,
) -> BuildResult:
    """Rebuild the ``.binfbx`` (and any edited/new textures) from a Blender dump.

    ``strict`` (the default) only allows edits the game is known to load: geometry and
    weights within the original file size (lower LODs are simplified to make room),
    existing materials, and textures repainted in place at their original resolution.
    Anything else raises :class:`BuildError` listing every problem. ``strict=False``
    allows new materials/textures and larger models for experiments.
    ``size_budget=False`` keeps the strict rules except the size budget, for builds
    whose new sizes are described by a generated ``.packmeta`` (see control_mod.packmeta).
    """
    pkg = Path(pkg_dir)
    dump = Path(dump_dir) if dump_dir else pkg / "blender_export"
    meta = json.loads((pkg / "model.json").read_text())
    template = BinFbx.parse((pkg / "template.binfbx").read_bytes())
    scene, arrays = _load_blender_dump(dump)
    result = BuildResult(b"")
    report = result.report
    problems: list[str] = []

    # ---------------------------------------------------------------- materials
    by_name = {m.name.text: i for i, m in enumerate(template.materials)}
    tex_by_png = {}
    for apath, rec in meta["textures"].items():
        if rec.get("png"):
            tex_by_png[(pkg / rec["png"]).resolve()] = apath

    def texture_for_image(image_path: str, like_apath: str | None) -> str | None:
        """Archive path to reference for an image used in Blender (encoding if needed)."""
        p = Path(image_path)
        if not p.is_absolute():
            p = (pkg / p).resolve()
        else:
            p = p.resolve()
        if p in tex_by_png:  # one of our exported PNGs, maybe edited in place
            apath = tex_by_png[p]
            rec = meta["textures"][apath]
            if p.exists() and _sha(p) != rec.get("sha256") and apath not in result.textures:
                orig = read_texture(apath) if read_texture else None
                if orig is None:
                    raise FileNotFoundError(f"original texture {apath} not available for re-encoding")
                from PIL import Image

                with Image.open(p) as im:
                    size = im.size
                want = (rec.get("width"), rec.get("height"))
                if strict and want[0] and size != want:
                    problems.append(
                        f"{p.name} is {size[0]}x{size[1]} but must stay {want[0]}x{want[1]}: the game's metadata "
                        "describes the original resolution. Scale your artwork to the original size."
                    )
                    return apath
                result.textures[apath] = texture.from_image(p, orig)
                report.append(f"texture modified: {apath}")
            return apath
        if not p.is_file():
            report.append(f"warning: image {p} not found, keeping original texture")
            return None
        # a brand new image: encode it like the texture it replaces
        if strict and size_budget:
            problems.append(
                f"new image {p.name} is used in a material: {WHY_NEW_FILES}. Instead paint over the "
                f"original texture (its PNG in textures/), keeping its resolution - or build without --size-matched."
            )
            return like_apath
        like = read_texture(like_apath) if (read_texture and like_apath) else None
        if like is None:
            raise ValueError(f"new image {p.name} has no texture slot to take its format from")
        if strict:
            # its metadata is cloned from the texture it replaces, so the size must match
            from PIL import Image

            info = texture.read_info(like)
            with Image.open(p) as im:
                size = im.size
            if size != (info.width, info.height):
                problems.append(
                    f"new image {p.name} is {size[0]}x{size[1]}; new textures must have the resolution of the "
                    f"texture they replace ({Path(like_apath).name}: {info.width}x{info.height})."
                )
                return like_apath
        stem = _safe(p.stem).lower()
        apath = f"{new_texture_root}/{meta['name']}/{stem}{Path(like_apath).suffix}"
        if apath not in result.textures:
            result.textures[apath] = texture.from_image(p, like)
            result.derived[apath] = like_apath
            report.append(f"texture added: {apath} (from {p.name})")
        return apath

    out_materials: list[Material] = list(template.materials)
    mat_index: dict[str, int] = {}
    for bm in scene["materials"]:
        idx = bm.get("ctl_index")
        if idx is None or not 0 <= int(idx) < len(template.materials):
            base = bm.get("ctl_material") or bm["name"]
            if base not in by_name:
                raise ValueError(
                    f"Blender material '{bm['name']}' is not linked to a game material. "
                    "Duplicate an existing material (it keeps the 'ctl_*' properties) instead."
                )
            idx = by_name[base]
        idx = int(idx)
        base = template.materials[idx].name.text
        original = bm.get("original", bm["name"] == base)
        src = template.materials[idx]
        meta_mat = meta["materials"][idx]
        changed: dict[str, str] = {}
        for param, img in (bm.get("textures") or {}).items():
            orig_apath = meta_mat["textures"].get(param)
            apath = texture_for_image(img, orig_apath)
            if apath and apath != orig_apath:
                changed[param] = apath
        renamed = not original
        if strict and size_budget and changed:
            for param, apath in changed.items():
                problems.append(
                    f"material '{bm['name']}': {param} was switched to {Path(apath).name}, but "
                    f"{WHY_TEXTURE_BINDING}. To change the look, edit the original texture "
                    f"({Path(meta_mat['textures'].get(param, '?')).stem}.png) in place - or build without --size-matched."
                )
            changed = {}
        if strict and size_budget and renamed:
            problems.append(
                f"material '{bm['name']}' is a new copy of '{base}': new materials need metadata "
                f"({WHY_TEXTURE_BINDING}). Assign an existing material instead, or build without --size-matched."
            )
            mat_index[bm["name"]] = idx
            continue
        if not changed and not renamed:
            mat_index[bm["name"]] = idx
            continue
        new = copy.deepcopy(src)
        for p in new.params:
            if p.name.text in changed:
                p.value = NLString(archive_to_game_texture_path(changed[p.name.text]))
        if renamed:
            new.name = NLString(bm["name"])
            new.hash = _derived_hash(src.hash, bm["name"])
            out_materials.append(new)
            mat_index[bm["name"]] = len(out_materials) - 1
            report.append(f"material added: {bm['name']} (based on {base})")
        else:
            out_materials[idx] = new
            mat_index[bm["name"]] = idx
            report.append(f"material changed: {base} ({', '.join(changed)})")

    # ---------------------------------------------------------------- meshes
    bone_index = {b["name"]: i for i, b in enumerate(meta["bones"])}
    lod_layout = {}
    for m in template.meshes:
        lod_layout.setdefault(m.lod, list(m.attrs))
    subs: list[Submesh] = []
    for i, om in enumerate(scene["meshes"]):
        lod = int(om["lod"])
        layout = lod_layout.get(lod) or lod_layout[0]
        uv_need = sum(1 for a in layout if a.usage == 7)
        col_need = sum(1 for a in layout if a.usage == 4 and a.format == 4)
        a = {k[len(f"m{i}_") :]: v for k, v in arrays.items() if k.startswith(f"m{i}_")}
        if "faces" not in a or len(a["faces"]) == 0:
            report.append(f"skipped empty mesh {om['name']}")
            continue
        g = unweld(a, om.get("uv_count", 1), om.get("color_count", 0), recompute_normals=om.get("smooth_normals", False))
        if not g["uvs"]:
            g["uvs"] = [np.zeros((len(g["positions"]), 2), np.float32)]
            report.append(f"warning: {om['name']} has no UV map")
        while len(g["uvs"]) < uv_need:
            g["uvs"].append(g["uvs"][0])
        while len(g["colors"]) < col_need:
            g["colors"].append(np.ones((len(g["positions"]), 4), np.float32))
        bi = bw = None
        skinned = any(a_.usage in (13, 15) for a_ in layout)
        if skinned:
            names = om.get("groups", [])
            if "bone_indices" not in g or not len(names):
                raise ValueError(f"mesh '{om['name']}' has no vertex groups; it must be weighted to the skeleton")
            gmap = np.array([bone_index.get(n, -1) for n in names] + [-1])
            unknown = sorted({n for n in names if n not in bone_index})
            if unknown:
                report.append(f"warning: {om['name']}: ignoring non-bone vertex groups {unknown[:5]}")
            gi = np.asarray(g["bone_indices"])
            bi = gmap[np.where(gi < 0, len(names), gi)]
            bw = np.where(bi >= 0, g["bone_weights"], 0.0).astype(np.float32)
            bi = np.where(bi >= 0, bi, 0)
            empty = bw.sum(1) == 0
            if empty.any():
                report.append(f"warning: {om['name']}: {int(empty.sum())} vertices have no bone weights (bound to bone 0)")
                bw[empty, 0] = 1.0
        mat_name = om.get("material")
        if mat_name not in mat_index:
            raise ValueError(f"mesh '{om['name']}' uses material '{mat_name}' which is not a game material")
        src_idx = int(om.get("source_index", -1))
        if src_idx >= len(template.meshes) or (src_idx >= 0 and template.meshes[src_idx].lod != lod):
            src_idx = -1
        subs.append(
            Submesh(
                lod=lod,
                material=mat_index[mat_name],
                positions=g["positions"],
                normals=g["normals"],
                tangents=None,
                uvs=g["uvs"][:uv_need] if uv_need else [],
                colors=g["colors"][:col_need],
                bone_indices=bi,
                bone_weights=bw,
                faces=g["faces"],
                islands=g.get("islands"),
                source_index=src_idx,
                layout=layout,
                extra={k: v for k, v in _extra_from_template(template, src_idx).items()},
            )
        )
    if not subs:
        raise ValueError("no meshes to build")
    # Meshes that come back from Blender unedited reuse the original vertex data exactly,
    # so only edited meshes can change size (Blender splits a few extra vertices).
    originals = decode(template)
    kept = 0
    for k, s in enumerate(subs):
        if 0 <= s.source_index < len(originals):
            o = originals[s.source_index]
            if o.lod == s.lod and same_geometry(o, s):
                passthrough = copy.copy(o)
                passthrough.material = s.material
                passthrough.layout = s.layout
                subs[k] = passthrough
                kept += 1
    if kept:
        report.append(f"{kept} of {len(subs)} meshes unchanged (original data kept)")
    if problems:
        raise BuildError(
            f"{len(problems)} change(s) in {meta['name']} cannot work in the game:\n  - " + "\n  - ".join(problems)
            + "\n(Use --experimental to build anyway, e.g. for testing.)"
        )
    if strict and size_budget:
        budget = len((pkg / "template.binfbx").read_bytes())
        try:
            out, notes = model.fit_to_size(template, subs, out_materials, budget)
        except (model.SizeBudgetError, model.SlotError) as e:
            raise BuildError(f"{meta['name']}: {e}") from None
        report.extend(notes)
    elif strict:
        # sizes will be described by a generated .packmeta: keep the mesh slots only
        try:
            subs, notes = model.conform(template, subs, out_materials, allow_new_meshes=True)
        except model.SlotError as e:
            raise BuildError(f"{meta['name']}: {e}") from None
        report.extend(notes)
        out = model.rebuild(template, subs, out_materials)
    else:
        out = model.rebuild(template, subs, out_materials)
    result.binfbx = out.to_bytes()
    report.append(f"meshes: {len(out.meshes)} (LODs {sorted({m.lod for m in out.meshes})}), materials: {len(out.materials)}")
    return result


def _signature(positions, uv0, bone_indices, bone_weights) -> np.ndarray:
    """Per-vertex rows identifying a vertex exactly (positions are exact through the
    Blender round trip; UVs and the dominant weight are compared on the game's grid)."""
    rows = [np.ascontiguousarray(positions, np.float32).view(np.int32).reshape(len(positions), 3)]
    rows.append(np.round(np.asarray(uv0, np.float64) * 4096).astype(np.int64).astype(np.int32))
    if bone_indices is not None:
        top = np.argmax(bone_weights, axis=1)
        rows.append(np.take_along_axis(np.asarray(bone_indices), top[:, None], 1).astype(np.int32))
        rows.append(np.round(np.take_along_axis(bone_weights, top[:, None], 1) * 255).astype(np.int32))
    return np.concatenate(rows, axis=1)


def same_geometry(a: Submesh, b: Submesh) -> bool:
    """True if ``b`` (from Blender) is the unedited version of ``a`` (from the game)."""
    if len(a.faces) != len(b.faces) or not a.uvs or not b.uvs:
        return False
    ka = _signature(a.positions, a.uvs[0], a.bone_indices, a.bone_weights)
    kb = _signature(b.positions, b.uvs[0], b.bone_indices, b.bone_weights)
    ua, ia = np.unique(ka, axis=0, return_index=True)
    ub, ib = np.unique(kb, axis=0, return_index=True)
    if ua.shape != ub.shape or not np.array_equal(ua, ub):
        return False
    na, nb = a.normals[ia].astype(np.float64), b.normals[ib].astype(np.float64)
    return bool(((na * nb).sum(1) > 0.98).all())


def _extra_from_template(t: BinFbx, idx: int) -> dict:
    if idx < 0:
        return {}
    m = t.meshes[idx]
    return {"unk1": m.unk1, "unk2": m.unk2, "unk3": m.unk3}


def _derived_hash(base: int, name: str) -> int:
    """New material id: keep the low 16 flag-like bits of the base, hash the rest."""
    h = int.from_bytes(hashlib.sha1(name.encode()).digest()[:8], "little")
    return (h & ~0xFFFF | base & 0xFFFF) & 0xFFFFFFFFFFFFFFFF
