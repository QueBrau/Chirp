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
the UTC received time and the deadline at `received + 48 hours`; assign Jose or
Braulio and acknowledge the case reference.

The 48-hour handling rule is a current legal requirement only if Chirp is a
covered platform and the request is valid under the TAKE IT DOWN Act. This tool
records the operational clock while counsel reviews applicability and edge
cases. It does not make a legal determination.

## Safe handling and removal evidence

Use a restricted evidence location approved by the operator. The case register
records only a digest, media type, opaque evidence reference, observer and time.
Access is granted to the assigned responder for a stated purpose and revoked
after review; every grant, removal attempt, verification, appeal and reappearance
is an audit event. Do not copy content into tickets, logs, email, screenshots or
test fixtures.

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
