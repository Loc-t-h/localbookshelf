"""CBZ and CBR: a cover is the first page, metadata is ComicInfo.xml if we're lucky."""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from .base import (
    ExtractedMetadata,
    clean_description,
    clean_text,
    parse_year,
    split_authors,
)

PAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")


ZIP_MAGIC = b"PK\x03\x04"
RAR_MAGIC = b"Rar!\x1a\x07"


def extract(path: Path) -> ExtractedMetadata:
    meta = ExtractedMetadata()
    try:
        _read_archive(path, meta)
    except Exception as exc:
        meta.error = f"unreadable comic archive: {exc}"
    if not meta.title:
        meta.title = None  # let the filename supply it
    return meta


def _read_archive(path: Path, meta: ExtractedMetadata) -> None:
    """Dispatch on the file's contents, not its extension.

    Comics are routinely mislabelled -- a .cbr that is really a zip is common
    enough that trusting the extension fails on real libraries. The extension
    is only a fallback for a file we cannot sniff.
    """
    try:
        with path.open("rb") as handle:
            magic = handle.read(8)
    except OSError as exc:
        meta.error = f"unreadable file: {exc}"
        return

    if magic.startswith(ZIP_MAGIC):
        _extract_zip(path, meta)
    elif magic.startswith(RAR_MAGIC):
        _extract_rar(path, meta)
    elif path.suffix.lower() == ".cbz":
        _extract_zip(path, meta)
    else:
        _extract_rar(path, meta)


def _extract_zip(path: Path, meta: ExtractedMetadata) -> None:
    with zipfile.ZipFile(path) as archive:
        names = [n for n in archive.namelist() if not n.endswith("/")]
        if not names:
            meta.error = "comic archive is empty"
            return
        _read_comic_info(_maybe_read(archive, names, "comicinfo.xml"), meta)
        cover_name = _first_page(names)
        if cover_name:
            meta.cover_bytes = archive.read(cover_name)


def _extract_rar(path: Path, meta: ExtractedMetadata) -> None:
    try:
        import rarfile
    except ImportError:
        meta.error = "rarfile not installed"
        return
    try:
        with rarfile.RarFile(path) as archive:
            names = [i.filename for i in archive.infolist() if not i.is_dir()]
            if not names:
                # rarfile opens a corrupt archive without complaining and just
                # returns nothing, so silence here means damage, not emptiness.
                meta.error = "comic archive is empty or corrupt"
                return
            _read_comic_info(_maybe_read(archive, names, "comicinfo.xml"), meta)
            cover_name = _first_page(names)
            if cover_name:
                meta.cover_bytes = archive.read(cover_name)
    except Exception as exc:
        # Almost always a missing `unar`/`unrar` binary rather than a bad file.
        if "cannot find working tool" in str(exc).lower() or "unrar" in str(exc).lower():
            meta.error = "CBR needs the `unar` binary (brew install unar / apt install unar)"
        else:
            meta.error = f"unreadable cbr: {exc}"


def _maybe_read(archive, names: list[str], target: str) -> bytes | None:
    for name in names:
        if name.lower().rsplit("/", 1)[-1] == target:
            try:
                return archive.read(name)
            except Exception:
                return None
    return None


def _first_page(names: list[str]) -> str | None:
    pages = [n for n in names if n.lower().endswith(PAGE_EXTENSIONS)]
    if not pages:
        return None
    # Exclude the usual non-page extras when there is anything else to pick.
    real = [n for n in pages if "__macosx" not in n.lower()]
    return sorted(real or pages, key=_natural_key)[0]


def _natural_key(name: str) -> list:
    """Sort 'page2.jpg' before 'page10.jpg'."""
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", name)
    ]


def _read_comic_info(data: bytes | None, meta: ExtractedMetadata) -> None:
    if not data:
        return
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError:
        return

    def field(tag: str) -> str | None:
        element = root.find(tag)
        return clean_text(element.text) if element is not None else None

    meta.series = field("Series")
    meta.title = field("Title")
    meta.publisher = field("Publisher")
    meta.description = clean_description(field("Summary"))
    meta.published_year = parse_year(field("Year"))
    meta.language = field("LanguageISO")

    number = field("Number")
    if number:
        try:
            meta.series_index = float(number)
        except ValueError:
            pass

    for tag in ("Writer", "Penciller", "Creator"):
        names = split_authors(field(tag))
        if names:
            meta.authors = names
            break

    # A comic with a series but no title reads better as "Series #3".
    if not meta.title and meta.series:
        if meta.series_index is not None:
            meta.title = f"{meta.series} #{_format_index(meta.series_index)}"
        else:
            meta.title = meta.series


def _format_index(value: float) -> str:
    return str(int(value)) if value == int(value) else str(value)
