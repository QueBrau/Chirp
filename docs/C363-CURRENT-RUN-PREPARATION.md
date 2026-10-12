# c363 current run preparation

This document records read-only preparation for a future mixed HTTP/WebSocket
load run. It is not a load result, capacity signoff, or permission to send
traffic. The private packet at
`.ticket-evidence/c363-packet-20261012/packet.json` is the source of the
observed Cloud Run values and file hashes.

The current services were observed in `chirps-prod/us-central1` at 2026-10-12:

- `chirp-api` is serving `chirp-api-data1011-824fe74` at 100% with image
  digest `sha256:12db1f54ffdfd3dea3070f5fab67bac35f7b2539d3774943e10627a52dd9d451`,
  concurrency 80, min/max scale 1/8, and `chirp-vpc` private-ranges-only egress.
- `chirp-ws` is serving `chirp-ws-data1011-824fe74` at 100% with the same image
  digest, concurrency 200, min/max scale 1/2, and the same VPC settings.

The serving image has not been proven to correspond to this packet's detached
source commit. That provenance must be bound to the reviewed build record before
execution. The Firebase QA manifest, short-lived tokens, throwaway
campus/chapter, fresh deployment verification inputs, T-30 baselines, and
two-operator watcher are also absent. The packet therefore remains `NOT_READY`.

When those inputs exist, use the existing `scripts/deploy-verify` authenticated
contract first, then invoke `backend/.venv/bin/python -m loadtest` with a Firebase
manifest and `--confirm-park-lifted`. Preserve the approved 02:00–05:00 ET quiet
window, staged WS/HTTP phases, abort thresholds, connection and Cloud Run/Redis
watchers, and marker-based cleanup in `backend/loadtest/RUNBOOK.md`. Never put
the bearer or Firebase tokens in commands, reports, or tracked files.

The resulting run must retain phase reports, watcher observations, cleanup and
post-run deployment verification. A local harness pass, authenticated readiness,
or this preparation does not establish production capacity, receipt delivery,
recovery, HA, or device acceptance.
