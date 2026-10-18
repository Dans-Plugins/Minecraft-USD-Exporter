"""Claims reader.

Loads the claims input contract, including direct adapters for Medieval
Factions' own files. See ARCHITECTURE.md § Claims overlay input contract.

Accepted inputs, all normalised to :class:`ClaimsData`:

1. A native ``claims.json``: ``{"format": "mcusd-claims", "version": 1,
   "factions": [...], "claims": [...]}``.
2. A bare JSON array of ``Claim`` objects (Medieval Factions' ``GET /api/claims``
   response saved to a file).
3. A Medieval Factions JSON-storage directory holding ``claimed_chunks.json``
   (``{"claims": [...]}``) and, optionally, ``factions.json``
   (``{"factions": [...]}``, colour read from ``flags.color``). The
   ``claimed_chunks.json`` file on its own is accepted too.

Claim records keep Medieval Factions' field names (``worldId``, ``x``, ``z``,
``factionId``); ``x``/``z`` are chunk coordinates.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from mcusd.blocks import Color, hashed_color, parse_color

FORMAT = "mcusd-claims"
SUPPORTED_VERSION = 1
MF_CLAIMS_FILE = "claimed_chunks.json"
MF_FACTIONS_FILE = "factions.json"


class ClaimsError(Exception):
    """The claims input is malformed or ambiguous."""


@dataclass(frozen=True, order=True)
class Claim:
    world_id: str  # canonical lowercase UUID
    x: int  # chunk X
    z: int  # chunk Z
    faction_id: str


@dataclass(frozen=True)
class Faction:
    id: str
    name: str
    color: Color
    color_from_input: bool  # False when derived from the ID


@dataclass
class ClaimsData:
    claims: list[Claim]
    factions: dict[str, Faction]
    source_kind: str  # "native", "api-array" or "mf-json-storage"
    warnings: list[str] = field(default_factory=list)

    def world_ids(self) -> list[str]:
        return sorted({c.world_id for c in self.claims})

    def select_world(self, world_uuid: str | None) -> list[Claim]:
        """Claims for one world.

        ``world_uuid`` is required when the input covers more than one world;
        otherwise the single world present is used.
        """
        worlds = self.world_ids()
        if world_uuid is None:
            if len(worlds) > 1:
                raise ClaimsError(
                    f"claims cover {len(worlds)} worlds ({', '.join(worlds)}); "
                    "choose one with --world-uuid"
                )
            return list(self.claims)
        wanted = _uuid(world_uuid, "--world-uuid")
        selected = [c for c in self.claims if c.world_id == wanted]
        if not selected and self.claims:
            self.warnings.append(
                f"no claims for world {wanted}; worlds present: {', '.join(worlds)}"
            )
        return selected

    def faction(self, faction_id: str) -> Faction:
        """The faction for an ID, with a deterministic fallback for unknown IDs."""
        found = self.factions.get(faction_id)
        if found is not None:
            return found
        return Faction(faction_id, faction_id, hashed_color(faction_id), False)


def load_claims(path: str | Path) -> ClaimsData:
    """Load any accepted claims input from a file or directory."""
    path = Path(path)
    if path.is_dir():
        return _from_mf_directory(path)
    data = _read_json(path)
    if isinstance(data, list):
        return _normalise(data, None, "api-array", str(path))
    if isinstance(data, dict):
        if "format" in data:
            return _from_native(data, str(path))
        if "claims" in data:  # Medieval Factions claimed_chunks.json on its own
            return _normalise(data["claims"], None, "mf-json-storage", str(path))
    raise ClaimsError(
        f"{path}: not a recognised claims input (expected a {FORMAT!r} document, "
        "a JSON array of claims, or a Medieval Factions JSON-storage directory)"
    )


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ClaimsError(f"claims input not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise ClaimsError(f"{path}: invalid JSON: {exc}") from None


def _from_native(data: dict, where: str) -> ClaimsData:
    if data.get("format") != FORMAT:
        raise ClaimsError(f"{where}: unknown format {data.get('format')!r} (expected {FORMAT!r})")
    version = data.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ClaimsError(f"{where}: 'version' must be an integer")
    if version > SUPPORTED_VERSION or version < 1:
        raise ClaimsError(
            f"{where}: version {version} is not supported (this exporter reads version "
            f"{SUPPORTED_VERSION})"
        )
    if "claims" not in data:
        raise ClaimsError(f"{where}: missing 'claims'")
    factions = None
    if "factions" in data:
        factions = [_native_faction(f, i, where) for i, f in enumerate(_list(data["factions"], "factions", where))]
    return _normalise(data["claims"], factions, "native", where)


def _from_mf_directory(directory: Path) -> ClaimsData:
    claims_file = directory / MF_CLAIMS_FILE
    if not claims_file.is_file():
        raise ClaimsError(
            f"{directory}: not a Medieval Factions JSON-storage directory ({claims_file} not found)"
        )
    claims_doc = _read_json(claims_file)
    if not isinstance(claims_doc, dict) or "claims" not in claims_doc:
        raise ClaimsError(f"{claims_file}: expected an object with a 'claims' list")
    factions = None
    warnings = []
    factions_file = directory / MF_FACTIONS_FILE
    if factions_file.is_file():
        doc = _read_json(factions_file)
        if not isinstance(doc, dict) or "factions" not in doc:
            raise ClaimsError(f"{factions_file}: expected an object with a 'factions' list")
        factions = [_mf_faction(f, i, str(factions_file)) for i, f in
                    enumerate(_list(doc["factions"], "factions", str(factions_file)))]
    else:
        warnings.append(f"{factions_file} not found; faction names and colours are derived from IDs")
    result = _normalise(claims_doc["claims"], factions, "mf-json-storage", str(claims_file))
    result.warnings[:0] = warnings
    return result


def _list(value, name: str, where: str) -> list:
    if not isinstance(value, list):
        raise ClaimsError(f"{where}: '{name}' must be a list")
    return value


def _unwrap(value):
    """Accept a value-class wrapper such as ``{"value": "..."}`` (lenient for Gson output)."""
    if isinstance(value, dict) and set(value) == {"value"}:
        return value["value"]
    return value


def _uuid(value, where: str) -> str:
    value = _unwrap(value)
    if not isinstance(value, str):
        raise ClaimsError(f"{where}: worldId must be a UUID string, got {value!r}")
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise ClaimsError(f"{where}: {value!r} is not a valid UUID") from None


def _int(value, name: str, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, float) and value.is_integer():  # Gson may write 12.0 for Any-typed numbers
            return int(value)
        raise ClaimsError(f"{where}: '{name}' must be an integer chunk coordinate, got {value!r}")
    return value


def _color(value, where: str) -> Color:
    try:
        if not isinstance(value, str):
            raise ValueError
        return parse_color(value)
    except ValueError:
        raise ClaimsError(f"{where}: colour must be '#rrggbb', got {value!r}") from None


def _native_faction(entry, index: int, where: str) -> Faction:
    at = f"{where}: factions[{index}]"
    if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
        raise ClaimsError(f"{at}: each faction needs a string 'id'")
    name = entry.get("name", entry["id"])
    if not isinstance(name, str):
        raise ClaimsError(f"{at}: 'name' must be a string")
    if entry.get("color") is not None:
        return Faction(entry["id"], name, _color(entry["color"], at), True)
    return Faction(entry["id"], name, hashed_color(entry["id"]), False)


def _mf_faction(entry, index: int, where: str) -> Faction:
    at = f"{where}: factions[{index}]"
    if not isinstance(entry, dict):
        raise ClaimsError(f"{at}: each faction must be an object")
    faction_id = _unwrap(entry.get("id"))
    if not isinstance(faction_id, str):
        raise ClaimsError(f"{at}: each faction needs a string 'id'")
    name = entry.get("name") if isinstance(entry.get("name"), str) else faction_id
    flags = entry.get("flags") or {}
    color = _unwrap(flags.get("color")) if isinstance(flags, dict) else None
    if color is not None:
        return Faction(faction_id, name, _color(color, at), True)
    return Faction(faction_id, name, hashed_color(faction_id), False)


def _normalise(raw_claims, factions: list[Faction] | None, kind: str, where: str) -> ClaimsData:
    claims = set()
    for index, entry in enumerate(_list(raw_claims, "claims", where)):
        at = f"{where}: claims[{index}]"
        if not isinstance(entry, dict):
            raise ClaimsError(f"{at}: each claim must be an object")
        missing = [k for k in ("worldId", "x", "z", "factionId") if k not in entry]
        if missing:
            raise ClaimsError(f"{at}: missing {', '.join(missing)}")
        faction_id = _unwrap(entry["factionId"])
        if not isinstance(faction_id, str):
            raise ClaimsError(f"{at}: 'factionId' must be a string")
        claims.add(Claim(_uuid(entry["worldId"], at), _int(entry["x"], "x", at),
                         _int(entry["z"], "z", at), faction_id))
    by_id: dict[str, Faction] = {}
    for faction in factions or []:
        by_id.setdefault(faction.id, faction)
    result = ClaimsData(sorted(claims, key=lambda c: (c.faction_id, c.world_id, c.z, c.x)), by_id, kind)
    if factions is not None:
        unknown = sorted({c.faction_id for c in claims} - set(by_id))
        for faction_id in unknown:
            result.warnings.append(
                f"claims reference unknown faction {faction_id!r}; drawn with a fallback colour"
            )
    return result
