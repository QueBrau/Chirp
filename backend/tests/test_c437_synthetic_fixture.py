from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
try:
    from c437_synthetic_fixture import main
finally:
    sys.path.pop(0)


def test_fixture_defaults_to_print_only_and_strict_prefix(capsys):
    assert main(["--project", "chirps-prod", "--bucket", "chirp-media", "--nonce", "abc12345"]) == 0
    packet = json.loads(capsys.readouterr().out)
    assert packet["mode"] == "PRINT_ONLY_NOT_EXECUTED"
    assert packet["prefix"].startswith("posts/c437-test-")
    assert len(packet["synthetic_objects"]) == 3
    assert all(name.startswith(packet["prefix"]) for name in packet["synthetic_objects"])


@pytest.mark.parametrize("args", [
    ["--project", "chirps-prod", "--bucket", "chirp-media", "--nonce", "bad/nonce"],
    ["--project", "chirps-prod", "--bucket", "chirp-media", "--nonce", "abc12345", "--execute"],
])
def test_fixture_rejects_unsafe_or_unapproved_execution(args, capsys):
    assert main(args) == 2
    assert json.loads(capsys.readouterr().out)["mode"] == "ERROR"
