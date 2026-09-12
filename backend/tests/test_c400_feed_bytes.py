"""Opt-in evidence harness: actual feed media bytes after c374's derivative pipeline
(board card c400, the backend-measurable half split out of c374's original acceptance
item 3).

RUN THIS FILE ON ITS OWN, NEVER AS PART OF A NORMAL SUITE RUN:

    CHIRP_MEDIA_EVIDENCE=1 .venv/bin/python -m pytest tests/test_c400_feed_bytes.py -q

Every test below is skipped unless CHIRP_MEDIA_EVIDENCE=1 (see pytestmark), matching
tests/test_c364_query_plans.py's convention: this produces a dated evidence file, it is
not a correctness gate that should run on every commit.

WRITE PATH (board c404): the command above writes its artefact OUTSIDE the repo, then
compares its STRUCTURE (which image shapes were measured, the hypothesis range, whether
window-determinism was recorded - never exact bytes, which legitimately vary by Pillow
build) against the committed infra/evidence/c400-feed-bytes-2026-09-12.json, and fails
only if that structure no longer matches. See tests/_evidence_write.py for why. To
deliberately regenerate the COMMITTED file itself:

    CHIRP_MEDIA_EVIDENCE=1 CHIRP_EVIDENCE_OUT=infra/evidence/c400-feed-bytes-2026-09-12.json \
        .venv/bin/python -m pytest tests/test_c400_feed_bytes.py -q

DRIFT GUARD, same standard c364 holds itself to: nothing here reimplements the
derivative pipeline. Every measurement calls the actual production function,
storage_service._build_media_derivative(), on real bytes this module builds - never a
hand-computed estimate of what it would produce.

WHAT "REPRESENTATIVE" MEANS HERE, stated honestly rather than assumed: this repo has no
corpus of real phone photos to measure against, so _synthetic_photo_bytes() below
builds Gaussian-noise images blurred into smoother, more photograph-like spatial
correlation (raw per-pixel noise compresses far WORSE under JPEG than any real photo,
since JPEG's DCT exploits the spatial correlation a blur restores) at a spread of real
phone-camera resolutions and aspect ratios (12MP 4:3 landscape/portrait, a 16:9 shape,
and one already-small case). This is a defensible approximation, not a substitute for
measuring real uploads - PERFORMANCE-EVIDENCE.md-style caveat, stated here rather than
left implicit.

SECOND MEASUREMENT: capability-url window determinism. c374/c140's whole cache-hit
argument depends on mint_media_token() producing a BYTE-IDENTICAL token for the same
(object, viewer) within one storage_service._revocation_window(), and a DIFFERENT one
once the window rolls over. That determinism is backend-measurable and is exactly the
PRECONDITION for any client-side cache hit; it is not proof of an actual RN cache hit
rate (device-dependent, out of scope here - see the card's remaining open half) and is
not presented as one.
"""
from __future__ import annotations

import io
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageFilter

from app import config
from app.services import storage_service

from . import _evidence_write

pytestmark = pytest.mark.skipif(
    os.environ.get("CHIRP_MEDIA_EVIDENCE") != "1",
    reason="opt-in evidence harness (board c400) -- set CHIRP_MEDIA_EVIDENCE=1 to run",
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_PATH = REPO_ROOT / "infra" / "evidence" / "c400-feed-bytes-2026-09-12.json"
REGENERATE_COMMAND = (
    "CHIRP_MEDIA_EVIDENCE=1 CHIRP_EVIDENCE_OUT=infra/evidence/c400-feed-bytes-2026-09-12.json "
    ".venv/bin/python -m pytest tests/test_c400_feed_bytes.py -q"
)

# Real phone-camera shapes: 12MP landscape/portrait 4:3 (the most common shape by far),
# a 16:9-ish wide shot, and one already-small case (a re-share or a low-end camera) that
# should NOT be upscaled. Not exhaustive - representative of the spread this app's
# compose flow actually accepts (ALLOWED_CONTENT_TYPES: jpeg/png/webp; MAX_UPLOAD_BYTES
# bounds compressed size, not these dimensions).
REPRESENTATIVE_SHAPES: list[tuple[str, int, int]] = [
    ("12mp_landscape_4x3", 4032, 3024),
    ("12mp_portrait_4x3", 3024, 4032),
    ("8mp_landscape_4x3", 3264, 2448),
    ("wide_16x9", 4032, 2268),
    ("small_already_under_cap", 1200, 900),
]

# The card's original hypothesis (c374's acceptance item 1): "initial hypothesis
# 100-300KB feed images". Checked, not assumed.
HYPOTHESIS_MIN_BYTES = 100_000
HYPOTHESIS_MAX_BYTES = 300_000


def _synthetic_photo_bytes(width: int, height: int, seed: int) -> bytes:
    """A JPEG with genuine spatial correlation, not a solid color and not raw noise.

    Image.effect_noise() gives real per-pixel variance (unlike a trivially-compressible
    solid fill); GaussianBlur afterward restores the spatial correlation a real photo
    has and raw noise does not, which is what actually governs JPEG compression ratio.
    seed varies the noise per fixture so the five images are not identical after blur.
    """
    channels = [
        Image.effect_noise((width, height), 48 + (seed * 7) % 40).point(
            lambda p, s=seed: (p + s * 13) % 256
        )
        for _ in range(3)
    ]
    image = Image.merge("RGB", channels).filter(ImageFilter.GaussianBlur(radius=3))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def _configure_signing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable media_signing_enabled() so mint_media_token()/media_capability_url() run
    for real rather than 503-ing - this harness needs no bucket or network, only the
    HMAC secret and a base url, both pure-local config."""
    settings = config.get_settings()
    monkeypatch.setattr(settings, "media_signing_secret", "evidence-harness-not-a-real-secret")
    monkeypatch.setattr(settings, "app_public_base_url", "https://chirps-prod.example")


def _structural_signature(payload: dict[str, Any]) -> dict[str, Any]:
    """The SET of things this run measured - never a timing or a byte count.

    Board c404: this is what gets compared against the committed file. A
    changed image_labels set or hypothesis range means the committed file no
    longer describes the same thing the harness measures, which is a real
    regression; the actual byte counts are expected to vary by Pillow build
    and are compared nowhere near this function.
    """
    return {
        "image_labels": sorted(e["label"] for e in payload["images"]),
        "hypothesis_range_bytes": payload["hypothesis_range_bytes"],
        "has_window_determinism": bool(payload.get("capability_url_window_determinism")),
    }


@pytest.fixture(scope="module", autouse=True)
def _write_evidence_on_teardown(request: pytest.FixtureRequest):
    request.node.session._c400_evidence: list[dict[str, Any]] = []  # type: ignore[attr-defined]
    request.node.session._c400_window_evidence: dict[str, Any] = {}  # type: ignore[attr-defined]
    yield
    entries = getattr(request.node.session, "_c400_evidence", [])
    window_evidence = getattr(request.node.session, "_c400_window_evidence", {})
    if not entries:
        return
    total_original = sum(e["original_bytes"] for e in entries)
    total_derivative = sum(e["derivative_bytes"] for e in entries)
    payload = {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "method": (
            "Synthetic Gaussian-noise-then-blurred JPEGs at representative phone-camera "
            "shapes, run through the real storage_service._build_media_derivative() - "
            "not a substitute for measuring real uploads. See module docstring."
        ),
        "regenerate_command": REGENERATE_COMMAND,
        "hypothesis_range_bytes": [HYPOTHESIS_MIN_BYTES, HYPOTHESIS_MAX_BYTES],
        "images": entries,
        "feed_page_totals": {
            "image_count": len(entries),
            "total_original_bytes": total_original,
            "total_derivative_bytes": total_derivative,
            "total_reduction_pct": round(100 * (1 - total_derivative / total_original), 1)
            if total_original
            else None,
        },
        "capability_url_window_determinism": window_evidence,
    }

    out_path = _evidence_write.evidence_output_path(EVIDENCE_PATH)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")

    if out_path == EVIDENCE_PATH:
        return  # deliberate write to the committed path - nothing to compare against

    committed = _evidence_write.load_committed(EVIDENCE_PATH)
    if committed is None:
        return  # no committed file yet - a future deliberate run creates it
    diff = _evidence_write.structural_diff(_structural_signature(committed), _structural_signature(payload))
    if diff:
        report = "\n".join(
            [
                f"c400 evidence ({out_path}) no longer matches what the committed "
                f"file ({EVIDENCE_PATH}) describes:",
                *[f"  - {line}" for line in diff],
                "",
                f"If this is a deliberate, reviewed change: {REGENERATE_COMMAND}",
            ]
        )
        pytest.fail(report, pytrace=False)


@pytest.mark.parametrize("label,width,height", REPRESENTATIVE_SHAPES)
def test_derivative_bytes_vs_pre_c374_original(
    request: pytest.FixtureRequest, label: str, width: int, height: int
) -> None:
    """Real production function, real (synthetic-photo) bytes in, real bytes out."""
    original_bytes = _synthetic_photo_bytes(width, height, seed=width + height)
    derivative_bytes = storage_service._build_media_derivative(original_bytes)
    out = Image.open(io.BytesIO(derivative_bytes))
    out.load()

    entry = {
        "label": label,
        "source_dimensions": [width, height],
        "derivative_dimensions": list(out.size),
        "original_bytes": len(original_bytes),
        "derivative_bytes": len(derivative_bytes),
        "reduction_pct": round(100 * (1 - len(derivative_bytes) / len(original_bytes)), 1),
        "within_hypothesis_range": HYPOTHESIS_MIN_BYTES <= len(derivative_bytes) <= HYPOTHESIS_MAX_BYTES,
    }
    request.node.session._c400_evidence.append(entry)  # type: ignore[attr-defined]

    # Real assertions, not just evidence-dumping: the derivative must never be LARGER
    # than the original (the whole point of bounding it), and must respect the long-edge
    # cap this module's own constant defines - both properties c374 claims, checked here
    # against the actual bytes rather than trusted from the implementation.
    assert len(derivative_bytes) < len(original_bytes), (
        f"{label}: derivative ({len(derivative_bytes)}B) is not smaller than the "
        f"original ({len(original_bytes)}B)"
    )
    assert max(out.size) <= storage_service.DERIVATIVE_MAX_DIMENSION, (
        f"{label}: derivative dimensions {out.size} exceed the long-edge cap"
    )
    # UPPER bound only, not the lower one: within_hypothesis_range was being computed
    # into the evidence file above but never actually asserted, which means nothing in
    # this repo would catch DERIVATIVE_JPEG_QUALITY drifting upward (82 -> 95 roughly
    # doubles feed bytes) - every other assertion here still passes on a quality bump
    # since it only checks dimensions, and the evidence file would quietly go stale
    # instead (found in review, chirps-17). A derivative landing UNDER 100KB is good
    # news, not a regression, so only the upper bound is a real gate; the lower bound
    # stays evidence-only in the recorded within_hypothesis_range field above.
    assert len(derivative_bytes) <= HYPOTHESIS_MAX_BYTES, (
        f"{label}: derivative is {len(derivative_bytes)}B, over the {HYPOTHESIS_MAX_BYTES}B "
        "feed-image ceiling - check DERIVATIVE_JPEG_QUALITY/DERIVATIVE_MAX_DIMENSION "
        "before assuming this is just fixture drift"
    )


def test_capability_url_is_identical_within_one_window_and_differs_across_windows(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The precondition for any client-side cache hit: real, backend-measurable, not
    the RN cache hit rate itself (device-dependent, out of scope here)."""
    _configure_signing(monkeypatch)
    object_name = "posts/evidence-harness-user/deadbeef-feed.jpg"
    viewer_id = "evidence-harness-viewer"
    window = storage_service._revocation_window()

    window_start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    same_window_later = window_start + (window / 2)
    next_window = window_start + window + timedelta(seconds=1)

    token_a = storage_service.mint_media_token(object_name, viewer_id, now=window_start)
    token_b = storage_service.mint_media_token(object_name, viewer_id, now=same_window_later)
    token_c = storage_service.mint_media_token(object_name, viewer_id, now=next_window)

    request.node.session._c400_window_evidence.update(  # type: ignore[attr-defined]
        {
            "revocation_window_hours": window.total_seconds() / 3600,
            "same_window_tokens_identical": token_a == token_b,
            "cross_window_tokens_differ": token_a != token_c,
            "note": (
                "Byte-identical tokens within one window are the PRECONDITION for RN's "
                "Image cache (which keys on the url string) to hit; a token change is a "
                "structural, unavoidable miss at the window boundary, not one this "
                "pipeline could recover - see MEDIA_TOKEN_TTL's 2x-window margin for why "
                "that boundary is rare in practice (module docstring, signed-reads "
                "section)."
            ),
        }
    )

    assert token_a == token_b, "same (object, viewer, window) must mint a byte-identical token"
    assert token_a != token_c, "crossing a window boundary must mint a different token"
