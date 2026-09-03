# LocalBookshelf — Goals & Spec

> A bookshelf view for a folder of books. Point it at a directory, get covers and
> metadata. Nothing else.

**Status:** M0–M6 built and tested on macOS · **Last updated:** 2026-09-03

> Implemented: all four format parsers, the two-phase scan pipeline, Open
> Library enrichment with confidence gating, the cover cache, the shelf UI with
> search/sort/filters, the detail panel, downloads, and sticky overrides.
> Remaining: M7 (deploy to the Ubuntu box as a systemd unit) and M8 (polish).
> See [README.md](README.md) to run it.

---

## 1. The one-sentence version

LocalBookshelf scans a folder of ebooks, downloads their metadata and covers, and
serves a single browsable shelf at `localhost` — without a reader, without a
library manager, and without ever touching the files on disk.

## 2. Why this exists

Calibre-Web-Automated, BookLore, Grimmory and friends are *library management
systems*. They want to own your files: rename them, restructure them into
`Author/Title (id)/`, convert formats, manage users, run ingest pipelines, and
render books in an in-browser reader. Each of those is a feature someone needs;
together they are a system that requires maintenance.

The actual unmet need is smaller: **I have a folder of books and I want to look
at it the way Plex lets me look at a folder of movies.** Covers in a grid, real
titles instead of `dl_9781234_final(2).epub`, a way to find the one I'm thinking
of, and a button to pull it down to whatever device I'm on.

## 3. Design tenets

These are the tie-breakers. When a feature request is ambiguous, resolve it
against this list.

1. **The library folder is read-only.** LocalBookshelf never renames, moves,
   rewrites, converts, or deletes a file in the library. Not once, not behind a
   setting. This is the single most important tenet — it's what makes the app
   safe to point at a folder you care about and safe to abandon later.
2. **The folder is the source of truth; the database is a cache.** Delete the
   database and a rescan reconstructs everything except your manual edits (which
   are separately exportable). No book exists only inside LocalBookshelf.
3. **Zero configuration to first shelf.** `localbookshelf serve ~/Books` and you
   have a shelf. Every other setting has a working default.
4. **Degrade, never block.** A book with no metadata, an unparseable file, a
   failed network lookup — these produce a card with a placeholder cover and the
   filename as its title. Nothing halts a scan.
5. **Small enough to read.** If the codebase stops being something one person can
   hold in their head on a weekend, a feature has gone in that shouldn't have.
6. **No accounts, no auth, no multi-user.** One person, one LAN, one shelf.

## 4. Scope

### 4.1 In scope (v1)

| Area | What ships |
| --- | --- |
| **Ingest** | Recursive scan of one or more root folders; incremental rescan; manual "Rescan" from the UI |
| **Formats** | EPUB, PDF, CBZ/CBR, MOBI/AZW3 |
| **Metadata** | Embedded metadata first, Open Library second, manual override last |
| **Covers** | Extracted or rendered from the file; fetched from Open Library as fallback; cached as WebP thumbnails |
| **Shelf view** | Responsive cover grid, sort, filter, full-text search over title/author/series |
| **Detail panel** | Cover, title, author, series + number, publisher, year, ISBN, description, format, file size, path |
| **Download** | Serve the original file byte-for-byte to the browser, correct MIME + filename |
| **Editing** | Edit any metadata field by hand; edits are sticky across rescans and metadata refreshes |
| **Access** | HTTP bound to localhost or the LAN; runs on Ubuntu (deploy) and macOS (dev) |
| **Service** | A systemd user unit for the Ubuntu box so the shelf is always up |

### 4.2 Explicitly out of scope

Listed because *not* building these is the product.

- **A reader.** No in-browser EPUB/PDF viewer. Download and use a real reader.
- **Format conversion.** No epub→mobi, no Calibre dependency.
- **File management.** No rename, no move, no delete, no folder restructuring, no
  duplicate merging that rewrites disk.
- **Acquisition.** No downloading books from anywhere. No integration with
  request/indexer tooling.
- **Send-to-Kindle / device sync.** Download is the export path.
- **Users, logins, permissions, sharing.** Not even one login.
- **Public internet hosting.** No TLS termination, no reverse-proxy config, no
  auth. LAN-only is the supported deployment; if you expose it past the router,
  that's on you and it's out of the support surface.
- **Windows.** Not a target. Nothing should gratuitously break there, but it is
  untested and unsupported.
- **Reading progress or a "currently reading" state.** There's no reader to
  source it from, and inventing a manual toggle invites a whole tracker.
- **OPDS feeds, mobile apps, Kobo sync.** See §11 for what might come later.

## 5. Users & primary flows

Single persona: me — the library and the server live on an Ubuntu box, and I
browse it from a Mac or a phone on the same network. Downloading is how a book
gets from the server to the device I'm actually holding, which is why it's the
primary action on the detail panel.

1. **First run.** Point at `~/Books` → scan runs → progress shown live → shelf
   fills in as books are processed.
2. **Browse.** Open the shelf, scroll a grid of covers, sort by recently added.
3. **Find.** Type "hyperion" → matching books, whatever the filename was.
4. **Inspect.** Click a cover → detail panel with description and metadata.
5. **Get.** Click Download → file lands in the browser's downloads folder.
6. **Fix.** A book got the wrong match → edit fields inline, or paste a correct
   Open Library ID and re-fetch → the fix survives every future rescan.
7. **Add books.** Drop files into the folder by any means → next scan picks them
   up.

## 6. Architecture

### 6.1 Stack (assumption — override if you'd rather)

- **Backend:** Python 3.11+, FastAPI, Uvicorn. Chosen for the ebook-parsing
  ecosystem (`ebooklib`, `PyMuPDF`, `mobi`, `rarfile`, `Pillow`) — the parsing is
  the hard part of this app and Python has the least of it left to write.
- **Database:** SQLite, single file, WAL mode. FTS5 virtual table for search.
- **Frontend:** Server-rendered HTML + a small amount of vanilla JS/Alpine. No
  build step, no SPA framework. The UI is a grid, a search box, and a panel.
- **Packaging:** a `localbookshelf` console script installed with `uv tool
  install` (or `pipx`), plus a systemd user unit for the Ubuntu host. See §6.4.

### 6.2 Processes

```
localbookshelf serve ~/Books
        │
        ├── HTTP server (FastAPI)  ── serves shelf, detail, covers, downloads
        │
        └── Scanner (background)   ── walk → identify → parse → enrich → store
                                       ▲
                                       └── triggered on start, on demand,
                                           and optionally by a file watcher
```

The scanner runs in a worker thread/task pool so the UI is live during a scan.
Book cards appear as they are processed.

### 6.3 On-disk layout

Nothing is written inside the library folder. All state lives in a data
directory — `$XDG_DATA_HOME/localbookshelf` (default `~/.local/share/…`) on
Ubuntu, `~/Library/Application Support/localbookshelf` on macOS, always
overridable with `--data-dir`:

```
localbookshelf/
├── library.db          # SQLite: books, authors, series, overrides, scan state
├── covers/
│   ├── <book_id>.webp        # display cover (~600px)
│   └── <book_id>.thumb.webp  # grid thumbnail (~300px)
└── logs/
```

### 6.4 Platforms

**Ubuntu is the deployment target; macOS is the development machine.** Both are
first-class — the same code runs on both, CI (if any) tests both, and a change
that only works on one is a bug.

| | Ubuntu (deploy) | macOS (dev) |
| --- | --- | --- |
| **Role** | Long-running host; likely headless; holds the library | Where the code is written and where the shelf is browsed from |
| **Python** | 3.11+ from `deadsnakes` or distro; `uv`-managed venv | 3.11+ via Homebrew or `uv` |
| **Runs as** | systemd **user** unit (`systemctl --user`), `Restart=always`, `loginctl enable-linger` so it survives logout | `localbookshelf serve` in a terminal; auto-reload during dev |
| **Data dir** | `~/.local/share/localbookshelf` | `~/Library/Application Support/localbookshelf` |
| **Library path** | e.g. `/srv/books` or a mounted volume | e.g. `~/Books`, or an SMB/NFS mount of the Ubuntu library for testing |
| **CBR support** | `apt install unar` | `brew install unar` |
| **Binds** | `0.0.0.0:8080` (LAN) | `127.0.0.1:8080` |

Platform notes that will actually bite:

- **Path handling** must be `pathlib` throughout, with no assumptions about
  case-sensitivity: macOS APFS is case-**in**sensitive by default and ext4 is
  case-sensitive. Two files differing only in case are one file on the Mac and
  two on the server. Normalize for comparison, store the path as found.
- **Filename encoding.** macOS stores filenames in NFD, Linux typically NFC.
  `Bronte.epub` vs `Brontë.epub` will hash-match fine (identity is content-based,
  §7 step 1) but string comparisons and filename parsing must normalize to NFC
  first.
- **`.DS_Store`, `@eaDir`, `._*` resource forks** — skip them explicitly, along
  with dotfiles and `#recycle`.
- **Network mounts.** If the Mac tests against an SMB/NFS mount of the Ubuntu
  library, `mtime` granularity and `stat` cost differ sharply. The identity hash
  reads only 64 KiB per file, which keeps this tolerable; never add a full-file
  hash without measuring it over a mount first.
- **PyMuPDF, Pillow, and the rest ship manylinux and macOS arm64 wheels** — keep
  it that way. A dependency that needs a compiler on either machine is a
  dependency to reconsider.

**Docker is the primary deployment path** (revised — this section originally
recommended a systemd unit). Two reasons decided it:

1. A container can mount the library **read-only**, which turns §3 tenet 1 from
   a coding discipline into something the kernel enforces. The scanner only ever
   reads, so it costs nothing and closes the failure mode permanently.
2. The server already runs other services this way, so the shelf inherits the
   same restart policy, logging, and update flow instead of being a special case.

`Dockerfile` and `docker-compose.yml` are in the repo; see the README. A systemd
user unit remains a perfectly good alternative if the container ever becomes the
awkward option — nothing in the app depends on being containerized.

## 7. The scan pipeline

For each file under a root with a known extension:

1. **Identify.** Compute a stable identity so a moved or renamed file is
   recognized as the same book and is not re-fetched:
   `sha256(first 64 KiB + file size)`. Full-file hashing is too slow on a large
   library; this is enough to be practically collision-free for ebooks.
2. **Skip if unchanged.** If identity and mtime match the stored row, stop here.
   This makes rescans of an unchanged 5,000-book library take seconds.
3. **Extract embedded metadata** (§8).
4. **Extract a cover** from the file (§8).
5. **Parse the filename** as a fallback signal — common patterns like
   `Author - Title (Year)`, `Title - Author`, `Series 03 - Title`. Used only to
   fill fields left empty and to build a search query.
6. **Enrich from Open Library** (§9), if fields are still missing.
7. **Apply manual overrides** (§10) — these always win.
8. **Store**, generate thumbnails, index for search.

Failures at any step are recorded on the book row (`scan_error`) and surfaced in
the UI as a small badge, never as a modal or a halted scan.

## 8. Format handling

| Format | Metadata source | Cover source | Notes |
| --- | --- | --- | --- |
| **EPUB** | OPF package doc via `ebooklib` — `dc:title`, `dc:creator`, `dc:description`, `dc:identifier` (ISBN), `dc:date`, `dc:publisher`, plus Calibre's `calibre:series` / `calibre:series_index` meta tags | manifest item with `properties="cover-image"`, else `<meta name="cover">`, else first image in spine order | Best case. Series info is usually present if the file came from a Calibre library. |
| **PDF** | `PyMuPDF` document metadata dict. Frequently junk — producer strings, LaTeX artifacts, empty titles. Treat a title that looks like a filename or contains `.pdf`/`Microsoft Word` as absent. | render page 1 at ~150 DPI | Lean hard on filename parsing and Open Library here. |
| **CBZ** | `ComicInfo.xml` inside the archive if present (`Series`, `Number`, `Title`, `Writer`, `Year`); otherwise filename only | first image file in natural sort order | |
| **CBR** | same as CBZ | same as CBZ | Needs `rarfile` plus an external `unar`/`unrar` binary. Detect its absence at startup and show a one-line warning rather than failing books silently. |
| **MOBI / AZW3** | PalmDB header + EXTH records via `mobi` — records 100 (author), 103 (description), 104 (ISBN), 503 (title) | EXTH record 201/203 offset into the image section | The most fragile parser. Wrap in a broad `except`, fall back to filename + Open Library. DRM'd files are unreadable — mark them `DRM` and move on. |

**Cover normalization:** every extracted cover is converted to WebP, resized to
600px on the long edge for the detail view and 300px for the grid, and stored in
the cover cache. Aspect ratios are preserved; the grid crops with CSS, not on
disk.

## 9. Metadata enrichment (Open Library)

**Order of precedence, highest first:** manual override → embedded metadata →
Open Library → filename-derived → filename verbatim.

Open Library is used only to fill fields that are still empty after embedded
extraction, and to supply descriptions and covers, which embedded metadata
usually lacks.

**Lookup strategy:**

1. If an ISBN was extracted: `GET /isbn/{isbn}.json` → work record. Highest
   confidence; accept without review.
2. Else search: `GET /search.json?title=…&author=…&limit=5`. Accept the top hit
   only if it clears a confidence bar (normalized title similarity ≥ 0.85 and
   author surname match). Otherwise store the candidates and mark the book
   `needs_review` — the detail panel offers the candidates as one-click fixes.
3. Cover images from `covers.openlibrary.org/b/id/{cover_i}-L.jpg`, used only if
   no cover was extractable from the file.

**Manners:** identify with a real `User-Agent` including a contact URL, cap at
~1 request/second, cache every response on disk keyed by request URL, and never
re-query a book that already has a confident match. A rescan of an unchanged
library must make **zero** network requests.

**Offline is normal.** With `--offline`, or on any network failure, the app runs
fully on embedded + filename metadata. Books enriched later get filled in on the
next scan.

## 10. Manual overrides

A separate `overrides` table keyed by book identity, holding only fields the user
has edited. It is applied last in the pipeline and is never written by any
automatic process. Consequences:

- Deleting `library.db` and rescanning loses nothing but overrides — so
  `localbookshelf export-overrides > overrides.json` and its `import` counterpart
  exist from v1.
- "Refresh metadata" on a book re-runs enrichment without disturbing edited
  fields; a per-field "revert to detected" clears one override.

## 11. Data model (sketch)

```sql
books(id, identity_hash, path, format, file_size, mtime,
      title, sort_title, description, publisher, published_year,
      isbn, openlibrary_id, series_id, series_index,
      cover_status, match_confidence, needs_review, scan_error,
      added_at, updated_at)

authors(id, name, sort_name)
book_authors(book_id, author_id, role)
series(id, name)

overrides(identity_hash, field, value, updated_at)

scan_runs(id, started_at, finished_at, files_seen, books_added,
          books_updated, errors)

books_fts  -- FTS5 over title, authors, series, description
```

`identity_hash` rather than `path` is the stable key, so reorganizing your folder
does not orphan your edits.

## 12. HTTP surface

```
GET  /                        shelf (grid); query params: q, sort, format,
                              author, series, page
GET  /book/{id}               detail panel (fragment + standalone page)
GET  /cover/{id}[?size=thumb] cover image (long-lived cache headers)
GET  /download/{id}           original file, Content-Disposition: attachment,
                              supports Range requests
POST /book/{id}/metadata      apply manual override
POST /book/{id}/refresh       re-run enrichment for one book
POST /book/{id}/match         accept an Open Library candidate
POST /scan                    trigger a rescan
GET  /scan/status             JSON progress for the live indicator
GET  /healthz
```

## 13. UI

**Shelf.** Responsive cover grid, ~6 columns at desktop width down to 2 on a
phone. Each card: cover, title, author, and a small format chip. Virtualized or
paginated past ~500 items. Sticky header with search, sort (Recently added ·
Title · Author · Series · Publication year), and filter chips (format, author,
series). A scan-in-progress indicator when one is running.

**Detail.** Slide-over panel on desktop, full page on mobile: large cover, title,
author(s), series + index, publisher, year, ISBN, description, format, file size,
full path, and two buttons — **Download** (primary) and **Edit metadata**. If
`needs_review` is set, a "Is this the right book?" strip with the Open Library
candidates.

**Look.** Dark by default, light via `prefers-color-scheme`. The covers should be
the only saturated things on screen — everything else is neutral. Keyboard: `/`
focuses search, `Esc` closes the panel, arrows move the grid selection.

## 14. Non-functional targets

- **Scan throughput:** ≥ 20 books/sec for embedded-only extraction on an SSD
  (PDF cover rendering is the outlier; render lazily on first view if it drags).
- **Incremental rescan:** 5,000 unchanged books in < 10 seconds, zero network.
- **Shelf paint:** < 300 ms for the first screen of a 5,000-book library.
- **Memory:** < 300 MB steady state.
- **Dependencies:** the whole thing installs with one `uv tool install` on both
  Ubuntu and macOS and needs no external services. `unar` for CBR is the only
  optional external binary, and its absence degrades to "CBR files show as
  untitled cards" rather than an error.
- **Portability:** identical behaviour on Ubuntu and macOS. A library scanned on
  one and copied to the other reproduces the same shelf.

## 15. Milestones

| # | Milestone | Done when |
| --- | --- | --- |
| **M0** ✅ | Walking skeleton | `serve` starts, walks a folder, lists filenames as text in a browser |
| **M1** ✅ | EPUB end-to-end | EPUB metadata + covers extracted, real cover grid renders |
| **M2** ✅ | Persistence & incremental scan | SQLite, identity hashing, unchanged files skipped, cover cache |
| **M3** ✅ | Remaining formats | PDF, CBZ/CBR, MOBI/AZW3 parsers with graceful failure |
| **M4** ✅ | Open Library enrichment | ISBN + search lookup, confidence gating, cached responses, `--offline` |
| **M5** ✅ | Shelf UX | Search (FTS5), sort, filters, detail panel, **download** |
| **M6** ✅ | Overrides & review | Inline editing, sticky overrides, candidate picker, export/import |
| **M7** | Ship to the server | Runs on Ubuntu as a systemd user unit, bound to the LAN, browsable from the Mac and phone; `unar` present for CBR |
| **M8** | Polish | File watcher, dark/light, keyboard nav, packaging |

M1 is the point where the app is already useful; M5 is the point where it's the
thing described at the top of this document; M7 is the point where it stops
being a thing you start by hand. Everything after M8 is §16.

Develop against a small sample library on the Mac (a few dozen books covering all
four formats, including a deliberately broken file and a DRM'd one), and treat
"still works on Ubuntu" as part of each milestone rather than a step at the end.

## 16. Deliberately deferred

Not rejected, just not now — and each one needs a fresh argument against §3
before it goes in:

- OPDS feed (would make it work with KOReader / Moon+ Reader natively)
- File-watcher-driven live updates instead of polling scans
- Collections / tags / shelves beyond series
- Additional metadata sources (Google Books, Hardcover) behind the same interface
- "Open in system default app" for local-machine use
- Duplicate detection (report only — never auto-delete)
- Audiobook (`.m4b`) support

## 17. Open questions

1. **Multiple formats of one book.** `Hyperion.epub` and `Hyperion.pdf` in the
   same folder — one card with two downloads, or two cards? *Leaning: one card,
   grouped on matched identity, with a format switcher on the download button —
   but two cards is simpler and could ship first.*
2. **Series-first shelving.** Should a 14-book series collapse into one shelf
   tile? Plex does this for seasons. *Leaning: flat in v1, revisit in M7.*
3. **Where the scan root lives.** CLI argument only, or a settings screen that
   can add/remove roots? *Leaning: CLI argument to start, persisted to the DB so
   later runs need no argument.*
4. **Author name normalization.** "Dan Simmons" vs "Simmons, Dan" vs "SIMMONS,
   DAN" will fragment the author filter. Needs at least a normalization pass;
   full author identity resolution is a rabbit hole.

## 18. Verification status (as of 2026-09-03)

What has actually been exercised, and what has not. Kept here because "it's
built" and "it's proven" are different claims.

### Verified

- All four format parsers, against generated fixtures covering the happy path
  plus a corrupt EPUB, an empty PDF, a DRM'd MOBI, a 1×1 spacer cover, and a
  file with no embedded metadata at all.
- Live Open Library enrichment: ISBN lookup, search with confidence gating,
  response caching. A rescan of an unchanged library makes **zero** network
  requests (measured).
- Content identity: a file moved and renamed to `gibberish_final(2).mobi` keeps
  its row, its cover, and its manual edits. No duplicate created.
- Overrides survive a forced rescan, including offline.
- Downloads are byte-identical to source, with Range support.
- LAN binding (`--host 0.0.0.0`), reachable from another address on the network.
- **Docker**: image builds, container reports healthy, full scan runs on Linux,
  and the read-only mount is enforced — an explicit `rm` of a book inside the
  container fails with `Read-only file system`.
- 65 tests, run on macOS.

### Not verified — read before trusting

1. **A genuine RAR has never been read end-to-end.** Creating a `.cbr` test
   fixture requires the proprietary `rar` binary, so the RAR branch of
   `formats/comic.py` has never successfully parsed a real archive. What *is*
   proven: `rarfile` correctly finds the `unar` backend inside the container, a
   `.cbr` that is really a ZIP is handled (dispatch is on magic bytes), and a
   corrupt archive reports an error instead of silently producing a blank book.
   **The first real `.cbr` scan will happen on the server.**
2. **Architecture.** The image was built and tested as `linux/arm64` on an Apple
   Silicon Mac. The Ubuntu server is `linux/amd64` and will pull a different set
   of binary wheels. PyMuPDF and Pillow publish manylinux builds for both, so
   the risk is low, but it is untested.
3. **Scale.** Everything above was measured against a 15-book synthetic library.
   The §14 performance targets have not been tested against a real collection.

### Next steps

- Deploy to the Ubuntu box (M7): `cp .env.example .env`, set `LIBRARY_PATH`,
  `docker compose up -d --build`. Watch the first scan for CBR behaviour.
- Remote access: **undecided, deliberately parked.** Tailscale (node sharing,
  free, requires a VPN client on every device) versus Cloudflare Tunnel +
  Access (browser-only, works for a non-technical guest, more moving parts).
  The deciding question is whether the one person being shared with will
  install a client. Either way the app itself stays auth-free and access
  control lives at the network layer — never port-forward this directly.
- Open question 1 (§17) becomes real once a library with both an EPUB and a PDF
  of the same book is scanned.
