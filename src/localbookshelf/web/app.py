"""The HTTP layer.

Server-rendered HTML with a small amount of JavaScript for the detail panel and
live search. No build step, no SPA -- the UI is a grid, a search box and a panel.
"""

from __future__ import annotations

import hashlib
import html
import mimetypes
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__
from ..config import Config
from ..covers import cover_paths
from ..db import Database
from ..library import DEFAULT_SORT, SORTS, Library, initial_for, order_clause
from ..scanner import Scanner

HERE = Path(__file__).parent

def _asset_stamp() -> str:
    """Short digest of the static files, for cache-busting their URLs."""
    newest = 0.0
    for path in sorted((HERE / "static").rglob("*")):
        if path.is_file():
            newest = max(newest, path.stat().st_mtime)
    return f"{__version__}-{int(newest)}"


# "#" collects digits and symbols, and leads because that is where they sort.
ALPHABET = ["#"] + [chr(c) for c in range(ord("A"), ord("Z") + 1)]
# Books per page, and so per auto-load step as you scroll. 48 divides evenly by
# 2, 3, 4, 6 and 8, which is the range of column counts the shelf grid settles
# on between phone and desktop widths, so a page tends to end on a full row.
PAGE_SIZE = 48

DOWNLOAD_MIMES = {
    "epub": "application/epub+zip",
    "pdf": "application/pdf",
    "cbz": "application/vnd.comicbook+zip",
    "cbr": "application/vnd.comicbook-rar",
    "mobi": "application/x-mobipocket-ebook",
}

FORMAT_LABELS = {
    "epub": "EPUB",
    "pdf": "PDF",
    "cbz": "CBZ",
    "cbr": "CBR",
    "mobi": "MOBI",
}


def create_app(config: Config, db: Database, library: Library, scanner: Scanner) -> FastAPI:
    app = FastAPI(title="LocalBookshelf", docs_url=None, redoc_url=None)
    app.state.config = config
    app.state.db = db
    app.state.library = library
    app.state.scanner = scanner

    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters["filesize"] = _filesize
    templates.env.filters["format_label"] = lambda f: FORMAT_LABELS.get(f, (f or "?").upper())
    templates.env.filters["series_index"] = _series_index
    templates.env.filters["initial"] = initial_for
    # Stamped onto the static URLs so a browser cannot keep serving the previous
    # CSS or JavaScript against new markup. Derived from the files themselves,
    # not from __version__: during development the version rarely moves while
    # the assets change constantly, and a stamp that does not change is a cache
    # buster that does not bust. In a built image the mtimes are fixed, so the
    # value is stable across restarts and still caches properly.
    templates.env.globals["version"] = _asset_stamp()

    def _query_args(request: Request) -> dict:
        params = request.query_params
        formats = [f for f in params.getlist("format") if f in FORMAT_LABELS]
        sort = params.get("sort") if params.get("sort") in SORTS else DEFAULT_SORT
        _, direction = order_clause(sort, params.get("dir"))
        return {
            "query": (params.get("q") or "").strip() or None,
            "formats": formats or None,
            "author_id": _as_int(params.get("author")),
            "series_id": _as_int(params.get("series")),
            "needs_review": params.get("review") == "1",
            "sort": sort,
            "direction": direction,
        }

    # -- pages -------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def shelf(request: Request, page: int = Query(1, ge=1)):
        args = _query_args(request)
        books, total = library.list_books(
            **args, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE
        )
        return templates.TemplateResponse(
            request,
            "shelf.html",
            {
                "books": books,
                "total": total,
                "page": page,
                "page_size": PAGE_SIZE,
                "has_more": page * PAGE_SIZE < total,
                "args": args,
                "raw_query": request.query_params,
                "alphabet": ALPHABET,
                "initials": library.initials(**args),
                # Jumping to a letter only makes sense while the shelf is in
                # alphabetical order, so the strip appears on the title sort
                # and nowhere else.
                "show_letters": args["sort"] == "title",
                "authors": library.authors(limit=200),
                "series": library.series(limit=200),
                "stats": library.stats(),
                "roots": [str(r) for r in library.roots()],
                "formats": FORMAT_LABELS,
                "sorts": {key: spec["label"] for key, spec in SORTS.items()},
            },
        )

    @app.get("/books", response_class=HTMLResponse)
    def books_fragment(request: Request, page: int = Query(1, ge=1)):
        """Grid items only -- used by search-as-you-type and 'load more'."""
        args = _query_args(request)
        books, total = library.list_books(
            **args, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE
        )
        headers = {
            "X-Total-Count": str(total),
            "X-Has-More": "1" if page * PAGE_SIZE < total else "0",
        }
        # Only on the first page: a filter change re-renders the whole grid, and
        # the A-Z strip has to be told which letters still have books behind
        # them. Appending page 2 changes nothing about that, so it is not
        # recomputed while scrolling.
        if page == 1:
            counts = library.initials(**args)
            headers["X-Initials"] = ",".join(f"{ch}:{n}" for ch, n in sorted(counts.items()))
        return templates.TemplateResponse(
            request,
            "_grid.html",
            {
                "books": books,
                "total": total,
                "page": page,
                "has_more": page * PAGE_SIZE < total,
            },
            headers=headers,
        )

    @app.get("/book/{book_id}", response_class=HTMLResponse)
    def book_detail(request: Request, book_id: int):
        book = library.get_book(book_id)
        if not book:
            raise HTTPException(404, "No such book")
        return templates.TemplateResponse(request, "_detail.html", {"book": book})

    # -- covers ------------------------------------------------------------

    @app.get("/cover/{book_id}")
    def cover(book_id: int, size: str = "display"):
        book = library.get_book(book_id)
        if not book:
            raise HTTPException(404, "No such book")
        if book["has_cover"]:
            display, thumb = cover_paths(config.covers_dir, book["identity_hash"])
            path = thumb if size == "thumb" else display
            if path.exists():
                return FileResponse(
                    path,
                    media_type="image/webp",
                    headers={"Cache-Control": "public, max-age=604800"},
                )
        return Response(
            content=_placeholder_svg(book),
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    # -- download ----------------------------------------------------------

    @app.get("/download/{book_id}")
    def download(book_id: int):
        book = library.get_book(book_id)
        if not book:
            raise HTTPException(404, "No such book")
        path = Path(book["path"])
        if not path.exists():
            raise HTTPException(410, "File is no longer on disk")
        media_type = DOWNLOAD_MIMES.get(
            book["format"], mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        )
        return FileResponse(
            path,
            media_type=media_type,
            filename=_download_name(book, path),
        )

    # -- mutations ---------------------------------------------------------

    @app.post("/book/{book_id}/metadata")
    async def update_metadata(request: Request, book_id: int):
        book = library.get_book(book_id)
        if not book:
            raise HTTPException(404, "No such book")
        form = await request.form()
        for field_name in ("title", "authors", "series", "series_index", "published_year",
                           "publisher", "isbn", "description"):
            if field_name in form:
                library.set_override(
                    book["identity_hash"], field_name, (form.get(field_name) or "").strip()
                )
        _rewrite(library, scanner, book_id)
        return RedirectResponse(f"/#book-{book_id}", status_code=303)

    @app.post("/book/{book_id}/revert")
    def revert_field(book_id: int, field: str = Form(...)):
        book = library.get_book(book_id)
        if not book:
            raise HTTPException(404, "No such book")
        library.set_override(book["identity_hash"], field, None)
        _rewrite(library, scanner, book_id)
        return JSONResponse({"ok": True})

    @app.post("/book/{book_id}/match")
    def accept_match(book_id: int, openlibrary_id: str = Form(...)):
        """Accept one of the stored Open Library candidates."""
        book = library.get_book(book_id)
        if not book:
            raise HTTPException(404, "No such book")
        chosen = next(
            (c for c in book["candidates"] if c.get("openlibrary_id") == openlibrary_id),
            None,
        )
        if not chosen:
            raise HTTPException(400, "Unknown candidate")
        identity = book["identity_hash"]
        if chosen.get("title"):
            library.set_override(identity, "title", chosen["title"])
        if chosen.get("authors"):
            library.set_override(identity, "authors", chosen["authors"])
        if chosen.get("year"):
            library.set_override(identity, "published_year", chosen["year"])
        if chosen.get("publisher"):
            library.set_override(identity, "publisher", chosen["publisher"])

        fields = scanner._fields_from_row(library.get_book(book_id))
        fields.openlibrary_id = openlibrary_id
        fields.needs_review = False
        fields.candidates = []
        fields.match_source = "manual"
        fields.match_confidence = 1.0
        library.upsert(fields)

        cover_id = chosen.get("cover_id")
        if cover_id and not book["has_cover"]:
            _fetch_candidate_cover(config, db, identity, cover_id, library, book_id)
        return JSONResponse({"ok": True})

    @app.post("/book/{book_id}/refresh")
    def refresh(book_id: int):
        book = library.get_book(book_id)
        if not book:
            raise HTTPException(404, "No such book")
        from ..enrich import OpenLibraryClient

        client = OpenLibraryClient(db, offline=config.offline)
        try:
            done = scanner._enrich_book(client, book, manual=True)
        finally:
            client.close()
        return JSONResponse(
            {"ok": done, "reason": None if done else "Nothing reliable to look up."}
        )

    @app.post("/scan")
    def trigger_scan(force: bool = Query(False)):
        started = scanner.start(force=force)
        return JSONResponse({"started": started, **scanner.state.snapshot()})

    @app.get("/scan/status")
    def scan_status():
        return JSONResponse(scanner.state.snapshot())

    @app.get("/healthz")
    def healthz():
        return JSONResponse({"ok": True, "books": library.stats()["books"]})

    return app


# -- helpers ---------------------------------------------------------------


def _rewrite(library: Library, scanner: Scanner, book_id: int) -> None:
    """Re-apply overrides onto the stored row so sorting and search see them."""
    book = library.get_book(book_id)
    if book:
        library.upsert(scanner._fields_from_row(book))


def _fetch_candidate_cover(config, db, identity, cover_id, library, book_id) -> None:
    from .. import covers as covers_module
    from ..enrich import OpenLibraryClient

    client = OpenLibraryClient(db, offline=config.offline)
    try:
        data = client.get_cover(int(cover_id))
    finally:
        client.close()
    if data and covers_module.store_cover(config.covers_dir, identity, data):
        with db.write() as conn:
            conn.execute(
                "UPDATE books SET has_cover = 1, cover_source = 'openlibrary' WHERE id = ?",
                (book_id,),
            )


def _as_int(value) -> int | None:
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def _filesize(num: int | None) -> str:
    size = float(num or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _series_index(value) -> str:
    if value is None:
        return ""
    return str(int(value)) if float(value) == int(value) else str(value)


def _download_name(book: dict, path: Path) -> str:
    """A readable filename, falling back to whatever is on disk."""
    title = book.get("title")
    if not title:
        return path.name
    authors = book.get("authors") or []
    stem = f"{authors[0]} - {title}" if authors else title
    safe = "".join(c for c in stem if c.isalnum() or c in " .,'-_()&").strip()
    safe = safe[:150].rstrip(". ")
    return f"{safe}{path.suffix}" if safe else path.name


def _placeholder_svg(book: dict) -> str:
    """A deterministic, readable stand-in for a missing cover."""
    title = book.get("display_title") or "Untitled"
    author = (book.get("authors") or [None])[0] or ""
    seed = int(hashlib.sha256(book["identity_hash"].encode()).hexdigest()[:8], 16)
    hue = seed % 360
    lines = _wrap(title, 18)[:4]
    text = "".join(
        f'<tspan x="150" dy="{34 if i else 0}">{html.escape(line)}</tspan>'
        for i, line in enumerate(lines)
    )
    author_line = (
        f'<text x="150" y="392" text-anchor="middle" font-size="17" fill="hsl({hue},25%,72%)"'
        f' font-family="Georgia, serif">{html.escape(author[:34])}</text>'
        if author
        else ""
    )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 450" width="300" height="450">
  <defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0%" stop-color="hsl({hue},22%,26%)"/>
    <stop offset="100%" stop-color="hsl({(hue + 40) % 360},26%,15%)"/>
  </linearGradient></defs>
  <rect width="300" height="450" fill="url(#g)"/>
  <rect x="14" y="14" width="272" height="422" fill="none" stroke="hsl({hue},30%,55%)" stroke-opacity="0.35"/>
  <text x="150" y="200" text-anchor="middle" font-size="26" fill="hsl({hue},30%,88%)"
        font-family="Georgia, 'Times New Roman', serif">{text}</text>
  {author_line}
</svg>"""


def _wrap(text: str, width: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word[:width]
    if current:
        lines.append(current)
    return lines or [text[:width]]
