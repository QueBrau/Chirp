"""Read the API and WebSocket origins actually COMPILED into an iOS build, and
emit the `client_build` record scripts/deployment_config.py expects.

Board card c418, unblocking c362's last acceptance item. c362 needs evidence of
what the shipped binary talks to, which is a different question from what the
repository config says - deployment_config.compare() asks both separately
(`client_repository` for the checked-in values, `client_build` for these) and the
whole point is that they can disagree. A build cut from a stale branch, or with
the wrong EAS profile, is exactly the case where they do.

WHY A SCRIPT AND NOT A NOTE ON THE CARD. The first observation for build
90e93270 was read by hand. That answer was right and it is worthless one build
later, because the next build needs the same work again and a pasted URL cannot
be re-checked. Every sibling verify-* here extracts from the real artifact at run
time for the same reason.

WHY IT PARSES THE HERMES STRING TABLE INSTEAD OF SCANNING FOR TEXT, which is the
whole engineering content of this script. A Hermes bundle stores string literals
CONCATENATED with no terminators; lengths live in a separate table. So any
scan-for-a-URL approach - `strings`, a regex over the bytes, a character-class
walk - cannot see where a literal ends, and silently runs one string into the
next. Both of these are real first-attempt outputs against this very artifact:

    wss://chirp-ws-...run.app/wsocial-mediumultiply_matrices...
    https://chirps-prod.web.append

Neither URL exists in the bundle. The table holds ".../ws" followed by
"ocial-medium", and ".web.app" followed by "endAllChildrenToContainer". Narrowing
the accepted character class does NOT fix it, because the following literal
begins with perfectly legal path characters - that fix was tried here first and
produced the garbage above. Only the length table gives an exact boundary.

Consequence, and it is deliberate: a non-Hermes artifact is REFUSED
(`bundle_not_hermes_bytecode`) rather than scanned by a weaker method. A checker
that reports a wrong endpoint with total confidence is worse than one that
reports nothing, and this is the evidence a release record cites.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import zipfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "infra" / "deployment.json"

# Same expression deployment_config.py validates build_id with. Kept in sync by
# hand rather than imported: this script must run against an artifact without
# pulling in the cloud-reading module and its gcloud assumptions.
BUILD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}")

HERMES_MAGIC = bytes.fromhex("c61fbc03c103191f")
HEADER_FIELDS = (
    "fileLength", "globalCodeIndex", "functionCount", "stringKindCount",
    "identifierCount", "stringCount", "overflowStringCount", "stringStorageSize",
    "bigIntCount", "bigIntStorageSize", "regExpCount", "regExpStorageSize",
    "arrayBufferSize", "objKeyBufferSize", "objValueBufferSize", "segmentID",
    "cjsModuleCount", "functionSourceCount", "debugInfoOffset",
)
# Versions this parser has been run against. Hermes has changed its header shape
# before; refusing an unknown version is the same call as refusing a non-Hermes
# file, for the same reason.
SUPPORTED_VERSIONS = (96,)
OVERFLOW_MARKER = 0xFF


class EndpointError(Exception):
    """Refusal with a stable label; never interpolates artifact bytes."""


def load_config(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        raise EndpointError("config_unreadable") from None


def expected_endpoints(config: dict) -> tuple[str, str]:
    """The two origins deployment_config.compare() checks `client_build` against.

    Derived from the same fields the same way, so this cannot drift into agreeing
    with itself while disagreeing with the checker that consumes its output.
    """
    try:
        api = config["services"]["api"]["client_origin"]
        ws = config["services"]["ws"]["client_origin"].replace("https:", "wss:") + config["client"]["ws_path"]
    except (KeyError, TypeError):
        raise EndpointError("config_missing_client_origins") from None
    return api, ws


def read_archive(app_archive: Path) -> tuple[bytes, str, int]:
    """Return (bundle bytes, sha256 of the archive, archive size).

    The .ipa is read as a zip; nothing is extracted to disk and nothing is
    executed. This inspects an artifact, it does not install one.
    """
    try:
        raw = app_archive.read_bytes()
    except OSError:
        raise EndpointError("artifact_unreadable") from None
    try:
        with zipfile.ZipFile(app_archive) as archive:
            names = [n for n in archive.namelist() if n.endswith(".app/main.jsbundle")]
            if len(names) != 1:
                # Zero: not an Expo/RN archive, or the bundle moved. More than one:
                # two apps in one payload, so there is no single answer to give.
                raise EndpointError("expected_exactly_one_main_jsbundle")
            return archive.read(names[0]), hashlib.sha256(raw).hexdigest(), len(raw)
    except zipfile.BadZipFile:
        raise EndpointError("artifact_not_a_zip") from None


def hermes_strings(bundle: bytes) -> list[str]:
    """Every UTF-8 string literal in a Hermes bundle, at its exact boundaries.

    Layout walked here (Hermes v96): a 32-byte-aligned header, then function
    headers, string kinds, identifier hashes, the small string table, the
    overflow string table and finally the string storage - each section 4-byte
    aligned. A small entry packs {isUTF16:1, offset:23, length:8}; length 0xFF
    means the real offset/length pair lives in the overflow table.
    """
    if bundle[:8] != HERMES_MAGIC:
        raise EndpointError("bundle_not_hermes_bytecode")
    version, = struct.unpack_from("<I", bundle, 8)
    if version not in SUPPORTED_VERSIONS:
        raise EndpointError("unsupported_hermes_version")

    cursor = 32  # 8 magic + 4 version + 20 sourceHash
    header: dict[str, int] = {}
    for name in HEADER_FIELDS:
        header[name], = struct.unpack_from("<I", bundle, cursor)
        cursor += 4

    # The cheapest possible proof that the layout above is the layout in hand. If
    # this disagrees, every offset below is nonsense and must not be reported.
    if header["fileLength"] != len(bundle):
        raise EndpointError("hermes_header_length_mismatch")

    def align(value: int, boundary: int = 4) -> int:
        return (value + boundary - 1) // boundary * boundary

    position = align(cursor, 32)
    position = align(position + header["functionCount"] * 16)
    position = align(position + header["stringKindCount"] * 4)
    position = align(position + header["identifierCount"] * 4)
    small_table = position
    position = align(position + header["stringCount"] * 4)
    overflow_table = position
    position = align(position + header["overflowStringCount"] * 8)
    storage = position
    if storage + header["stringStorageSize"] > len(bundle):
        raise EndpointError("hermes_string_storage_out_of_range")

    literals: list[str] = []
    for index in range(header["stringCount"]):
        entry, = struct.unpack_from("<I", bundle, small_table + index * 4)
        is_utf16 = entry & 1
        offset = (entry >> 1) & 0x7FFFFF
        length = (entry >> 24) & 0xFF
        if length == OVERFLOW_MARKER:
            offset, length = struct.unpack_from("<II", bundle, overflow_table + offset * 8)
        if is_utf16:
            # URLs in this codebase are ASCII; a UTF-16 entry is never one of them,
            # and decoding it would only add ways to be wrong.
            continue
        if storage + offset + length > len(bundle):
            raise EndpointError("hermes_string_entry_out_of_range")
        literals.append(bundle[storage + offset:storage + offset + length].decode("utf-8", "replace"))
    return literals


def observe(app_archive: Path, build_id: str, config: dict, now: datetime) -> dict:
    if not BUILD_ID.fullmatch(build_id):
        raise EndpointError("invalid_build_id")
    api_expected, ws_expected = expected_endpoints(config)
    bundle, digest, size = read_archive(app_archive)
    literals = set(hermes_strings(bundle))

    findings: list[dict] = []

    def note(field: str, detail: str) -> None:
        findings.append({"field": field, "kind": "drift", "detail": detail})

    # Exact literal equality, which the string table makes available and a text
    # scan does not. "Contains" would be wrong in the direction that matters:
    # "/wsocial" contains "/ws".
    api_ok = api_expected in literals
    ws_ok = ws_expected in literals
    if not api_ok:
        note("api_url", "configured_api_origin_is_not_a_literal_in_the_bundle")
    if not ws_ok:
        note("ws_url", "configured_ws_url_is_not_a_literal_in_the_bundle")

    # c246, the failure this card family exists for: the socket resolving to the
    # API service because EXPO_PUBLIC_WS_URL was never set for the build profile.
    # Both services run the SAME image, so chirp-api would likely answer /ws and
    # messaging would look fine while chirp-ws stayed dark - the bug is invisible
    # from the app and only the artifact settles it.
    api_socket = api_expected.replace("https:", "wss:")
    if any(literal.startswith(api_socket) for literal in literals):
        note("ws_url", "socket_url_points_at_the_api_service_c246")

    # A local dev endpoint compiled into a distributable build is the mirror of
    # the same mistake. Scoped to our own scheme+host shapes so third-party SDK
    # and mock-fixture strings (Stripe's localhost:8081, the mock job board's
    # localhost:3000) are not reported as ours.
    for literal in literals:
        if re.fullmatch(r"(https?|wss?)://(localhost|127\.0\.0\.1|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+)(:\d+)?(/[\w./~-]*)?", literal):
            if literal.rstrip("/") in {api_expected.rstrip("/"), ws_expected.rstrip("/")}:
                note("client_build", "local_endpoint_compiled_into_a_distributable_build")

    record = {
        "build_id": build_id,
        "api_url": api_expected if api_ok else None,
        "ws_url": ws_expected if ws_ok else None,
        "observed_at": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    return {
        "verdict": "DRIFT" if findings else "CLIENT_MATCH",
        "client_build": record,
        "expected": {"api_url": api_expected, "ws_url": ws_expected},
        "artifact": {"name": app_archive.name, "sha256": digest, "bytes": size},
        "bundle": {"format": "hermes", "version": SUPPORTED_VERSIONS[0], "utf8_literals": len(literals)},
        "findings": findings,
        "evidence_scope": "compiled_artifact_string_table_observation_not_a_device_test",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("archive", type=Path, help="path to the .ipa")
    parser.add_argument("--build-id", required=True, help="EAS build id the archive came from")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--report", type=Path)
    try:
        args = parser.parse_args(argv)
        report = observe(args.archive, args.build_id, load_config(args.config), datetime.now(timezone.utc))
        output = json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.report:
            args.report.write_text(output)
        print(output, end="")
        return 0 if report["verdict"] == "CLIENT_MATCH" else 1
    except EndpointError as exc:
        print(json.dumps({"verdict": "NOT_PROVEN", "error": str(exc)}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
