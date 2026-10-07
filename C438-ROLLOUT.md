# c438 legal acceptance rollout

Migration 0040 creates the current Terms and Privacy policy rows and append-only
account acceptance records. The API accepts only the exact current versions, records
the account, timestamp, age category, guardian confirmation when age 17, and source.

`LEGAL_ENFORCEMENT_ENABLED` defaults to `false` for the compatibility window. In
that mode, old clients can still use existing operations, while `/auth/me` and the
mobile session gate expose `legal_required`. After the mobile release is available
to the supported clients, enable the flag in a new revision. Product routes using
`get_current_user`, including organization and payment routes, then return
`428 legal_acceptance_required` until the current policies are accepted. Legal status
and acceptance remain available; malformed policy configuration fails closed with
`503 legal_policy_unavailable`.

Rollout sequence: apply migration 0040, deploy the compatibility image, verify the
current policy rows and acceptance endpoint, release the mobile gate, then enable
the enforcement flag. Rollback disables the flag first, then rolls back the image;
the migration downgrade is available for a reviewed schema rollback and removes only
the c438 tables.

Organization creation and Stripe setup use a separate explicit declaration of
organization authority. Migration 0040 also creates append-only
`organization_authority_acceptances`, recording account, chapter, membership,
role term, role, policy version, time and (for payment setup) the bound Stripe
account. The current organization/payment policy version is `2026-10-06` and its
URL is `https://chirpsocials.com/payments`. Increment the server version whenever
these terms materially change. New mobile actions fetch the current version and
ask the authorized person to confirm it; they do not infer authority from Terms
acceptance or grant a role based on the declaration.

For setup, the client confirms a snapshot of the active membership, role term and
connected account. The API checks it again before and after provider calls.
A changed role term (including a round trip to the same role) or account binding
requires another confirmation. The initial authorization to create an account is
recorded before provider work, then bound to the resulting account. Failed
provider work does not falsely claim successful onboarding. Stripe obtains its
own agreement from its authorized representative. These records cover org
creation and payment setup; existing server permissions still govern all other
organization operations. Default-off compatibility permits old clients without
the new declarations; enabling enforcement makes absence a 428.

WebSocket handshakes and the existing bounded authorization poll now use the same
policy check as HTTP when enforcement is enabled. Unaccepted sessions close with
4428, malformed policy configuration with 4503. The mobile socket revalidates on
4428 or an authenticated HTTP428 probe and preserves `legalRequired`, so users
see the material-change screen. A policy change on an open socket is detected at
the existing 24-36 second reconciliation interval. A pre-accept close can be
collapsed to HTTP403 by transport layers; the authenticated client probe handles
that path. No SQL session is retained while writing to the socket.

Review and apply the following narrowly scoped grants only after migration0040
exists and the runtime identities are verified; this document applies none:

```sql
GRANT SELECT ON TABLE legal_policies TO chirp_api, chirp_ws;
GRANT SELECT, INSERT ON TABLE legal_acceptances TO chirp_api;
GRANT SELECT ON TABLE legal_acceptances TO chirp_ws;
GRANT SELECT, INSERT ON TABLE organization_authority_acceptances TO chirp_api;
```

Neither runtime can activate policies or modify/delete acceptance history with
these grants. Migration0040 is still undeployed and unmerged, so the authority
table is included in that reserved migration rather than allocating Q's0042.
Fresh validation databases must apply the revised0040 from scratch.

Before enabling enforcement, the API deployment role grants must be reviewed and
present for the operators and runtime identity that apply migration 0040 and set the
flag. The migration and policy seed are database operations; the mobile client never
gets authority to create, activate, or supersede a policy version.

The current policy mapping is explicit: Terms `2026-10-06` maps to
`https://chirpsocials.com/terms`, and Privacy `2026-10-06` maps to
`https://chirpsocials.com/privacy`, both effective 2026-10-06. A material edit gets
a new database version and effective date, and clients must re-accept both current
rows together.

## Store policy review (2026-10-07)

Google Play’s [Age-Restricted Content and Functionality policy](https://support.google.com/googleplay/android-developer/answer/16302250?hl=en)
requires blocking minors through Play Console tools when a core feature enables
anonymous communication or random connections; its expansion is dated
August 26, 2026 in the [distribution policy update](https://developer.android.com/distribute/play-policies).
Chirp’s campus board deliberately hides authors and assigns daily pseudonyms, so
campus or organization scope does not establish an exemption. Verified .edu status
also does not resolve the under-18 classification. The product choice remains
access from age 17 with guardian permission; this review does not claim Google
approval. Store classification and the Play Console blocking configuration remain
release-owner decisions after policy and safety review.

Apple’s [App Store Review Guidelines 1.2 and 2.3.6](https://developer.apple.com/app-store/review/guidelines/)
require UGC filtering, reporting, blocking, contact information, and accurate age
rating; primarily anonymous chat apps may not be allowed and may be removed. Chirp
has campus and org scopes plus named DMs, but those facts alone do not prove that its
anonymous board is outside the guideline’s concern. This is a reviewed
classification issue, not a silent age change; the approved 17+ product choice
remains pending the release owner’s final store submission and age-rating decision.
