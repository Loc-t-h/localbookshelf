"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from .config import (
    LIBRARY_PATH_VAR,
    Config,
    default_data_dir,
    load_env_file,
    roots_from_env,
)
from .db import Database
from .library import Library
from .scanner import Scanner

log = logging.getLogger(__name__)


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
    serve.add_argument("paths", nargs="*", type=Path,
                       help=f"library folder(s) to index (overrides ${LIBRARY_PATH_VAR})")
    serve.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to allow LAN access")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--offline", action="store_true", help="skip Open Library lookups")
    serve.add_argument("--no-scan", action="store_true", help="don't scan on startup")
    serve.add_argument("--rescan", action="store_true", help="re-parse every file, ignoring the cache")

    scan = sub.add_parser("scan", help="index the library and exit")
    scan.add_argument("paths", nargs="*", type=Path,
                      help=f"library folder(s) to index (overrides ${LIBRARY_PATH_VAR})")
    scan.add_argument("--offline", action="store_true")
    scan.add_argument("--force", action="store_true", help="re-parse and re-enrich everything")

    sub.add_parser("roots", help="show the configured library folders")
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

    # Noted before loading, so `roots` can say where the path really came from.
    library_path_was_exported = LIBRARY_PATH_VAR in os.environ
    env_file = load_env_file()
    if env_file:
        log.debug("read configuration from %s", env_file)

    roots, failed = _resolve_roots(getattr(args, "paths", []))
    if failed:
        return 2

    config = Config(
        data_dir=args.data_dir or default_data_dir(),
        roots=roots,
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
    if command == "roots":
        if not library.roots():
            print(_no_library_message(), file=sys.stderr)
            return 2
        from_env_file = (
            env_file is not None
            and not library_path_was_exported
            and not getattr(args, "paths", [])
        )
        for root in library.roots():
            marker = "" if root.is_dir() else "  (missing)"
            print(f"{root}{marker}")
        if from_env_file:
            print(f"\nfrom {env_file}")
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


def _no_library_message() -> str:
    return (
        f"No library folder configured. Set {LIBRARY_PATH_VAR} in .env "
        "(see .env.example), or pass one: localbookshelf serve ~/Books"
    )


def _resolve_roots(cli_paths: list[Path]) -> tuple[list[Path], bool]:
    """Library folders to use, and whether the configuration was unusable.

    Command line wins over the environment, so a one-off `serve ~/Other` does
    not have to edit .env -- and, unlike the old `add` subcommand, does not
    quietly persist either.

    A folder that is configured but currently absent is a warning, not an
    error: an unplugged drive among several roots should not take the whole
    shelf down. Every root being absent is an error, because that is a typo
    rather than a missing disk.
    """
    roots = [p.expanduser() for p in cli_paths] if cli_paths else roots_from_env()
    if not roots:
        return [], False

    present = [root for root in roots if root.is_dir()]
    if not present:
        print(
            "None of the configured library folders exist:\n  "
            + "\n  ".join(str(root) for root in roots),
            file=sys.stderr,
        )
        return [], True
    for root in roots:
        if root not in present:
            log.warning("library folder is missing, skipping it: %s", root)
    return present, False


def _serve(args, config: Config, db: Database, library: Library) -> int:
    import uvicorn

    from .web.app import create_app

    roots = library.roots()
    scanner = Scanner(config, db, library)
    app = create_app(config, db, library, scanner)

    if roots and not args.no_scan:
        scanner.start(force=args.rescan)
    elif not roots:
        print(_no_library_message(), file=sys.stderr)

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
    if not library.roots():
        print(_no_library_message(), file=sys.stderr)
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
