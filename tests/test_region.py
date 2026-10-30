"""AC3: region container, every compression scheme and external chunks."""

import builtins
import struct

import anvil
import pytest

from mcusd.world import region as R
from mcusd.world.region import RegionError, RegionFile, UnsupportedCompressionError, write_region
from tests.nbt_builders import chunk_bytes


def _raw_scheme_byte(path, local_x, local_z):
    data = path.read_bytes()
    (location,) = struct.unpack_from(">I", data, 4 * (local_x + 32 * local_z))
    return data[(location >> 8) * 4096 + 4]


@pytest.mark.parametrize("scheme", [R.GZIP, R.ZLIB, R.UNCOMPRESSED, R.LZ4])
def test_each_scheme_round_trips(tmp_path, scheme):
    payload = chunk_bytes(3, 5)
    path = write_region(tmp_path / "r.0.0.mca", {(3, 5): payload}, scheme=scheme)
    assert _raw_scheme_byte(path, 3, 5) == scheme
    assert RegionFile(path).read_chunk(3, 5) == payload


def test_all_schemes_and_external_decode_to_identical_nbt(tmp_path):
    """Same chunk stored five ways: gzip, zlib, raw, LZ4 and external .mcc (zlib)."""
    slots = {(0, 0): R.GZIP, (1, 0): R.ZLIB, (2, 0): R.UNCOMPRESSED, (3, 0): R.LZ4, (4, 0): R.ZLIB}
    payload = chunk_bytes(-30, -32)
    path = write_region(
        tmp_path / "r.-1.-1.mca",
        {slot: payload for slot in slots},
        scheme=slots,
        external={(4, 0)},
    )
    assert (tmp_path / "c.-28.-32.mcc").is_file()
    assert _raw_scheme_byte(path, 4, 0) == R.ZLIB | R.EXTERNAL_FLAG
    region = RegionFile(path)
    decoded = {slot: region.read_chunk(*slot) for slot in slots}
    assert set(decoded.values()) == {payload}
    parsed = [region.read_nbt(*slot) for slot in slots]
    assert all(p.pretty_tree() == parsed[0].pretty_tree() for p in parsed)


def test_external_chunk_with_lz4_base_scheme(tmp_path):
    payload = chunk_bytes(33, 1)
    path = write_region(tmp_path / "r.1.0.mca", {(1, 1): payload}, scheme=R.LZ4, external={(1, 1)})
    assert (tmp_path / "c.33.1.mcc").is_file()
    assert RegionFile(path).read_chunk(1, 1) == payload


def test_decoded_nbt_is_accepted_by_anvil_chunk(tmp_path):
    path = write_region(tmp_path / "r.-1.0.mca", {(31, 2): chunk_bytes(-1, 2)})
    chunk = anvil.Chunk(RegionFile(path).read_nbt(31, 2))
    assert (chunk.x, chunk.z) == (-1, 2)


def test_scheme_127_names_file_and_chunk(tmp_path):
    path = write_region(tmp_path / "r.-2.3.mca", {(5, 7): chunk_bytes(-59, 103)})
    data = bytearray(path.read_bytes())
    (location,) = struct.unpack_from(">I", data, 4 * (5 + 32 * 7))
    data[(location >> 8) * 4096 + 4] = R.CUSTOM
    path.write_bytes(bytes(data))
    with pytest.raises(UnsupportedCompressionError) as excinfo:
        RegionFile(path).read_chunk(5, 7)
    message = str(excinfo.value)
    assert "r.-2.3.mca" in message
    assert "(-59, 103)" in message
    assert "127" in message


def test_unknown_scheme_is_an_error(tmp_path):
    path = write_region(tmp_path / "r.0.0.mca", {(0, 0): chunk_bytes(0, 0)})
    data = bytearray(path.read_bytes())
    data[2 * 4096 + 4] = 9
    path.write_bytes(bytes(data))
    with pytest.raises(UnsupportedCompressionError, match="unknown compression scheme 9"):
        RegionFile(path).read_chunk(0, 0)


def test_missing_external_file(tmp_path):
    path = write_region(tmp_path / "r.0.0.mca", {(2, 2): chunk_bytes(2, 2)}, external={(2, 2)})
    (tmp_path / "c.2.2.mcc").unlink()
    with pytest.raises(RegionError, match=r"c\.2\.2\.mcc is missing"):
        RegionFile(path).read_chunk(2, 2)


def test_lz4_without_package_gives_install_hint(tmp_path, monkeypatch):
    path = write_region(tmp_path / "r.0.0.mca", {(0, 0): chunk_bytes(0, 0)}, scheme=R.LZ4)
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("lz4"):
            raise ImportError("no lz4")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(UnsupportedCompressionError, match=r"minecraft-usd-exporter\[lz4\]"):
        RegionFile(path).read_chunk(0, 0)


def test_header_listing_and_timestamps(tmp_path):
    chunks = {(0, 0): chunk_bytes(0, 0), (31, 31): chunk_bytes(31, 31), (7, 1): chunk_bytes(7, 1)}
    path = write_region(tmp_path / "r.0.0.mca", chunks, timestamps={(7, 1): 1_700_000_000})
    region = RegionFile(path)
    assert region.present_chunks() == [(0, 0), (7, 1), (31, 31)]
    assert not region.has_chunk(1, 1)
    assert region.read_chunk(1, 1) is None
    stamps = {(e.local_x, e.local_z): e.timestamp for e in region.entries()}
    assert stamps[(7, 1)] == 1_700_000_000
    assert sorted((x, z) for x, z, _ in region.iter_chunks()) == sorted(chunks)


def test_large_chunk_spans_multiple_sectors(tmp_path):
    payload = chunk_bytes(0, 0) + bytes(range(256)) * 64  # trailing data is kept verbatim
    path = write_region(tmp_path / "r.0.0.mca", {(0, 0): payload}, scheme=R.UNCOMPRESSED)
    assert RegionFile(path).entries()[0].sector_count == 5
    assert RegionFile(path).read_chunk(0, 0) == payload


def test_empty_region_file_has_no_chunks(tmp_path):
    path = tmp_path / "r.0.0.mca"
    path.write_bytes(b"")
    assert RegionFile(path).present_chunks() == []


def test_truncated_header(tmp_path):
    path = tmp_path / "r.0.0.mca"
    path.write_bytes(b"\0" * 100)
    with pytest.raises(RegionError, match="truncated header"):
        RegionFile(path)


def test_chunk_past_end_of_file(tmp_path):
    path = write_region(tmp_path / "r.0.0.mca", {(0, 0): chunk_bytes(0, 0)})
    path.write_bytes(path.read_bytes()[: 2 * 4096 + 2])
    with pytest.raises(RegionError, match="past end of file"):
        RegionFile(path).read_chunk(0, 0)


def test_corrupt_zlib_data_names_chunk(tmp_path):
    path = write_region(tmp_path / "r.1.1.mca", {(0, 0): chunk_bytes(32, 32)})
    data = bytearray(path.read_bytes())
    data[2 * 4096 + 5 : 2 * 4096 + 15] = b"\xff" * 10
    path.write_bytes(bytes(data))
    with pytest.raises(RegionError, match=r"chunk \(32, 32\).*zlib"):
        RegionFile(path).read_chunk(0, 0)


def test_region_name_parsing():
    assert R.region_coords("/x/r.-3.12.mca") == (-3, 12)
    with pytest.raises(RegionError):
        R.region_coords("r.0.0.mcr")


def test_out_of_range_local_coordinates(tmp_path):
    path = write_region(tmp_path / "r.0.0.mca", {})
    with pytest.raises(ValueError):
        RegionFile(path).read_chunk(32, 0)


@pytest.mark.parametrize(
    "data, expected",
    [
        (b"", 0x02CC5D05),
        (b"a", 0x550D7456),
        (b"abc", 0x32D153FF),
        (b"Nobody inspects the spammish repetition", 0xE2293B2F),
    ],
)
def test_xxh32_reference_vectors(data, expected):
    assert R.xxh32(data) == expected


def test_length_beyond_allocated_sectors_is_rejected(tmp_path):
    """A damaged length must not read into the neighbouring chunk's sectors."""
    first, second = chunk_bytes(0, 0), chunk_bytes(1, 0)
    path = write_region(tmp_path / "r.0.0.mca", {(0, 0): first, (1, 0): second}, scheme=R.UNCOMPRESSED)
    data = bytearray(path.read_bytes())
    start = 2 * 4096  # chunk (0, 0) occupies sector 2; chunk (1, 0) follows in sector 3
    struct.pack_into(">I", data, start, 4096 + 100)
    path.write_bytes(bytes(data))
    region = RegionFile(path)
    with pytest.raises(RegionError, match="exceeds its 1 allocated sector"):
        region.read_chunk(0, 0)
    assert region.read_chunk(1, 0) == second


def _lz4_blocks(stream):
    pos, blocks = 0, []
    while pos < len(stream):
        magic, token, clen, dlen, checksum = struct.unpack_from("<8sBiii", stream, pos)
        assert magic == b"LZ4Block"
        blocks.append((token, clen, dlen, checksum))
        pos += 21 + clen
    return blocks


def test_lz4_writer_matches_lz4_java_block_layout():
    payload = bytes(200_000)  # compressible, spans four 64 KiB blocks
    blocks = _lz4_blocks(R.compress(payload, R.LZ4))
    assert [b[2] for b in blocks] == [65536, 65536, 65536, 200_000 - 3 * 65536, 0]
    assert all(token & 0x0F == 6 for token, *_ in blocks)  # 64 KiB, as the game writes (0x26)
    assert blocks[0][0] & 0xF0 == 0x20
    assert R.decompress(R.compress(payload, R.LZ4), R.LZ4) == payload


def test_lz4_writer_uses_raw_blocks_for_incompressible_data():
    payload = bytes(random_bytes(70_000))
    stream = R.compress(payload, R.LZ4)
    blocks = _lz4_blocks(stream)
    assert blocks[0][0] & 0xF0 == 0x10 and blocks[0][1] == blocks[0][2] == 65536
    assert R.decompress(stream, R.LZ4) == payload


def random_bytes(n):
    import random

    return random.Random(5).randbytes(n)


def test_lz4_checksum_mismatch_is_detected():
    payload = bytes(random_bytes(3000))  # incompressible -> stored as a raw block
    stream = bytearray(R.compress(payload, R.LZ4))
    assert stream[8] & 0xF0 == 0x10
    stream[21 + 100] ^= 0xFF  # flip one payload byte, leave the checksum alone
    with pytest.raises(RegionError, match="checksum mismatch"):
        R.decompress(bytes(stream), R.LZ4)


def test_lz4_block_larger_than_declared_size_is_rejected():
    stream = bytearray(R.compress(bytes(5000), R.LZ4))
    stream[8] = (stream[8] & 0xF0) | 0  # declare 1 KiB blocks
    with pytest.raises(RegionError, match="block size"):
        R.decompress(bytes(stream), R.LZ4)


def test_zero_length_chunk_names_chunk(tmp_path):
    path = write_region(tmp_path / "r.0.0.mca", {(3, 4): chunk_bytes(3, 4)})
    data = bytearray(path.read_bytes())
    struct.pack_into(">I", data, 2 * 4096, 0)
    path.write_bytes(bytes(data))
    with pytest.raises(RegionError, match=r"chunk \(3, 4\).*zero-length chunk"):
        RegionFile(path).read_chunk(3, 4)


def test_truncated_chunk_data_is_rejected(tmp_path):
    """The length field fits its sectors, but the file ends partway through the data."""
    path = write_region(tmp_path / "r.0.0.mca", {(0, 0): chunk_bytes(0, 0)}, scheme=R.UNCOMPRESSED)
    path.write_bytes(path.read_bytes()[: 2 * 4096 + 14])
    with pytest.raises(RegionError, match="chunk data is truncated"):
        RegionFile(path).read_chunk(0, 0)


def test_malformed_lz4_chunk_names_file_and_chunk(tmp_path):
    """LZ4 framing errors are re-raised with the chunk's location, as a plain RegionError."""
    path = write_region(tmp_path / "r.1.1.mca", {(0, 0): chunk_bytes(32, 32)}, scheme=R.UNCOMPRESSED)
    data = bytearray(path.read_bytes())
    data[2 * 4096 + 4] = R.LZ4  # raw NBT relabelled as LZ4: no LZ4Block magic
    path.write_bytes(bytes(data))
    with pytest.raises(RegionError) as excinfo:
        RegionFile(path).read_chunk(0, 0)
    assert not isinstance(excinfo.value, UnsupportedCompressionError)
    message = str(excinfo.value)
    assert "r.1.1.mca" in message
    assert "(32, 32)" in message
    assert "LZ4Block magic" in message


def test_lz4_truncated_block_header():
    with pytest.raises(RegionError, match="truncated LZ4 block header"):
        R.decompress(b"LZ4Block", R.LZ4)


def test_lz4_wrong_magic():
    with pytest.raises(RegionError, match="LZ4Block magic"):
        R.decompress(b"\0" * 21, R.LZ4)


def test_lz4_truncated_block_body():
    stream = R.compress(bytes(random_bytes(3000)), R.LZ4)  # one raw 3000-byte block
    with pytest.raises(RegionError, match=r"truncated LZ4 block$"):
        R.decompress(stream[: 21 + 100], R.LZ4)


def test_lz4_raw_block_with_mismatched_lengths():
    stream = struct.pack("<8sBiii", b"LZ4Block", 0x16, 4, 5, 0) + b"abcd"
    with pytest.raises(RegionError, match="raw LZ4 block has mismatched lengths"):
        R.decompress(stream, R.LZ4)


def test_lz4_unknown_block_method():
    stream = struct.pack("<8sBiii", b"LZ4Block", 0x36, 4, 4, 0) + b"abcd"
    with pytest.raises(RegionError, match="unknown LZ4Block method 0x30"):
        R.decompress(stream, R.LZ4)


def test_lz4_stream_without_end_marker_still_decodes():
    payload = bytes(random_bytes(3000))
    stream = R.compress(payload, R.LZ4)
    assert R.decompress(stream[:-21], R.LZ4) == payload


def test_compress_rejects_custom_scheme():
    with pytest.raises(UnsupportedCompressionError, match="cannot write compression scheme 127"):
        R.compress(b"data", R.CUSTOM)


def test_write_region_rejects_out_of_range_slot(tmp_path):
    path = tmp_path / "r.0.0.mca"
    with pytest.raises(ValueError, match=r"out of range: \(32, 0\)"):
        write_region(path, {(32, 0): chunk_bytes(32, 0)})
    assert not path.exists()


def test_write_region_requires_external_storage_past_255_sectors(tmp_path):
    payload = bytes(255 * 4096)  # plus the 5-byte record header: 256 sectors
    with pytest.raises(ValueError, match="needs 256 sectors; store it externally"):
        write_region(tmp_path / "r.0.0.mca", {(0, 0): payload}, scheme=R.UNCOMPRESSED)
    path = write_region(
        tmp_path / "r.0.0.mca", {(0, 0): payload}, scheme=R.UNCOMPRESSED, external={(0, 0)}
    )
    region = RegionFile(path)
    assert region.entries()[0].sector_count == 1
    assert region.read_chunk(0, 0) == payload


def test_region_and_external_file_names():
    assert R.region_filename(-1, 2) == "r.-1.2.mca"
    assert R.region_coords(R.region_filename(-3, 7)) == (-3, 7)
    assert R.external_chunk_filename(-28, -32) == "c.-28.-32.mcc"
    with pytest.raises(RegionError):
        R.region_coords("r.0.0.mca.bak")


def test_region_file_checks_name_before_reading(tmp_path):
    with pytest.raises(RegionError, match="not a region file name"):
        RegionFile(tmp_path / "level.dat")  # does not exist; the name is rejected first


def test_chunk_coords_in_negative_region(tmp_path):
    region = RegionFile(write_region(tmp_path / "r.-1.-2.mca", {}))
    assert region.chunk_coords(31, 0) == (-1, -64)
    assert region.chunk_coords(0, 31) == (-32, -33)
