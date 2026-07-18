# Bringing real places into Blender — techniques

Survey of the established ways to reconstruct a real location in Blender, and
how each maps onto this skill. The reference tools are **Blosm / blender-osm**
(`vvoovv`), **BlenderGIS** (`domlysz`), and **OSM2World**. Techniques that
depend on provider imagery as *construction geometry* are intentionally kept
out — this skill builds editable OSM blocks and uses imagery only as reference
(see `SKILL.md`).

Legend: ✅ implemented (default) · 🟡 partial · 📋 recommended / roadmap.

## Geometry from OpenStreetMap
- ✅ **Buildings, roads, areas** from OSM footprints — the construction source.
- ✅ **Heights** from `height` / `building:levels` (explicit) with typed
  defaults; provenance preserved (`height_source`).
- ✅ **Simple 3D Building parts.** `building:part` volumes replace the
  backward-compatible 2D `building` outline when parts are present. Per-part
  `height`, `min_height` / `building:min_level`, material, color, and roof tags
  create real stepped massing without double walls. The largest grounded part
  becomes the single inferred entrance anchor.
- ✅ **Varied estimated heights** — default heights are reshaped
  deterministically per `osm_id` + footprint area so the skyline is not a field
  of identical boxes. Explicit heights are never touched. (`height_variation`)
- ✅ **Source-aware appearance** — facade and roof colors retain separate
  provenance. Explicit OSM values outrank material/semantic priors; sRGB inputs
  are converted to scene-linear values before Principled BSDF. Aggregate facade
  and roof confidence is written to the build report.

## Roofs
- ✅ **Roof caps** — thin darker parapet band on individual buildings.
- ✅ **Procedural roof shapes** from `roof:shape` — gabled, hipped, pyramidal,
  dome/onion (apex), skillion/mono-pitch, etc. Non-rectangular plans use the
  footprint's principal axis as an oriented-bounding-box ridge proxy, the same
  strategy Blosm/OSM2World use. (`roof_shapes`) Tiers/roofs are inferred, not
  surveyed. See OSM *Simple 3D Buildings* and *OSM-4D/Roof table*.
- ✅ **Roof dimensions and axes.** Explicit `roof:height` and `roof:levels`
  outrank the bounded fallback. `roof:orientation=along/across` controls ridge
  direction, while `roof:direction` controls skillion slope direction.

## Domain semantics
- ✅ **Stadium / grandstand interiors** — `building=stadium`/`grandstand` gets
  an outer wall plus concentric, **stepped** seating tiers (tread + riser)
  descending to the pitch instead of a hollow box. (`stadium_interior`)
- 🟡 **Airport / port / rail** — `special_features` carry runways, taxiways,
  aprons, helipads as semantic infrastructure.

## Terrain
- ✅ **DEM / SRTM elevation.** `place_to_3d --terrain` fetches a real elevation
  grid over the scene bounds from **OpenTopoData** (public SRTM 30 m, no API
  key) and stores it in `scene.json` (`terrain: {nx, ny, extent, z[][], zmin}`)
  in the same local meter frame as the geometry. The block builder then replaces
  the flat plane with a displaced ground mesh (`BLK_Terrain`) and lifts
  buildings, roads, and areas onto the bilinearly-sampled elevation. Opt-in via
  `--terrain` / `style.terrain.enabled`, so terrain-less runs stay reproducible.
  Elevation is real data (attribute NASA SRTM); feature draping uses each
  feature's centroid, so a single very steep footprint is approximated.

## Roads, vegetation, water
- 🟡 **Roads** are width-aware flat ribbons, draped onto the terrain when a DEM
  is present; Blosm additionally renders them as curves with a profile.
- 🟡 **Water / green / pitch** areas are flat polygons by material.
- 📋 **Vegetation** — `natural=tree`, `landuse=forest/grass` as scatter/instances.

## Sky & atmosphere
- ✅ **Physical sky.** The world uses a Sky Texture gradient aligned to the sun
  instead of a flat background colour (`sky_model`). The default is
  **Hosek/Wilkie** (falling back to Preetham) because **Nishita is Cycles-only**
  and the block loop renders in Eevee; `sky_model: "flat"` restores the solid
  colour. Turbidity/ground-albedo are tunable. Gives a real horizon gradient and
  atmospheric depth in every render.

  *Why this is the right choice (researched):* Hosek/Wilkie is the model designed
  for **ground-level / horizon** views — exactly this use case — whereas Preetham
  targets high-altitude/aerial and reads more pastel. Nishita is the most
  physically accurate model but is **Cycles-only** (still true in 4.2/4.3), so it
  cannot drive the Eevee block loop. An HDRI would be photoreal but needs an
  external, licence-bound `.hdr` asset, which breaks the skill's reproducible,
  key-less, procedural contract. So Hosek/Wilkie is the best *available* sky here.
  Caveat: Hosek/Wilkie and Preetham are marked upstream-legacy and may be
  replaced; if the loop ever renders in Cycles, prefer Nishita there.
- ✅ **Rooftop props.** Individual buildings receive small deterministic roof
  clutter (chimneys, vents, AC/stairwell boxes) via `roof_props`, the standard
  procedural-city trick for breaking up bare rooftops — geometry-only, seeded
  per building, and skipped on tiny/short footprints.
- 📋 **Volumetric haze / clouds** — aerial perspective for distance; deferred
  (Eevee volumetrics are costly at city scale).

## Materials
- ✅ Procedural per-feature block materials (roughness, saturation caps).
- ✅ **Facade grammar and LOD.** `building:levels`, height, type/use, and material
  drive floor spacing, bay spacing, opening ratios, and PBR response. Shader
  windows align to each face tangent and are masked off roofs. Buildings near
  the focus also receive a capped editable window-panel mesh; distant/merged
  buildings remain cheap.
- ✅ **Construction-detail LOD.** Near-field volumes receive a plinth, bounded
  floor strings, a cornice/parapet, and one entrance on the selected grounded
  volume. The grammar is tag/metric driven, capped per building, and explicitly
  reported as inferred; distant geometry keeps the low-cost shader/flat LOD.
- ✅ **Imagery-derived roof colors (needs a Google API key).** A geo-referenced
  aerial reference can populate `roof_color` when OSM lacks it. It never
  overwrites `building:colour` or a facade prior, and the image is never pasted
  into scene geometry or textures.
- ✅ **Anti-overfit views.** The builder creates aerial, oblique tuning, and a
  different-azimuth holdout camera. Checkpoint selection uses the worse of
  tuning and holdout, with an explicit generalization-gap gate.
- 📋 Tileable façade/roof textures with UV mapping (Blosm premium does this);
  optional and licence-sensitive, so kept out of the default block look.

## Reference-only (never construction geometry)
- ❌ **Google Photorealistic 3D Tiles**, satellite basemaps, Street View —
  used only as comparison references during evaluation, never pasted into the
  scene as a substitute for constructed blocks. This is a hard skill guardrail.

## Sources
- Blosm / blender-osm: <https://github.com/vvoovv/blosm> ·
  Profiled/roof docs: <https://github.com/vvoovv/blosm/wiki/Profiled-roofs>
- BlenderGIS: <https://github.com/domlysz/BlenderGIS> ·
  SRTM: <https://github.com/domlysz/BlenderGIS/wiki/SRTM>
- OSM Simple 3D Buildings: <https://wiki.openstreetmap.org/wiki/Simple_3D_Buildings>
- Blender color management: <https://docs.blender.org/manual/en/latest/render/color_management.html>
- Blender Principled BSDF: <https://docs.blender.org/manual/en/latest/render/shader_nodes/shader/principled.html>
- Inverse procedural facade layouts: <https://doi.org/10.1145/2601097.2601162>
- CIEDE2000 implementation notes: <https://doi.org/10.1002/col.20070>
- OSM-4D / Roof table: <https://wiki.openstreetmap.org/wiki/OSM-4D/Roof_table>
- blender-osm on the OSM wiki: <https://wiki.openstreetmap.org/wiki/Blender-osm>
- OpenTopoData (elevation API used by `--terrain`): <https://www.opentopodata.org/>
- NASA SRTM: <https://www.earthdata.nasa.gov/data/instruments/srtm>
- Blender Sky Texture (Hosek/Wilkie, Preetham, Nishita; Eevee support notes):
  <https://docs.blender.org/manual/en/latest/render/shader_nodes/textures/sky.html>
- Sky model comparison (Hosek/Wilkie vs Nishita, ground vs aerial use):
  <https://blenderartists.org/t/sky-texture-hosek-wilkie-vs-nishita/1492126>
