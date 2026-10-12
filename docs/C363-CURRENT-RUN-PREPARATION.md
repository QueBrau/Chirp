# c363 current run preparation

This document records read-only preparation for a future mixed HTTP/WebSocket
load run. It is not a load result, capacity signoff, or permission to send
traffic. The private packet at
`.ticket-evidence/c363-packet-20261012/packet.json` is the source of the
observed Cloud Run values and file hashes.

The current services were read from `chirps-prod/us-central1` on 2026-10-12. The
raw `gcloud` outputs are private files with local capture bounds of 15:43:27
EDT (API), 15:43:29 EDT (WS), and 15:44:22 EDT (packet); these are not claimed
to be authoritative provider observation timestamps.

- `chirp-api` is serving `chirp-api-data1011-824fe74` at 100% with image
  digest `sha256:12db1f54ffdfd3dea3070f5fab67bac35f7b2539d3774943e10627a52dd9d451`,
  concurrency 80, min/max scale 1/8, and `chirp-vpc` private-ranges-only egress.
- `chirp-ws` is serving `chirp-ws-data1011-824fe74` at 100% with the same image
  digest, concurrency 200, min/max scale 1/2, and the same VPC settings.

The packet records both the service `latestReady`/100%-traffic values and
separate named-revision reads showing Ready, matching image, concurrency, and
timeout. These are private read-only captures, not a fresh authenticated
`deploy-verify` result. Prior release evidence records schema 0043; the current
schema head and runtime contract still require a fresh authenticated check.

The serving digest is bound by the prior private release record to source
commit `824fe74c7fdd032a473ecf422ff0c62e28dcd447` and image digest
`sha256:12db1f54ffdfd3dea3070f5fab67bac35f7b2539d3774943e10627a52dd9d451`.
The packet's detached harness commit is `0154b6f5a66e0ec6945c2870c3068c34183b0d8b`;
that is a separate source comparison and is not claimed to be the serving
source. The Firebase QA manifest, short-lived tokens, throwaway
campus/chapter, fresh deployment verification inputs, T-30 baselines, and
two-operator watcher are also absent. The packet therefore remains `NOT_READY`.

When those inputs exist, use the existing `scripts/deploy-verify` authenticated
contract first. Run the load command from the worktree's `backend` directory
with `/Users/joseperdomo/Desktop/chirps/Chirp/backend/.venv/bin/python`; this
packet does not assume a worktree-local virtualenv. Preserve the approved
02:00–05:00 ET quiet window, staged WS phases at 25, 50, then 100 sockets,
the HTTP phases, abort thresholds, connection and Cloud Run/Redis
watchers, and marker-based cleanup in `backend/loadtest/RUNBOOK.md`. Never put
the bearer or Firebase tokens in commands, reports, or tracked files.

The resulting run must retain phase reports, watcher observations, cleanup and
post-run deployment verification. A local harness pass, authenticated readiness,
or this preparation does not establish production capacity, receipt delivery,
recovery, HA, or device acceptance.
