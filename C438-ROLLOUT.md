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
