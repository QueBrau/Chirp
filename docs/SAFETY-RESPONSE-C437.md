# Urgent safety response runbook (c437)

This runbook is an operational aid for `chirp.shared@gmail.com`. Jose is the
primary responder and Braulio is the backup. Both are assigned every day,
including weekends. The local case register is `scripts/safety-case`; it stores
an opaque case reference, notice completeness, UTC timestamps, responder names,
status, hashes and audit events. It must not receive intimate imagery, CSAM,
message bodies, raw evidence paths or provider credentials. Keep its SQLite file
mode 0600 in an access-controlled local directory and remove it through the
approved local-device process when its retention period is decided.

## Intake and clock

Create a case at receipt time, even when the notice is incomplete. Capture only
the five notice flags: signature, sufficient identification of the image or
video, location on Chirp, good-faith statement, and requester contact details.
Do not ask for an image attachment or CSAM by email. If a flag is missing, set
`needs_information`, record the missing flag names, and reply with the case
reference and the minimum missing information. Do not start the 48-hour removal
clock until the notice is valid. Once all required elements are present, record
the first valid-notice time separately from the original intake time and set the
deadline at `valid_notice + 48 hours`; assign Jose or
Braulio and acknowledge the case reference.

The operator commands are explicit and metadata-only. For example:

```sh
scripts/safety-case --db /private/tmp/chirp-safety.sqlite3 intake \
  --elements contact --received-at 2026-10-06T12:00:00Z
scripts/safety-case --db /private/tmp/chirp-safety.sqlite3 complete-notice SAF-REFERENCE \
  --elements signature,identification,location,good_faith,contact
scripts/safety-case --db /private/tmp/chirp-safety.sqlite3 attempt SAF-REFERENCE \
  --surface post --attempt 1 --outcome removed --verification-ref provider-receipt-1
scripts/safety-case --db /private/tmp/chirp-safety.sqlite3 status SAF-REFERENCE
scripts/safety-case --db /private/tmp/chirp-safety.sqlite3 hold SAF-REFERENCE
scripts/safety-case --db /private/tmp/chirp-safety.sqlite3 release-hold SAF-REFERENCE
```

`close` refuses incomplete notices, active preservation holds, unverified
controlled surfaces, and missing known-copy review. Exact duplicate attempt
replays are idempotent; conflicting replays fail. Reopening preserves the
original receipt time and deadline.

Record `known_copy` separately from the reported `media` surface. Use
`none_found` with a scan receipt when no copies exist, or `verified_absent`
with a receipt after identified copies have been removed and checked. A failed
scan or later failed verification blocks closure. Following `reopen`, record
fresh attempts with new, increasing attempt numbers for every declared surface
and the copy scan; earlier response evidence cannot close the reopened case.
New attempt numbers must increase for each surface; an older numbered attempt
cannot overwrite a later result. A failed verification after closure returns
the case to the open deadline queue automatically.
Use `appeal --reference OPAQUE_ID` and
`report-confirmed-csam --reference OPAQUE_ID` to record an external decision or
report receipt. These references are stored as hashes; they contain no content.

The 48-hour handling rule is a current legal requirement only if Chirp is a
covered platform and the request is valid under the TAKE IT DOWN Act. This tool
records the operational clock while counsel reviews applicability and edge
cases. It does not make a legal determination.

## Safe handling and removal evidence

Use a restricted evidence location approved by the operator. The case register
records only a digest, media type, opaque evidence reference, observer and time.
Operators must grant evidence access outside this tool only to the assigned
responder and revoke it after review. Recording an access event does not change
any file or provider permission. Removal attempts, verification, appeals and
reappearance are audit events. Do not copy content into tickets, logs, email,
screenshots or test fixtures.

For each Chirp-controlled surface, record a removal attempt and its result:

1. Hide or soft-remove the reported post, comment or chirp through the existing
   scoped moderation capability.
2. Revoke media access by the existing media entitlement path. Feed hiding alone
   is not proof that an object is inaccessible.
3. Search the controlled inventory for known identical copies using a digest or
   other approved non-content reference. Record the search scope and result,
   without storing the image or raw object path in the case register.
4. Retry a transient failure with a new attempt number. Verify each surface with
   an independent read or provider receipt and record the verification reference.
5. Close only after all known controlled surfaces are verified absent. A later
   reappearance reopens the case and receives a new removal attempt and audit
   trail.

Current code evidence limits what can be claimed. `POST /moderation/chirps/{id}/remove`
and the moderation content-removal route soft-remove database content with an
audit trail and campus scope. `GET /media/{token}` rechecks viewer entitlement,
and `app.jobs.media_reconcile` can delete old unreferenced GCS objects only as a
separate dry-run-by-default job with a separate identity. The API runtime cannot
delete permanent `posts/` objects. Therefore the register and drill prove case
handling and evidence discipline; they do not prove production GCS deletion,
provider deletion, or a known-copy detector. Those remain operational gates.

## Bounded known-copy inventory

`backend/app/services/known_copy_inventory.py` is a read-only operator tool for the
supported permanent media prefixes `posts/` and `avatars/`. It lists both prefixes
with explicit object-count, per-object-byte, aggregate-byte and wall-clock limits,
then streams only size/metadata candidates through a bounded SHA-256 hashing sink.
The manifest binds each matching object to its provider generation, size, provider
checksums and computed digest. It contains no image bytes; the CLI prints only counts,
the manifest digest and incomplete reasons. It never deletes or changes a provider
object. A cap, timeout, target-generation change, missing metadata, provider error or
unsupported input makes the manifest incomplete and therefore unsuitable for closure.
The two prefix listings are not an atomic bucket snapshot; concurrent additions or
replacements make the result incomplete where detected and require a fresh scan.

Example, using a private manifest destination:

```sh
python3 -m app.services.known_copy_inventory \
  --bucket "$MEDIA_BUCKET_NAME" \
  --object 'posts/<opaque-user-id>/<object>.jpg' \
  --generation '<provider-generation>' \
  --manifest /private/operator/c437-known-copy-manifest.json
```

This is a discovery receipt, not deletion authority. Do not add a `--delete` option
or run it from an API request. A responder may record its manifest digest as the
`known_copy` verification reference only after independent review. Provider removal,
generation-preconditioned deletion, post-delete verification and IAM approval remain
separate gates.

## Reviewed known-copy removal procedure

`backend/app/services/known_copy_removal.py` is the separate, explicit operator
step for a reviewed synthetic or production plan. `build_removal_plan` accepts only
a complete inventory and binds the bucket, target generation, manifest digest,
`posts/` scope, every object generation and every object SHA-256 into an immutable
plan digest. It refuses incomplete scans, missing generations, malformed references
and scope expansion. `GCSRemovalProvider.delete` sends only conditional deletes with
`if_generation_match`, disabled retries and a bounded deadline; `execute_removal`
writes a private `0600` receipt after each object and can retry only that exact plan.
It never deletes a replacement generation. `verify_removal` reads the reviewed names
again and marks a replacement generation `reappeared`; only a receipt whose every
outcome is `removed_verified` or `already_absent` yields the `c437-removal:<digest>`
reference accepted for a case attempt.

The operator CLI consumes the private inventory JSON directly:

```sh
python3 -m app.services.known_copy_removal plan \
  --inventory /private/operator/c437-known-copy-manifest.json \
  --output /private/operator/c437-removal-plan.json
python3 -m app.services.known_copy_removal execute \
  --plan /private/operator/c437-removal-plan.json \
  --receipt /private/operator/c437-removal-receipt.json \
  --confirm-plan-digest '<reviewed-plan-digest>' --allow-provider-delete
python3 -m app.services.known_copy_removal verify \
  --plan /private/operator/c437-removal-plan.json \
  --receipt /private/operator/c437-removal-receipt.json
```

`execute` is an explicit provider mutation and is blocked unless the operator
supplies both `--allow-provider-delete` and the exact reviewed plan digest. `verify`
is the required fresh read-only step before a case reference is recorded.

The case register rejects an arbitrary known-copy success reference. After reviewing
the private receipt, derive its reference with `c437_receipt_reference` and provide
both the receipt path and the exact `c437-removal:<digest>` value to the
`known_copy` `verified_absent` attempt command. The register verifies the receipt's
private mode, complete outcomes and canonical receipt digest before recording it.
This digest is an integrity check for the operator-supplied receipt, not a
cryptographic attestation from Google Cloud. Closure still requires a fresh
read-only provider verification against the exact plan and retention of that
receipt. The result means the reviewed serving objects were absent at verification
time; it does not prove historical object-version purge or permanent deletion.

For root review, use a disposable bucket and a harmless non-sensitive fixture under
`posts/synthetic-c437/`: create two objects with different names and identical
synthetic bytes, capture their provider generations, run the bounded inventory with
the target generation, review the printed manifest digest, build the posts-only plan,
and execute it with a private receipt path. Read back both names, then recreate one
name to obtain a new generation and run `verify_removal`; the receipt must become
`reappeared` and no second delete may be sent. Repeat with a stale generation and a
forced provider failure to prove conditional refusal and same-plan retry. Root must
approve the bucket, fixture names, service identity, retention and deletion window
before any provider call; this worktree performed no such call.

The adapter follows the documented Google Cloud Storage Python APIs for paginated
listing and `Blob.download_to_file(..., if_generation_match=...)`:
[Bucket listing](https://docs.cloud.google.com/python/docs/reference/storage/latest/google.cloud.storage.bucket.Bucket),
[Blob downloads](https://docs.cloud.google.com/python/docs/reference/storage/latest/google.cloud.storage.blob.Blob),
and [generation preconditions](https://docs.cloud.google.com/python/docs/reference/storage/latest/generation_metageneration).

## Appeals and reappearance

Record an appeal against the case reference with the appellant's contact and
the decision under review, using the same minimum-data rule. Preserve the
original removal decision and assign a second responder where practicable. Do
not restore content while a child-safety or legal-preservation hold is active.
If content reappears, mark the case `reopened`, repeat controlled-surface
removal and known-copy review, and escalate the pattern to the primary and
backup. A dismissed report is not evidence that the underlying content was
safe; record the reason and reviewer.

## Child-safety escalation

Do not request or transmit suspected CSAM through `chirp.shared@gmail.com`, the
case register, or test fixtures. Restrict access, preserve only necessary
metadata and the minimum lawful evidence under the approved process, and
immediately contact the designated child-safety/legal response path. Counsel
must confirm the reporting and preservation route, including any report to
NCMEC or another competent authority, before this is represented as deployed
fulfillment. Record `child_safety_escalate` and any confirmed report reference,
never the material itself. Protect the reporter and subject from unnecessary
disclosure.

## Current official sources (checked 2026-10-06)

- [FTC: Complying with the TAKE IT DOWN Act](https://www.ftc.gov/business-guidance/resources/complying-take-it-down-act)
  says a covered platform must remove validly noticed content and known
  identical copies within 48 hours, and recommends an identifying number and
  status communication.
- [Google Play UGC policy](https://support.google.com/googleplay/android-developer/answer/9876937)
  requires robust moderation plus in-app reporting and blocking for relevant
  UGC apps.
- [Google Play Child Safety Standards](https://support.google.com/googleplay/android-developer/answer/18258653)
  requires published standards, in-app feedback/reporting, action after actual
  knowledge of CSAM, a process for confirmed CSAM reports, and a child-safety
  point of contact for covered social/anonymous-chat categories.
- [Apple App Review Guidelines](https://developer.apple.com/app-store/review/guidelines/)
  section 1.2 requires UGC moderation, reporting with timely responses, user
  blocking and published contact information; applicability to Chirp's
  anonymous features still needs release review.

These sources guide the runbook and do not establish that Chirp is covered by
each law or policy. Counsel/store review and actual production evidence remain
separate acceptance gates.

## Synthetic drill

Run the harmless tabletop in a disposable location:

```sh
python3 scripts/safety_response.py --db /private/tmp/chirp-c437-drill.sqlite3 drill
```

The drill creates one complete notice and one incomplete notice, exercises the
48-hour deadline, restricted evidence manifest, transient failure and retry,
known-copy search, independent verification, reappearance reopening and
child-safety escalation metadata. It contains no harmful media and makes no
network, provider, deletion or notification call.
