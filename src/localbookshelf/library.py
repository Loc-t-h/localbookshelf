"""Reads and writes over the book database.

Overrides are applied here, at write time, rather than when rendering: that way
a manually corrected title is the value that sorting, filtering and full-text
search all see.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .db import Database
from .identity import normalize_text

# Fields a user is allowed to override by hand.
OVERRIDABLE = {
    "title",
    "authors",
    "description",
    "publisher",
    "published_year",
    "isbn",
    "language",
    "series",
    "series_index",
}

# Each sort names the column the direction toggle applies to, plus fixed
# tiebreakers. `<col> IS NULL` sorts to 0/1, which pins books with no author,
# no series or no year to the bottom in *both* directions -- a shelf sorted by
# author should never open on a wall of "Unknown author".
SORTS = {
    "added": {
        "label": "Recently added",
        "default_dir": "desc",
        "expr": "b.added_at {d}, b.id {d}",
    },
    "title": {
        "label": "Title",
        "default_dir": "asc",
        "expr": "b.sort_title COLLATE NOCASE {d}, b.id ASC",
    },
    "author": {
        "label": "Author",
        "default_dir": "asc",
        "expr": "b.primary_author_sort IS NULL ASC,"
                " b.primary_author_sort COLLATE NOCASE {d},"
                " s.name COLLATE NOCASE ASC, b.series_index ASC,"
                " b.sort_title COLLATE NOCASE ASC",
    },
    "series": {
        "label": "Series",
        "default_dir": "asc",
        "expr": "b.series_id IS NULL ASC, s.name COLLATE NOCASE {d},"
                " b.series_index ASC, b.sort_title COLLATE NOCASE ASC",
    },
    "year": {
        "label": "Publication year",
        "default_dir": "desc",
        "expr": "b.published_year IS NULL ASC, b.published_year {d},"
                " b.sort_title COLLATE NOCASE ASC",
    },
    "modified": {
        "label": "File date",
        "default_dir": "desc",
        "expr": "b.mtime {d}, b.sort_title COLLATE NOCASE ASC",
    },
    "size": {
        "label": "File size",
        "default_dir": "desc",
        "expr": "b.file_size {d}, b.sort_title COLLATE NOCASE ASC",
    },
}
DEFAULT_SORT = "added"


def order_clause(sort: str, direction: str | None) -> tuple[str, str]:
    """Return (SQL ORDER BY body, resolved direction)."""
    spec = SORTS.get(sort) or SORTS[DEFAULT_SORT]
    if direction not in ("asc", "desc"):
        direction = spec["default_dir"]
    return spec["expr"].format(d=direction.upper()), direction

LEADING_ARTICLE = re.compile(r"^(the|a|an)\s+", re.I)


@dataclass
class BookFields:
    """The merged, final view of one book, ready to be written."""

    identity_hash: str
    path: str
    format: str
    file_size: int
    mtime: float

    title: str | None = None
    authors: list[str] = field(default_factory=list)
    description: str | None = None
    publisher: str | None = None
    published_year: int | None = None
    isbn: str | None = None
    language: str | None = None
    series: str | None = None
    series_index: float | None = None

    openlibrary_id: str | None = None
    match_source: str | None = None
    match_confidence: float | None = None
    needs_review: bool = False
    candidates: list[dict] = field(default_factory=list)

    cover_source: str | None = None
    has_cover: bool = False
    drm: bool = False
    scan_error: str | None = None
    enriched: bool = False


# The A-Z strip filters on whatever column the shelf is sorted by -- the only
# reading that matches what you are looking at. The four non-alphabetical sorts
# (date added, year, file date, size) get no strip, because "skip to S" is
# meaningless in them.
LETTER_COLUMNS = {
    "title": "b.sort_title",
    "author": "b.primary_author_sort",
    "series": "s.name",
}


def sort_title_for(title: str | None) -> str:
    return LEADING_ARTICLE.sub("", (title or "").strip()).lower()


# Surname particles. Without these, "Ursula K. Le Guin" files under G and
# "Ludwig van Beethoven" under B.
SURNAME_PARTICLES = {
    "de", "del", "dela", "della", "der", "den", "des", "di", "du", "da", "das",
    "dos", "van", "von", "vander", "ter", "ten", "le", "la", "el", "al", "bin",
    "ibn", "st", "san", "santa",
}


def author_sort_for(name: str) -> str:
    """'Dan Simmons' -> 'simmons, dan'. Already-inverted names are left alone."""
    cleaned = normalize_text(name).strip()
    if "," in cleaned:
        return cleaned.lower()
    parts = cleaned.split()
    if len(parts) < 2:
        return cleaned.lower()

    # Walk left from the last word while the preceding word is a particle, so
    # "Le Guin" and "van der Berg" stay whole.
    index = len(parts) - 1
    while index > 1 and parts[index - 1].lower().strip(".") in SURNAME_PARTICLES:
        index -= 1
    surname = " ".join(parts[index:])
    given = " ".join(parts[:index])
    return f"{surname}, {given}".lower()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Library:
    def __init__(self, db: Database, config: Config) -> None:
        self.db = db
        self.config = config

    # -- roots -------------------------------------------------------------

    def roots(self) -> list[Path]:
        """The configured library folders.

        Configuration, not state: these come from $LIBRARY_PATH or the command
        line and are never written to the database, so deleting the data
        directory costs you the cache and nothing else.
        """
        return sorted({root.resolve() for root in self.config.roots})

    # -- overrides ---------------------------------------------------------

    def overrides_for(self, identity: str) -> dict:
        rows = self.db.query(
            "SELECT field, value FROM overrides WHERE identity_hash = ?", (identity,)
        )
        out: dict = {}
        for row in rows:
            field_name, raw = row["field"], row["value"]
            if field_name not in OVERRIDABLE:
                continue
            out[field_name] = _decode_override(field_name, raw)
        return out

    def set_override(self, identity: str, field_name: str, value) -> None:
        if field_name not in OVERRIDABLE:
            raise ValueError(f"not an overridable field: {field_name}")
        encoded = _encode_override(field_name, value)
        with self.db.write() as conn:
            if encoded is None:
                conn.execute(
                    "DELETE FROM overrides WHERE identity_hash = ? AND field = ?",
                    (identity, field_name),
                )
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO overrides (identity_hash, field, value, updated_at)"
                    " VALUES (?, ?, ?, ?)",
                    (identity, field_name, encoded, _now()),
                )

    def apply_overrides(self, fields: BookFields) -> BookFields:
        for name, value in self.overrides_for(fields.identity_hash).items():
            setattr(fields, name, value)
        return fields

    def export_overrides(self) -> list[dict]:
        rows = self.db.query(
            "SELECT o.identity_hash, o.field, o.value, o.updated_at,"
            "       (SELECT path FROM books WHERE identity_hash = o.identity_hash) AS path"
            " FROM overrides o ORDER BY o.identity_hash, o.field"
        )
        return [dict(row) for row in rows]

    def import_overrides(self, records: list[dict]) -> int:
        count = 0
        with self.db.write() as conn:
            for record in records:
                identity = record.get("identity_hash")
                field_name = record.get("field")
                if not identity or field_name not in OVERRIDABLE:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO overrides (identity_hash, field, value, updated_at)"
                    " VALUES (?, ?, ?, ?)",
                    (identity, field_name, record.get("value"), record.get("updated_at") or _now()),
                )
                count += 1
        return count

    # -- writing books -----------------------------------------------------

    def upsert(self, fields: BookFields, *, run_id: int | None = None) -> tuple[int, bool]:
        """Insert or update one book. Returns (book_id, was_created)."""
        fields = self.apply_overrides(fields)
        existing = self.db.query_one(
            "SELECT id, added_at FROM books WHERE identity_hash = ?",
            (fields.identity_hash,),
        )
        now = _now()
        series_id = self._series_id(fields.series)
        primary = fields.authors[0] if fields.authors else None

        values = {
            "identity_hash": fields.identity_hash,
            "path": fields.path,
            "format": fields.format,
            "file_size": fields.file_size,
            "mtime": fields.mtime,
            "title": fields.title,
            "sort_title": sort_title_for(fields.title),
            "description": fields.description,
            "publisher": fields.publisher,
            "published_year": fields.published_year,
            "isbn": fields.isbn,
            "language": fields.language,
            "series_id": series_id,
            "series_index": fields.series_index,
            "primary_author": primary,
            "primary_author_sort": author_sort_for(primary) if primary else None,
            "openlibrary_id": fields.openlibrary_id,
            "match_source": fields.match_source,
            "match_confidence": fields.match_confidence,
            "needs_review": int(fields.needs_review),
            "candidates": json.dumps(fields.candidates) if fields.candidates else None,
            "cover_source": fields.cover_source,
            "has_cover": int(fields.has_cover),
            "drm": int(fields.drm),
            "scan_error": fields.scan_error,
            "enriched_at": now if fields.enriched else None,
            "updated_at": now,
            "seen_run": run_id,
        }

        with self.db.write() as conn:
            if existing:
                book_id = existing["id"]
                if not fields.enriched:
                    values.pop("enriched_at")  # don't clear a previous enrichment
                assignments = ", ".join(f"{k} = :{k}" for k in values)
                conn.execute(
                    f"UPDATE books SET {assignments} WHERE id = :id",
                    {**values, "id": book_id},
                )
                created = False
            else:
                values["added_at"] = now
                columns = ", ".join(values)
                placeholders = ", ".join(f":{k}" for k in values)
                cursor = conn.execute(
                    f"INSERT INTO books ({columns}) VALUES ({placeholders})", values
                )
                book_id = cursor.lastrowid
                created = True

            self._write_authors(conn, book_id, fields.authors)
            self._reindex(conn, book_id, fields)

        return book_id, created

    def touch_seen(self, identity: str, run_id: int) -> None:
        with self.db.write() as conn:
            conn.execute(
                "UPDATE books SET seen_run = ? WHERE identity_hash = ?", (run_id, identity)
            )

    def _series_id(self, name: str | None) -> int | None:
        name = (name or "").strip()
        if not name:
            return None
        row = self.db.query_one("SELECT id FROM series WHERE name = ?", (name,))
        if row:
            return row["id"]
        with self.db.write() as conn:
            cursor = conn.execute("INSERT INTO series (name) VALUES (?)", (name,))
            return cursor.lastrowid

    def _write_authors(self, conn, book_id: int, authors: list[str]) -> None:
        conn.execute("DELETE FROM book_authors WHERE book_id = ?", (book_id,))
        for position, name in enumerate(authors[:12]):
            name = normalize_text(name).strip()
            if not name:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO authors (name, sort_name) VALUES (?, ?)",
                (name, author_sort_for(name)),
            )
            row = conn.execute("SELECT id FROM authors WHERE name = ?", (name,)).fetchone()
            if row:
                conn.execute(
                    "INSERT OR IGNORE INTO book_authors (book_id, author_id, position)"
                    " VALUES (?, ?, ?)",
                    (book_id, row["id"], position),
                )

    def _reindex(self, conn, book_id: int, fields: BookFields) -> None:
        conn.execute("DELETE FROM books_fts WHERE book_id = ?", (book_id,))
        conn.execute(
            "INSERT INTO books_fts (title, authors, series, description, book_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                fields.title or Path(fields.path).stem,
                " ".join(fields.authors),
                fields.series or "",
                (fields.description or "")[:4000],
                book_id,
            ),
        )

    def delete_missing(self, run_id: int, roots: list[Path]) -> int:
        """Remove books whose files were not seen by the given scan run."""
        if not roots:
            return 0
        rows = self.db.query(
            "SELECT id, identity_hash, path FROM books WHERE seen_run IS NOT ? OR seen_run IS NULL",
            (run_id,),
        )
        prefixes = [str(root.resolve()) for root in roots]
        removed = 0
        with self.db.write() as conn:
            for row in rows:
                # Only forget books under a root we actually just scanned.
                if not any(row["path"].startswith(prefix) for prefix in prefixes):
                    continue
                if Path(row["path"]).exists():
                    continue
                conn.execute("DELETE FROM books_fts WHERE book_id = ?", (row["id"],))
                conn.execute("DELETE FROM books WHERE id = ?", (row["id"],))
                removed += 1
        return removed

    # -- reading -----------------------------------------------------------

    def list_books(
        self,
        *,
        query: str | None = None,
        formats: list[str] | None = None,
        author_id: int | None = None,
        series_id: int | None = None,
        needs_review: bool = False,
        sort: str = DEFAULT_SORT,
        direction: str | None = None,
        letter: str | None = None,
        limit: int = 120,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        joins, where, params = self._filters(
            query=query, formats=formats, author_id=author_id,
            series_id=series_id, needs_review=needs_review,
            sort=sort, letter=letter,
        )
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        order, _ = order_clause(sort, direction)
        # Relevance leads only while the shelf is on its default sort. Picking
        # "Title" during a search should actually sort by title.
        if query and sort == DEFAULT_SORT:
            order = "f.rank, " + order

        total_row = self.db.query_one(
            f"SELECT COUNT(*) AS n FROM books b{joins}{clause}", tuple(params)
        )
        total = total_row["n"] if total_row else 0

        rows = self.db.query(
            f"SELECT b.*, s.name AS series_name FROM books b{joins}{clause}"
            f" ORDER BY {order} LIMIT ? OFFSET ?",
            tuple(params) + (limit, offset),
        )
        return [self._row_to_book(row) for row in rows], total

    def _filters(
        self,
        *,
        query: str | None,
        formats: list[str] | None,
        author_id: int | None,
        series_id: int | None,
        needs_review: bool,
        sort: str,
        letter: str | None,
    ) -> tuple[str, list[str], list]:
        """Joins, WHERE conditions and params shared by list_books and initials.

        One builder for both, so the counts behind the A-Z strip cannot drift
        from the shelf the strip filters.
        """
        where: list[str] = []
        params: list = []
        joins = " LEFT JOIN series s ON s.id = b.series_id"

        if query:
            joins += " JOIN books_fts f ON f.book_id = b.id"
            where.append("books_fts MATCH ?")
            params.append(_fts_query(query))
        if formats:
            where.append(f"b.format IN ({','.join('?' * len(formats))})")
            params.extend(formats)
        if author_id:
            where.append("b.id IN (SELECT book_id FROM book_authors WHERE author_id = ?)")
            params.append(author_id)
        if series_id:
            where.append("b.series_id = ?")
            params.append(series_id)
        if needs_review:
            where.append("b.needs_review = 1")

        column = LETTER_COLUMNS.get(sort)
        if letter and column:
            initial = f"UPPER(SUBSTR({column}, 1, 1))"
            if letter == "#":
                # Digits and symbols. A NULL sort value makes this NULL rather
                # than true, so books with no author or series stay out of it.
                where.append(f"{initial} NOT BETWEEN 'A' AND 'Z'")
            else:
                where.append(f"{initial} = ?")
                params.append(letter[:1].upper())

        return joins, where, params

    def initials(
        self,
        *,
        query: str | None = None,
        formats: list[str] | None = None,
        author_id: int | None = None,
        series_id: int | None = None,
        needs_review: bool = False,
        sort: str = DEFAULT_SORT,
        direction: str | None = None,
    ) -> dict[str, int]:
        """How many books sit under each initial, under the current filters.

        The strip always renders the full alphabet so it keeps one stable shape
        instead of reflowing while you type; this decides which letters are
        clickable. Empty dict for a sort that is not alphabetical.
        """
        column = LETTER_COLUMNS.get(sort)
        if not column:
            return {}

        joins, where, params = self._filters(
            query=query, formats=formats, author_id=author_id,
            series_id=series_id, needs_review=needs_review,
            sort=sort, letter=None,
        )
        where.append(f"{column} IS NOT NULL AND {column} <> ''")
        clause = " WHERE " + " AND ".join(where)
        initial = f"UPPER(SUBSTR({column}, 1, 1))"
        rows = self.db.query(
            f"SELECT CASE WHEN {initial} BETWEEN 'A' AND 'Z' THEN {initial}"
            f" ELSE '#' END AS ch, COUNT(*) AS n"
            f" FROM books b{joins}{clause} GROUP BY ch",
            tuple(params),
        )
        return {row["ch"]: row["n"] for row in rows if row["ch"]}

    def get_book(self, book_id: int) -> dict | None:
        row = self.db.query_one(
            "SELECT b.*, s.name AS series_name FROM books b"
            " LEFT JOIN series s ON s.id = b.series_id WHERE b.id = ?",
            (book_id,),
        )
        return self._row_to_book(row) if row else None

    def get_book_by_identity(self, identity: str) -> dict | None:
        row = self.db.query_one(
            "SELECT b.*, s.name AS series_name FROM books b"
            " LEFT JOIN series s ON s.id = b.series_id WHERE b.identity_hash = ?",
            (identity,),
        )
        return self._row_to_book(row) if row else None

    def _row_to_book(self, row) -> dict:
        book = dict(row)
        book["authors"] = [
            r["name"]
            for r in self.db.query(
                "SELECT a.name FROM authors a JOIN book_authors ba ON ba.author_id = a.id"
                " WHERE ba.book_id = ? ORDER BY ba.position",
                (row["id"],),
            )
        ]
        book["candidates"] = json.loads(row["candidates"]) if row["candidates"] else []
        book["overrides"] = self.overrides_for(row["identity_hash"])
        book["display_title"] = book.get("title") or Path(book["path"]).stem
        book["exists"] = Path(book["path"]).exists()
        return book

    def authors(self, limit: int = 400) -> list[dict]:
        return [
            dict(row)
            for row in self.db.query(
                "SELECT a.id, a.name, COUNT(ba.book_id) AS n FROM authors a"
                " JOIN book_authors ba ON ba.author_id = a.id"
                " GROUP BY a.id ORDER BY n DESC, a.sort_name LIMIT ?",
                (limit,),
            )
        ]

    def series(self, limit: int = 400) -> list[dict]:
        return [
            dict(row)
            for row in self.db.query(
                "SELECT s.id, s.name, COUNT(b.id) AS n FROM series s"
                " JOIN books b ON b.series_id = s.id"
                " GROUP BY s.id ORDER BY n DESC, s.name LIMIT ?",
                (limit,),
            )
        ]

    def stats(self) -> dict:
        row = self.db.query_one(
            "SELECT COUNT(*) AS books, COALESCE(SUM(file_size), 0) AS bytes,"
            " SUM(needs_review) AS review, SUM(scan_error IS NOT NULL) AS errors,"
            " SUM(has_cover = 0) AS no_cover FROM books"
        )
        formats = self.db.query(
            "SELECT format, COUNT(*) AS n FROM books GROUP BY format ORDER BY n DESC"
        )
        return {
            "books": row["books"] if row else 0,
            "bytes": row["bytes"] if row else 0,
            "needs_review": (row["review"] or 0) if row else 0,
            "errors": (row["errors"] or 0) if row else 0,
            "no_cover": (row["no_cover"] or 0) if row else 0,
            "formats": [dict(f) for f in formats],
        }


def _fts_query(raw: str) -> str:
    """Turn user input into a safe FTS5 prefix query."""
    terms = re.findall(r"[\w']+", raw, flags=re.UNICODE)
    if not terms:
        return '""'
    return " ".join(f'"{term}"*' for term in terms[:8])


def _encode_override(field_name: str, value):
    if value is None or value == "" or value == []:
        return None
    if field_name == "authors":
        if isinstance(value, str):
            value = [v.strip() for v in value.split(",") if v.strip()]
        return json.dumps(value)
    return str(value)


def _decode_override(field_name: str, raw):
    if raw is None:
        return None
    if field_name == "authors":
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return [raw]
    if field_name == "published_year":
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    if field_name == "series_index":
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
    return raw
