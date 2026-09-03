"""Generate a sample library covering every format we claim to support.

    uv run python tests/make_fixtures.py /tmp/sample-books

Produces valid EPUB, PDF, CBZ and MOBI files -- plus a few deliberately awkward
ones (no metadata, junk PDF title, a corrupt file, a DRM-flagged MOBI) so the
failure paths get exercised too.
"""

from __future__ import annotations

import io
import random
import struct
import sys
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw


# -- images ----------------------------------------------------------------


def cover_image(title: str, hue: int, size=(600, 900)) -> bytes:
    image = Image.new("RGB", size, _hsl(hue, 0.45, 0.30))
    draw = ImageDraw.Draw(image)
    draw.rectangle([24, 24, size[0] - 24, size[1] - 24], outline=_hsl(hue, 0.5, 0.7), width=3)
    y = size[1] // 2 - 40
    for line in _wrap(title.upper(), 16):
        draw.text((44, y), line, fill=_hsl(hue, 0.25, 0.93))
        y += 34
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=88)
    return buffer.getvalue()


def page_image(number: int, hue: int) -> bytes:
    image = Image.new("RGB", (700, 1000), _hsl(hue, 0.30, 0.88))
    draw = ImageDraw.Draw(image)
    draw.text((40, 40), f"page {number}", fill=(20, 20, 20))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=80)
    return buffer.getvalue()


def _hsl(h, s, l):
    import colorsys

    r, g, b = colorsys.hls_to_rgb(h / 360, l, s)
    return (int(r * 255), int(g * 255), int(b * 255))


def _wrap(text: str, width: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        if len(f"{current} {word}".strip()) <= width:
            current = f"{current} {word}".strip()
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


# -- EPUB ------------------------------------------------------------------


def write_epub(path: Path, *, title, author, description=None, series=None,
               series_index=None, isbn=None, year=None, publisher=None,
               with_cover=True, hue=210, epub3=False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = [f"<dc:title>{_esc(title)}</dc:title>"]
    meta.append(f'<dc:creator opf:role="aut">{_esc(author)}</dc:creator>')
    meta.append("<dc:language>en</dc:language>")
    if description:
        meta.append(f"<dc:description>{_esc(description)}</dc:description>")
    if publisher:
        meta.append(f"<dc:publisher>{_esc(publisher)}</dc:publisher>")
    if year:
        meta.append(f"<dc:date>{year}-01-01</dc:date>")
    if isbn:
        meta.append(f'<dc:identifier opf:scheme="ISBN">urn:isbn:{isbn}</dc:identifier>')
    if series and not epub3:
        meta.append(f'<meta name="calibre:series" content="{_esc(series)}"/>')
        if series_index is not None:
            meta.append(f'<meta name="calibre:series_index" content="{series_index}"/>')
    if series and epub3:
        meta.append(f'<meta property="belongs-to-collection" id="c1">{_esc(series)}</meta>')
        if series_index is not None:
            meta.append(f'<meta refines="#c1" property="group-position">{series_index}</meta>')
    if with_cover:
        meta.append('<meta name="cover" content="cover-img"/>')

    manifest = ['<item id="chap" href="chapter.xhtml" media-type="application/xhtml+xml"/>']
    if with_cover:
        properties = ' properties="cover-image"' if epub3 else ""
        manifest.append(
            f'<item id="cover-img" href="images/cover.jpg" media-type="image/jpeg"{properties}/>'
        )

    opf = f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="{'3.0' if epub3 else '2.0'}" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
    {chr(10).join('    ' + m for m in meta)}
  </metadata>
  <manifest>
    {chr(10).join('    ' + m for m in manifest)}
  </manifest>
  <spine><itemref idref="chap"/></spine>
</package>"""

    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr(
            "OEBPS/chapter.xhtml",
            f"<html xmlns='http://www.w3.org/1999/xhtml'><body><h1>{_esc(title)}</h1>"
            "<p>Sample text.</p></body></html>",
        )
        if with_cover:
            zf.writestr("OEBPS/images/cover.jpg", cover_image(title, hue))


# -- PDF -------------------------------------------------------------------


def write_pdf(path: Path, *, title, author, junk_title=False, pages=3) -> None:
    import fitz

    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    for number in range(pages):
        page = doc.new_page(width=420, height=595)
        page.insert_text((60, 120), title if number == 0 else f"Page {number + 1}", fontsize=20)
        if number == 0 and author:
            page.insert_text((60, 160), author, fontsize=12)
    doc.set_metadata({
        "title": f"{path.stem}.pdf" if junk_title else title,
        "author": "" if junk_title else author,
        "producer": "Microsoft Word",
    })
    doc.save(path)
    doc.close()


# -- CBZ -------------------------------------------------------------------


def write_cbz(path: Path, *, series, number, title=None, writer=None, year=None,
              summary=None, with_info=True, hue=30, pages=4) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        if with_info:
            fields = [f"<Series>{_esc(series)}</Series>", f"<Number>{number}</Number>"]
            if title:
                fields.append(f"<Title>{_esc(title)}</Title>")
            if writer:
                fields.append(f"<Writer>{_esc(writer)}</Writer>")
            if year:
                fields.append(f"<Year>{year}</Year>")
            if summary:
                fields.append(f"<Summary>{_esc(summary)}</Summary>")
            zf.writestr(
                "ComicInfo.xml",
                "<?xml version='1.0'?><ComicInfo>" + "".join(fields) + "</ComicInfo>",
            )
        # Written out of order on purpose: natural sort must still find page 1.
        for index in [10, 2, 1, 3][:pages]:
            zf.writestr(f"page{index}.jpg", page_image(index, hue))


# -- MOBI ------------------------------------------------------------------


def write_mobi(path: Path, *, title, author, description=None, publisher=None,
               isbn=None, year=None, with_cover=True, hue=280, drm=False) -> None:
    """Build a minimal but structurally valid PalmDB/MOBI file."""
    path.parent.mkdir(parents=True, exist_ok=True)

    exth_records = []
    if author:
        exth_records.append((100, author.encode("utf-8")))
    if publisher:
        exth_records.append((101, publisher.encode("utf-8")))
    if description:
        exth_records.append((103, description.encode("utf-8")))
    if isbn:
        exth_records.append((104, isbn.encode("utf-8")))
    if year:
        exth_records.append((106, f"{year}-01-01".encode("utf-8")))
    if with_cover:
        exth_records.append((201, struct.pack(">I", 0)))  # first image record
    exth_records.append((503, title.encode("utf-8")))

    exth_body = b"".join(
        struct.pack(">II", rec_type, len(data) + 8) + data for rec_type, data in exth_records
    )
    exth = b"EXTH" + struct.pack(">II", len(exth_body) + 12, len(exth_records)) + exth_body
    exth += b"\0" * ((4 - len(exth) % 4) % 4)

    mobi_header_length = 232
    full_name = title.encode("utf-8")
    full_name_offset = 16 + mobi_header_length + len(exth)

    text_record = b"Sample book text.\0"
    image_records = [cover_image(title, hue)] if with_cover else []
    first_image_index = 2  # record 0 is the header, record 1 is the text

    header = bytearray(mobi_header_length)
    header[0:4] = b"MOBI"
    struct.pack_into(">I", header, 4, mobi_header_length)
    struct.pack_into(">I", header, 8, 2)          # book
    struct.pack_into(">I", header, 12, 65001)     # utf-8
    struct.pack_into(">I", header, 16, 0xDEADBEEF)
    struct.pack_into(">I", header, 20, 6)         # file version
    struct.pack_into(">I", header, 64, full_name_offset)
    struct.pack_into(">I", header, 68, len(full_name))
    struct.pack_into(">I", header, 84, 6)         # min version
    struct.pack_into(">I", header, 88, first_image_index)
    struct.pack_into(">I", header, 108, 0x40)     # EXTH present

    palmdoc = struct.pack(">HHIHHHH", 1, 0, len(text_record), 1, 4096, 2 if drm else 0, 0)
    record0 = palmdoc + bytes(header) + exth + full_name
    record0 += b"\0" * ((4 - len(record0) % 4) % 4)

    records = [record0, text_record] + image_records
    count = len(records)
    offset = 78 + count * 8 + 2  # record list, then a two-byte gap by convention

    entries = bytearray()
    positions = []
    cursor = offset
    for index in range(count):
        positions.append(cursor)
        entries += struct.pack(">IBBH", cursor, 0, 0, index)
        cursor += len(records[index])

    name = title.encode("ascii", "replace")[:31].ljust(32, b"\0")
    palmdb = (
        name
        + struct.pack(">HH", 0, 0)
        + struct.pack(">IIIIII", 0, 0, 0, 0, 0, 0)
        + b"BOOKMOBI"
        + struct.pack(">II", 0, 0)
        + struct.pack(">H", count)
    )
    assert len(palmdb) == 78, len(palmdb)

    path.write_bytes(palmdb + bytes(entries) + b"\0\0" + b"".join(records))


def _esc(text: str) -> str:
    return (
        str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


# -- the library ------------------------------------------------------------


def build(root: Path) -> None:
    random.seed(7)
    root.mkdir(parents=True, exist_ok=True)

    write_epub(
        root / "Dan Simmons" / "Hyperion.epub",
        title="Hyperion", author="Dan Simmons",
        description="On the world called Hyperion, beyond the reach of galactic law, "
                    "waits a creature called the Shrike.",
        series="Hyperion Cantos", series_index=1, isbn="9780553283686",
        year=1989, publisher="Bantam Spectra", hue=205,
    )
    write_epub(
        root / "Dan Simmons" / "The Fall of Hyperion.epub",
        title="The Fall of Hyperion", author="Dan Simmons",
        series="Hyperion Cantos", series_index=2, year=1990,
        publisher="Bantam Spectra", hue=190,
    )
    write_epub(
        root / "Ursula K. Le Guin" / "The Dispossessed.epub",
        title="The Dispossessed", author="Ursula K. Le Guin",
        description="Shevek, a brilliant physicist, decides to take action.",
        isbn="9780061054884", year=1974, hue=25, epub3=True,
        series="Hainish Cycle", series_index=6,
    )
    # No embedded metadata at all: everything must come from the filename.
    write_epub(
        root / "loose" / "Le Guin, Ursula K. - The Left Hand of Darkness (1969).epub",
        title="", author="", with_cover=False, hue=300,
    )
    # Cover present, but a 1x1 spacer that must be rejected.
    _write_tiny_cover_epub(root / "loose" / "Ted Chiang - Exhalation.epub")

    write_pdf(root / "manuals" / "Structure and Interpretation of Computer Programs.pdf",
              title="Structure and Interpretation of Computer Programs",
              author="Harold Abelson")
    write_pdf(root / "manuals" / "Kernighan, Brian - The C Programming Language.pdf",
              title="ignored", author="ignored", junk_title=True)

    write_cbz(root / "comics" / "Saga 001.cbz", series="Saga", number=1,
              title="Chapter One", writer="Brian K. Vaughan", year=2012,
              summary="Two soldiers from opposite sides of a war fall in love.", hue=15)
    write_cbz(root / "comics" / "Saga 002.cbz", series="Saga", number=2,
              writer="Brian K. Vaughan", year=2012, hue=340)
    write_cbz(root / "comics" / "Bone #12 - The Great Cow Race.cbz",
              series="", number=0, with_info=False, hue=60)

    write_mobi(root / "kindle" / "Neuromancer.mobi", title="Neuromancer",
               author="William Gibson", publisher="Ace Books", year=1984,
               isbn="9780441569595",
               description="Case was the sharpest data-thief in the Matrix.", hue=170)
    write_mobi(root / "kindle" / "Snow Crash.azw3", title="Snow Crash",
               author="Neal Stephenson", year=1992, hue=280)
    write_mobi(root / "kindle" / "Locked Book.mobi", title="Locked Book",
               author="Nobody", drm=True, with_cover=False)

    # Things that must not stop a scan.
    (root / "broken").mkdir(exist_ok=True)
    (root / "broken" / "truncated.epub").write_bytes(b"PK\x03\x04 this is not a zip")
    (root / "broken" / "empty.pdf").write_bytes(b"")
    (root / "broken" / "notes.txt").write_text("ignored: unsupported extension")
    (root / ".DS_Store").write_bytes(b"\0\0\0\1Bud1")
    (root / "._Hyperion.epub").write_bytes(b"AppleDouble resource fork")

    print(f"Sample library written to {root}")
    total = sum(1 for p in root.rglob("*") if p.is_file())
    print(f"{total} files")


def _write_tiny_cover_epub(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    Image.new("RGB", (1, 1), (255, 255, 255)).save(buffer, "GIF")
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="content.opf" media-type="application/oebps-package+xml"/>'
            "</rootfiles></container>",
        )
        zf.writestr(
            "content.opf",
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            "<dc:title>Exhalation</dc:title><dc:creator>Ted Chiang</dc:creator>"
            '<meta name="cover" content="c"/></metadata>'
            '<manifest><item id="c" href="spacer.gif" media-type="image/gif"/></manifest>'
            "<spine/></package>",
        )
        zf.writestr("spacer.gif", buffer.getvalue())


if __name__ == "__main__":
    build(Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/sample-books").expanduser())
