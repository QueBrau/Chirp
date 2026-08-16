import { Link } from "react-router-dom";

import { usePageMeta } from "../components/usePageMeta";
import { PhoneMock } from "../components/PhoneMock";

export function Home() {
  usePageMeta(
    "Chirp — your campus, your chapter, one app",
    "Chirp is where students post, where student orgs run themselves, and where chapter dues get paid without a spreadsheet.",
  );

  return (
    <>
      {/* Pinned dark in both colour schemes: this is a brand moment, the same
          way DESIGN.md section 7 treats the sign-in screen. */}
      <section className="on-brand hero">
        <div className="wrap hero__grid">
          <div>
            <p className="eyebrow">Built for campus</p>
            <h1 className="display">
              Your campus.
              <br />
              Your chapter.
              <br />
              One app.
            </h1>
            <div className="accent-bar" aria-hidden="true" />
            <p className="lede" style={{ marginTop: "var(--space-6)" }}>
              Chirp is where students post, where student orgs actually run themselves, and where
              chapter dues get paid without a spreadsheet and three reminder texts.
            </p>
            <div className="hero__actions">
              <Link className="btn btn--primary" to="/how-it-works">
                See how it works
              </Link>
              <Link className="btn btn--ghost" to="/features">
                What&apos;s inside
              </Link>
            </div>
          </div>

          <PhoneMock />
        </div>
      </section>

      {/* Three proof points, deliberately not identical in weight — the dues
          card carries the warm accent because it is the claim that matters
          most. Identical spacing everywhere is the slop tell (DESIGN.md 10.3). */}
      <section className="section">
        <div className="wrap">
          <p className="eyebrow">Why Chirp</p>
          <h2 className="display" style={{ fontSize: "clamp(26px, 3.4vw, 38px)" }}>
            Three things campus software keeps getting wrong.
          </h2>
          <div className="accent-bar" aria-hidden="true" />

          <div className="grid grid--3" style={{ marginTop: "var(--space-48)" }}>
            <article className="card">
              <div className="card__icon" aria-hidden="true">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" />
                  <circle cx="9" cy="7" r="4" />
                  <path d="M23 21v-2a4 4 0 0 0-3-3.87" />
                  <path d="M16 3.13a4 4 0 0 1 0 7.75" />
                </svg>
              </div>
              <h3 className="title">For every student, not just Greek life</h3>
              <p className="copy" style={{ marginTop: "var(--space-2)", lineHeight: 1.6 }}>
                Everyone lands on the campus feed. Fraternities, sororities, clubs and intramural
                teams are orgs you join on top of that, by invite code. You are never asked to be
                Greek to use the app.
              </p>
            </article>

            <article className="card">
              <div className="card__icon" aria-hidden="true">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <rect x="3" y="3" width="7" height="7" rx="1.5" />
                  <rect x="14" y="3" width="7" height="7" rx="1.5" />
                  <rect x="3" y="14" width="7" height="7" rx="1.5" />
                  <rect x="14" y="14" width="7" height="7" rx="1.5" />
                </svg>
              </div>
              <h3 className="title">Org tools that replace three spreadsheets</h3>
              <p className="copy" style={{ marginTop: "var(--space-2)", lineHeight: 1.6 }}>
                A private feed only your chapter sees, events with real RSVPs, the family tree, the
                member roster, meeting minutes and attendance. All of it role-gated, so officers get
                officer tools and nobody else does.
              </p>
            </article>

            <article className="card">
              <div className="card__icon card__icon--warm" aria-hidden="true">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <rect x="2" y="5" width="20" height="14" rx="2.5" />
                  <path d="M2 10h20" />
                </svg>
              </div>
              <h3 className="title">Dues that never touch a paper check</h3>
              <p className="copy" style={{ marginTop: "var(--space-2)", lineHeight: 1.6 }}>
                Members pay by card or bank transfer. The money lands in your chapter&apos;s own
                account, not ours, and the ledger is append-only so the books cannot be quietly
                rewritten.
              </p>
            </article>
          </div>
        </div>
      </section>

      {/* The honest bit. Worth its own zone rather than a footnote. */}
      <section className="section section--tight">
        <div className="wrap">
          <div className="grid grid--2" style={{ gap: "var(--space-48)", alignItems: "center" }}>
            <div>
              <p className="eyebrow">On the money</p>
              <h2 className="title" style={{ fontSize: "clamp(22px, 2.6vw, 30px)" }}>
                Your chapter is the merchant, not Chirp.
              </h2>
              <div className="accent-bar accent-bar--warm" aria-hidden="true" />
              <p className="lede" style={{ marginTop: "var(--space-5)", fontSize: "var(--reading)" }}>
                Dues are processed by Stripe into an account your chapter owns. Chirp never takes
                custody of your money and never stores a card or bank number. We charge a small
                platform fee on each payment; everything else is between your chapter and its
                members.
              </p>
              <p style={{ marginTop: "var(--space-5)" }}>
                <Link to="/privacy">Read what we actually collect</Link>
              </p>
            </div>

            <div className="card">
              <div style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap" }}>
                <span className="chip chip--success">Append-only ledger</span>
                <span className="chip chip--accent">Card or bank</span>
                <span className="chip">Officer-gated</span>
              </div>
              <p className="copy" style={{ marginTop: "var(--space-5)", lineHeight: 1.7 }}>
                Every dues payment writes one line to a ledger that can be added to but never edited
                or deleted. When a treasurer hands the books to next year&apos;s treasurer, the
                history comes with them.
              </p>
            </div>
          </div>
        </div>
      </section>

      <section className="section section--tight">
        <div className="wrap" style={{ textAlign: "center" }}>
          <h2 className="title" style={{ fontSize: "clamp(22px, 2.8vw, 32px)" }}>
            Bringing Chirp to your campus?
          </h2>
          <p className="lede" style={{ margin: "var(--space-4) auto 0" }}>
            Chirp is rolling out campus by campus. If you run a chapter or a student org and want in
            early, tell us.
          </p>
          <p style={{ marginTop: "var(--space-6)" }}>
            <Link className="btn btn--primary" to="/contact">
              Get in touch
            </Link>
          </p>
        </div>
      </section>
    </>
  );
}
