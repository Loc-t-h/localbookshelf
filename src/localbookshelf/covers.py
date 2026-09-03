"""Cover normalization and the on-disk cover cache.

Covers are keyed by the book's content identity rather than its database id, so
the cache survives a dropped database and a rescan without re-extracting or
re-downloading anything.
"""

from __future__ import annotations

import io
from pathlib import Path

DISPLAY_MAX = 700  # long edge, detail panel
THUMB_MAX = 360  # long edge, grid
WEBP_QUALITY = 82
MIN_DIMENSION = 40  # anything smaller is an icon or a spacer, not a cover


def cover_paths(covers_dir: Path, identity: str) -> tuple[Path, Path]:
    """Return (display, thumbnail) paths, sharded to keep directories small."""
    shard = covers_dir / identity[:2]
    return shard / f"{identity}.webp", shard / f"{identity}.thumb.webp"


def has_cover(covers_dir: Path, identity: str) -> bool:
    display, thumb = cover_paths(covers_dir, identity)
    return display.exists() and thumb.exists()


def store_cover(covers_dir: Path, identity: str, data: bytes) -> bool:
    """Convert raw image bytes into cached WebP renditions.

    Returns False for anything that isn't a usable cover image, which is a
    normal outcome -- EPUBs in particular often hand us a 1x1 spacer GIF.
    """
    if not data or len(data) < 100:
        return False
    try:
        from PIL import Image, ImageOps
    except ImportError:  # pragma: no cover - dependency is declared
        return False

    try:
        with Image.open(io.BytesIO(data)) as image:
            image = ImageOps.exif_transpose(image)
            if image.width < MIN_DIMENSION or image.height < MIN_DIMENSION:
                return False
            if image.mode not in ("RGB", "L"):
                # Flatten transparency onto white rather than onto black.
                if image.mode in ("RGBA", "LA", "P"):
                    image = image.convert("RGBA")
                    background = Image.new("RGB", image.size, (255, 255, 255))
                    background.paste(image, mask=image.split()[-1])
                    image = background
                else:
                    image = image.convert("RGB")

            display, thumb = cover_paths(covers_dir, identity)
            display.parent.mkdir(parents=True, exist_ok=True)

            _save_resized(image, display, DISPLAY_MAX)
            _save_resized(image, thumb, THUMB_MAX)
        return True
    except Exception:
        # Truncated JPEGs, CMYK oddities, decompression-bomb guards -- all of
        # which mean "no cover", never "stop the scan".
        return False


def _save_resized(image, path: Path, max_edge: int) -> None:
    from PIL import Image

    copy = image.copy()
    copy.thumbnail((max_edge, max_edge), Image.LANCZOS)
    copy.save(path, "WEBP", quality=WEBP_QUALITY, method=4)


def delete_cover(covers_dir: Path, identity: str) -> None:
    for path in cover_paths(covers_dir, identity):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
