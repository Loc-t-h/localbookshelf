"""Runtime configuration and platform-specific paths."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

APP_NAME = "localbookshelf"

# Extensions we will attempt to open. Anything else in the library is ignored.
SUPPORTED_EXTENSIONS = {
    ".epub": "epub",
    ".pdf": "pdf",
    ".cbz": "cbz",
    ".cbr": "cbr",
    ".mobi": "mobi",
    ".azw": "mobi",
    ".azw3": "mobi",
}

# Junk that shows up in libraries shared between macOS, Linux and NAS boxes.
IGNORED_DIR_NAMES = {
    "@eaDir",
    "#recycle",
    ".git",
    ".Trash",
    ".Trashes",
    "__MACOSX",
    "lost+found",
}


def default_data_dir() -> Path:
    """Where the database and cover cache live. Never inside the library."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / APP_NAME


@dataclass
class Config:
    data_dir: Path = field(default_factory=default_data_dir)
    roots: list[Path] = field(default_factory=list)
    host: str = "127.0.0.1"
    port: int = 8080
    offline: bool = False

    @property
    def db_path(self) -> Path:
        return self.data_dir / "library.db"

    @property
    def covers_dir(self) -> Path:
        return self.data_dir / "covers"

    @property
    def http_cache_dir(self) -> Path:
        return self.data_dir / "http-cache"

    def ensure_dirs(self) -> None:
        self.covers_dir.mkdir(parents=True, exist_ok=True)
        self.http_cache_dir.mkdir(parents=True, exist_ok=True)
