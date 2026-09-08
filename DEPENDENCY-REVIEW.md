# Dependency review — c365

Reviewed 2026-09-07 against the committed locks and source at `fa4bb77`, plus
the bounded upgrade in this change. Advisory results are a dated snapshot.

The web build moves from unsupported Vite 5.4.21 to **Vite 7.3.6**, resolving
**esbuild 0.28.2**, and patches **React Router 6.30.4 to 6.30.6**. This closes
the reported Vite/esbuild findings and one Router advisory. Two moderate Router
advisories remain in the version scan; their prerequisites are absent from the
reviewed site. This is not a zero-advisory result or evidence of exploitation.

## What actually runs

| Surface | Lock or runtime evidence | Scope of this review |
| --- | --- | --- |
| Static website | `web/package-lock.json`, npm lock v3; 118 package entries before, 124 after, excluding the root entry | All locked web packages included in npm audit. Vite/plugin/esbuild run during development/build; React and Router ship in browser JavaScript. |
| Web serving | `web/firebase.json` publishes `dist`, excludes `node_modules`, rewrites SPA paths to `/index.html`, and routes `/media/**` to `chirp-api` | Firebase serves static output; it does not run Vite or esbuild. Configuration inspection, not a production Hosting probe. |
| Local web tools | `web/package.json` uses `vite`, `tsc -b && vite build`, and `vite preview`; README documents those commands | No configured network host, custom CORS, proxy, SSR, or standalone esbuild `serve`/`servedir` use found. A developer can still override CLI options; this is not a census of every machine. |
| Backend production | `backend/requirements.lock`: 58 exact PyPI pins, all with SHA-256 hashes | All 58 included in the 69-package OSV query below. Production source locks are unchanged. |
| Backend tests | `backend/requirements-dev.lock`: 69 exact hashed pins | Includes the same 58 production versions plus 11 test packages; equality verified. All 69 queried. |
| Backend image | `backend/Dockerfile`: `python:3.12-slim`, `pip --require-hashes`, application install with `--no-deps`, non-root UID 1000, Uvicorn on `$PORT` | Base image tag floats. Python/OS packages, image digest, and isolated package-build tooling are not covered by a PyPI runtime lock scan. No container or deployed-image scan was performed. |
| Development/CI services | `docker-compose.yml` and CI use `postgres:16` / `redis:7`; backend CI uses Python 3.12 | These image tags float. No live Cloud SQL/Redis version or OS advisory conclusion is inferred from them. |
| Mobile | `app-mobile/package-lock.json`: 969 package entries excluding root; Expo 54.0.36, React 19.1.0, React Native 0.81.5, TypeScript 5.9.3 | Lock inventory only; no fresh mobile advisory-clean claim. CI's Node runtime changes below do not upgrade these packages. Expo major upgrade c174 stays parked. |
| Signal spike | `spikes/libsignal-node/package-lock.json`: 3 package entries excluding root; `@signalapp/libsignal-client` 0.100.0 | Separate Node experiment, not part of the web build or backend Dockerfile. No upgrade or scanner claim. |

## Advisory disposition

Rows identify unique advisories, not npm's aggregate vulnerable-package count.
The upstream links are the source for affected versions, fixes, and prerequisites.

| Package and advisory | Prerequisites and current reachability | Disposition |
| --- | --- | --- |
| Vite 5.4.21 — [GHSA-4w7w-66w2-5vf9](https://github.com/vitejs/vite/security/advisories/GHSA-4w7w-66w2-5vf9), optimized-dependency source-map traversal | Reachable Vite dev server plus a predictable sensitive `.map` file. Build/development tooling; Firebase static hosting does not execute this handler. No network exposure configured in the reviewed web scripts/config. | Fixed on the selected line in 7.3.2; installed 7.3.6. Cleared from the post-upgrade scan. |
| Vite's embedded launch-editor — [GHSA-v6wh-96g9-6wx3](https://github.com/vitejs/launch-editor/security/advisories/GHSA-v6wh-96g9-6wx3), Windows NTLM disclosure | Windows with NTLM enabled, running editor middleware, and a malicious website/request reaching its known URL. This is a developer-machine prerequisite, not a static-host vulnerability. The documented developer machines are Macs; other machines were not inspected. | Fixed in Vite 7.3.5, installed 7.3.6. Cleared. |
| Vite 5.4.21 — [GHSA-fx2h-pf6j-xcff](https://github.com/vitejs/vite/security/advisories/GHSA-fx2h-pf6j-xcff), Windows alternate-path `fs.deny` bypass | Exposed Windows Vite dev server and sensitive files addressable through Windows alternate path forms. No such deployed server is established by the repository. | High scanner severity; fixed in 7.3.5, installed 7.3.6. Cleared. |
| esbuild 0.21.5 — [GHSA-67mh-4wv8-2f99](https://github.com/evanw/esbuild/security/advisories/GHSA-67mh-4wv8-2f99), development-server CORS | An attacker-controlled website can read the standalone esbuild server, including localhost. This repo uses esbuild through Vite's build/transform path; no standalone esbuild serving path was found. | Affected through 0.24.2; fixed from 0.25.0. Installed 0.28.2. Cleared without a transitive override. |
| react-router-dom 6.30.4 — [GHSA-jjmj-jmhj-qwj2](https://github.com/remix-run/react-router/security/advisories/GHSA-jjmj-jmhj-qwj2), open redirect/XSS | Browser runtime package. Exploitation requires an untrusted redirect/navigation target; current Router destinations are application literals. | Upstream lists 6.30.6 as the patched v6 version. Installed 6.30.6. Cleared; the separate bypass advisory below remains. |
| react-router 6.30.6 — [GHSA-wrjc-x8rr-h8h6](https://github.com/remix-run/react-router/security/advisories/GHSA-wrjc-x8rr-h8h6), untrusted-path redirect bypass | Browser runtime package remains version-affected (`>=6.0.0 <7.18.0`). Reviewed `Link`/`NavLink` destinations, including shared CTA callers, are literal local paths. No query/user-controlled value reaches a Router navigation target. | Remains moderate in npm audit. Upstream fix is 7.18.0 or later; no v6 fix listed. Reassess before adding dynamic navigation, and plan a separate Router major migration if required. |
| react-router 6.30.6 — [GHSA-337j-9hxr-rhxg](https://github.com/remix-run/react-router/security/advisories/GHSA-337j-9hxr-rhxg), SSR hydration constructor injection | Version-affected (`>=6.4.0 <7.18.0`), but upstream explicitly excludes Declarative Mode. This site uses `createRoot` + `BrowserRouter`; there is no SSR/hydration or server error serialization. | Remains moderate in npm audit. Current execution prerequisites absent; reassess before introducing SSR or changing Router mode. Fix listed from 7.18.0. |

The invitation query is a separate application path: `JoinChapter.tsx` validates
the code shape and encodes it into a fixed `chirp://join-chapter` anchor/automatic
handoff. It does not pass the query value to React Router. This inspection is the
basis for the navigation reachability conclusion, not a blanket assertion that
all untrusted URLs are harmless.

Two additional upstream esbuild reports were checked when selecting 0.28.2:
[Windows standalone `servedir` traversal](https://github.com/evanw/esbuild/security/advisories/GHSA-g7r4-m6w7-qqqr)
and [Deno registry-controlled binary download](https://github.com/evanw/esbuild/security/advisories/GHSA-gv7w-rqvm-qjhr).
Both list fixes from 0.28.1. Neither was an additional finding in the original
web npm audit; the reviewed web tooling uses Node/npm, not Deno or `servedir`.

## Compatibility decision

[Vite's support policy](https://vite.dev/releases), checked on the review date,
lists 7.3 for important/security fixes; 5.4 is no longer supported. Vite 7.3.6
retains the Rollup/esbuild build path already used here. The existing locked
`@vitejs/plugin-react` 4.7.0 declares Vite 7 compatibility; its peer dependency
tree passes `npm ls`. React 18.3.1, TypeScript 5.5.4, plugin-react 4.7.0, and
Rollup 4.62.4 remain at their previous exact locked versions. Lock changes are
limited to Vite/esbuild and their required platform/glob dependencies, and the
Router patch family (`@remix-run/router` 1.23.3 to 1.23.4).

The [Vite 6](https://v6.vite.dev/guide/migration) and
[Vite 7 migration guides](https://v7.vite.dev/guide/migration) were checked against
the actual configuration. The site has no custom SSR/plugin hooks, library mode,
or Sass legacy API. Vite 7 raises its default browser targets, so `build.target`
explicitly preserves the old `es2020`, Edge 88, Firefox 78, Chrome 87, Safari 14
target set. This preserves compilation targets; it is not a new device test.

Vite's package requires Node `^20.19.0 || >=22.12.0`. The web package instead
declares the currently maintained LTS lines `^22.12.0 || ^24.0.0`. The **web and
mobile CI jobs** move from Node 20 to Node 22 without changing mobile dependencies.
Node 20 reached EOL on 2026-04-30
according to the [official schedule](https://github.com/nodejs/Release/blob/main/schedule.json);
the [release table](https://nodejs.org/en/about/previous-releases) lists 22/24 as LTS. The
local verification used Node 22.13.1 and npm 10.9.2; this is compatibility evidence,
not a claim that the workstation has the latest security patch. Use the latest
maintained patch of the selected LTS line. npm engines are advisory unless the
installer enforces them; CI selects Node 22 explicitly. Mobile CI validation is
recorded separately by the integrating maintainer; the web build/browser checks
below do not establish native Expo compatibility.

## Executed verification

- Baseline: TypeScript **5.5.4**, `npm run typecheck`, and `npm run build` passed
  on Vite 5.4.21. JavaScript bundle: 225.16 kB / 68.62 kB gzip.
- Updated lock: clean `npm ci --no-audit --no-fund`, dependency/peer tree check,
  TypeScript **5.5.4**, `npm run typecheck`, and `npm run build` passed on
  Vite **7.3.6**. JavaScript: 224.74 kB / 68.84 kB gzip; CSS: 16.58 kB / 4.09 kB
  gzip. These are build sizes, not latency measurements.
- `npm audit --json --package-lock-only --ignore-scripts`: before, **4 vulnerable
  package entries** (1 high, 3 moderate), representing **7 unique advisories**;
  after, **2 moderate package entries**, representing the **2 Router advisories**
  retained above. Both runs return exit 1 because findings exist. No suppression,
  `audit fix --force`, or claim of a clean overall npm audit.
- Backend: queried the exact name/version of all **69** development-lock PyPI
  packages through the [OSV batch API](https://google.github.io/osv.dev/post-v1-querybatch/).
  All 69 result entries returned no matching advisories. The 58 production pins
  are an exact subset. This was an API package-version query, not `pip-audit`,
  an installed environment scan, or a container/OS scan. No backend lock changed.
- Real browser: Headless Chrome **148.0.7778.96** against the compiled output
  served by local Vite preview passed **13 direct route/query cases**, including
  the invitation, both Stripe return paths, and the unknown-page fallback.
  Fresh JavaScript/CSS asset responses had correct status and content types.
  Client `Link` navigation retained the document, browser Back restored the
  route, and the phone menu closed on navigation. Zero JavaScript exceptions or
  external requests. The invitation's automatic native-app timer was inhibited
  in this isolated check; valid/invalid/missing-code anchor targets were checked.
  No native app was launched. Preview's SPA fallback is not evidence that real
  Firebase rewrites, headers, association files, or native handoff work.

To repeat the web checks, use the declared Node version, run `npm ci`,
`npm ls vite esbuild react-router-dom @vitejs/plugin-react`,
`./node_modules/.bin/tsc --version`, `npm run typecheck`, `npm run build`, and
`npm audit --json --package-lock-only --ignore-scripts` from `web/`. Inspect audit
JSON and exit status separately. Recheck the upstream prerequisites rather than
automatically dismissing the two retained IDs. For backend queries, use each
exact lock pin with OSV ecosystem `PyPI`, check response cardinality/errors, and
follow any `next_page_token` before reporting a complete result.

## Next review and responsibility

Recommended operating rule for board assignment: the **release maintainer who
claims the next dependency review** owns rescanning all shipped locks and the
actual built image, checking upstream support/advisories, and recording the
result before closing that review. Review at each release and monthly; the next
routine target from this snapshot is **2026-10-07**. No recurring automation or
new human commitment was created by this change.

Reassess sooner if a relevant advisory arrives, dev-server exposure changes, or
the website gains dynamic navigation/SSR. Retain the two Router IDs and the
container/OS scan boundary in that handoff. Mobile's existing c174 remains the
separate parked Expo major-upgrade task; this web change does not unpark it.
Deploying the new static artifact and checking real Hosting/deep-link behavior
remain release acceptance under c388, with device behavior under the existing
device acceptance cards. No deployment was performed here.
