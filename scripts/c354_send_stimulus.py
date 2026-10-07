"""Make a message ARRIVE on a phone, so c354's device walk is possible at all.

WHY THIS EXISTS. c354 needs connect/send/receive, background/foreground catch-up
and hard-kill/relaunch observed on a real device. The receive half has no way to
happen through the app: `app/(tabs)/messages/[id].tsx` hard-disables the composer
(`editable={false}`) and `src/crypto/groups.ts` throws
`TODO(milestone-3): libsignal` on every entry point, so nobody can type and send
a message on any account, on any build. A device-holder opening the app and
waiting will wait forever. The walk was not blocked on a build or on a
permission; it was blocked on there being no sender.

So this sends as a SECOND identity against the same API the phone is pointed at,
and the phone receives over its live socket exactly as it would from a real peer.

WHAT MAKES A SYNTHETIC SENDER POSSIBLE. `POST /messages` stores ciphertext and
never parses it (SPEC 8.1), and `POST /devices` stores the identity key and
signed-prekey signature WITHOUT verifying them - checked in
`app/routers/keys.py`, which validates sizes and nothing more. So a sender device
registered from random bytes of the right length is indistinguishable, to the
server, from a real one. That is a deliberate property of a server that cannot
read messages, not a hole to fix: the point of this tool is that it needs no
crypto stack, which is the thing milestone 3 has not built.

The payload is therefore NOT decryptable by the receiving phone, and must not be
confused with a real conversation. What it exercises is the transport: does the
socket deliver, does a backgrounded app catch up, does a hard-killed app recover
history. Whether the ciphertext decrypts is c23's question, not c354's.

WHAT IT WILL NOT DO. It refuses every non-loopback target unless you say so on the
command line. The device walk may run against production, because that is what the
phone build points at, so the flag exists to make any remote write a decision rather
than a default. Redirects are refused so the bearer token cannot follow an untrusted
target. Every row it writes is real, in a real conversation, visible to every real
member of it - so send into a conversation you own.
"""
from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import os
import secrets
import ssl
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

# From app/schemas/e2ee.py. Sizes are validated on registration; contents are not.
IDENTITY_KEY_BYTES = 32
PREKEY_PUBLIC_BYTES = 32
PREKEY_SIGNATURE_BYTES = 64
# app/core/validation.py MAX_CIPHERTEXT_B64_LENGTH is 65_536; nothing here goes near it.

PROD_MARKERS = ("run.app", "chirpsocials.com", "chirps-prod")


def _validate_api_target(api: str, allow_remote: bool) -> str:
    """Validate the base URL before credentials or a request are constructed."""
    try:
        parsed = urlsplit(api)
        hostname = parsed.hostname
        port = parsed.port  # force malformed ports to fail here
    except (TypeError, ValueError):
        _fail("invalid --api URL")
    if parsed.scheme not in {"http", "https"} or not hostname:
        _fail("--api must be an http(s) URL with a hostname")
    if parsed.username is not None or parsed.password is not None:
        _fail("--api must not contain URL userinfo")
    if parsed.query or parsed.fragment:
        _fail("--api must not contain a query or fragment")
    try:
        is_loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        is_loopback = hostname.lower() == "localhost"
    if not is_loopback and not allow_remote:
        _fail("--api is remote; pass --allow-prod deliberately for any non-loopback target")
    if not is_loopback and parsed.scheme != "https":
        _fail("--api must use https for every non-loopback target")
    return api.rstrip("/")


def _b64(n: int) -> str:
    return base64.b64encode(secrets.token_bytes(n)).decode("ascii")


def _call(api: str, path: str, token: str, body: dict | None = None, method: str = "GET") -> tuple[int, object]:
    """One JSON request, with credentials never followed to another origin."""
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        api.rstrip("/") + path,
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
            **({"Content-Type": "application/json"} if data is not None else {}),
        },
    )
    context = ssl.create_default_context()
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def _reject(self, req, fp, code, msg, headers):
            raise urllib.error.URLError("redirect refused")

        http_error_301 = _reject
        http_error_302 = _reject
        http_error_303 = _reject
        http_error_307 = _reject
        http_error_308 = _reject

    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context), _NoRedirect
    )
    try:
        with opener.open(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
            try:
                payload = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                return 0, "invalid_response"
            return response.status, payload
    except urllib.error.HTTPError as error:
        # Do not echo arbitrary HTML or response bodies that may contain tokens.
        return error.code, "http_error"
    except (urllib.error.URLError, TimeoutError, OSError):
        # Do not expose a traceback or echo an untrusted URL. The operator only
        # needs a stable failure class; retry/target diagnosis stays local.
        return 0, "network_error"


def _fail(message: str) -> None:
    print("FAILED: " + message, file=sys.stderr)
    raise SystemExit(1)


def register_sender_device(api: str, token: str, label: str) -> str:
    """Register a device for the TOKEN'S OWN user and return its id.

    Sizes come from app/schemas/e2ee.py. The bytes are random because nothing
    verifies them; see the module docstring before treating that as a finding.
    """
    status, payload = _call(api, "/devices", token, method="POST", body={
        "device_label": label,
        "registration_id": secrets.randbelow(2**31 - 1),
        "identity_key_b64": _b64(IDENTITY_KEY_BYTES),
        "signed_prekey": {
            "key_id": secrets.randbelow(2**31 - 1),
            "public_key_b64": _b64(PREKEY_PUBLIC_BYTES),
            "signature_b64": _b64(PREKEY_SIGNATURE_BYTES),
        },
        "one_time_prekeys": [],
    })
    if status == 201 and isinstance(payload, dict) and "id" in payload:
        return str(payload["id"])
    if status == 429:
        _fail("device registration is rate limited for this user; wait, or pass --device "
              "with an id this user already owns")
    _fail(f"device registration returned {status}: {payload!r}")
    raise AssertionError("unreachable")


def list_conversations(api: str, token: str) -> list[dict]:
    status, payload = _call(api, "/conversations", token)
    if status != 200 or not isinstance(payload, list):
        _fail(f"GET /conversations returned {status}: {payload!r}")
    return payload


def send(api: str, token: str, conversation: str, device: str, text: str) -> tuple[int, object]:
    # The server never parses this, so the plaintext marker is for the OPERATOR's
    # benefit when reading rows back - it is not a message the phone can decrypt.
    return _call(api, f"/conversations/{conversation}/messages", token, method="POST", body={
        "sender_device_id": device,
        "ciphertext_b64": base64.b64encode(text.encode("utf-8")).decode("ascii"),
        "message_type": "signal",
    })


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="c354-send-stimulus",
        description="Send synthetic messages so a device can be observed receiving them (board c354).",
    )
    parser.add_argument("--api", required=True,
                        help="API base URL. Point this at whatever the phone build points at.")
    parser.add_argument("--token", default=os.environ.get("CHIRP_STIMULUS_TOKEN"),
                        help="Firebase ID token for the SENDING identity, i.e. NOT the account on the "
                             "phone. Defaults to $CHIRP_STIMULUS_TOKEN so it stays out of your shell history.")
    parser.add_argument("--conversation",
                        help="Conversation id. Omit to list what this identity can see and stop.")
    parser.add_argument("--device",
                        help="Reuse a sender device id instead of registering one. Registration is "
                             "rate limited per user, so pass this on repeat runs.")
    parser.add_argument("--count", type=int, default=1, help="How many messages to send (default 1).")
    parser.add_argument("--interval", type=float, default=2.0,
                        help="Seconds between sends (default 2). Raise it to watch catch-up behaviour.")
    parser.add_argument("--label", default="c354-stimulus",
                        help="Device label, so this sender is identifiable later.")
    parser.add_argument("--allow-prod", action="store_true",
                        help="Required for every non-loopback --api target. Every row written is real "
                             "and visible to every member of the conversation.")
    args = parser.parse_args()

    if not args.token:
        _fail("no token: pass --token or set CHIRP_STIMULUS_TOKEN")

    api = _validate_api_target(args.api, args.allow_prod)
    looks_prod = any(marker in api for marker in PROD_MARKERS)
    if looks_prod:
        print(f"TARGET IS PRODUCTION: {api}")

    if args.conversation is None:
        conversations = list_conversations(api, args.token)
        if not conversations:
            _fail("this identity is in no conversations; it must share one with the account on the "
                  "phone before it can send anything there")
        print(f"{len(conversations)} conversation(s) visible to the sending identity:")
        for row in conversations:
            print(f"  {row.get('id')}  kind={row.get('kind')!r}  title={row.get('title')!r}")
        print("\nRe-run with --conversation <id>. Pick one the phone's account is also in.")
        return 0

    device = args.device or register_sender_device(api, args.token, args.label)
    if args.device is None:
        print(f"registered sender device {device} (pass --device {device} next time)")

    sent = 0
    for index in range(1, args.count + 1):
        # Distinguishable on purpose: a device-holder watching several arrive needs to
        # say WHICH one showed up late, or not at all, rather than "some did".
        stamp = time.strftime("%H:%M:%S")
        status, payload = send(api, args.token, args.conversation, device,
                               f"c354 stimulus {index}/{args.count} at {stamp}")
        if status == 201:
            sent += 1
            created = payload.get("created_at") if isinstance(payload, dict) else None
            print(f"  [{index}/{args.count}] 201 sent at {stamp}  created_at={created}")
        else:
            # Printed, not raised: a partial run is still useful evidence, and the
            # operator needs to see WHICH send failed and why.
            print(f"  [{index}/{args.count}] {status} {payload!r}")
            if status in (401, 403):
                print("    401/403 stops the run: the token or membership is wrong, and retrying "
                      "will not change that.")
                break
        if index < args.count:
            time.sleep(args.interval)

    print(f"\n{sent}/{args.count} delivered to the API. "
          "That is an accepted write, NOT proof the phone rendered it - that is what you are "
          "watching the device for.")
    return 0 if sent else 1


if __name__ == "__main__":
    raise SystemExit(main())
