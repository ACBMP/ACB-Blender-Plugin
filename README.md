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
python3 -m venv .venv && .venv/bin/pip install -e vendor/anvilforge-py -e . pytest
mkdir -p ~/.config/blender/5.2/scripts/addons   # Blender only creates it on first add-on install
ln -s "$PWD/blender/acb_map_editor" ~/.config/blender/5.2/scripts/addons/acb_map_editor
```

Tests: `.venv/bin/pytest tests/` (headless edits), and
`blender --background --factory-startup --python tests/blender_smoke.py -- <map forge>` (the add-on end to end).
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
     Unique** splits one off. **Mesh to Collision** turns any plain mesh into new static collision, with climb
     edges.
   - Climb edges (ledges): **Show Climb Edges** draws them. **Generate Climb Edges** rebuilds the selected
     elements' ledges from their own collision (adjust *Min ledge depth* / *Min wall drop* in the redo panel);
     **Remove Climb Edges** drops them. After reshaping collision that has ledges, Apply reports them as stale.
   - Trigger zones (cube/sphere empties under their element) and out-of-bounds wall quads: move or scale them.
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
  (`NumberOfObjectsToActivate`). New ids come from 0xF0xxxxxx, one 64k slice per world; no ACB forge uses that
  range.
- Escort paths and chest points are written as an AdditionalWorldData override entry in the map forge, under the
  skins table's id. The skins forges are never edited. Chest data is regenerated from the map's chest spawns,
  with phantom twins exactly as retail has them.
- Edited collision drops the stored MOPP (`MoppCodeVersionNumber=0`), so ACB compiles its own at load.

## Known limits (milestone 1)

- **Visual meshes are display-only.** Moving, copying or deleting an entity carries its visual along, but the meshes
  themselves can't be edited, and new collision (Mesh to Collision) has no visible geometry in game. Skinned meshes
  (elevators, crowd, birds) aren't shown. Out-of-bounds fog walls show as wireframe. Materials use the diffuse and
  normal maps (visible in Material Preview / Rendered shading); specular maps are ignored, and materials without a
  diffuse texture (blend spots, decals, FX planes) show plain white.
- **Navmesh isn't rebuilt.** Crowd flows are locked because their points carry navmesh triangle refs. NPCs ignore
  new collision.
- **Climb edges** (GuidanceSystem) are precomputed per entity, and moving an entity carries them along. Generated
  edges are ledges only (no beams, poles or ropes), taken from the entity's own collision. Ubisoft's tool also used
  neighbouring geometry, so regenerating a retail object changes its ledges: on San Marco, Mont St-Michel and
  Firenze about 11% of the retail ledge length is reproduced, and most generated ledges aren't in retail. Not yet
  tested in game.
- Out-of-bounds quads: position, rotation and size round-trip exactly. Whether the stored position is the wall's
  bottom or its centre isn't verified in-game, so the drawn quad may sit half a height off.
- Group children can't be copied on their own; copy the whole group.
