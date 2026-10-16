"""MOBI / AZW / AZW3 metadata via the PalmDB record list and EXTH header.

This is the most fragile format we handle, so the parser is written to be
defensive rather than complete: every field is optional, every read is bounds
checked, and a file we cannot make sense of degrades to filename-only metadata.

Layout, for the next person reading this:

    PalmDB header        78 bytes, record count at offset 76, followed by an
                         8-byte entry per record giving its file offset
    record 0             PalmDOC header (16 bytes) then the MOBI header
    MOBI header          starts at offset 16 of record 0; 'MOBI' magic, then
                         its length at +4, EXTH flags at +108
    EXTH block           immediately after the MOBI header when flags & 0x40
"""

from __future__ import annotations

import struct
from pathlib import Path

from .base import (
    ExtractedMetadata,
    clean_description,
    clean_text,
    looks_like_image,
    looks_like_junk_title,
    normalize_isbn,
    parse_year,
    split_authors,
)

PALMDB_HEADER_SIZE = 78

# MOBI header field offsets, relative to the 'MOBI' magic.
MOBI_HEADER_LENGTH = 4
MOBI_TEXT_ENCODING = 12
MOBI_FULL_NAME_OFFSET = 64
MOBI_FULL_NAME_LENGTH = 68
MOBI_FIRST_IMAGE_INDEX = 88
MOBI_EXTH_FLAGS = 108

# EXTH record types we care about.
EXTH_AUTHOR = 100
EXTH_PUBLISHER = 101
EXTH_DESCRIPTION = 103
EXTH_ISBN = 104
EXTH_PUBLISHED = 106
EXTH_COVER_OFFSET = 201
EXTH_LANGUAGE = 524
EXTH_UPDATED_TITLE = 503

MIN_COVER_BYTES = 2_000


def extract(path: Path) -> ExtractedMetadata:
    meta = ExtractedMetadata()
    try:
        with path.open("rb") as handle:
            data = handle.read()
    except OSError as exc:
        meta.error = f"unreadable file: {exc}"
        return meta

    try:
        records = _record_offsets(data)
        if len(records) < 2:
            meta.error = "not a PalmDB container"
            return meta

        record0 = data[records[0] : records[1]]
        if len(record0) < 16:
            meta.error = "truncated record 0"
            return meta

        # PalmDOC header: encryption type lives at offset 12.
        encryption = struct.unpack_from(">H", record0, 12)[0]
        if encryption != 0:
            meta.drm = True
            meta.error = "DRM protected"
            # Keep going: the EXTH block is usually still readable.

        if record0[16:20] != b"MOBI":
            meta.error = meta.error or "no MOBI header"
            return meta

        encoding = _encoding(record0)
        full_name = _full_name(record0, encoding)
        if not looks_like_junk_title(full_name):
            meta.title = full_name

        header_length = struct.unpack_from(">I", record0, 16 + MOBI_HEADER_LENGTH)[0]
        exth_flags = _u32(record0, 16 + MOBI_EXTH_FLAGS)
        exth: dict[int, list[bytes]] = {}
        if exth_flags is not None and exth_flags & 0x40:
            exth = _read_exth(record0, 16 + header_length)

        _apply_exth(meta, exth, encoding)

        first_image = _u32(record0, 16 + MOBI_FIRST_IMAGE_INDEX)
        meta.cover_bytes = _find_cover(data, records, exth, first_image)
    except (struct.error, IndexError, ValueError) as exc:
        meta.error = meta.error or f"malformed mobi: {exc}"
    return meta


def _record_offsets(data: bytes) -> list[int]:
    if len(data) < PALMDB_HEADER_SIZE:
        return []
    count = struct.unpack_from(">H", data, 76)[0]
    if count == 0 or count > 20_000:
        return []
    needed = PALMDB_HEADER_SIZE + count * 8
    if len(data) < needed:
        return []
    offsets = [
        struct.unpack_from(">I", data, PALMDB_HEADER_SIZE + i * 8)[0]
        for i in range(count)
    ]
    # Records must be ascending and inside the file to be usable.
    offsets = [o for o in offsets if o <= len(data)]
    offsets.append(len(data))
    return offsets


def _encoding(record0: bytes) -> str:
    code = _u32(record0, 16 + MOBI_TEXT_ENCODING)
    return "utf-8" if code == 65001 else "cp1252"


def _full_name(record0: bytes, encoding: str) -> str | None:
    offset = _u32(record0, 16 + MOBI_FULL_NAME_OFFSET)
    length = _u32(record0, 16 + MOBI_FULL_NAME_LENGTH)
    if offset is None or length is None or length == 0 or length > 4096:
        return None
    if offset + length > len(record0):
        return None
    return clean_text(record0[offset : offset + length].decode(encoding, "replace"))


def _read_exth(record0: bytes, start: int) -> dict[int, list[bytes]]:
    out: dict[int, list[bytes]] = {}
    if start + 12 > len(record0) or record0[start : start + 4] != b"EXTH":
        return out
    count = struct.unpack_from(">I", record0, start + 8)[0]
    if count > 1024:
        return out
    pos = start + 12
    for _ in range(count):
        if pos + 8 > len(record0):
            break
        rec_type, rec_len = struct.unpack_from(">II", record0, pos)
        if rec_len < 8 or pos + rec_len > len(record0):
            break
        out.setdefault(rec_type, []).append(record0[pos + 8 : pos + rec_len])
        pos += rec_len
    return out


def _apply_exth(
    meta: ExtractedMetadata, exth: dict[int, list[bytes]], encoding: str
) -> None:
    def text(record_type: int) -> str | None:
        values = exth.get(record_type)
        if not values:
            return None
        return clean_text(values[0].decode(encoding, "replace"))

    updated_title = text(EXTH_UPDATED_TITLE)
    if updated_title and not looks_like_junk_title(updated_title):
        meta.title = updated_title

    for raw in exth.get(EXTH_AUTHOR, []):
        meta.authors.extend(split_authors(raw.decode(encoding, "replace")))

    meta.publisher = text(EXTH_PUBLISHER)
    meta.description = clean_description(text(EXTH_DESCRIPTION))
    meta.isbn = normalize_isbn(text(EXTH_ISBN))
    meta.published_year = parse_year(text(EXTH_PUBLISHED))
    meta.language = text(EXTH_LANGUAGE)


def _find_cover(
    data: bytes,
    records: list[int],
    exth: dict[int, list[bytes]],
    first_image: int | None,
) -> bytes | None:
    """Prefer the declared cover record; fall back to the first real image.

    The declared offset is relative to the first image record, and both numbers
    are wrong often enough that everything is validated against image magic
    bytes before being accepted.
    """
    candidates: list[int] = []
    cover_values = exth.get(EXTH_COVER_OFFSET)
    if cover_values and first_image is not None:
        try:
            offset = struct.unpack(">I", cover_values[0][:4])[0]
            candidates.append(first_image + offset)
        except struct.error:
            pass

    if first_image is not None and 0 < first_image < len(records) - 1:
        candidates.extend(range(first_image, min(first_image + 12, len(records) - 1)))

    for index in candidates:
        blob = _record(data, records, index)
        if blob and len(blob) >= MIN_COVER_BYTES and looks_like_image(blob):
            return blob
    return None


def _record(data: bytes, records: list[int], index: int) -> bytes | None:
    if index < 0 or index >= len(records) - 1:
        return None
    return data[records[index] : records[index + 1]]


def _u32(buffer: bytes, offset: int) -> int | None:
    if offset + 4 > len(buffer):
        return None
    return struct.unpack_from(">I", buffer, offset)[0]
