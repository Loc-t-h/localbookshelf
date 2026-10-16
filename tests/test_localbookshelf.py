"""Tests for the behaviour that must not regress.

Run with: uv run pytest -q
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import make_fixtures  # noqa: E402

from localbookshelf import covers, formats  # noqa: E402
from localbookshelf.config import (  # noqa: E402
    Config,
    load_env_file,
    roots_from_env,
)
from localbookshelf.db import Database  # noqa: E402
from localbookshelf.enrich import (  # noqa: E402
    Candidate,
    author_matches,
    score,
    title_similarity,
)
from localbookshelf.filename import guess  # noqa: E402
from localbookshelf.formats.base import (  # noqa: E402
    clean_text,
    clean_description,
    looks_like_junk_title,
    strip_series_prefix,
    normalize_isbn,
    split_authors,
)
from localbookshelf.identity import file_identity  # noqa: E402
from localbookshelf.library import (  # noqa: E402
    initial_for,
    SORTS,
    Library,
    author_sort_for,
    sort_title_for,
)
from localbookshelf.scanner import Scanner  # noqa: E402


@pytest.fixture(scope="session")
def sample_library(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("books")
    make_fixtures.build(root)
    return root


@pytest.fixture
def shelf(tmp_path, sample_library):
    """A scanned, offline library. No network, no Open Library."""
    library_root = tmp_path / "library"
    shutil.copytree(sample_library, library_root)
    config = Config(data_dir=tmp_path / "data", roots=[library_root], offline=True)
    config.ensure_dirs()
    db = Database(config.db_path)
    library = Library(db, config)
    scanner = Scanner(config, db, library)
    scanner.start(block=True)
    return config, db, library, scanner, library_root


# -- format parsing ---------------------------------------------------------


def test_epub_metadata_and_cover(sample_library):
    meta = formats.extract(sample_library / "Dan Simmons" / "Hyperion.epub")
    assert meta.title == "Hyperion"
    assert meta.authors == ["Dan Simmons"]
    assert meta.series == "Hyperion Cantos"
    assert meta.series_index == 1.0
    assert meta.isbn == "9780553283686"
    assert meta.published_year == 1989
    assert meta.cover_bytes and len(meta.cover_bytes) > 1000
    assert meta.error is None


def test_epub3_collection_series(sample_library):
    meta = formats.extract(sample_library / "Ursula K. Le Guin" / "The Dispossessed.epub")
    assert meta.series == "Hainish Cycle"
    assert meta.series_index == 6.0


def test_mobi_exth_metadata_and_cover(sample_library):
    meta = formats.extract(sample_library / "kindle" / "Neuromancer.mobi")
    assert meta.title == "Neuromancer"
    assert meta.authors == ["William Gibson"]
    assert meta.publisher == "Ace Books"
    assert meta.isbn == "9780441569595"
    assert meta.cover_bytes and meta.cover_bytes[:3] == b"\xff\xd8\xff"


def test_mobi_drm_is_flagged_not_fatal(sample_library):
    meta = formats.extract(sample_library / "kindle" / "Locked Book.mobi")
    assert meta.drm is True
    # The EXTH block is still readable behind the DRM flag.
    assert meta.title == "Locked Book"


def test_comic_info_and_natural_page_order(sample_library):
    meta = formats.extract(sample_library / "comics" / "Saga 001.cbz")
    assert meta.series == "Saga"
    assert meta.series_index == 1.0
    assert meta.authors == ["Brian K. Vaughan"]
    assert meta.cover_bytes  # page1.jpg, not page10.jpg


def test_mislabelled_cbr_that_is_really_a_zip_still_reads(sample_library, tmp_path):
    """Comics in the wild are routinely misnamed; dispatch on content."""
    mislabelled = tmp_path / "Saga 001.cbr"
    mislabelled.write_bytes((sample_library / "comics" / "Saga 001.cbz").read_bytes())
    meta = formats.extract(mislabelled)
    assert meta.series == "Saga"
    assert meta.cover_bytes
    assert meta.error is None


def test_corrupt_comic_archive_is_reported(tmp_path):
    """rarfile opens damaged archives silently and lists nothing."""
    broken = tmp_path / "broken.cbr"
    broken.write_bytes(b"Rar!\x1a\x07\x00" + b"garbage")
    meta = formats.extract(broken)
    assert meta.error is not None


def test_pdf_junk_title_rejected(sample_library):
    meta = formats.extract(
        sample_library / "manuals" / "Kernighan, Brian - The C Programming Language.pdf"
    )
    assert meta.title is None  # "....pdf" is not a title
    assert meta.cover_bytes  # page one still renders


def test_corrupt_files_report_errors_without_raising(sample_library):
    for name in ("broken/truncated.epub", "broken/empty.pdf"):
        meta = formats.extract(sample_library / name)
        assert meta.error is not None
        assert meta.title is None


# -- filename heuristics ----------------------------------------------------


@pytest.mark.parametrize(
    "name,title,authors,series,index",
    [
        ("Dan Simmons - Hyperion.epub", "Hyperion", ["Dan Simmons"], None, None),
        (
            "Le Guin, Ursula K. - The Left Hand of Darkness (1969).epub",
            "The Left Hand of Darkness",
            ["Ursula K. Le Guin"],
            None,
            None,
        ),
        ("Bone #12 - The Great Cow Race.cbz", "The Great Cow Race", [], "Bone", 12.0),
        ("Discworld 05 - Sourcery.epub", "Sourcery", [], "Discworld", 5.0),
        ("Saga v03 - Chapter 12.cbz", "Chapter 12", [], "Saga", 3.0),
        (
            "Dan Simmons - Hyperion Cantos 01 - Hyperion.epub",
            "Hyperion",
            ["Dan Simmons"],
            "Hyperion Cantos",
            1.0,
        ),
    ],
)
def test_filename_guesses(name, title, authors, series, index):
    result = guess(Path("/library") / name)
    assert result.title == title
    assert result.authors == authors
    assert result.series == series
    assert result.series_index == index


def test_filename_year_and_noise_stripping():
    result = guess(Path("/library/Snow Crash (z-lib.org) [retail] (1992).epub"))
    assert result.title == "Snow Crash"
    assert result.year == 1992


def test_initials_survive_dot_normalization():
    # Dots are only separators when the name has no spaces of its own.
    assert guess(Path("/l/The.Left.Hand.Of.Darkness.epub")).title == "The Left Hand Of Darkness"
    assert guess(Path("/l/Ursula K. Le Guin - Rocannon's World.epub")).authors == [
        "Ursula K. Le Guin"
    ]


# -- text helpers -----------------------------------------------------------


def test_junk_title_detection():
    assert looks_like_junk_title("Microsoft Word - report.doc")
    assert looks_like_junk_title("thesis.pdf")
    assert looks_like_junk_title("a1b2c3d4e5f6a7b8")
    assert not looks_like_junk_title("Hyperion")
    # Matching the filename is not, by itself, junk.
    assert not looks_like_junk_title("Structure and Interpretation of Computer Programs")


def test_clean_description_strips_markdown():
    raw = (
        "[Comment by A. Critic][1]:\n\n> A **great** book, see [the review](http://x).\n\n"
        "---\n\n[1]: http://example.com/review\n"
    )
    cleaned = clean_description(raw)
    assert "[1]" not in cleaned
    assert "http://" not in cleaned
    assert ">" not in cleaned
    assert "**" not in cleaned
    assert "A great book, see the review." in cleaned


def test_clean_description_strips_html():
    assert clean_description("<p>One.</p><p>Two &amp; three.</p>") == "One.\nTwo & three."


def test_isbn_normalization():
    assert normalize_isbn("urn:isbn:978-0-553-28368-6") == "9780553283686"
    assert normalize_isbn("ISBN 0-441-56959-5") == "0441569595"
    assert normalize_isbn("not-an-isbn") is None


def test_author_splitting_keeps_inverted_names_intact():
    assert split_authors("Simmons, Dan") == ["Simmons, Dan"]
    assert split_authors("Neil Gaiman & Terry Pratchett") == ["Neil Gaiman", "Terry Pratchett"]
    assert split_authors("A. Smith; B. Jones") == ["A. Smith", "B. Jones"]


def test_sort_keys():
    assert sort_title_for("The Dispossessed") == "dispossessed"
    assert author_sort_for("Dan Simmons") == "simmons, dan"
    assert author_sort_for("Simmons, Dan") == "simmons, dan"


def test_surname_particles_keep_the_surname_whole():
    # Le Guin files under L, not G.
    assert author_sort_for("Ursula K. Le Guin") == "le guin, ursula k."
    assert author_sort_for("Ludwig van Beethoven") == "van beethoven, ludwig"
    assert author_sort_for("Vincent van der Berg") == "van der berg, vincent"
    assert author_sort_for("Cixin Liu") == "liu, cixin"
    assert author_sort_for("Plato") == "plato"


# -- matching ---------------------------------------------------------------


def test_title_similarity_and_author_matching():
    assert title_similarity("The Dispossessed", "Dispossessed") == 1.0
    assert title_similarity("Hyperion", "Hyperion: A Novel") > 0.9
    assert title_similarity("Hyperion", "Dune") < 0.5
    assert author_matches(["Dan Simmons"], ["Simmons, Dan"])
    assert not author_matches(["Dan Simmons"], ["Frank Herbert"])


def test_wrong_author_pushes_match_below_accept_threshold():
    candidate = Candidate(openlibrary_id="/works/1", title="Hyperion", authors=["Friedrich Hölderlin"])
    assert score(candidate, "Hyperion", ["Dan Simmons"]) < 0.85

    right = Candidate(openlibrary_id="/works/2", title="Hyperion", authors=["Dan Simmons"])
    assert score(right, "Hyperion", ["Dan Simmons"]) >= 0.85


# -- covers -----------------------------------------------------------------


def test_tiny_images_are_rejected_as_covers(tmp_path):
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (1, 1)).save(buffer, "GIF")
    assert covers.store_cover(tmp_path, "a" * 64, buffer.getvalue()) is False
    assert covers.store_cover(tmp_path, "b" * 64, b"not an image") is False


def test_cover_renditions_are_written(tmp_path, sample_library):
    meta = formats.extract(sample_library / "Dan Simmons" / "Hyperion.epub")
    assert covers.store_cover(tmp_path, "c" * 64, meta.cover_bytes) is True
    display, thumb = covers.cover_paths(tmp_path, "c" * 64)
    assert display.exists() and thumb.exists()
    assert thumb.stat().st_size < display.stat().st_size


# -- scanning ---------------------------------------------------------------


def test_scan_indexes_every_supported_format(shelf):
    _, _, library, _, _ = shelf
    stats = library.stats()
    assert stats["books"] == 15
    assert {f["format"] for f in stats["formats"]} == {"epub", "pdf", "mobi", "cbz"}


def test_scan_skips_junk_and_unsupported_files(shelf):
    _, db, _, _, _ = shelf
    paths = [row["path"] for row in db.query("SELECT path FROM books")]
    assert not any("notes.txt" in p for p in paths)
    assert not any(".DS_Store" in p for p in paths)
    assert not any("/._" in p for p in paths)


def test_unreadable_files_are_recorded_not_dropped(shelf):
    _, db, _, _, _ = shelf
    errors = db.query("SELECT path, scan_error FROM books WHERE scan_error IS NOT NULL")
    assert len(errors) == 3  # truncated.epub, empty.pdf, DRM mobi


def test_rescan_of_unchanged_library_is_a_no_op(shelf):
    _, _, _, scanner, _ = shelf
    scanner.start(block=True)
    state = scanner.state.snapshot()
    assert state["skipped"] == 15
    assert state["added"] == 0 and state["updated"] == 0


def test_identity_and_overrides_survive_a_move_and_rename(shelf):
    _, db, library, scanner, root = shelf
    book = library.get_book_by_identity(
        file_identity(root / "Dan Simmons" / "Hyperion.epub")
    )
    library.set_override(book["identity_hash"], "title", "Hyperion (corrected)")
    scanner.start(block=True)

    destination = root / "elsewhere" / "gibberish_final(2).epub"
    destination.parent.mkdir(exist_ok=True)
    (root / "Dan Simmons" / "Hyperion.epub").rename(destination)
    scanner.start(block=True)

    after = library.get_book(book["id"])
    assert after is not None, "the moved file must not become a new book"
    assert after["path"] == str(destination)
    assert after["title"] == "Hyperion (corrected)"
    assert library.stats()["books"] == 15


def test_deleted_files_are_forgotten(shelf):
    _, _, library, scanner, root = shelf
    (root / "kindle" / "Snow Crash.azw3").unlink()
    scanner.start(block=True)
    assert library.stats()["books"] == 14


def test_library_files_are_never_modified(shelf, sample_library):
    """The load-bearing promise: scanning must not touch the library."""
    _, _, _, scanner, root = shelf
    scanner.start(force=True, block=True)
    for original in sample_library.rglob("*"):
        if not original.is_file():
            continue
        copy = root / original.relative_to(sample_library)
        assert copy.exists(), f"{copy} disappeared"
        assert copy.read_bytes() == original.read_bytes(), f"{copy} was modified"


def test_nothing_is_written_inside_the_library(shelf):
    config, _, _, _, root = shelf
    assert config.data_dir not in root.parents
    assert not list(root.rglob("*.db"))
    assert not list(root.rglob("*.webp"))


# -- search and filtering ---------------------------------------------------


def test_search_covers_title_author_and_series(shelf):
    _, _, library, _, _ = shelf
    assert {b["title"] for b in library.list_books(query="hyperion")[0]} == {
        "Hyperion",
        "The Fall of Hyperion",
    }
    assert library.list_books(query="gibson")[0][0]["title"] == "Neuromancer"
    assert len(library.list_books(query="saga")[0]) == 2


def test_search_input_cannot_break_fts_syntax(shelf):
    _, _, library, _, _ = shelf
    for nasty in ['" OR 1=1 --', "NEAR(", "*", "^", '"""', "AND OR NOT"]:
        library.list_books(query=nasty)  # must not raise


def test_format_filter(shelf):
    _, _, library, _, _ = shelf
    books, total = library.list_books(formats=["pdf"])
    assert total == 3
    assert all(b["format"] == "pdf" for b in books)


def test_overrides_are_visible_to_sorting_and_search(shelf):
    _, _, library, scanner, _ = shelf
    book = library.list_books(query="dispossessed")[0][0]
    library.set_override(book["identity_hash"], "title", "Zzz Renamed")
    library.upsert(scanner._fields_from_row(library.get_book(book["id"])))

    assert library.list_books(query="Zzz")[0][0]["id"] == book["id"]
    by_title, _ = library.list_books(sort="title", limit=200)
    assert by_title[-1]["id"] == book["id"]


# -- sorting ----------------------------------------------------------------


@pytest.mark.parametrize("sort", list(SORTS))
@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_every_sort_runs_in_both_directions(shelf, sort, direction):
    _, _, library, _, _ = shelf
    books, total = library.list_books(sort=sort, direction=direction, limit=200)
    assert total == 15 and len(books) == 15


def test_sort_directions_are_actual_reverses(shelf):
    _, _, library, _, _ = shelf
    ascending = [b["id"] for b in library.list_books(sort="title", direction="asc", limit=200)[0]]
    descending = [b["id"] for b in library.list_books(sort="title", direction="desc", limit=200)[0]]
    assert ascending == list(reversed(descending))


def test_title_sort_ignores_leading_articles(shelf):
    _, _, library, _, _ = shelf
    titles = [b["title"] for b in library.list_books(sort="title", limit=200)[0]]
    assert titles.index("The Dispossessed") < titles.index("Exhalation")


def test_year_sort_is_chronological(shelf):
    _, _, library, _, _ = shelf
    years = [
        b["published_year"]
        for b in library.list_books(sort="year", direction="asc", limit=200)[0]
        if b["published_year"]
    ]
    assert years == sorted(years)


@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_books_without_an_author_sort_last_either_way(shelf, direction):
    """The defect that made the author sort look broken: unknowns came first."""
    _, _, library, _, _ = shelf
    books = library.list_books(sort="author", direction=direction, limit=200)[0]
    named = [i for i, b in enumerate(books) if b["authors"]]
    unnamed = [i for i, b in enumerate(books) if not b["authors"]]
    assert unnamed, "fixture should contain books with no author"
    assert max(named) < min(unnamed)


@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_books_without_a_series_sort_last_either_way(shelf, direction):
    _, _, library, _, _ = shelf
    books = library.list_books(sort="series", direction=direction, limit=200)[0]
    in_series = [i for i, b in enumerate(books) if b["series_name"]]
    loose = [i for i, b in enumerate(books) if not b["series_name"]]
    assert max(in_series) < min(loose)


def test_explicit_sort_wins_over_search_relevance(shelf):
    _, _, library, _, _ = shelf
    books = library.list_books(query="hyperion", sort="title", direction="asc")[0]
    assert [b["title"] for b in books] == ["The Fall of Hyperion", "Hyperion"]


def test_unknown_sort_and_direction_fall_back_safely(shelf):
    _, _, library, _, _ = shelf
    assert library.list_books(sort="'; DROP TABLE books--", direction="sideways")[1] == 15


def test_series_sort_orders_by_index_within_a_series(shelf):
    _, _, library, _, _ = shelf
    books = library.list_books(sort="series", limit=200)[0]
    cantos = [b["series_index"] for b in books if b["series_name"] == "Hyperion Cantos"]
    assert cantos == [1.0, 2.0]


def test_override_export_and_import_round_trip(shelf):
    _, _, library, _, _ = shelf
    book = library.list_books(query="neuromancer")[0][0]
    library.set_override(book["identity_hash"], "title", "Kept")
    exported = library.export_overrides()

    library.set_override(book["identity_hash"], "title", None)
    assert "title" not in library.overrides_for(book["identity_hash"])

    library.import_overrides(exported)
    assert library.overrides_for(book["identity_hash"])["title"] == "Kept"


# -- library path configuration ---------------------------------------------


def test_library_path_is_read_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("LIBRARY_PATH", str(tmp_path / "Books"))
    assert roots_from_env() == [tmp_path / "Books"]


def test_library_path_accepts_several_folders(monkeypatch, tmp_path):
    monkeypatch.setenv("LIBRARY_PATH", f"{tmp_path / 'a'}:{tmp_path / 'b'}")
    assert roots_from_env() == [tmp_path / "a", tmp_path / "b"]


def test_library_path_expands_a_tilde(monkeypatch):
    monkeypatch.setenv("LIBRARY_PATH", "~/Books")
    assert roots_from_env() == [Path.home() / "Books"]


def test_unset_library_path_yields_no_roots(monkeypatch):
    monkeypatch.delenv("LIBRARY_PATH", raising=False)
    assert roots_from_env() == []


def test_env_file_is_read_but_never_beats_the_real_environment(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text('LIBRARY_PATH="/from/file"\nPORT=9999\n')

    monkeypatch.setenv("LIBRARY_PATH", "/from/environment")
    monkeypatch.delenv("PORT", raising=False)
    assert load_env_file([env_file]) == env_file

    # The exported value wins; the unset one is filled in from the file.
    assert roots_from_env() == [Path("/from/environment")]
    assert os.environ["PORT"] == "9999"


def test_roots_are_not_persisted_in_the_database(shelf):
    """The library folder is configuration; the database stays disposable."""
    config, db, library, _, root = shelf
    assert library.roots() == [root.resolve()]

    tables = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "roots" not in tables

    # A fresh Library over the same database, configured with nothing, sees
    # no library at all -- proof the path came from config and not the cache.
    assert Library(db, Config(data_dir=config.data_dir)).roots() == []


# -- titles that are really something else -----------------------------------


def test_an_identifier_in_the_title_field_counts_as_junk():
    """Calibre writes the ISBN or ASIN into dc:title on some files."""
    assert looks_like_junk_title("0123456789 (N)")   # identifier plus a tag
    assert looks_like_junk_title("0123456789")
    assert looks_like_junk_title("9780123456789")
    assert looks_like_junk_title("978-0-12-345678-9")
    assert looks_like_junk_title("020161622X")       # ISBN-10 check digit
    assert looks_like_junk_title("B01ABC2345")       # ASIN


def test_numeric_titles_are_not_mistaken_for_identifiers():
    """The whole point is to lose bad titles, not real ones."""
    for title in ["1984", "2666", "1939-1945", "1Q84", "Catch-22",
                  "2001: A Space Odyssey", "BLACKWATER"]:
        assert not looks_like_junk_title(title), title


def test_series_prefix_is_stripped_from_the_title():
    assert strip_series_prefix("[Harrow Cycle 02] • Second Light",
                               "Harrow Cycle") == "Second Light"
    assert strip_series_prefix("[Discworld 5] Sourcery") == "Sourcery"
    assert strip_series_prefix("[Book 3]: The End") == "The End"
    # No index, but it names the series we already extracted.
    assert strip_series_prefix("[Harrow Cycle] • First Light",
                               "Harrow Cycle") == "First Light"


def test_brackets_that_are_part_of_the_title_survive():
    assert strip_series_prefix("[Illustrated] Dune") == "[Illustrated] Dune"
    assert strip_series_prefix("[Vol. II] War and Peace") == "[Vol. II] War and Peace"
    # Stripping would leave nothing, so it stays as it came.
    assert strip_series_prefix("[Harrow Cycle 02]", "Harrow Cycle") == "[Harrow Cycle 02]"
    assert strip_series_prefix(None) is None


def test_epub_with_an_isbn_title_falls_back_to_the_filename(tmp_path):
    """End to end: a bogus dc:title must not shadow the filename."""
    import zipfile

    opf = """<?xml version="1.0"?>
    <package xmlns="http://www.idpf.org/2007/opf" version="2.0">
      <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"
                xmlns:opf="http://www.idpf.org/2007/opf">
        <dc:title>0123456789 (N)</dc:title>
        <dc:creator opf:role="aut">Dana Hale</dc:creator>
        <dc:identifier opf:scheme="ISBN">9780123456789</dc:identifier>
      </metadata>
    </package>"""
    container = """<?xml version="1.0"?>
    <container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
      <rootfiles><rootfile full-path="content.opf"
        media-type="application/oebps-package+xml"/></rootfiles>
    </container>"""

    path = tmp_path / "A Field Guide to Nothing - Dana Hale.epub"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("content.opf", opf)

    meta = formats.extract(path)
    assert meta.title is None, "the ISBN must not be taken as a title"
    assert meta.isbn == "9780123456789"
    assert meta.authors == ["Dana Hale"]


# -- an ISBN in the filename -------------------------------------------------


def test_isbn_is_taken_from_the_filename():
    """An exact key beats a title guess, so it is worth mining the name for."""
    g = guess(Path("Emergent Cities A Field Study"
                   " - Mara Iwasaki (9781234567897).pdf"))
    assert g.isbn == "9781234567897"
    # Removing it also unblocks the author split, which otherwise sees
    # "Mara Iwasaki (9781234567897)" and rejects it as not a name.
    assert g.authors == ["Mara Iwasaki"]
    assert g.title == "Emergent Cities A Field Study"
    # ...and the 978 prefix must not be mistaken for a publication year.
    assert g.year is None


def test_filenames_without_an_isbn_are_unaffected():
    assert guess(Path("Dan Simmons - Hyperion.epub")).isbn is None
    g = guess(Path("Some Book (2019).epub"))
    assert g.isbn is None and g.year == 2019


def test_inline_markup_is_stripped_from_metadata_text():
    """Publisher toolchains leak tags into PDF title fields."""
    assert clean_text("<em>Emergent Cities: A Field Study</em>") == (
        "Emergent Cities: A Field Study"
    )
    # Not a markup stripper in general: a real angle bracket stays.
    assert clean_text("Programming <T> Generics") == "Programming <T> Generics"


# -- the A-Z strip -----------------------------------------------------------
# The strip scrolls the shelf to an initial rather than filtering it, so what
# the server owes it is only the per-letter counts and a bucket for each book.


def test_initial_buckets_letters_and_lumps_the_rest_under_hash():
    assert initial_for("hyperion") == "H"
    assert initial_for("  zed") == "Z"
    assert initial_for("1984") == "#"
    assert initial_for("\u00e9mile") == "#"      # not A-Z after upcasing
    assert initial_for("") == "#"
    assert initial_for(None) == "#"


def test_initials_cover_every_book_exactly_once(shelf):
    _, _, library, _, _ = shelf
    counts = library.initials()
    total = library.list_books(limit=1)[1]
    assert sum(counts.values()) == total
    assert all(len(ch) == 1 for ch in counts)


def test_every_count_matches_the_books_behind_it(shelf):
    """The strip dims letters with nothing behind them, so the counts have to
    agree with what a jump to that letter would actually land on."""
    _, _, library, _, _ = shelf
    counts = library.initials()
    books = library.list_books(sort="title", limit=500)[0]
    tally: dict[str, int] = {}
    for book in books:
        ch = initial_for(book["sort_title"])
        tally[ch] = tally.get(ch, 0) + 1
    assert counts == tally


def test_initials_are_the_same_whatever_the_sort(shelf):
    """Books are bucketed by title regardless of order, because the strip is
    only ever offered on the title sort."""
    _, _, library, _, _ = shelf
    by_title = library.initials(sort="title")
    for sort in ("added", "author", "series", "year", "modified", "size"):
        assert library.initials(sort=sort) == by_title, sort


def test_initials_respect_the_other_filters(shelf):
    """The strip counts what the current shelf would show, not the whole library."""
    _, _, library, _, _ = shelf
    counts = library.initials(formats=["epub"])
    epubs = library.list_books(formats=["epub"], limit=1)[1]
    assert sum(counts.values()) == epubs


def test_a_letter_with_no_books_is_absent_from_the_counts(shelf):
    _, _, library, _, _ = shelf
    counts = library.initials()
    books = library.list_books(sort="title", limit=500)[0]
    present = {initial_for(b["sort_title"]) for b in books}
    for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        if ch not in present:
            assert ch not in counts
