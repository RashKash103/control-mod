"""control-mod tools for Blender: a "Control Mod" tab in the 3D view's sidebar (N panel).

Works inside a .blend made by ``control-mod add``. Only uses bpy, numpy and the stdlib.

* **Adopt selected** - turns imported objects (glb/fbx/obj, a kitbash part ...) into
  parts the build accepts: game-material copies with their textures converted to the
  right slots and resolutions, weights from the body, the game skeleton, a LOD collection.
* **Check** - lists everything the build would refuse or that may not look right.
* **Copy material** - a new game material from the active one (keeps shader + params).
* **New texture** - gives a material slot its own new texture (same resolution).
* **Save images** - saves painted images, writes packed images to files.
* **LOD buttons** - show one LOD at a time.
* **Save & build** - saves and runs ``control-mod build`` for this mod.

Ways to load it (all do the same):

* ``control-mod open <mod>`` starts Blender with it;
* every .blend made by control-mod contains it as the text block ``control_mod_tools.py``
  (registered on load when Blender may run scripts; otherwise open it in the Text Editor
  and press Run Script);
* *Edit > Preferences > Add-ons > Install from Disk* with this file;
* headless::

    blender --background <file.blend> --python control_mod_tools.py -- adopt [--import FILE]
        [--objects A,B] [--material NAME] [--lod N] [--weights transfer|auto|bone|keep]
        [--bone NAME] [--flip-green] [--save]
    blender --background <file.blend> --python control_mod_tools.py -- check
"""


import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import bpy
import numpy as np

bl_info = {
    "name": "control-mod tools",
    "description": "Adopt imported models, check and build Control outfit mods",
    "author": "control-mod",
    "version": (0, 2, 0),
    "blender": (4, 2, 0),
    "location": "3D View > Sidebar > Control Mod",
    "category": "Import-Export",
}

TEXT_NAME = "control_mod_tools.py"
COLOR_PARAMS = ("g_sColorMap", "g_sBaseColorMap", "g_sDiffuseMap")
# tiling (UV-independent) maps a copied material keeps as they are
KEEP_PARAMS = ("g_sBaseDetailMap",)


def log(*a):
    print("[control-mod]", *a, flush=True)


# =============================================================================== helpers


def game_armature():
    obs = [o for o in bpy.data.objects if o.type == "ARMATURE"]
    return next((o for o in obs if o.get("ctl_model")), obs[0] if obs else None)


def is_game_material(mat) -> bool:
    return mat is not None and mat.get("ctl_material") is not None


def is_original_material(mat) -> bool:
    return is_game_material(mat) and mat.get("ctl_blender_name", mat.get("ctl_material")) == mat.name


def original_of(mat):
    """The game's own material a (copied) game material is based on."""
    idx = mat.get("ctl_index")
    for m in bpy.data.materials:
        if is_original_material(m) and m.get("ctl_index") == idx:
            return m
    return None


def slot_nodes(mat) -> dict:
    if not mat or not mat.use_nodes:
        return {}
    return {n["ctl_param"]: n for n in mat.node_tree.nodes if n.type == "TEX_IMAGE" and n.get("ctl_param")}


def lod_of(ob) -> int | None:
    for c in ob.users_collection:
        if c.name.upper().startswith("LOD") and c.name[3:].split(".")[0].isdigit():
            return int(c.name[3:].split(".")[0])
    return None


def lod_collection(lod: int):
    name = f"LOD{lod}"
    if name in bpy.data.collections:
        return bpy.data.collections[name]
    c = bpy.data.collections.new(name)
    arm = game_armature()
    parent = arm.users_collection[0] if arm else bpy.context.scene.collection
    parent.children.link(c)
    return c


def blend_dir() -> Path:
    if not bpy.data.filepath:
        raise RuntimeError("save the .blend first (images are written next to it)")
    return Path(bpy.data.filepath).parent


def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", s).strip("_").lower() or "part"


def unique_material_name(base: str) -> str:
    name, i = base, 1
    while name in bpy.data.materials:
        i += 1
        name = f"{base}_{i}"
    return name


def image_pixels(img) -> np.ndarray:
    w, h = img.size
    px = np.empty(w * h * 4, np.float32)
    img.pixels.foreach_get(px)
    return px.reshape(h, w, 4)


def resize(px: np.ndarray, w: int, h: int) -> np.ndarray:
    """Bilinear resize of an (h, w, 4) array."""
    sh, sw = px.shape[:2]
    if (sw, sh) == (w, h):
        return px
    ys = np.clip((np.arange(h) + 0.5) * sh / h - 0.5, 0, sh - 1)
    xs = np.clip((np.arange(w) + 0.5) * sw / w - 0.5, 0, sw - 1)
    y0, x0 = np.floor(ys).astype(int), np.floor(xs).astype(int)
    y1, x1 = np.minimum(y0 + 1, sh - 1), np.minimum(x0 + 1, sw - 1)
    fy, fx = (ys - y0)[:, None, None], (xs - x0)[None, :, None]
    top = px[y0][:, x0] * (1 - fx) + px[y0][:, x1] * fx
    bot = px[y1][:, x0] * (1 - fx) + px[y1][:, x1] * fx
    return (top * (1 - fy) + bot * fy).astype(np.float32)


def linear_to_srgb(c):
    c = np.clip(np.asarray(c, np.float32), 0, 1)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * np.power(c, 1 / 2.4) - 0.055)


def write_image(name: str, px: np.ndarray, path: Path, non_color: bool):
    h, w = px.shape[:2]
    path.parent.mkdir(parents=True, exist_ok=True)
    img = bpy.data.images.get(name)
    if img is None or tuple(img.size) != (w, h):
        img = bpy.data.images.new(name, w, h, alpha=True)
    img.pixels.foreach_set(np.ascontiguousarray(px, np.float32).ravel())
    img.filepath_raw = str(path)
    img.file_format = "PNG"
    img.save()
    if non_color:
        img.colorspace_settings.name = "Non-Color"
    return img


def relpath(p: Path) -> str:
    try:
        return bpy.path.relpath(str(p))
    except ValueError:  # other drive
        return str(p)


# =============================================================================== material conversion


def _trace_image(sock, depth=5):
    """(image node, output socket name) feeding ``sock``, looking through a few nodes."""
    if sock is None or not sock.is_linked or depth == 0:
        return None
    link = sock.links[0]
    node = link.from_node
    if node.type == "TEX_IMAGE":
        return node, link.from_socket.name
    for inp in node.inputs:
        if inp.type in ("RGBA", "VECTOR", "VALUE") and inp.is_linked:
            found = _trace_image(inp, depth - 1)
            if found:
                return found
    return None


def _channel_source(sock):
    """(image, channel index or None) for a scalar input such as Roughness."""
    if sock is None or not sock.is_linked:
        return None, None
    link = sock.links[0]
    node = link.from_node
    if node.type in ("SEPARATE_COLOR", "SEPARATE_RGB", "SEPARATE_XYZ"):
        ch = {"Red": 0, "R": 0, "X": 0, "Green": 1, "G": 1, "Y": 1, "Blue": 2, "B": 2, "Z": 2}.get(link.from_socket.name, 0)
        found = _trace_image(node.inputs[0])
        return (found[0].image, ch) if found and found[0].image else (None, None)
    found = _trace_image(sock)
    if found and found[0].image:
        return found[0].image, 3 if found[1] == "Alpha" else 0
    return None, None


def read_source_material(mat) -> dict:
    """What an imported (Principled BSDF) material looks like: images and constants."""
    out = {"color": None, "color_value": (0.8, 0.8, 0.8, 1.0), "normal": None,
           "rough": (None, None), "rough_value": 0.5, "alpha": False}
    if not mat or not mat.use_nodes:
        if mat is not None:
            out["color_value"] = tuple(mat.diffuse_color)
        return out
    bsdf = next((n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
    if bsdf is None:
        found = next((n for n in mat.node_tree.nodes if n.type == "TEX_IMAGE" and n.image), None)
        out["color"] = found.image if found else None
        return out
    found = _trace_image(bsdf.inputs["Base Color"])
    if found and found[0].image:
        out["color"] = found[0].image
    else:
        out["color_value"] = tuple(bsdf.inputs["Base Color"].default_value)
    nsock = bsdf.inputs["Normal"]
    if nsock.is_linked:
        nnode = nsock.links[0].from_node
        src = _trace_image(nnode.inputs.get("Color")) if nnode.type == "NORMAL_MAP" else None
        if src and src[0].image:
            out["normal"] = src[0].image
    out["rough"] = _channel_source(bsdf.inputs["Roughness"])
    out["rough_value"] = float(bsdf.inputs["Roughness"].default_value)
    out["alpha"] = bsdf.inputs["Alpha"].is_linked
    return out


def default_base_material():
    """A sensible game material to base new materials on: a standard (non-skin, non-hair),
    non-alpha-tested, untinted material, the most used one in LOD0."""
    faces = {}
    for ob in bpy.data.objects:
        if ob.type == "MESH" and lod_of(ob) == 0:
            for s in ob.material_slots:
                if is_original_material(s.material):
                    faces[s.material.name] = faces.get(s.material.name, 0) + len(ob.data.polygons)

    def score(m):
        nodes = slot_nodes(m)
        tinted = any(n.label.startswith("g_vColorMultiplier") for n in m.node_tree.nodes) if m.use_nodes else False
        return (m.get("ctl_shader") == "standardmaterial", "g_sAlphaTestSampler" not in nodes,
                not tinted, "g_sColorMap" in nodes, faces.get(m.name, 0))

    cands = [m for m in bpy.data.materials if is_original_material(m)]
    return max(cands, key=score) if cands else None


def convert_material(src_mat, base, name: str, flip_green: bool = False) -> tuple:
    """A copy of game material ``base`` that looks like ``src_mat``. Every texture slot
    gets a new image at the resolution of the slot it replaces."""
    info = read_source_material(src_mat)
    mat = base.copy()  # keeps the ctl_* properties: a new game material based on ``base``
    mat.name = unique_material_name(safe_name(name))
    folder = blend_dir() / "textures" / "adopted"
    notes = []
    for param, node in slot_nodes(mat).items():
        if param in KEEP_PARAMS or node.image is None:
            continue
        w, h = node.image.size
        if not w or not h:
            notes.append(f"{param}: can't read the original image; kept")
            continue
        non_color = param not in COLOR_PARAMS
        if param in COLOR_PARAMS:
            if info["color"] is not None:
                px = resize(image_pixels(info["color"]), w, h)
                if not info["alpha"]:
                    px[..., 3] = 1.0
                what = info["color"].name
            else:
                px = np.empty((h, w, 4), np.float32)
                px[..., :3] = linear_to_srgb(info["color_value"][:3])
                px[..., 3] = 1.0
                what = "flat base colour"
        elif param == "g_sNormalMap":
            if info["normal"] is not None:
                px = resize(image_pixels(info["normal"]), w, h)
                if flip_green:
                    px[..., 1] = 1.0 - px[..., 1]
                what = info["normal"].name + (" (green flipped)" if flip_green else "")
            else:
                px = np.empty((h, w, 4), np.float32)
                px[...] = (0.5, 0.5, 1.0, 1.0)
                what = "flat normal"
        elif param == "g_sSmoothnessMap":
            img, ch = info["rough"]
            px = np.ones((h, w, 4), np.float32)
            if img is not None:
                rough = resize(image_pixels(img), w, h)[..., ch]
                what = f"1 - roughness ({img.name})"
            else:
                rough = np.float32(info["rough_value"])
                what = f"1 - roughness {info['rough_value']:.2f}"
            px[..., :3] = (1.0 - rough)[..., None] if np.ndim(rough) else 1.0 - rough
        else:
            # no equivalent in the source: the base texture's average, so nothing of the
            # base's UV layout shows up on the new part
            mean = image_pixels(node.image).reshape(-1, 4).mean(0)
            px = np.empty((h, w, 4), np.float32)
            px[...] = mean
            what = f"average of {node.image.name}"
        img_name = f"{mat.name}_{param}"
        node.image = write_image(img_name, px, folder / f"{img_name}.png", non_color)
        node.image.filepath = relpath(folder / f"{img_name}.png")
        notes.append(f"{param} <- {what} ({w}x{h})")
    return mat, notes


# =============================================================================== adopt


def _bone_names() -> set:
    arm = game_armature()
    return {b.name for b in arm.data.bones} if arm else set()


def _weight_source(exclude: set):
    """A temporary joined copy of the skinned LOD0 game meshes (rest pose)."""
    srcs = [o for o in bpy.data.objects if o.type == "MESH" and lod_of(o) == 0 and o.vertex_groups
            and o not in exclude and not o.name.startswith("_") and o.get("ctl_source_index") is not None]
    if not srcs:
        return None
    copies = []
    for o in srcs:
        c = o.copy()
        c.data = o.data.copy()
        c.modifiers.clear()
        bpy.context.scene.collection.objects.link(c)
        copies.append(c)
    with bpy.context.temp_override(active_object=copies[0], object=copies[0],
                                   selected_objects=copies, selected_editable_objects=copies):
        bpy.ops.object.join()
    copies[0].name = "_ctl_weight_source"
    return copies[0]


def _apply_modifier(ob, mod):
    with bpy.context.temp_override(object=ob, active_object=ob, selected_objects=[ob]):
        bpy.ops.object.modifier_apply(modifier=mod.name)


def transfer_weights(ob, source):
    for g in source.vertex_groups:
        if g.name not in ob.vertex_groups:
            ob.vertex_groups.new(name=g.name)
    mod = ob.modifiers.new("_ctl_weights", "DATA_TRANSFER")
    mod.object = source
    mod.use_vert_data = True
    mod.data_types_verts = {"VGROUP_WEIGHTS"}
    mod.vert_mapping = "POLYINTERP_NEAREST"
    mod.layers_vgroup_select_src = "ALL"
    mod.layers_vgroup_select_dst = "NAME"
    # put it first so it sees the mesh as it is (other modifiers are applied at build time)
    while ob.modifiers[0] != mod:
        with bpy.context.temp_override(object=ob):
            bpy.ops.object.modifier_move_up(modifier=mod.name)
    _apply_modifier(ob, mod)
    # drop groups that got no weight
    n = len(ob.data.vertices)
    used = set()
    for v in ob.data.vertices:
        used.update(g.group for g in v.groups if g.weight > 1e-4)
    unused = [g.name for g in ob.vertex_groups if g.index not in used]  # names: indices shift on removal
    for name in unused:
        ob.vertex_groups.remove(ob.vertex_groups[name])
    with bpy.context.temp_override(object=ob, active_object=ob):
        if ob.vertex_groups:
            bpy.ops.object.vertex_group_normalize_all(lock_active=False)
    return n


def adopt(objects, base=None, lod: int = 0, weights: str = "transfer", bone: str = "C_spine2_JNT",
          flip_green: bool = False) -> list[str]:
    """Make ``objects`` (and the meshes under any selected non-game armature/empty) parts
    of this model. Returns a report."""
    arm = game_armature()
    if arm is None:
        raise RuntimeError("no game skeleton in this file - open a .blend made by control-mod add")
    base = base or default_base_material()
    if base is None:
        raise RuntimeError("no game materials in this file")
    if not is_original_material(base):
        base = original_of(base) or base
    report = [f"base material for converted materials: {base.name}"]
    bones = _bone_names()

    # selected rigs/empties bring their meshes; they are removed afterwards
    meshes, rigs = [], []
    for o in objects:
        if o == arm or (o.type == "MESH" and o.get("ctl_source_index") is not None):
            continue
        if o.type == "MESH":
            meshes.append(o)
        elif o.type in ("ARMATURE", "EMPTY"):
            rigs.append(o)
            meshes += [c for c in o.children_recursive if c.type == "MESH"]
    # importers (glTF) add bone display shapes: not part of the model
    shapes = {pb.custom_shape for o in bpy.data.objects if o.type == "ARMATURE" and o != arm
              for pb in o.pose.bones if pb.custom_shape is not None}
    meshes = [o for o in dict.fromkeys(meshes) if o not in shapes]
    if not meshes:
        raise RuntimeError("select the imported mesh objects (or their armature) to adopt")

    pose = arm.data.pose_position
    arm.data.pose_position = "REST"
    source = _weight_source(set(meshes)) if weights == "transfer" else None
    converted = {}
    try:
        for ob in meshes:
            mw = ob.matrix_world.copy()
            ob.parent = None
            ob.matrix_world = mw
            for m in [m for m in ob.modifiers if m.type == "ARMATURE"]:
                ob.modifiers.remove(m)
            if ob.data.shape_keys:
                ob.shape_key_clear()
                report.append(f"{ob.name}: shape keys removed (the game model has none)")
            for i, uv in enumerate(ob.data.uv_layers[:2]):
                uv.name = f"UV{i}"

            # materials
            if not ob.material_slots:
                ob.data.materials.append(None)
            for slot in ob.material_slots:
                m = slot.material
                if is_game_material(m):
                    continue
                key = m.name if m else f"{ob.name}_material"
                if key not in converted:
                    converted[key], notes = convert_material(m, base, key, flip_green)
                    report.append(f"material {converted[key].name}: copy of {base.name}")
                    report += [f"  {n}" for n in notes]
                slot.link = "OBJECT" if slot.link == "OBJECT" else "DATA"
                slot.material = converted[key]

            # weights
            for g in list(ob.vertex_groups):
                if g.name not in bones or weights != "keep":
                    ob.vertex_groups.remove(g)
            if weights == "transfer":
                if source is None:
                    raise RuntimeError("no skinned LOD0 meshes to copy weights from; use weights='bone'")
                transfer_weights(ob, source)
                if ob.vertex_groups:
                    report.append(f"{ob.name}: weights copied from the nearest body/clothing ({len(ob.vertex_groups)} bones)")
                else:
                    ob.vertex_groups.new(name=bone).add(range(len(ob.data.vertices)), 1.0, "REPLACE")
                    report.append(f"{ob.name}: no weights found nearby; rigidly bound to {bone}")
            elif weights == "bone":
                if bone not in bones:
                    raise RuntimeError(f"bone {bone!r} is not in the skeleton")
                g = ob.vertex_groups.new(name=bone)
                g.add(range(len(ob.data.vertices)), 1.0, "REPLACE")
                report.append(f"{ob.name}: rigidly bound to {bone}")

            # skeleton + collection
            ob.parent = arm
            ob.matrix_parent_inverse = arm.matrix_world.inverted()
            mod = ob.modifiers.new("Armature", "ARMATURE")
            mod.object = arm
            for c in list(ob.users_collection):
                c.objects.unlink(ob)
            lod_collection(lod).objects.link(ob)
            ob["ctl_adopted"] = True

        if weights == "auto":
            bpy.ops.object.select_all(action="DESELECT")
            for ob in meshes:
                ob.modifiers.remove(ob.modifiers["Armature"])
                ob.select_set(True)
            arm.select_set(True)
            bpy.context.view_layer.objects.active = arm
            bpy.ops.object.parent_set(type="ARMATURE_AUTO")
            report.append("weights: automatic (bone heat)")
    finally:
        arm.data.pose_position = pose
        if source is not None:
            bpy.data.meshes.remove(source.data)
    for s in shapes:
        if s.name in bpy.data.objects and not any(
                pb.custom_shape == s for o in bpy.data.objects if o.type == "ARMATURE" and o not in rigs for pb in o.pose.bones):
            bpy.data.objects.remove(s)
    for r in rigs:
        if r.name in bpy.data.objects:
            data = r.data
            bpy.data.objects.remove(r)
            if data is not None and data.users == 0 and isinstance(data, bpy.types.Armature):
                bpy.data.armatures.remove(data)
    report.append(f"adopted into LOD{lod}: " + ", ".join(o.name for o in meshes))
    return report


# =============================================================================== check


def check() -> tuple[list[str], list[str]]:
    """(errors, warnings) for the current file, mirroring the build's rules."""
    errors, warnings = [], []
    arm = game_armature()
    bones = _bone_names()
    for ob in bpy.data.objects:
        if ob.type != "MESH" or ob.name.startswith("_"):
            continue
        if lod_of(ob) is None:
            warnings.append(f"{ob.name}: not in a LOD collection (counts as LOD0)")
        used = {p.material_index for p in ob.data.polygons}
        for i in used:
            m = ob.material_slots[i].material if i < len(ob.material_slots) else None
            if m is None:
                warnings.append(f"{ob.name}: faces without a material are skipped")
            elif not is_game_material(m):
                errors.append(f"{ob.name}: material '{m.name}' is not a game material - adopt the object, "
                              "or use a copy of a game material")
        if arm is not None:
            groups = {g.index: g.name for g in ob.vertex_groups}
            stray = sorted(n for n in groups.values() if n not in bones)
            if stray:
                warnings.append(f"{ob.name}: vertex groups that are not bones are ignored: {', '.join(stray[:6])}")
            bone_idx = {i for i, n in groups.items() if n in bones}
            rigid = ob.parent_type == "BONE" and ob.parent_bone
            if not rigid:
                loose = sum(1 for v in ob.data.vertices if not any(g.group in bone_idx and g.weight > 0 for g in v.groups))
                if loose:
                    warnings.append(f"{ob.name}: {loose} vertices have no bone weights (they will follow the root bone)")
            if not any(m.type == "ARMATURE" and m.object == arm for m in ob.modifiers) and not rigid:
                warnings.append(f"{ob.name}: no Armature modifier - it won't move in Blender's pose preview")
        if ob.data.shape_keys:
            warnings.append(f"{ob.name}: shape keys are baked at their current values")

    seen = set()
    for mat in bpy.data.materials:
        if not is_game_material(mat) or mat.users == 0:
            continue
        orig = original_of(mat)
        orig_nodes = slot_nodes(orig) if orig else {}
        for param, node in slot_nodes(mat).items():
            img = node.image
            if img is None or img.name in seen and param not in orig_nodes:
                continue
            path = bpy.path.abspath(img.filepath) if img.filepath else ""
            if img.packed_file is not None or not path:
                errors.append(f"material {mat.name}: image '{img.name}' ({param}) is packed or not saved to a file - "
                              "use Save images")
                continue
            if not os.path.isfile(path):
                errors.append(f"material {mat.name}: image file {path} is missing")
                continue
            if img.is_dirty and img.name not in seen:
                warnings.append(f"image '{img.name}' has unsaved changes - use Save images (the build reads the file)")
            want = orig_nodes.get(param).image if param in orig_nodes else None
            if want is not None and want.size[0] and tuple(img.size) != tuple(want.size):
                errors.append(f"material {mat.name}: {param} image '{img.name}' is {img.size[0]}x{img.size[1]}, "
                              f"must be {want.size[0]}x{want.size[1]}")
            if node.get("ctl_shared_with") and img.is_dirty:
                warnings.append(f"image '{img.name}' is SHARED with {node['ctl_shared_with']} other models")
            seen.add(img.name)
    return errors, warnings


def write_text(name: str, lines: list[str]):
    txt = bpy.data.texts.get(name) or bpy.data.texts.new(name)
    txt.clear()
    txt.write("\n".join(lines) + "\n")


def check_report() -> tuple[list[str], int]:
    errors, warnings = check()
    lines = ["control-mod check", ""]
    lines += [f"ERROR: {e}" for e in errors] + [f"warning: {w}" for w in warnings]
    if not errors and not warnings:
        lines.append("no problems found")
    write_text("control-mod_CHECK", lines)
    return lines, len(errors)


# =============================================================================== small tools


def copy_material(mat, name: str, objects) -> bpy.types.Material:
    if not is_game_material(mat):
        raise RuntimeError(f"'{mat.name if mat else None}' is not a game material")
    new = mat.copy()
    new.name = unique_material_name(safe_name(name))
    for ob in objects:
        for s in ob.material_slots:
            if s.material == mat:
                s.material = new
    return new


def new_texture(mat, param: str, color=None) -> bpy.types.Image:
    node = slot_nodes(mat).get(param)
    if node is None or node.image is None:
        raise RuntimeError(f"material {mat.name} has no {param} image")
    w, h = node.image.size
    if color is None:
        px = image_pixels(node.image)
    else:
        px = np.empty((h, w, 4), np.float32)
        px[..., :3] = color[:3]
        px[..., 3] = 1.0
    name = f"{safe_name(mat.name)}_{param}"
    path = blend_dir() / "textures" / "new" / f"{name}.png"
    img = write_image(name, px, path, param not in COLOR_PARAMS)
    img.filepath = relpath(path)
    node.image = img
    return img


def save_images() -> list[str]:
    done = []
    folder = blend_dir() / "textures" / "saved"
    for mat in bpy.data.materials:
        for param, node in slot_nodes(mat).items():
            img = node.image
            if img is None:
                continue
            if img.packed_file is not None or not img.filepath:
                path = folder / f"{safe_name(img.name)}.png"
                write_image(img.name, image_pixels(img), path, param not in COLOR_PARAMS)
                if img.packed_file is not None:
                    img.unpack(method="REMOVE")
                img.filepath = relpath(path)
                done.append(f"wrote {img.name} -> {path.name}")
            elif img.is_dirty:
                img.save()
                done.append(f"saved {img.name}")
    return done


def show_lod(lod: int | None):
    def walk(lc):
        for ch in lc.children:
            n = ch.collection.name.upper()
            if n.startswith("LOD") and n[3:].split(".")[0].isdigit():
                ch.hide_viewport = lod is not None and int(n[3:].split(".")[0]) != lod
            walk(ch)

    walk(bpy.context.view_layer.layer_collection)


def mod_location() -> tuple[Path, str] | None:
    """(workspace, mod id) for a .blend at <workspace>/mods/<mod>/models/<model>/<model>.blend."""
    if not bpy.data.filepath:
        return None
    p = Path(bpy.data.filepath).resolve()
    parts = p.parts
    for i in range(len(parts) - 1, 0, -1):
        if parts[i] == "models" and i >= 2 and parts[i - 2] == "mods":
            ws = Path(*parts[: i - 2])
            if (ws / "control-mod.toml").exists() or (ws / "pyproject.toml").exists():
                return ws, parts[i - 1]
    return None


def run_build() -> tuple[bool, list[str]]:
    loc = mod_location()
    if loc is None:
        return False, ["this .blend is not inside a control-mod workspace (mods/<mod>/models/...)"]
    ws, mod = loc
    uv = shutil.which("uv") or next((str(p) for p in (Path.home() / ".local" / "bin" / "uv.exe",
                                                       Path.home() / ".cargo" / "bin" / "uv.exe") if p.exists()), None)
    if uv is None:
        return False, ["uv not found - run 'uv run control-mod build " + mod + "' in a terminal"]
    bpy.ops.wm.save_mainfile()
    proc = subprocess.run([uv, "run", "control-mod", "build", mod], cwd=ws, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    lines = (proc.stdout + proc.stderr).splitlines()
    write_text("control-mod_BUILD", [f"control-mod build {mod}", ""] + lines)
    return proc.returncode == 0, lines


def install_text_block():
    """Keep a copy of this script in the .blend (Text Editor), registered on load."""
    try:
        src = Path(__file__).read_text(encoding="utf-8")
    except (NameError, OSError):
        return
    txt = bpy.data.texts.get(TEXT_NAME) or bpy.data.texts.new(TEXT_NAME)
    if txt.as_string() != src:
        txt.clear()
        txt.write(src)
    txt.use_module = True


# =============================================================================== UI


def _material_items(self, context):
    items = [("AUTO", "Automatic", "a standard, untinted material of this model")]
    for m in bpy.data.materials:
        if is_original_material(m):
            items.append((m.name, m.name, m.get("ctl_shader", "")))
    return items


def _param_items(self, context):
    ob = context.active_object
    mat = ob.active_material if ob else None
    return [(p, p, "") for p in slot_nodes(mat)] or [("NONE", "(no texture slots)", "")]


class CTLMOD_Props(bpy.types.PropertyGroup):
    base_material: bpy.props.EnumProperty(name="Base material", items=_material_items,
                                          description="game material new materials are copies of (shader + parameters)")
    lod: bpy.props.IntProperty(name="LOD", default=0, min=0, max=3)
    weights: bpy.props.EnumProperty(name="Weights", default="transfer", items=[
        ("transfer", "From body", "copy weights from the nearest LOD0 body/clothing surface"),
        ("auto", "Automatic", "Blender's automatic weights (bone heat)"),
        ("bone", "One bone", "rigid: everything follows one bone"),
        ("keep", "Keep", "keep vertex groups that already have game bone names"),
    ])
    bone: bpy.props.StringProperty(name="Bone", default="C_spine2_JNT")
    flip_green: bpy.props.BoolProperty(name="Flip normal map green", default=False,
                                       description="for normal maps made for the other convention (DirectX vs OpenGL)")
    new_name: bpy.props.StringProperty(name="Name", default="my_material")
    param: bpy.props.EnumProperty(name="Slot", items=_param_items)
    color: bpy.props.FloatVectorProperty(name="Colour", subtype="COLOR", size=3, min=0, max=1, default=(0.8, 0.8, 0.8))
    fill: bpy.props.BoolProperty(name="Fill with colour", default=False,
                                 description="start from a flat colour instead of a copy of the current image")


class CTLMOD_OT_adopt(bpy.types.Operator):
    bl_idname = "control_mod.adopt"
    bl_label = "Adopt selected"
    bl_description = "Make the selected imported objects game-ready: game-material copies, textures, weights, skeleton, LOD"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        p = context.scene.control_mod
        base = None if p.base_material == "AUTO" else bpy.data.materials.get(p.base_material)
        try:
            report = adopt(list(context.selected_objects), base, p.lod, p.weights, p.bone, p.flip_green)
        except RuntimeError as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        write_text("control-mod_ADOPT", report)
        self.report({"INFO"}, report[-1] + " (details: text block control-mod_ADOPT)")
        return {"FINISHED"}


class CTLMOD_OT_check(bpy.types.Operator):
    bl_idname = "control_mod.check"
    bl_label = "Check"
    bl_description = "List what the build would refuse or what may look wrong (text block control-mod_CHECK)"

    def execute(self, context):
        lines, n_err = check_report()
        n_warn = sum(1 for ln in lines if ln.startswith("warning:"))
        self.report({"ERROR" if n_err else "WARNING" if n_warn else "INFO"},
                    f"{n_err} error(s), {n_warn} warning(s) - see text block control-mod_CHECK"
                    if n_err or n_warn else "no problems found")
        return {"FINISHED"}


class CTLMOD_OT_copy_material(bpy.types.Operator):
    bl_idname = "control_mod.copy_material"
    bl_label = "Copy material"
    bl_description = "Copy the active game material as a new one and use it on the selected objects"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        ob = context.active_object
        try:
            new = copy_material(ob.active_material if ob else None, context.scene.control_mod.new_name,
                                context.selected_objects or [ob])
        except RuntimeError as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        self.report({"INFO"}, f"new material {new.name}")
        return {"FINISHED"}


class CTLMOD_OT_new_texture(bpy.types.Operator):
    bl_idname = "control_mod.new_texture"
    bl_label = "New texture"
    bl_description = "Give the active material's slot its own new texture (same resolution)"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        p = context.scene.control_mod
        ob = context.active_object
        try:
            img = new_texture(ob.active_material, p.param, tuple(p.color) if p.fill else None)
        except (RuntimeError, AttributeError) as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        self.report({"INFO"}, f"{p.param} now uses {img.name}")
        return {"FINISHED"}


class CTLMOD_OT_save_images(bpy.types.Operator):
    bl_idname = "control_mod.save_images"
    bl_label = "Save images"
    bl_description = "Save painted images and write packed images to files"

    def execute(self, context):
        done = save_images()
        self.report({"INFO"}, f"{len(done)} image(s) saved" if done else "nothing to save")
        return {"FINISHED"}


class CTLMOD_OT_show_lod(bpy.types.Operator):
    bl_idname = "control_mod.show_lod"
    bl_label = "Show LOD"
    lod: bpy.props.IntProperty(default=-1)

    def execute(self, context):
        show_lod(None if self.lod < 0 else self.lod)
        return {"FINISHED"}


class CTLMOD_OT_build(bpy.types.Operator):
    bl_idname = "control_mod.build"
    bl_label = "Save & build mod"
    bl_description = "Save this file and run 'control-mod build' for its mod (takes a minute)"

    def execute(self, context):
        ok, lines = run_build()
        self.report({"INFO" if ok else "ERROR"},
                    ("build finished" if ok else "build failed") + " - see text block control-mod_BUILD")
        return {"FINISHED"}


class CTLMOD_PT_panel(bpy.types.Panel):
    bl_label = "control-mod"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Control Mod"

    def draw(self, context):
        p = context.scene.control_mod
        col = self.layout.column()
        box = col.box()
        box.label(text="Adopt imported objects", icon="IMPORT")
        box.prop(p, "base_material")
        box.prop(p, "lod")
        box.prop(p, "weights")
        if p.weights == "bone":
            box.prop(p, "bone")
        box.prop(p, "flip_green")
        box.operator("control_mod.adopt")
        box = col.box()
        box.label(text="Materials and textures", icon="MATERIAL")
        box.prop(p, "new_name")
        box.operator("control_mod.copy_material")
        box.prop(p, "param")
        row = box.row()
        row.prop(p, "fill")
        if p.fill:
            row.prop(p, "color", text="")
        box.operator("control_mod.new_texture")
        box.operator("control_mod.save_images")
        box = col.box()
        box.label(text="View", icon="HIDE_OFF")
        row = box.row(align=True)
        for i in range(4):
            row.operator("control_mod.show_lod", text=f"LOD{i}").lod = i
        row.operator("control_mod.show_lod", text="All").lod = -1
        box = col.box()
        box.label(text="Build", icon="CHECKMARK")
        box.operator("control_mod.check")
        box.operator("control_mod.build")


CLASSES = (CTLMOD_Props, CTLMOD_OT_adopt, CTLMOD_OT_check, CTLMOD_OT_copy_material, CTLMOD_OT_new_texture,
           CTLMOD_OT_save_images, CTLMOD_OT_show_lod, CTLMOD_OT_build, CTLMOD_PT_panel)


def register():
    unregister()  # running the script twice replaces the old version
    for c in CLASSES:
        bpy.utils.register_class(c)
    bpy.types.Scene.control_mod = bpy.props.PointerProperty(type=CTLMOD_Props)


def unregister():
    for c in reversed(CLASSES):
        existing = getattr(bpy.types, c.__name__, None)
        if existing is not None:
            try:
                bpy.utils.unregister_class(existing)
            except RuntimeError:
                pass
    if hasattr(bpy.types.Scene, "control_mod"):
        del bpy.types.Scene.control_mod


# =============================================================================== headless


def import_file(path: Path) -> list:
    before = set(bpy.data.objects)
    ext = path.suffix.lower()
    if ext in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=str(path))
    elif ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=str(path))
    elif ext == ".obj":
        bpy.ops.wm.obj_import(filepath=str(path))
    elif ext == ".blend":
        with bpy.data.libraries.load(str(path)) as (src, dst):
            dst.objects = list(src.objects)
        for o in dst.objects:
            if o is not None:
                bpy.context.scene.collection.objects.link(o)
    else:
        raise SystemExit(f"can't import {path.suffix} files (glb, gltf, fbx, obj, blend)")
    return [o for o in bpy.data.objects if o not in before]


def main(argv: list[str]):
    cmd, rest = argv[0], argv[1:]
    opts, flags = {}, set()
    it = iter(rest)
    for a in it:
        if a in ("--flip-green", "--save"):
            flags.add(a[2:])
        elif a.startswith("--"):
            opts[a[2:]] = next(it)
    if cmd == "adopt":
        objs = import_file(Path(opts["import"])) if "import" in opts else []
        if "objects" in opts:
            objs += [bpy.data.objects[n] for n in opts["objects"].split(",")]
        base = bpy.data.materials.get(opts["material"]) if "material" in opts else None
        if "material" in opts and base is None:
            raise SystemExit(f"no material named {opts['material']!r}")
        for line in adopt(objs, base, int(opts.get("lod", 0)), opts.get("weights", "transfer"),
                          opts.get("bone", "C_spine2_JNT"), "flip-green" in flags):
            log(line)
        if "save" in flags:
            bpy.ops.wm.save_mainfile()
            log("saved", bpy.data.filepath)
    elif cmd == "check":
        lines, n_err = check_report()
        for line in lines[2:]:
            log(line)
        if n_err:
            sys.exit(2)
    else:
        raise SystemExit(f"unknown command {cmd}")


register()

if __name__ == "__main__":
    _argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    if _argv:
        try:
            main(_argv)
        except SystemExit:
            raise
        except Exception:
            import traceback

            traceback.print_exc()
            sys.exit(1)
    elif not bpy.app.background:
        install_text_block()
