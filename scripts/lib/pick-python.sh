# Shared interpreter resolver for scripts/ wrappers (c392, generalising c391).
#
# c391 found that `exec python3` in scripts/deploy-verify picked up the
# python.org 3.13 framework build on the deploy operator's Mac, which ships
# with an EMPTY default trust store until its Install Certificates step is
# run; every https probe then failed at the TLS layer, reported status 0, and
# the verdict read NOT_READY/auth_gate for a deployment that was fine (deploy
# windows #13 and #14, Sep 8). c392 found the same bare `exec python3` in five
# more entry points (deployment-config, monitoring-check, network-audit,
# spend-report, and recovery-check's own shebang) and moves the fix here so
# every wrapper resolves and validates its interpreter the same way instead of
# carrying its own copy.
#
# Resolution order: $CHIRP_PYTHON, then this checkout's backend/.venv/bin/python
# (located relative to THIS FILE, never the caller's cwd, so the wrapper works
# no matter where it is invoked from), then python3 on PATH. Whichever wins
# must prove it holds CA certificates before a single probe is sent.
#
# Usage (sourced, not executed):
#   here="$(cd "$(dirname "$0")" && pwd)"
#   source "$here/lib/pick-python.sh"
#   python=$(chirp_pick_python <wrapper-name>) || exit $?
#   exec "$python" "$here/<module>.py" "$@"

chirp_pick_python() {
  local wrapper_name="$1"
  local lib_dir python
  lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [ -n "${CHIRP_PYTHON:-}" ]; then
    python="$CHIRP_PYTHON"
  elif [ -x "$lib_dir/../../backend/.venv/bin/python" ]; then
    python="$lib_dir/../../backend/.venv/bin/python"
  else
    python="$(command -v python3)"
  fi
  if ! "$python" -c 'import ssl, sys; sys.exit(0 if ssl.create_default_context().cert_store_stats()["x509_ca"] > 0 else 1)' 2>/dev/null; then
    printf '%s: %s has no CA certificates in its default trust store; every https probe would fail before reaching the service. Set CHIRP_PYTHON to an interpreter with certificates (the repo backend/.venv/bin/python works).\n' "$wrapper_name" "$python" >&2
    return 2
  fi
  printf '%s: interpreter %s\n' "$wrapper_name" "$python" >&2
  printf '%s\n' "$python"
}
