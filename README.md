# LocalBookshelf

A bookshelf view for a folder of books. Point it at a directory, get covers and
metadata. No reader, no library manager, no accounts.

```
localbookshelf serve ~/Books
  → http://localhost:8080
```

**The library folder is read-only.** LocalBookshelf never renames, moves,
rewrites, converts or deletes anything in it. All state lives in a separate data
directory; delete it and a rescan rebuilds everything.

See [localbookshelf_specs.md](localbookshelf_specs.md) for the full spec and the
reasoning behind the scope.

## Install

Requires Python 3.11+.

```bash
uv sync                      # or: pipx install -e .
uv run localbookshelf serve ~/Books
```

Optional, for CBR comics: `brew install unar` (macOS) / `apt install unar` (Ubuntu).

## Usage

```bash
localbookshelf serve ~/Books          # index and serve (default command)
localbookshelf serve --host 0.0.0.0   # allow other devices on the LAN
localbookshelf serve --offline        # no Open Library lookups
localbookshelf serve --rescan         # re-parse every file, ignoring the cache

localbookshelf scan                   # index and exit
localbookshelf status                 # library statistics
localbookshelf add ~/Comics           # remember another folder
localbookshelf roots                  # list configured folders

localbookshelf export-overrides -o edits.json   # back up manual metadata edits
localbookshelf import-overrides edits.json
```

Folders passed to `serve` or `scan` are remembered, so later runs need no
argument.

## Running on your network

By default the server binds to `127.0.0.1`, reachable only from the machine it
runs on. To reach it from other devices, bind to every interface:

```bash
localbookshelf serve ~/Books --host 0.0.0.0
```

It prints the address to use from your phone or laptop:

```
  📚 LocalBookshelf
     local:   http://localhost:8080
     network: http://192.168.50.3:8080
```

There is **no authentication** — anyone on your network can browse and download
the whole library. That is the intended trade-off for a home LAN. Do not port
forward this to the internet; see "Remote access" below.

## Docker (Ubuntu server)

```bash
cp .env.example .env      # set LIBRARY_PATH to your books folder
docker compose up -d --build
```

The shelf is then at `http://<server-ip>:8080`.

Two things worth knowing about the compose file:

- **The library is mounted read-only** (`read_only: true` on the bind mount).
  The app never writes to your books, and this makes that a guarantee the
  kernel enforces rather than a promise the code keeps.
- **The database and cover cache live in a named volume**, not a bind mount,
  because SQLite's write-ahead log misbehaves on NFS/SMB.

The image runs as UID/GID 1000, which matches the first user account on a stock
Ubuntu install. If your books are owned by someone else, uncomment the `user:`
line in `docker-compose.yml` — and run `docker compose down -v` first, since the
data volume takes its ownership from the image when it is first created.

```bash
docker compose logs -f            # follow
docker compose exec localbookshelf localbookshelf --data-dir /data status
docker compose restart            # rescans on startup
```

## Remote access

Do **not** forward a port to this. It has no authentication, it serves file
paths, and it will hand your entire library to anyone who finds it.

Put access control in front of it instead — the app stays small and auth lives
at the network layer:

- **Tailscale (recommended).** Install on the server and your devices; reach the
  shelf at its tailnet address. Nothing is publicly exposed at all.
- **Cloudflare Tunnel + Cloudflare Access.** No open inbound ports; you get a
  real hostname on your domain with an identity check at the edge. Best if you
  want browser access without installing a VPN client on every device.

## What it does

- Scans EPUB, PDF, CBZ/CBR, MOBI/AZW3 recursively.
- Reads embedded metadata first, then falls back to parsing the filename, then
  fills the remaining gaps from Open Library.
- Extracts a cover from the file (page 1 for PDFs, first page for comics), and
  downloads one only when the file has none.
- Serves a cover grid with search, sort and filters, a detail panel, and a
  download button.
- Remembers manual metadata edits by file *content*, so they survive renames,
  moves, and a deleted database.

## Keyboard

| Key | Action |
| --- | --- |
| `/` | Focus search |
| `Esc` | Close the detail panel |
| `Enter` | Open the focused book |

## Layout

```
src/localbookshelf/
├── cli.py          entry point
├── config.py       paths, supported extensions
├── db.py           SQLite schema
├── identity.py     content hashing (the primary key for a book)
├── filename.py     "Author - Series 03 - Title (2011).epub" heuristics
├── covers.py       cover cache, WebP renditions
├── enrich.py       Open Library client, matching, confidence
├── library.py      reads and writes, override application
├── scanner.py      the two-phase scan pipeline
├── formats/        epub, pdf, comic, mobi parsers
└── web/            FastAPI routes, templates, CSS/JS
```

## Data directory

- macOS: `~/Library/Application Support/localbookshelf`
- Linux: `~/.local/share/localbookshelf`

Override with `--data-dir`. It holds `library.db` and the cover cache; nothing is
written inside your library folder.
