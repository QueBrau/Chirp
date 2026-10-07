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

The Terms/Privacy acknowledgement does not establish chapter officer or payment
authority. Those remain separate server-owned membership and Stripe-account checks
and are deferred remaining c438 acceptance work, rather than being inferred from
legal consent.
A future authority confirmation must bind an explicit chapter role or connected
account identity, use a versioned confirmation record, and require re-confirmation
after role or connected-account changes. c438 deliberately leaves that confirmation
out rather than treating legal consent as financial authority. WebSocket handshake
authorization is also unchanged: it continues to use its existing Firebase identity
and suspension checks, and is deferred remaining c438 acceptance work rather than
being covered by the HTTP enforcement flag. Store review and app-store age-rating
classification remain release-owner steps after the mobile policy screen is reviewed.

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
