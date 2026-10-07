from __future__ import annotations

import io

import pytest
from PIL import Image

from beluno.malware import ScannerUnavailable, parse_reply
from beluno.media_files import JPEG, PDF, PNG, WEBP, RejectedFile, clean, sniff


def encoded(fmt: str, size: tuple[int, int] = (40, 20), **options: object) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(output, format=fmt, **options)
    return output.getvalue()


def test_the_bytes_decide_the_type_not_the_name() -> None:
    assert sniff(encoded("JPEG")) == JPEG
    assert sniff(encoded("PNG")) == PNG
    assert sniff(encoded("WEBP")) == WEBP
    assert sniff(b"%PDF-1.7\n") == PDF
    assert sniff(b"GIF89a") is None
    assert sniff(b"MZ\x90\x00") is None


def test_images_keep_their_kind_and_lose_every_tag() -> None:
    exif = Image.Exif()
    exif[0x010F] = "Camera maker"
    for fmt, expected in (("PNG", PNG), ("WEBP", WEBP), ("JPEG", JPEG)):
        result = clean(encoded(fmt, exif=exif.tobytes()), allow_pdf=False)
        assert (result.content_type, result.width, result.height) == (expected, 40, 20)
        with Image.open(io.BytesIO(result.data)) as image:
            assert dict(image.getexif()) == {}


def test_heic_becomes_jpeg_and_rotation_is_applied() -> None:
    heic = encoded("HEIF")
    result = clean(heic, allow_pdf=False)
    assert (result.content_type, result.width, result.height) == (JPEG, 40, 20)
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90 degrees when shown
    rotated = clean(encoded("JPEG", exif=exif.tobytes()), allow_pdf=False)
    assert (rotated.width, rotated.height) == (20, 40)


def test_pdfs_only_as_receipts_and_bombs_refused() -> None:
    assert clean(b"%PDF-1.4\n%%EOF", allow_pdf=True).content_type == PDF
    with pytest.raises(RejectedFile) as refused:
        clean(b"%PDF-1.4\n%%EOF", allow_pdf=False)
    assert refused.value.reason == "type"
    bomb = encoded("PNG", size=(9000, 9000))
    with pytest.raises(RejectedFile) as exploded:
        clean(bomb, allow_pdf=False)
    assert exploded.value.reason == "unreadable"
    big = clean(encoded("PNG", size=(5000, 1000)), allow_pdf=False)
    assert (big.width, big.height) == (4096, 819)


def test_clamd_replies() -> None:
    assert parse_reply("stream: OK").clean
    found = parse_reply("stream: Eicar-Test-Signature FOUND")
    assert (found.clean, found.signature) == (False, "Eicar-Test-Signature")
    too_large = parse_reply("INSTREAM size limit exceeded. ERROR")
    assert (too_large.clean, too_large.too_large) == (False, True)
    with pytest.raises(ScannerUnavailable):
        parse_reply("stream: lstat() failed. ERROR")


def test_comments_and_colour_profiles_are_dropped_too() -> None:
    from PIL import ImageCms

    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    jpeg = clean(encoded("JPEG", comment=b"shot at 12 Main St"), allow_pdf=False)
    assert b"Main St" not in jpeg.data
    png = clean(encoded("PNG", icc_profile=profile), allow_pdf=False)
    with Image.open(io.BytesIO(png.data)) as image:
        assert "icc_profile" not in image.info


def test_files_that_do_not_decode_are_refused_not_retried() -> None:
    sample = bytearray(encoded("WEBP", exif=b"Exif\x00\x00garbage"))
    for corrupt in (bytes(sample[:40]), b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\xff" * 64):
        with pytest.raises(RejectedFile) as refused:
            clean(corrupt, allow_pdf=False)
        assert refused.value.reason == "unreadable"
    # A PNG body behind JPEG magic is decoded only as JPEG, and fails.
    with pytest.raises(RejectedFile):
        clean(b"\xff\xd8\xff" + encoded("PNG"), allow_pdf=False)
