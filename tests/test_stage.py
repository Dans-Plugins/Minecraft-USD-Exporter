"""Stage assembler: root layer naming, sublayer order and re-export, independent of a real world."""

from pxr import Sdf, Usd, UsdGeom

from mcusd import __version__
from mcusd.usd.stage import root_filename, write_root_layer


def make_layer(path, probe):
    """Write a layer defining ``/World`` with an ``mcusd:probe`` value, to observe which opinion wins."""
    path.parent.mkdir(parents=True, exist_ok=True)
    layer = Sdf.Layer.CreateNew(str(path))
    prim = Sdf.CreatePrimInLayer(layer, "/World")
    prim.specifier = Sdf.SpecifierDef
    Sdf.AttributeSpec(prim, "mcusd:probe", Sdf.ValueTypeNames.Int).default = probe
    layer.Save()
    return path


def write(out, overlays=(), bbox=(0, 0, 0, 1, 1, 1), binary=False):
    return write_root_layer(out, terrain=out / "terrain.usdc", overlays=list(overlays), source_world="probe-world",
                            dimension="minecraft:the_nether", bbox=list(bbox), binary=binary)


def test_root_filename_follows_binary_flag():
    assert root_filename(False) == "world.usda"
    assert root_filename(True) == "world.usdc"


def test_root_layer_metadata(tmp_path):
    make_layer(tmp_path / "terrain.usdc", 1)
    path = write(tmp_path, bbox=(-7, -64, 3, 12, 80, 40))
    assert path == tmp_path / "world.usda"
    assert path.read_text().startswith("#usda 1.0")
    layer = Sdf.Layer.FindOrOpen(str(path))
    assert layer.defaultPrim == "World"
    stage = Usd.Stage.Open(layer)
    assert UsdGeom.GetStageUpAxis(stage) == UsdGeom.Tokens.y
    assert UsdGeom.GetStageMetersPerUnit(stage) == 1.0
    data = layer.customLayerData
    assert data["mcusd:sourceWorld"] == "probe-world"
    assert data["mcusd:dimension"] == "minecraft:the_nether"
    assert list(data["mcusd:bbox"]) == [-7, -64, 3, 12, 80, 40]
    assert data["mcusd:exporterVersion"] == __version__


def test_binary_root_is_written_as_crate(tmp_path):
    make_layer(tmp_path / "terrain.usdc", 1)
    path = write(tmp_path, binary=True)
    assert path == tmp_path / "world.usdc"
    assert path.read_bytes()[:8] == b"PXR-USDC"
    assert not (tmp_path / "world.usda").exists()
    assert list(Sdf.Layer.FindOrOpen(str(path)).subLayerPaths) == ["./terrain.usdc"]


def test_overlays_are_listed_in_order_before_terrain_and_win(tmp_path):
    make_layer(tmp_path / "terrain.usdc", 1)
    first = make_layer(tmp_path / "overlays" / "first.usda", 3)
    second = make_layer(tmp_path / "overlays" / "second.usda", 2)
    path = write(tmp_path, overlays=[first, second])
    layer = Sdf.Layer.FindOrOpen(str(path))
    assert list(layer.subLayerPaths) == ["./overlays/first.usda", "./overlays/second.usda", "./terrain.usdc"]
    stage = Usd.Stage.Open(layer)
    assert stage.GetPrimAtPath("/World").GetAttribute("mcusd:probe").Get() == 3
    stage.MuteLayer(Sdf.Layer.FindOrOpen(str(first)).identifier)
    assert stage.GetPrimAtPath("/World").GetAttribute("mcusd:probe").Get() == 2
    stage.MuteLayer(Sdf.Layer.FindOrOpen(str(second)).identifier)
    assert stage.GetPrimAtPath("/World").GetAttribute("mcusd:probe").Get() == 1


def test_rewriting_replaces_the_previous_root_layer(tmp_path):
    make_layer(tmp_path / "terrain.usdc", 1)
    overlay = make_layer(tmp_path / "overlays" / "claims.usda", 2)
    write(tmp_path, overlays=[overlay], bbox=(0, 0, 0, 15, 15, 15))
    path = write(tmp_path, bbox=(16, 0, 16, 31, 15, 31))
    layer = Sdf.Layer.FindOrOpen(str(path))
    assert list(layer.subLayerPaths) == ["./terrain.usdc"]
    assert list(layer.customLayerData["mcusd:bbox"]) == [16, 0, 16, 31, 15, 31]
    assert Usd.Stage.Open(layer).GetPrimAtPath("/World").GetAttribute("mcusd:probe").Get() == 1
