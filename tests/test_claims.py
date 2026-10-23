"""AC8: the claims input contract, its three input shapes, and the overlay layer."""

import json
from pathlib import Path

import numpy as np
import pytest
from pxr import Sdf, Usd, UsdGeom

from mcusd.blocks import hashed_color, parse_color
from mcusd.overlays.claims_input import ClaimsError, load_claims
from mcusd.select import BBox
from mcusd.usd.claims import claims_in_bbox, faction_prim_name, write_claims_layer

WORLD = "9c2e7d1a-5b3f-4c8e-a1d2-3e4f5a6b7c8d"
OTHER_WORLD = "11111111-2222-4333-8444-555555555555"
NORTH = "1b7f0000-aaaa-4bbb-8ccc-000000000001"
SOUTH = "1b7f0000-aaaa-4bbb-8ccc-000000000002"
CLAIMS = [
    {"worldId": WORLD, "x": 12, "z": -4, "factionId": NORTH},
    {"worldId": WORLD, "x": 13, "z": -4, "factionId": NORTH},
    {"worldId": WORLD, "x": -1, "z": 0, "factionId": SOUTH},
]
COLORS = {NORTH: "#3366cc", SOUTH: "#aa2211"}
NAMES = {NORTH: "Northreach", SOUTH: "Southmoor"}
EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "claims.json"


def write_json(path, data):
    path.write_text(json.dumps(data))
    return path


def native(tmp_path, *, with_factions=True, claims=CLAIMS, name="claims.json"):
    doc = {"format": "mcusd-claims", "version": 1, "claims": claims}
    if with_factions:
        doc["factions"] = [{"id": f, "name": NAMES[f], "color": COLORS[f]} for f in (NORTH, SOUTH)]
    return write_json(tmp_path / name, doc)


def api_array(tmp_path):
    return write_json(tmp_path / "api_claims.json", CLAIMS)


def mf_directory(tmp_path, *, with_factions=True):
    directory = tmp_path / "medieval_factions_data"
    directory.mkdir()
    write_json(directory / "claimed_chunks.json", {"claims": CLAIMS})
    if with_factions:
        factions = [{
            "id": f, "version": 3, "name": NAMES[f], "description": "",
            "flags": {"color": COLORS[f], "alliesCanInteractWithLand": False},
            "roles": {"defaultRoleId": "r", "roles": []},
        } for f in (NORTH, SOUTH)]
        write_json(directory / "factions.json", {"factions": factions})
    return directory


def overlay(tmp_path, source, *, binary=False, min_y=-64, max_y=319, world_uuid=None, out="out"):
    data = load_claims(source)
    stats = write_claims_layer(tmp_path / out, data, data.select_world(world_uuid),
                               min_y=min_y, max_y=max_y, binary=binary)
    return data, stats, Usd.Stage.Open(str(stats.path))


def describe(stage):
    """Everything that defines the overlay, as plain data."""
    result = {}
    for faction in stage.GetPrimAtPath("/World/Overlays/Claims").GetChildren():
        inst = UsdGeom.PointInstancer(faction.GetChild("Chunks"))
        color = UsdGeom.PrimvarsAPI(faction).GetPrimvar("displayColor").Get()[0]
        result[faction.GetName()] = {
            "name": faction.GetCustomDataByKey("mcusd:factionName"),
            "id": faction.GetCustomDataByKey("mcusd:factionId"),
            "color": tuple(round(c, 6) for c in color),
            "positions": sorted(tuple(p) for p in inst.GetPositionsAttr().Get()),
        }
    return result


def geometry(desc):
    return {k: v["positions"] for k, v in desc.items()}


def test_three_input_shapes_give_the_same_overlay(tmp_path):
    a = describe(overlay(tmp_path, native(tmp_path), out="a")[2])
    b = describe(overlay(tmp_path, mf_directory(tmp_path), out="b")[2])
    c = describe(overlay(tmp_path, api_array(tmp_path), out="c")[2])
    assert a == b  # native and MF JSON storage carry the same names and colours
    assert geometry(a) == geometry(c)  # the REST array has no faction data, same boxes
    # without faction data, the native contract matches the REST array exactly
    d = describe(overlay(tmp_path, native(tmp_path, with_factions=False, name="bare.json"), out="d")[2])
    assert d == c


def test_box_minimum_corner_and_size(tmp_path):
    _, _, stage = overlay(tmp_path, native(tmp_path), min_y=-64, max_y=319)
    faction = stage.GetPrimAtPath(f"/World/Overlays/Claims/{faction_prim_name(NORTH)}")
    inst = UsdGeom.PointInstancer(faction.GetChild("Chunks"))
    assert sorted(tuple(p) for p in inst.GetPositionsAttr().Get()) == [(192, -64, -64), (208, -64, -64)]
    (box_path,) = inst.GetPrototypesRel().GetTargets()
    points = np.array(UsdGeom.Mesh(stage.GetPrimAtPath(box_path)).GetPointsAttr().Get())
    assert points.min(axis=0).tolist() == [0, 0, 0]
    assert points.max(axis=0).tolist() == [16, 384, 16]
    opacity = UsdGeom.Mesh(stage.GetPrimAtPath(box_path)).GetDisplayOpacityPrimvar().Get()
    assert list(opacity) == pytest.approx([0.35])
    lo, hi = (np.array(v) for v in inst.GetExtentAttr().Get())
    assert lo.tolist() == [192, -64, -64] and hi.tolist() == [224, 320, -48]


def test_faction_colours_come_from_input(tmp_path):
    for source in (native(tmp_path), mf_directory(tmp_path)):
        data = load_claims(source)
        assert data.faction(NORTH).color == parse_color("#3366cc")
        assert data.faction(NORTH).color_from_input


def test_missing_factions_fall_back_to_ids_without_warning(tmp_path):
    data = load_claims(api_array(tmp_path))
    assert data.source_kind == "api-array"
    assert data.faction(NORTH).name == NORTH
    assert data.faction(NORTH).color == hashed_color(NORTH)
    assert data.warnings == []


def test_unknown_faction_id_warns_and_uses_fallback(tmp_path):
    claims = CLAIMS + [{"worldId": WORLD, "x": 0, "z": 0, "factionId": "ghost"}]
    data, stats, stage = overlay(tmp_path, native(tmp_path, claims=claims))
    assert any("'ghost'" in w for w in data.warnings)
    ghost = stage.GetPrimAtPath(f"/World/Overlays/Claims/{faction_prim_name('ghost')}")
    assert ghost.GetCustomDataByKey("mcusd:factionName") == "ghost"
    assert stats.factions == 3 and stats.claims == 4


def test_multi_world_input_requires_world_uuid(tmp_path):
    claims = CLAIMS + [{"worldId": OTHER_WORLD, "x": 5, "z": 5, "factionId": NORTH}]
    data = load_claims(native(tmp_path, claims=claims))
    with pytest.raises(ClaimsError, match="--world-uuid"):
        data.select_world(None)
    assert len(data.select_world(WORLD.upper())) == 3  # UUIDs compare canonically
    assert data.select_world(OTHER_WORLD) == [c for c in data.claims if c.world_id == OTHER_WORLD]
    with pytest.raises(ClaimsError, match="not a valid UUID"):
        data.select_world("not-a-uuid")


def test_single_world_needs_no_uuid_and_unmatched_uuid_warns(tmp_path):
    data = load_claims(native(tmp_path))
    assert len(data.select_world(None)) == 3
    assert data.select_world(OTHER_WORLD) == []
    assert any("no claims for world" in w for w in data.warnings)


@pytest.mark.parametrize(
    "doc, message",
    [
        ({"format": "something-else", "version": 1, "claims": []}, "unknown format"),
        ({"format": "mcusd-claims", "version": 2, "claims": []}, "version 2 is not supported"),
        ({"format": "mcusd-claims", "version": "1", "claims": []}, "must be an integer"),
        ({"format": "mcusd-claims", "version": 1}, "missing 'claims'"),
        ({"format": "mcusd-claims", "version": 1, "claims": [{"worldId": WORLD, "x": 1, "z": 2}]}, "missing factionId"),
        ({"format": "mcusd-claims", "version": 1, "claims": [{"worldId": "x", "x": 1, "z": 2, "factionId": "f"}]}, "not a valid UUID"),
        ({"format": "mcusd-claims", "version": 1, "claims": [{"worldId": WORLD, "x": "1", "z": 2, "factionId": "f"}]}, "'x' must be an integer"),
        ({"format": "mcusd-claims", "version": 1, "claims": [{"worldId": WORLD, "x": True, "z": 2, "factionId": "f"}]}, "'x' must be an integer"),
        ({"format": "mcusd-claims", "version": 1, "claims": [], "factions": [{"id": "f", "color": "blue"}]}, "rrggbb"),
        ({"format": "mcusd-claims", "version": 1, "claims": "nope"}, "must be a list"),
        ({"hello": "world"}, "not a recognised claims input"),
        ({"format": "mcusd-claims", "version": 0, "claims": []}, "version 0 is not supported"),
        ({"format": "mcusd-claims", "version": True, "claims": []}, "'version' must be an integer"),
        ({"format": "mcusd-claims", "version": 1, "claims": [{"worldId": WORLD, "x": 1.5, "z": 2, "factionId": "f"}]}, "'x' must be an integer"),
        ({"format": "mcusd-claims", "version": 1, "claims": [{"worldId": 123, "x": 1, "z": 2, "factionId": "f"}]}, "worldId must be a UUID string"),
        ({"format": "mcusd-claims", "version": 1, "claims": [{"worldId": WORLD, "x": 1, "z": 2, "factionId": 7}]}, "'factionId' must be a string"),
        ({"format": "mcusd-claims", "version": 1, "claims": ["nope"]}, "each claim must be an object"),
        ({"format": "mcusd-claims", "version": 1, "claims": [], "factions": "nope"}, "'factions' must be a list"),
        ({"format": "mcusd-claims", "version": 1, "claims": [], "factions": [{"name": "f"}]}, "needs a string 'id'"),
        ({"format": "mcusd-claims", "version": 1, "claims": [], "factions": [{"id": "f", "name": 3}]}, "'name' must be a string"),
    ],
)
def test_invalid_inputs_are_rejected(tmp_path, doc, message):
    with pytest.raises(ClaimsError, match=message):
        load_claims(write_json(tmp_path / "bad.json", doc))


def test_invalid_json_and_missing_file(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(ClaimsError, match="invalid JSON"):
        load_claims(bad)
    with pytest.raises(ClaimsError, match="not found"):
        load_claims(tmp_path / "missing.json")
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    with pytest.raises(ClaimsError, match="claimed_chunks.json not found"):
        load_claims(empty_dir)


def test_mf_directory_without_factions_file_warns(tmp_path):
    data = load_claims(mf_directory(tmp_path, with_factions=False))
    assert data.source_kind == "mf-json-storage"
    assert any("factions.json not found" in w for w in data.warnings)
    assert len(data.claims) == 3


@pytest.mark.parametrize(
    "claims_doc, factions_doc, message",
    [
        ([], None, "expected an object with a 'claims' list"),
        ({"claims": []}, {"other": []}, "expected an object with a 'factions' list"),
        ({"claims": []}, {"factions": ["nope"]}, "each faction must be an object"),
        ({"claims": []}, {"factions": [{"name": "n"}]}, "needs a string 'id'"),
    ],
)
def test_malformed_mf_directory_is_rejected(tmp_path, claims_doc, factions_doc, message):
    directory = tmp_path / "medieval_factions_data"
    directory.mkdir()
    write_json(directory / "claimed_chunks.json", claims_doc)
    if factions_doc is not None:
        write_json(directory / "factions.json", factions_doc)
    with pytest.raises(ClaimsError, match=message):
        load_claims(directory)


def test_world_ids_are_stored_in_canonical_lowercase(tmp_path):
    claims = [{"worldId": {"value": WORLD.upper()}, "x": 0, "z": 0, "factionId": NORTH}]
    data = load_claims(write_json(tmp_path / "api.json", claims))
    assert data.world_ids() == [WORLD]
    assert len(data.select_world(WORLD)) == 1


def test_mf_faction_value_wrappers_and_missing_name(tmp_path):
    directory = tmp_path / "medieval_factions_data"
    directory.mkdir()
    write_json(directory / "claimed_chunks.json", {"claims": CLAIMS})
    write_json(directory / "factions.json", {"factions": [
        {"id": {"value": NORTH}, "flags": {"color": {"value": "#3366cc"}}},
        {"id": SOUTH, "name": "Southmoor", "flags": {}},
    ]})
    data = load_claims(directory)
    north, south = data.faction(NORTH), data.faction(SOUTH)
    assert (north.name, north.color, north.color_from_input) == (NORTH, parse_color("#3366cc"), True)
    assert (south.name, south.color, south.color_from_input) == ("Southmoor", hashed_color(SOUTH), False)
    assert data.warnings == []


def test_native_faction_without_colour_and_duplicate_ids(tmp_path):
    doc = {"format": "mcusd-claims", "version": 1, "claims": CLAIMS, "factions": [
        {"id": NORTH, "color": "#3366cc"},
        {"id": NORTH, "name": "Impostor", "color": "#000000"},
        {"id": SOUTH, "name": "Southmoor"},
    ]}
    data = load_claims(write_json(tmp_path / "claims.json", doc))
    north, south = data.faction(NORTH), data.faction(SOUTH)
    assert (north.name, north.color) == (NORTH, parse_color("#3366cc"))  # the first entry for an ID wins
    assert (south.color, south.color_from_input) == (hashed_color(SOUTH), False)


def test_claimed_chunks_file_alone_and_value_wrappers(tmp_path):
    wrapped = [{"worldId": WORLD, "x": 1.0, "z": 2, "factionId": {"value": NORTH}}]
    data = load_claims(write_json(tmp_path / "claimed_chunks.json", {"claims": wrapped}))
    (claim,) = data.claims
    assert (claim.x, claim.z, claim.faction_id) == (1, 2, NORTH)


def test_duplicate_claims_are_merged(tmp_path):
    data = load_claims(native(tmp_path, claims=CLAIMS + CLAIMS[:1]))
    assert len(data.claims) == 3


def test_layer_opens_on_its_own_and_binary_mode(tmp_path):
    _, stats, stage = overlay(tmp_path, native(tmp_path))
    assert stats.path.name == "claims.usda" and stats.path.parent.name == "overlays"
    assert stats.path.read_text().startswith("#usda 1.0")
    assert stage.GetPrimAtPath("/World/Overlays/Claims").IsValid()
    assert stage.GetDefaultPrim().GetPath() == Sdf.Path("/World")
    _, stats_b, stage_b = overlay(tmp_path, native(tmp_path), binary=True, out="bin")
    assert stats_b.path.name == "claims.usdc" and stats_b.path.read_bytes()[:8] == b"PXR-USDC"
    assert describe(stage_b) == describe(stage)


def test_claims_filtered_to_export_bbox(tmp_path):
    data = load_claims(native(tmp_path))
    inside = claims_in_bbox(data.claims, BBox(-16, 0, 0, -1, 10, 15))
    assert [(c.x, c.z) for c in inside] == [(-1, 0)]
    assert len(claims_in_bbox(data.claims, BBox(-16, 0, -64, 223, 10, 15))) == 3
    assert claims_in_bbox(data.claims, None) == data.claims


def test_faction_prim_names_are_valid_and_distinct():
    ids = [NORTH, SOUTH, "a-b", "a_b", "a_2db", "Faction One", "ünï"]
    names = [faction_prim_name(i) for i in ids]
    assert all(Sdf.Path.IsValidIdentifier(n) for n in names)
    assert len(set(names)) == len(ids)


def test_example_file_loads(tmp_path):
    data, stats, stage = overlay(tmp_path, EXAMPLE)
    assert data.source_kind == "native" and not data.warnings
    assert stats.factions == 2 and stats.claims == 4
