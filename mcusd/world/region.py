"""Region reader.

Reads the 8 KiB ``.mca`` header, locates chunks, decompresses them (gzip, zlib,
uncompressed, LZ4 when the optional ``lz4`` package is installed, and external
``c.<x>.<z>.mcc`` files for oversized chunks) and returns raw NBT.

Implemented in milestone M2.
"""
