# web/ — the Chirp public website

Vite + React + TypeScript, built to static files and served by **Firebase
Hosting** on the existing `chirps-prod` project — the same project as Cloud Run,
Cloud SQL and Firebase Auth. No new vendors.

Nothing here calls the Chirp API. No auth, no fetch, no CORS change. That is
what keeps a marketing site from ever being able to break the product.

## This is not a marketing nice-to-have

Three of these routes are wired into product behaviour:

| Route | Who needs it | What breaks without it |
| --- | --- | --- |
| `/stripe/connect/return` | `backend/app/routers/payments.py:40` | Stripe Connect onboarding. `_onboarding_urls()` raises `503 app_public_base_url_not_configured` until `APP_PUBLIC_BASE_URL` is set, and Stripe rejects `chirp://` schemes, so it must be a real https page |
| `/stripe/connect/refresh` | same | the retry path when a single-use Account Link expires |
| `/join-chapter` | invite sharing | a raw `chirp://` link arrives as dead text when pasted into a message |
| `/privacy` | the App Store | submission is rejected without a publicly reachable privacy-policy URL |
| `/terms`, `/privacy` | `app-mobile/app/(auth)/sign-in.tsx:198` | the sign-in screen already promises both documents to every user |

**The two Stripe routes are a contract with the backend.** `payments.py:40`
builds them by string concatenation and Stripe hits them exactly as written. If
you rename them in `src/App.tsx`, that function changes in the same commit.

## Why React, and what it costs

The site is a single-page app: Firebase rewrites every path to `index.html` and
React Router resolves the route on the client. The header, nav and footer exist
once in `src/components/Layout.tsx` instead of being hand-copied into nine files
that can drift apart.

Two consequences worth knowing rather than discovering later:

- **Pages need JavaScript to render.** The HTML shell is nearly empty. Browsers
  and the App Store reviewer are fine; a crawler that does not execute JS sees
  very little. If `/privacy` ever needs to be readable without JS, that is the
  reason to reach for server rendering — and it would mean leaving Firebase
  Hosting, since it only serves static files.
- **The bundle is ~218 KB (~66 KB gzipped)** versus effectively zero JS for the
  hand-written HTML this replaced. Fine for a marketing site; it is a real
  regression in first-paint cost for the three bounce pages, which are the ones
  a user hits mid-flow on a phone.

`usePageMeta()` sets the per-route `<title>` and description, because an SPA
otherwise serves the landing page's title on every path — including `/privacy`,
which is the URL submitted to Apple.

## Develop

```sh
cd web
npm install
npm run dev          # vite dev server, hot reload
npm run typecheck    # tsc --noEmit
npm run build        # tsc -b && vite build -> dist/
npm run preview      # serve the real production build with SPA fallback
```

Always QA against `npm run preview`, not `npm run dev` — the dev server resolves
routes differently, and the routing is the part most likely to break.

## Before the first deploy — required

1. **Replace every `CONTACT_EMAIL_PLACEHOLDER`.** There is no confirmed public
   contact address, so the literal placeholder is used rather than inventing a
   domain we do not own.
   ```sh
   grep -rn CONTACT_EMAIL_PLACEHOLDER src/
   ```
2. **Fill the company paragraph in `src/pages/About.tsx`**, marked with a
   `JOSE:` comment.
3. **Get `src/pages/Terms.tsx` sections 8, 9 and 10 written by a lawyer.** They
   are deliberately left as marked placeholders. Liability, arbitration and
   governing law drafted by an agent read as enforceable while being untailored
   to your entity and your state — worse than an empty section.
4. **Resolve the `NEEDS DECISION` comments in `src/pages/Privacy.tsx`.** Each is
   a product or legal call, not a writing task: retention for soft-deleted
   content, ghost profiles for people who are not users, the minimum age, and
   which privacy regimes apply.

Everything in `Privacy.tsx` that is *not* marked is a factual statement about
what the code actually stores, traced to a model under `backend/app/models/`. If
the schema changes, that page changes with it.

## Deploy

```sh
cd web
npm run build
firebase deploy --only hosting --project chirps-prod
```

`firebase.json` and `.firebaserc` live here, so `web/` is a self-contained
deployable the same way `backend/` and `app-mobile/` are. This never touches the
Cloud Run service: different CLI, different config, different product. They only
share a GCP project.

## Verify after deploying

The two Stripe routes must resolve with a single `200`, not a redirect — Stripe
hits them exactly as `payments.py` builds them, and a redirect hop there fails
confusingly, long after a real user has finished KYC.

All three must print `200`. The two Stripe lines must print `200` and not `301`
or `308`. (No inline comments below on purpose — Jose's zsh has
`interactive_comments` off, so a trailing `#` gets handed to `head` as an
argument and the check errors instead of answering.)

```sh
curl -sI https://chirps-prod.web.app/privacy | head -1
curl -sI https://chirps-prod.web.app/stripe/connect/return | head -1
curl -sI https://chirps-prod.web.app/stripe/connect/refresh | head -1
```

Then open `/join-chapter?code=TEST123` in a real browser and confirm the button
targets `chirp://join-chapter?code=TEST123`. `curl` cannot check this one: the
code is rendered by React, so the HTML shell alone will not contain it.

Only then point the backend at the site:

```sh
gcloud run services update chirp-api --region=us-central1 --project=chirps-prod \
  --update-env-vars=APP_PUBLIC_BASE_URL=https://chirps-prod.web.app
```

## Not done yet, on purpose

- **No `.well-known/` files.** `apple-app-site-association` needs a real Apple
  Team ID and `assetlinks.json` needs a release signing-cert fingerprint from
  EAS. Neither exists. A file with a fabricated Team ID serving `200` would read
  as finished while doing nothing, so there is nothing there instead. They go in
  `public/.well-known/` when those values exist.
- **Universal links are not wired.** `app-mobile/app.json` still declares the
  placeholder `applinks:chirp.example.com`. Until a real domain and Team ID
  exist, the visible "Open Chirp" button on the bounce pages is the mechanism,
  not a fallback.
- **No custom domain.** `chirps-prod.web.app` unblocks Stripe and the App Store
  today; a domain purchase is a separate decision.
- **No store badges.** The app is not published, so linking one would 404.
