# ctlmod — outfit & model modding for Control: Ultimate Edition

`ctlmod` lets you take any character model out of Control, edit it in Blender
(reshape and remodel meshes, add new pieces, re-weight, repaint textures), turn it back
into game files, and install it with a small mod manager that can switch individual
outfit mods on and off without touching the original game files.

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
* It tells you when an edit reaches beyond your mod: textures shared with other
  models are flagged in Blender, in build reports and at install time.

## What the game accepts

Confirmed by in-game tests (November 2026, Steam version):

| change | works? | how ctlmod handles it |
|---|---|---|
| Repaint / recolour an existing texture, same resolution | ✅ | edit its PNG; the build re-encodes it in the original format |
| Reshape, remodel, delete geometry; re-weight | ✅ | as long as the total geometry fits the original's buffers |
| Add new parts using the model's existing materials | ✅ (the [example mod](#6-example-mod-and-in-game-check)'s halo) | merged into the matching mesh; LOD1 is simplified automatically to make room |
| Model or its vertex/index buffers larger than the original | ❌ crashes | the build pads every model to exactly the original sizes and refuses bigger ones |
| Different number of meshes per LOD | ❌ crashes (with ray tracing on) | the build keeps the original mesh slots |
| Change which texture a material uses / new materials | ❌ ignored by the game | refused at build time with an explanation |
| Brand-new texture files, changed texture resolution | ❌ | refused at build time with an explanation |

The reason for the limits: each game archive has a `.packmeta` metadata file that
records every file's size and how textures are bound to materials, and the game trusts
it over the files themselves. ctlmod cannot write that metadata yet (see
[docs/FORMATS.md](docs/FORMATS.md)), so it keeps every change consistent with the
original metadata. `ctlmod build --experimental` lifts the checks for experiments.

---

## Contents

1. [Setup](#1-setup)
2. [Quick start](#2-quick-start)
3. [The workflow in detail](#3-the-workflow-in-detail)
4. [Editing in Blender — rules of the road](#4-editing-in-blender--rules-of-the-road)
5. [Shared textures: changes that reach other models](#5-shared-textures-changes-that-reach-other-models)
6. [Example mod and in-game check](#6-example-mod-and-in-game-check)
7. [The mod manager](#7-the-mod-manager)
8. [Sharing mods](#8-sharing-mods)
9. [Save game backups](#9-save-game-backups)
10. [Troubleshooting](#10-troubleshooting)
11. [Command reference](#11-command-reference)
12. [How it works / development](#12-how-it-works--development)

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
`ctlmod …` in the rest of this document). The first `add` or `build` also indexes which
models use which textures (about 30 seconds, cached until the game updates).

---

## 2. Quick start

A mod that changes Jesse's default **Bureau Casual** outfit (`jesse_civilian`):

```bash
uv run ctlmod backup-saves
```

```bash
uv run ctlmod find jesse --characters
```

```bash
uv run ctlmod new my_casual --name "My casual outfit" --author "me"
```

```bash
uv run ctlmod add my_casual jesse_civilian_publish_physx
```

```bash
uv run ctlmod open my_casual
```

Edit in Blender, **save** (Ctrl+S), close Blender, then:

```bash
uv run ctlmod build my_casual
```

```bash
uv run ctlmod render my_casual --built
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
uv run ctlmod info jesse_civilian_publish_physx --textures
```

```bash
uv run ctlmod usage jesse_civilian_publish_physx --model
```

`find` accepts substrings or globs (`*civilian*`). `info` shows every mesh (per LOD),
its material and — with `--textures` — which texture file feeds each material slot.
`usage --model` shows which of the model's textures are shared with other models.

Jesse's outfits are `data/objects/characters/intermediate/jesse_<outfit>_publish_physx.binfbx`:

| file | outfit |
|---|---|
| `jesse_civilian` | Bureau Casual (default, confirmed) |
| `jesse_suit`, `jesse_suit_gold` | formal suits |
| `jesse_assistant`, `jesse_janitor`, `jesse_jumpsuit`, `jesse_tactical`, `jesse_urban`, `jesse_master`, `jesse_hedron`, `jesse_expedition`, `jesse_superhero` | other unlockable / DLC outfits |

(These are the game's internal names; `ctlmod render` shows what each looks like.)

### 3.2 Create a mod and add models

```bash
uv run ctlmod new red_casual
```

```bash
uv run ctlmod add red_casual jesse_civilian_publish_physx
```

`add` extracts the model into `mods/red_casual/models/<model>/` and lists the textures
that other models share (see [section 5](#5-shared-textures-changes-that-reach-other-models)):

| file | what it is |
|---|---|
| `<model>.blend` | **the file you edit** |
| `textures/*.png` | every texture the model uses, decoded to PNG |
| `model.json`, `geometry.npz`, `template.binfbx` | the original data; don't edit |

A mod can contain several models (e.g. several outfits); run `add` once per model.

### 3.3 Edit

Open the `.blend` (`ctlmod open red_casual`) and edit — see
[section 4](#4-editing-in-blender--rules-of-the-road). Remember to **save** the
`.blend` and any texture you painted. The `.blend` contains a text block
**`ctlmod_READ_ME`** (Scripting workspace / Text Editor) with these rules and the list
of shared textures.

### 3.4 Build

```bash
uv run ctlmod build red_casual
```

This runs Blender in the background to read your `.blend`, checks every change against
[what the game accepts](#what-the-game-accepts), and writes the game files into
`mods/red_casual/build/files/…` plus a `manifest.json`. A report tells you what
happened, e.g.:

```
texture modified: data_pc/textures/characters/andro/jesse/tops/jesse_civilian_jacket_d.tex
47 of 48 meshes unchanged (original data kept)
model was 44,888 bytes over its size budget; LOD1 now uses LOD2's geometry (fits: 6,996,822 <= 7,878,072)
WARNING: jesse_hands_d.tex is shared - this also changes 8 other model(s): jesse_hedron_publish_physx, ...
```

If an edit can't work in the game, the build stops and lists every problem with what to
do instead. Only files you actually changed end up in the mod.

### 3.5 Check

```bash
uv run ctlmod render red_casual --built
```

```bash
uv run ctlmod render red_casual --built --view face
```

```bash
uv run ctlmod impact red_casual
```

`--built` re-imports the **built game file** (not your `.blend`) and renders it to
`mods/<mod>/renders/`, so you see what the game will get. Views: `front`, `back`,
`side`, `face`; `--lod N` renders a lower LOD. `impact` lists everything the mod
changes outside its own models.

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
* one object per game mesh (`LOD0_03_jesse_civ_jacket`, …), each with an Armature
  modifier,
* the skeleton (`<model>_skeleton`) — the game's bones, in Blender's Z-up space,
  facing −Y, left side on +X,
* one Blender material per game material, with image nodes for every texture slot
  (shared textures are **red** nodes labelled `SHARED xN`),
* the `ctlmod_READ_ME` text block.

### 4.1 Meshes

* **Edit freely**: move/sculpt/remodel vertices, add or delete geometry, retopologise,
  change UVs, split or join objects. Modifiers are applied at build time (except the
  Armature modifier, which is ignored so the mesh is exported in rest pose).
* **Size budget**: the game's metadata records each model's file size and the sizes of
  its two vertex buffers and its index buffer, and allocates memory (including
  ray-tracing data) from those. So the total amount of geometry across all LODs must not
  exceed the original; the build pads whatever is left over so every size matches
  exactly. Meshes you didn't touch keep their original data. If your edits add more
  than that, the build gives LOD1 LOD2's geometry to make room, and if that is still not
  enough it stops and tells you by how much. Keep added parts reasonably light (a
  Decimate modifier helps) — deleting hidden geometry (e.g. body parts under clothing)
  also frees room.
* **The LOD comes from the collection** an object is in. An object outside any `LODn`
  collection counts as LOD0.
* **Mesh slots stay the same**: the game expects every LOD to keep the original
  model's list of meshes (one per material). ctlmod takes care of it:
  * **new objects** are merged into the original mesh of the same LOD that uses the same
    material — so give new parts one of the materials that LOD already has (4.3), plus
    bone weights (4.2). An object with several material slots is split per slot first;
  * **deleting** an original mesh's object leaves an invisible placeholder in its slot.
* Objects whose name starts with `_` are ignored (handy for reference/helper objects).
* Keep **UV map names** `UV0`, `UV1` (the first map is the one textures use).
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

### 4.3 Materials and textures

You control **which of the model's materials each face uses** and **what the textures
look like**:

* **Repaint a texture in place**: paint it in Blender (Texture Paint, then
  *Image → Save*) or edit `textures/<name>.png` in any image editor. Keep its
  **resolution**. The build re-encodes it in the original format (BC1/BC3/BC4/BC5/BC7)
  with the same mip count, so the file has exactly the original size.
* **Shared textures** (red `SHARED xN` nodes; image names start with `SHARED(N)_`):
  painting them changes every other model that uses them — see
  [section 5](#5-shared-textures-changes-that-reach-other-models).
* **Assign the model's existing materials** to new faces (material slots); new parts
  render with that material's textures (confirmed in game with the example mod's halo).
* **Not possible yet** (the build refuses with an explanation): pointing a material's
  image node at a different or new image, duplicating materials into new ones, and
  resizing textures. The game binds textures from its own metadata, so these changes
  would be silently ignored or crash. Want two pieces that share a material to look
  different? Give them different areas of the same texture (UVs), or paint the texture.
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

Some colours come from material parameters, not textures: the formal suit's jacket uses
a tiny flat texture tinted by `g_vColorMultiplier`.

### 4.4 Material variants

Some models switch materials at runtime (`ctlmod info` lists *material variants*,
e.g. Jesse Master's `frame`, NPCs' `hiss`). They are preserved automatically.

### 4.5 What can't be changed

The skeleton, animations, cloth simulation setup (`.binapx`), corrective-shape data
(`.rbf`), physics/ragdoll shapes and the material **shader code**. Big silhouette
changes may clip with animations or cloth, as in any game mod.

### 4.6 Levels of detail (LODs)

The game switches to LOD1–3 at distance. Choose per model (`ctlmod add … --lods X`,
or `ctlmod build <mod> --lods X` to change it):

| mode | LOD1+ come from | use when |
|---|---|---|
| `authored` *(default)* | the `LOD1`…`LOD3` collections, as in the `.blend` | texture-only mods, small edits, or you edit every LOD yourself |
| `decimate` | LOD0, automatically decimated to each original LOD's triangle ratio | geometry changes without hand-made LODs |
| `copy` | LOD0, copied | `--experimental` only (full copies never fit the size budget) |

A LOD whose original uses materials LOD0 doesn't have (Jesse's LOD3 is one baked mesh)
keeps its original geometry in `decimate`/`copy` modes.

With `authored`, LODs you didn't touch keep the **original** look — fine for
texture-only mods, visible at distance if you changed LOD0's shape (then use
`decimate`, or edit the LOD collections too).

---

## 5. Shared textures: changes that reach other models

Many textures are used by more than one model — Jesse's face textures by all 12
outfits, `jesse_hands_d` by 9, fabric detail maps by dozens of models, and
`default*.tex` by hundreds. Repainting a shared texture changes **all** of them. ctlmod
makes this visible at every step:

| where | what you see |
|---|---|
| `ctlmod add` | a list of the model's shared textures and who else uses them |
| Blender | red image nodes `SHARED xN: g_sColorMap`, images named `SHARED(N)_…`, and the `ctlmod_READ_ME` text |
| `ctlmod build` | `WARNING: <texture> is shared - this also changes N other model(s): …` |
| `ctlmod impact <mod>` | every file the mod changes, and every other model affected (`--all` for the full list); also edited-but-not-built textures |
| `ctlmod install` | `note: '<mod>' edits shared textures - this also changes N other model(s)` |
| `ctlmod usage <texture>` | which models use a texture (`--model <name>`: which textures of a model are shared) |

The usage index is built from the game files themselves (all 6,707 models) and is
rebuilt automatically after a game update. If a change to a shared texture is
intended (e.g. a new face for every outfit), there is nothing else to do; otherwise
pick a texture used only by your model (`ctlmod usage <model> --model` marks those
"only this model").

---

## 6. Example mod and in-game check

`mods/demo_civilian` changes the default **Bureau Casual** outfit using only edits the
game accepts:

| change | kind |
|---|---|
| **red tank top** and **red jacket** | textures repainted in place (used only by this outfit) |
| **halo** over the head | new object using the boots' material, weighted to `C_head_JNT` (merged into the boots mesh) |

The halo makes the model about 44 KB larger, so the build reuses LOD2's geometry for
LOD1 — a handy check that the size budget works. To try it:

1. `ctlmod backup-saves`
2. Close the game, then `ctlmod install`.
3. Start the game (Jesse wears Bureau Casual by default).
4. `ctlmod uninstall` (game closed) restores the vanilla game.

In a fresh clone the extracted model isn't in git; recreate the mod with
`ctlmod add demo_civilian jesse_civilian_publish_physx --force`, run
[`examples/demo_edit.py`](examples/demo_edit.py) on the `.blend`
(`--halo jesse_civ_boots --recolor-png <tank top png> --recolor-png <jacket png>`), and
`ctlmod build demo_civilian`. The script also shows how to script Blender edits.

If nothing changes in-game: make sure the game was closed while installing, check
`ctlmod status`, try another patch slot (`ctlmod setup --patch-number 2` then
`ctlmod install`), and see [Troubleshooting](#10-troubleshooting).

---

## 7. The mod manager

Control loads every `*.rmdp` archive in `data_packfiles`, and an archive named
`ep100-001-generic` overrides files of `ep100-000-generic` (same for `-pc`, `ep101-…`,
etc.). One archive per mod would collide as soon as two mods touch the same original
archive — and all outfits live in the same one. So ctlmod **merges all enabled mods
into one patch per original archive**, rebuilt on every `install`.

```bash
uv run ctlmod list
```

```bash
uv run ctlmod disable red_casual
```

```bash
uv run ctlmod order red_casual 1
```

```bash
uv run ctlmod install
```

* **Per-outfit control**: each outfit is a separate model file, so a mod that only
  contains `jesse_civilian` leaves every other outfit alone — unless it repaints shared
  textures, which `install` and `impact` point out. Put outfits in separate mods if you
  want to toggle them separately.
* **Load order**: if two enabled mods change the same file, the one later in
  `ctlmod list` wins and `install`/`status` report the conflict.
* **Safety**: archives written by ctlmod carry a marker; `uninstall` deletes only those.
  If another (non-ctlmod) mod already uses patch slot 001, ctlmod moves to 002.
* **After a game update**: rebuild your mods (`ctlmod build <mod>`) and `ctlmod install`
  again — the size budget and textures come from the current game files.
* **Loose files**: `ctlmod export-loose <folder>` writes the merged files as a folder
  tree, for use with a loose-file loader instead of patch archives.

---

## 8. Sharing mods

```bash
uv run ctlmod pack red_casual
```

```bash
uv run ctlmod unpack dist\red_casual-1.0.0.zip
```

`pack` zips `mod.toml` + the built files into `dist/`; `--sources` also includes the
`.blend` and model package so others can keep editing. The receiver runs `unpack` and
`install`. Edit `name`, `author`, `version`, `description` in `mods/<id>/mod.toml`.
Note that packed mods contain (modified) game assets — share responsibly.

---

## 9. Save game backups

```bash
uv run ctlmod backup-saves
```

Copies your saves (Steam: `Steam\userdata\<id>\870780`; Epic/GOG:
`%LOCALAPPDATA%\Remedy\Control`) into `backups/saves-<timestamp>/` (git-ignored). To
restore, quit the game, pause Steam Cloud, and copy the files back.

---

## 10. Troubleshooting

| problem | fix |
|---|---|
| `Blender not found` / `Game directory not configured` | `ctlmod setup --blender … --game …` |
| `matches N models; be more specific` | use the exact name from `ctlmod find` |
| build: `… cannot work in the game: …` | each listed item says what to do instead (section 4.3) |
| build: `… larger than the original even with the simplest LODs` | lighten or remove added geometry (4.1) |
| build: `mesh '…' has no vertex groups` | weight the mesh (4.2) or parent it to a bone |
| build: `material '…' is not a game material` | use one of the model's materials (4.3) |
| build: `no LOD0 meshes found` | keep meshes in the `LOD0` collection |
| build: `new geometry in LODn uses material '…', which no original LODn mesh uses` | give the part a material that LOD already has (4.1) |
| game crashes while loading a save | something bypassed the checks (e.g. `--experimental`); `ctlmod uninstall`, rebuild without it |
| model looks fine close up but wrong far away | stale LODs with `authored` → `--lods decimate`, or edit LOD1–3 |
| textures look washed out / wrong in Blender | Blender preview ≠ game shader; normal/mask maps are Non-Color |
| `add --force` lost my edits | `--force` re-exports from the game; keep backups of your `.blend` |
| Blender failed: … | the error tail is printed; run the shown Blender command yourself to see the full log |

When the game crashes, Windows logs where: *Event Viewer → Windows Logs →
Application*, "Application Error" for `Control_DX12.exe`. A crash in
`rl_rmdwin10_f.dll` at offset `0x1c08e0` means a file didn't match the game's
metadata (usually a model that grew); one in `renderer_rmdwin10_f.dll` at `0xa866b`
(ray-tracing setup) means a model's buffers or mesh list no longer matched the
original's metadata.

---

## 11. Command reference

| command | purpose |
|---|---|
| `setup [--game] [--blender] [--patch-number]` | configure paths |
| `find [pattern] [--characters] [--files]` | search models / files |
| `info <model> [--textures]` | meshes, materials, textures of a model |
| `usage <texture>` / `usage <model> --model` | who uses a texture / which of a model's textures are shared |
| `extract <glob> [-o dir] [--png]` | dump raw game files (textures as PNG) |
| `new <mod>` | create a mod project |
| `add <mod> <model> [--lods] [--force]` | extract a model and create its `.blend` |
| `open <mod> [model]` | open the `.blend` in Blender |
| `build <mod> [--lods] [--install] [--experimental]` | `.blend` → game files (checked against what the game accepts) |
| `render <mod> [model] [--built] [--view] [--lod]` | preview image |
| `impact <mod> [--all]` | what the mod changes outside its own models |
| `list`, `enable`, `disable`, `order` | mod manager |
| `install [--dry-run]`, `uninstall`, `status` | apply / remove mods |
| `export-loose <dir>` | merged files as folders |
| `pack <mod> [--sources]`, `unpack <zip>` | share mods |
| `backup-saves` | back up save games |
| `selftest [pattern]` | verify the parser against every game model |

---

## 12. How it works / development

```
src/ctlmod/
  archive.py     .bin/.rmdp archives (read/write)
  binfbx.py      .binfbx model files (lossless parse/serialise)
  geometry.py    vertex/index buffer decode/encode
  model.py       rebuild a .binfbx from edited meshes; fit it to the size budget
  texture.py     .tex (DDS) <-> images, BC1/3/4/5/7
  package.py     game <-> Blender conversion, model packages, checked build from Blender
  usage.py       game-wide index of which models use which textures
  gamedata.py    all game archives as one file system
  project.py     mod projects, Blender driver
  installer.py   mod manager
  cli.py         command line
  blender/ctl_blender.py   runs inside Blender (import/export/render)
examples/demo_edit.py      scripted Blender edits (halo, recolour)
docs/FORMATS.md            file format notes, including what is known about .packmeta
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
