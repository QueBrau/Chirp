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
export function JoinChapter() {
  usePageMeta("Join your org on Chirp");

  const [params] = useSearchParams();
  const code = params.get("code");

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
        <p className="lede" style={{ margin: "var(--space-4) auto 0", fontSize: 15 }}>
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

        <p className="caption" style={{ marginTop: "var(--space-5)" }}>
          Don't have Chirp yet? <Link to="/">Find out what it is</Link>, then come back to this link.
        </p>
      </div>
    </div>
  );
}
