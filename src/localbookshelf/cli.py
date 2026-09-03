"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .config import Config, default_data_dir
from .db import Database
from .library import Library
from .scanner import Scanner


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="localbookshelf",
        description="A bookshelf view for a folder of books.",
    )
    parser.add_argument("--data-dir", type=Path, default=None,
                        help=f"where the database and covers live (default: {default_data_dir()})")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the web UI")
    serve.add_argument("paths", nargs="*", type=Path, help="library folder(s) to index")
    serve.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to allow LAN access")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--offline", action="store_true", help="skip Open Library lookups")
    serve.add_argument("--no-scan", action="store_true", help="don't scan on startup")
    serve.add_argument("--rescan", action="store_true", help="re-parse every file, ignoring the cache")

    scan = sub.add_parser("scan", help="index the library and exit")
    scan.add_argument("paths", nargs="*", type=Path)
    scan.add_argument("--offline", action="store_true")
    scan.add_argument("--force", action="store_true", help="re-parse and re-enrich everything")

    add = sub.add_parser("add", help="remember a library folder")
    add.add_argument("paths", nargs="+", type=Path)

    remove = sub.add_parser("remove", help="forget a library folder")
    remove.add_argument("paths", nargs="+", type=Path)

    sub.add_parser("roots", help="list configured library folders")
    sub.add_parser("status", help="show library statistics")

    export = sub.add_parser("export-overrides", help="write manual edits to stdout as JSON")
    export.add_argument("-o", "--output", type=Path)

    importer = sub.add_parser("import-overrides", help="restore manual edits from JSON")
    importer.add_argument("input", type=Path)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("PIL").setLevel(logging.WARNING)

    config = Config(
        data_dir=args.data_dir or default_data_dir(),
        offline=getattr(args, "offline", False),
    )
    config.ensure_dirs()
    db = Database(config.db_path)
    library = Library(db, config)

    command = args.command or "serve"
    if command == "serve":
        return _serve(args, config, db, library)
    if command == "scan":
        return _scan(args, config, db, library)
    if command == "add":
        for path in args.paths:
            _add_root(library, path)
        return 0
    if command == "remove":
        for path in args.paths:
            library.remove_root(path)
            print(f"Removed {path}")
        return 0
    if command == "roots":
        roots = library.roots()
        if not roots:
            print("No library folders configured. Try: localbookshelf add ~/Books")
        for root in roots:
            marker = "" if root.is_dir() else "  (missing)"
            print(f"{root}{marker}")
        return 0
    if command == "status":
        return _status(library)
    if command == "export-overrides":
        payload = json.dumps(library.export_overrides(), indent=2)
        if args.output:
            args.output.write_text(payload)
            print(f"Wrote {args.output}")
        else:
            print(payload)
        return 0
    if command == "import-overrides":
        records = json.loads(args.input.read_text())
        count = library.import_overrides(records)
        print(f"Imported {count} overrides. Run a scan to apply them.")
        return 0

    parser.print_help()
    return 1


def _add_root(library: Library, path: Path) -> bool:
    resolved = path.expanduser().resolve()
    if not resolved.is_dir():
        print(f"Not a directory: {resolved}", file=sys.stderr)
        return False
    library.add_root(resolved)
    return True


def _serve(args, config: Config, db: Database, library: Library) -> int:
    import uvicorn

    from .web.app import create_app

    for path in args.paths:
        if not _add_root(library, path):
            return 2

    roots = library.roots()
    scanner = Scanner(config, db, library)
    app = create_app(config, db, library, scanner)

    if roots and not args.no_scan:
        scanner.start(force=args.rescan)
    elif not roots:
        print("No library folder yet. Pass one: localbookshelf serve ~/Books", file=sys.stderr)

    print("\n  📚 LocalBookshelf")
    if args.host in ("0.0.0.0", "::"):
        # Bound to every interface, so print the address other devices can use.
        print(f"     local:   http://localhost:{args.port}")
        lan = _lan_address()
        if lan:
            print(f"     network: http://{lan}:{args.port}")
    else:
        print(f"     url:     http://{args.host}:{args.port}")
    for root in roots:
        print(f"     library: {root}")
    print(f"     data:    {config.data_dir}\n")

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def _lan_address() -> str | None:
    """This machine's address on the local network.

    Opens a UDP socket toward a documentation address to see which interface
    the kernel would route through. No packets are actually sent.
    """
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 1))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def _scan(args, config: Config, db: Database, library: Library) -> int:
    for path in args.paths:
        if not _add_root(library, path):
            return 2
    if not library.roots():
        print("No library folder configured.", file=sys.stderr)
        return 2

    scanner = Scanner(config, db, library)
    scanner.start(force=args.force, block=True)
    state = scanner.state.snapshot()
    print(state.get("message") or "Done.")
    return 0


def _status(library: Library) -> int:
    stats = library.stats()
    roots = library.roots()
    print(f"Library folders: {', '.join(str(r) for r in roots) or 'none'}")
    print(f"Books:           {stats['books']}")
    print(f"Total size:      {stats['bytes'] / 1_073_741_824:.2f} GB")
    for entry in stats["formats"]:
        print(f"  {entry['format']:<6} {entry['n']}")
    if stats["no_cover"]:
        print(f"Without covers:  {stats['no_cover']}")
    if stats["needs_review"]:
        print(f"Needs review:    {stats['needs_review']}")
    if stats["errors"]:
        print(f"With problems:   {stats['errors']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
