"""Build an editable block model from the normalized ``scene.json`` contract.

The builder only understands reusable geometry and categories: buildings,
roads, areas, and point/line/surface features. Run-specific identities,
verified measurements, and visual choices belong in style.json.

Usage:
  blender -b -P scripts/blocks_build.py -- <scene.json> <outdir> <slug> \
    [--style <style.json>] [--render] [--samples 96]
"""

import colorsys
import json
import math
import os
import sys
import zlib

import bmesh
import bpy
from mathutils import Matrix


_HERE = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else None
if _HERE and _HERE not in sys.path:
    sys.path.append(_HERE)
from blender_build import add_flat_polygon, add_prism, dedupe_ring  # noqa: E402


DEFAULT_STYLE = {
    # Use each feature's own OSM/estimated color by default so buildings read
    # as a real, varied neighborhood; the saturation/value caps keep the
    # per-building palette from turning garish.
    "color_mode": "scene",
    "color_boost": 1.0,
    "building_max_sat": 0.72,
    "building_max_val": 0.93,
    "editable_radius": 200.0,
    "merge_distant_buildings": True,
    "focus": {"point": None, "match": None, "span": None},
    "feature_overrides": {},
    # Default building detail (general, provenance/geometry-driven, and per-run
    # reversible with enabled:false). height_variation only reshapes
    # estimated/default heights — never explicit OSM height/levels —
    # deterministically per osm_id and footprint area, so the skyline reads as
    # a varied neighborhood instead of a field of identical default boxes.
    "height_variation": {"enabled": True, "factor_min": 0.8, "factor_max": 1.8,
                         "min": 4.0, "max": 42.0, "area_full": 1600.0},
    # roof_detail adds a thin, darker roof/parapet band to individual buildings.
    "roof_detail": {"enabled": True, "band": 1.1, "darken": 0.7},
    # roof_shapes builds real pitched roofs from OSM roof:shape (gabled, hipped,
    # pyramidal, skillion, ...). Tag-driven; buildings without the tag keep the
    # flat roof_detail cap. max_height caps how tall the roof rises.
    "roof_shapes": {"enabled": True, "max_height": 5.0},
    # terrain drapes the scene over a real SRTM/DEM grid when scene.json carries
    # a "terrain" block (from place_to_3d --terrain): the ground is displaced and
    # buildings/roads/areas are lifted to the sampled elevation. No-op on flat or
    # terrain-less scenes, so runs without a terrain block stay reproducible.
    "terrain": {"enabled": True},
    # image_colors samples roof/area color from an authorized aerial reference.
    # Aerial evidence never overwrites facade color. The image stays external:
    # only a sourced attribute is derived, never pasted as geometry or texture.
    "image_colors": {"enabled": True, "patch": 3, "boost": 1.0,
                     "target": "roof"},
    # facade adds a face-aligned shader grid and bounded near-field window-panel
    # geometry. Merged/distant buildings remain cheap; parameters come from
    # levels, height, semantic type/use, and material rather than identity.
    "facade": {"enabled": True, "floor_height": 3.2, "bay": 3.6,
               "window_color": [0.05, 0.08, 0.13],
               "window_width_ratio": 0.66, "window_height_ratio": 0.62,
               "geometry_enabled": True, "geometry_radius": 95.0,
               "geometry_depth": 0.08, "max_windows_per_building": 240},
    # construction_detail adds readable near-field architectural hierarchy:
    # plinth, floor strings, cornice/parapet and one entry on the selected
    # grounded volume. It is a bounded inferred LOD grammar. Explicit OSM
    # building:part/min_height/roof tags always control the actual massing.
    "construction_detail": {
        "enabled": True, "geometry_radius": 125.0,
        "base_height": 0.42, "base_outset": 0.10,
        "cornice_height": 0.24, "cornice_outset": 0.14,
        "floor_bands": True, "floor_band_height": 0.08,
        "floor_band_outset": 0.07, "max_floor_bands": 12,
        "entrances": True, "entrance_width": 1.55,
        "entrance_height": 2.45, "entrance_depth": 0.12,
    },
    # stadium_interior. Tag-driven (building=stadium/grandstand): replace the
    # hollow flat-topped box with an outer wall plus raked seating tiers that
    # descend to the pitch. Geometry-only reusable semantics; seat_color and
    # wall_color are optional per-run overrides.
    "stadium_interior": {"enabled": True, "tiers": 7, "inner_scale": 0.32,
                         "pitch_height": 2.0, "seat_color": None, "wall_color": None},
    "samples": 96,
    "resolution": [1600, 1000],
    "camera": {"azimuth_deg": 225.0, "elev_deg": 33.0, "margin": 1.85,
               "aerial_height_factor": 2.2,
               "holdout_azimuth_offset_deg": 110.0,
               "holdout_elev_deg": 28.0},
    "sun": {"energy": 3.0, "angle_deg": 14.0, "rot_deg": [52, 6, 35]},
    "sky": [0.60, 0.68, 0.78, 1.3],
    # sky_model renders a physical sky gradient (Eevee-compatible Hosek/Wilkie,
    # falling back to Preetham) aligned to the sun, instead of a flat background
    # colour. Set to "flat" to fall back to the solid `sky` colour above.
    "sky_model": "hosek_wilkie",
    "sky_turbidity": 2.6,
    "sky_ground_albedo": 0.3,
    "sky_strength": 1.0,
    # roof_props scatters small deterministic rooftop clutter (chimneys, vents,
    # AC/stairwell boxes) on individual buildings for silhouette detail.
    "roof_props": {"enabled": True, "density": 1.0},
    "palette": {
        "GROUND": [0.79, 0.78, 0.73],
        "ASPHALT": [0.44, 0.44, 0.47],
        "FOOTWAY": [0.60, 0.59, 0.56],
        "GREEN": [0.30, 0.49, 0.25],
        "WATER": [0.23, 0.48, 0.62],
        "AREA": [0.62, 0.61, 0.57],
        "FEATURE": [0.72, 0.71, 0.67],
        "BLD_A": [0.70, 0.66, 0.60],
        "BLD_B": [0.62, 0.64, 0.68],
        "BLD_C": [0.74, 0.70, 0.64],
        "BLD_D": [0.58, 0.60, 0.63],
        "ROOF": [0.34, 0.30, 0.28],
    },
}


def _deep_update(base, override):
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
    return base


def load_style(style_path=None):
    style = json.loads(json.dumps(DEFAULT_STYLE))
    if style_path and os.path.exists(style_path):
        with open(style_path, encoding="utf-8") as stream:
            _deep_update(style, json.load(stream))
    return style


def feature_matches(feature, selector):
    selector = str(selector or "").strip().lower()
    if not selector:
        return False
    exact = (feature.get("osm_id"), feature.get("id"))
    if any(selector == str(value).lower() for value in exact if value not in (None, "")):
        return True
    searchable = (feature.get("name"), feature.get("type"), feature.get("kind"),
                  feature.get("family"))
    return any(selector in str(value).lower() for value in searchable
               if value not in (None, ""))


def feature_override(feature, style):
    merged = {}
    for selector, values in style.get("feature_overrides", {}).items():
        if feature_matches(feature, selector):
            _deep_update(merged, values)
    return merged


def safe_name(value):
    text = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(value))
    return text.strip("_")[:80] or "anonymous"


def poly_area(points):
    if len(points) < 3:
        return 0.0
    return abs(sum(
        points[index][0] * points[(index + 1) % len(points)][1]
        - points[(index + 1) % len(points)][0] * points[index][1]
        for index in range(len(points))
    )) * 0.5


def centroid(points):
    if not points:
        return 0.0, 0.0
    return (sum(point[0] for point in points) / len(points),
            sum(point[1] for point in points) / len(points))


def feature_points(feature):
    if feature.get("point"):
        return [feature["point"]]
    for key in ("footprint", "polygon", "path"):
        if feature.get(key):
            return feature[key]
    return []


def _srgb_to_linear(value):
    """Convert a display-sRGB channel to Blender scene-linear space."""
    value = max(0.0, min(1.0, float(value)))
    if value <= 0.04045:
        return value / 12.92
    return ((value + 0.055) / 1.055) ** 2.4


def make_material(name, rgb, roughness=0.85, metallic=0.0):
    material = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    material.use_nodes = True
    bsdf = material.node_tree.nodes.get("Principled BSDF")
    if bsdf is None:
        bsdf = material.node_tree.nodes.new("ShaderNodeBsdfPrincipled")
        output = (material.node_tree.nodes.get("Material Output")
                  or material.node_tree.nodes.new("ShaderNodeOutputMaterial"))
        material.node_tree.links.new(bsdf.outputs[0], output.inputs[0])
    linear = tuple(_srgb_to_linear(channel) for channel in rgb[:3])
    bsdf.inputs["Base Color"].default_value = (*linear, 1.0)
    bsdf.inputs["Roughness"].default_value = roughness
    bsdf.inputs["Metallic"].default_value = metallic
    if "Specular IOR Level" in bsdf.inputs:
        bsdf.inputs["Specular IOR Level"].default_value = 0.05
    material.diffuse_color = (*linear, 1.0)
    return material


def material_for_color(rgb, style):
    rgb = [float(channel) for channel in rgb[:3]]
    boost = float(style.get("color_boost", 1.0))
    if boost != 1.0:
        grey = sum(rgb) / 3.0
        rgb = [min(1.0, max(0.0, grey + (channel - grey) * boost)) for channel in rgb]
    hue, sat, val = colorsys.rgb_to_hsv(*rgb)
    if style.get("building_max_sat") is not None:
        sat = min(sat, float(style["building_max_sat"]))
    if style.get("building_max_val") is not None:
        val = min(val, float(style["building_max_val"]))
    rgb = colorsys.hsv_to_rgb(hue, sat, val)
    quantized = [min(20, max(0, int(round(channel * 20)))) for channel in rgb]
    name = "BLK_C_%02d%02d%02d" % tuple(quantized)
    return make_material(name, [channel / 20.0 for channel in quantized])


def build_materials(style):
    return {key: make_material("BLK_" + key, rgb)
            for key, rgb in style["palette"].items()}


def new_collection(name, parent=None):
    collection = bpy.data.collections.get(name)
    if collection is None:
        collection = bpy.data.collections.new(name)
        (parent or bpy.context.scene.collection).children.link(collection)
    return collection


def safe_clear():
    """Clear the scene without resetting preferences or disconnecting add-ons."""
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for collection in list(bpy.data.collections):
        bpy.data.collections.remove(collection)
    for datablocks in (bpy.data.meshes, bpy.data.curves, bpy.data.lights,
                       bpy.data.cameras, bpy.data.materials, bpy.data.images):
        for datablock in list(datablocks):
            if datablock.users == 0:
                datablocks.remove(datablock)


def add_box(bm, cx, cy, cz, sx, sy, sz, rotation=0.0):
    matrix = (Matrix.Translation((cx, cy, cz))
              @ Matrix.Rotation(rotation, 4, "Z")
              @ Matrix.Diagonal((sx, sy, sz, 1.0)))
    bmesh.ops.create_cube(bm, size=1.0, matrix=matrix)


def add_prism_checked(bm, footprint, base, top):
    points = dedupe_ring(footprint, closed=True)
    if len(points) < 3 or top <= base:
        return False
    signed = sum(
        points[index][0] * points[(index + 1) % len(points)][1]
        - points[(index + 1) % len(points)][0] * points[index][1]
        for index in range(len(points))
    )
    if signed < 0:
        points.reverse()
    add_prism(bm, points, base, top)
    return True


def add_path(bm, path, width, z, height=0.04):
    made = False
    for start, end in zip(path, path[1:]):
        dx, dy = end[0] - start[0], end[1] - start[1]
        length = math.hypot(dx, dy)
        if length < 0.2:
            continue
        add_box(bm, (start[0] + end[0]) / 2, (start[1] + end[1]) / 2,
                z + height / 2, length + width * 0.25, width, height,
                math.atan2(dy, dx))
        made = True
    return made


def finalize(name, bm, material, collection):
    if not bm.faces:
        bm.free()
        return None
    mesh = bpy.data.meshes.new(name)
    bm.to_mesh(mesh)
    bm.free()
    obj = bpy.data.objects.new(name, mesh)
    obj.data.materials.append(material)
    collection.objects.link(obj)
    return obj


def choose_focus(scene, style):
    focus = style.get("focus") or {}
    if focus.get("point") and len(focus["point"]) >= 2:
        point = focus["point"]
        center = float(point[0]), float(point[1])
        source = "style.point"
    else:
        match = focus.get("match")
        candidates = (list(scene.get("special_features", []))
                      + list(scene.get("buildings", []))
                      + list(scene.get("areas", [])))
        selected = next((item for item in candidates if feature_matches(item, match)), None)
        center = centroid(feature_points(selected)) if selected else (0.0, 0.0)
        source = "style.match" if selected else "scene_origin"
    default_span = max(80.0, min(float(scene.get("radius", 350.0)) * 0.7, 320.0))
    return center, float(focus.get("span") or default_span), source


def _road_material(road, materials):
    vehicle_types = {"motorway", "trunk", "primary", "secondary", "tertiary",
                     "residential", "living_street", "unclassified", "service"}
    return materials["ASPHALT"] if road.get("type") in vehicle_types else materials["FOOTWAY"]


def _area_material(area, materials, style):
    if style.get("color_mode") == "scene" and area.get("color"):
        return material_for_color(area["color"], style)
    area_type = str(area.get("type", "")).lower()
    if any(token in area_type for token in ("water", "river", "basin")):
        return materials["WATER"]
    if any(token in area_type for token in ("green", "grass", "park", "garden", "pitch")):
        return materials["GREEN"]
    return materials["AREA"]


def _building_material(building, materials, style):
    if style.get("color_mode") == "scene" and building.get("color"):
        return material_for_color(building["color"], style)
    keys = ("BLD_A", "BLD_B", "BLD_C", "BLD_D")
    seed = zlib.crc32(str(building.get("osm_id", building.get("id", 0))).encode())
    return materials[keys[seed % len(keys)]]


def varied_building_height(building, style, fallback):
    """Reshape only estimated/default heights into a varied but deterministic
    value, driven by osm_id and footprint area. Explicit OSM height/levels are
    left untouched so provenance is preserved. Opt-in via style.height_variation.
    """
    config = style.get("height_variation") or {}
    if not config.get("enabled"):
        return fallback
    source = str(building.get("height_source", "")).lower()
    if source not in ("", "default", "estimate", "estimated"):
        return fallback
    seed = zlib.crc32(("h:" + str(building.get("osm_id", building.get("id", 0)))).encode())
    jitter = (seed % 10007) / 10007.0
    area = poly_area(building.get("footprint", []))
    area_factor = max(0.0, min(1.0, area / float(config.get("area_full", 1600.0))))
    low = float(config.get("factor_min", 0.75))
    high = float(config.get("factor_max", 1.6))
    factor = low + (high - low) * (0.6 * jitter + 0.4 * area_factor)
    varied = float(fallback) * factor
    return max(float(config.get("min", 4.0)), min(float(config.get("max", 60.0)), varied))


def roof_material_for(building, override, style, materials):
    """Return explicit/aerial roof colour before deriving it from the wall."""
    darken = float((style.get("roof_detail") or {}).get("darken", 0.7))
    rgb = override.get("roof_color") or building.get("roof_color")
    if rgb:
        return material_for_color(rgb, style)
    rgb = override.get("color")
    if rgb is None and style.get("color_mode") == "scene":
        rgb = building.get("color")
    if rgb:
        return material_for_color([max(0.0, channel * darken) for channel in rgb[:3]], style)
    return materials.get("ROOF") or materials.get("AREA")


def surface_parameters(material_name):
    """Return conservative PBR parameters from a reusable material class."""
    token = str(material_name or "").strip().lower().replace(" ", "_")
    if "glass" in token:
        return {"roughness": 0.24, "metallic": 0.0}
    if any(value in token for value in ("metal", "steel", "copper", "zinc")):
        return {"roughness": 0.38, "metallic": 0.55}
    if any(value in token for value in ("brick", "stone", "sandstone")):
        return {"roughness": 0.88, "metallic": 0.0}
    if any(value in token for value in ("concrete", "cement", "plaster", "render")):
        return {"roughness": 0.82, "metallic": 0.0}
    if "wood" in token:
        return {"roughness": 0.72, "metallic": 0.0}
    return {"roughness": 0.80, "metallic": 0.0}


def facade_parameters(building, style, height=None, base=0.0):
    """Derive a compact facade grammar from tags and measured dimensions.

    The grammar is intentionally identity-free: it uses levels, building/use
    classes, material, and height.  All missing-data choices remain inferred.
    """
    cfg = style.get("facade") or {}
    floor_h = max(2.2, float(cfg.get("floor_height", 3.2)))
    levels = building.get("levels")
    if levels not in (None, 0) and height is not None:
        observed = (float(height) - float(base)) / max(1, int(levels))
        if 2.2 <= observed <= 5.5:
            floor_h = observed

    tokens = " ".join(str(building.get(key, "")) for key in
                      ("type", "building_use", "building_material")).lower()
    bay = max(2.0, float(cfg.get("bay", 3.6)))
    width_ratio = float(cfg.get("window_width_ratio", 0.66))
    height_ratio = float(cfg.get("window_height_ratio", 0.62))
    profile = "mixed"
    if any(token in tokens for token in ("industrial", "warehouse", "hangar", "factory")):
        profile, bay, width_ratio, height_ratio = "industrial", 6.0, 0.62, 0.46
    elif any(token in tokens for token in ("office", "commercial", "retail", "hotel", "glass")):
        profile, bay, width_ratio, height_ratio = "commercial", 3.3, 0.78, 0.72
    elif any(token in tokens for token in ("house", "residential", "apartments", "detached")):
        profile, bay, width_ratio, height_ratio = "residential", 3.2, 0.62, 0.58
    elif any(token in tokens for token in ("church", "cathedral", "civic", "public", "school")):
        profile, bay, width_ratio, height_ratio = "civic", 4.4, 0.52, 0.66

    surface = surface_parameters(building.get("building_material"))
    return {
        "profile": profile,
        "floor_height": round(floor_h, 3),
        "bay": round(max(2.0, bay), 3),
        "window_width_ratio": round(max(0.25, min(0.88, width_ratio)), 3),
        "window_height_ratio": round(max(0.25, min(0.88, height_ratio)), 3),
        "wall_roughness": surface["roughness"],
        "wall_metallic": surface["metallic"],
        "window_roughness": 0.20,
    }


def _facade_name(rgb, spec=None):
    quant = tuple(min(9, max(0, int(round(channel * 9)))) for channel in rgb[:3])
    spec = spec or {}
    signature = (int(round(float(spec.get("floor_height", 3.2)) * 10)),
                 int(round(float(spec.get("bay", 3.6)) * 10)),
                 int(round(float(spec.get("window_width_ratio", 0.66)) * 10)),
                 int(round(float(spec.get("window_height_ratio", 0.62)) * 10)))
    return "FACADE_%d%d%d_%02d_%02d_%d%d" % (quant + signature)


def make_facade_material(wall_rgb, style, spec=None):
    """Procedural window/floor facade (shader only, no extra geometry). Windows
    align to each face tangent and are masked off horizontal roof faces."""
    cfg = style.get("facade") or {}
    spec = dict(spec or {})
    name = _facade_name(wall_rgb, spec)
    existing = bpy.data.materials.get(name)
    if existing is not None:
        return existing
    window = list(cfg.get("window_color", [0.05, 0.08, 0.13]))[:3]
    wall = list(wall_rgb[:3])
    wall_linear = [_srgb_to_linear(value) for value in wall]
    window_linear = [_srgb_to_linear(value) for value in window]
    floor_h = max(2.2, float(spec.get("floor_height", cfg.get("floor_height", 3.2))))
    bay = max(2.0, float(spec.get("bay", cfg.get("bay", 3.6))))
    width_ratio = float(spec.get("window_width_ratio", cfg.get("window_width_ratio", 0.66)))
    height_ratio = float(spec.get("window_height_ratio", cfg.get("window_height_ratio", 0.62)))
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    tree = material.node_tree
    tree.nodes.clear()
    nodes, links = tree.nodes, tree.links
    out = nodes.new("ShaderNodeOutputMaterial")
    bsdf = nodes.new("ShaderNodeBsdfPrincipled")
    links.new(bsdf.outputs[0], out.inputs["Surface"])
    geometry = nodes.new("ShaderNodeNewGeometry")
    pos_sep = nodes.new("ShaderNodeSeparateXYZ")
    normal_sep = nodes.new("ShaderNodeSeparateXYZ")
    links.new(geometry.outputs["Position"], pos_sep.inputs[0])
    links.new(geometry.outputs["Normal"], normal_sep.inputs[0])

    z_axis = nodes.new("ShaderNodeCombineXYZ")
    z_axis.inputs["Z"].default_value = 1.0
    tangent = nodes.new("ShaderNodeVectorMath")
    tangent.operation = "CROSS_PRODUCT"
    links.new(z_axis.outputs[0], tangent.inputs[0])
    links.new(geometry.outputs["Normal"], tangent.inputs[1])
    horizontal = nodes.new("ShaderNodeVectorMath")
    horizontal.operation = "DOT_PRODUCT"
    links.new(geometry.outputs["Position"], horizontal.inputs[0])
    links.new(tangent.outputs["Vector"], horizontal.inputs[1])

    def math(op, in0=None, in1=None, a=None, b=None):
        node = nodes.new("ShaderNodeMath")
        node.operation = op
        if a is not None:
            node.inputs[0].default_value = a
        if b is not None:
            node.inputs[1].default_value = b
        if in0 is not None:
            links.new(in0, node.inputs[0])
        if in1 is not None:
            links.new(in1, node.inputs[1])
        return node.outputs[0]

    zf = math("FRACT", in0=math("DIVIDE", in0=pos_sep.outputs["Z"], b=floor_h))
    hf = math("FRACT", in0=math("DIVIDE", in0=horizontal.outputs["Value"], b=bay))
    z_margin = max(0.04, (1.0 - height_ratio) * 0.5)
    h_margin = max(0.04, (1.0 - width_ratio) * 0.5)
    zmask = math("MULTIPLY", in0=math("GREATER_THAN", in0=zf, b=z_margin),
                 in1=math("LESS_THAN", in0=zf, b=1.0 - z_margin))
    hmask = math("MULTIPLY", in0=math("GREATER_THAN", in0=hf, b=h_margin),
                 in1=math("LESS_THAN", in0=hf, b=1.0 - h_margin))
    wall_mask = math("LESS_THAN", in0=math("ABSOLUTE", in0=normal_sep.outputs["Z"]),
                     b=0.55)
    mask = math("MULTIPLY", in0=math("MULTIPLY", in0=zmask, in1=hmask),
                in1=wall_mask)

    mix = nodes.new("ShaderNodeMixRGB")
    mix.blend_type = "MIX"
    links.new(mask, mix.inputs[0])
    mix.inputs[1].default_value = (*wall_linear, 1.0)
    mix.inputs[2].default_value = (*window_linear, 1.0)
    links.new(mix.outputs["Color"], bsdf.inputs["Base Color"])

    rough = math("SUBTRACT", a=float(spec.get("wall_roughness", 0.80)),
                 in1=math("MULTIPLY", in0=mask,
                          b=max(0.0, float(spec.get("wall_roughness", 0.80))
                                - float(spec.get("window_roughness", 0.20)))))
    links.new(rough, bsdf.inputs["Roughness"])
    bsdf.inputs["Metallic"].default_value = float(spec.get("wall_metallic", 0.0))
    return material


def add_facade_panels(footprint, base, top, identity, style, spec, collection):
    """Add bounded near-field window panels from a reusable facade grammar.

    Panels are inferred LOD geometry, never claimed as surveyed openings.  The
    count is capped and evenly sampled across all faces to avoid a detail
    explosion or a first-edge bias.
    """
    cfg = style.get("facade") or {}
    if not cfg.get("geometry_enabled", True) or top - base < 2.4:
        return 0
    ring = dedupe_ring(footprint, closed=True)
    if len(ring) < 3:
        return 0
    floor_h = max(2.2, float(spec.get("floor_height", 3.2)))
    bay = max(2.0, float(spec.get("bay", 3.6)))
    floors = max(1, int((top - base) / floor_h))
    width_ratio = float(spec.get("window_width_ratio", 0.66))
    height_ratio = float(spec.get("window_height_ratio", 0.62))
    depth = max(0.02, float(cfg.get("geometry_depth", 0.08)))
    candidates = []
    signed = sum(
        ring[index][0] * ring[(index + 1) % len(ring)][1]
        - ring[(index + 1) % len(ring)][0] * ring[index][1]
        for index in range(len(ring))
    )
    orientation = 1.0 if signed >= 0.0 else -1.0
    for start, end in zip(ring, ring[1:] + ring[:1]):
        dx, dy = end[0] - start[0], end[1] - start[1]
        length = math.hypot(dx, dy)
        if length < 1.5:
            continue
        bays = max(1, int(round(length / bay)))
        slot = length / bays
        window_w = min(slot * width_ratio, max(0.6, bay * 0.88))
        nx, ny = orientation * dy / length, orientation * -dx / length
        angle = math.atan2(dy, dx)
        for floor in range(floors):
            cz = base + (floor + 0.5) * ((top - base) / floors)
            window_h = min((top - base) / floors * height_ratio,
                           max(0.8, floor_h * 0.82))
            for bay_index in range(bays):
                position = (bay_index + 0.5) / bays
                candidates.append((
                    start[0] + dx * position + nx * (depth * 0.6),
                    start[1] + dy * position + ny * (depth * 0.6),
                    cz, window_w, window_h, angle,
                ))
    if not candidates:
        return 0
    limit = max(1, int(cfg.get("max_windows_per_building", 240)))
    stride = max(1, int(math.ceil(len(candidates) / limit)))
    selected = candidates[::stride][:limit]
    bm = bmesh.new()
    for cx, cy, cz, width, height, angle in selected:
        add_box(bm, cx, cy, cz, width, depth, height, angle)
    window_rgb = list(cfg.get("window_color", [0.05, 0.08, 0.13]))[:3]
    material = make_material("BLK_FACADE_GLASS", window_rgb,
                             roughness=float(spec.get("window_roughness", 0.20)))
    obj = finalize("FacadeWindows_" + safe_name(identity), bm, material, collection)
    if obj is None:
        return 0
    obj["detail_kind"] = "procedural_facade_windows"
    obj["provenance"] = "inferred"
    return len(selected)


def construction_detail_spec(building, style, facade_spec, base, top):
    """Choose bounded construction layers from tags and facade metrics.

    The result never changes the source massing. It only controls inferred
    near-field readability and is therefore safe to disable per run.
    """
    cfg = style.get("construction_detail") or {}
    height = max(0.0, float(top) - float(base))
    part_kind = str(building.get("building_part") or "").lower()
    enabled = bool(cfg.get("enabled", True)) and height >= 3.0 and part_kind != "roof"
    floor_h = max(2.2, float(facade_spec.get("floor_height", 3.2)))
    floors = max(1, int(round(height / floor_h)))
    profile = str(facade_spec.get("profile", "mixed"))
    max_bands = max(0, int(cfg.get("max_floor_bands", 12)))
    possible_bands = max(0, floors - 1)
    band_count = min(possible_bands, max_bands)
    grounded = float(building.get("min_height", base)) <= 0.15
    anchor = bool(building.get("detail_anchor", not building.get("is_building_part")))
    return {
        "enabled": enabled,
        "plinth": enabled and grounded,
        "cornice": enabled,
        "floor_bands": (enabled and bool(cfg.get("floor_bands", True))
                        and profile not in ("industrial",) and band_count > 0),
        "floor_band_count": band_count,
        "floors": floors,
        "entrance": (enabled and grounded and anchor
                     and bool(cfg.get("entrances", True))),
    }


def _outset_ring(points, meters):
    ring = dedupe_ring(points, closed=True)
    if len(ring) < 3 or meters <= 0.0:
        return ring
    cx, cy = centroid(ring)
    radius = sum(math.hypot(p[0] - cx, p[1] - cy) for p in ring) / len(ring)
    return _scale_ring(ring, cx, cy, 1.0 + float(meters) / max(1.0, radius))


def _longest_edge(points):
    ring = dedupe_ring(points, closed=True)
    if len(ring) < 3:
        return None
    return max(zip(ring, ring[1:] + ring[:1]),
               key=lambda pair: math.hypot(pair[1][0] - pair[0][0],
                                           pair[1][1] - pair[0][1]))


def add_construction_details(footprint, base, top, identity, building, style,
                             facade_spec, wall_rgb, collection):
    """Add a plinth, floor strings, cornice and a single inferred entrance.

    All bands for a building share one mesh and are capped by LOD settings.
    Values are labeled as inferred so OSM parts and measured dimensions remain
    distinguishable from presentation geometry.
    """
    cfg = style.get("construction_detail") or {}
    spec = construction_detail_spec(building, style, facade_spec, base, top)
    if not spec["enabled"]:
        return {"objects": 0, "bands": 0, "entrances": 0}
    detail_rgb = [max(0.0, min(1.0, float(value) * 0.90))
                  for value in list(wall_rgb[:3])]
    detail_material = material_for_color(detail_rgb, style)
    ornaments = bmesh.new()
    band_count = 0

    if spec["plinth"]:
        plinth_h = min(float(cfg.get("base_height", 0.42)), (top - base) * 0.16)
        ring = _outset_ring(footprint, float(cfg.get("base_outset", 0.10)))
        if add_prism_checked(ornaments, ring, base, base + plinth_h):
            band_count += 1

    if spec["floor_bands"]:
        wanted = spec["floor_band_count"]
        possible = max(1, spec["floors"] - 1)
        selected = sorted(set(
            max(1, min(possible, int(round((index + 1) * possible / (wanted + 1)))))
            for index in range(wanted)
        ))
        band_h = max(0.03, float(cfg.get("floor_band_height", 0.08)))
        ring = _outset_ring(footprint, float(cfg.get("floor_band_outset", 0.07)))
        floor_step = (top - base) / spec["floors"]
        for floor in selected:
            z = base + floor * floor_step
            if z + band_h < top - 0.15 and add_prism_checked(
                    ornaments, ring, z, z + band_h):
                band_count += 1

    if spec["cornice"]:
        cornice_h = min(max(0.08, float(cfg.get("cornice_height", 0.24))),
                        (top - base) * 0.08)
        ring = _outset_ring(footprint, float(cfg.get("cornice_outset", 0.14)))
        if add_prism_checked(ornaments, ring, top - cornice_h, top):
            band_count += 1

    objects = 0
    obj = finalize("Detail_" + safe_name(identity), ornaments,
                   detail_material, collection)
    if obj is not None:
        obj["detail_kind"] = "procedural_construction_bands"
        obj["provenance"] = "inferred"
        objects += 1

    entrances = 0
    edge = _longest_edge(footprint) if spec["entrance"] else None
    if edge:
        start, end = edge
        dx, dy = end[0] - start[0], end[1] - start[1]
        length = math.hypot(dx, dy)
        if length >= 1.2:
            signed = sum(
                footprint[index][0] * footprint[(index + 1) % len(footprint)][1]
                - footprint[(index + 1) % len(footprint)][0] * footprint[index][1]
                for index in range(len(footprint))
            )
            orientation = 1.0 if signed >= 0.0 else -1.0
            nx, ny = orientation * dy / length, orientation * -dx / length
            width = min(length * 0.38, max(1.0, float(cfg.get("entrance_width", 1.55))))
            height = min(top - base - 0.2, max(1.9, float(cfg.get("entrance_height", 2.45))))
            depth = max(0.04, float(cfg.get("entrance_depth", 0.12)))
            entry = bmesh.new()
            add_box(entry, (start[0] + end[0]) * 0.5 + nx * depth * 0.65,
                    (start[1] + end[1]) * 0.5 + ny * depth * 0.65,
                    base + height * 0.5, width, depth, height,
                    math.atan2(dy, dx))
            entry_material = make_material("BLK_ENTRY", [0.12, 0.10, 0.08],
                                           roughness=0.45)
            entry_obj = finalize("Entry_" + safe_name(identity), entry,
                                 entry_material, collection)
            if entry_obj is not None:
                entry_obj["detail_kind"] = "procedural_entrance"
                entry_obj["provenance"] = "inferred"
                objects += 1
                entrances = 1
    return {"objects": objects, "bands": band_count, "entrances": entrances}


def _scale_ring(points, cx, cy, scale):
    return [(cx + (point[0] - cx) * scale, cy + (point[1] - cy) * scale)
            for point in points]


def is_stadium(building):
    """Tag-driven detection of stadium/grandstand footprints (reusable domain
    semantics, never a specific venue name)."""
    token = " ".join(str(building.get(key, "")) for key in ("type", "kind")).lower()
    return "stadium" in token or "grandstand" in token or "arena" in token


def add_stadium_bowl(footprint, base, height, style, materials, collection,
                     seat_material, wall_material):
    """Replace a stadium's hollow flat-topped extrusion with a real interior:
    an outer perimeter wall plus concentric seating tiers that rake down toward
    the pitch. Geometry-only and reusable for any stadium-tagged footprint.
    """
    cfg = style.get("stadium_interior") or {}
    ring = dedupe_ring(footprint, closed=True)
    if len(ring) < 3:
        return False
    tiers = max(2, int(cfg.get("tiers", 7)))
    inner_scale = float(cfg.get("inner_scale", 0.32))
    pitch_gap = float(cfg.get("pitch_height", 2.0))
    cx, cy = centroid(ring)

    wall = bmesh.new()
    if add_prism_checked(wall, footprint, base, height):
        finalize("Stadium_Wall", wall, wall_material, collection)
    else:
        wall.free()

    span = max(0.5, height - base - pitch_gap)
    seating = bmesh.new()
    count = len(ring)
    for tier in range(tiers):
        scale_outer = 1.0 - (1.0 - inner_scale) * (tier / tiers)
        scale_inner = 1.0 - (1.0 - inner_scale) * ((tier + 1) / tiers)
        z_outer = base + pitch_gap + span * (1.0 - tier / tiers)
        z_inner = base + pitch_gap + span * (1.0 - (tier + 1) / tiers)
        outer = _scale_ring(ring, cx, cy, scale_outer)
        inner = _scale_ring(ring, cx, cy, scale_inner)
        v_outer = [seating.verts.new((p[0], p[1], z_outer)) for p in outer]
        v_tread = [seating.verts.new((p[0], p[1], z_outer)) for p in inner]
        v_riser = [seating.verts.new((p[0], p[1], z_inner)) for p in inner]
        for i in range(count):
            j = (i + 1) % count
            try:  # horizontal seating tread
                seating.faces.new((v_outer[i], v_outer[j], v_tread[j], v_tread[i]))
            except ValueError:
                pass
            try:  # vertical riser down to the next tier
                seating.faces.new((v_tread[i], v_tread[j], v_riser[j], v_riser[i]))
            except ValueError:
                pass
    if not seating.faces:
        seating.free()
        return True
    seating.normal_update()
    mesh = bpy.data.meshes.new("Stadium_Seating")
    seating.to_mesh(mesh)
    seating.free()
    obj = bpy.data.objects.new("Stadium_Seating", mesh)
    obj.data.materials.append(seat_material)
    collection.objects.link(obj)
    return True


# OSM roof:shape values built as real pitched roofs (rest fall back to flat).
# Apex-type shapes converge to a single top point; ridge-type shapes build a
# ridge along the footprint's principal axis (an oriented-bounding-box proxy,
# the standard approach in blender-osm / osm2world for non-rectangular plans).
APEX_ROOF_SHAPES = {"pyramidal", "dome", "onion", "conical", "cone", "round"}
RIDGE_ROOF_SHAPES = {"gabled", "hipped", "half-hipped", "half_hipped",
                     "gambrel", "mansard", "saltbox"}
SLOPE_ROOF_SHAPES = {"skillion", "mono_pitched", "mono-pitched", "lean_to", "shed"}
PITCHED_ROOF_SHAPES = APEX_ROOF_SHAPES | RIDGE_ROOF_SHAPES | SLOPE_ROOF_SHAPES


def _principal_axis(points, cx, cy):
    """Dominant 2D direction of a footprint via the 2x2 covariance eigenvector
    (a lightweight oriented-bounding-box proxy for the roof ridge)."""
    sxx = sum((p[0] - cx) ** 2 for p in points)
    syy = sum((p[1] - cy) ** 2 for p in points)
    sxy = sum((p[0] - cx) * (p[1] - cy) for p in points)
    trace = sxx + syy
    disc = max(0.0, (trace * 0.5) ** 2 - (sxx * syy - sxy * sxy))
    lam = trace * 0.5 + math.sqrt(disc)
    if abs(sxy) > 1e-9:
        vx, vy = lam - syy, sxy
    else:
        vx, vy = (1.0, 0.0) if sxx >= syy else (0.0, 1.0)
    norm = math.hypot(vx, vy) or 1.0
    return vx / norm, vy / norm


def _roof_axis(points, cx, cy, orientation=None, direction=None, shape=None):
    """Resolve a roof axis from OSM orientation/direction before fallback PCA."""
    axis_x, axis_y = _principal_axis(points, cx, cy)
    orientation = str(orientation or "").strip().lower()
    if orientation == "across":
        axis_x, axis_y = -axis_y, axis_x
    if direction is not None and shape in SLOPE_ROOF_SHAPES:
        # OSM bearings are clockwise from north; local +Y is north.
        angle = math.radians(float(direction))
        axis_x, axis_y = math.sin(angle), math.cos(angle)
    return axis_x, axis_y


def roof_height_for(building, style, base, height):
    """Use explicit OSM roof height/levels before a bounded inferred fallback."""
    available = max(0.2, float(height) - float(base))
    explicit = building.get("roof_height")
    if explicit is not None and float(explicit) > 0.05:
        return min(float(explicit), available * 0.85)
    roof_levels = building.get("roof_levels")
    if roof_levels not in (None, 0):
        return min(float(roof_levels) * 3.0, available * 0.85)
    cfg = style.get("roof_shapes") or {}
    return max(1.0, min(float(cfg.get("max_height", 5.0)), 0.28 * available))


def add_pitched_roof(bm, footprint, wall_top, roof_height, shape,
                     orientation=None, direction=None):
    """Build a real roof of the given OSM shape on top of the walls. Robust for
    any footprint; faces that would be degenerate are skipped."""
    ring = dedupe_ring(footprint, closed=True)
    count = len(ring)
    if count < 3 or roof_height <= 0.05:
        return False
    cx, cy = centroid(ring)
    base_z = wall_top
    apex_z = wall_top + roof_height
    base = [bm.verts.new((p[0], p[1], base_z)) for p in ring]

    if shape in APEX_ROOF_SHAPES:
        apex = bm.verts.new((cx, cy, apex_z))
        for i in range(count):
            try:
                bm.faces.new((base[i], base[(i + 1) % count], apex))
            except ValueError:
                pass
        return True

    axis_x, axis_y = _roof_axis(ring, cx, cy, orientation, direction, shape)
    proj = [(p[0] - cx) * axis_x + (p[1] - cy) * axis_y for p in ring]
    lo, hi = min(proj), max(proj)
    mid = (lo + hi) * 0.5

    if shape in SLOPE_ROOF_SHAPES:
        span = (hi - lo) or 1.0
        top = [bm.verts.new((ring[i][0], ring[i][1],
                             base_z + roof_height * ((proj[i] - lo) / span)))
               for i in range(count)]
        for i in range(count):
            j = (i + 1) % count
            try:
                bm.faces.new((base[i], base[j], top[j], top[i]))
            except ValueError:
                pass
        try:
            bm.faces.new(list(reversed(top)))
        except ValueError:
            pass
        return True

    # ridge family (gabled/hipped/...): ridge segment along the principal axis
    inset = 0.15 * (hi - lo)
    r0 = bm.verts.new((cx + axis_x * (lo + inset), cy + axis_y * (lo + inset), apex_z))
    r1 = bm.verts.new((cx + axis_x * (hi - inset), cy + axis_y * (hi - inset), apex_z))
    for i in range(count):
        j = (i + 1) % count
        ridge_i = r0 if proj[i] < mid else r1
        ridge_j = r0 if proj[j] < mid else r1
        try:
            if ridge_i is ridge_j:
                bm.faces.new((base[i], base[j], ridge_i))
            else:
                bm.faces.new((base[i], base[j], ridge_j, ridge_i))
        except ValueError:
            pass
    return True


def make_terrain_sampler(terrain):
    """Return sample(x, y) -> elevation (meters, base at 0) via bilinear
    interpolation over the SRTM/DEM grid. Coordinates are in the scene's local
    meter frame; out-of-range points clamp to the nearest edge."""
    grid = terrain["z"]
    ny = len(grid)
    nx = len(grid[0]) if ny else 0
    extent = float(terrain.get("extent", 1.0)) or 1.0
    zmin = float(terrain.get("zmin", 0.0))
    if nx < 2 or ny < 2:
        return lambda x, y: 0.0

    def sample(x, y):
        fx = min(max((x + extent) / (2.0 * extent) * (nx - 1), 0.0), nx - 1)
        fy = min(max((y + extent) / (2.0 * extent) * (ny - 1), 0.0), ny - 1)
        ix, iy = int(fx), int(fy)
        ix2, iy2 = min(ix + 1, nx - 1), min(iy + 1, ny - 1)
        tx, ty = fx - ix, fy - iy
        top = grid[iy][ix] * (1 - tx) + grid[iy][ix2] * tx
        bottom = grid[iy2][ix] * (1 - tx) + grid[iy2][ix2] * tx
        return (top * (1 - ty) + bottom * ty) - zmin

    return sample


def build_terrain_ground(terrain, material, collection, pad=1.12):
    """Build a displaced ground mesh from the SRTM/DEM grid instead of a flat
    plane. Returns True on success."""
    grid = terrain["z"]
    ny = len(grid)
    nx = len(grid[0]) if ny else 0
    if nx < 2 or ny < 2:
        return False
    extent = float(terrain.get("extent", 1.0))
    zmin = float(terrain.get("zmin", 0.0))
    bm = bmesh.new()
    verts = [[None] * nx for _ in range(ny)]
    for j in range(ny):
        for i in range(nx):
            x = (-extent + 2.0 * extent * i / (nx - 1)) * pad
            y = (-extent + 2.0 * extent * j / (ny - 1)) * pad
            verts[j][i] = bm.verts.new((x, y, grid[j][i] - zmin - 0.05))
    for j in range(ny - 1):
        for i in range(nx - 1):
            try:
                bm.faces.new((verts[j][i], verts[j][i + 1],
                              verts[j + 1][i + 1], verts[j + 1][i]))
            except ValueError:
                pass
    bm.normal_update()
    mesh = bpy.data.meshes.new("BLK_Terrain")
    bm.to_mesh(mesh)
    bm.free()
    obj = bpy.data.objects.new("BLK_Terrain", mesh)
    obj.data.materials.append(material)
    collection.objects.link(obj)
    return True


def _linear_to_srgb(value):
    """Convert a scene-linear channel (Blender image pixel) to display sRGB."""
    value = max(0.0, min(1.0, float(value)))
    if value <= 0.0031308:
        return 12.92 * value
    return 1.055 * (value ** (1.0 / 2.4)) - 0.055


def _meters_to_pixel(x, y, size, meters_per_px):
    """Local meters (origin at scene centre, +y north) -> pixel (from left,
    from top) of a north-up square aerial centred on the scene."""
    half = size * 0.5
    return half + x / meters_per_px, half - y / meters_per_px


def make_image_color_sampler(scene, style, base_dir=None):
    """Return sample(x, y) -> sRGB [r,g,b] read from the authorized aerial
    reference, or None when there is no usable reference. The image is only
    read for colour; it is never added to the scene as geometry or texture."""
    ref = scene.get("satellite_reference")
    cfg = style.get("image_colors") or {}
    if not cfg.get("enabled", True) or not isinstance(ref, dict):
        return None
    raw_path = ref.get("path")
    meters_per_px = ref.get("meters_per_px")
    if not raw_path or not meters_per_px:
        return None
    # scene.json may store a relative reference path; resolve against the run dir.
    candidates = [raw_path]
    if base_dir:
        candidates.append(os.path.join(base_dir, os.path.basename(raw_path)))
        if ref.get("filename"):
            candidates.append(os.path.join(base_dir, ref["filename"]))
    path = next((candidate for candidate in candidates if os.path.exists(candidate)), None)
    if not path:
        return None
    try:
        import numpy as np
        image = bpy.data.images.load(path, check_existing=True)
        width, height = image.size
        if width < 2 or height < 2:
            return None
        pixels = np.array(image.pixels[:], dtype="float32").reshape(height, width, 4)
    except Exception:
        return None
    declared = float(ref.get("size", width)) or width
    mpp = float(meters_per_px) * (declared / width)  # meters per loaded pixel
    patch = max(0, int(cfg.get("patch", 2)))
    boost = float(cfg.get("boost", 1.0))

    def sample(x, y):
        px, py = _meters_to_pixel(x, y, width, mpp)
        col = int(round(px))
        row = height - 1 - int(round(py))  # Blender pixels are bottom-up
        if col < 0 or col >= width or row < 0 or row >= height:
            return None
        c0, c1 = max(0, col - patch), min(width, col + patch + 1)
        r0, r1 = max(0, row - patch), min(height, row + patch + 1)
        block = pixels[r0:r1, c0:c1, :3].reshape(-1, 3)
        if block.size == 0:
            return None
        mean = block.mean(axis=0)
        rgb = [_linear_to_srgb(channel) for channel in mean]
        if boost != 1.0:
            grey = sum(rgb) / 3.0
            rgb = [min(1.0, max(0.0, grey + (c - grey) * boost)) for c in rgb]
        return rgb

    return sample


def setup_sky_world(world, style, sun=None):
    """Set the world to a physical sky gradient aligned to the sun. Uses
    Hosek/Wilkie (Eevee-compatible), falling back to Preetham, then to the flat
    `sky` colour. `sky_model == 'flat'` forces the solid colour."""
    world.use_nodes = True
    tree = world.node_tree
    background = tree.nodes.get("Background") or tree.nodes.new("ShaderNodeBackground")
    output = tree.nodes.get("World Output") or tree.nodes.new("ShaderNodeOutputWorld")
    try:
        tree.links.new(background.outputs[0], output.inputs[0])
    except Exception:
        pass
    for node in list(tree.nodes):
        if node.type == "TEX_SKY":
            tree.nodes.remove(node)
    flat = style.get("sky", [0.60, 0.68, 0.78, 1.3])
    if str(style.get("sky_model", "hosek_wilkie")).lower() in ("flat", "none", "solid"):
        background.inputs[0].default_value = (*flat[:3], 1.0)
        background.inputs[1].default_value = float(flat[3])
        return "flat"
    sky = tree.nodes.new("ShaderNodeTexSky")
    applied = "flat"
    for sky_type in ("HOSEK_WILKIE", "PREETHAM"):
        try:
            sky.sky_type = sky_type
            applied = sky_type.lower()
            break
        except Exception:
            continue
    if applied == "flat":
        tree.nodes.remove(sky)
        background.inputs[0].default_value = (*flat[:3], 1.0)
        background.inputs[1].default_value = float(flat[3])
        return "flat"
    if sun is not None:
        from mathutils import Vector
        light_dir = sun.rotation_euler.to_quaternion() @ Vector((0.0, 0.0, -1.0))
        try:
            sky.sun_direction = (-light_dir).normalized()
        except Exception:
            pass
    for attr, value in (("turbidity", float(style.get("sky_turbidity", 2.6))),
                        ("ground_albedo", float(style.get("sky_ground_albedo", 0.3)))):
        try:
            setattr(sky, attr, value)
        except Exception:
            pass
    try:
        tree.links.new(sky.outputs[0], background.inputs[0])
    except Exception:
        pass
    background.inputs[1].default_value = float(style.get("sky_strength", 1.0))
    return applied


def add_roof_props(style, collection=None):
    """Scatter small deterministic rooftop clutter (chimneys, vents, AC boxes)
    on individual buildings so roofs are not bare. Geometry-only, per-building
    deterministic; returns the number of buildings that received props."""
    cfg = style.get("roof_props") or {}
    if not cfg.get("enabled", True):
        return 0
    from mathutils import Vector
    density = float(cfg.get("density", 1.0))
    prop_mat = make_material("BLK_RoofProp", [0.40, 0.40, 0.42], roughness=0.85)
    if collection is None:
        collection = bpy.data.collections.get("BLK_09_ROOFPROPS")
        if collection is None:
            collection = bpy.data.collections.new("BLK_09_ROOFPROPS")
            bpy.context.scene.collection.children.link(collection)
    made = 0
    for obj in [o for o in bpy.data.objects if o.type == "MESH" and o.name.startswith("Bld_")]:
        corners = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
        xs = [v.x for v in corners]
        ys = [v.y for v in corners]
        minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
        top = max(v.z for v in corners)
        width, depth = maxx - minx, maxy - miny
        area = width * depth
        if top < 5.0 or area < 60.0:
            continue
        seed = zlib.crc32(obj.name.encode())
        count = max(1, min(5, int(area / 380.0 * density)))
        bm = bmesh.new()
        for k in range(count):
            r1 = ((seed >> (k * 6)) % 1000) / 1000.0
            r2 = ((seed >> (k * 6 + 3)) % 1000) / 1000.0
            r3 = ((seed >> (k * 6 + 9)) % 100) / 100.0
            px = minx + width * (0.2 + 0.6 * r1)
            py = miny + depth * (0.2 + 0.6 * r2)
            prop_h = 1.2 + 2.4 * r3
            prop_s = 0.8 + 1.6 * r3
            add_box(bm, px, py, top + prop_h / 2.0, prop_s, prop_s, prop_h)
        if not bm.faces:
            bm.free()
            continue
        mesh = bpy.data.meshes.new("RoofProps_" + obj.name)
        bm.to_mesh(mesh)
        bm.free()
        prop_obj = bpy.data.objects.new("RoofProps_" + obj.name, mesh)
        prop_obj.data.materials.append(prop_mat)
        collection.objects.link(prop_obj)
        made += 1
    return made


def build_from_scene(scene_path, outdir, slug, style_path=None,
                     do_render=False, samples=None, clear=True):
    with open(scene_path, encoding="utf-8") as stream:
        source = json.load(stream)
    style = load_style(style_path)
    if samples is not None:
        style["samples"] = int(samples)
    os.makedirs(outdir, exist_ok=True)
    if clear:
        safe_clear()

    materials = build_materials(style)
    root = new_collection("BLOCKS_" + safe_name(slug).upper())
    terrain_collection = new_collection("BLK_00_TERRAIN", root)
    road_collection = new_collection("BLK_01_ROADS", root)
    area_collection = new_collection("BLK_02_AREAS", root)
    building_collection = new_collection("BLK_03_BUILDINGS", root)
    feature_collection = new_collection("BLK_04_FEATURES", root)
    rig_collection = new_collection("BLK_05_CAMERA_LIGHT", root)

    focus_center, focus_span, focus_source = choose_focus(source, style)
    report = {
        "slug": slug,
        "focus": {"center": [round(value, 2) for value in focus_center],
                  "span": round(focus_span, 2), "source": focus_source},
        "roads": 0,
        "areas": 0,
        "buildings_individual": 0,
        "buildings_merged": 0,
        "features": [],
        "construction_detail": {
            "building_parts": sum(1 for item in source.get("buildings", [])
                                  if item.get("is_building_part")),
            "detail_objects": 0,
            "bands": 0,
            "entrances": 0,
        },
        "building_appearance": {
            "color_sources": {},
            "roof_color_sources": {},
            "facade_shader_buildings": 0,
            "facade_geometry_buildings": 0,
            "facade_window_panels": 0,
            "_facade_confidence_total": 0.0,
            "_roof_confidence_total": 0.0,
        },
    }

    terrain_data = source.get("terrain")
    terrain_on = bool(terrain_data) and bool((style.get("terrain") or {}).get("enabled", True))
    ground_at = make_terrain_sampler(terrain_data) if terrain_on else (lambda x, y: 0.0)
    if terrain_on and build_terrain_ground(terrain_data, materials["GROUND"], terrain_collection):
        report["terrain"] = {"source": terrain_data.get("source"),
                             "zmin": terrain_data.get("zmin"),
                             "zmax": terrain_data.get("zmax")}
    else:
        terrain_on = False
        ground_at = lambda x, y: 0.0
        bpy.ops.mesh.primitive_plane_add(size=max(4.0, 4.0 * float(source.get("radius", 350.0))),
                                         location=(0.0, 0.0, -0.05))
        ground = bpy.context.active_object
        ground.name = "BLK_Ground"
        for collection in list(ground.users_collection):
            collection.objects.unlink(ground)
        terrain_collection.objects.link(ground)
        ground.data.materials.append(materials["GROUND"])

    image_target = str((style.get("image_colors") or {}).get("target", "roof")).lower()
    if image_target != "roof":
        raise ValueError("aerial image colors may target roofs only; use verified street-level facade evidence via feature_overrides")
    image_sampler = make_image_color_sampler(source, style, outdir)
    report["image_colored"] = 0

    road_meshes = {}
    for index, road in enumerate(source.get("roads", [])):
        path = road.get("path", [])
        if len(path) < 2:
            continue
        material = _road_material(road, materials)
        bm = road_meshes.setdefault(material.name, (material, bmesh.new()))[1]
        width = max(0.5, float(road.get("width", 3.0)))
        z = float(road.get("z", 0.0)) + 0.02 + (index % 200) * 0.0001
        if terrain_on:
            z += ground_at(*centroid(path))
        if add_path(bm, path, width, z):
            report["roads"] += 1
    for name, (material, bm) in road_meshes.items():
        finalize("BLK_Roads_" + safe_name(name), bm, material, road_collection)

    area_meshes = {}
    for index, area in enumerate(source.get("areas", [])):
        polygon = area.get("polygon", [])
        if len(polygon) < 3:
            continue
        if image_sampler is not None:
            sampled = image_sampler(*centroid(polygon))
            if sampled is not None:
                area["color"] = sampled
                report["image_colored"] += 1
        material = _area_material(area, materials, style)
        bm = area_meshes.setdefault(material.name, (material, bmesh.new()))[1]
        z = float(area.get("z", 0.0)) + 0.03 + (index % 200) * 0.0001
        if terrain_on:
            z += ground_at(*centroid(polygon))
        add_flat_polygon(bm, polygon, z)
        report["areas"] += 1
    for name, (material, bm) in area_meshes.items():
        finalize("BLK_Areas_" + safe_name(name), bm, material, area_collection)

    merged = {}
    editable_radius = float(style.get("editable_radius", 200.0))
    merge_distant = bool(style.get("merge_distant_buildings", True))
    appearance = report["building_appearance"]
    for building in source.get("buildings", []):
        footprint = building.get("footprint", [])
        if len(footprint) < 3:
            continue
        if image_sampler is not None:
            sampled = image_sampler(*centroid(footprint))
            if sampled is not None and not str(building.get("roof_color_source", "")).startswith("osm:"):
                # A north-up aerial observes the roof, not the facade.  Keeping
                # the channels separate prevents a roof pixel from repainting
                # every wall of the building.
                building["roof_color"] = sampled
                building["roof_color_source"] = "imagery_aerial"
                building["roof_color_confidence"] = 0.65
                report["image_colored"] += 1
        color_source = str(building.get("color_source") or "legacy_prior")
        roof_source = str(building.get("roof_color_source") or "derived_from_facade")
        appearance["color_sources"][color_source] = appearance["color_sources"].get(color_source, 0) + 1
        appearance["roof_color_sources"][roof_source] = appearance["roof_color_sources"].get(roof_source, 0) + 1
        facade_confidence = float(building.get("color_confidence", 0.25))
        roof_confidence = float(building.get(
            "roof_color_confidence", facade_confidence * 0.60))
        appearance["_facade_confidence_total"] += facade_confidence
        appearance["_roof_confidence_total"] += roof_confidence
        override = feature_override(building, style)
        base = float(override.get("min_height", building.get("min_height", 0.0)))
        raw_height = float(override.get("height", building.get("height", 9.0)))
        if not override.get("height"):
            raw_height = varied_building_height(building, style, raw_height)
        height = max(base + 0.2, raw_height)
        stadium_cfg = style.get("stadium_interior") or {}
        if stadium_cfg.get("enabled", True) and is_stadium(building):
            seat_rgb = stadium_cfg.get("seat_color") or override.get("color")
            seat_material = (material_for_color(seat_rgb, style) if seat_rgb
                             else materials.get("FEATURE") or materials["AREA"])
            wall_rgb = stadium_cfg.get("wall_color") or override.get("color")
            wall_material = (material_for_color(wall_rgb, style) if wall_rgb
                             else materials.get("FEATURE") or materials["AREA"])
            if add_stadium_bowl(footprint, base, height, style, materials,
                                building_collection, seat_material, wall_material):
                report["buildings_individual"] += 1
                continue
        material = (material_for_color(override["color"], style)
                    if override.get("color") else _building_material(building, materials, style))
        center = centroid(footprint)
        if terrain_on:
            lift = ground_at(center[0], center[1])
            base += lift
            height += lift
        distance = math.hypot(center[0] - focus_center[0], center[1] - focus_center[1])
        individual = (not merge_distant or distance <= editable_radius
                      or building.get("name") or override.get("individual"))
        if individual:
            facade_spec = facade_parameters(building, style, height=height, base=base)
            facade_shader = False
            wall_rgb = (override.get("color")
                        or (building.get("color") if style.get("color_mode") == "scene" else None)
                        or style["palette"]["BLD_A"])
            if (style.get("facade") or {}).get("enabled", True):
                material = make_facade_material(wall_rgb, style, facade_spec)
                facade_shader = True
                appearance["facade_shader_buildings"] += 1
            identity = building.get("name") or building.get("osm_id") or report["buildings_individual"]
            shape = str(building.get("roof_shape") or "").strip().lower().replace(" ", "_")
            rs_cfg = style.get("roof_shapes") or {}
            if rs_cfg.get("enabled", True) and shape in PITCHED_ROOF_SHAPES and (height - base) > 3.0:
                roof_h = roof_height_for(building, style, base, height)
                wall_top = max(base + 0.2, height - roof_h)
                bm = bmesh.new()
                if add_prism_checked(bm, footprint, base, wall_top):
                    finalize("Bld_" + safe_name(identity), bm, material, building_collection)
                    report["buildings_individual"] += 1
                    roof = bmesh.new()
                    if add_pitched_roof(
                            roof, footprint, wall_top, height - wall_top, shape,
                            building.get("roof_orientation"),
                            building.get("roof_direction")):
                        finalize("Roof_" + safe_name(identity), roof,
                                 roof_material_for(building, override, style, materials),
                                 building_collection)
                    else:
                        roof.free()
                    geometry_radius = float((style.get("facade") or {}).get(
                        "geometry_radius", 95.0))
                    if facade_shader and distance <= geometry_radius:
                        panels = add_facade_panels(
                            footprint, base, wall_top, identity, style,
                            facade_spec, building_collection)
                        if panels:
                            appearance["facade_geometry_buildings"] += 1
                            appearance["facade_window_panels"] += panels
                    construction_radius = float((style.get("construction_detail") or {}).get(
                        "geometry_radius", 125.0))
                    if distance <= construction_radius:
                        detail = add_construction_details(
                            footprint, base, wall_top, identity, building, style,
                            facade_spec, wall_rgb, building_collection)
                        for key, source_key in (("detail_objects", "objects"),
                                                ("bands", "bands"),
                                                ("entrances", "entrances")):
                            report["construction_detail"][key] += detail[source_key]
                    continue
                bm.free()
            roof_cfg = style.get("roof_detail") or {}
            band = float(roof_cfg.get("band", 1.1)) if roof_cfg.get("enabled") else 0.0
            body_top = height - band if band > 0.0 and (height - base) > band + 1.0 else height
            bm = bmesh.new()
            if add_prism_checked(bm, footprint, base, body_top):
                identity = building.get("name") or building.get("osm_id") or report["buildings_individual"]
                finalize("Bld_" + safe_name(identity), bm, material, building_collection)
                report["buildings_individual"] += 1
                if body_top < height:
                    cap = bmesh.new()
                    if add_prism_checked(cap, footprint, body_top, height):
                        finalize("Roof_" + safe_name(identity),
                                 cap, roof_material_for(building, override, style, materials),
                                 building_collection)
                    else:
                        cap.free()
                geometry_radius = float((style.get("facade") or {}).get(
                    "geometry_radius", 95.0))
                if facade_shader and distance <= geometry_radius:
                    panels = add_facade_panels(
                        footprint, base, body_top, identity, style,
                        facade_spec, building_collection)
                    if panels:
                        appearance["facade_geometry_buildings"] += 1
                        appearance["facade_window_panels"] += panels
                construction_radius = float((style.get("construction_detail") or {}).get(
                    "geometry_radius", 125.0))
                if distance <= construction_radius:
                    detail = add_construction_details(
                        footprint, base, body_top, identity, building, style,
                        facade_spec, wall_rgb, building_collection)
                    for key, source_key in (("detail_objects", "objects"),
                                            ("bands", "bands"),
                                            ("entrances", "entrances")):
                        report["construction_detail"][key] += detail[source_key]
            else:
                bm.free()
        else:
            bm = merged.setdefault(material.name, (material, bmesh.new()))[1]
            if add_prism_checked(bm, footprint, base, height):
                report["buildings_merged"] += 1
    for name, (material, bm) in merged.items():
        finalize("BLK_Buildings_" + safe_name(name), bm, material, building_collection)

    appearance_count = sum(appearance["color_sources"].values())
    appearance["facade_color_confidence_mean"] = round(
        appearance.pop("_facade_confidence_total") / max(1, appearance_count), 3)
    appearance["roof_color_confidence_mean"] = round(
        appearance.pop("_roof_confidence_total") / max(1, appearance_count), 3)

    for index, feature in enumerate(source.get("special_features", [])):
        override = feature_override(feature, style)
        identity = feature.get("name") or feature.get("osm_id") or feature.get("id") or index
        material = (material_for_color(override["color"], style)
                    if override.get("color") else materials["FEATURE"])
        geometry = feature.get("geometry")
        bm = bmesh.new()
        made = False
        if geometry == "surface" and feature.get("polygon"):
            add_flat_polygon(bm, feature["polygon"], float(override.get("z", 0.06)))
            made = True
        elif geometry == "line" and feature.get("path"):
            width = max(0.5, float(override.get("width", feature.get("width", 3.0))))
            made = add_path(bm, feature["path"], width, float(override.get("z", 0.06)))
        elif feature.get("point"):
            point = feature["point"]
            width = max(0.5, float(override.get("width", feature.get("width", 3.0))))
            height = max(0.2, float(override.get("height", feature.get("height", 10.0))))
            add_box(bm, float(point[0]), float(point[1]), height / 2, width, width, height)
            made = True
        if not made:
            bm.free()
            continue
        obj = finalize("Feature_" + safe_name(identity), bm, material, feature_collection)
        if obj:
            report["features"].append({
                "id": str(feature.get("id", feature.get("osm_id", identity))),
                "name": obj.name,
                "kind": feature.get("kind"),
                "height": float(override.get("height", feature.get("height", 0.0))),
            })

    camera_cfg = style["camera"]
    azimuth = math.radians(float(camera_cfg["azimuth_deg"]))
    elevation = math.radians(float(camera_cfg["elev_deg"]))
    distance = focus_span * float(camera_cfg["margin"])
    oblique_data = bpy.data.cameras.new("BLK_CamOblique")
    oblique_data.lens = 42
    oblique = bpy.data.objects.new("BLK_CamOblique", oblique_data)
    oblique.location = (
        focus_center[0] + distance * math.cos(azimuth) * math.cos(elevation),
        focus_center[1] + distance * math.sin(azimuth) * math.cos(elevation),
        distance * math.sin(elevation),
    )
    rig_collection.objects.link(oblique)

    holdout_azimuth = azimuth + math.radians(float(
        camera_cfg.get("holdout_azimuth_offset_deg", 110.0)))
    holdout_elevation = math.radians(float(camera_cfg.get("holdout_elev_deg", 28.0)))
    holdout_distance = distance * 1.05
    holdout_data = bpy.data.cameras.new("BLK_CamHoldout")
    holdout_data.lens = 45
    holdout = bpy.data.objects.new("BLK_CamHoldout", holdout_data)
    holdout.location = (
        focus_center[0] + holdout_distance * math.cos(holdout_azimuth) * math.cos(holdout_elevation),
        focus_center[1] + holdout_distance * math.sin(holdout_azimuth) * math.cos(holdout_elevation),
        holdout_distance * math.sin(holdout_elevation),
    )
    rig_collection.objects.link(holdout)

    aerial_data = bpy.data.cameras.new("BLK_CamAerial")
    aerial_data.lens = 50
    aerial = bpy.data.objects.new("BLK_CamAerial", aerial_data)
    aerial.location = (focus_center[0], focus_center[1] - focus_span * 0.12,
                       focus_span * float(camera_cfg["aerial_height_factor"]))
    rig_collection.objects.link(aerial)

    target = bpy.data.objects.new("BLK_Target", None)
    target.location = (focus_center[0], focus_center[1], min(12.0, focus_span * 0.06))
    rig_collection.objects.link(target)
    for camera in (oblique, aerial, holdout):
        constraint = camera.constraints.new("TRACK_TO")
        constraint.target = target
        constraint.track_axis = "TRACK_NEGATIVE_Z"
        constraint.up_axis = "UP_Y"

    sun_cfg = style["sun"]
    sun_data = bpy.data.lights.new("BLK_Sun", "SUN")
    sun_data.energy = float(sun_cfg["energy"])
    sun_data.angle = math.radians(float(sun_cfg["angle_deg"]))
    sun = bpy.data.objects.new("BLK_Sun", sun_data)
    sun.rotation_euler = tuple(math.radians(value) for value in sun_cfg["rot_deg"])
    rig_collection.objects.link(sun)

    world = bpy.context.scene.world or bpy.data.worlds.new("World")
    bpy.context.scene.world = world
    setup_sky_world(world, style, sun)
    add_roof_props(style)

    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.samples = int(style["samples"])
    scene.cycles.use_denoising = True
    scene.render.resolution_x, scene.render.resolution_y = map(int, style["resolution"])
    scene.render.resolution_percentage = 100
    scene.camera = oblique
    try:
        scene.view_settings.view_transform = "AgX"
    except Exception:
        pass
    for look in ("Medium High Contrast", "AgX - Medium High Contrast", "None"):
        try:
            scene.view_settings.look = look
            break
        except Exception:
            continue

    report["objects"] = len(bpy.data.objects)
    report_path = os.path.join(outdir, "build_report.json")
    with open(report_path, "w", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, ensure_ascii=False)

    blend_path = os.path.join(outdir, f"{slug}_blocks.blend")
    bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(blend_path))
    if do_render:
        render_views(outdir)
    print("BLOCKS BUILD OK:", json.dumps(report, ensure_ascii=False))
    return report


def render_views(outdir):
    scene = bpy.context.scene
    for camera_name, filename in (("BLK_CamOblique", "blocks_oblique.png"),
                                  ("BLK_CamAerial", "blocks_aerial.png"),
                                  ("BLK_CamHoldout", "blocks_holdout.png")):
        camera = bpy.data.objects.get(camera_name)
        if camera is None:
            continue
        scene.camera = camera
        scene.render.filepath = os.path.abspath(os.path.join(outdir, filename))
        bpy.ops.render.render(write_still=True)


def _cli():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if len(argv) < 3:
        print("usage: blender -b -P blocks_build.py -- <scene.json> <outdir> <slug> "
              "[--style f] [--render] [--samples n]")
        return
    scene_path, outdir, slug = argv[:3]
    style_path = argv[argv.index("--style") + 1] if "--style" in argv else None
    samples = int(argv[argv.index("--samples") + 1]) if "--samples" in argv else None
    build_from_scene(scene_path, outdir, slug, style_path,
                     do_render="--render" in argv, samples=samples)


if __name__ == "__main__":
    _cli()
