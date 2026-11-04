"""Example of scripted edits to a ctlmod .blend - the same things a modder does by hand.

Run with:
    blender --background <model>.blend --python examples/demo_edit.py -- [--halo] [--red-jacket] [--recolor-png PATH]

* ``--halo``        adds a new mesh (a torus over the head) skinned to the head bone,
                    using an existing game material
* ``--red-jacket``  duplicates the jacket material and gives the copy a new red colour map
                    (a new material + new texture, only affects this outfit)
* ``--recolor-png`` tints an exported PNG in place (edits the original texture; affects
                    every model that uses it)
"""

import sys
from pathlib import Path

import bpy
import numpy as np

argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
blend_dir = Path(bpy.data.filepath).parent
arm = next(o for o in bpy.data.objects if o.type == "ARMATURE")
lod0 = bpy.data.collections["LOD0"]

if "--halo" in argv:
    head = arm.data.bones["C_head_JNT"]
    top = arm.matrix_world @ head.head_local
    bpy.ops.mesh.primitive_torus_add(major_radius=0.12, minor_radius=0.012, location=(top.x, top.y, top.z + 0.32))
    halo = bpy.context.active_object
    halo.name = "halo"
    for c in halo.users_collection:
        c.objects.unlink(halo)
    lod0.objects.link(halo)
    bpy.ops.object.shade_smooth()
    halo.data.materials.append(bpy.data.materials["jesse_suit_accessories"])
    vg = halo.vertex_groups.new(name="C_head_JNT")
    vg.add(list(range(len(halo.data.vertices))), 1.0, "REPLACE")
    halo.parent = arm
    halo.matrix_parent_inverse = arm.matrix_world.inverted()
    mod = halo.modifiers.new("Armature", "ARMATURE")
    mod.object = arm
    print("[demo] added halo")

if "--red-jacket" in argv:
    src = bpy.data.materials["jesse_suit_jacket"]
    red = src.copy()  # copies keep the ctl_material property -> a new game material based on it
    red.name = "jesse_suit_jacket_red"
    png = blend_dir / "textures" / "jacket_red_d.png"
    img = bpy.data.images.new("jacket_red_d", 64, 64)
    px = np.zeros((64, 64, 4), np.float32)
    px[..., 0], px[..., 3] = 0.8, 1.0
    img.pixels.foreach_set(px.ravel())
    img.filepath_raw = str(png)
    img.file_format = "PNG"
    img.save()
    red.node_tree.nodes["g_sColorMap"].image = img
    for ob in lod0.objects:
        if ob.type == "MESH":
            for slot in ob.material_slots:
                if slot.material == src:
                    slot.material = red
    print("[demo] red jacket material")

for i, a in enumerate(argv):
    if a == "--recolor-png":
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
