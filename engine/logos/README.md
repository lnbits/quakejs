# Map logos

The extension owner supplied `bitfest.png` and `lnbits.png`. These are the
original, unchanged RGBA images. `update-maps.py` losslessly converts them to TGA
inside the game pack for the shipped browser renderer.

`placements.json` specifies two Bitfest and two LNbits wall decals for each of
the six selected maps. Coordinates and surface indexes refer to the pinned,
unmodified OpenArena BSPs. `view` records an interior inspection position; it is
not used by the game or the decal builder.

`logo_maps.py` validates each decal footprint against its wall triangles, inserts
four world surfaces, and connects them to their parent walls' visibility leaves.
The decals preserve image proportions, render with transparency, and use a small
offset to prevent flickering against the wall. Their textures are clamped and
excluded from texture-detail reduction so lettering stays clear.

Only rendering data changes. Entities, collision brushes/planes, spawn positions,
lightmaps and visibility data remain unchanged. There are four extra quads (eight
triangles) per map; no new runtime scripts, network requests or server processes.

To rebuild, run the existing command documented in `../BUILD.md`. Always use the
pinned original OpenArena release; applying decals twice is rejected.
