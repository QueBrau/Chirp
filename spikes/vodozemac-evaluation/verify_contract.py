#!/usr/bin/env python3
"""Probe actual Chirp schemas with fresh public output from the isolated Rust spike.

Imports schemas only. Does not start the app, read credentials, connect to a
database, send HTTP, or assert that syntactic acceptance proves secure mapping.
"""

from __future__ import annotations

import argparse
import base64
import copy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from pydantic import ValidationError  # noqa: E402
from app.core.validation import MAX_CIPHERTEXT_B64_LENGTH  # noqa: E402
from app.schemas.e2ee import DeviceCreate, DevicePrekeyBundleOut  # noqa: E402
from app.schemas.messaging import MessageCreate, MessageOut  # noqa: E402

SPECIMEN: dict = {}


class CurrentContract(unittest.TestCase):
    def registration(self) -> dict:
        return copy.deepcopy(SPECIMEN["shape_only_registration"])

    def test_candidate_shape_is_accepted_but_does_not_prove_semantics(self):
        result = DeviceCreate.model_validate(self.registration())
        self.assertEqual(len(base64.b64decode(result.identity_key_b64)), 32)
        self.assertEqual(len(base64.b64decode(result.signed_prekey.signature_b64)), 64)
        self.assertEqual(SPECIMEN["candidate"], "vodozemac")
        self.assertEqual(SPECIMEN["version"], "0.11.0")

    def test_native_unpadded_base64_requires_explicit_transport_conversion(self):
        data = self.registration()
        data["identity_key_b64"] = SPECIMEN["native_unpadded_curve_b64"]
        with self.assertRaises(ValidationError):
            DeviceCreate.model_validate(data)

    def test_required_signed_prekey_cannot_simply_be_omitted_for_olm(self):
        data = self.registration()
        del data["signed_prekey"]
        with self.assertRaises(ValidationError):
            DeviceCreate.model_validate(data)

    def test_two_identities_cannot_be_concatenated_into_current_identity_column(self):
        data = self.registration()
        both = base64.b64decode(data["identity_key_b64"]) + base64.b64decode(SPECIMEN["signing_identity_b64"])
        data["identity_key_b64"] = base64.b64encode(both).decode("ascii")
        with self.assertRaises(ValidationError):
            DeviceCreate.model_validate(data)

    def test_adding_a_signing_identity_field_silently_loses_it(self):
        data = self.registration()
        data["signing_identity_b64"] = SPECIMEN["signing_identity_b64"]
        result = DeviceCreate.model_validate(data).model_dump()
        self.assertNotIn("signing_identity_b64", result)
        self.assertNotIn("signing_identity_b64", DevicePrekeyBundleOut.model_fields)

    def test_schema_accepts_invalid_signature_of_correct_length(self):
        data = self.registration()
        data["signed_prekey"]["signature_b64"] = base64.b64encode(bytes(64)).decode("ascii")
        DeviceCreate.model_validate(data)

    def test_null_kyber_and_exhausted_ec_are_allowed_not_new_protocol_fallback(self):
        data = self.registration()
        result = DeviceCreate.model_validate(data)
        self.assertIsNone(result.kyber_last_resort)
        bundle = DevicePrekeyBundleOut.model_validate({
            "device_id": SPECIMEN["message"]["sender_device_id"],
            "registration_id": data["registration_id"],
            "identity_key_b64": data["identity_key_b64"],
            "signed_prekey": data["signed_prekey"],
        })
        self.assertIsNone(bundle.one_time_prekey)
        self.assertIsNone(bundle.kyber_prekey)

    def test_key_identifiers_are_bounded_integers_not_native_u64_base64_ids(self):
        for value in [2**31, -1, "AAAAAAAAAAE"]:
            with self.subTest(value=value):
                data = self.registration()
                data["one_time_prekeys"][0]["key_id"] = value
                with self.assertRaises(ValidationError):
                    DeviceCreate.model_validate(data)

    def test_opaque_framed_olm_ciphertext_passes_current_message_schema(self):
        result = MessageCreate.model_validate(SPECIMEN["message"])
        decoded = json.loads(base64.b64decode(result.ciphertext_b64, validate=True))
        self.assertEqual(decoded["evaluation_version"], 1)
        self.assertEqual(decoded["olm_type"], 0)
        self.assertTrue(decoded["body"])

    def test_new_algorithm_type_is_rejected_by_current_outer_enum(self):
        data = dict(SPECIMEN["message"], message_type="olm")
        with self.assertRaises(ValidationError):
            MessageCreate.model_validate(data)

    def test_outer_targeting_algorithm_epoch_fields_are_not_retained(self):
        data = dict(SPECIMEN["message"], recipient_device_id="other", algorithm="olm", epoch=1)
        result = MessageCreate.model_validate(data).model_dump()
        for field in ["recipient_device_id", "algorithm", "epoch"]:
            self.assertNotIn(field, result)

    def test_http_output_reencodes_stored_opaque_bytes_without_loss(self):
        result = MessageOut.model_validate({
            "id": "00000000-0000-4000-8000-000000000004",
            "conversation_id": "00000000-0000-4000-8000-000000000001",
            "sender_device_id": SPECIMEN["message"]["sender_device_id"],
            "message_type": "signal", "created_at": "2026-09-21T00:00:00Z",
            "ciphertext": base64.b64decode(SPECIMEN["message"]["ciphertext_b64"], validate=True),
        })
        self.assertEqual(result.ciphertext_b64, SPECIMEN["message"]["ciphertext_b64"])

    def test_nested_json_framing_exceeds_limit_for_maximum_unicode_text(self):
        self.assertEqual(MAX_CIPHERTEXT_B64_LENGTH, 65536)
        self.assertLess(SPECIMEN["lengths"]["raw_single_olm_transport_chars"], MAX_CIPHERTEXT_B64_LENGTH)
        for field in ["max_text_single_device_b64", "max_text_two_devices_b64"]:
            with self.subTest(field=field):
                self.assertGreater(len(SPECIMEN[field]), MAX_CIPHERTEXT_B64_LENGTH)
                with self.assertRaises(ValidationError):
                    MessageCreate.model_validate(dict(SPECIMEN["message"], ciphertext_b64=SPECIMEN[field]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("specimen", type=Path)
    args = parser.parse_args()
    if args.specimen.stat().st_size > 1_000_000:
        parser.error("specimen exceeds private local fixture limit")
    global SPECIMEN
    SPECIMEN = json.loads(args.specimen.read_text())
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(CurrentContract)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print(json.dumps({"schema_tests": result.testsRun, "successful": result.wasSuccessful(),
                      "candidate": "vodozemac 0.11.0", "lengths": SPECIMEN["lengths"],
                      "scope": "host crypto plus imported schemas; no HTTP, DB or device proof"}, sort_keys=True))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
