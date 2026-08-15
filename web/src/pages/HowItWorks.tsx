import { Link } from "react-router-dom";

import { usePageMeta } from "../components/usePageMeta";

export function HowItWorks() {
  usePageMeta(
    "How Chirp works — Chirp",
    "The four steps from signing up with your school email to running your org's dues, events, and tree from your phone.",
  );

  return (
    <>
      <section className="page-head">
        <div className="wrap">
          <p className="eyebrow">How it works</p>
          <h1 className="display">Four steps, and you&rsquo;re running your org.</h1>
          <div className="accent-bar" aria-hidden="true"></div>
          <p className="lede">
            Chirp works the same way whether you&rsquo;re brand new to campus or you&rsquo;re
            the treasurer trying to get dues out of forty pledges.
          </p>
        </div>
      </section>

      <section className="section section--tight">
        <div className="wrap">
          <div className="steps">
            <div className="step">
              <div className="step__num" aria-hidden="true"></div>
              <div>
                <h2 className="headline">Sign up with your school email</h2>
                <p className="caption" style={{ marginTop: "var(--space-2)", lineHeight: "1.6", maxWidth: "var(--measure)" }}>
                  Create an account with your school email and pick your campus.
                  That&rsquo;s what puts you on the right Home feed and the right Yak
                  board.
                </p>
              </div>
            </div>

            <div className="step">
              <div className="step__num" aria-hidden="true"></div>
              <div>
                <h2 className="headline">Land on your campus feed &mdash; no org required</h2>
                <p className="caption" style={{ marginTop: "var(--space-2)", lineHeight: "1.6", maxWidth: "var(--measure)" }}>
                  The moment you&rsquo;re signed in you&rsquo;re on Home, seeing For You and
                  Campus posts from your school. You do not need to belong to a
                  fraternity, sorority, club, or team to use Chirp.
                </p>
              </div>
            </div>

            <div className="step">
              <div className="step__num" aria-hidden="true"></div>
              <div>
                <h2 className="headline">Join your org with an invite code</h2>
                <p className="caption" style={{ marginTop: "var(--space-2)", lineHeight: "1.6", maxWidth: "var(--measure)" }}>
                  Get a code from an officer and enter it under Orgs. That code is
                  what puts you inside your chapter&rsquo;s private feed, its events, and
                  its family tree.
                </p>
              </div>
            </div>

            <div className="step">
              <div className="step__num" aria-hidden="true"></div>
              <div>
                <h2 className="headline">Run the org from your phone</h2>
                <p className="caption" style={{ marginTop: "var(--space-2)", lineHeight: "1.6", maxWidth: "var(--measure)" }}>
                  Dues, events, meetings, the tree &mdash; officers manage all of it from
                  the Orgs tab, role by role, without a separate spreadsheet or a
                  separate app.
                </p>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* Closing zone for officers, on the specific mechanic of how roles get set. */}
      <section className="section">
        <div className="wrap">
          <div className="grid grid--2" style={{ gap: "var(--space-10)", alignItems: "center" }}>
            <div>
              <p className="eyebrow">For officers</p>
              <h2 className="title" style={{ fontSize: "clamp(22px, 2.6vw, 30px)" }}>Invite codes are how roles get set.</h2>
              <div className="accent-bar accent-bar--warm" aria-hidden="true"></div>
              <p className="lede" style={{ marginTop: "var(--space-5)", fontSize: "16px" }}>
                An officer creates the invite code, and the role a new member
                gets is set at the moment that code is minted. There&rsquo;s no separate
                step to promote someone afterward &mdash; the code already carries the
                role it was built for.
              </p>
            </div>

            <div className="card">
              <p className="caption" style={{ marginBottom: "var(--space-3)" }}>Roles a code can carry</p>
              <div style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap" }}>
                <span className="chip chip--accent">President</span>
                <span className="chip">Treasurer</span>
                <span className="chip">Secretary</span>
                <span className="chip">Historian</span>
                <span className="chip">Member</span>
                <span className="chip">Pledge</span>
              </div>
            </div>
          </div>
        </div>
      </section>

      <section className="section section--tight">
        <div className="wrap" style={{ textAlign: "center" }}>
          <h2 className="title" style={{ fontSize: "clamp(22px, 2.8vw, 32px)" }}>Ready to bring this to your org?</h2>
          <p className="lede" style={{ margin: "var(--space-4) auto 0" }}>
            If you run a chapter or a student org and want in early, tell us.
          </p>
          <p style={{ marginTop: "var(--space-6)" }}>
            <Link className="btn btn--primary" to="/contact">Get in touch</Link>
          </p>
        </div>
      </section>
    </>
  );
}
