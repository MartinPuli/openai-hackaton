"""
Synthetic tests for the default building-detail heuristics in blocks_build
(varied estimated heights + roof caps + per-feature scene colors).

blocks_build imports bpy at module load, so the Blender-only modules are
stubbed to exercise the pure helpers in plain CI. These are the tag/provenance
driven synthetic checks the generalization contract requires before the
heuristic ships enabled by default.

Run: python3 -m pytest tests/test_building_detail.py -q
"""
import os
import sys
import types
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

# Stub Blender-only modules so the pure helpers import without a live Blender.
for _name in ("bpy", "bmesh"):
    sys.modules.setdefault(_name, mock.MagicMock())
_mathutils = types.ModuleType("mathutils")
_mathutils.Matrix = mock.MagicMock()
sys.modules.setdefault("mathutils", _mathutils)
_blender_build = types.ModuleType("blender_build")
_blender_build.add_flat_polygon = lambda *a, **k: None
_blender_build.add_prism = lambda *a, **k: None
_blender_build.dedupe_ring = lambda pts, closed=True: list(pts)
sys.modules.setdefault("blender_build", _blender_build)

import blocks_build as bb  # noqa: E402

SQUARE = [[0, 0], [40, 0], [40, 40], [0, 40]]  # 1600 m^2


def _bld(osm_id, height=9.0, source="default", footprint=None):
    return {"osm_id": osm_id, "height": height, "height_source": source,
            "footprint": footprint or SQUARE}


def test_detail_defaults_are_on():
    # The whole point of the update: these ship enabled by default.
    assert bb.DEFAULT_STYLE["height_variation"]["enabled"] is True
    assert bb.DEFAULT_STYLE["roof_detail"]["enabled"] is True
    assert bb.DEFAULT_STYLE["color_mode"] == "scene"


def test_variation_produces_a_varied_skyline():
    style = bb.load_style()
    hv = style["height_variation"]
    heights = {bb.varied_building_height(_bld(i), style, 9.0) for i in range(24)}
    assert len(heights) > 6
    for h in heights:
        assert hv["min"] <= h <= hv["max"]


def test_variation_is_deterministic():
    style = bb.load_style()
    assert bb.varied_building_height(_bld(123), style, 9.0) == \
        bb.varied_building_height(_bld(123), style, 9.0)


def test_explicit_heights_are_preserved():
    # Provenance guard: explicit OSM height/levels are never reshaped.
    style = bb.load_style()
    assert bb.varied_building_height(_bld(1, 30.0, "explicit"), style, 30.0) == 30.0
    assert bb.varied_building_height(_bld(2, 24.0, "levels"), style, 24.0) == 24.0


def test_disabled_flag_is_a_no_op():
    style = bb.load_style()
    style["height_variation"]["enabled"] = False
    assert bb.varied_building_height(_bld(9), style, 9.0) == 9.0


def test_larger_footprints_bias_taller():
    style = bb.load_style()
    small = bb.varied_building_height(
        _bld(7, footprint=[[0, 0], [6, 0], [6, 6], [0, 6]]), style, 9.0)
    big = bb.varied_building_height(
        _bld(7, footprint=[[0, 0], [80, 0], [80, 80], [0, 80]]), style, 9.0)
    assert big >= small


def test_stadium_interior_default_on():
    assert bb.DEFAULT_STYLE["stadium_interior"]["enabled"] is True


def test_is_stadium_is_tag_driven_not_name_driven():
    # Reusable domain semantics: driven by the OSM type/kind tag, never a name.
    assert bb.is_stadium({"type": "stadium"})
    assert bb.is_stadium({"type": "grandstand"})
    assert bb.is_stadium({"kind": "arena"})
    assert not bb.is_stadium({"type": "yes"})
    assert not bb.is_stadium({"type": "apartments", "name": "La Bombonera Cafe"})


def test_scale_ring_moves_points_toward_centroid():
    ring = [[0, 0], [10, 0], [10, 10], [0, 10]]  # centroid (5, 5)
    half = bb._scale_ring(ring, 5.0, 5.0, 0.5)
    assert half[0] == (2.5, 2.5) and half[2] == (7.5, 7.5)


def test_roof_shapes_default_on():
    assert bb.DEFAULT_STYLE["roof_shapes"]["enabled"] is True


def test_pitched_roof_shape_set_is_tag_driven():
    assert {"gabled", "hipped", "pyramidal", "skillion"} <= bb.PITCHED_ROOF_SHAPES
    assert "flat" not in bb.PITCHED_ROOF_SHAPES
    assert "" not in bb.PITCHED_ROOF_SHAPES


def test_principal_axis_of_long_rectangle_points_along_length():
    ring = [[0, 0], [100, 0], [100, 10], [0, 10]]  # long along X
    axis_x, axis_y = bb._principal_axis(ring, 50.0, 5.0)
    assert abs(abs(axis_x) - 1.0) < 0.05 and abs(axis_y) < 0.05


def test_terrain_default_on():
    assert bb.DEFAULT_STYLE["terrain"]["enabled"] is True


def test_terrain_sampler_bilinear_and_clamped():
    terrain = {"nx": 2, "ny": 2, "extent": 100.0, "zmin": 0.0,
               "z": [[0.0, 10.0], [20.0, 30.0]]}
    sample = bb.make_terrain_sampler(terrain)
    assert abs(sample(-100, -100) - 0.0) < 1e-6   # corner grid[0][0]
    assert abs(sample(100, 100) - 30.0) < 1e-6    # corner grid[1][1]
    assert abs(sample(0, 0) - 15.0) < 1e-6        # centre = mean of corners
    assert abs(sample(9999, 9999) - 30.0) < 1e-6  # clamps to nearest edge


def test_terrain_sampler_subtracts_zmin():
    terrain = {"nx": 2, "ny": 2, "extent": 100.0, "zmin": 100.0,
               "z": [[100.0, 100.0], [100.0, 140.0]]}
    sample = bb.make_terrain_sampler(terrain)
    assert abs(sample(-100, -100) - 0.0) < 1e-6   # base rides at 0
    assert abs(sample(100, 100) - 40.0) < 1e-6


def test_image_colors_default_on():
    assert bb.DEFAULT_STYLE["image_colors"]["enabled"] is True


def test_linear_to_srgb_endpoints_and_midtone():
    assert abs(bb._linear_to_srgb(0.0)) < 1e-6
    assert abs(bb._linear_to_srgb(1.0) - 1.0) < 1e-6
    assert bb._linear_to_srgb(0.5) > 0.5            # sRGB brightens midtones


def test_srgb_to_linear_endpoints_and_midtone():
    assert abs(bb._srgb_to_linear(0.0)) < 1e-6
    assert abs(bb._srgb_to_linear(1.0) - 1.0) < 1e-6
    assert bb._srgb_to_linear(0.5) < 0.5


def test_meters_to_pixel_center_east_north():
    px, py = bb._meters_to_pixel(0, 0, 640, 1.0)
    assert px == 320 and py == 320                  # scene origin -> centre
    px_e, py_e = bb._meters_to_pixel(10, 0, 640, 1.0)
    assert px_e == 330 and py_e == 320              # +x east -> right
    px_n, py_n = bb._meters_to_pixel(0, 10, 640, 1.0)
    assert px_n == 320 and py_n == 310              # +y north -> up (smaller py)


def test_facade_default_on():
    assert bb.DEFAULT_STYLE["facade"]["enabled"] is True


def test_facade_name_buckets_similar_colors_and_splits_distinct():
    assert bb._facade_name([0.70, 0.68, 0.60]) == bb._facade_name([0.72, 0.69, 0.61])
    assert bb._facade_name([0.70, 0.68, 0.60]) != bb._facade_name([0.20, 0.30, 0.50])


def test_facade_parameters_use_levels_before_defaults():
    style = bb.load_style()
    spec = bb.facade_parameters(
        {"type": "apartments", "levels": 5, "building_material": "brick"},
        style, height=16.0, base=0.0)
    assert abs(spec["floor_height"] - 3.2) < 1e-6
    assert spec["profile"] == "residential"
    assert spec["wall_roughness"] > 0.8


def test_facade_profiles_are_semantic_not_identity_driven():
    style = bb.load_style()
    office = bb.facade_parameters({"type": "office", "name": "Any Name"}, style)
    unrelated_office = bb.facade_parameters(
        {"type": "office", "name": "Completely Different"}, style)
    warehouse = bb.facade_parameters({"type": "warehouse", "name": "Any Name"}, style)
    assert office == unrelated_office
    assert office["profile"] == "commercial"
    assert warehouse["profile"] == "industrial"
    assert office["bay"] != warehouse["bay"]


def test_aerial_color_defaults_to_roof_not_facade():
    assert bb.DEFAULT_STYLE["image_colors"]["target"] == "roof"


def test_construction_detail_default_on_and_bounded():
    cfg = bb.DEFAULT_STYLE["construction_detail"]
    assert cfg["enabled"] is True
    assert cfg["geometry_radius"] > bb.DEFAULT_STYLE["facade"]["geometry_radius"]
    assert 0 < cfg["max_floor_bands"] <= 16


def test_construction_detail_uses_grounded_anchor_not_identity():
    style = bb.load_style()
    facade = bb.facade_parameters({"type": "apartments"}, style,
                                  height=18.0, base=0.0)
    first = bb.construction_detail_spec(
        {"type": "apartments", "detail_anchor": True, "name": "Alpha"},
        style, facade, 0.0, 18.0)
    same = bb.construction_detail_spec(
        {"type": "apartments", "detail_anchor": True, "name": "Beta"},
        style, facade, 0.0, 18.0)
    suspended = bb.construction_detail_spec(
        {"type": "apartments", "detail_anchor": True, "min_height": 6.0},
        style, facade, 6.0, 18.0)
    assert first == same
    assert first["entrance"] and first["floor_bands"]
    assert not suspended["entrance"] and not suspended["plinth"]


def test_roof_height_prefers_explicit_osm_values():
    style = bb.load_style()
    assert bb.roof_height_for({"roof_height": 3.7}, style, 0.0, 20.0) == 3.7
    assert bb.roof_height_for({"roof_levels": 2}, style, 0.0, 20.0) == 6.0
    assert bb.roof_height_for({}, style, 0.0, 20.0) <= style["roof_shapes"]["max_height"]


def test_roof_orientation_across_rotates_the_general_axis():
    ring = [[0, 0], [100, 0], [100, 10], [0, 10]]
    along = bb._roof_axis(ring, 50.0, 5.0, "along", None, "gabled")
    across = bb._roof_axis(ring, 50.0, 5.0, "across", None, "gabled")
    dot = along[0] * across[0] + along[1] * across[1]
    assert abs(dot) < 1e-6


def test_sky_and_roof_props_defaults():
    # Physical sky (Eevee-compatible) and rooftop clutter ship on by default.
    assert bb.DEFAULT_STYLE["sky_model"] == "hosek_wilkie"
    assert bb.DEFAULT_STYLE["roof_props"]["enabled"] is True
