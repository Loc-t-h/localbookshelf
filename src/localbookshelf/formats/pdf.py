"""PDF metadata, and a cover rendered from page one.

PDF metadata is the least trustworthy of any format we handle -- exporters
routinely leave the source document's filename, the typesetting program, or
nothing at all in the title field -- so most of the work here is deciding what
to *reject* and hand back to filename parsing and Open Library instead.
"""

from __future__ import annotations

from pathlib import Path

from .base import (
    ExtractedMetadata,
    clean_description,
    clean_text,
    looks_like_junk_title,
    parse_year,
    split_authors,
)

COVER_ZOOM = 1.6  # ~115 DPI; enough for a 600px-tall cover on most page sizes


def extract(path: Path, *, render_cover: bool = True) -> ExtractedMetadata:
    meta = ExtractedMetadata()
    try:
        import pymupdf as fitz
    except ImportError:  # pragma: no cover - dependency is declared
        meta.error = "PyMuPDF not installed"
        return meta

    doc = None
    try:
        doc = fitz.open(path)
        if doc.needs_pass:
            meta.drm = True
            meta.error = "password protected"
            return meta

        info = doc.metadata or {}
        title = clean_text(info.get("title"))
        if not looks_like_junk_title(title):
            meta.title = title
        meta.authors = split_authors(clean_text(info.get("author")))
        meta.description = clean_description(info.get("subject"))
        meta.published_year = parse_year(
            info.get("creationDate") or info.get("modDate")
        )

        if render_cover and doc.page_count > 0:
            meta.cover_bytes = _render_first_page(doc)
    except Exception as exc:  # PyMuPDF raises a wide variety of errors
        meta.error = f"unreadable pdf: {exc}"
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass
    return meta


def _render_first_page(doc) -> bytes | None:
    import pymupdf as fitz

    try:
        page = doc.load_page(0)
        pixmap = page.get_pixmap(matrix=fitz.Matrix(COVER_ZOOM, COVER_ZOOM), alpha=False)
        return pixmap.tobytes("png")
    except Exception:
        return None
