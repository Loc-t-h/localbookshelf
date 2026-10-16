"""Shared vocabulary for the format parsers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..identity import normalize_text


@dataclass
class ExtractedMetadata:
    """What a parser managed to pull out of one file.

    Every field is optional. A parser that finds nothing returns an empty
    instance rather than raising -- a book with no metadata is a valid book with
    a placeholder cover, not an error.
    """

    title: str | None = None
    authors: list[str] = field(default_factory=list)
    description: str | None = None
    publisher: str | None = None
    published_year: int | None = None
    isbn: str | None = None
    language: str | None = None
    series: str | None = None
    series_index: float | None = None

    cover_bytes: bytes | None = None
    drm: bool = False
    error: str | None = None


ISBN_RE = re.compile(r"(?:97[89])?\d{9}[\dXx]")

# Producer/creator junk that PDF and DOCX exporters leave in the title field.
JUNK_TITLE_MARKERS = (
    "microsoft word",
    "microsoft powerpoint",
    "untitled",
    "unknown",
    "pdfsam",
    "calibre",
    "adobe acrobat",
    "printed by",
    "-  -",
    "output.tex",
    "thesis.dvi",
)


# Inline markup that leaks into metadata fields from publisher toolchains, as
# in a PDF whose title is "<em>Emergent Cities</em>". Only a known tag list, so
# a title that genuinely contains an angle bracket survives.
INLINE_TAG_RE = re.compile(
    r"</?(?:em|i|b|strong|span|p|br|div|u|sub|sup|small|font)\b[^>]*>",
    re.I,
)


def clean_text(value: str | None, *, limit: int = 20_000) -> str | None:
    """Collapse whitespace, normalize unicode, strip stray markup, drop empties."""
    if not value:
        return None
    text = normalize_text(str(value)).replace("\x00", "")
    text = INLINE_TAG_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None
    return text[:limit]


def clean_description(value: str | None) -> str | None:
    """Flatten a description to plain paragraphs.

    Embedded descriptions are usually HTML fragments; Open Library's are
    hand-edited Markdown, complete with reference-style links, blockquoted
    review excerpts and footnote definitions. Neither renders as-is in a panel
    that only emits <p> tags, so both get reduced to text here.
    """
    if not value:
        return None
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", str(value))
    text = re.sub(r"<[^>]+>", "", text)
    text = (
        text.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&nbsp;", " ")
    )

    # Markdown, in the order that avoids one pattern eating another.
    text = re.sub(r"^[ \t]*\[[^\]]+\]:[ \t]*\S+.*$", "", text, flags=re.M)  # [1]: http://…
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)                        # images
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)                    # [text](url)
    text = re.sub(r"\[([^\]]*)\]\[[^\]]*\]", r"\1", text)                   # [text][1]
    text = re.sub(r"^[ \t]*>[ \t]?", "", text, flags=re.M)                  # blockquotes
    text = re.sub(r"^[ \t]*(?:[-*_][ \t]*){3,}$", "", text, flags=re.M)     # rules
    text = re.sub(r"^#{1,6}[ \t]*", "", text, flags=re.M)                   # headings
    text = re.sub(r"\*\*([^*]+)\*\*|__([^_]+)__", r"\1\2", text)            # bold

    text = normalize_text(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:20_000] or None


# A short tag some tools glue onto the end of an identifier, as in
# "0123456789 (N)" or "9780123456789 [ebook]".
TRAILING_TAG_RE = re.compile(r"\s*[(\[][^)\]]{0,12}[)\]]\s*$")

# Amazon's ASIN: a capital B then nine more uppercase alphanumerics. Requiring
# digits as well keeps real ten-letter titles ("BLACKWATER") out of it.
ASIN_RE = re.compile(r"B[0-9A-Z]{9}")


def _is_bare_identifier(text: str) -> bool:
    """True if the whole string is an ISBN, an ASIN or a long digit run."""
    if not text:
        return False
    if ASIN_RE.fullmatch(text) and sum(c.isdigit() for c in text) >= 2:
        return True
    compact = re.sub(r"[-\s]", "", text)
    if compact[-1:] in {"x", "X"}:  # an ISBN-10 check digit
        compact = compact[:-1]
    # Nine digits is past every plausible numeric title ("1984", "2666",
    # "1939-1945") and still catches both ISBN-10 and ISBN-13.
    return compact.isdigit() and len(compact) >= 9


# "[Harrow Cycle 02] • Second Light" -- a leading bracketed series tag, with
# whatever separator the exporter chose after it.
SERIES_PREFIX_RE = re.compile(r"^\[(?P<tag>[^\]]{1,60})\]\s*(?:[•·|:\u2013\u2014-]\s*)?")


def strip_series_prefix(title: str | None, series: str | None = None) -> str | None:
    """Drop a bracketed series tag that only repeats the series fields.

    The shelf shows series and index on their own line, so a title carrying
    them again is noise. Stripping is deliberately narrow: the tag has to end
    in a number or name the series we already found, so a bracket that is part
    of the actual title survives.
    """
    if not title:
        return title
    match = SERIES_PREFIX_RE.match(title)
    if not match:
        return title

    tag = match.group("tag").strip()
    names_the_series = bool(series) and tag.lower().startswith(series.strip().lower())
    if not (names_the_series or re.search(r"\d\s*$", tag)):
        return title

    remainder = title[match.end():].strip()
    # Never strip our way down to nothing: a title that was *only* the tag is
    # better shown as it came than blanked.
    return remainder if len(remainder) >= 2 else title


def looks_like_junk_title(title: str | None) -> bool:
    """True if a title field is really an artifact of whatever produced the file.

    PDF metadata is the main offender: it routinely carries the source document
    path or the exporter's name instead of a title.

    Note that a title merely *matching* the filename is not junk. It is usually
    correct, and rejecting it costs us a real title and demotes the metadata to
    "filename-derived", which then invites an online search to overwrite a
    perfectly good value with a differently-punctuated one.
    """
    if not title:
        return True
    low = title.strip().lower()
    if len(low) < 2:
        return True
    if any(marker in low for marker in JUNK_TITLE_MARKERS):
        return True
    if low.endswith((".pdf", ".doc", ".docx", ".epub", ".indd", ".tex", ".qxd")):
        return True
    # Bare hex/uuid-ish identifiers.
    if re.fullmatch(r"[0-9a-f\-]{12,}", low):
        return True
    # An identifier sitting where the title should be. Calibre and some shop
    # exports write the ISBN or ASIN into the title field, and such a "title"
    # outranks the filename -- which is usually where the real one survives --
    # so it is genuinely worse than having no title at all.
    if _is_bare_identifier(TRAILING_TAG_RE.sub("", title.strip()).strip()):
        return True
    return False


def parse_year(value: str | None) -> int | None:
    """Pull a plausible publication year out of a free-form date string."""
    if not value:
        return None
    match = re.search(r"(1[0-9]{3}|20[0-9]{2})", str(value))
    if not match:
        return None
    year = int(match.group(1))
    return year if 1400 <= year <= 2100 else None


def normalize_isbn(value: str | None) -> str | None:
    """Extract a bare ISBN-10/13 from strings like 'urn:isbn:978-0-575-08160-1'."""
    if not value:
        return None
    condensed = re.sub(r"[\s\-]", "", str(value))
    match = ISBN_RE.search(condensed)
    if not match:
        return None
    isbn = match.group(0).upper()
    return isbn if len(isbn) in (10, 13) else None


def split_authors(value: str | None) -> list[str]:
    """Split a combined author string into individual names.

    Deliberately conservative: 'Simmons, Dan' must not become two authors, so we
    only split on separators that are unambiguous in practice.
    """
    if not value:
        return []
    text = clean_text(value)
    if not text:
        return []
    parts = re.split(r"\s*(?:;|&|\band\b|\|)\s*", text)
    if len(parts) == 1 and text.count(",") >= 2:
        # "A, B, C" is a list; "Simmons, Dan" is one person inverted.
        parts = [p.strip() for p in text.split(",")]
    out: list[str] = []
    for part in parts:
        name = clean_text(part)
        if name and len(name) > 1 and name.lower() not in {"unknown", "anonymous", "n/a"}:
            out.append(name)
    return out[:12]


IMAGE_MAGIC = (
    (b"\xff\xd8\xff", "jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
    (b"RIFF", "webp"),
)


def looks_like_image(data: bytes) -> bool:
    return any(data.startswith(magic) for magic, _ in IMAGE_MAGIC)
