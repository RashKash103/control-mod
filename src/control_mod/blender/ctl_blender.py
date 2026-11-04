"""Blender side of control-mod. Runs *inside* Blender; only uses bpy, numpy and the stdlib.

Invoked by the ``control-mod`` CLI as::

    blender --background --factory-startup --python ctl_blender.py -- import <pkg_dir> <out.blend>
    blender --background <file.blend> --python ctl_blender.py -- export <pkg_dir> [--lods authored|copy|decimate]
    blender --background <file.blend> --python ctl_blender.py -- render <out.png> [--view front|back|side|face] [--lod N]

Conventions shared with ``control_mod.package`` (game <-> Blender conversion is done there):

* each game mesh is one object; its LOD comes from the collection it lives in
  (``LOD0``, ``LOD1`` ...); a mesh with several material slots is split per slot
* objects whose name starts with ``_`` are ignored
* ``ctl_*`` custom properties link objects/materials back to the original file
* image nodes carry the material parameter they feed in ``node["ctl_param"]``
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import bpy
import numpy as np
from mathutils import Matrix, Vector

COLOR_PARAMS = ("g_sColorMap", "g_sBaseColorMap", "g_sDiffuseMap")


def log(*a):
    print("[control-mod]", *a, flush=True)


# =============================================================================== import


def _material(info: dict, meta: dict, pkg: Path) -> bpy.types.Material:
    mat = bpy.data.materials.new(info["name"])
    mat["ctl_material"] = info["name"]
    mat["ctl_index"] = info["index"]
    # Blender appends .001 to repeated names; remembering the name it chose lets the
    # exporter tell the original material apart from user-made copies (which keep props)
    mat["ctl_blender_name"] = mat.name
    mat["ctl_shader"] = info["shader"]
    if not mat.use_nodes:
        mat.use_nodes = True
    nt = mat.node_tree
    bsdf = next(n for n in nt.nodes if n.type == "BSDF_PRINCIPLED")
    x = -700
    y = 300
    alpha_from = None
    for param, apath in info["textures"].items():
        rec = meta["textures"].get(apath) or {}
        png = rec.get("png")
        node = nt.nodes.new("ShaderNodeTexImage")
        node.label = param
        node.name = param
        node["ctl_param"] = param
        node.location = (x, y)
        y -= 280
        if not png:
            continue
        img = bpy.data.images.load(str((pkg / png).resolve()), check_existing=True)
        node.image = img
        shared = rec.get("shared_with") or []
        if shared:
            # make it hard to miss that painting this image changes other models too
            node["ctl_shared_with"] = len(shared)
            node.label = f"SHARED x{len(shared)}: {param}"
            node.use_custom_color = True
            node.color = (0.8, 0.15, 0.1)
            if not img.name.startswith("SHARED"):
                img.name = f"SHARED({len(shared)})_{img.name}"
        if param in COLOR_PARAMS:
            tint = next((p["value"] for p in info["params"] if p["name"] == "g_vColorMultiplier"), None)
            if tint and any(abs(c - 1.0) > 1e-3 for c in tint[:3]):
                mix = nt.nodes.new("ShaderNodeMix")
                mix.data_type = "RGBA"
                mix.blend_type = "MULTIPLY"
                mix.label = "g_vColorMultiplier (preview only)"
                mix.location = (x + 350, y + 280)
                mix.inputs["Factor"].default_value = 1.0
                mix.inputs[7].default_value = (*tint[:3], 1.0)
                nt.links.new(node.outputs["Color"], mix.inputs[6])
                nt.links.new(mix.outputs[2], bsdf.inputs["Base Color"])
            else:
                nt.links.new(node.outputs["Color"], bsdf.inputs["Base Color"])
            nt.nodes.active = node  # Workbench/solid view previews the active image node
            alpha_from = alpha_from or node
        else:
            img.colorspace_settings.name = "Non-Color"
        if param == "g_sNormalMap":
            nm = nt.nodes.new("ShaderNodeNormalMap")
            nm.location = (x + 350, y + 280)
            nm.uv_map = "UV0"
            nt.links.new(node.outputs["Color"], nm.inputs["Color"])
            nt.links.new(nm.outputs["Normal"], bsdf.inputs["Normal"])
        if param == "g_sAlphaTestSampler":
            alpha_from = node
    alpha_test = any(p["name"] == "g_bAlphaTest" and isinstance(p["value"], list) and p["value"][:1] == [1] for p in info["params"])
    if alpha_from is not None and (alpha_test or info["shader"] == "hair"):
        nt.links.new(alpha_from.outputs["Alpha"], bsdf.inputs["Alpha"])
    return mat


def _armature(meta: dict, coll) -> bpy.types.Object:
    arm = bpy.data.armatures.new(meta["name"] + "_skeleton")
    obj = bpy.data.objects.new(meta["name"] + "_skeleton", arm)
    obj["ctl_model"] = meta["name"]
    coll.objects.link(obj)
    arm.display_type = "STICK"
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode="EDIT")
    ebs = []
    for b in meta["bones"]:
        eb = arm.edit_bones.new(b["name"])
        eb.head = Vector(b["head"])
        eb.tail = Vector(b["tail"])
        m = Matrix(b["matrix"])
        eb.align_roll(m.col[2].to_3d())
        ebs.append(eb)
    for eb, b in zip(ebs, meta["bones"]):
        if b["parent"] >= 0:
            eb.parent = ebs[b["parent"]]
    bpy.ops.object.mode_set(mode="OBJECT")
    return obj


def do_import(pkg: Path, out_blend: Path):
    meta = json.loads((pkg / "model.json").read_text())
    geo = np.load(pkg / "geometry.npz")
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o)
    scene = bpy.context.scene
    root = bpy.data.collections.new(meta["name"])
    scene.collection.children.link(root)
    scene["ctl_package"] = str(pkg.resolve())
    arm = _armature(meta, root) if meta["bones"] else None

    mats = [_material(m, meta, pkg) for m in meta["materials"]]
    lod_colls = {}
    for i, mi in enumerate(meta["meshes"]):
        lod = mi["lod"]
        if lod not in lod_colls:
            c = bpy.data.collections.new(f"LOD{lod}")
            root.children.link(c)
            lod_colls[lod] = c
        a = {k[len(f"m{i}_"):]: geo[k] for k in geo.files if k.startswith(f"m{i}_")}
        verts, faces = a["verts"], a["faces"]
        me = bpy.data.meshes.new(mi["name"])
        me.from_pydata(verts.tolist(), [], faces.tolist())
        aligned = len(me.loops) == 3 * len(faces)
        for k in range(mi["uv_count"] if aligned else 0):
            uv = me.uv_layers.new(name=f"UV{k}")
            uv.data.foreach_set("uv", a[f"corner_uv{k}"].ravel())
        for k in range(mi["color_count"] if aligned else 0):
            ca = me.color_attributes.new(f"Col{k}", "FLOAT_COLOR", "CORNER")
            ca.data.foreach_set("color", a[f"corner_color{k}"].ravel())
        if "islands" in a:
            at = me.attributes.new("ctl_island", "INT", "POINT")
            at.data.foreach_set("value", a["islands"].ravel())
        if len(me.loops) == len(a["corner_normals"]):
            me.normals_split_custom_set(a["corner_normals"].tolist())
        else:  # Blender dropped invalid faces; corner data no longer lines up
            log(f"warning: {mi['name']}: {len(faces) - len(me.polygons)} invalid faces dropped; using smooth normals")
        me.materials.append(mats[mi["material"]])
        ob = bpy.data.objects.new(mi["name"], me)
        ob["ctl_source_index"] = mi["source_index"]
        lod_colls[lod].objects.link(ob)
        if arm is not None and "bone_indices" in a:
            ob.parent = arm
            mod = ob.modifiers.new("Armature", "ARMATURE")
            mod.object = arm
            bi, bw = a["bone_indices"], a["bone_weights"]
            names = [b["name"] for b in meta["bones"]]
            for bone in np.unique(bi[bw > 0]):
                vg = ob.vertex_groups.new(name=names[bone])
                sel = (bi == bone) & (bw > 0)
                rows, cols = np.nonzero(sel)
                ws = np.round(bw[rows, cols], 4)
                for w in np.unique(ws):
                    vg.add(rows[ws == w].tolist(), float(w), "REPLACE")
        log(f"imported {mi['name']}: {len(verts)} verts, {len(faces)} tris")
    # only LOD0 visible by default
    for lod, c in lod_colls.items():
        if lod > 0:
            layer = bpy.context.view_layer.layer_collection.children[root.name].children[c.name]
            layer.hide_viewport = True
    _readme(meta)
    out_blend.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(out_blend), relative_remap=True)
    log("saved", out_blend)


def _readme(meta: dict):
    """A text block (Scripting / Text Editor) with the rules and the shared textures."""
    lines = [
        f"control-mod - {meta['name']}",
        "",
        "WHAT THE GAME ACCEPTS (see README):",
        "  * edit, add or delete geometry in LOD0..LOD3; weight new parts to existing bones",
        "  * use the existing materials only (no copies / new materials)",
        "  * repaint textures in place (the PNGs in textures/), keeping their resolution",
        "  * the built model must fit the original file size; LOD1-3 are simplified",
        "    automatically to make room",
        "",
    ]
    shared = sorted(
        ((r.get("png") or "", r.get("shared_with") or []) for r in meta["textures"].values() if r.get("shared_with") and r.get("png")),
        key=lambda t: -len(t[1]),
    )
    if shared:
        lines.append("SHARED TEXTURES - editing these also changes other models:")
        for png, others in shared:
            names = ", ".join(o.rsplit("/", 1)[-1].rsplit(".", 1)[0] for o in others[:4])
            more = f" and {len(others) - 4} more" if len(others) > 4 else ""
            lines.append(f"  {png:<55} {len(others):>4} other model(s): {names}{more}")
    else:
        lines.append("None of this model's textures are used by other models.")
    txt = bpy.data.texts.new("control-mod_READ_ME")
    txt.write("\n".join(lines) + "\n")


# =============================================================================== export


def _lod_of(ob) -> int | None:
    for c in ob.users_collection:
        if c.name.upper().startswith("LOD") and c.name[3:].split(".")[0].isdigit():
            return int(c.name[3:].split(".")[0])
    return None


def _image_path(img) -> str:
    return bpy.path.abspath(img.filepath, library=img.library) if img.filepath else ""


def _dump_object(ob, arm, lod: int, decimate_ratio: float | None, meshes: list, arrays: dict, used_mats: dict):
    disabled = []
    for m in ob.modifiers:
        if m.type == "ARMATURE" and m.show_viewport:
            m.show_viewport = False
            disabled.append(m)
    dec = None
    if decimate_ratio is not None and decimate_ratio < 0.999:
        dec = ob.modifiers.new("_ctl_decimate", "DECIMATE")
        dec.ratio = max(decimate_ratio, 0.01)
    try:
        dg = bpy.context.evaluated_depsgraph_get()
        ev = ob.evaluated_get(dg)
        me = ev.to_mesh()
        me.calc_loop_triangles()
        nv, nt, nl = len(me.vertices), len(me.loop_triangles), len(me.loops)
        if nt == 0:
            ev.to_mesh_clear()
            return
        rel = (arm.matrix_world.inverted() @ ob.matrix_world) if arm else ob.matrix_world
        co = np.empty(nv * 3, np.float32)
        me.vertices.foreach_get("co", co)
        co = co.reshape(-1, 3) @ np.array(rel.to_3x3()).T + np.array(rel.translation)
        tri_v = np.empty(nt * 3, np.int32)
        me.loop_triangles.foreach_get("vertices", tri_v)
        tri_l = np.empty(nt * 3, np.int32)
        me.loop_triangles.foreach_get("loops", tri_l)
        tri_m = np.empty(nt, np.int32)
        me.loop_triangles.foreach_get("material_index", tri_m)
        cn = np.empty(nl * 3, np.float32)
        me.corner_normals.foreach_get("vector", cn)
        nrm_m = np.array(rel.to_3x3().inverted_safe().transposed())
        cn = cn.reshape(-1, 3) @ nrm_m.T
        cn /= np.maximum(np.linalg.norm(cn, axis=1, keepdims=True), 1e-12)
        if rel.determinant() < 0:  # mirrored object: keep winding outward
            tri_v = tri_v.reshape(-1, 3)[:, [0, 2, 1]].ravel()
            tri_l = tri_l.reshape(-1, 3)[:, [0, 2, 1]].ravel()
        uv_layers = sorted(me.uv_layers, key=lambda u: (not u.name.upper().startswith("UV"), u.name))
        uvs = []
        for u in uv_layers:
            d = np.empty(nl * 2, np.float32)
            u.data.foreach_get("uv", d)
            uvs.append(d.reshape(-1, 2))
        cols = []
        for ca in me.color_attributes:
            d = np.empty(len(ca.data) * 4, np.float32)
            ca.data.foreach_get("color", d)
            d = d.reshape(-1, 4)
            cols.append(d if ca.domain == "CORNER" else d[np.array([l.vertex_index for l in me.loops])])
        groups = [g.name for g in ob.vertex_groups]
        gi = np.full((nv, 8), -1, np.int32)
        gw = np.zeros((nv, 8), np.float32)
        maxk = 0
        for v in me.vertices:
            gs = sorted(((g.weight, g.group) for g in v.groups if g.weight > 0), reverse=True)[:8]
            for k, (w, g) in enumerate(gs):
                gi[v.index, k] = g
                gw[v.index, k] = w
            maxk = max(maxk, len(gs))
        if ob.parent is not None and ob.parent_type == "BONE" and ob.parent_bone:  # rigid child of a bone
            if ob.parent_bone not in groups:
                groups.append(ob.parent_bone)
            none = gw.sum(1) == 0
            gi[none, 0] = groups.index(ob.parent_bone)
            gw[none, 0] = 1.0
            maxk = max(maxk, 1)
        isl = me.attributes.get("ctl_island")
        islands = None
        if isl is not None and isl.domain == "POINT" and isl.data_type == "INT":
            islands = np.empty(nv, np.int32)
            isl.data.foreach_get("value", islands)

        for slot in np.unique(tri_m):
            sel = np.nonzero(tri_m == slot)[0]
            mat = ob.material_slots[slot].material if slot < len(ob.material_slots) else None
            if mat is None:
                log(f"warning: {ob.name} has faces without a material; skipped")
                continue
            used_mats[mat.name] = mat
            corners_l = tri_l.reshape(-1, 3)[sel].ravel()
            faces_v = tri_v.reshape(-1, 3)[sel]
            used, local = np.unique(faces_v.ravel(), return_inverse=True)
            i = len(meshes)
            pre = f"m{i}_"
            arrays[pre + "verts"] = co[used].astype(np.float32)
            arrays[pre + "faces"] = local.reshape(-1, 3).astype(np.int32)
            arrays[pre + "corner_normals"] = cn[corners_l].astype(np.float32)
            for k, d in enumerate(uvs):
                arrays[pre + f"corner_uv{k}"] = d[corners_l]
            for k, d in enumerate(cols):
                arrays[pre + f"corner_color{k}"] = d[corners_l]
            if maxk:
                arrays[pre + "bone_indices"] = gi[used, : max(maxk, 1)]
                arrays[pre + "bone_weights"] = gw[used, : max(maxk, 1)]
            if islands is not None:
                arrays[pre + "islands"] = islands[used]
            meshes.append({
                "name": ob.name if len(np.unique(tri_m)) == 1 else f"{ob.name}:{mat.name}",
                "lod": lod,
                "material": mat.name,
                "source_index": int(ob.get("ctl_source_index", -1)) if len(np.unique(tri_m)) == 1 or slot == 0 else -1,
                "uv_count": len(uvs),
                "color_count": len(cols),
                "groups": groups,
                # decimation scrambles custom normals: let control-mod recompute smooth ones
                "smooth_normals": dec is not None,
            })
        ev.to_mesh_clear()
    finally:
        if dec is not None:
            ob.modifiers.remove(dec)
        for m in disabled:
            m.show_viewport = True


def do_export(pkg: Path, lods: str):
    meta = json.loads((pkg / "model.json").read_text())
    arm = next((o for o in bpy.data.objects if o.type == "ARMATURE"), None)
    objs = [o for o in bpy.data.objects if o.type == "MESH" and not o.name.startswith("_")]
    by_lod: dict[int, list] = {}
    for o in objs:
        lod = _lod_of(o)
        if lod is None:
            log(f"note: {o.name} is not in a LOD collection; treating as LOD0")
            lod = 0
        by_lod.setdefault(lod, []).append(o)
    if 0 not in by_lod:
        raise SystemExit("no LOD0 meshes found")
    lod_count = meta["lod_count"]
    tmpl_faces = {}
    for m in meta["meshes"]:
        tmpl_faces[m["lod"]] = tmpl_faces.get(m["lod"], 0) + m["face_count"]
    meshes, arrays, used = [], {}, {}
    plan = []
    if lods == "authored":
        plan = [(lod, o, None) for lod, os_ in sorted(by_lod.items()) for o in os_]
    else:
        for lod in range(lod_count):
            ratio = None
            if lods == "decimate" and lod > 0:
                ratio = tmpl_faces.get(lod, 1) / max(tmpl_faces.get(0, 1), 1)
            plan += [(lod, o, ratio) for o in by_lod[0]]
    for lod, o, ratio in plan:
        _dump_object(o, arm, lod, ratio, meshes, arrays, used)
        if lods != "authored" and lod > 0:
            meshes[-1]["source_index"] = -1
    mats = []
    for name, mat in used.items():
        tex = {}
        if mat.use_nodes:
            for n in mat.node_tree.nodes:
                if n.type == "TEX_IMAGE" and n.get("ctl_param") and n.image is not None:
                    tex[n["ctl_param"]] = _image_path(n.image)
        mats.append({
            "name": name,
            "ctl_material": mat.get("ctl_material"),
            "ctl_index": mat.get("ctl_index"),
            # files made before ctl_blender_name existed: the original kept the game name
            "original": mat.get("ctl_blender_name", mat.get("ctl_material")) == name,
            "textures": tex,
        })
    out = pkg / "blender_export"
    out.mkdir(exist_ok=True)
    (out / "scene.json").write_text(json.dumps({"meshes": meshes, "materials": mats, "lods": lods}, indent=1))
    np.savez_compressed(out / "scene.npz", **arrays)
    log(f"exported {len(meshes)} meshes, {len(mats)} materials -> {out}")


# =============================================================================== render


def do_render(out_png: Path, view: str, lod: int):
    scene = bpy.context.scene
    for c in bpy.data.collections:
        if c.name.upper().startswith("LOD"):
            show = c.name.upper() == f"LOD{lod}"
            def walk(lc):
                for ch in lc.children:
                    if ch.collection == c:
                        ch.hide_viewport = not show
                        ch.exclude = not show
                    walk(ch)
            walk(bpy.context.view_layer.layer_collection)
            c.hide_render = not show
    meshes = [o for o in scene.objects if o.type == "MESH" and o.visible_get()]
    if not meshes:
        raise SystemExit("nothing to render")
    pts = np.array([list(o.matrix_world @ Vector(c)) for o in meshes for c in o.bound_box])
    lo, hi = pts.min(0), pts.max(0)
    center = Vector(((lo + hi) / 2).tolist())
    size = float(max(hi - lo))
    if view == "face":
        center = Vector((center.x, center.y, hi[2] - 0.12))
        size = 0.35
    cam_data = bpy.data.cameras.new("_ctl_cam")
    cam_data.type = "ORTHO"
    cam_data.ortho_scale = size * 1.1
    cam = bpy.data.objects.new("_ctl_cam", cam_data)
    scene.collection.objects.link(cam)
    d = {"front": Vector((0, -1, 0)), "back": Vector((0, 1, 0)), "side": Vector((1, 0, 0)), "face": Vector((0, -1, 0))}[view]
    cam.location = center + d * 10
    cam.rotation_euler = (-d).to_track_quat("-Z", "Y").to_euler()
    scene.camera = cam
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.display.shading.light = "STUDIO"
    scene.display.shading.color_type = "TEXTURE"
    scene.render.resolution_x = 900
    scene.render.resolution_y = 1100 if view != "side" else 1100
    scene.render.film_transparent = False
    scene.render.filepath = str(out_png)
    bpy.ops.render.render(write_still=True)
    log("rendered", out_png)


def main():
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    if not argv:
        raise SystemExit(__doc__)
    cmd, rest = argv[0], argv[1:]
    opts = {}
    pos = []
    it = iter(rest)
    for a in it:
        if a.startswith("--"):
            opts[a[2:]] = next(it)
        else:
            pos.append(a)
    if cmd == "import":
        do_import(Path(pos[0]), Path(pos[1]))
    elif cmd == "export":
        do_export(Path(pos[0]), opts.get("lods", "authored"))
    elif cmd == "render":
        do_render(Path(pos[0]), opts.get("view", "front"), int(opts.get("lod", 0)))
    else:
        raise SystemExit(f"unknown command {cmd}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        import traceback

        traceback.print_exc()
        sys.exit(1)
