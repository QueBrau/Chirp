"""Every scripts/ entry point must resolve its interpreter the same way (c392).

c391 fixed this only for scripts/deploy-verify: `exec python3` picked up the
python.org 3.13 framework build on the deploy operator's Mac, whose default
trust store is EMPTY until its Install Certificates step is run, so every
https probe failed at the TLS layer and the verdict read NOT_READY for a
deployment that was fine. The other five entry points (deployment-config,
monitoring-check, network-audit, spend-report, and recovery-check's own
`#!/usr/bin/env python3` shebang) carried the identical defect. c392 moves the
fix into scripts/lib/pick-python.sh and puts every wrapper on it.

These tests drive the real wrappers as subprocesses, each against a sandbox
copy of scripts/ that carries the one wrapper under test, every scripts/*.py
module, and the shared helper - never the real repo's backend/.venv or PATH
python3, so the resolution order is exercised against constructed shims, not
whatever this machine happens to have.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "scripts" / "lib" / "pick-python.sh"
CERTLESS = {
    "SSL_CERT_FILE": "/nonexistent/c392/cert.pem",
    "SSL_CERT_DIR": "/nonexistent/c392/certs",
}
CA_COUNT = "import ssl; print(ssl.create_default_context().cert_store_stats()['x509_ca'])"

WRAPPERS = [
    ("deploy-verify", "deploy_verify.py"),
    ("deployment-config", "deployment_config.py"),
    ("monitoring-check", "monitoring_check.py"),
    ("monitoring-apply", "monitoring_apply.py"),
    ("network-audit", "network_audit.py"),
    ("spend-report", "spend_report.py"),
    ("recovery-check", "recovery_check.py"),
    ("local-db-encoding", "local_db_encoding.py"),
]
WRAPPER_NAMES = [name for name, _ in WRAPPERS]


def _clean_env(extra: dict[str, str]) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "CHIRP_PYTHON" and k not in CERTLESS}
    env.update(extra)
    return env


def _ca_count(extra: dict[str, str]) -> int:
    result = subprocess.run(
        [sys.executable, "-c", CA_COUNT],
        env=_clean_env(extra), text=True, capture_output=True, timeout=60, check=True,
    )
    return int(result.stdout.strip())


def _shim(path: Path, marker: Path) -> Path:
    """An executable that records it was chosen, then defers to the real interpreter."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\n: > "{marker}"\nexec "{sys.executable}" "$@"\n')
    path.chmod(0o755)
    return path


def _sandbox(tmp_path: Path, name: str) -> Path:
    """A copy of one wrapper, every scripts/*.py module (deployment_config.py
    imports deploy_verify.py, and network_audit.py imports deployment_config.py,
    so all six must be present regardless of which wrapper is under test), and the
    shared helper, with NO backend/.venv beside them, so PATH/CHIRP_PYTHON
    resolution is deterministic."""
    scripts = tmp_path / "scripts"
    (scripts / "lib").mkdir(parents=True)
    (scripts / name).write_bytes((ROOT / "scripts" / name).read_bytes())
    (scripts / name).chmod(0o755)
    for _wrapper_name, module in WRAPPERS:
        (scripts / module).write_bytes((ROOT / "scripts" / module).read_bytes())
    (scripts / "lib" / "pick-python.sh").write_bytes(LIB.read_bytes())
    assert not (tmp_path / "backend" / ".venv").exists(), "the sandbox must start without a venv"
    return scripts / name


def _run(wrapper: Path, extra: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(wrapper), *args], env=_clean_env(extra), text=True, capture_output=True, timeout=60
    )


@pytest.mark.parametrize("name,module", WRAPPERS, ids=WRAPPER_NAMES)
def test_override_shim_is_chosen_and_cited(tmp_path: Path, name: str, module: str) -> None:
    wrapper = _sandbox(tmp_path, name)
    marker = tmp_path / "override-chosen"
    override = _shim(tmp_path / "override" / "python", marker)

    result = _run(wrapper, {"CHIRP_PYTHON": str(override)}, "--help")

    assert marker.exists(), "the override shim must actually run, not merely be selected in principle"
    assert result.stderr.splitlines()[0] == f"{name}: interpreter {override}", result.stderr
    assert result.returncode == 0, result.stderr
    assert "usage" in result.stdout.lower()


@pytest.mark.parametrize("name,module", WRAPPERS, ids=WRAPPER_NAMES)
def test_certless_interpreter_refused(tmp_path: Path, name: str, module: str) -> None:
    assert _ca_count({}) > 0, "the test interpreter itself must hold CA certificates"
    if _ca_count(CERTLESS) != 0:
        pytest.skip("this platform ignores SSL_CERT_FILE/SSL_CERT_DIR; cannot construct a certless interpreter")
    wrapper = _sandbox(tmp_path, name)

    result = _run(wrapper, {"CHIRP_PYTHON": sys.executable, **CERTLESS}, "--help")

    assert result.returncode == 2, result.stderr
    assert result.stdout == "", "nothing may run, not even --help: no report, no usage text"
    assert "no CA certificates" in result.stderr
    assert sys.executable in result.stderr, "the refusal must name the interpreter it rejected"


def test_every_wrapper_sources_the_shared_helper() -> None:
    source_line = 'source "$here/lib/pick-python.sh"'
    for name, _module in WRAPPERS:
        text = (ROOT / "scripts" / name).read_text()
        assert source_line in text, f"{name} does not source the shared resolver"
        assert "x509_ca" not in text, f"{name} carries its own preflight copy instead of the shared one"
