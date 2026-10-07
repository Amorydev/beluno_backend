"""Recognise uploaded files by their bytes and rewrite images without metadata.

The declared content type is never trusted: the first bytes decide. Images are
decoded and encoded again, which drops every EXIF, XMP, and GPS field (after
applying the camera's rotation); HEIC becomes JPEG. PDFs (receipts only) are kept
as they are. Decoding is bounded (pixel count) against decompression bombs.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import pillow_heif
from PIL import Image, ImageOps

pillow_heif.register_heif_opener()

MAX_PIXELS = 60_000_000  # e.g. 10000 x 6000; larger frames are refused
Image.MAX_IMAGE_PIXELS = MAX_PIXELS
MAX_EDGE = 4096  # stored images are scaled down to fit
JPEG_QUALITY = 88

JPEG, PNG, WEBP, HEIC, PDF = (
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/heic",
    "application/pdf",
)
IMAGE_TYPES = frozenset({JPEG, PNG, WEBP, HEIC})


class RejectedFile(ValueError):
    """The file is not an accepted type, or it cannot be read as one."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason  # "type" or "unreadable"


@dataclass(frozen=True)
class CleanFile:
    data: bytes
    content_type: str
    width: int | None
    height: int | None


def sniff(data: bytes) -> str | None:
    """The content type the first bytes show, among the accepted ones."""

    if data.startswith(b"\xff\xd8\xff"):
        return JPEG
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return PNG
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return WEBP
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"mif1", b"msf1", b"heim"):
        return HEIC
    if data.startswith(b"%PDF-"):
        return PDF
    return None


def clean(data: bytes, *, allow_pdf: bool) -> CleanFile:
    found = sniff(data)
    if found == PDF and allow_pdf:
        return CleanFile(data, PDF, None, None)
    if found not in IMAGE_TYPES:
        raise RejectedFile("type")
    return _reencode(data, found)


# Pillow's names for the sniffed types: it never guesses another decoder.
PILLOW_FORMATS = {JPEG: "JPEG", PNG: "PNG", WEBP: "WEBP", HEIC: "HEIF"}


def _reencode(data: bytes, found: str) -> CleanFile:
    try:
        with Image.open(io.BytesIO(data), formats=[PILLOW_FORMATS[found]]) as source:
            # Pillow only raises past twice its limit: check the header before decoding.
            if source.width * source.height > MAX_PIXELS:
                raise RejectedFile("unreadable")
            source.load()
            image = ImageOps.exif_transpose(source)
    except RejectedFile:
        raise
    except Exception as error:
        # A file that does not decode cleanly is refused, never retried.
        raise RejectedFile("unreadable") from error
    image.thumbnail((MAX_EDGE, MAX_EDGE))
    # Nothing travels from the original: no EXIF, XMP, ICC profile, or comments.
    image.info.clear()
    output = io.BytesIO()
    if found == PNG:
        target = PNG
        image.save(output, format="PNG", optimize=True)
    elif found == WEBP:
        target = WEBP
        image.save(output, format="WEBP", quality=JPEG_QUALITY)
    else:
        # JPEG, and HEIC (which many viewers cannot show) become JPEG.
        target = JPEG
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        image.save(output, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return CleanFile(output.getvalue(), target, image.width, image.height)
