"""SQLite storage.

The database is a cache, not the source of truth. Everything here except the
``overrides`` table can be reconstructed by rescanning the library folder, which
is why there is no migration machinery beyond a version stamp: if the schema
ever changes incompatibly, we drop and rescan.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    id              INTEGER PRIMARY KEY,
    identity_hash   TEXT    NOT NULL UNIQUE,
    path            TEXT    NOT NULL,
    format          TEXT    NOT NULL,
    file_size       INTEGER NOT NULL,
    mtime           REAL    NOT NULL,

    title           TEXT,
    sort_title      TEXT,
    description     TEXT,
    publisher       TEXT,
    published_year  INTEGER,
    isbn            TEXT,
    language        TEXT,
    series_id       INTEGER REFERENCES series(id),
    series_index    REAL,

    -- Denormalized from book_authors so the shelf can sort by author without
    -- a join. book_authors remains the source of truth for filtering.
    primary_author      TEXT,
    primary_author_sort TEXT,

    openlibrary_id  TEXT,
    match_source    TEXT,
    match_confidence REAL,
    needs_review    INTEGER NOT NULL DEFAULT 0,
    candidates      TEXT,

    cover_source    TEXT,
    has_cover       INTEGER NOT NULL DEFAULT 0,
    drm             INTEGER NOT NULL DEFAULT 0,
    scan_error      TEXT,

    enriched_at     TEXT,
    added_at        TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    seen_run        INTEGER
);

CREATE INDEX IF NOT EXISTS books_sort_title  ON books(sort_title);
CREATE INDEX IF NOT EXISTS books_added_at    ON books(added_at);
CREATE INDEX IF NOT EXISTS books_series      ON books(series_id, series_index);
CREATE INDEX IF NOT EXISTS books_path        ON books(path);
CREATE INDEX IF NOT EXISTS books_author_sort ON books(primary_author_sort);

CREATE TABLE IF NOT EXISTS authors (
    id        INTEGER PRIMARY KEY,
    name      TEXT NOT NULL UNIQUE,
    sort_name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS book_authors (
    book_id   INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    author_id INTEGER NOT NULL REFERENCES authors(id) ON DELETE CASCADE,
    position  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (book_id, author_id)
);

CREATE INDEX IF NOT EXISTS book_authors_author ON book_authors(author_id);

CREATE TABLE IF NOT EXISTS series (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

-- Manual edits. Keyed by content identity so they survive a moved file, a
-- dropped database, or a full rescan. Never written by an automated process.
CREATE TABLE IF NOT EXISTS overrides (
    identity_hash TEXT NOT NULL,
    field         TEXT NOT NULL,
    value         TEXT,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (identity_hash, field)
);

CREATE TABLE IF NOT EXISTS scan_runs (
    id            INTEGER PRIMARY KEY,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    files_seen    INTEGER NOT NULL DEFAULT 0,
    books_added   INTEGER NOT NULL DEFAULT 0,
    books_updated INTEGER NOT NULL DEFAULT 0,
    books_removed INTEGER NOT NULL DEFAULT 0,
    errors        INTEGER NOT NULL DEFAULT 0
);

-- Cached Open Library responses, so a rescan of an unchanged library makes
-- zero network requests.
CREATE TABLE IF NOT EXISTS http_cache (
    url        TEXT PRIMARY KEY,
    body       TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS books_fts USING fts5(
    title, authors, series, description,
    book_id UNINDEXED,
    tokenize = "unicode61 remove_diacritics 2"
);
"""


class Database:
    """Thread-local SQLite connections over one database file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._local = threading.local()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA foreign_keys = ON")
            self._local.conn = conn
        return conn

    def _initialize(self) -> None:
        conn = self.conn
        conn.executescript(SCHEMA)
        # v1 kept the library folders here. They are configuration now, read
        # from $LIBRARY_PATH, so the table is dead weight in older databases.
        conn.execute("DROP TABLE IF EXISTS roots")
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """Transaction scope. Commits on success, rolls back on exception."""
        conn = self.conn
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
