"""The scan pipeline.

Runs in two phases, deliberately:

1. **Index** -- walk the roots, parse embedded metadata, extract covers, write
   rows. Purely local, fast, and parallel. The shelf fills up during this phase.
2. **Enrich** -- for the books that still have gaps, query Open Library one at a
   time under a rate limit. Slow, network-bound, and entirely optional.

Splitting them means you get a usable shelf in seconds instead of waiting on a
rate-limited network pass, and it means `--offline` is just "stop after phase 1".
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from . import covers, formats
from .config import IGNORED_DIR_NAMES, Config
from .db import Database
from .enrich import OpenLibraryClient, enrich
from .filename import guess as guess_from_filename
from .formats.base import ExtractedMetadata
from .identity import file_identity, normalize_text
from .library import BookFields, Library

log = logging.getLogger("localbookshelf.scan")

EXTRACT_WORKERS = min(8, (os.cpu_count() or 4))


@dataclass
class ScanState:
    running: bool = False
    phase: str = "idle"  # idle | walking | indexing | enriching | done
    run_id: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    total_files: int = 0
    processed: int = 0
    added: int = 0
    updated: int = 0
    skipped: int = 0
    removed: int = 0
    errors: int = 0
    enrich_total: int = 0
    enriched: int = 0
    current: str | None = None
    message: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def snapshot(self) -> dict:
        with self._lock:
            data = {
                k: v
                for k, v in self.__dict__.items()
                if not k.startswith("_")
            }
        total = data["total_files"] or 1
        data["percent"] = min(100, round(100 * data["processed"] / total))
        return data


class Scanner:
    def __init__(self, config: Config, db: Database, library: Library) -> None:
        self.config = config
        self.db = db
        self.library = library
        self.state = ScanState()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()

    # -- control -----------------------------------------------------------

    def start(self, *, force: bool = False, block: bool = False) -> bool:
        """Kick off a scan. Returns False if one is already running."""
        if self.state.running:
            return False
        self._cancel.clear()
        self._thread = threading.Thread(
            target=self._run, kwargs={"force": force}, daemon=True, name="scanner"
        )
        self._thread.start()
        if block:
            self._thread.join()
        return True

    def cancel(self) -> None:
        self._cancel.set()

    def wait(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    # -- the run -----------------------------------------------------------

    def _run(self, *, force: bool) -> None:
        state = self.state
        with state._lock:
            state.running = True
            state.phase = "walking"
            state.started_at = _now()
            state.finished_at = None
            state.total_files = state.processed = 0
            state.added = state.updated = state.skipped = state.removed = 0
            state.errors = state.enrich_total = state.enriched = 0
            state.message = None
            state.current = None

        roots = self.library.roots()
        run_id = self._begin_run()
        state.run_id = run_id

        try:
            if not roots:
                state.message = "No library folder configured."
                return

            paths = list(self._walk(roots))
            with state._lock:
                state.total_files = len(paths)
                state.phase = "indexing"

            self._index(paths, run_id, force=force)

            if not self._cancel.is_set():
                state.removed = self.library.delete_missing(run_id, roots)

            if not self.config.offline and not self._cancel.is_set():
                self._enrich_pass(force=force)

            state.phase = "done"
            state.message = self._summary()
        except Exception as exc:  # a scan must never take the server with it
            log.exception("scan failed")
            state.phase = "done"
            state.message = f"Scan failed: {exc}"
        finally:
            self._finish_run(run_id)
            with state._lock:
                state.running = False
                state.finished_at = _now()
                state.current = None

    def _summary(self) -> str:
        s = self.state
        if self._cancel.is_set():
            return "Scan cancelled."
        bits = [f"{s.added} added", f"{s.updated} updated", f"{s.skipped} unchanged"]
        if s.removed:
            bits.append(f"{s.removed} removed")
        if s.enriched:
            bits.append(f"{s.enriched} enriched")
        if s.errors:
            bits.append(f"{s.errors} with problems")
        return ", ".join(bits) + "."

    # -- phase 1: walk + index --------------------------------------------

    def _walk(self, roots: list[Path]) -> Iterator[Path]:
        seen: set[str] = set()
        for root in roots:
            root = root.expanduser().resolve()
            if not root.is_dir():
                log.warning("skipping missing root: %s", root)
                continue
            for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                dirnames[:] = [
                    d
                    for d in dirnames
                    if d not in IGNORED_DIR_NAMES and not d.startswith(".")
                ]
                for name in filenames:
                    if name.startswith("._") or name.startswith("."):
                        continue  # AppleDouble forks and dotfiles
                    path = Path(dirpath) / name
                    if formats.format_for_path(path) is None:
                        continue
                    key = normalize_text(str(path))
                    if key in seen:
                        continue
                    seen.add(key)
                    yield path

    def _index(self, paths: list[Path], run_id: int, *, force: bool) -> None:
        from concurrent.futures import ThreadPoolExecutor
        from functools import partial

        # Extraction is parallel (it is mostly file I/O and image decoding);
        # all database writes happen here, on one thread.
        extract = partial(self._extract_one, force=force)
        with ThreadPoolExecutor(max_workers=EXTRACT_WORKERS) as pool:
            for result in pool.map(extract, paths):
                if self._cancel.is_set():
                    break
                self._store(result, run_id)

    def _extract_one(self, path: Path, *, force: bool = False) -> dict | None:
        """Runs on a worker thread. Returns everything needed to write a row."""
        if self._cancel.is_set():
            return None
        try:
            stat = path.stat()
        except OSError as exc:
            log.warning("cannot stat %s: %s", path, exc)
            return None

        try:
            identity = file_identity(path, stat.st_size)
        except OSError as exc:
            log.warning("cannot read %s: %s", path, exc)
            return None

        existing = self.db.query_one(
            "SELECT id, path, mtime, file_size, has_cover, title, description, publisher,"
            "       published_year, isbn, openlibrary_id, match_source, match_confidence,"
            "       needs_review, candidates"
            " FROM books WHERE identity_hash = ?",
            (identity,),
        )
        unchanged = (
            existing
            and existing["mtime"] == stat.st_mtime
            and existing["file_size"] == stat.st_size
            and existing["path"] == str(path)
        )
        if unchanged and not force:
            return {"identity": identity, "path": path, "skip": True}

        meta = formats.extract(path)
        cover_source = None
        cached = covers.has_cover(self.config.covers_dir, identity)

        # A forced rescan re-extracts the cover, which is the only way to
        # replace a bad one already sitting in the cache.
        has_cover = cached and not force
        if meta.cover_bytes and not has_cover:
            if covers.store_cover(self.config.covers_dir, identity, meta.cover_bytes):
                has_cover = True
                cover_source = "file"
        if not has_cover and cached:
            # Forced, but this file has no cover of its own -- keep the one we
            # downloaded previously rather than blanking the card.
            has_cover = True
            cover_source = "cache"
        elif has_cover and cover_source is None:
            cover_source = "cache"
        meta.cover_bytes = None  # don't carry image bytes any further

        return {
            "identity": identity,
            "path": path,
            "stat": stat,
            "meta": meta,
            "has_cover": has_cover,
            "cover_source": cover_source,
            "moved": bool(existing) and existing["path"] != str(path),
            "previous": dict(existing) if existing else None,
        }

    def _store(self, result: dict | None, run_id: int) -> None:
        state = self.state
        if result is None:
            with state._lock:
                state.processed += 1
                state.errors += 1
            return

        if result.get("skip"):
            self.library.touch_seen(result["identity"], run_id)
            with state._lock:
                state.processed += 1
                state.skipped += 1
                state.current = result["path"].name
            return

        fields = self._merge(result)
        try:
            _, created = self.library.upsert(fields, run_id=run_id)
        except Exception:
            log.exception("failed to store %s", result["path"])
            with state._lock:
                state.processed += 1
                state.errors += 1
                state.current = result["path"].name
            return

        with state._lock:
            state.processed += 1
            state.current = result["path"].name
            if created:
                state.added += 1
            else:
                state.updated += 1
            if fields.scan_error:
                state.errors += 1

    def _merge(self, result: dict) -> BookFields:
        """Embedded metadata first, filename guesses only for the gaps."""
        path: Path = result["path"]
        meta: ExtractedMetadata = result["meta"]
        stat = result["stat"]
        guess = guess_from_filename(path)

        title = meta.title or guess.title or path.stem
        authors = meta.authors or (guess.authors or [])
        series = meta.series or guess.series
        series_index = meta.series_index if meta.series_index is not None else guess.series_index
        year = meta.published_year or guess.year

        source = "embedded" if meta.title else "filename"

        fields = BookFields(
            identity_hash=result["identity"],
            path=str(path),
            format=formats.format_for_path(path) or path.suffix.lstrip("."),
            file_size=stat.st_size,
            mtime=stat.st_mtime,
            title=normalize_text(title),
            authors=[normalize_text(a) for a in authors],
            description=meta.description,
            publisher=meta.publisher,
            published_year=year,
            isbn=meta.isbn,
            language=meta.language,
            series=series,
            series_index=series_index,
            match_source=source,
            cover_source=result.get("cover_source"),
            has_cover=result["has_cover"],
            drm=meta.drm,
            scan_error=meta.error,
        )
        return self._carry_forward(fields, result.get("previous"))

    def _carry_forward(self, fields: BookFields, previous: dict | None) -> BookFields:
        """Keep previously downloaded metadata that the file itself can't supply.

        Re-reading a file tells us nothing new about its description, publisher
        or Open Library identity, so a forced rescan must not blank them --
        especially in offline mode, where nothing would refill them.
        """
        if not previous or not previous.get("openlibrary_id"):
            return fields

        fields.openlibrary_id = previous["openlibrary_id"]
        fields.description = fields.description or previous["description"]
        fields.publisher = fields.publisher or previous["publisher"]
        fields.published_year = fields.published_year or previous["published_year"]
        fields.isbn = fields.isbn or previous["isbn"]

        # The file gave us no title of its own, so the matched one still stands.
        if fields.match_source == "filename" and previous["match_source"] in (
            "openlibrary",
            "manual",
        ):
            fields.title = previous["title"] or fields.title
            fields.match_source = previous["match_source"]
            fields.match_confidence = previous["match_confidence"]

        if not fields.authors:
            # An author the match supplied but the filename never had.
            fields.authors = [
                row["name"]
                for row in self.db.query(
                    "SELECT a.name FROM authors a"
                    " JOIN book_authors ba ON ba.author_id = a.id"
                    " WHERE ba.book_id = ? ORDER BY ba.position",
                    (previous["id"],),
                )
            ]
        return fields

    # -- phase 2: enrichment ----------------------------------------------

    def _enrich_pass(self, *, force: bool) -> None:
        state = self.state
        rows = self.db.query(
            "SELECT id, identity_hash, title, description, has_cover, isbn, openlibrary_id"
            " FROM books"
            # Normally: only books with gaps that we have not looked up before.
            # Forced: everything eligible, re-deriving from the cached responses.
            " WHERE (:force OR (enriched_at IS NULL AND"
            "        (description IS NULL OR has_cover = 0 OR openlibrary_id IS NULL)))"
            # A file we could not parse has no trustworthy title to search with,
            # and searching its filename anyway produces confident matches to
            # entirely unrelated books. Wrong metadata is worse than none.
            "   AND scan_error IS NULL"
            # Open Library indexes collected volumes, not single comic issues,
            # so a CBZ search reliably matches the wrong thing.
            "   AND format NOT IN ('cbz', 'cbr')"
            " ORDER BY added_at DESC",
            {"force": int(force)},
        )
        if not rows:
            return

        with state._lock:
            state.phase = "enriching"
            state.enrich_total = len(rows)
            state.enriched = 0

        client = OpenLibraryClient(self.db, offline=self.config.offline)
        try:
            for row in rows:
                if self._cancel.is_set():
                    break
                book = self.library.get_book(row["id"])
                if not book:
                    continue
                with state._lock:
                    state.current = book["display_title"]
                try:
                    self._enrich_book(client, book, refresh=force)
                except Exception:
                    log.exception("enrichment failed for %s", book["path"])
                with state._lock:
                    state.enriched += 1
        finally:
            client.close()

    def _enrich_book(
        self,
        client: OpenLibraryClient,
        book: dict,
        *,
        manual: bool = False,
        refresh: bool = False,
    ) -> bool:
        """Look one book up. Returns False if it was skipped as unsearchable.

        ``manual`` relaxes the comic rule -- if someone explicitly asks to look
        up a CBZ, let them -- but never the parse-error rule, because a file we
        could not read gives us nothing trustworthy to search with.

        ``refresh`` (a forced rescan, or the Refresh button) lets a freshly
        fetched description and publisher replace the stored ones instead of
        only filling gaps. Without it, a description saved before a parsing fix
        could never be corrected. Manual edits are unaffected either way: they
        live in the overrides table and are re-applied after this.
        """
        if book.get("scan_error"):
            return False
        if not manual and book["format"] in ("cbz", "cbr"):
            return False

        identity = book["identity_hash"]
        overrides = book["overrides"]
        want_cover = not book["has_cover"] and "cover" not in overrides

        result = enrich(
            client,
            title=book.get("title"),
            authors=book.get("authors") or [],
            isbn=book.get("isbn"),
            want_cover=want_cover,
        )

        fields = self._fields_from_row(book)
        fields.enriched = True

        if result.matched:
            self._apply_match(
                fields, result.matched, result.description, refresh=refresh or manual
            )
        if result.candidates:
            fields.candidates = [c.to_dict() for c in result.candidates]
            fields.needs_review = result.needs_review

        if result.cover_bytes and not fields.has_cover:
            if covers.store_cover(self.config.covers_dir, identity, result.cover_bytes):
                fields.has_cover = True
                fields.cover_source = "openlibrary"

        self.library.upsert(fields, run_id=self.state.run_id)
        return True

    def _apply_match(
        self, fields: BookFields, match, description: str | None, *, refresh: bool = False
    ) -> None:
        """Fill gaps only. Embedded metadata outranks a search result."""
        if match.title and (fields.match_source == "filename" or not fields.title):
            fields.title = match.title
        if not fields.authors and match.authors:
            fields.authors = [a for a in match.authors if a]
        if match.publisher and (refresh or not fields.publisher):
            fields.publisher = match.publisher
        if not fields.published_year:
            fields.published_year = match.year
        if description and (refresh or not fields.description):
            fields.description = description
        fields.openlibrary_id = match.openlibrary_id
        fields.match_confidence = match.confidence
        fields.match_source = "openlibrary"
        fields.needs_review = False
        fields.candidates = []

    def _fields_from_row(self, book: dict) -> BookFields:
        return BookFields(
            identity_hash=book["identity_hash"],
            path=book["path"],
            format=book["format"],
            file_size=book["file_size"],
            mtime=book["mtime"],
            title=book.get("title"),
            authors=list(book.get("authors") or []),
            description=book.get("description"),
            publisher=book.get("publisher"),
            published_year=book.get("published_year"),
            isbn=book.get("isbn"),
            language=book.get("language"),
            series=book.get("series_name"),
            series_index=book.get("series_index"),
            openlibrary_id=book.get("openlibrary_id"),
            match_source=book.get("match_source"),
            match_confidence=book.get("match_confidence"),
            needs_review=bool(book.get("needs_review")),
            candidates=book.get("candidates") or [],
            cover_source=book.get("cover_source"),
            has_cover=bool(book.get("has_cover")),
            drm=bool(book.get("drm")),
            scan_error=book.get("scan_error"),
        )

    # -- run bookkeeping ---------------------------------------------------

    def _begin_run(self) -> int:
        with self.db.write() as conn:
            cursor = conn.execute(
                "INSERT INTO scan_runs (started_at) VALUES (?)", (_now(),)
            )
            return cursor.lastrowid

    def _finish_run(self, run_id: int) -> None:
        s = self.state
        with self.db.write() as conn:
            conn.execute(
                "UPDATE scan_runs SET finished_at = ?, files_seen = ?, books_added = ?,"
                " books_updated = ?, books_removed = ?, errors = ? WHERE id = ?",
                (_now(), s.processed, s.added, s.updated, s.removed, s.errors, run_id),
            )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
