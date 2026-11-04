"""Example of scripted edits to a ctlmod .blend - the same things a modder does by hand.

Run with:
    blender --background <model>.blend --python examples/demo_edit.py -- [options]

Options:

* ``--halo MATERIAL``     adds a new mesh (a torus over the head) skinned to the head
                          bone, using the existing game material MATERIAL
* ``--red-copy MATERIAL`` duplicates MATERIAL as ``<MATERIAL>_red``, gives the copy a new
                          red colour map and uses it on every mesh that used MATERIAL
                          (a new material + new texture: only affects this model)
* ``--copy-texture PNG``  with ``--red-copy``: point the copy at an existing exported
                          texture PNG instead of creating a new texture file
* ``--recolor-png PATH``  tints an exported PNG red in place (edits the original
                          texture: affects every model that uses it); repeatable
* ``--red-copy`` / ``--copy-texture`` / ``--red-jacket`` create new materials or
  textures, which the game does not load yet: builds need ``--experimental``
* ``--halo`` / ``--red-jacket`` without a name keep working for the Formal suit
  (``jesse_suit_accessories`` / ``jesse_suit_jacket``)

Material names are the game's, as listed by ``ctlmod info <model>``.
"""

import sys
from pathlib import Path

import bpy
import numpy as np

argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
blend_dir = Path(bpy.data.filepath).parent
arm = next(o for o in bpy.data.objects if o.type == "ARMATURE")
lod0 = bpy.data.collections["LOD0"]


def option(flag: str, default: str | None = None) -> str | None:
    """Value after ``flag`` (or ``default`` if the flag has no value); None if absent."""
    if flag not in argv:
        return None
    i = argv.index(flag)
    if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
        return argv[i + 1]
    return default


def game_material(name: str) -> bpy.types.Material:
    for m in bpy.data.materials:
        if m.get("ctl_material") == name and m.get("ctl_blender_name", m.name) == m.name:
            return m
    raise SystemExit(f"no game material named {name!r}")


halo_mat = option("--halo", "jesse_suit_accessories")
if halo_mat:
    head = arm.data.bones["C_head_JNT"]
    top = arm.matrix_world @ head.head_local
    bpy.ops.mesh.primitive_torus_add(major_radius=0.12, minor_radius=0.012, location=(top.x, top.y, top.z + 0.32))
    halo = bpy.context.active_object
    halo.name = "halo"
    for c in halo.users_collection:
        c.objects.unlink(halo)
    lod0.objects.link(halo)
    bpy.ops.object.shade_smooth()
    halo.data.materials.append(game_material(halo_mat))
    vg = halo.vertex_groups.new(name="C_head_JNT")
    vg.add(list(range(len(halo.data.vertices))), 1.0, "REPLACE")
    halo.parent = arm
    halo.matrix_parent_inverse = arm.matrix_world.inverted()
    mod = halo.modifiers.new("Armature", "ARMATURE")
    mod.object = arm
    print(f"[demo] added halo ({halo_mat})")

red_src = option("--red-copy") or ("jesse_suit_jacket" if "--red-jacket" in argv else None)
reuse = option("--copy-texture")  # existing exported PNG -> the copy references that game texture
if red_src:
    src = game_material(red_src)
    red = src.copy()  # copies keep the ctl_* properties -> a new game material based on src
    red.name = f"{red_src}_red"
    if reuse:
        img = bpy.data.images.load(str(Path(reuse).resolve()), check_existing=True)
    else:
        png = blend_dir / "textures" / f"{red_src}_red_d.png"
        img = bpy.data.images.new(png.stem, 64, 64)
        px = np.zeros((64, 64, 4), np.float32)
        px[..., 0], px[..., 3] = 0.8, 1.0
        img.pixels.foreach_set(px.ravel())
        img.filepath_raw = str(png)
        img.file_format = "PNG"
        img.save()
    red.node_tree.nodes["g_sColorMap"].image = img
    for ob in bpy.data.objects:  # every LOD, so the change also holds at a distance
        if ob.type == "MESH":
            for slot in ob.material_slots:
                if slot.material == src:
                    slot.material = red
    print(f"[demo] {red.name}: new material using " + (f"existing texture {Path(reuse).name}" if reuse else "a new red texture"))

for i, flag in enumerate(argv):
    if flag != "--recolor-png" or i + 1 >= len(argv):
        continue
    path = Path(argv[i + 1]).resolve()
    img = bpy.data.images.load(str(path))
    px = np.array(img.pixels[:], np.float32).reshape(-1, 4)
    px[:, 0] = np.clip(px[:, 0] * 3.0 + 0.3, 0, 1)
    px[:, 1] *= 0.4
    px[:, 2] *= 0.4
    img.pixels.foreach_set(px.ravel())
    img.save()
    print("[demo] recoloured", path)

bpy.ops.wm.save_mainfile()
