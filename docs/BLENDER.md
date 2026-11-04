# Blender guide for control-mod

This guide is for people who edit the `.blend` files control-mod makes. It assumes you
know Blender well (modifiers, vertex groups, custom normals, shader nodes, data
transfer, texture paint) and explains what the build does with each part of the file:
what you **can** change, what you **should** do so it looks right in game, and what you
**can't** change.

Unless a section says otherwise, everything here applies to the default build mode, which
writes `.packmeta` metadata. `control-mod build --size-matched` is stricter: see
[Size-matched builds](#size-matched-builds).

* [The file at a glance](#the-file-at-a-glance)
* [Rules in one table](#rules-in-one-table)
* [Space, units and transforms](#space-units-and-transforms)
* [Geometry](#geometry)
* [How objects become game meshes](#how-objects-become-game-meshes)
* [Levels of detail](#levels-of-detail)
* [Skinning](#skinning)
* [Materials](#materials)
* [Textures](#textures)
* [Bringing in outside models (glb, fbx, obj)](#bringing-in-outside-models-glb-fbx-obj)
* [The Control Mod sidebar](#the-control-mod-sidebar)
* [Checking your work](#checking-your-work)
* [What can't be changed](#what-cant-be-changed)
* [Size-matched builds](#size-matched-builds)
* [Reference: custom properties and text blocks](#reference-custom-properties-and-text-blocks)

---

## The file at a glance

`control-mod add <mod> <model>` writes `mods/<mod>/models/<model>/`:

| path | what |
|---|---|
| `<model>.blend` | the file you edit |
| `textures/*.png` | every texture the model uses, decoded from the game's `.tex` files |
| `model.json`, `geometry.npz`, `template.binfbx` | the original data the build compares against; **don't edit or delete** |

Inside the `.blend`:

```
Scene
└─ <model>                         collection
   ├─ <model>_skeleton             armature (the game's bones, rest pose)
   ├─ LOD0                         collection, visible
   │   ├─ LOD0_00_<mesh>           one object per game mesh, Armature modifier, vertex groups
   │   └─ ...
   ├─ LOD1 … LOD3                  collections, hidden in the viewport
Text blocks: control-mod_READ_ME, control_mod_tools.py
```

* One **object per game mesh**. A game mesh is one material on one LOD, so most objects
  have a single material slot.
* One **Blender material per game material**, with an Image Texture node for every
  texture slot of the game material. The node's name is the slot name (`g_sColorMap`, …).
  Images used by other models are shown as red nodes labelled `SHARED xN` and their image
  names start with `SHARED(N)_`.
* The **Control Mod** tab in the 3D view's sidebar (N) has the tools described in
  [The Control Mod sidebar](#the-control-mod-sidebar). `control-mod open <mod>` loads them.
  If you open the file yourself and Blender blocks scripts, click *Allow Execution* in the
  warning bar, or run the `control_mod_tools.py` text block (Text Editor, *Run Script*).

---

## Rules in one table

| | can | should | can't |
|---|---|---|---|
| **Geometry** | anything: sculpt, remodel, retopo, add, delete, join, split, modifiers | keep added parts light; edit LOD1–3 too (or use `--lods decimate`) | have loose verts/edges render (only faces export) |
| **Objects** | add, delete, rename, move between LOD collections | keep the original objects (deleting leaves an invisible placeholder) | move the armature object |
| **Skeleton** | pose it to test deformation | leave it at rest when you save (it doesn't matter to the build, but it does to Data Transfer) | add, remove, rename or move bones |
| **Weights** | repaint, transfer, mirror, any number of groups | stay within the LOD's influence count (8 for Jesse's LOD0) | weight to bones that aren't in the skeleton |
| **Materials** | assign any game material to faces; copy a game material into a new one | base copies on a material with the right shader (cloth, skin, hair, standard) | create a material from scratch; change shaders or material parameters |
| **Textures** | repaint in place; give a material slot a new image | save images to files; keep normal and mask maps *Non-Color* | change a texture's resolution |
| **UVs** | edit, re-unwrap, add a second map | keep the first map named `UV0` and inside −8…+8 | — |
| **Normals** | custom split normals, Weighted Normal, Smooth by Angle | check the shading of hard UV seams with normal maps | — |

---

## Space, units and transforms

* Blender space is the game space converted to Z-up: the character stands at the origin
  facing **−Y**, its left side on **+X**, metres.
* Mesh **object transforms are baked** at build time, relative to the armature. Moving,
  rotating or scaling objects is fine, and mirrored (negative-scale) objects keep their
  winding.
* Don't move, rotate or scale the **armature object**, and don't apply its transform:
  the bones must stay where the game's bones are.
* The build exports the mesh at **rest pose** (it switches the Armature modifier off),
  so posing the armature to check deformation is harmless.

## Geometry

What the build takes from each mesh object:

* The **evaluated mesh**: every modifier in the stack is applied, except Armature
  modifiers. Subdivision, Mirror, Solidify, Weld, Decimate, Data Transfer, Geometry Nodes
  all work.
* **Triangles** only. Quads and n-gons are triangulated the way Blender's viewport does
  it; loose vertices and edges are dropped.
* **Shape keys** are baked at their current values (the game model has no morph targets).
* **Faces without a material** are skipped (with a warning).
* Objects whose name starts with **`_`** are ignored entirely: use them for reference
  meshes, booleans' cutters, helpers.
* Objects outside a `LODn` collection count as LOD0 (with a note).

Vertex data:

| data | how it's stored | notes |
|---|---|---|
| position | float32 | |
| normal | 16-bit per component | custom split normals are exported as you see them |
| tangent | 8-bit, computed by the build | from UV0, per vertex (not MikkTSpace); hard UV seams can show faint normal-map seams |
| UV | 16-bit fixed point, 1/4096 steps | range **−8 … +8**; values outside are clamped |
| vertex colour | 8-bit RGBA | used only where the model already has colour attributes; missing ones are filled white |
| bone weights | 8-bit, up to 4 or 8 influences | see [Skinning](#skinning) |

* **UV maps**: the first map (by name, `UV0`, `UV1`, …) is the one textures use. A LOD
  that stores two UV sets gets your second map, or a copy of the first if you have only
  one. Extra maps are dropped.
* **Normals**: anything Blender can show is exported: custom split normals, *Smooth by
  Angle*, Weighted Normal modifier, flat shading. The game shades with these normals and
  the build's tangents.
* **Vertex counts**: there's no fixed limit. A LOD with more than 65,535 vertices
  switches the model to 32-bit indices. That works in the format, but no model that large
  has been tested in game yet, so keep LODs reasonable.
* Every vertex is split by the build where its normal, UV or colour differs between
  faces (like any game exporter), so the game's vertex count is higher than Blender's.

## How objects become game meshes

The game expects each LOD to keep the original list of meshes, one per material, in
the original order. The build maps your objects onto that list:

1. An object (or one material slot of a multi-material object) whose geometry is
   **unchanged** reuses the original mesh data byte for byte.
2. **Edited** objects replace their original mesh.
3. **New** objects, and objects split off from originals, are merged into the original
   mesh of the same LOD that uses the same material. If no original mesh of that LOD uses
   that material (e.g. a material you copied), the part becomes an **extra mesh** of its
   LOD.
4. An original object you **deleted** leaves an invisible placeholder in its slot.

What that means in practice:

* **Joining objects** (Ctrl+J) is fine: the build splits per material slot again.
* **Replacing several meshes with one**, e.g. a torso and legs replaced by one bodysuit:
  * *keeping both materials*: join or model the new piece with the torso material on
    the upper faces and the legs material on the lower ones. Each part lands in its
    original slot. This is the simplest route and keeps both texture sets;
  * *with one material*: delete the old objects and give the new mesh one material (an
    existing one or a copy). The old slots become placeholders. The new mesh's UVs must
    fit that material's textures, so you'll usually also give it new textures.
  * Either way, do the same in LOD1–3, or use `--lods decimate` (see below), or the old
    torso and legs come back at a distance.
* Deleting an object only removes it from **that LOD**.

## Levels of detail

The game switches to LOD1, LOD2, LOD3 as the character gets further from the camera.
An object's LOD is the `LODn` collection it's in. Only LOD0 is visible when the file
opens. Use the sidebar's LOD buttons (or the outliner) to look at the others.

The LOD **mode** is set per model (`control-mod add … --lods X`, or `control-mod build
<mod> --lods X`, which is remembered in `mod.toml`):

| mode | what the game gets for LOD1–3 | use when |
|---|---|---|
| `authored` *(default)* | the `LOD1`…`LOD3` collections, exactly as in the file | texture-only mods; small edits; you edit every LOD yourself |
| `decimate` | LOD0, decimated at build time to each original LOD's triangle count | shape changes without hand-made LODs |
| `copy` | LOD0 as is, in every LOD | best quality at distance, heaviest |

* With `authored`, LODs you didn't edit keep the **original** look. A new jacket on LOD0
  shows the old one from across a room.
* `decimate` and `copy` ignore the LOD1–3 collections (except as noted below).
  `decimate` uses a Decimate (collapse) modifier and recomputes smooth normals, so hard
  edges and custom normals don't survive in LOD1–3.
* Some models have a LOD that uses materials LOD0 doesn't have: Jesse's LOD3 is a single
  baked mesh with its own material. Such a LOD keeps its original geometry in `decimate`
  and `copy` modes, so at the furthest distance you see the original outfit unless you
  edit that LOD's object yourself (in `authored` mode).
* A common recipe: work on LOD0; when the shape is final, duplicate your new objects
  into LOD1/LOD2 with a Decimate modifier each (ratio ≈ original LOD triangles / LOD0
  triangles), keep `authored`, and edit LOD3's baked mesh or accept the original there.

## Skinning

* The armature is the game's skeleton. **Bones can't be added, removed, renamed or
  re-positioned**: the skeleton, animations, ragdoll and cloth simulation live in other
  game files. Bones you add in Blender are ignored, and weights to them are lost.
* Weights are the **vertex groups named after bones**. Other groups are ignored (the
  Check tool lists them).
* Each LOD has a fixed number of **influences per vertex** (Jesse: 8 in LOD0, 4 in
  LOD3). The build keeps the largest ones, renormalises, and quantises to 8 bits, so very
  small weights (< ~0.002) vanish.
* **Unweighted vertices** are bound to the root bone and stay behind when the character
  moves. Check lists them.
* **Rigid parts** (buckles, a badge, a halo): either weight everything to one bone, or
  parent the object to a bone (*Parent > Bone*). Bone-parented objects need no groups.
* **Cloth**: the jacket tails, hair strands and straps are driven by cloth joints
  (`…_cloth_JNT12`, `…hair…`). Weighting new geometry to those joints makes it swing
  with the simulation. Geometry weighted only to body bones stays rigid.
* Good ways to weight new parts: *Data Transfer* (vertex groups, nearest face
  interpolated) from the body or clothing underneath, which is what the Adopt tool does;
  *Parent > With Automatic Weights* for separate, closed meshes; manual painting with the
  armature in pose mode to see deformation.
* Test in pose mode (rotate the spine, arms, legs) before building; then *Pose > Clear
  Transform > All*.

## Materials

The build turns each Blender material back into a game material by its custom
properties (`ctl_material`, `ctl_index`). That gives these rules:

* **Assigning** any of the model's materials to any faces is always fine.
* **Copying** a game material creates a new game material: the copy keeps the shader,
  every parameter and every texture of the original, under its new name. Use the
  *New Material* (duplicate) button next to the material's name in the Material
  properties, which makes `<name>.001` (rename it), or the sidebar's *Copy material*.
  (*Copy Material* in the specials menu is a clipboard copy of the settings, not a new
  material.) Then give the copy new images (below). This is how you make one piece look
  different without touching the rest of the outfit.
* A material **created from scratch** (*New*) or imported with an outside model has no
  link to a game shader: the build refuses it. Use the sidebar's *Adopt selected* to
  convert imported materials.
* **Only the images in the slot nodes count.** Everything else in the node tree is a
  preview: you can add, rewire or delete preview nodes, change the BSDF, and so on,
  without effect on the game. Don't rename the slot nodes.
* **Shaders and parameters** (colour multipliers, smoothness ranges, specular,
  detail tiling, alpha test) **can't** be edited. A copy inherits them from its base:
  pick a base that suits the new part.
  * `standardmaterial` (boots, jacket, jeans): good general base.
  * `cloth` (tank top): fabric shading with a tiling detail map and fuzz.
  * `character`: skin (subsurface, wrinkle maps) — only for skin.
  * `hair`: alpha-tested hair cards.
  * Materials that show a *g_vColorMultiplier (preview only)* node tint their colour
    map in game; the formal suit's jacket, for example, is a tiny texture tinted dark.
* **Material variants** (some models swap materials at runtime, e.g. Jesse Master's
  `frame`, NPCs' `hiss`) are preserved; `control-mod info <model>` lists them.
* The Blender preview is an approximation (base colour × tint, normal map). The game
  also uses smoothness, specular, fuzz, translucency, wrinkle and detail maps.

## Textures

Texture slots you'll see most:

| slot | content | colour space | game format |
|---|---|---|---|
| `g_sColorMap` | albedo (+ alpha for hair, lashes) | sRGB | BC1 / BC3 / BC7 |
| `g_sNormalMap` | tangent-space normal; only R and G are stored, B is rebuilt | Non-Color | BC5 |
| `g_sSmoothnessMap` | smoothness (1 − roughness) | Non-Color | BC4 |
| `g_sSpecularColorMap` | specular intensity | Non-Color | BC4 |
| `g_sAlphaTestSampler` | cut-out alpha | Non-Color | BC3 |
| `g_sBaseDetailMap` | tiling fabric detail (cloth shader) | Non-Color | BC5 |
| `g_sFuzzinessMap`, `g_sTranslucencyMap`, `g_sWrinkle*` | shader-specific masks | Non-Color | BC4/BC5 |

**Repainting in place**: paint in Texture Paint mode or edit `textures/<name>.png` in any
editor, and save. The build re-encodes the PNG in the original format with the original
mip count. Keep the resolution.

**New images**: point a slot node at another image file and the build creates a new
game texture used only by this model (in `data_pc/textures/control-mod/<mod>/<model>/`).
The new image must have **exactly the resolution of the texture it replaces**: the game's
metadata for the new texture is copied from that one. The sidebar's *New texture*
creates one at the right size (a copy of the current image or a flat colour) and
*Adopt* does it for imported textures. Pointing a slot at another exported PNG of the
same model reuses that game texture.

Images must be **files on disk**: the build reads the PNG files, not Blender's memory.

* **Unsaved paint** is not in the build. Use *Image > Save*, *Save All Images*, or the
  sidebar's *Save images*.
* **Packed images** (glTF imports, *Pack Resources*) have no file; the build keeps the
  original texture and warns. *Save images* writes them out.
* Other 8-bit formats (JPEG, TGA, BMP) work too, as long as the resolution matches.
  Save 16-bit or float images as 8-bit PNG first.

**Shared textures** (red `SHARED xN` nodes): repainting them changes every model that
uses them — Jesse's face for all outfits, `jesse_hands_d` for nine, `default*.tex` for
hundreds. If that's not what you want, copy the material and give the copy a new image
instead.

Normal maps:

* The game rebuilds blue from red and green, so whatever you put in blue is ignored.
* Which green-channel convention the game uses has not been verified. Compare your map
  with an exported game normal map of a similar surface: if bumps look inverted in game,
  flip the green channel (the Adopt tool has an option for it).
* Bakes from Blender use MikkTSpace; the game uses the build's per-vertex tangents.
  They agree closely except along hard UV seams.

## Bringing in outside models (glb, fbx, obj)

An imported model doesn't "just work": its materials aren't game materials, its
textures are usually packed and the wrong size, and it's rigged to a different
skeleton (or none). The **Adopt** tool fixes all of that:

From the command line (imports and adopts in one step, saves the `.blend`):

```bash
uv run control-mod adopt my_mod path/to/part.glb
```

Or in Blender: *File > Import* the model, place and scale it on the character, select
its objects (or its armature), and press *Adopt selected* in the sidebar.

What Adopt does, per selected mesh (meshes under a selected armature or empty included):

1. Clears the parent, keeping the world transform, and removes Armature modifiers and
   shape keys. Bone display shapes the glTF importer creates are left out and deleted.
2. Renames UV maps to `UV0`, `UV1`.
3. Replaces every non-game material with a **copy of a base game material** (the one you
   choose, or automatically: the most used untinted `standardmaterial` without alpha
   test), named after the imported material. Each slot of the copy gets a new PNG in
   `textures/adopted/`, at the slot's resolution:

   | slot | from the imported material |
   |---|---|
   | colour | Base Color image (alpha kept only if the BSDF's Alpha is linked), or the Base Color value |
   | normal | the image behind the Normal Map node (green optionally flipped), or flat |
   | smoothness | 1 − roughness (the channel the Roughness input reads, e.g. green of a glTF metallic-roughness map), or 1 − the Roughness value |
   | detail (`g_sBaseDetailMap`) | kept from the base (it tiles) |
   | anything else | flat average of the base's texture, so the base's UV layout doesn't show |

4. Removes vertex groups that aren't game bones, then weights the mesh (*Weights*):
   * **From body** (default): Data Transfer (nearest face, interpolated) from a temporary
     joined copy of the skinned LOD0 game meshes, then normalises. Works best for things
     worn close to the body. If it finds no weights at all, the part is bound to the
     chosen bone.
   * **Automatic**: Blender's bone heat. Needs closed, manifold meshes.
   * **One bone**: rigid, e.g. a hat on `C_head_JNT`.
   * **Keep**: keeps groups that already have game bone names (models rigged to this
     skeleton).
5. Parents it to the game skeleton with an Armature modifier and moves it to the chosen
   LOD collection.
6. Deletes the selected foreign armatures and empties.

Adopt writes a report into the text block `control-mod_ADOPT`. Afterwards:

* look at the result in pose mode, fix weights where cloth or joints need care;
* check the converted textures (metallic surfaces have no direct equivalent: the
  standard shader's specular comes from the base material);
* run *Check*, then build.

Doing it by hand instead: copy a game material, put the model's textures (resized to
the slot sizes, saved as files) in the copy's slot nodes, weight the mesh to the game
bones, parent it to the skeleton, put it in a LOD collection.

## The Control Mod sidebar

3D view > sidebar (N) > **Control Mod**:

| button | what it does |
|---|---|
| **Adopt selected** | see above. Options: base material, LOD, weights mode, bone, flip normal green |
| **Copy material** | copies the active object's active material as a new game material named *Name*, and uses it on the selected objects |
| **New texture** | gives the active material's *Slot* its own new image at the right resolution (a copy of the current one, or a flat colour), saved to `textures/new/` |
| **Save images** | saves every edited image; writes packed or unsaved images to `textures/saved/` |
| **LOD0 … LOD3 / All** | shows one LOD collection (or all) |
| **Check** | lists build errors and warnings in the text block `control-mod_CHECK` |
| **Save & build mod** | saves the file and runs `uv run control-mod build <mod>`; output in the text block `control-mod_BUILD` (Blender is busy for about a minute) |

The same script runs headless:

```bash
blender --background my.blend --python src/control_mod/blender/control_mod_tools.py -- check
```

## Checking your work

1. Sidebar > **Check** (or `uv run control-mod check <mod>`): errors are what the build
   would refuse; warnings are things that build but may look wrong (unweighted
   vertices, unsaved images, stray vertex groups, objects outside LOD collections).
2. **Build**: `uv run control-mod build <mod>`. The report lists every changed file,
   unchanged meshes, new materials and textures, and shared textures.
3. **Render what the game will get**: `uv run control-mod render <mod> --built
   [--view front|back|side|face] [--lod N]` re-imports the built file, not your `.blend`.
4. **In game**: `uv run control-mod install`, start the game (closed while installing),
   look at the outfit up close and at a distance (LODs).

## What can't be changed

* The skeleton and anything that depends on it: animations, ragdoll and physics
  shapes, cloth simulation setup (`.binapx`), corrective shapes (`.rbf`).
* Shaders and material parameters (colour multipliers, smoothness ranges, …). A copied
  material takes them from its base.
* Texture resolutions.
* Which outfit the game shows: a mod changes an existing outfit's model; it can't add a
  new outfit entry to the game's menus.

Big silhouette changes can clip with animations or cloth, as in any game mod.

## Size-matched builds

`control-mod build <mod> --size-matched` writes no metadata. It's for loose-file loaders
and other tools that can't install `.packmeta` files, and it adds these restrictions:

* the model must fit the original buffer sizes: added geometry has to be paid for by
  removing some (LOD1 may borrow LOD2's geometry to make room; the build says by how
  much it's over);
* no new or copied materials, no new images: only in-place repaints;
* new parts must use a material their LOD already has.

## Reference: custom properties and text blocks

Don't edit these by hand; copy objects and materials with Blender's own tools, which
keep them.

| on | property | meaning |
|---|---|---|
| material | `ctl_material` | game material it is (or was copied from) |
| material | `ctl_index` | that material's index in the model |
| material | `ctl_blender_name` | the original's name in Blender; a copy keeps the original's value, which is how the build tells copies apart |
| material | `ctl_shader` | game shader (informational) |
| image node | `ctl_param` | the material slot this node feeds |
| image node | `ctl_shared_with` | number of other models using the image |
| object | `ctl_source_index` | the original game mesh this object came from |
| object | `ctl_adopted` | set by Adopt |
| mesh | `ctl_island` attribute | piece numbering kept from the game (regenerated for new geometry) |
| armature | `ctl_model` | model name |
| scene | `ctl_package` | where the model package was |

| text block | |
|---|---|
| `control-mod_READ_ME` | the rules in short, and this model's shared textures |
| `control_mod_tools.py` | the sidebar tools (registered when the file opens, if allowed) |
| `control-mod_CHECK`, `control-mod_ADOPT`, `control-mod_BUILD` | reports from the tools |
