"""Region reader.

Reads the 8 KiB ``.mca`` header, locates chunks, decompresses them (gzip, zlib,
uncompressed, LZ4 when the optional ``lz4`` package is installed, and external
``c.<x>.<z>.mcc`` files for oversized chunks) and returns raw NBT bytes, which
:func:`parse_nbt` turns into the ``nbt.NBTFile`` that ``anvil.Chunk`` accepts.

Container rules follow the `Region file format
<https://minecraft.wiki/w/Region_file_format>`_ article. LZ4 chunks (scheme 4)
are stored in lz4-java's ``LZ4BlockOutputStream`` framing (``LZ4Block`` magic,
then per-block token, little-endian compressed/decompressed lengths and an
XXH32 checksum); this was confirmed against a region written by a vanilla
1.21.11 server with ``region-file-compression=lz4``.

:func:`write_region` is the inverse, used to build synthetic region files in
tests and to rewrite trimmed fixture worlds. It never touches a user's world:
the exporter itself is read-only.
"""

from __future__ import annotations

import gzip
import io
import math
import re
import struct
import zlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from nbt import nbt

SECTOR_BYTES = 4096
HEADER_BYTES = 2 * SECTOR_BYTES
CHUNKS_PER_SIDE = 32

GZIP = 1
ZLIB = 2
UNCOMPRESSED = 3
LZ4 = 4
CUSTOM = 127
EXTERNAL_FLAG = 128

SCHEME_NAMES = {
    GZIP: "gzip",
    ZLIB: "zlib",
    UNCOMPRESSED: "uncompressed",
    LZ4: "LZ4",
    CUSTOM: "custom",
}

_REGION_NAME = re.compile(r"^r\.(-?\d+)\.(-?\d+)\.mca$")
_LZ4_MAGIC = b"LZ4Block"
_LZ4_HEADER = struct.Struct("<8sBiii")  # magic, token, compressed, decompressed, checksum
_LZ4_METHOD_RAW = 0x10
_LZ4_METHOD_LZ4 = 0x20
_LZ4_CHECKSUM_SEED = 0x9747B28C  # lz4-java LZ4BlockOutputStream default


class RegionError(Exception):
    """A region file or chunk could not be read."""


class UnsupportedCompressionError(RegionError):
    """A chunk uses a compression scheme this tool cannot decode."""


def region_coords(path: str | Path) -> tuple[int, int]:
    """Return ``(region_x, region_z)`` parsed from an ``r.<x>.<z>.mca`` name."""
    match = _REGION_NAME.match(Path(path).name)
    if not match:
        raise RegionError(f"not a region file name (expected r.<x>.<z>.mca): {path}")
    return int(match.group(1)), int(match.group(2))


def region_filename(region_x: int, region_z: int) -> str:
    return f"r.{region_x}.{region_z}.mca"


def external_chunk_filename(chunk_x: int, chunk_z: int) -> str:
    """Name of the external ``.mcc`` file for an absolute chunk coordinate."""
    return f"c.{chunk_x}.{chunk_z}.mcc"


def parse_nbt(raw: bytes) -> nbt.NBTFile:
    """Parse uncompressed chunk NBT into the object ``anvil.Chunk`` accepts."""
    return nbt.NBTFile(buffer=io.BytesIO(raw))


@dataclass(frozen=True)
class ChunkEntry:
    """One populated slot of a region header."""

    local_x: int
    local_z: int
    sector_offset: int
    sector_count: int
    timestamp: int


class RegionFile:
    """Read-only access to one ``.mca`` region file."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.x, self.z = region_coords(self.path)
        self._data = self.path.read_bytes()
        if 0 < len(self._data) < HEADER_BYTES:
            raise RegionError(
                f"{self.path}: truncated header ({len(self._data)} bytes, "
                f"expected at least {HEADER_BYTES})"
            )
        self._entries: dict[tuple[int, int], ChunkEntry] = {}
        if self._data:
            self._parse_header()

    def _parse_header(self) -> None:
        locations = struct.unpack_from(">1024I", self._data, 0)
        timestamps = struct.unpack_from(">1024I", self._data, SECTOR_BYTES)
        for index, location in enumerate(locations):
            offset, count = location >> 8, location & 0xFF
            if offset == 0 and count == 0:
                continue
            local_x, local_z = index % CHUNKS_PER_SIDE, index // CHUNKS_PER_SIDE
            self._entries[(local_x, local_z)] = ChunkEntry(
                local_x, local_z, offset, count, timestamps[index]
            )

    def entries(self) -> list[ChunkEntry]:
        """Populated header entries, ordered by (local_z, local_x)."""
        return [self._entries[k] for k in sorted(self._entries, key=lambda k: (k[1], k[0]))]

    def present_chunks(self) -> list[tuple[int, int]]:
        """Local ``(x, z)`` coordinates (0–31) of every chunk in the header."""
        return [(e.local_x, e.local_z) for e in self.entries()]

    def has_chunk(self, local_x: int, local_z: int) -> bool:
        return (local_x, local_z) in self._entries

    def chunk_coords(self, local_x: int, local_z: int) -> tuple[int, int]:
        """Absolute chunk coordinates for a local slot."""
        return self.x * CHUNKS_PER_SIDE + local_x, self.z * CHUNKS_PER_SIDE + local_z

    def _where(self, local_x: int, local_z: int) -> str:
        cx, cz = self.chunk_coords(local_x, local_z)
        return f"{self.path}: chunk ({cx}, {cz}) [local ({local_x}, {local_z})]"

    def read_chunk(self, local_x: int, local_z: int) -> bytes | None:
        """Return the decompressed NBT bytes of a chunk, or ``None`` if absent."""
        if not (0 <= local_x < CHUNKS_PER_SIDE and 0 <= local_z < CHUNKS_PER_SIDE):
            raise ValueError(f"local chunk coordinates out of range: ({local_x}, {local_z})")
        entry = self._entries.get((local_x, local_z))
        if entry is None:
            return None
        start = entry.sector_offset * SECTOR_BYTES
        if start + 5 > len(self._data):
            raise RegionError(f"{self._where(local_x, local_z)}: chunk lies past end of file")
        (length,) = struct.unpack_from(">I", self._data, start)
        if length < 1:
            raise RegionError(f"{self._where(local_x, local_z)}: zero-length chunk")
        scheme_byte = self._data[start + 4]
        payload = self._data[start + 5 : start + 4 + length]
        scheme = scheme_byte & ~EXTERNAL_FLAG
        if scheme_byte & EXTERNAL_FLAG:
            cx, cz = self.chunk_coords(local_x, local_z)
            external = self.path.parent / external_chunk_filename(cx, cz)
            try:
                payload = external.read_bytes()
            except FileNotFoundError:
                raise RegionError(
                    f"{self._where(local_x, local_z)}: external chunk file {external} is missing"
                ) from None
        elif len(payload) != length - 1:
            raise RegionError(f"{self._where(local_x, local_z)}: chunk data is truncated")
        try:
            return decompress(payload, scheme)
        except UnsupportedCompressionError as exc:
            raise UnsupportedCompressionError(f"{self._where(local_x, local_z)}: {exc}") from None
        except RegionError as exc:
            raise RegionError(f"{self._where(local_x, local_z)}: {exc}") from None
        except (OSError, EOFError, zlib.error, ValueError) as exc:
            raise RegionError(
                f"{self._where(local_x, local_z)}: could not decompress "
                f"{SCHEME_NAMES.get(scheme, scheme)} data: {exc}"
            ) from None

    def read_nbt(self, local_x: int, local_z: int) -> nbt.NBTFile | None:
        raw = self.read_chunk(local_x, local_z)
        return None if raw is None else parse_nbt(raw)

    def iter_chunks(self) -> Iterator[tuple[int, int, bytes]]:
        """Yield ``(local_x, local_z, nbt_bytes)`` for every present chunk."""
        for local_x, local_z in self.present_chunks():
            raw = self.read_chunk(local_x, local_z)
            assert raw is not None
            yield local_x, local_z, raw


# --- compression -------------------------------------------------------------


def _require_lz4():
    try:
        import lz4.block
    except ImportError:
        raise UnsupportedCompressionError(
            "chunk uses LZ4 compression (scheme 4) but the optional 'lz4' package "
            "is not installed; install it with: pip install \"minecraft-usd-exporter[lz4]\""
        ) from None
    return lz4.block


def decompress(data: bytes, scheme: int) -> bytes:
    """Decompress one chunk payload stored with ``scheme`` (without the +128 flag)."""
    if scheme == GZIP:
        return gzip.decompress(data)
    if scheme == ZLIB:
        return zlib.decompress(data)
    if scheme == UNCOMPRESSED:
        return bytes(data)
    if scheme == LZ4:
        return _lz4_stream_decompress(data)
    if scheme == CUSTOM:
        raise UnsupportedCompressionError(
            "chunk uses custom compression (scheme 127), which is not supported"
        )
    raise UnsupportedCompressionError(f"unknown compression scheme {scheme}")


def compress(data: bytes, scheme: int) -> bytes:
    """Compress ``data`` the way the game would for ``scheme`` (1–4)."""
    if scheme == GZIP:
        return gzip.compress(data, mtime=0)
    if scheme == ZLIB:
        return zlib.compress(data)
    if scheme == UNCOMPRESSED:
        return bytes(data)
    if scheme == LZ4:
        return _lz4_stream_compress(data)
    raise UnsupportedCompressionError(f"cannot write compression scheme {scheme}")


def _lz4_stream_decompress(data: bytes) -> bytes:
    lz4_block = None
    out = []
    pos = 0
    while pos < len(data):
        if len(data) - pos < _LZ4_HEADER.size:
            raise RegionError("truncated LZ4 block header")
        magic, token, compressed_len, decompressed_len, _checksum = _LZ4_HEADER.unpack_from(data, pos)
        if magic != _LZ4_MAGIC:
            raise RegionError("LZ4 data does not start with the LZ4Block magic")
        pos += _LZ4_HEADER.size
        if decompressed_len == 0:  # end-of-stream marker written by finish()
            break
        block = data[pos : pos + compressed_len]
        if len(block) != compressed_len:
            raise RegionError("truncated LZ4 block")
        method = token & 0xF0
        if method == _LZ4_METHOD_RAW:
            out.append(block)
        elif method == _LZ4_METHOD_LZ4:
            if lz4_block is None:
                lz4_block = _require_lz4()
            out.append(lz4_block.decompress(block, uncompressed_size=decompressed_len))
        else:
            raise RegionError(f"unknown LZ4Block method 0x{method:02x}")
        pos += compressed_len
    return b"".join(out)


def _lz4_stream_compress(data: bytes) -> bytes:
    lz4_block = _require_lz4()
    compressed = lz4_block.compress(data, store_size=False)
    checksum = xxh32(data, _LZ4_CHECKSUM_SEED) & 0x0FFFFFFF
    header = _LZ4_HEADER.pack(_LZ4_MAGIC, _LZ4_METHOD_LZ4, len(compressed), len(data), checksum)
    end = _LZ4_HEADER.pack(_LZ4_MAGIC, _LZ4_METHOD_RAW, 0, 0, 0)
    return header + compressed + end


_P1, _P2, _P3, _P4, _P5 = 2654435761, 2246822519, 3266489917, 668265263, 374761393
_M32 = 0xFFFFFFFF


def _rotl(x: int, r: int) -> int:
    return ((x << r) | (x >> (32 - r))) & _M32


def xxh32(data: bytes, seed: int = 0) -> int:
    """Pure-Python XXH32 (for the lz4-java stream checksum when writing)."""
    n = len(data)
    pos = 0
    if n >= 16:
        v1 = (seed + _P1 + _P2) & _M32
        v2 = (seed + _P2) & _M32
        v3 = seed & _M32
        v4 = (seed - _P1) & _M32
        while pos <= n - 16:
            a, b, c, d = struct.unpack_from("<4I", data, pos)
            v1 = (_rotl((v1 + a * _P2) & _M32, 13) * _P1) & _M32
            v2 = (_rotl((v2 + b * _P2) & _M32, 13) * _P1) & _M32
            v3 = (_rotl((v3 + c * _P2) & _M32, 13) * _P1) & _M32
            v4 = (_rotl((v4 + d * _P2) & _M32, 13) * _P1) & _M32
            pos += 16
        h = (_rotl(v1, 1) + _rotl(v2, 7) + _rotl(v3, 12) + _rotl(v4, 18)) & _M32
    else:
        h = (seed + _P5) & _M32
    h = (h + n) & _M32
    while pos <= n - 4:
        (k,) = struct.unpack_from("<I", data, pos)
        h = (_rotl((h + k * _P3) & _M32, 17) * _P4) & _M32
        pos += 4
    while pos < n:
        h = (_rotl((h + data[pos] * _P5) & _M32, 11) * _P1) & _M32
        pos += 1
    h ^= h >> 15
    h = (h * _P2) & _M32
    h ^= h >> 13
    h = (h * _P3) & _M32
    h ^= h >> 16
    return h


# --- writing -----------------------------------------------------------------


def write_region(
    path: str | Path,
    chunks: Mapping[tuple[int, int], bytes],
    *,
    scheme: int | Mapping[tuple[int, int], int] = ZLIB,
    external: frozenset[tuple[int, int]] | set[tuple[int, int]] = frozenset(),
    timestamps: Mapping[tuple[int, int], int] | None = None,
) -> Path:
    """Write a region file holding ``chunks`` (local ``(x, z)`` -> NBT bytes).

    ``scheme`` is one compression scheme for every chunk or a per-chunk mapping.
    Chunks listed in ``external`` are stored in ``c.<x>.<z>.mcc`` files beside the
    region, as the game does for chunks larger than 255 sectors. Used for tests and
    fixture trimming; the exporter never writes into a world folder.
    """
    path = Path(path)
    region_x, region_z = region_coords(path)
    locations = [0] * (CHUNKS_PER_SIDE * CHUNKS_PER_SIDE)
    stamps = [0] * (CHUNKS_PER_SIDE * CHUNKS_PER_SIDE)
    body = bytearray()
    next_sector = HEADER_BYTES // SECTOR_BYTES
    for (local_x, local_z) in sorted(chunks, key=lambda k: (k[1], k[0])):
        if not (0 <= local_x < CHUNKS_PER_SIDE and 0 <= local_z < CHUNKS_PER_SIDE):
            raise ValueError(f"local chunk coordinates out of range: ({local_x}, {local_z})")
        chunk_scheme = scheme if isinstance(scheme, int) else scheme[(local_x, local_z)]
        compressed = compress(chunks[(local_x, local_z)], chunk_scheme)
        if (local_x, local_z) in external:
            cx = region_x * CHUNKS_PER_SIDE + local_x
            cz = region_z * CHUNKS_PER_SIDE + local_z
            (path.parent / external_chunk_filename(cx, cz)).write_bytes(compressed)
            record = struct.pack(">IB", 1, chunk_scheme | EXTERNAL_FLAG)
        else:
            record = struct.pack(">IB", len(compressed) + 1, chunk_scheme) + compressed
        sectors = math.ceil(len(record) / SECTOR_BYTES)
        if sectors > 255:
            raise ValueError(
                f"chunk ({local_x}, {local_z}) needs {sectors} sectors; store it externally"
            )
        record += b"\0" * (sectors * SECTOR_BYTES - len(record))
        index = local_x + local_z * CHUNKS_PER_SIDE
        locations[index] = (next_sector << 8) | sectors
        stamps[index] = (timestamps or {}).get((local_x, local_z), 0)
        body += record
        next_sector += sectors
    header = struct.pack(">1024I", *locations) + struct.pack(">1024I", *stamps)
    path.write_bytes(header + bytes(body))
    return path
