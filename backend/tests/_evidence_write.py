"""Shared write-path and structural-diff helpers for opt-in evidence harnesses
(board card c404).

WHAT WAS WRONG: test_c364_query_plans.py and test_c400_feed_bytes.py each wrote
straight to their COMMITTED infra/evidence/*.json path on every run, so any
experimental or sabotaged run silently overwrote the file other cards cite as
their source (c364's is cited as the 9.2ms search number in PERFORMANCE-
EVIDENCE.md; c400's is the 79.5% reduction). Found live, not theorized: raising
DERIVATIVE_JPEG_QUALITY to falsify c400's byte-ceiling assertion rewrote the
committed file with the sabotaged numbers, caught only by an incidental
`git status` before it could ride along with an unrelated commit.

THE OBVIOUS FIX TRADES ONE SILENT FAILURE FOR ANOTHER. Just not writing on a
plain run means: the code changes, nobody remembers to re-run with the write
flag, and the numbers other cards cite quietly drift from reality with nothing
ever failing — the committed file goes STALE instead of SABOTAGED, which is
the same defect in a different coat. So a plain opt-in run now:
  1. writes its artefact OUTSIDE the repo (proven by `git status` staying clean
     — c404 acceptance criterion 1);
  2. reads the existing committed file, if any;
  3. compares STRUCTURE against it — never exact numbers, since timings and
     byte counts legitimately vary by machine, Postgres version, Pillow build;
  4. FAILS if the committed file no longer describes the same SET of things
     the harness measures (a query family or image shape missing/added, a
     cardinality parameter changed) — that is not staleness, it is wrong;
  5. only REPORTS (prints, does not fail) a purely numeric difference.

To deliberately update the committed file, set CHIRP_EVIDENCE_OUT to its own
path — see each harness's own module docstring for the exact command, and see
the `regenerate_command` field this module writes into every evidence payload,
so a reader holding a stale file knows precisely how to refresh it rather than
guessing at environment variables (chirps-17, review).
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def evidence_output_path(committed_path: Path) -> Path:
    """Where THIS run writes its evidence artefact.

    Default: outside the repo entirely (the system tempdir), so a plain opt-in
    run never touches git. Set CHIRP_EVIDENCE_OUT to `committed_path` itself
    (or any path) to deliberately write there instead — a human decision, made
    on purpose, the same way c364/c402's own ceilings ask to be raised on
    purpose rather than silently.
    """
    override = os.environ.get("CHIRP_EVIDENCE_OUT")
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / committed_path.name


def load_committed(committed_path: Path) -> dict[str, Any] | None:
    """The existing committed file's contents, or None if it doesn't exist yet
    (a first-ever run, or a harness that hasn't shipped its file yet)."""
    if not committed_path.exists():
        return None
    return json.loads(committed_path.read_text(encoding="utf-8"))


def structural_diff(committed: dict[str, Any] | None, fresh: dict[str, Any]) -> list[str]:
    """Human-readable differences between two STRUCTURAL signatures.

    Both signatures must already be reduced by the CALLER to only the fields
    that matter — never raw timings or byte counts (see each harness's own
    `_structural_signature()` for what it keeps and why). A returned non-empty
    list means the committed file no longer describes the same set of things
    the harness measures, and the caller should treat that as a real failure,
    not a report — everything reaching this function is, by construction,
    something a plain numeric drift would not have put here.
    """
    if committed is None:
        return ["no committed evidence file exists yet - nothing to compare against"]
    lines: list[str] = []
    for key in sorted(set(committed) | set(fresh)):
        old, new = committed.get(key), fresh.get(key)
        if isinstance(old, list) and isinstance(new, list):
            old_set, new_set = set(old), set(new)
            missing, added = old_set - new_set, new_set - old_set
            if missing:
                lines.append(f"{key}: committed has {sorted(missing)} the fresh run does not")
            if added:
                lines.append(f"{key}: fresh run has {sorted(added)} the committed file does not")
        elif old != new:
            lines.append(f"{key}: committed={old!r} fresh={new!r}")
    return lines
