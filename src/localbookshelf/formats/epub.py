"""EPUB metadata and cover extraction.

Implemented directly on ``zipfile`` + ``ElementTree`` rather than through a
library: we only ever read the OPF package document and one image, so pulling in
a full EPUB parser would cost a dependency and a whole-book parse per file to do
strictly less than this.
"""

from __future__ import annotations

import posixpath
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from .base import (
    ExtractedMetadata,
    clean_description,
    clean_text,
    normalize_isbn,
    parse_year,
    split_authors,
)

DC_NS = "{http://purl.org/dc/elements/1.1/}"
OPF_NS = "{http://www.idpf.org/2007/opf}"
CONTAINER_NS = "{urn:oasis:names:tc:opendocument:xmlns:container}"

IMAGE_MIMES = ("image/jpeg", "image/png", "image/gif", "image/webp")


def extract(path: Path) -> ExtractedMetadata:
    meta = ExtractedMetadata()
    try:
        with zipfile.ZipFile(path) as archive:
            opf_path = _find_opf(archive)
            if not opf_path:
                meta.error = "no OPF package document"
                return meta
            root = ElementTree.fromstring(archive.read(opf_path))
            _read_metadata(root, meta)
            meta.cover_bytes = _read_cover(archive, root, opf_path)
    except (zipfile.BadZipFile, ElementTree.ParseError) as exc:
        meta.error = f"corrupt epub: {exc}"
    except (KeyError, OSError) as exc:
        meta.error = f"unreadable epub: {exc}"
    return meta


def _find_opf(archive: zipfile.ZipFile) -> str | None:
    try:
        container = ElementTree.fromstring(archive.read("META-INF/container.xml"))
        rootfile = container.find(f".//{CONTAINER_NS}rootfile")
        if rootfile is not None:
            full_path = rootfile.get("full-path")
            if full_path:
                return full_path
    except (KeyError, ElementTree.ParseError):
        pass
    # Some malformed files omit the container; fall back to any .opf present.
    for name in archive.namelist():
        if name.lower().endswith(".opf"):
            return name
    return None


def _read_metadata(root: ElementTree.Element, meta: ExtractedMetadata) -> None:
    metadata = root.find(f"{OPF_NS}metadata")
    if metadata is None:
        metadata = root  # be forgiving about missing wrappers

    meta.title = clean_text(_text(metadata.find(f"{DC_NS}title")))
    meta.description = clean_description(_text(metadata.find(f"{DC_NS}description")))
    meta.publisher = clean_text(_text(metadata.find(f"{DC_NS}publisher")))
    meta.language = clean_text(_text(metadata.find(f"{DC_NS}language")))
    meta.published_year = parse_year(_text(metadata.find(f"{DC_NS}date")))

    # Prefer creators explicitly marked as authors; fall back to all creators.
    authors: list[str] = []
    fallback: list[str] = []
    for element in metadata.findall(f"{DC_NS}creator"):
        name = clean_text(element.text)
        if not name:
            continue
        role = element.get(f"{OPF_NS}role") or element.get("role")
        (authors if role == "aut" else fallback).append(name)
    for name in authors or fallback:
        meta.authors.extend(split_authors(name))

    for element in metadata.findall(f"{DC_NS}identifier"):
        isbn = normalize_isbn(element.text)
        if isbn:
            meta.isbn = isbn
            break

    _read_series(metadata, meta)

    if not meta.published_year:
        for element in metadata.findall(f"{OPF_NS}meta"):
            if element.get("property") == "dcterms:issued":
                meta.published_year = parse_year(element.text)
                break


def _read_series(metadata: ElementTree.Element, meta: ExtractedMetadata) -> None:
    """Series lives in Calibre's EPUB2 meta tags or EPUB3 collections."""
    for element in metadata.findall(f"{OPF_NS}meta"):
        name = element.get("name")
        content = element.get("content")
        if name == "calibre:series":
            meta.series = clean_text(content)
        elif name == "calibre:series_index":
            meta.series_index = _to_float(content)

    if meta.series:
        return

    # EPUB3: <meta property="belongs-to-collection" id="c1">Name</meta>
    #        <meta refines="#c1" property="group-position">3</meta>
    for element in metadata.findall(f"{OPF_NS}meta"):
        if element.get("property") != "belongs-to-collection":
            continue
        meta.series = clean_text(element.text)
        collection_id = element.get("id")
        if collection_id:
            for refine in metadata.findall(f"{OPF_NS}meta"):
                if (
                    refine.get("refines") == f"#{collection_id}"
                    and refine.get("property") == "group-position"
                ):
                    meta.series_index = _to_float(refine.text)
        break


def _read_cover(
    archive: zipfile.ZipFile, root: ElementTree.Element, opf_path: str
) -> bytes | None:
    manifest = root.find(f"{OPF_NS}manifest")
    if manifest is None:
        return None
    items = manifest.findall(f"{OPF_NS}item")
    base = posixpath.dirname(opf_path)

    # 1. EPUB3 declares the cover outright.
    for item in items:
        if "cover-image" in (item.get("properties") or ""):
            data = _read_zip_entry(archive, base, item.get("href"))
            if data:
                return data

    # 2. EPUB2 points at a manifest id from <meta name="cover">.
    metadata = root.find(f"{OPF_NS}metadata")
    if metadata is not None:
        for element in metadata.findall(f"{OPF_NS}meta"):
            if element.get("name") != "cover":
                continue
            cover_id = element.get("content")
            for item in items:
                if item.get("id") == cover_id:
                    data = _read_zip_entry(archive, base, item.get("href"))
                    if data:
                        return data

    # 3. An image whose id or filename says "cover".
    for item in items:
        href = (item.get("href") or "").lower()
        ident = (item.get("id") or "").lower()
        if (item.get("media-type") or "") in IMAGE_MIMES and (
            "cover" in href or "cover" in ident
        ):
            data = _read_zip_entry(archive, base, item.get("href"))
            if data:
                return data

    # 4. Give up and take the first image in the manifest.
    for item in items:
        if (item.get("media-type") or "") in IMAGE_MIMES:
            data = _read_zip_entry(archive, base, item.get("href"))
            if data:
                return data
    return None


def _read_zip_entry(archive: zipfile.ZipFile, base: str, href: str | None) -> bytes | None:
    if not href:
        return None
    from urllib.parse import unquote

    target = posixpath.normpath(posixpath.join(base, unquote(href)))
    for candidate in (target, target.lstrip("/"), href, unquote(href)):
        try:
            data = archive.read(candidate)
        except KeyError:
            continue
        if data:
            return data
    return None


def _text(element: ElementTree.Element | None) -> str | None:
    return element.text if element is not None else None


def _to_float(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
