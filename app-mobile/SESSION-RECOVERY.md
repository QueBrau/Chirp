# Session and live-update recovery (c343, c346)

These changes address F02, F03, F06 and F14 in the mobile audit. They change client behavior; they do not deploy a build or change Firebase, GCP, backend routes, or database permissions.

## Recovery behavior and budgets

| Operation | Budget | What happens when it expires |
| --- | --- | --- |
| Initial account load or manual session retry | 10 seconds total for token + `/auth/me` + body decoding | A returning Firebase user sees “Can't load your account” with “Try again.” |
| API request | 15 seconds total for fetch + forced refresh + one retry + body decoding | Rejects with safe timeout copy and aborts fetch. |
| Shared Firebase token lookup/forced refresh | 15 seconds | Releases its refresh slot; its late result cannot install a token. |
| Explicit Firebase credential mutation or logout caller | 15 seconds including queue wait | Returns a timeout; the underlying SDK mutation retains queue ownership until it settles. |
| CreateSheet photo network flow | 60 seconds total | Aborts local file read, signed URL request, or PUT and offers retry. Time spent in the OS photo picker is excluded. |
| WebSocket connection attempt | 10 seconds to open | Retires the attempt and uses the finite transport retry budget. |
| WebSocket auth revalidation | 10 seconds total for fresh token + `/auth/me` + body decoding, once per explicit run | Shows account recovery; a late result cannot start another socket. |

A transient backend/token failure never calls Firebase sign-out. A previously resolved account remains available during a failed ordinary background refresh. Initial failure or failed socket-auth revalidation shows recovery; the retry button starts another bounded account attempt. Account recovery uses manual retry, without an automatic account retry loop. Only a 404 whose detail is `user_not_registered` enters onboarding. Firebase reporting no user enters signed-out state.

## Live-update recovery (c346)

The gateway uses `4401` for missing/invalid authentication or an unknown user, `4403` for suspension, and `4503` for unavailable Redis fan-out. The first observed `4401` or `4403` in a socket run asks SessionProvider to force a token refresh and load `/auth/me` under one ten-second budget. That response determines ready, unregistered, or suspended status. A close code alone cannot invent suspension. Ready permits one new socket; another terminal auth close in the same run stops and exposes “Can't load your account” / “Try again.” A successful revalidation keeps Provider ready without restarting its connection effect or resetting this budget.

Transport failures, including `4503`, receive at most six consecutive automatic reconnects after the initial attempt. Delays use 50–100% jitter around exponential 1, 2, 4, 8, 16 and 30 second caps. Only five seconds continuously open resets the transport failure count; brief open/close cycles do not. This stability reset does not replenish the auth recovery allowance. Missing connection callbacks time out, and an error without a following close retires the failed socket.

After the transport budget is exhausted, the HTTP account stays ready and the tabs show “Live updates paused” with “Retry live updates.” The action coalesces repeated taps, refreshes the current token/account, and starts a new explicit run only if that same account remains ready. Ordinary repeated `connect()` calls cannot replenish a running or paused budget.

The backend can reject authentication before accepting the WebSocket. Some runtimes expose that as a generic network error/`1006`, so those failures use the finite transport budget; the explicit retry still performs authoritative account revalidation. This implementation does not claim that every runtime delivers the application's close code.

Each run and socket belongs to the current UID/generation. Retiring it detaches all handlers, cancels connect/stability/reconnect timers and pending auth recovery, and guards callbacks already queued by the runtime. Logout/account replacement closes the owned connection; late A callbacks or `/me` answers cannot deliver events to B, retire B's socket or start another A attempt. App foreground/OS suspension behavior and end-to-end Redis delivery still require their separate device/deployment checks.

## Ownership across asynchronous work

One UID and generation own the token store. Explicit auth actions invalidate that ownership before their first await; normal same-user token notifications keep the current generation. Requests capture an owner once and cannot retry with another account's bearer. Provider account, campus, verification, bootstrap, socket callbacks and composer upload completion reject superseded owners.

Q's c379 `applyCampusVerification` hook is retained. A successful redeem invalidates an older verification GET, and a callback captured by the previous account cannot publish into the replacement account.

Firebase commits `currentUser` before returning credentials. Explicit credential/logout SDK mutations therefore run through a queue and the public `beforeAuthStateChanged` veto. App session publication remains quarantined until the current action and every older queued or in-flight SDK mutation settle. Canceling a newer native sheet does not release that quarantine while an older SDK call can still commit after its pre-commit veto. After settlement, an unexpected SDK UID remains quarantined until a later explicit action succeeds; a failed/cancelled action may restore only its recorded baseline UID or an empty Firebase session. Timing out the caller does **not** release an unabortable SDK mutation: doing so could let its late callback commit under a newer action's ownership.

If the SDK call stalls indefinitely, another sign-in can also time out while waiting for that queue. Retry can succeed once the underlying SDK call settles; instant recovery from an unabortable SDK stall is not claimed. Token refresh runs outside this queue, so an old generation's stalled token lookup does not block the next generation.

## Upload scope and cancellation

CreateSheet shares one operation across local file read, signed URL acquisition, and GCS PUT. Closing the sheet, unmounting it, or changing account cancels the operation; late picker/upload completions cannot reattach the old photo or show a stale error. The GCS PUT retains the signed content-type/size headers and receives no Firebase bearer.

Profile upload UI was outside this lane's owned files. Its existing separate URL and PUT calls now have the API's finite defaults (15 seconds and 60 seconds respectively), but they do not yet share one overall deadline or a profile-screen cancellation lifecycle. Follow-up work is still needed there.

## Validation and remaining device proof

`npm run verify:c343-session` transpiles and executes the production TypeScript handlers against deferred Firebase/network boundaries, a fake clock, and a component hook scheduler. It checks recovery rendering and retry, account/token races, SDK pre-commit race ordering modeled from the installed SDK, late secondary fetches, Q's redeem race, socket ownership, upload cancellation and safe timeout copy. Its c346 cases execute the actual Provider/socket integration for terminal auth close codes, suspension, unregistered accounts, stalled/expired auth, finite `4503` retries, missing callbacks, retry UI and logout/account replacement during recovery. It is registered in CI alongside the existing verifiers. It is not a native Firebase/provider integration test.

A deployed device/build check remains required: cold start while offline; restore connectivity and retry; sign out or switch accounts during token/credential acquisition; cancel Apple/Google sheets; stall/retry photo upload then close the composer; confirm verified campus access after redeem; expire a socket token, suspend an open session, and interrupt Redis/network delivery to verify recovery and the paused-live-updates action. Native SDK persistence, OS suspension, actual runtime close-code delivery, and deployed network behavior have not been claimed as verified by these local checks.
