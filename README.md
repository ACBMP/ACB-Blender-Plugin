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
`tests/blender_oob.py` the same way for the out-of-bounds wall, `tests/blender_switch_map.py -- <forge A> <forge B>` for
switching and closing maps;
`xvfb-run -a blender --factory-startup --python tests/blender_oob_ui.py -- <forge>` edits it in Edit Mode, which needs a window;
`xvfb-run -a blender --factory-startup --enable-event-simulate --python tests/blender_spawns_paths_ui.py -- <forge>` draws an
Escort path with simulated clicks and edits spawns; `tests/blender_flow_edit_ui.py` (also windowed) edits a crowd
flow's points in Edit Mode).
They read a real map, `$ACB_MULTI` (default: the vbox install path).

## Workflow (Blender)

1. **Open ACB Map**: pick a `DataPC_*.forge`. The first open unpacks it into `~/.cache/acbmap/` (a few seconds);
   after that, opening is near-instant. One map per scene: the folder button next to the map's name opens another
   (closing this one), the **X** closes it (removes its objects; edits not saved to a forge are lost). The map shows its textured visual meshes (most detailed LOD), and solid
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
     Collision, and move the spawns onto it. The map still installs over the map it came from. New pieces go
     into the always-loaded grid cell; the few elements new objects are cloned from stay, hidden and never loaded
     in game. The navmesh stays the old map's until you edit it (Navmesh panel: Block Area / Add Walkable).
   - Visible geometry: **Mesh to Scenery** turns a plain mesh into a visible object without collision; **Replace
     Visual** (select a plain mesh, then the element) swaps an element's visible mesh. Material slots holding one of
     the map's materials (`ACBMat_*`, in the material list once a map is open) keep it; a slot whose Base Color
     comes from an Image Texture gets a new map material showing that image (DXT1, resized to a power of two up to
     1024, flat normal map, opaque); other slots get the map's most used textured material. A mesh without a UV map
     gets box-projected uvs.
   - Climb edges (ledges and swing poles): **Show Climb Edges** draws them. **Generate Climb Edges** rebuilds the
     selected elements' edges from their collision, leaving out ledges whose hanging space another object takes or
     that sit less than the minimum drop above any floor (adjust *Min ledge depth* / *Min wall drop* / *Use
     surrounding geometry* in the redo panel); **Remove Climb Edges** drops them. After reshaping collision that has ledges, Apply reports them as stale.
   - Trigger zones (cube/sphere empties under their element): move or scale them.
   - **Out-of-bounds boundary:** one wall object per map (`…:wall`, under the out-of-bounds element), edited as
     one connected piece. See [Editing the out-of-bounds boundary](#editing-the-out-of-bounds-boundary).
   - **Add Element**: places a copy of one of the map's own spawns, benches, haystacks, chase breakers, etc. at
     the 3D cursor.
   - **Spawns** (panel): spawn counts per kind (free-for-all, Team 1-4, Chest), each with an eye to show only the
     kinds you're working on and a **+** that adds one at the 3D cursor (Shift+right-click places the cursor),
     facing the way the view looks. Spawns are drawn as figures coloured by kind, with arrows the way the player
     faces; G moves and R Z turns them as usual. With spawns selected: turn them into another kind in one click
     (a spawn made a Chest gets the chest data layer and joins Chest Capture's list), and **Drop to Ground**.
   - **Navmesh (where NPCs walk)** (panel): the walkable surface every NPC (crowd, guards, Escort VIPs) paths
     over. NPCs don't look at collision, only at this, so to make them walk somewhere new or keep them out of
     somewhere, edit it. **Show Navmesh** draws it (blue wire). **Edit Navmesh** opens it in Edit Mode: X → Faces
     takes ground away, E on boundary edges extends it, F fills new faces, G moves vertices (K cuts a precise
     outline first). **Apply** rebuilds the navigation data of only the pieces that changed (see below); **Revert**
     throws unapplied edits away. With objects selected: **Block Area** takes the ground away under them at their
     height (put a cube where a new wall or crate stands: NPCs path around it), **Add Walkable** makes their top
     faces (up to 45° steep) walkable, joined to the navmesh around them where their edges meet; with *Solid* the
     ground under them stops being walkable (off for a bridge NPCs may also pass under). Crowd flow points must
     stay on the navmesh: an edit that would leave one off it is refused (move the flow first). Edits work inside
     the map's navigation grid only (its NavMeshManager cells, e.g. SanMarco's 96 x 128 m play area).
   - **NPC Paths (crowd flows)** (panel): the green-blue lines crowd NPCs walk (and Escort VIPs follow). Click one,
     **Edit Flow Points**, then in Edit Mode: G moves points, E extends from an end point, right-click → Subdivide
     adds points, X → Vertices deletes; **Apply** (works in Edit Mode). Every point is put onto the navmesh (its
     height snapped to the walkable surface) and the map's navigation data is updated with it: the point's navmesh
     triangle, its waypoint, and the waypoints NPCs use to get on and off the flow at that point. A point that isn't
     over the navmesh is refused and the line goes back. **Show Navmesh** draws the walkable surface (blue wire).
     The navmesh is many separate pieces on purpose: one per walkable surface per 32 m cell. Pieces that touch at a
     cell border are joined by seam links; separate surfaces (curbs, steps, roofs, ledges) only by jump, climb and
     drop links, so gaps between pieces in the wire are normal. Flows can't be copied (a copy would share the
     original's navigation data).
   - **Escort Paths (VIP routes)** (panel): the routes the Escort VIP NPCs walk, made of crowd flows (the
     green-blue lines). **Draw Path**, then click flows in the viewport: each click extends the path through the
     connected flows up to the one clicked, so clicking the start and a few points along the way is enough.
     Every Escort path is a closed loop (the last flow leads back to the first, as on every retail map): clicking
     the first flow again, Enter, Esc or right-click finishes and closes the loop through the connected flows, and
     **Close Loop** closes a path that isn't. **Ctrl+click** a node on the path to make it a VIP spawn +
     checkpoint, **Backspace** undoes the last click. The current path is drawn thick with numbered nodes
     (spheres = VIP spawns, cones = checkpoints); red segments join flows that aren't connected (the VIP may not walk them).
     The node list can also toggle spawn/checkpoint, reorder and remove nodes; **Reverse** flips the path.
     **Hide Escort Paths** (also in the NPC Paths panel) hides the path lines, which sit on the crowd flows they
     follow; Draw Path shows them again.
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
- Navmesh edits (`acbmap/navedit.py` over `acbmap/navmodel.py`) rebuild only the navmeshes whose triangles changed.
  Triangles are clipped at the 32 m cell borders (a navmesh belongs to one cell's NavMeshManager) and kept
  counter-clockwise; unchanged triangles keep their retail neighbours, edge codes and links. Changed edges get a
  seam (LinkType-0 metalink in the lower manager, side A its navmesh) where another navmesh's edge lies along them,
  else wall (0xffe4) where ground was taken away or rises beyond, ledge (0xffe2) where it drops. Jump / climb /
  drop links follow their edges (dropped when a side loses every triangle). Waypoints on changed ground are
  re-found or removed, obstacle corners of the changed area get new ones 0.35 m off the corner, links running over
  removed ground are cut and new / moved waypoints are linked to the waypoints they see (symmetric, nearest 10
  within 40 m); changed triangles list their own and the nearest visible waypoints (pathfinding's way in).
  DirectConnectionSets are the seam-connected components (as retail), the navmesh MoppCode is left empty
  (`NavMesh::BuildMopp` compiles one at load when it's empty), AABVs are recomputed, and every index the edit moved
  is rewritten, including the crowd flows' MetaLinkRef / TriangleArray / WayPointArray. With no edit the managers
  come back byte-identical on every MP map.
- Most retail static collision is merged into compounds (an entity with a `MultiInertComponent`, whose
  `MultiMeshShape` lists member entities and holds one MOPP over all of them; members have `IsMerged=1`). ACB
  uses that MOPP as stored and never gives a merged member its own rigid body. So moving, reshaping or deleting a
  member dissolves its compound (the compound entity goes, its members get `IsMerged=0` and collide on their own),
  and copies are always unmerged. The checks flag a merged entity outside any compound (it would have no
  collision).
- New collision and scenery go into the level-0 grid cell under each piece's origin (`World.GridLayout`: 32 m
  cells), where retail activates every building, with the piece's own MeshShape and Mesh in that cell's entry.
  Tested in game: an element in the whole-map cell (always loaded) collides, but its LOD-selected meshes aren't
  drawn and its climb edges barely grab; the same element in its 32 m cell is drawn and climbable. Map materials and texture sets they borrow from a cell entry that isn't
  always loaded are copied into the new entry (they live inside cell entries and load only with them).

## Known limits (milestone 1)

- **Visual meshes** are replaced whole (Replace Visual), not edited in place, and written without baked ambient
  occlusion or LODs (one mesh at every distance). At most 65535 vertices per mesh. Skinned meshes (elevators,
  crowd, birds) aren't shown. Out-of-bounds fog walls show as wireframe. Materials use the diffuse and
  normal maps (visible in Material Preview / Rendered shading); specular maps are ignored, and materials without a
  diffuse texture (blend spots, decals, FX planes) show plain white.
- **Navmesh** edits are untested in game. New navmeshes, waypoints and their links come from the editor's own
  rules (corner waypoints, line-of-sight links), not Ubisoft's generator, so pathing over edited ground may look
  less natural than retail's. Nothing is generated from collision: new collision still needs Block Area / Add
  Walkable (or hand edits) to change where NPCs go, and new jump / climb / drop links aren't made (NPCs only step
  between pieces that touch). Edits outside the map's NavMeshManager cells are refused (a new cell would need its
  own grid-cell entry). New crowd flows can't be created yet. Where NPCs get on and off an edited flow is
  recomputed by line of sight over the navmesh (nearest 32 waypoints visible through a 0.4 m corridor); retail's
  lists are a smaller selection made by Ubisoft's tool, whose rule isn't known.
- **Climb edges** (GuidanceSystem) are precomputed per entity, and moving an entity carries them along. The
  generator makes ledges and swing poles (the only other type the MP maps use). Over the 11 MP maps it finds 91% of
  retail's edges; about half of what it writes retail doesn't have (short side edges of beam ends and sills, extra
  cornices), so regenerating a retail object adds handholds. Tested in game: generated edges grab, and a wall climbs
  when its handholds are at most about 1.3 m apart (retail: 0.6 m median, the lowest about 0.75 m up).
- Out-of-bounds walls: regenerating a retail boundary from its own corners reproduces its sections (centre, normal,
  size) on 6 of the 9 retail maps that have one exactly; the others differ by one tile split or by up to 22 cm.
  The fog mesh is rebuilt in the retail pattern (strip plus alpha fins at the corners), not edited in place.
- Group children can't be copied on their own; copy the whole group.
