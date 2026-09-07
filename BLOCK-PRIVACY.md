# Block privacy and deployment (c342)

Named actions must not identify an anonymous Chirp's author by changing which
Chirps appear. Anonymous actions must not identify that author by changing the
named feed. The two intentions are independent, and either still prevents the
blocked person from contacting the blocker.

| Stored intention | Named posts/comments hidden | Author's Chirps hidden | Incoming contact refused |
| --- | --- | --- | --- |
| Named only | Yes | No | Yes |
| Anonymous only | No | Yes | Yes |
| Both | Yes | Yes | Yes |

This refines c279's earlier rule that a named block hid everything. That rule
allowed a caller to block candidate accounts by name and identify anonymous
authors by watching Chirps disappear or return. Preserving anonymous intent alone
would fix the named-delete defect but leave this visibility probe open.

`source='named'` enables named-feed filtering. A non-null
`anonymous_created_at` independently enables Chirps filtering. When only anonymous
intent remains, `source='by_chirp'`; the database refuses a row with that source and
no anonymous marker. Contact enforcement still consults any surviving pair and
keeps c243's asymmetric direction: the blocker's own outbound attempts do not
disclose which anonymous author they blocked.

The anonymous timestamp never appears in an API response. Named create returns
the time of the current named action, whether it inserted a row or added named
intent to an anonymous row. A repeated named create conflicts; an anonymous-only
row behaves like no named row. Named delete removes only named intent, returns
the same 404 for absent and anonymous-only rows, and cannot restore anonymous
Chirps. Upserts and row locks preserve these intentions across concurrent writes.

Anonymous undo is `DELETE /moderation/blocks/by-chirp/{chirp_id}`. It resolves the
author on the server, requires the same verified campus and self-block checks as
anonymous create, and returns an empty 204 whether or not anonymous intent existed.
It preserves any named intent. A retained, soft-removed Chirp remains a usable
reference; a missing or physically purged Chirp returns 404. The mobile API wrapper
accepts only the Chirp ID. This change does not add a settings screen or an undo
button, and callers must retain the reference if they want to offer undo.

These are response and state-transition guarantees. Database lock contention and
I/O are not constant time. Hiding all of one author's Chirps also groups their
anonymous posts by visibility; this change does not promise unlinkability between
anonymous posts or protection against colluding accounts.

## Migration and rollout

Migration 0034 follows the separately reserved 0033 payment migration. Check the
actual chain with `alembic heads` before merge or deployment; do not create another
head to bypass that dependency.

Earlier named upgrades discarded whether an anonymous intent ever existed. That
history cannot be reconstructed from `source`. Migration 0034 therefore preserves
the anonymous hide for **every existing row**, setting its marker to `created_at`.
This is conservative protection, not recovered proof of anonymous intent. A
legacy named block consequently keeps hiding Chirps after its named intent is
removed; an explicit anonymous undo can remove that protection through a retained
Chirp reference. New named-only writes explicitly insert SQL NULL and follow the
table above.

The column's `now()` server default preserves the old hide behavior for older
revisions that omit it during migrate-before-deploy. It does not repair all old
routes: an older named DELETE can still erase both intentions, and older named
operations still filter the anonymous feed. Rollout must update both API and WS
services and verify every serving revision before privacy acceptance is closed.
Do not treat a mixed deployment or unauthenticated health response as proof of
the fix. No production migration or deployment is performed by these tests.

After migration and complete deployment, verify with test accounts on a verified
campus: anonymous block, candidate named create/delete cycles, anonymous undo,
both feed surfaces, and incoming contact refusal while either intention remains.
Compare named action timestamps and response shapes as well as status codes.

Downgrade keeps all pairs and their `source`, so old readers retain contact
protection and hide at least the content already hidden. It discards independent
intent and restores the older privacy weaknesses. Rolling forward after a
downgrade conservatively marks every remaining row anonymous again; a downgrade
is not an accepted privacy rollback.

## Regression coverage

`test_c342_block_intents.py` covers named DELETE probing, old timestamp exposure,
repeated named cycles, independent visibility, anonymous undo, campus/self/auth
guards, removed references, recipient protection, and concurrent named/anonymous
writes. The first three scenarios fail against the pre-fix implementation.
`test_c342_block_migration.py` runs seeded upgrade/downgrade/upgrade and verifies
legacy backfill, legacy writer defaults, explicit NULL, and database constraints.
The c279, blocks, contact-blocks, and feed-blocks suites preserve the surrounding
privacy and safety behavior.
