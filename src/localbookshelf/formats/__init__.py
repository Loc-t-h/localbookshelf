"""Format dispatch.

Every parser takes a path and returns an :class:`ExtractedMetadata`. Parsers do
not raise: an unreadable file produces a record with ``error`` set, which the
UI surfaces as a badge on an otherwise normal card.
"""

from __future__ import annotations

from pathlib import Path

from ..config import SUPPORTED_EXTENSIONS
from . import comic, epub, mobi, pdf
from .base import ExtractedMetadata

__all__ = ["ExtractedMetadata", "extract", "format_for_path"]


def format_for_path(path: Path) -> str | None:
    return SUPPORTED_EXTENSIONS.get(path.suffix.lower())


def extract(path: Path) -> ExtractedMetadata:
    kind = format_for_path(path)
    try:
        if kind == "epub":
            return epub.extract(path)
        if kind == "pdf":
            return pdf.extract(path)
        if kind in ("cbz", "cbr"):
            return comic.extract(path)
        if kind == "mobi":
            return mobi.extract(path)
    except Exception as exc:  # a parser bug must not stop a scan
        return ExtractedMetadata(error=f"{kind} parser failed: {exc!r}")
    return ExtractedMetadata(error=f"unsupported extension: {path.suffix}")
