"""Bounded image derivatives at finalize time (board card c374).

finalize_media_object() no longer moves an uploaded photo's bytes unexamined into
posts/ or avatars/ - board c132's copy_blob was replaced by a Pillow pipeline that
decodes every upload, strips its EXIF, downscales it to a 1600px long edge, and
re-encodes it as a JPEG at quality 82 (see storage_service's module docstring,
"DERIVATIVES, NOT COPIES", and finalize_media_object's own docstring).

These tests are unit-level, not router-level: _decode_and_normalize_image and
_build_media_derivative are pure functions on bytes and are exercised directly here,
and finalize_media_object is exercised against a minimal fake bucket with no database
or HTTP client involved. The router-level move/validation/orphan-on-failure behavior
already has its own coverage in test_media_url_validation.py and test_profile_picture.py
and is not duplicated here.

FALSIFICATION, NOT JUST NO-CRASH: every test asserts a specific, exactly-derivable
property of the OUTPUT bytes (a precise pixel dimension, an absent EXIF tag, a
particular pixel's approximate color, a specific rejected-content error) rather than
merely that a call succeeded or raised something. A pipeline that silently skipped the
resize, or "stripped EXIF" by deleting only the orientation tag, would still pass a
no-crash test and must not pass these.
"""

from __future__ import annotations

import io
import logging
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from PIL import ExifTags, Image

from app.services import storage_service


def _jpeg_bytes(
    size: tuple[int, int], color: tuple[int, int, int] = (120, 60, 200), exif=None
) -> bytes:
    buffer = io.BytesIO()
    kwargs = {} if exif is None else {"exif": exif.tobytes()}
    Image.new("RGB", size, color).save(buffer, format="JPEG", **kwargs)
    return buffer.getvalue()


def _png_bytes_half_transparent(size: tuple[int, int]) -> bytes:
    """Top half opaque red, bottom half fully transparent - the compositing test's
    fixture. paste() with no mask overwrites pixels outright rather than blending,
    which is exactly what a two-region fixture needs."""
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    top = Image.new("RGBA", (size[0], size[1] // 2), (220, 20, 20, 255))
    image.paste(top, (0, 0))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _png_bytes_palette_with_transparency(size: tuple[int, int]) -> bytes:
    """A real palette-mode (P) PNG carrying a tRNS chunk - the OTHER transparency shape
    _decode_and_normalize_image must composite onto white, distinct from the RGBA/LA
    fixture above (common real-world shape: many simple-graphics/sticker PNGs save this
    way). Index 0 is the transparent background, index 1 opaque red - same top/bottom
    two-region layout as _png_bytes_half_transparent, for the same assertion style."""
    image = Image.new("P", size, 0)
    image.putpalette([255, 255, 255, 220, 20, 20] + [0, 0, 0] * 254)
    top = Image.new("P", (size[0], size[1] // 2), 1)
    top.putpalette(image.getpalette())
    image.paste(top, (0, 0))
    image.info["transparency"] = 0
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# _decode_and_normalize_image / _build_media_derivative - pure functions on bytes
# ---------------------------------------------------------------------------


def test_exif_orientation_is_baked_in_and_the_full_exif_block_is_stripped():
    """Orientation 6 means "rotate 90 CW to display correctly" - a 4000x3000 source
    must come out the OTHER way around (long edge now vertical) once that rotation is
    physically applied, and the 1600px long-edge cap then applies to THAT dimension,
    not the original one. Make/Model prove the strip is the whole EXIF block, not just
    the orientation tag exif_transpose() itself removes."""
    exif = Image.Exif()
    exif[ExifTags.Base.Orientation] = 6
    exif[ExifTags.Base.Make] = "TestCam"
    exif[ExifTags.Base.Model] = "Model X"
    source = _jpeg_bytes((4000, 3000), exif=exif)

    derivative = storage_service._build_media_derivative(source)
    out = Image.open(io.BytesIO(derivative))
    out.load()

    assert out.format == "JPEG"
    # 4000x3000 rotated 90 by orientation 6 -> 3000x4000, then long-edge-1600:
    # scale = 1600/4000 = 0.4 -> (1200, 1600).
    assert out.size == (1200, 1600)
    assert "exif" not in out.info
    assert dict(out.getexif()) == {}


def test_an_already_small_image_is_re_encoded_but_not_upscaled():
    source = _jpeg_bytes((40, 30))
    derivative = storage_service._build_media_derivative(source)
    out = Image.open(io.BytesIO(derivative))
    out.load()
    assert out.format == "JPEG"
    assert out.size == (40, 30)


def test_an_oversized_image_is_downscaled_to_exactly_a_1600px_long_edge():
    source = _jpeg_bytes((3200, 1600))
    derivative = storage_service._build_media_derivative(source)
    out = Image.open(io.BytesIO(derivative))
    out.load()
    assert out.size == (1600, 800)


def test_transparency_is_composited_onto_white_not_left_black():
    source = _png_bytes_half_transparent((40, 40))
    derivative = storage_service._build_media_derivative(source)
    out = Image.open(io.BytesIO(derivative)).convert("RGB")

    opaque_pixel = out.getpixel((20, 5))  # top quarter: was opaque red
    transparent_pixel = out.getpixel((20, 35))  # bottom quarter: was fully transparent

    assert opaque_pixel[0] > 180 and opaque_pixel[1] < 80 and opaque_pixel[2] < 80, opaque_pixel
    assert all(channel > 220 for channel in transparent_pixel), (
        f"transparent region did not composite onto white: got {transparent_pixel}"
    )


def test_palette_mode_transparency_is_also_composited_onto_white():
    source = _png_bytes_palette_with_transparency((40, 40))
    derivative = storage_service._build_media_derivative(source)
    out = Image.open(io.BytesIO(derivative)).convert("RGB")

    opaque_pixel = out.getpixel((20, 5))  # top quarter: was opaque red (palette index 1)
    transparent_pixel = out.getpixel((20, 35))  # bottom quarter: was the transparent index

    assert opaque_pixel[0] > 180 and opaque_pixel[1] < 80 and opaque_pixel[2] < 80, opaque_pixel
    assert all(channel > 220 for channel in transparent_pixel), (
        f"transparent region did not composite onto white: got {transparent_pixel}"
    )


def test_undecodable_bytes_are_rejected_as_a_400_not_a_crash():
    with pytest.raises(HTTPException) as excinfo:
        storage_service._build_media_derivative(b"this is not an image, just text")
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == "invalid_media_content"


def test_a_decompression_bomb_is_rejected_as_a_400_and_logged_distinctly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A small, highly-compressible file can still decode to an enormous pixel count.
    This fixture (400 megapixels) is more than 2x Image.MAX_IMAGE_PIXELS, so Pillow's
    own DecompressionBombError fires natively during image.load() - the redundant
    safety-net branch in _decode_and_normalize_image, not the explicit 1x-threshold
    check (see the test below for the gap that check exists to close)."""
    bomb = Image.new("RGB", (20000, 20000), (10, 10, 10))
    buffer = io.BytesIO()
    bomb.save(buffer, format="PNG", optimize=True)
    bomb_bytes = buffer.getvalue()
    assert len(bomb_bytes) < storage_service.MAX_UPLOAD_BYTES, (
        "fixture must itself pass the compressed-size cap - the pixel-count guard is "
        "the thing under test, not a redundant check of MAX_UPLOAD_BYTES"
    )

    with caplog.at_level(logging.WARNING, logger=storage_service.logger.name):
        with pytest.raises(HTTPException) as excinfo:
            storage_service._build_media_derivative(bomb_bytes)
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == "invalid_media_content"
    assert "decompression bomb" in "\n".join(r.getMessage() for r in caplog.records)


def test_a_bomb_in_pillows_warn_only_1x_to_2x_gap_is_still_rejected():
    """Pillow's own DecompressionBombError only fires above 2x MAX_IMAGE_PIXELS; between
    1x and 2x it only emits an unenforced DecompressionBombWarning that nothing escalates
    to an exception. A 10000x10000 (100-megapixel) solid-color PNG sits squarely in that
    gap - above 1x (~89.5M px) but under 2x (~179M px) - and would decode and re-encode
    successfully with NO rejection at all if this code relied on Pillow's default
    behavior alone. This is the actual regression test for that gap, exercising the
    explicit pixel-count check in _decode_and_normalize_image, not the test above's
    Pillow-native >2x raise."""
    size = 10000
    assert Image.MAX_IMAGE_PIXELS < size * size < 2 * Image.MAX_IMAGE_PIXELS, (
        "fixture must sit inside Pillow's 1x-2x warn-only gap for this test to mean "
        "anything - otherwise it's indistinguishable from the >2x test above"
    )
    bomb = Image.new("RGB", (size, size), (10, 10, 10))
    buffer = io.BytesIO()
    bomb.save(buffer, format="PNG", optimize=True)
    bomb_bytes = buffer.getvalue()
    assert len(bomb_bytes) < storage_service.MAX_UPLOAD_BYTES

    with pytest.raises(HTTPException) as excinfo:
        storage_service._build_media_derivative(bomb_bytes)
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == "invalid_media_content"


# ---------------------------------------------------------------------------
# finalize_media_object - the two GCS legs (download, then upload), each with their
# own 400/502 split, and the always-.jpg destination naming
# ---------------------------------------------------------------------------


class _FakeBlob:
    def __init__(self, name: str, captured: dict) -> None:
        self.name = name
        self._captured = captured

    def download_as_bytes(self) -> bytes:
        if self._captured.get("download_404"):
            from google.api_core.exceptions import NotFound

            raise NotFound("simulated missing tmp object")
        if self._captured.get("download_error"):
            raise TimeoutError("simulated transient GCS error")
        return self._captured.get("source_bytes") or _jpeg_bytes((40, 30))

    def upload_from_string(self, data: bytes, **kwargs) -> None:
        if self._captured.get("upload_error"):
            raise RuntimeError("simulated destination write failure")
        self._captured.setdefault("upload_calls", []).append(
            {"new_name": self.name, "data": data, "kwargs": kwargs}
        )

    def delete(self) -> None:
        self._captured.setdefault("deleted", []).append(self.name)


class _FakeBucket:
    def __init__(self, captured: dict) -> None:
        self._captured = captured

    def blob(self, name: str) -> _FakeBlob:
        return _FakeBlob(name, self._captured)


def _install(monkeypatch: pytest.MonkeyPatch, captured: dict) -> None:
    monkeypatch.setattr(storage_service, "_bucket_name", lambda: "test-c374-bucket")
    fake_bucket = _FakeBucket(captured)
    monkeypatch.setattr(
        storage_service,
        "_storage_client",
        lambda: SimpleNamespace(bucket=lambda name: fake_bucket),
    )


def test_finalize_always_writes_a_jpg_regardless_of_the_uploaded_extension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}
    _install(monkeypatch, captured)

    url = storage_service.finalize_media_object("user-1", "tmp/user-1/photo.webp")

    assert url == "https://storage.googleapis.com/test-c374-bucket/posts/user-1/photo.jpg"
    [call] = captured["upload_calls"]
    assert call["new_name"] == "posts/user-1/photo.jpg"
    assert call["kwargs"]["content_type"] == "image/jpeg"
    assert call["kwargs"]["if_generation_match"] == 0
    # The bytes actually handed to GCS are a real, valid JPEG - not just a name that
    # claims so.
    written = Image.open(io.BytesIO(call["data"]))
    written.load()
    assert written.format == "JPEG"
    assert captured["deleted"] == ["tmp/user-1/photo.webp"]


def test_finalize_downscales_a_real_oversized_upload_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {"source_bytes": _jpeg_bytes((3200, 1600))}
    _install(monkeypatch, captured)

    storage_service.finalize_media_object("user-1", "tmp/user-1/big.jpg")

    written = Image.open(io.BytesIO(captured["upload_calls"][0]["data"]))
    written.load()
    assert written.size == (1600, 800)


def test_finalize_rejects_corrupt_image_content_as_400_without_touching_the_upload_leg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {"source_bytes": b"not an image"}
    _install(monkeypatch, captured)

    with pytest.raises(HTTPException) as excinfo:
        storage_service.finalize_media_object("user-1", "tmp/user-1/photo.jpg")
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == "invalid_media_content"
    # The bad content was caught before GCS was ever touched again: no upload
    # attempted, and the tmp source is left alone for the lifecycle rule - same as any
    # other aborted finalize (see finalize_media_object's docstring).
    assert "upload_calls" not in captured
    assert "deleted" not in captured


def test_finalize_missing_tmp_object_is_still_a_400(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {"download_404": True}
    _install(monkeypatch, captured)

    with pytest.raises(HTTPException) as excinfo:
        storage_service.finalize_media_object("user-1", "tmp/user-1/gone.jpg")
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == "media_upload_not_found"


def test_finalize_other_download_failures_are_a_502_not_a_400_or_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A branch this refactor introduced: unlike the old single copy_blob() call, the
    download leg now has its own failure mode distinct from a 404 - a transient or
    permission error reading the tmp object must not be confused with the object
    simply not existing."""
    captured = {"download_error": True}
    _install(monkeypatch, captured)

    with pytest.raises(HTTPException) as excinfo:
        storage_service.finalize_media_object("user-1", "tmp/user-1/photo.jpg")
    assert excinfo.value.status_code == 502
    assert excinfo.value.detail == "media_finalize_failed"


def test_finalize_upload_leg_failure_is_a_502(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {"upload_error": True}
    _install(monkeypatch, captured)

    with pytest.raises(HTTPException) as excinfo:
        storage_service.finalize_media_object("user-1", "tmp/user-1/photo.jpg")
    assert excinfo.value.status_code == 502
    assert excinfo.value.detail == "media_finalize_failed"


def test_finalize_uses_the_identical_pipeline_for_avatars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """destination_prefix is the only thing that varies between the post-media and
    avatar call sites (board c221/c374) - this pins that the derivative pipeline
    itself does not branch on it."""
    captured = {"source_bytes": _jpeg_bytes((3200, 1600))}
    _install(monkeypatch, captured)

    url = storage_service.finalize_media_object(
        "user-1", "tmp/user-1/portrait.png", destination_prefix=storage_service.AVATAR_PREFIX
    )
    assert url == "https://storage.googleapis.com/test-c374-bucket/avatars/user-1/portrait.jpg"
    written = Image.open(io.BytesIO(captured["upload_calls"][0]["data"]))
    written.load()
    assert written.size == (1600, 800)
