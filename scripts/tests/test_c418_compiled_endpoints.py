"""Compiled-endpoint extraction from a Hermes bundle (board card c418).

Every bundle here is SYNTHESISED rather than taken from a real .ipa, for two
reasons: an 18MB artifact does not belong in the repo, and a synthetic bundle is
the only way to construct the cases that must FAIL. The builder below emits a
real Hermes v96 container, so it also serves as an independent statement of the
layout the parser walks - if the two disagree, these tests break rather than the
next release quietly citing a wrong URL.

The case this file exists for is test_substring_path_is_not_a_match. The first
implementation of this checker scanned the bundle for text, could not see where a
literal ended, and reported ".../ws" + "ocial-medium" as an endpoint called
"/wsocial-medium...". Narrowing the character class did not fix it, because the
next literal starts with legal path characters. That is why the parser reads the
length table, and why "contains" is never enough here.
"""
import importlib.util
import io
import json
import struct
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

PATH = Path(__file__).resolve().parents[1] / "compiled_endpoints.py"
spec = importlib.util.spec_from_file_location("compiled_endpoints", PATH)
ce = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ce)

API = "https://chirp-api-593616178468.us-central1.run.app"
WS = "wss://chirp-ws-593616178468.us-central1.run.app/ws"
NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
BUILD = "90e93270-3a95-46b4-b358-71de257be08c"

CONFIG = {
    "services": {
        "api": {"client_origin": API},
        "ws": {"client_origin": "https://chirp-ws-593616178468.us-central1.run.app"},
    },
    "client": {"ws_path": "/ws", "build_evidence_max_age_hours": 24},
}


def hermes_bundle(strings, *, version=96, corrupt_length=False, force_overflow=False):
    """A minimal but REAL Hermes v96 container carrying `strings`.

    Sections are emitted in the order the parser expects with the same alignment
    rules. Storage is packed with no separators, which is the whole point: it is
    what makes a text scan unable to find a boundary.
    """
    storage = b""
    entries = []
    for s in strings:
        raw = s.encode()
        offset = len(storage)
        storage += raw
        entries.append((offset, len(raw)))

    overflow = b""
    small = b""
    overflow_count = 0
    for offset, length in entries:
        if length >= 0xFF or force_overflow:
            small += struct.pack("<I", (0xFF << 24) | (overflow_count << 1))
            overflow += struct.pack("<II", offset, length)
            overflow_count += 1
        else:
            small += struct.pack("<I", (length << 24) | (offset << 1))

    def pad(blob, boundary=4):
        return blob + b"\x00" * (-len(blob) % boundary)

    header_fields = {n: 0 for n in ce.HEADER_FIELDS}
    header_fields["stringCount"] = len(entries)
    header_fields["overflowStringCount"] = overflow_count
    header_fields["stringStorageSize"] = len(storage)

    head = ce.HERMES_MAGIC + struct.pack("<I", version) + b"\x00" * 20
    body = b"".join(struct.pack("<I", header_fields[n]) for n in ce.HEADER_FIELDS)
    prefix = pad(head + body, 32)  # functionCount/kinds/identifiers are all 0 here
    blob = prefix + pad(small) + pad(overflow) + storage

    total = len(blob) if not corrupt_length else len(blob) + 1
    # fileLength is the 1st header field, immediately after the 32-byte preamble.
    blob = blob[:32] + struct.pack("<I", total) + blob[36:]
    return blob


def ipa(bundle_bytes, *, entries=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name in (entries or ["Payload/chirp.app/main.jsbundle"]):
            z.writestr(name, bundle_bytes)
    return buf.getvalue()


class CompiledEndpointTests(unittest.TestCase):
    def observe(self, bundle_bytes, *, build_id=BUILD, entries=None, config=None):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "chirp.ipa"
            path.write_bytes(ipa(bundle_bytes, entries=entries))
            return ce.observe(path, build_id, config or CONFIG, NOW)

    # --- the happy path, and that it really is reading the artifact ---

    def test_matching_bundle_reports_client_match(self):
        report = self.observe(hermes_bundle(["unrelated", API, WS, "also-unrelated"]))
        self.assertEqual(report["verdict"], "CLIENT_MATCH")
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["client_build"]["api_url"], API)
        self.assertEqual(report["client_build"]["ws_url"], WS)
        self.assertEqual(report["client_build"]["build_id"], BUILD)
        self.assertEqual(report["client_build"]["observed_at"], "2026-09-16T12:00:00Z")

    def test_record_shape_is_what_deployment_config_consumes(self):
        """deployment_config.compare() reads exactly these four keys off client_build."""
        report = self.observe(hermes_bundle([API, WS]))
        self.assertEqual(set(report["client_build"]), {"build_id", "api_url", "ws_url", "observed_at"})

    # --- THE case: exact boundaries, not substrings ---

    def test_substring_path_is_not_a_match(self):
        """A bundle whose real literal is "/wsocial..." must NOT satisfy "/ws".

        This is the first implementation's bug preserved as a test. Storage packs
        the literals with no separator, so a scanner reads straight through.
        """
        bundle = hermes_bundle([API, WS + "ocial-medium", "ultiply_matrices"])
        report = self.observe(bundle)
        self.assertEqual(report["verdict"], "DRIFT")
        self.assertIn("configured_ws_url_is_not_a_literal_in_the_bundle",
                      [f["detail"] for f in report["findings"]])
        self.assertIsNone(report["client_build"]["ws_url"])

    def test_adjacent_literals_do_not_bleed_into_each_other(self):
        """The positive twin of the above: correct URL, junk literal right after it."""
        report = self.observe(hermes_bundle([API, WS, "ocial-medium", "endAllChildrenToContainer"]))
        self.assertEqual(report["verdict"], "CLIENT_MATCH")

    # --- c246, the failure this card family exists for ---

    def test_socket_pointing_at_the_api_service_is_drift(self):
        bad = API.replace("https:", "wss:") + "/ws"
        report = self.observe(hermes_bundle([API, WS, bad]))
        self.assertEqual(report["verdict"], "DRIFT")
        self.assertIn("socket_url_points_at_the_api_service_c246",
                      [f["detail"] for f in report["findings"]])

    def test_missing_api_origin_is_drift(self):
        report = self.observe(hermes_bundle([WS, "nothing-else"]))
        self.assertEqual(report["verdict"], "DRIFT")
        self.assertIn("configured_api_origin_is_not_a_literal_in_the_bundle",
                      [f["detail"] for f in report["findings"]])
        self.assertIsNone(report["client_build"]["api_url"])

    # --- third-party strings must not be reported as ours ---

    def test_vendor_and_mock_local_urls_are_not_reported(self):
        """Real strings from this app's own bundle: Stripe's and the mock job board's.

        Reporting these would make the check cry wolf on every build, which is how
        a checker stops being read.
        """
        report = self.observe(hermes_bundle([
            API, WS, "http://localhost:8081/intentConfiguration",
            "http://localhost:3000", "https://careers.northgate.example/summer-analyst",
        ]))
        self.assertEqual(report["verdict"], "CLIENT_MATCH")

    # --- refusals: never guess ---

    def test_non_hermes_bundle_is_refused_not_scanned(self):
        """A weaker method on an unknown format is how a wrong URL gets published."""
        plain = f'var API="{API}";var WS="{WS}";'.encode()
        with self.assertRaises(ce.EndpointError) as caught:
            self.observe(plain)
        self.assertEqual(str(caught.exception), "bundle_not_hermes_bytecode")

    def test_unknown_hermes_version_is_refused(self):
        with self.assertRaises(ce.EndpointError) as caught:
            self.observe(hermes_bundle([API, WS], version=255))
        self.assertEqual(str(caught.exception), "unsupported_hermes_version")

    def test_header_length_mismatch_is_refused(self):
        """The cheapest proof the layout in hand is the layout assumed."""
        with self.assertRaises(ce.EndpointError) as caught:
            self.observe(hermes_bundle([API, WS], corrupt_length=True))
        self.assertEqual(str(caught.exception), "hermes_header_length_mismatch")

    def test_two_apps_in_one_payload_is_refused(self):
        with self.assertRaises(ce.EndpointError) as caught:
            self.observe(hermes_bundle([API, WS]), entries=[
                "Payload/chirp.app/main.jsbundle", "Payload/other.app/main.jsbundle"])
        self.assertEqual(str(caught.exception), "expected_exactly_one_main_jsbundle")

    def test_archive_without_a_bundle_is_refused(self):
        with self.assertRaises(ce.EndpointError) as caught:
            self.observe(hermes_bundle([API, WS]), entries=["Payload/chirp.app/Info.plist"])
        self.assertEqual(str(caught.exception), "expected_exactly_one_main_jsbundle")

    def test_invalid_build_id_is_refused_before_reading_anything(self):
        with self.assertRaises(ce.EndpointError) as caught:
            self.observe(hermes_bundle([API, WS]), build_id="not a build id")
        self.assertEqual(str(caught.exception), "invalid_build_id")

    def test_config_without_client_origins_is_refused(self):
        with self.assertRaises(ce.EndpointError) as caught:
            self.observe(hermes_bundle([API, WS]), config={"services": {}, "client": {}})
        self.assertEqual(str(caught.exception), "config_missing_client_origins")

    # --- table mechanics ---

    def test_overflow_string_entries_are_read(self):
        """Literals >= 255 bytes live in the overflow table; a URL can share it."""
        report = self.observe(hermes_bundle([API, WS, "x" * 600], force_overflow=True))
        self.assertEqual(report["verdict"], "CLIENT_MATCH")

    def test_expected_endpoints_derive_the_ws_url_the_same_way_the_checker_does(self):
        api, ws = ce.expected_endpoints(CONFIG)
        self.assertEqual(api, API)
        self.assertEqual(ws, WS)
        self.assertTrue(ws.startswith("wss://"))


if __name__ == "__main__":
    unittest.main()
