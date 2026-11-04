# Control file formats (as implemented by ctlmod)

Reverse engineered from the Steam release of Control: Ultimate Edition (archives
version 9, models version 46). All structures are little endian. Field names in
*italics* are not understood; ctlmod preserves them verbatim. Every claim below is
checked by the code: `ctlmod selftest` re-serialises all 6,707 models byte-identically,
and `tests/test_game_data.py` checks decoded geometry against itself.

## Packfiles: `.bin` + `.rmdp`

`data_packfiles/<ep>-<NNN>-<platform>[-<lang>].{bin,rmdp,packmeta}`, e.g.
`ep100-000-generic` (models, generic data), `ep100-000-pc` (textures), `ep101-…`
(expansion content). The engine enumerates `*.rmdp`; a higher `NNN` is meant to
override lower ones. Whether added archives also need a `.packmeta` is not known yet.

`.bin` (table of contents):

| offset | type | field |
|---|---|---|
| 0 | u8 | endianness (0 = little) |
| 1 | u32 | version (9) |
| 5 | u32 | folder count |
| 9 | u32 | file count |
| 13 | u32 | deletion folder count (1 in every game archive) |
| 17 | u32 | deletion file count (0) |
| 21 | u32 | name table size |
| 25 | u8[128] | tag area: 13 zero bytes, then `x%016x` + NUL at byte 38 = build time as a Windows FILETIME; the rest is uninitialised memory (ctlmod puts its `ctlmod` marker at byte 121) |
| 153 | Folder[n] | 48 bytes each, pre-order, siblings sorted by name |
| … | File[n] | 60 bytes each, grouped by folder, sorted by name |
| … | Folder[deletions] | a second tree of deleted paths; game archives have only an empty root |
| … | File[deletions] | |
| end − size | char[] | NUL-terminated names (not de-duplicated) |

Folder: `u32 name_hash, i64 next_sibling, i64 parent, u32 0xCACACACA, i64 name_offset,
i64 first_child, i64 first_file`. Folder 0 is the unnamed root (hash 0, name −1).

File: `u32 name_hash, i64 next_file, i64 parent_folder, u32 flags, i64 name_offset,
u64 data_offset, u64 data_size, u32 data_crc32, u64 write_time (FILETIME)`.

`name_hash` = zlib CRC-32 of the lower-cased name; `data_crc32` = CRC-32 of the
payload. `.rmdp` holds the raw payloads, each starting on a 32-byte boundary, zero
padded so the file length is a multiple of 32 (no compression).

The engine loads these records directly into memory and converts indices to pointers
(path lookup in `rl_rmdwin10_f.dll`), so the counts and the deletion tree must be
exact: an archive without the deletion root crashes the game at startup (this was
ctlmod 0.1's bug). `tests/test_archive.py` rebuilds small game archives from their
contents and requires byte-identical output.

## Metadata: `.packmeta` (partly decoded, not written by ctlmod)

Each archive's `.packmeta` holds per-file metadata the engine trusts over the files:

```
u32 count, u32 1, u32 count, u32 strings_size
char[strings_size]       NUL-terminated backslash paths
u32[count]               string offsets (path order)
u32 n (= count)
u64[n]                   64-bit path hashes, sorted ascending (binary-search index)
u32[n]                   string offset of each hash's path
u32 type_count (33), Type[]: u32 hash, u32 len, char[len] name, u32 0
                         e.g. r::FileInfoMetadata 0x95e8c0ef, rend::TextureMetadata
                         0x968a49b7, rend::MeshMetadata, ... (identical in every pack)
objects                  grouped by type; each: u32 0xd34db33f, u32 version(2),
                         u32 size, u32 type hash, payload
```

* `r::FileInfoMetadata` payload: `u32 1, u64 file size, u32 data CRC-32`.
* `rend::MeshMetadata` (type `0xfe276448`) contains a nested object with the model's
  **secondary vertex buffer size, primary vertex buffer size, index count and index
  size** (the first fields of the `.binfbx` header), a bounding-box object (6 floats)
  and shader-template information. The renderer allocates GPU buffers and ray-tracing
  data from these.
* `rend::TextureMetadata` contains format, width, height, mip count and per-mip
  streaming offsets (nested objects).
* A pack whose files need no metadata (e.g. the language string tables) has
  `count = 0` and only the type registry.
* The 64-bit path hash is **not** the engine's `StringHash64` (FNV-1a over
  `c | 0x20`), CityHash64, FarmHash64, xxHash or CRC-64 of the stored path; it is
  presumably produced by `ResourceID::createResourceID` (`rl_rmdwin10_f.dll` 0x11d970).
  How objects are linked to files is not decoded yet.

Behaviour confirmed in game (patch archives without a `.packmeta`):

* Metadata is looked up per file across all archives, so a replaced file is described
  by the **original** archive's metadata.
* A replaced texture with the same format, resolution and size works (a different CRC
  is fine).
* A replaced model with the **same size** loads and its geometry is used; a **larger**
  model makes the game abort while loading (`NullMemoryResource` in
  `rl_rmdwin10_f.dll` at 0x1c08e0, the engine's deliberate out-of-pool crash).
* Texture paths in a model's materials are **ignored**: texture bindings come from
  metadata. Changing a material's texture, even with a new material id, has no effect.
* With ray tracing enabled, a model whose buffer sizes or per-LOD mesh list differ from
  the original crashes in `Mesh::finalize`-built tables (`renderer_rmdwin10_f.dll`
  0xa866b). ctlmod therefore keeps the original mesh slots and pads the buffers to the
  original sizes (and caps the surface-area table at the original triangle count).

## Textures: `.tex`

Plain DDS files. Characters use BC1 (`DXT1`), BC3 (`DXT5`), BC4 (DX10 format 80),
BC5 (`ATI2`, normal maps: X,Y in R,G) and BC7 (DX10 98). The game's tool writes
`pitch = width × height` for BC1/BC4 (not the DDS-spec value); ctlmod keeps the
template's convention. Material parameters reference textures as
`runtimedata\data_pc\textures\…\name.tex` = archive path `data_pc/textures/…/name.tex`.

## Models: `.binfbx` (version 46)

```
u32 version (46)
u32 secondary_size, u32 primary_size, u32 index_count, u32 index_size (2|4)
u8  secondary[secondary_size]      vertex stream 1
u8  primary[primary_size]          vertex stream 0
u8  indices[index_count*index_size]
u32 bone_count
    Bone: str name, f32[12] inverse bind (3x3 row-major M, then t), f32[4] bounding
          sphere (model space), i32 parent
u32 unk_a, u32 unk_b, f32 unk_c (usually 0, 0, 1.0)
u32 n, f32[n] LOD switch thresholds, f32 unk_d (usually -1)
f32[4] bounding sphere (bbox centre, max vertex distance), f32[6] bbox (LOD0)
u32 lod_count
u32 material_count, Material[]
u32 n, u32[n] material index per mesh
u32 variant_count, Variant[]: str name, u32[mesh count] alternate material per mesh
u32 n, u32[n] material index per shadow mesh
u32 mesh_count,   Mesh[]
u32 shadow_count, Mesh[]
trailer: u32 0, f32 total LOD0 area, u32 n (= LOD0 triangle count),
         f32[n] cumulative area fraction per LOD0 triangle (surface sampling table)
```

Strings: `u32 length` + bytes, usually including the trailing NUL (not always — e.g.
the shader name `cloth`).

Bone matrices use row vectors in a **left-handed, Y-up** space (metres):
`bone_local = model · M + t`, so the bone's rest position is `−M t` (column form).
Characters face +Z; their left side is −X.

### Material

```
u32 version (7), u64 id (low 16 bits look like flags: …40ea/…80ea/…c0ea)
str name, str shader (standardmaterial, character, cloth, hair, eye, decal, …), str source path
u32[6] flags (render flags / blend / cull / …)
u32 param_count, Param[]: str name, u32 type, value
```

| type | value |
|---|---|
| 0 / 1 / 2 / 3 | f32 / f32×2 / f32×3 / f32×4 |
| 7, 9 | str texture path |
| 8 | sampler (no data) |
| 12 | u32 (bools) |
| 16 | i32 |

### Mesh (also used for shadow meshes)

```
u32 lod, u32 vertex_count (of the whole LOD block), u32 face_count,
u32 secondary_offset (bytes), u32 primary_offset (bytes), u32 index_offset (indices),
u32 max influences per vertex (8, 4, 1 rigid)
f32[4] sphere, f32[6] bbox
u32 unk1 (1)
u8 attr_count, attr[]: u8 stream, u8 usage, u8 format, u8 pad
u32 bone count needed by this LOD (all bones for LOD0; ≈ distinct bones used otherwise)
f32 unk2 (per-mesh, varies 0.5–9; 1.0 for shadow meshes; possibly texel density)
u8  unk3
f32 longest edge length
```

All meshes of a LOD share one vertex block (same offsets/vertex count) and each owns
a range of the index buffer. Shadow meshes reuse those buffers: consecutive meshes are
merged into one index range and the shadow mesh copies the first member's bounds.

### Vertex attributes

| usage | format | size | encoding |
|---|---|---|---|
| 2 position | 0 | 12 | f32 ×3 |
| 8 normal | 1 | 8 | snorm16 ×4: xyz × 32766; **w = island id** |
| 4 tangent | 3 | 4 | u8 ×4 stored B,G,R,A: xyz = c/255·2−1; A = 255 → bitangent sign +1, 0 → −1 |
| 4 colour | 4 | 4 | u8 RGBA |
| 7 uv | 2 | 4 | s16 ×2, value / 4096 (top-left origin) |
| 13 bone indices | 5 | 8 | u16 ×4 (global bone index) |
| 15 rigid bone | 5 | 4 | u32 |
| 5 weights | 6 (or 5) | 4 | u8 ×4, sorted descending, sum = 255 |

Bitangent = cross(normal, tangent) · sign; tangents follow UV0's U direction.
Stream 0 usually holds position + skinning, stream 1 tangent + normal + UVs.
8-influence vertices have two bone-index and two weight attributes.

The *island id* numbers connected pieces of a LOD in order (wrapping at 80; hair
cards get 0–79). Its exact definition comes from the artists' tools, so ctlmod keeps
it per vertex (Blender attribute `ctl_island`) and regenerates it for new geometry.

## ctlmod model package (`mods/<mod>/models/<model>/`)

* `template.binfbx` — original file
* `model.json` — bones (Blender-space head/tail/matrix), materials (params, texture
  slots → archive paths), textures (archive path, PNG, format, sha256), meshes
* `geometry.npz` — per mesh `m<i>_verts`, `m<i>_faces` (welded), per-corner
  `corner_normals`, `corner_uv<k>`, `corner_color<k>`, per-vertex `bone_indices`,
  `bone_weights`, `islands`
* `blender_export/scene.{json,npz}` — what the Blender exporter wrote at the last build

Game → Blender: position `(x, y, z) → (−x, −z, y)`, UV `v → 1 − v`, triangle winding
reversed.
