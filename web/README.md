# web/ — the Chirp public website

Plain static HTML and CSS. No build step, no framework, no bundle, no auth, and
nothing here calls the Chirp API. That is deliberate: this site has to load fast
for people who are not signed in, and two of its pages are load-bearing for the
backend (see below), so it must not be able to break at build time.

Deployed to **Firebase Hosting** on the existing `chirps-prod` project — the same
project as Cloud Run, Cloud SQL and Firebase Auth. Zero new vendors.

## This is not a marketing nice-to-have

Three of these pages are wired into product behaviour:

| Path | Who needs it | What breaks without it |
| --- | --- | --- |
| `/stripe/connect/return` | `backend/app/routers/payments.py:40` | Stripe Connect onboarding. `_onboarding_urls()` raises `503 app_public_base_url_not_configured` until `APP_PUBLIC_BASE_URL` is set, and Stripe rejects `chirp://` schemes, so it must be a real https page |
| `/stripe/connect/refresh` | same | the retry path when a single-use Account Link expires |
| `/join-chapter` | invite sharing | a raw `chirp://` link dies when pasted into a text message |
| `/privacy` | the App Store | submission is rejected without a publicly reachable privacy-policy URL |
| `/terms`, `/privacy` | `app-mobile/app/(auth)/sign-in.tsx:198` | the sign-in screen already promises both documents to every user |

**The two Stripe paths are a contract with the backend.** They are built by string
concatenation in `payments.py:40`. If you move or rename those directories, that
function has to change in the same commit.

## Before the first deploy — required

1. **Replace every `CONTACT_EMAIL_PLACEHOLDER`.** There is no confirmed public
   contact address, so the literal placeholder was used rather than inventing a
   domain we do not own. Find them all with:
   ```sh
   grep -rn CONTACT_EMAIL_PLACEHOLDER public/
   ```
2. **Fill the company paragraph in `about.html`**, marked with a `JOSE:` comment.
3. **Get `terms.html` sections 8, 9 and 10 written by a lawyer.** They are
   deliberately left as marked placeholders. Liability, arbitration and governing
   law drafted by an agent would read as enforceable while being untailored to
   your entity and your state — worse than having nothing there.
4. **Resolve the `NEEDS DECISION` comments in `privacy.html`.** Each is a product
   or legal call, not a writing task: data retention for soft-deleted content,
   ghost profiles for people who are not users, the minimum age, and which
   privacy regimes apply.

Everything in `privacy.html` that is *not* marked is a factual statement about
what the code actually stores, traced to a model under `backend/app/models/`. If
the schema changes, this page has to change with it.

## Deploy

```sh
cd web
firebase login                              # human with access to chirps-prod
firebase deploy --only hosting --project chirps-prod
```

`firebase.json` and `.firebaserc` live in this directory, so `web/` is a
self-contained deployable the same way `backend/` and `app-mobile/` are. This
never touches the Cloud Run service: different CLI, different config, different
product. They only share a GCP project.

Preview locally without deploying:

```sh
cd web && firebase emulators:start --only hosting
```

## Verify after deploying

The one thing worth checking first is that the two Stripe paths resolve with a
single `200` and not a redirect — Stripe hits them exactly as `payments.py` builds
them, with no trailing slash, and a redirect hop there fails in a confusing way
much later, after a real user has already finished KYC.

```sh
curl -sI https://chirps-prod.web.app/privacy                | head -1   # 200
curl -sI https://chirps-prod.web.app/stripe/connect/return  | head -1   # 200, NOT 301/308
curl -sI https://chirps-prod.web.app/stripe/connect/refresh | head -1   # 200, NOT 301/308
curl -s  "https://chirps-prod.web.app/join-chapter?code=ABC123" | grep -c "join-chapter"
```

Then, and only then, point the backend at it:

```sh
gcloud run services update chirp-api --region=us-central1 --project=chirps-prod \
  --update-env-vars=APP_PUBLIC_BASE_URL=https://chirps-prod.web.app
```

## Not done yet, on purpose

- **`.well-known/` is empty.** `apple-app-site-association` needs a real Apple
  Team ID and `assetlinks.json` needs a release signing-cert fingerprint from EAS.
  Neither exists yet. A file with a fabricated Team ID serving `200` would read as
  finished while doing nothing, so there is nothing there instead.
- **Universal links are not wired.** `app-mobile/app.json` still declares the
  placeholder `applinks:chirp.example.com`. Until a real domain and Team ID exist,
  the visible "Open Chirp" button on the bounce pages is the mechanism, not a
  fallback.
- **No custom domain.** Shipping on `chirps-prod.web.app` unblocks Stripe and the
  App Store today; a domain purchase is a separate decision.
- **No store badges.** The app is not published, so linking one would 404.
