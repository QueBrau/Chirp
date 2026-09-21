# Invite links on chirpsocials.com (c5)

Jose selected the existing landing-page domain on September 21, 2026. Invite
sharing and QR codes now default to
`https://chirpsocials.com/join-chapter?code=...`. The app retains its `chirp://`
scheme, the browser hand-off and invite-code forwarding through authentication.
This changes no API origin, Stripe callback origin, DNS or site ownership.

## Association contract

- iOS: `applinks:chirpsocials.com`; AASA app identifier
  `7G7PL87H62.app.chirps.mobile`, using c39's recorded signing team and the
  existing bundle identifier. Only `/join-chapter` is associated. Legal pages,
  Stripe callbacks and arbitrary landing-page paths stay on the website.
- Android: the HTTPS intent filter uses the same host and exact invite path.
  The actual release signing certificate's SHA-256 fingerprint is not in the
  reviewed repository evidence. `assetlinks.json` deliberately remains `[]`,
  matching the previously hosted empty association. This is not verified Android
  App Links support. Obtain the fingerprint from the actual signed release and
  add the package/certificate association in a reviewed follow-up before claiming it.
- `www.chirpsocials.com` is not configured. No alias or redirect to it is needed.
- `EXPO_PUBLIC_WEB_URL` remains available for local testing. An EAS override is
  part of the build inputs; check it before release so the compiled shared URL
  cannot silently diverge from the signed associated domain.

The visible files `public/apple-app-site-association` and
`public/assetlinks.json` survive Vite's public-file copy and Firebase's existing
hidden-file exclusion. Exact Hosting rewrites expose them under `/.well-known/`
before the SPA fallback. They are internal rewrites, not HTTP redirects.
`appAssociation: NONE` prevents Firebase from generating an empty Apple document
in place of the reviewed one. Both Apple request paths have explicit JSON headers;
the `.well-known` paths use a five-minute browser cache policy.

References: [Expo iOS Universal Links](https://docs.expo.dev/linking/ios-universal-links/),
[Expo Android App Links](https://docs.expo.dev/linking/android-app-links/),
[Firebase Hosting configuration](https://firebase.google.com/docs/hosting/full-config).

## Build and publication checks

Use a clean checkout of the reviewed commit, reread the current board and run:

```sh
cd app-mobile
npm run verify:invites
npx tsc --version
npx tsc --noEmit
cd ../web
npm ci
npm run build
npm run verify:associations
```

The mobile check executes the real invite helper, including code escaping and
the local-host override. The web check reads the actual `dist` JSON and verifies
the association paths, identities, content-type headers and rewrite order. It
does not contact Firebase, inspect a signed app or establish device behavior.

Publish Hosting from that reviewed checkout using the existing project procedure
in [README.md](README.md). Confirm the live response at BOTH:

- `https://chirpsocials.com/.well-known/apple-app-site-association`
- `https://chirpsocials.com/apple-app-site-association`

Require HTTPS certificate verification, direct HTTP 200 without a redirect,
`application/json`, and parsed JSON matching the reviewed file. Status 200 alone
is insufficient: before this change Firebase returned a valid but empty
`applinks.details` array. A SPA document is also not an association.
Check the two existing Stripe callback pages on their configured origin and the
invite browser fallback after publication. Keep the previous Hosting version
available for rollback.

## Native and device acceptance remain

A new signed build is required; the existing preview `90e93270` cannot acquire
new native entitlements from a website release. Verify its actual application
identifier and associated-domains entitlement against the hosted AASA, together
with the effective compiled invite host. The recorded team identifier is an
input to this change, not a fresh signing attestation. Do not create a new Expo
project or replace the working application identity to bypass access problems.

On the new build, test a link tapped from another app with Chirp closed and open.
Confirm the intended invite reaches the join screen, survives sign-in/sign-up,
and still respects the existing join authorization. Also test a phone without
Chirp installed: the browser page must remain useful and show the same invite
code. Opening a same-domain link inside Safari is not by itself a native-launch
test. Record build, OS, entry point, observed result and timestamp on c5.

The custom host, source checks or successful Hosting publication alone do not
close c5. Android additionally needs its real signing association and device
verification. c73's marketing-subdomain relocation remains a separate decision.
