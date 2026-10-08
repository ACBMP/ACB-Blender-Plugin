# ACB map editor

Edit Assassin's Creed Brotherhood multiplayer maps (retail or the ported ACFE ones) in Blender: spawns, Escort
paths, chest points, interactive/parkour objects, trigger zones, out-of-bounds walls, collision geometry, and
any serialized field of any gameplay object.

- `acbmap/`: core library and CLI (no Blender). It is built on anvilforge's engine-rule codec (`anvilforge.fastload`),
  included as the `vendor/anvilforge-py` submodule, and on the rules learned porting ACR maps (acr-map-port
  NOTES.md).
- `blender/acb_map_editor/`: the Blender add-on (sidebar tab **ACB**, press N in the 3D viewport).

## Requirements

- Blender 4.2 or newer (developed on 5.2).
- **liblzo2**, the compression library ACB forges use. On Linux, install it from the package manager (`pacman -S
  lzo`, `apt install liblzo2-2`, `dnf install lzo`). On Windows/macOS, bundle the library into the zip with
  `--lzo` (below), or point the `ANVILFORGE_LZO` environment variable at it.
- Nothing else: no pip packages.

## Install (any machine)

```
git clone --recursive git@github.com:ACBMP/ACB-Blender-Plugin.git
cd ACB-Blender-Plugin
python3 build_addon.py            # -> dist/acb_map_editor.zip  (add --lzo path/to/lzo2.dll on Windows)
```

In Blender, use **Edit → Preferences → Add-ons → Install from Disk** to pick `dist/acb_map_editor.zip`, then enable
**ACB Map Editor**. In the add-on's preferences, set the game's `multi` folder; Install/Restore use it.

If you already cloned without `--recursive`, run `git submodule update --init`.

## Development setup

Instead of the zip, link the checkout so code changes apply on Blender restart:

```
git submodule update --init
python3 -m venv .venv && .venv/bin/pip install -e vendor/anvilforge-py -e . pytest numpy
mkdir -p ~/.config/blender/5.2/scripts/addons   # Blender only creates it on first add-on install
ln -s "$PWD/blender/acb_map_editor" ~/.config/blender/5.2/scripts/addons/acb_map_editor
```

Tests: `.venv/bin/pytest tests/` (headless edits), and
`blender --background --factory-startup --python tests/blender_smoke.py -- <map forge>` (the add-on end to end;
`tests/blender_oob.py` the same way for the out-of-bounds wall).
They read a real map, `$ACB_MULTI` (default: the vbox install path).

## Workflow (Blender)

1. **Open ACB Map**: pick a `DataPC_*.forge`. The first open unpacks it into `~/.cache/acbmap/` (a few seconds);
   after that, opening is near-instant. The map shows its textured visual meshes (most detailed LOD), and solid
   shading is switched to texture colour. Untick *Visual meshes* in the file dialog for the collision-only view.
2. Edit:
   - Move, rotate or scale elements as usual. **Shift+D** copies an element, **X** deletes it. Moving a group moves
     its children. Scenery is selectable too: clicking a building selects its entity.
   - Collision meshes: **Show Collision** (they're hidden while visuals are shown), then use edit mode and assign
     faces to the collision material slots. Shapes are shared between objects as in the game; **Make Shape
     Unique** splits one off. **Mesh to Collision** turns any plain mesh into new static collision that is also
     visible in game, with climb edges. Big meshes are split into pieces (at most *Max piece size*, 20000
     triangles and a 15-tile uv span each).
   - **New map from an existing one:** **Clear Scenery** removes every visible mesh and static collision element
     (and the far-distance stand-ins of the old buildings), keeping spawns, chests, interactive objects, zones and
     out-of-bounds. Then import your geometry (File → Import → Wavefront OBJ / FBX), select it and use Mesh to
     Collision, and move the spawns onto it. The map still installs over the map it came from.
   - Visible geometry: **Mesh to Scenery** turns a plain mesh into a visible object without collision; **Replace
     Visual** (select a plain mesh, then the element) swaps an element's visible mesh. Material slots holding one of
     the map's materials (`ACBMat_*`, in the material list once a map is open) keep it; other slots get the map's
     most used textured material. A mesh without a UV map gets box-projected uvs.
   - Climb edges (ledges and swing poles): **Show Climb Edges** draws them. **Generate Climb Edges** rebuilds the
     selected elements' edges from their collision, leaving out ledges whose hanging space another object takes or
     that sit less than the minimum drop above any floor (adjust *Min ledge depth* / *Min wall drop* / *Use
     surrounding geometry* in the redo panel); **Remove Climb Edges** drops them. After reshaping collision that has ledges, Apply reports them as stale.
   - Trigger zones (cube/sphere empties under their element): move or scale them.
   - **Out-of-bounds boundary:** one wall object per map (`…:wall`, under the out-of-bounds element), edited as
     one connected piece. See [Editing the out-of-bounds boundary](#editing-the-out-of-bounds-boundary).
   - **Add Element**: places a copy of one of the map's own spawns, benches, haystacks, chase breakers, etc. at
     the 3D cursor.
   - **Escort Paths**: select crowd-flow points and use *Append Selected Flows*. Toggle VIP spawn/checkpoint per
     node, and reorder or remove nodes.
   - **Inspector**: every serialized field of the active element and its components. Click a value to edit it;
     links jump to their target.
3. **Apply Edits** (also done by Save) pushes Blender changes into the document. **Save Forge** writes
   `~/.cache/acbmap/edited/<forge>` and runs the structural checks against the original.
4. **Install into Game** replaces the game's copy (the original is kept once as `<forge>.orig`). **Restore
   Original** puts it back. Every player needs the edited forge.

Undo: Blender undo covers everything up to *Apply*. Inspector edits go straight into the document and aren't
undoable, so to back out, reopen the map.

## Editing the out-of-bounds boundary

The boundary is a polyline: one vertex per wall corner, at the **base** of the wall (where it meets the ground).
The wall surface above it is only a display of the wall's height.

1. **Edit Boundary** (ACB panel, Tools). It selects the wall, enters Edit Mode with vertex selection, turns X-ray
   on and switches to a top view framed on the whole boundary. By hand: select the `…:wall` object, Tab, 1
   (vertex select), Alt+Z (X-ray), numpad 7 (top view).
2. Select corners: click a corner dot (it turns orange); Shift+click adds to the selection, B box-selects,
   C brush-selects, Alt+click on a segment selects the whole wall. Without X-ray, corners behind the wall surface
   or inside the ground can't be clicked.
3. Edit:
   - **G** moves the selected corners (G Z: only up/down; G X / G Y: along one axis).
   - **E** extends the wall with a new corner from a selected end corner.
   - **Right-click → Subdivide** on a selected segment adds a corner in its middle.
   - **X → Dissolve Vertices** removes the selected corners (the neighbours are joined).
   - Each corner's wall height is its `acb_height` attribute (view and edit it in the Spreadsheet editor); new
     corners copy their neighbour's.
4. **Apply** (or Save). The gameplay sections (tiles of at most 5 m), the collision strip and the fog mesh are all
   regenerated from the polyline, so the wall stays one connected piece. Sections face the inside of a closed
   boundary; open walls keep the side they had. Tab back to Object Mode whenever you like (Apply also picks up
   edits made while still in Edit Mode).

## CLI

```
acbmap dump <forge> [-v]                  element kinds and counts
acbmap roundtrip <forge> [--touch TYPE..] save without edits, verify the content is identical
acbmap check <forge> [--against SRC]      structural checks (only problems SRC doesn't have)
acbmap install <forge> | uninstall <name> | status
```

## What the save keeps right (engine rules)

- Only touched roots are re-encoded and only their entries rebuilt; everything else is byte-identical. Entries
  are laid out retail-style, so no header straddles a 0x8000 streaming chunk.
- New roots go into their source's grid-cell entry, inside the block's activated prefix
  (`NumberOfObjectsToActivate`). New ids come from 0xE9600000-0xEAA00000, one 64k
  slice per world, a run no ACB multi forge uses. Never 0xF0000000 and up: the engine numbers the objects it
  creates at runtime from there, and a forge object with such an id never appears in game (maps saved by
  earlier editor versions are renumbered when opened).
- Escort paths and chest points are written as an AdditionalWorldData override entry in the map forge, under the
  skins table's id. The skins forges are never edited. Chest data is regenerated from the map's chest spawns,
  with phantom twins exactly as retail has them.
- Edited collision drops the stored MOPP (`MoppCodeVersionNumber=0`), so ACB compiles its own at load.
- Most retail static collision is merged into compounds (an entity with a `MultiInertComponent`, whose
  `MultiMeshShape` lists member entities and holds one MOPP over all of them; members have `IsMerged=1`). ACB
  uses that MOPP as stored and never gives a merged member its own rigid body. So moving, reshaping or deleting a
  member dissolves its compound (the compound entity goes, its members get `IsMerged=0` and collide on their own),
  and copies are always unmerged. The checks flag a merged entity outside any compound (it would have no
  collision).
- New collision and scenery go into the whole-map grid cell, which is always loaded (level-0 cells stream in only
  within a radius of the World's anchor). Map materials and texture sets they borrow from a cell entry that isn't
  always loaded are copied into the new entry (they live inside cell entries and load only with them).

## Known limits (milestone 1)

- **Visual meshes** are replaced whole (Replace Visual), not edited in place, and written without baked ambient
  occlusion or LODs (one mesh at every distance). At most 65535 vertices per mesh. Skinned meshes (elevators,
  crowd, birds) aren't shown. Out-of-bounds fog walls show as wireframe. Materials use the diffuse and
  normal maps (visible in Material Preview / Rendered shading); specular maps are ignored, and materials without a
  diffuse texture (blend spots, decals, FX planes) show plain white.
- **Navmesh isn't rebuilt.** Crowd flows are locked because their points carry navmesh triangle refs. NPCs ignore
  new collision, and on a cleared map they keep walking the old map's navmesh.
- **Climb edges** (GuidanceSystem) are precomputed per entity, and moving an entity carries them along. The
  generator makes ledges and swing poles (the only other type the MP maps use; poles match retail's: 186 of 190
  found, 178 of 184 generated are retail poles). Ledges follow physical rules, but retail's were placed by hand:
  facades get a ledge on every cornice where retail often has a few, so regenerating a retail object changes how it
  climbs (about a quarter of retail ledge length is reproduced within 15 cm). Not yet tested in game.
- Out-of-bounds walls: regenerating a retail boundary from its own corners reproduces its sections (centre, normal,
  size) on 6 of the 9 retail maps that have one exactly; the others differ by one tile split or by up to 22 cm.
  The fog mesh is rebuilt in the retail pattern (strip plus alpha fins at the corners), not edited in place.
- Group children can't be copied on their own; copy the whole group.
