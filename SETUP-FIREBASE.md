# Firebase setup (milestone 1 — real auth)

Everything the app and backend need for Firebase Auth is already wired up **behind
placeholders**: `app-mobile/src/auth/config.ts` has `hasFirebaseConfig()` returning
`false`, and the backend defaults to `AUTH_MODE=emulated`. Nothing breaks until you
do this checklist — flipping it on is the last step, not a prerequisite for anyone
else's work.

Owner: Jose or Q. Should take ~20 minutes end to end, plus Apple Developer Program
approval time if that's not already sorted (see §6).

---

## 1. Create the Firebase project

1. https://console.firebase.google.com → **Add project**.
2. Name it `chirp` (or `chirp-prod` / `chirp-dev` if you want separate dev/prod
   projects — recommended eventually, but one project is fine to start).
3. Google Analytics: optional, skip it (Enable Google Analytics → toggle off) —
   nothing in Chirp depends on it and it's one less thing to configure.
4. This Firebase project should be **the same GCP project** the backend deploys to
   (Cloud Run + Cloud SQL, per SPEC §1) — Firebase project creation lets you attach
   to an existing GCP project instead of making a new one. Pick the existing one if
   it already exists; otherwise let Firebase create it and use that project going
   forward for Cloud Run/Cloud SQL/Secret Manager.

## 2. Enable the three auth providers

Console → **Build → Authentication → Sign-in method → Add new provider**.

- **Email/Password** — enable it. Leave "Email link (passwordless sign-in)" off;
  Chirp uses password auth (`src/auth/session.ts`).
- **Google** — enable it. Set the public-facing "Project support email" when
  prompted (required for the consent screen). Firebase auto-generates the OAuth
  client IDs; you don't create them by hand.
- **Apple** — enable it. This one needs more than a toggle — see §5 before you can
  actually flip it to "Enabled" with a working config (Services ID, key, team ID).
  It's fine to leave it in a half-configured state while you do §3/§4 and come back.

## 3. Register the apps

Console → **Project settings (gear icon) → General → Your apps → Add app**.

Register three apps under this one Firebase project (one Firebase project, three
app registrations — that's normal):

### iOS app
- Bundle ID: `app.chirps.mobile` (matches `app-mobile/app.json` → `expo.ios.bundleIdentifier`).
- App Store ID: leave blank for now (fill in once you have one).
- Download `GoogleService-Info.plist` — you won't need to commit this or wire it
  into the Expo config for milestone 1 (JS SDK config in `config.ts` is enough for
  email/password + the web-based Google/Apple flows). It becomes relevant when the
  dev build adds native Google/Apple Sign-In modules (see §4 note).

### Android app
- Package name: `app.chirps.mobile` (matches `app-mobile/app.json` → `expo.android.package`).
- SHA-1 certificate fingerprint: **required for Google Sign-In on Android**, skip
  for now if you're only wiring email/password — add it later when the dev build
  lands (`eas credentials` or `keytool` gives you the SHA-1).
- Download `google-services.json` — same story as the plist above, not needed yet.

### Web app
- Nickname: `chirp-web` (internal label only, doesn't ship anywhere).
- **This is the one that matters for milestone 1.** The config object Firebase
  shows you after registering (`apiKey`, `authDomain`, `projectId`, `storageBucket`,
  `messagingSenderId`, `appId`) is what the Firebase JS SDK uses on all platforms
  in Expo — see §4.

## 4. Wire the config into the mobile app

Open `app-mobile/src/auth/config.ts`. Replace every `"REPLACE_ME_..."` placeholder
with the matching value from the **web app** config you just registered (§3):

| `config.ts` field | Firebase console value |
|---|---|
| `apiKey` | `apiKey` |
| `authDomain` | `authDomain` (looks like `chirp-xxxxx.firebaseapp.com`) |
| `projectId` | `projectId` |
| `storageBucket` | `storageBucket` |
| `messagingSenderId` | `messagingSenderId` |
| `appId` | `appId` (the web app's `appId`, not iOS/Android's) |

Once every field is filled in, `hasFirebaseConfig()` in that same file flips to
`true` automatically (it just checks that nothing starts with `REPLACE_ME`
anymore) — no other code change needed. That's what switches `sign-in.tsx` off
the "Demo mode — Firebase not configured" fallback and onto real
`signInWithEmail` / `signUpWithEmail` (`src/auth/session.ts`).

**Do not commit real values to a public repo** if this repo is or becomes public —
`apiKey` here is not a secret in the security sense (Firebase web config is
designed to be public, protected by Firebase Auth's domain/app-check rules
instead), but treat it as config-not-secret and keep it consistent with wherever
the rest of the team's env conventions land.

Google/Apple buttons stay visual-only (mock flow) even after this step —
`expo-auth-session` (Google) and `expo-apple-authentication` (Apple) need native
module config that only works in an EAS dev build, not the JS-only setup this
milestone builds on. That's milestone-1-follow-up, not blocked by anything above.

## 5. Apple Sign-In — the extra setup Apple requires

Apple Sign-In needs its own credential chain in the **Apple Developer** portal
before Firebase's "Apple" provider toggle can go from configured-but-broken to
actually working:

1. **Apple Developer Program membership** ($99/yr) — required, no way around it.
2. **App ID**: developer.apple.com → Certificates, IDs & Profiles → Identifiers →
   your App ID (`app.chirps.mobile`) → enable the **Sign In with Apple** capability.
3. **Services ID**: create a new Services ID (a *different* identifier, e.g.
   `app.chirps.mobile.signin`) → enable Sign In with Apple → configure it with:
   - Primary App ID: `app.chirps.mobile`
   - Return URL: the `authDomain` from §4, i.e.
     `https://chirp-xxxxx.firebaseapp.com/__/auth/handler`
4. **Key**: Certificates, IDs & Profiles → Keys → new key → enable Sign In with
   Apple → download the `.p8` file (**one-time download, Apple will not let you
   re-download it** — store it in the team's password manager or GCP Secret
   Manager immediately).
5. Back in Firebase console → Authentication → Sign-in method → Apple → fill in:
   - Services ID (from step 3)
   - Apple Team ID (top-right of the Apple Developer portal)
   - Key ID + the `.p8` file contents (from step 4)
6. Save. The provider now shows "Enabled" for real.

### App Store requirement (why this isn't optional)

**Apple App Store Review Guideline 4.8**: if an app offers any third-party or
social login (Google, in Chirp's case), it must also offer Sign In with Apple as
an equivalent option. SPEC §1 already calls this out ("Apple required by App
Store if any social login exists") — this isn't a nice-to-have, it's a submission
blocker. Do this before TestFlight/App Store submission (SPEC §7, milestone 7),
not after.

## 6. Service account key → backend `auth_mode=firebase`

The backend verifies ID tokens server-side via `firebase_admin`
(`app/middleware/auth.py`, already written and tested against a mocked SDK in
`backend/tests/test_auth_firebase_mode.py`). It needs Application Default
Credentials to do that — a service account key, not the web config from §4.

1. Firebase console → Project settings → **Service accounts** → **Generate new
   private key**. Downloads a JSON file — treat it as a full-access credential to
   the project, because it is one.
2. **Store it in GCP Secret Manager**, not in the repo, not in `.env` committed
   anywhere:
   ```bash
   gcloud secrets create firebase-service-account \
     --data-file=path/to/the-downloaded-key.json \
     --project=<your-gcp-project-id>
   ```
3. Grant the Cloud Run service's runtime service account `roles/secretmanager.secretAccessor`
   on that secret, and mount it as `GOOGLE_APPLICATION_CREDENTIALS` (a file path,
   via Cloud Run's "Secrets" volume mount UI/flag) or set
   `GOOGLE_APPLICATION_CREDENTIALS` to point at the mounted path. `firebase_admin.initialize_app()`
   (called with no args in `app/middleware/auth.py`) picks up Application Default
   Credentials automatically from that env var — no code change needed.
4. Set these env vars wherever the backend runs (Cloud Run env vars, or
   `backend/.env` for a local real-auth test):
   ```
   AUTH_MODE=firebase
   FIREBASE_PROJECT_ID=<the projectId from §3/§4>
   ```
5. Install the optional dependency if it isn't already in the deployed image:
   `pip install -e ".[firebase]"` (it's declared in `pyproject.toml` under
   `[project.optional-dependencies] firebase`).

### Local sanity check before deploying

Skip the install line if the extra is already present. It carries no inline
comment on purpose: Jose's zsh has `interactive_comments` off, so a trailing `#`
is passed to pip as arguments rather than ignored.

```bash
cd backend
.venv/bin/pip install -e ".[firebase]"
export AUTH_MODE=firebase
export FIREBASE_PROJECT_ID=<your-project-id>
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/the-downloaded-key.json
.venv/bin/uvicorn 'app.main:create_app' --factory --port 8000
```

Then hit any protected route with a **real** Firebase ID token (grab one from a
signed-in mobile client via `getIdToken()` in `src/auth/session.ts`, or Firebase's
REST `signInWithPassword` endpoint) as `Authorization: Bearer <token>`. A bad or
missing token should 401; a valid one should resolve to the matching
`users.firebase_uid` row (404/401 `user_not_registered` if that Firebase identity
hasn't hit `POST /auth/bootstrap` yet — that's expected, not a config bug).

Switch back to `AUTH_MODE=emulated` (or just unset it — that's the default) for
day-to-day local dev; `X-Debug-Firebase-Uid` is a lot faster to work with than
minting real tokens.

---

## Recap — what changes where

| What | Where | Before this doc | After this doc |
|---|---|---|---|
| Mobile auth config | `app-mobile/src/auth/config.ts` | `REPLACE_ME_*` placeholders | real web app config (§4) |
| Mobile auth behavior | `app-mobile/app/(auth)/sign-in.tsx` | "Demo mode" caption, mock flow | real `signInWithEmail`/`signUpWithEmail` |
| Backend auth mode | env var `AUTH_MODE` | `emulated` (default) | `firebase` |
| Backend Firebase project | env var `FIREBASE_PROJECT_ID` | unset | your project ID |
| Backend credentials | `GOOGLE_APPLICATION_CREDENTIALS` / Secret Manager | none | service account key (§6) |
| Apple Sign-In | Apple Developer + Firebase console | not configured | Services ID + key wired (§5) |

Google/Apple **native** sign-in buttons on mobile stay a documented TODO past this
doc — they need `expo-auth-session` / `expo-apple-authentication` native config in
an EAS dev build (SPEC milestone 1 depends on the dev build skeleton, which this
doc doesn't cover). Email/password is fully real once §4 and §6 are done.
