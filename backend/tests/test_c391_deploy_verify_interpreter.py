"""scripts/deploy-verify must run under an interpreter that can complete TLS (c391).

Deploy windows #13 and #14 (Sep 8) ran the wrapper through PATH's python3, the
python.org 3.13 framework build whose default trust store is empty until its
Install Certificates step is run. Every https probe failed at the TLS layer,
reported status 0, and the verdict read NOT_READY for a deployment that was fine.
tests/test_c361_deploy_verify_cli.py could not see this: its fixtures are plain
http, so a certless interpreter passes them (59/59 under the old wrapper).

These tests drive the real wrapper as a subprocess. The certless condition is
CONSTRUCTED by pointing OpenSSL's default paths at nothing (SSL_CERT_FILE and
SSL_CERT_DIR), and asserted to exist before it is relied on; the resolution order
is exercised against shims in a sandbox copy of scripts/, never against whatever
this machine happens to have on PATH.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
WRAPPER = ROOT / "scripts" / "deploy-verify"
CERTLESS = {
    "SSL_CERT_FILE": "/nonexistent/c391/cert.pem",
    "SSL_CERT_DIR": "/nonexistent/c391/certs",
}
CA_COUNT = "import ssl; print(ssl.create_default_context().cert_store_stats()['x509_ca'])"


def _clean_env(extra: dict[str, str]) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "CHIRP_PYTHON" and k not in CERTLESS}
    env.update(extra)
    return env


def _run(wrapper: Path, extra: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(wrapper), *args], env=_clean_env(extra), text=True, capture_output=True, timeout=60
    )


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


def _sandbox(tmp_path: Path) -> tuple[Path, Path]:
    """A copy of the wrapper and its module with NO backend/.venv beside them, plus a
    bin/ whose python3 is this interpreter, so PATH fallback resolves deterministically."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("deploy-verify", "deploy_verify.py"):
        (scripts / name).write_bytes((ROOT / "scripts" / name).read_bytes())
    (scripts / "deploy-verify").chmod(0o755)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "python3").symlink_to(sys.executable)
    assert not (tmp_path / "backend" / ".venv").exists(), "the sandbox must start without a venv"
    return scripts / "deploy-verify", bindir


def test_certless_interpreter_is_refused_before_any_probe() -> None:
    assert _ca_count({}) > 0, "the test interpreter itself must hold CA certificates"
    if _ca_count(CERTLESS) != 0:
        pytest.skip("this platform ignores SSL_CERT_FILE/SSL_CERT_DIR; cannot construct a certless interpreter")

    result = _run(WRAPPER, {"CHIRP_PYTHON": sys.executable, **CERTLESS}, "--help")

    assert result.returncode == 2, result.stderr
    assert result.stdout == "", "nothing may run, not even --help: no report, no usage text"
    assert "no CA certificates" in result.stderr
    assert sys.executable in result.stderr, "the refusal must name the interpreter it rejected"


def test_chirp_python_override_beats_the_checkout_venv(tmp_path: Path) -> None:
    wrapper, bindir = _sandbox(tmp_path)
    venv_marker = tmp_path / "venv-chosen"
    _shim(tmp_path / "backend" / ".venv" / "bin" / "python", venv_marker)
    override_marker = tmp_path / "override-chosen"
    override = _shim(tmp_path / "override" / "python", override_marker)

    result = _run(wrapper, {"CHIRP_PYTHON": str(override), "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}"}, "--help")

    assert result.returncode == 0, result.stderr
    assert override_marker.exists() and not venv_marker.exists()
    assert result.stderr.splitlines()[0] == f"deploy-verify: interpreter {override}"
    assert "usage:" in result.stdout


def test_checkout_venv_beats_path_python3(tmp_path: Path) -> None:
    wrapper, bindir = _sandbox(tmp_path)
    venv_marker = tmp_path / "venv-chosen"
    _shim(tmp_path / "backend" / ".venv" / "bin" / "python", venv_marker)

    result = _run(wrapper, {"PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}"}, "--help")

    assert result.returncode == 0, result.stderr
    assert venv_marker.exists()
    assert result.stderr.splitlines()[0].endswith("backend/.venv/bin/python")
    assert "usage:" in result.stdout


def test_without_override_or_venv_falls_back_to_path_python3(tmp_path: Path) -> None:
    wrapper, bindir = _sandbox(tmp_path)

    result = _run(wrapper, {"PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}"}, "--help")

    assert result.returncode == 0, result.stderr
    assert result.stderr.splitlines()[0] == f"deploy-verify: interpreter {bindir / 'python3'}"
    assert "usage:" in result.stdout
