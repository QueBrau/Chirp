import { useEffect, useMemo } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { usePageMeta } from "../components/usePageMeta";

/**
 * Invite hand-off.
 *
 * An https link survives being pasted into a text message; a raw chirp:// link
 * arrives as dead text, which is exactly where invites get sent. This page
 * exists to bridge that: it reads ?code= and hands off to
 * chirp://join-chapter?code=..., the deep link
 * app-mobile/app/(auth)/join-chapter.tsx already handles.
 *
 * Path must stay /join-chapter. app-mobile/app.json declares it in both the iOS
 * associatedDomains entry and the Android intent filter, so once universal
 * links are wired this same URL opens the app directly and this page never
 * renders at all.
 */
/**
 * Invite codes are minted server-side by `secrets.token_urlsafe(9)`
 * (backend/app/routers/chapters.py), which yields 12 URL-safe base64 characters.
 * Anything else came from a hand-crafted URL, not from Chirp.
 *
 * This is not an XSS guard — React escapes the value either way. It stops the
 * page being a text-reflection surface: without it,
 * /join-chapter?code=SUSPENDED-CALL-1-800-555-0100 renders attacker-chosen text
 * large and bold under Chirp's brand, on the one URL this whole feature exists
 * to get people tapping from a text message. An unrecognisable code falls back
 * to the generic invite copy rather than echoing it.
 */
const CODE_SHAPE = /^[A-Za-z0-9_-]{6,24}$/;

function validCode(raw: string | null): string | null {
  return raw !== null && CODE_SHAPE.test(raw) ? raw : null;
}

export function JoinChapter() {
  usePageMeta("Join your org on Chirp");

  const [params] = useSearchParams();
  const code = validCode(params.get("code"));

  // ?code= comes from a URL anyone can craft, so it is untrusted. React escapes
  // it on render by default, and encodeURIComponent matches what
  // app-mobile/src/auth/inviteLink.ts does, so the two cannot drift on encoding.
  const target = useMemo(
    () => (code ? `chirp://join-chapter?code=${encodeURIComponent(code)}` : "chirp://join-chapter"),
    [code],
  );

  // Best-effort auto-open. iOS Safari routinely blocks a custom-scheme
  // navigation that was not triggered by a direct user gesture, so the button
  // below is the actual mechanism and nothing depends on this firing.
  useEffect(() => {
    const timer = window.setTimeout(() => {
      window.location.href = target;
    }, 350);
    return () => window.clearTimeout(timer);
  }, [target]);

  return (
    <div className="bounce">
      <div className="bounce__card">
        <div className="bounce__mark" aria-hidden="true">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" />
            <circle cx="9" cy="7" r="4" />
            <path d="M19 8v6" />
            <path d="M22 11h-6" />
          </svg>
        </div>

        <h1 className="title">You've been invited to an org on Chirp</h1>
        <p className="lede" style={{ margin: "var(--space-4) auto 0", fontSize: "var(--reading)" }}>
          Open Chirp to join. Your role is already set by whoever invited you.
        </p>

        {code !== null && (
          <div className="code-well">
            <p className="caption">Invite code &middot; tap and hold to copy</p>
            <p className="code-well__value">{code}</p>
          </div>
        )}

        <a className="btn btn--primary" href={target}>
          Open Chirp
        </a>

        {/* c305: no store listing, no TestFlight exists yet (c39 artifact
            uninstallable, c10 backlog) - the ONE true way to get Chirp today is
            the person who sent the invite, so that is what the copy says. When
            c10 ships a real TestFlight link, this paragraph is where it goes. */}
        <p className="copy" style={{ marginTop: "var(--space-5)" }}>
          Don't have Chirp yet? It's in early testing with invited chapters, so ask the
          person who sent you this link to get you set up. Curious meanwhile?{" "}
          <Link to="/">See what Chirp is</Link>.
        </p>
      </div>
    </div>
  );
}
