# Session recovery and request ownership (c343)

This change addresses F02, F03 and F14 in the mobile audit. It changes client behavior; it does not deploy a build or change Firebase, GCP, backend routes, or database permissions.

## Recovery behavior and budgets

| Operation | Budget | What happens when it expires |
| --- | --- | --- |
| Initial account load or manual session retry | 10 seconds total for token + `/auth/me` + body decoding | A returning Firebase user sees “Can't load your account” with “Try again.” |
| API request | 15 seconds total for fetch + forced refresh + one retry + body decoding | Rejects with safe timeout copy and aborts fetch. |
| Shared Firebase token lookup/forced refresh | 15 seconds | Releases its refresh slot; its late result cannot install a token. |
| Explicit Firebase credential mutation or logout caller | 15 seconds including queue wait | Returns a timeout; the underlying SDK mutation retains queue ownership until it settles. |
| CreateSheet photo network flow | 60 seconds total | Aborts local file read, signed URL request, or PUT and offers retry. Time spent in the OS photo picker is excluded. |

A transient backend/token failure never calls Firebase sign-out. A previously resolved account remains available during a failed background refresh. Initial failure shows recovery; the retry button starts another bounded attempt. This implementation uses manual retry, without an automatic retry loop. Only a 404 whose detail is `user_not_registered` enters onboarding. Firebase reporting no user enters signed-out state.

## Ownership across asynchronous work

One UID and generation own the token store. Explicit auth actions invalidate that ownership before their first await; normal same-user token notifications keep the current generation. Requests capture an owner once and cannot retry with another account's bearer. Provider account, campus, verification, bootstrap, socket callbacks and composer upload completion reject superseded owners.

Q's c379 `applyCampusVerification` hook is retained. A successful redeem invalidates an older verification GET, and a callback captured by the previous account cannot publish into the replacement account.

Firebase commits `currentUser` before returning credentials. Explicit credential/logout SDK mutations therefore run through a queue and the public `beforeAuthStateChanged` veto. App session publication remains quarantined until the current action settles. Timing out the caller does **not** release an unabortable SDK mutation: doing so could let its late callback commit under a newer action's ownership.

If the SDK call stalls indefinitely, another sign-in can also time out while waiting for that queue. Retry can succeed once the underlying SDK call settles; instant recovery from an unabortable SDK stall is not claimed. Token refresh runs outside this queue, so an old generation's stalled token lookup does not block the next generation.

## Upload scope and cancellation

CreateSheet shares one operation across local file read, signed URL acquisition, and GCS PUT. Closing the sheet, unmounting it, or changing account cancels the operation; late picker/upload completions cannot reattach the old photo or show a stale error. The GCS PUT retains the signed content-type/size headers and receives no Firebase bearer.

Profile upload UI was outside this lane's owned files. Its existing separate URL and PUT calls now have the API's finite defaults (15 seconds and 60 seconds respectively), but they do not yet share one overall deadline or a profile-screen cancellation lifecycle. Follow-up work is still needed there.

## Validation and remaining device proof

`npm run verify:c343-session` transpiles and executes the production TypeScript handlers against deferred Firebase/network boundaries, a fake clock, and a component hook scheduler. It checks recovery rendering and retry, account/token races, SDK pre-commit race ordering modeled from the installed SDK, late secondary fetches, Q's redeem race, socket ownership, upload cancellation and safe timeout copy. It is registered in CI alongside the existing verifiers. It is not a native Firebase/provider integration test.

A deployed device/build check remains required: cold start while offline; restore connectivity and retry; sign out or switch accounts during token/credential acquisition; cancel Apple/Google sheets; stall/retry photo upload then close the composer; confirm verified campus access after redeem. Native SDK persistence, OS suspension, and deployed network behavior have not been claimed as verified by these local checks.
