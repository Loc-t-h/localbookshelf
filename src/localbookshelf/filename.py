"""Guess title, author and series from a filename.

This is the fallback when a file carries no usable embedded metadata, which is
the common case for PDFs and comics. It is also what builds the query we send to
Open Library, so it pays for itself twice.

Deliberately a small pile of heuristics rather than anything clever: it only has
to be right often enough to make a search query work, and every field it
produces is overwritten by a confident online match.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .formats.base import normalize_isbn
from .identity import normalize_text

# Release-group noise, quality tags, and format markers.
NOISE_PATTERNS = [
    r"\(z-lib(?:rary)?[^)]*\)",
    r"\(libgen[^)]*\)",
    r"\(annas?[-\s]?archive[^)]*\)",
    r"\b(?:retail|epub|mobi|azw3?|pdf|cbz|cbr)\b",
    r"\bv\d+\.\d+\b",  # true version numbers only; 'v03' is a volume marker
    r"\b(?:\d+th|\d+rd|\d+nd|\d+st)\s+ed(?:ition)?\b",
    r"\b(?:unabridged|abridged|illustrated|annotated|revised)\b",
    r"\b(?:ocr|scan|scanned|dragged|calibre)\b",
    r"\[[^\]]*\]",
    r"\{[^}]*\}",
    r"_+",
]

# "Series 03 - Title", "Series #3 - Title", "(Series Book 3)"
SERIES_PATTERNS = [
    re.compile(r"^(?P<series>.+?)\s*[#]\s*(?P<index>\d+(?:\.\d+)?)\s*[-–—]\s*(?P<title>.+)$"),
    re.compile(r"^(?P<series>.+?)\s+v(?:ol)?\.?\s*(?P<index>\d{1,3})\s*[-–—]\s*(?P<title>.+)$", re.I),
    re.compile(r"^(?P<series>.+?)\s+(?P<index>\d{1,3}(?:\.\d+)?)\s*[-–—]\s*(?P<title>.+)$"),
    re.compile(r"^(?P<title>.+?)\s*[\(\[](?P<series>.+?)\s+(?:book|bk|vol|volume|part)\s*\.?\s*(?P<index>\d+(?:\.\d+)?)[\)\]]", re.I),
]

YEAR_RE = re.compile(r"[\(\[]?\b((?:1[89]|20)\d{2})\b[\)\]]?")

# An ISBN sitting in the filename, usually parenthesised as in
# "... - Mara Iwasaki (9781234567897)". Worth taking seriously: unlike a title
# guess, an ISBN is an exact key, so the lookup it enables cannot match the
# wrong book. Removing it also unblocks the author split, which otherwise sees
# "Mara Iwasaki (9781234567897)" and decides it is not a name.
ISBN_IN_NAME_RE = re.compile(
    r"[(\[]?\b(97[89][\d\-\s]{10,16}|\d[\d\-\s]{8,14}[\dXx])\b[)\]]?"
)

# A run of 2-4 capitalized words, which is what a name usually looks like.
AUTHOR_HINT_RE = re.compile(r"^[A-Z][\w'`\-\.]*(?:\s+[A-Z][\w'`\-\.]*){1,3}$")

# Words that appear in titles and effectively never inside a person's name.
# "The Great Cow Race" is shaped exactly like a name without this check.
TITLE_WORDS = {
    "the", "a", "an", "of", "and", "or", "in", "on", "at", "to", "for", "from",
    "with", "book", "vol", "volume", "part", "novel", "story", "tales", "war",
    "art", "guide", "handbook", "history",
}


@dataclass
class FilenameGuess:
    title: str | None = None
    authors: list[str] | None = None
    series: str | None = None
    series_index: float | None = None
    year: int | None = None
    isbn: str | None = None

    def __post_init__(self) -> None:
        if self.authors is None:
            self.authors = []


def guess(path: Path) -> FilenameGuess:
    stem = normalize_text(path.stem)
    result = FilenameGuess()

    cleaned = _strip_noise(stem)

    # Before the year, so a 13-digit ISBN starting 978... is not mined for one.
    isbn_match = ISBN_IN_NAME_RE.search(cleaned)
    if isbn_match:
        candidate = normalize_isbn(isbn_match.group(1))
        if candidate:
            result.isbn = candidate
            cleaned = cleaned[: isbn_match.start()] + " " + cleaned[isbn_match.end():]

    year_match = YEAR_RE.search(cleaned)
    if year_match:
        result.year = int(year_match.group(1))
        cleaned = YEAR_RE.sub(" ", cleaned, count=1)

    cleaned = _tidy(_normalize_separators(cleaned))
    if not cleaned:
        result.title = stem or path.name
        return result

    # Series first: "Bone #12 - The Great Cow Race" must not be read as
    # "author - title" on that same hyphen.
    series, index, remainder = _split_series(cleaned)
    result.series_index = index

    if series:
        # The series half may still carry the author: "Simmons - Hyperion 01".
        author_part, series = _split_author_title(series)
        result.series = _tidy(series)
        title_part = remainder
    else:
        author_part, title_part = _split_author_title(cleaned)

    if author_part:
        result.authors = [author_part]
    result.title = _tidy(title_part) or stem

    # A parent folder named after an author is a strong signal for loose files.
    if not result.authors:
        parent = normalize_text(path.parent.name)
        if parent and _is_name(parent):
            result.authors = [parent]

    return result


def _strip_noise(text: str) -> str:
    out = text
    for pattern in NOISE_PATTERNS:
        out = re.sub(pattern, " ", out, flags=re.I)
    return out


def _normalize_separators(text: str) -> str:
    """Turn dot- and underscore-separated filenames into spaced ones.

    Only when the name has no spaces of its own: otherwise this would destroy
    the periods in initials, turning 'Ursula K. Le Guin' into 'Ursula K Le Guin'.
    """
    out = text.replace("_", " ")
    if " " not in out.strip() and out.count(".") >= 2:
        out = out.replace(".", " ")
    return out


def _is_name(text: str) -> bool:
    """Does this look like a person's name rather than a title?"""
    if not AUTHOR_HINT_RE.match(text):
        return False
    words = text.split()
    if not 1 < len(words) <= 4:
        return False
    return not any(word.strip(".,'").lower() in TITLE_WORDS for word in words)


def _tidy(text: str | None) -> str | None:
    if not text:
        return None
    out = re.sub(r"[\s]+", " ", text)
    out = re.sub(r"\s*[-–—]\s*$", "", out)
    out = re.sub(r"^\s*[-–—]\s*", "", out)
    out = out.replace("()", "").replace("[]", "")
    out = re.sub(r"\s{2,}", " ", out).strip(" -–—,;")
    return out or None


def _split_author_title(text: str) -> tuple[str | None, str]:
    """Handle 'Author - Title' and 'Title - Author' and 'Author_-_Title'."""
    parts = [p.strip() for p in re.split(r"\s+[-–—]\s+", text) if p.strip()]
    if len(parts) < 2:
        return None, text

    left, right = parts[0], " - ".join(parts[1:])

    # "Simmons, Dan - Hyperion" -> inverted author on the left.
    if re.match(r"^[^,]+,\s*[^,]+$", left) and len(left.split()) <= 5:
        surname, given = [p.strip() for p in left.split(",", 1)]
        return f"{given} {surname}", right

    left_is_name, right_is_name = _is_name(left), _is_name(right)

    if left_is_name and not right_is_name:
        return left, right
    if right_is_name and not left_is_name:
        return right, left
    if left_is_name and right_is_name:
        # Both are name-shaped ("Dan Simmons - Hyperion Cantos"). Author-first
        # is overwhelmingly the more common filename convention.
        return left, right
    return None, text


def _split_series(text: str) -> tuple[str | None, float | None, str]:
    for pattern in SERIES_PATTERNS:
        match = pattern.match(text)
        if not match:
            continue
        series = _tidy(match.group("series"))
        title = _tidy(match.group("title")) or text
        try:
            index = float(match.group("index"))
        except (TypeError, ValueError):
            index = None
        if series and len(series) > 1:
            return series, index, title
    return None, None, text
