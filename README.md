# ctlmod — outfit & model modding for Control: Ultimate Edition

`ctlmod` lets you take any character model out of Control, edit it in Blender
(meshes, UVs, weights, materials, textures — including **adding** meshes, materials
and textures), turn it back into game files, and install it with a small mod manager
that can switch individual outfit mods on and off without touching the original game
files.

```
game archives ──export──▶ model package ──import──▶ .blend  (you edit here)
                                                      │
game ◀──install── patch archives ◀──compose── mod build ◀──build──┘
```

* Works for **any** model: everything is discovered by reading the game's archives —
  nothing is hard-coded per character. `ctlmod find --characters` lists 180+
  character models (Jesse's 12 outfits, NPCs, enemies…).
* Original game files are never modified. Mods are installed as extra *patch archives*
  (`ep100-001-generic.bin/.rmdp`, …) that ctlmod tags and can remove at any time.
* All file formats were reverse engineered and are verified against **all 6,707
  models** in the game (byte-exact parse/re-serialise, `ctlmod selftest`).

> **Status.** Everything up to and including writing the patch archives is
> implemented and tested (53 automated tests, including a full Blender round trip).
> Whether the game picks the patch archives up is based on how the engine and existing
> community tools behave, but **it has not been confirmed in-game yet** — do the
> [smoke test](#5-smoke-test-confirm-the-game-loads-mods) first.

---

## Contents

1. [Setup](#1-setup)
2. [Quick start](#2-quick-start)
3. [The workflow in detail](#3-the-workflow-in-detail)
4. [Editing in Blender — rules of the road](#4-editing-in-blender--rules-of-the-road)
5. [Smoke test: confirm the game loads mods](#5-smoke-test-confirm-the-game-loads-mods)
6. [The mod manager](#6-the-mod-manager)
7. [Sharing mods](#7-sharing-mods)
8. [Save game backups](#8-save-game-backups)
9. [Troubleshooting](#9-troubleshooting)
10. [Command reference](#10-command-reference)
11. [How it works / development](#11-how-it-works--development)

---

## 1. Setup

Requirements: Windows, [uv](https://docs.astral.sh/uv/), Blender 4.2+ (tested with
5.2), Control: Ultimate Edition (tested with the Steam version).

```bash
uv sync
```

```bash
uv run ctlmod setup
```

`setup` auto-detects the game and Blender and writes `ctlmod.toml`. If detection
fails, pass the paths explicitly:

```bash
uv run ctlmod setup --game "C:\Program Files (x86)\Steam\steamapps\common\Control" --blender "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe"
```

All commands below are run from this folder as `uv run ctlmod …` (shortened to
`ctlmod …` in the rest of this document).

---

## 2. Quick start

A mod that changes Jesse's **Formal** outfit (`jesse_suit`):

```bash
uv run ctlmod backup-saves
```

```bash
uv run ctlmod find jesse --characters
```

```bash
uv run ctlmod new my_suit --name "My formal suit" --author "me"
```

```bash
uv run ctlmod add my_suit jesse_suit_publish_physx
```

```bash
uv run ctlmod open my_suit
```

Edit in Blender, **save** (Ctrl+S), close Blender, then:

```bash
uv run ctlmod build my_suit
```

```bash
uv run ctlmod render my_suit --built
```

```bash
uv run ctlmod install
```

Start the game and wear the outfit. To go back to the vanilla game at any time:

```bash
uv run ctlmod uninstall
```

---

## 3. The workflow in detail

### 3.1 Find the model

```bash
uv run ctlmod find jesse --characters
```

```bash
uv run ctlmod info jesse_suit_publish_physx --textures
```

`find` accepts substrings or globs (`*civilian*`). `info` shows every mesh (per LOD),
its material, the material's shader and — with `--textures` — which texture file feeds
each material slot and which archive it lives in.

Jesse's outfits are `data/objects/characters/intermediate/jesse_<outfit>_publish_physx.binfbx`:

| file | outfit |
|---|---|
| `jesse_civilian` | Bureau Casual (default) |
| `jesse_suit`, `jesse_suit_gold` | Formal / Golden Suit |
| `jesse_assistant`, `jesse_janitor`, `jesse_jumpsuit`, `jesse_tactical`, `jesse_urban`, `jesse_master`, `jesse_hedron`, `jesse_expedition`, `jesse_superhero` | other unlockable / DLC outfits |

(The names are the game's internal names; which in-game outfit each maps to is easy
to confirm with `ctlmod render`.)

### 3.2 Create a mod and add models

```bash
uv run ctlmod new red_suits
```

```bash
uv run ctlmod add red_suits jesse_suit_publish_physx
```

`add` extracts the model into `mods/red_suits/models/<model>/`:

| file | what it is |
|---|---|
| `<model>.blend` | **the file you edit** |
| `textures/*.png` | every texture the model uses, decoded to PNG |
| `model.json`, `geometry.npz`, `template.binfbx` | the original data; don't edit |

A mod can contain several models (e.g. several outfits); run `add` once per model.
Each model has a **LOD mode** (`--lods`, see [4.6](#46-levels-of-detail-lods)).

### 3.3 Edit

Open the `.blend` (`ctlmod open red_suits`) and edit away — see
[section 4](#4-editing-in-blender--rules-of-the-road) for what is possible.
Remember to **save** the `.blend` and any texture you painted.

### 3.4 Build

```bash
uv run ctlmod build red_suits
```

This runs Blender in the background to read your `.blend`, then writes the game files
into `mods/red_suits/build/files/…` and a `manifest.json`. The report tells you what
changed, e.g.:

```
texture modified: data_pc/textures/characters/andro/jesse/tops/jesse_suit_blouse_d.tex
material added: jesse_suit_jacket_red (based on jesse_suit_jacket)
meshes: 32 (LODs [0, 1, 2, 3]), materials: 12
```

Only textures you actually changed are included in the mod.

### 3.5 Check

```bash
uv run ctlmod render red_suits --built
```

```bash
uv run ctlmod render red_suits --built --view face
```

`--built` re-imports the **built game file** (not your `.blend`) and renders it to
`mods/<mod>/renders/`, so you see exactly what the game will get. Views: `front`,
`back`, `side`, `face`; `--lod N` renders a lower LOD.

### 3.6 Install, test, uninstall

```bash
uv run ctlmod install
```

```bash
uv run ctlmod status
```

```bash
uv run ctlmod uninstall
```

Quit the game before installing or uninstalling (the game keeps archives open).

---

## 4. Editing in Blender — rules of the road

The `.blend` contains:

* a collection named after the model, with sub-collections **`LOD0` … `LOD3`**
  (only LOD0 is visible by default),
* one object per game mesh (`LOD0_03_jesse_suit_jacket`, …), each with an Armature
  modifier,
* the skeleton (`<model>_skeleton`) — the game's bones, in Blender's Z-up space,
  facing −Y, left side on +X,
* one Blender material per game material, with image nodes for every texture slot.

### 4.1 Meshes

* **Edit freely**: move/sculpt/remodel vertices, add or delete geometry, retopologise,
  change UVs, split or join objects. Modifiers are applied at build time (except the
  Armature modifier, which is ignored so the mesh is exported in rest pose).
* **The LOD comes from the collection** an object is in. An object outside any `LODn`
  collection counts as LOD0.
* **Add new meshes**: put the object in `LOD0` (and other LODs if you author them),
  give it a game material (4.3) and bone weights (4.2). An object with several material
  slots is split into one game mesh per slot.
* Objects whose name starts with `_` are ignored (handy for reference/helper objects).
* Delete a game mesh by deleting its object.
* Keep **UV map names** `UV0`, `UV1` (the first map is the one textures use). A mesh
  without UVs still builds, but textures won't map.
* Normals: custom split normals are exported as you see them. Recalculating or
  "Smooth by Angle" are fine.
* Don't apply the object transform to the skeleton or move the armature object; moving
  mesh objects is fine (their transform is baked in).

### 4.2 Skinning (bone weights)

* Vertex groups named after **bones of the skeleton** are the bone weights. Bones can't
  be added, removed or renamed — the skeleton and all animations live in other game
  files. Groups that aren't bone names are ignored (with a warning).
* LOD0 of Jesse uses up to **8 influences per vertex**, LOD3 uses 4; extra influences
  are dropped (smallest first) and the rest renormalised automatically.
* Easiest ways to weight a new mesh:
  * *Data Transfer* modifier (Vertex Data → Vertex Groups, nearest face interpolated)
    from the body/clothing it sits on, or
  * parent it with *Armature Deform → With Automatic Weights*, or
  * for rigid accessories, assign everything to one bone (e.g. `C_head_JNT`) or parent
    the object to a bone (*Object → Parent → Bone*) — that also works without groups.
* Clothing that simulates in-game (Jesse's jacket tails, hair strands, straps) is driven
  by **cloth joints** (`…_cloth_JNT12`, `…hair…`). Weighting your new geometry to those
  joints makes it move with the simulation.
* Every vertex of a skinned model must have some weight; unweighted vertices are bound
  to the root bone and reported.

### 4.3 Materials

Each game material keeps its shader and parameters; in Blender you control **which
material each face uses** and **which images feed its texture slots**.

* **Edit an existing texture in place**: paint it in Blender (Texture Paint, then
  *Image → Save*) or edit `textures/<name>.png` in any image editor. The build notices
  the change and re-encodes it in the original format.
  ⚠ Textures are often **shared** — e.g. `jesse_hands_d`, the face textures, or
  `jesse_suit_pants_d` (used by both pants *and* jacket). Changing a shared texture
  changes it on every model that uses it, in every outfit.
* **Give one mesh its own look (new material + new texture)**: in Blender, duplicate
  the material (material slot → *New Material* copy button, or `material.copy()`),
  rename it, assign it to the faces, and swap images in its image nodes (open/drag a new
  image into the node labelled e.g. `g_sColorMap`). The copy keeps the original's
  shader and parameters; the build adds it as a new game material and encodes new
  images in the same format as the slot's original texture.
  New textures are stored under `data_pc/textures/ctlmod/<mod>/…` — a **new** file path
  in the game. Whether the game loads brand-new paths from a patch archive is not
  verified yet (see section 5); editing textures in place always works the same way the
  original files do.
* Don't delete the image nodes or their `ctl_param` property, and don't rename the
  original materials (a renamed original becomes a new material, which is harmless but
  unnecessary).
* The preview material in Blender is an approximation (base colour × `g_vColorMultiplier`,
  normal map). The game's shaders also use smoothness/specular/detail/fuzz maps and
  parameters that Blender doesn't show — check the result in-game.

Texture slots you'll see most:

| slot | content | format |
|---|---|---|
| `g_sColorMap` | colour (+ alpha for hair/lashes) | BC1/BC3/BC7 |
| `g_sNormalMap` | tangent-space normal map; only R,G are stored (B is rebuilt) | BC5 |
| `g_sSmoothnessMap`, `g_sSpecularColorMap` | grey masks | BC4 |
| `g_sAlphaTestSampler` | alpha for alpha-tested materials | BC3 |
| `g_sBaseDetailMap` | tiling fabric detail (cloth shader) | BC5 |

Textures may change size (keep power-of-two sizes such as 1024 or 2048).

### 4.4 Material variants

Some models switch materials at runtime (`ctlmod info` lists *material variants*,
e.g. Jesse Master's `frame`, NPCs' `hiss`). They are preserved automatically for
original meshes; new meshes use their own material in every variant.

### 4.5 What can't be changed

The skeleton, animations, cloth simulation setup (`.binapx`), corrective-shape data
(`.rbf`), physics/ragdoll shapes and the material **shader code**. Big silhouette
changes may clip with animations or cloth, as in any game mod.

### 4.6 Levels of detail (LODs)

The game switches to LOD1–3 at distance. Choose per model (`ctlmod add … --lods X`,
or `ctlmod build <mod> --lods X` to change it):

| mode | LOD1+ come from | use when |
|---|---|---|
| `authored` *(default)* | the `LOD1`…`LOD3` collections, exactly as in the `.blend` | you only re-textured, or you edit every LOD yourself |
| `copy` | LOD0, copied | quick tests; always consistent, slightly heavier |
| `decimate` | LOD0, automatically decimated to the original LOD's triangle ratio | geometry changes without hand-made LODs |

With `authored`, LODs you didn't touch keep the **original** look — fine for
texture-only mods, wrong if you changed LOD0's shape.

---

## 5. Smoke test: confirm the game loads mods

Do this once to confirm patch archives work on your game version. The included example
mod **`demo_halo`** (Jesse's Formal outfit) is already built and tests three things at
once � open `mods/demo_halo/renders/` to see what to expect:

| change | kind | if you see it in-game� |
|---|---|---|
| **red blouse** | in-place texture edit | patch archives work |
| **halo** over the head | new mesh | geometry mods work |
| **red jacket** | new material + new texture path | new files load too |

1. `ctlmod backup-saves`
2. Close the game, then `ctlmod install`. (In a fresh clone the extracted model isn't in
   git; recreate it first with `ctlmod add demo_halo jesse_suit_publish_physx --force`,
   then run `examples/demo_edit.py` with `--halo --red-jacket --recolor-png <blouse png>`
   on the `.blend`, and `ctlmod build demo_halo`.)
3. Start the game and equip the **Formal** outfit.
4. `ctlmod uninstall` (game closed) restores the vanilla game.

If the blouse and halo change but the jacket doesn't, new texture paths aren't loaded
by the game � use in-place texture edits for recolours (section 4.3). The edits were
made with [`examples/demo_edit.py`](examples/demo_edit.py), which also shows how to
script Blender edits.

If nothing changes in-game: make sure the game was closed while installing, check
`ctlmod status`, try another patch slot (`ctlmod setup --patch-number 2` then
`ctlmod install`), and see [Troubleshooting](#9-troubleshooting).

---

## 6. The mod manager

Control loads every `*.rmdp` archive in `data_packfiles`, and an archive named
`ep100-001-generic` overrides files of `ep100-000-generic` (same for `-pc`, `ep101-…`,
etc.). One archive per mod would collide as soon as two mods touch the same original
archive — and all outfits live in the same one. So ctlmod **merges all enabled mods
into one patch per original archive**, rebuilt on every `install`.

```bash
uv run ctlmod list
```

```bash
uv run ctlmod disable red_suits
```

```bash
uv run ctlmod order red_suits 1
```

```bash
uv run ctlmod install
```

* **Per-outfit control**: each outfit is a separate model file, so a mod that only
  contains `jesse_suit` leaves every other outfit alone. Put outfits in separate mods if
  you want to toggle them separately.
* **Load order**: if two enabled mods change the same file, the one later in
  `ctlmod list` wins and `install`/`status` report the conflict.
* **Safety**: archives written by ctlmod carry a marker; `uninstall` deletes only those.
  If another (non-ctlmod) mod already uses patch slot 001, ctlmod moves to 002.
* **After a game update**: run `ctlmod install` again (and rebuild mods if the update
  changed the models you edit).
* **Loose files**: `ctlmod export-loose <folder>` writes the merged files as a folder
  tree, for use with a loose-file loader instead of patch archives.

---

## 7. Sharing mods

```bash
uv run ctlmod pack red_suits
```

```bash
uv run ctlmod unpack dist\red_suits-1.0.0.zip
```

`pack` zips `mod.toml` + the built files into `dist/`; `--sources` also includes the
`.blend` and model package so others can keep editing. The receiver runs `unpack` and
`install`. Edit `name`, `author`, `version`, `description` in `mods/<id>/mod.toml`.
Note that packed mods contain (modified) game assets — share responsibly.

---

## 8. Save game backups

```bash
uv run ctlmod backup-saves
```

Copies your saves (Steam: `Steam\userdata\<id>\870780`; Epic/GOG:
`%LOCALAPPDATA%\Remedy\Control`) into `backups/saves-<timestamp>/`. To restore, quit
the game, pause Steam Cloud, and copy the files back. A backup was made on
2026-11-03 in `backups/saves-20261103-185843/`.

---

## 9. Troubleshooting

| problem | fix |
|---|---|
| `Blender not found` / `Game directory not configured` | `ctlmod setup --blender … --game …` |
| `matches N models; be more specific` | use the exact name from `ctlmod find` |
| build: `mesh '…' has no vertex groups` | weight the mesh (4.2) or parent it to a bone |
| build: `material '…' is not a game material` | use a copy of an existing material (4.3) |
| build: `no LOD0 meshes found` | keep meshes in the `LOD0` collection |
| model looks fine in Blender but is wrong far away | LOD mode `authored` with stale LODs → `--lods decimate` |
| textures look washed out / wrong in Blender | Blender preview ≠ game shader; normal/mask maps are Non-Color |
| `add --force` lost my edits | `--force` re-exports from the game; keep backups of your `.blend` |
| Blender failed: … | the error tail is printed; run the shown Blender command yourself to see the full log |

---

## 10. Command reference

| command | purpose |
|---|---|
| `setup [--game] [--blender] [--patch-number]` | configure paths |
| `find [pattern] [--characters] [--files]` | search models / files |
| `info <model> [--textures]` | meshes, materials, textures of a model |
| `extract <glob> [-o dir] [--png]` | dump raw game files (textures as PNG) |
| `new <mod>` | create a mod project |
| `add <mod> <model> [--lods] [--force]` | extract a model and create its `.blend` |
| `open <mod> [model]` | open the `.blend` in Blender |
| `build <mod> [--lods] [--install]` | `.blend` → game files |
| `render <mod> [model] [--built] [--view] [--lod]` | preview image |
| `list`, `enable`, `disable`, `order` | mod manager |
| `install [--dry-run]`, `uninstall`, `status` | apply / remove mods |
| `export-loose <dir>` | merged files as folders |
| `pack <mod> [--sources]`, `unpack <zip>` | share mods |
| `backup-saves` | back up save games |
| `selftest [pattern]` | verify the parser against every game model |

---

## 11. How it works / development

```
src/ctlmod/
  archive.py     .bin/.rmdp archives (read/write)
  binfbx.py      .binfbx model files (lossless parse/serialise)
  geometry.py    vertex/index buffer decode/encode
  model.py       rebuild a .binfbx from edited meshes (shadow meshes, bounds, area table)
  texture.py     .tex (DDS) <-> images, BC1/3/4/5/7
  package.py     game <-> Blender conversion, model packages, build from Blender
  gamedata.py    all game archives as one file system
  project.py     mod projects, Blender driver
  installer.py   mod manager
  cli.py         command line
  blender/ctl_blender.py   runs inside Blender (import/export/render)
examples/demo_edit.py      scripted Blender edits (halo, red jacket, recolour)
docs/FORMATS.md            file format notes
tests/                     pytest suite
```

Tests:

```bash
uv run pytest
```

Synthetic tests always run; tests needing the game or Blender skip automatically when
those aren't found (`CTLMOD_GAME_DIR`, `CTLMOD_BLENDER` override detection). Set
`CTLMOD_FULL_SCAN=1` to round-trip every game model instead of only characters.

File format details are in [docs/FORMATS.md](docs/FORMATS.md).
