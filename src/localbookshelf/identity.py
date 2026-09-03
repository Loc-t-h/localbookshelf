"""Content-based file identity.

The identity hash is the primary key for a book, not its path. A file that is
renamed or moved within the library keeps its identity, and therefore keeps its
manual metadata overrides and its downloaded cover.

We hash the first 64 KiB plus the file size rather than the whole file: a full
hash of a 5,000-book library is minutes of I/O, most of it over a network mount,
for no practical gain in collision resistance on this kind of data.
"""

from __future__ import annotations

import hashlib
import unicodedata
from pathlib import Path

HASH_PREFIX_BYTES = 64 * 1024


def file_identity(path: Path, size: int | None = None) -> str:
    """Return a stable hex identity for a file's contents."""
    if size is None:
        size = path.stat().st_size
    digest = hashlib.sha256()
    digest.update(str(size).encode("ascii"))
    digest.update(b"\0")
    with path.open("rb") as handle:
        digest.update(handle.read(HASH_PREFIX_BYTES))
    return digest.hexdigest()


def normalize_text(value: str) -> str:
    """Normalize to NFC.

    macOS stores filenames decomposed (NFD) and Linux composed (NFC), so the
    same file yields different Python strings on the two machines. Everything
    that is compared, searched, or stored goes through here first.
    """
    return unicodedata.normalize("NFC", value)
