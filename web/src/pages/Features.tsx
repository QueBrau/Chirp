import { Link } from "react-router-dom";

import { usePageMeta } from "../components/usePageMeta";

export function Features() {
  usePageMeta(
    "Features — Chirp",
    "A tour of what's actually in Chirp: the campus Home feed, private Org tools, the Yak board, private messages, treasurer and secretary tools, and the alumni network.",
  );

  return (
    <>
      <section className="page-head">
        <div className="wrap">
          <p className="eyebrow">What&rsquo;s inside</p>
          <h1 className="display">The feed, the org, and the books &mdash; all in one app.</h1>
          <div className="accent-bar" aria-hidden="true"></div>
          <p className="lede">
            Chirp is a campus app first: every student lands on a feed the day they
            sign up. Join an org with an invite code and you get a private space for
            that org&rsquo;s feed, events, family tree, and officer tools, on top of
            everything else.
          </p>
        </div>
      </section>

      {/* Home feed. Text-left, card-right. */}
      <section className="section">
        <div className="wrap">
          <div className="grid grid--2" style={{ gap: "var(--space-48)", alignItems: "center" }}>
            <div>
              <p className="eyebrow">Home feed</p>
              <h2 className="title" style={{ fontSize: "clamp(22px, 2.6vw, 30px)" }}>For You and Campus, side by side.</h2>
              <div className="accent-bar" aria-hidden="true"></div>
              <p className="lede" style={{ marginTop: "var(--space-5)", fontSize: "16px" }}>
                The Home tab opens on a mixed feed of text, photo, and video posts.
                Switch between For You, a feed shaped by what you engage with, and
                Campus, a reverse-chronological view of everything posted at your
                school. Posting here never requires being in an org.
              </p>
            </div>

            <div className="card">
              <div style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap" }}>
                <span className="chip chip--accent">For You</span>
                <span className="chip">Campus</span>
              </div>
              <p className="copy" style={{ marginTop: "var(--space-5)", lineHeight: "1.7" }}>
                Posts can be text, a photo, or a video. Comments and likes work the
                way you&rsquo;d expect &mdash; no separate app for the social side of campus.
              </p>
            </div>
          </div>
        </div>
      </section>

      {/* Orgs. Intro row, then a 3-up grid of the tools underneath it. */}
      <section className="section section--tight">
        <div className="wrap">
          <p className="eyebrow">Orgs</p>
          <h2 className="display" style={{ fontSize: "clamp(26px, 3.4vw, 38px)" }}>A private world for your chapter.</h2>
          <div className="accent-bar" aria-hidden="true"></div>
          <p className="lede" style={{ marginTop: "var(--space-5)" }}>
            Every fraternity, sorority, club, and intramural team is an org you join
            with an invite code from an officer. Nothing posted inside an org ever
            appears on the campus feed.
          </p>

          <div className="grid grid--3" style={{ marginTop: "var(--space-48)" }}>
            <article className="card">
              <div className="card__icon" aria-hidden="true">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <rect x="3" y="4" width="18" height="18" rx="2" ry="2" /><line x1="16" y1="2" x2="16" y2="6" /><line x1="8" y1="2" x2="8" y2="6" /><line x1="3" y1="10" x2="21" y2="10" />
                </svg>
              </div>
              <h3 className="title">Events with real RSVPs</h3>
              <p className="copy" style={{ marginTop: "var(--space-2)", lineHeight: "1.6" }}>
                Officers post events with a cover, date, and location. Members RSVP
                Going, Maybe, or Can&rsquo;t, and the guest list updates as they do.
              </p>
            </article>

            <article className="card">
              <div className="card__icon" aria-hidden="true">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <line x1="6" y1="3" x2="6" y2="15" /><circle cx="18" cy="6" r="3" /><circle cx="6" cy="18" r="3" /><path d="M18 9a9 9 0 0 1-9 9" />
                </svg>
              </div>
              <h3 className="title">The family tree</h3>
              <p className="copy" style={{ marginTop: "var(--space-2)", lineHeight: "1.6" }}>
                Big/little lineage lives here as an interactive tree, with families
                in their own colors and a little confirming their big before the
                pairing is official.
              </p>
            </article>

            <article className="card">
              <div className="card__icon" aria-hidden="true">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" /><circle cx="9" cy="7" r="4" /><path d="M23 21v-2a4 4 0 0 0-3-3.87" /><path d="M16 3.13a4 4 0 0 1 0 7.75" />
                </svg>
              </div>
              <h3 className="title">The member roster</h3>
              <p className="copy" style={{ marginTop: "var(--space-2)", lineHeight: "1.6" }}>
                Every active member, their role, and their pledge class in one
                list &mdash; the roster an officer actually needs, not a spreadsheet.
              </p>
            </article>
          </div>
        </div>
      </section>

      {/* Yak. Card-left, text-right — the reverse of the Home feed row above, for rhythm. */}
      <section className="section">
        <div className="wrap">
          <div className="grid grid--2" style={{ gap: "var(--space-48)", alignItems: "center" }}>
            <div className="card">
              <span className="chip chip--accent">Anonymous to other students</span>
              <p className="copy" style={{ marginTop: "var(--space-5)", lineHeight: "1.7" }}>
                Posts show no name and no photo. Everyone sees a vote score &mdash;
                upvote or downvote, same as the rest of the board.
              </p>
            </div>

            <div>
              <p className="eyebrow">Yak</p>
              <h2 className="title" style={{ fontSize: "clamp(22px, 2.6vw, 30px)" }}>A campus board, honestly explained.</h2>
              <div className="accent-bar" aria-hidden="true"></div>
              <p className="lede" style={{ marginTop: "var(--space-5)", fontSize: "16px" }}>
                Yak is a board for your whole campus, not your org. What you post
                there shows up without your name attached, to other students.
              </p>
              <div className="note">
                <p>
                  The server does still record who posted each Yak, and that record
                  is never returned by the API to other students. It exists only so
                  reports and abuse can be acted on &mdash; most anonymous apps hide this;
                  we&rsquo;d rather say it plainly.
                </p>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* Messages. Compact, tight section — the smallest feature on the page. */}
      <section className="section section--tight">
        <div className="wrap">
          <div className="grid grid--2" style={{ gap: "var(--space-48)", alignItems: "center" }}>
            <div>
              <p className="eyebrow">Messages</p>
              <h2 className="title" style={{ fontSize: "clamp(22px, 2.6vw, 30px)" }}>Direct messages, kept private.</h2>
              <div className="accent-bar" aria-hidden="true"></div>
              <p className="lede" style={{ marginTop: "var(--space-5)", fontSize: "16px" }}>
                Message another student one-to-one or in a group. Messages are
                private, and stored so that the server is not designed to read
                them &mdash; not a public feed, not a group chat everyone can see.
              </p>
            </div>

            <div className="card">
              <div className="card__icon" aria-hidden="true">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z" />
                </svg>
              </div>
              <p className="copy" style={{ lineHeight: "1.7" }}>
                Threads live under the Messages tab, separate from your org&rsquo;s feed
                and separate from Yak.
              </p>
            </div>
          </div>
        </div>
      </section>

      {/* Officer tools. 3-up grid, same rhythm as the homepage's proof-point section. */}
      <section className="section">
        <div className="wrap">
          <p className="eyebrow">For officers</p>
          <h2 className="display" style={{ fontSize: "clamp(26px, 3.4vw, 38px)" }}>The tools that make an org runnable from a phone.</h2>
          <div className="accent-bar" aria-hidden="true"></div>

          <div className="grid grid--3" style={{ marginTop: "var(--space-48)" }}>
            <article className="card">
              <div className="card__icon card__icon--warm" aria-hidden="true">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <rect x="2" y="5" width="20" height="14" rx="2.5" /><path d="M2 10h20" />
                </svg>
              </div>
              <h3 className="title">Treasurer</h3>
              <p style={{ marginTop: "var(--space-2)" }}><span className="chip chip--success">Append-only</span></p>
              <p className="copy" style={{ marginTop: "var(--space-3)", lineHeight: "1.6" }}>
                Dues cycles, an append-only ledger &mdash; nothing is ever edited or
                deleted, only corrected with a new entry &mdash; dues paid by card or
                bank transfer, and a CSV export for handing the books to next
                year&rsquo;s treasurer.
              </p>
            </article>

            <article className="card">
              <div className="card__icon" aria-hidden="true">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><polyline points="14 2 14 8 20 8" /><line x1="16" y1="13" x2="8" y2="13" /><line x1="16" y1="17" x2="8" y2="17" />
                </svg>
              </div>
              <h3 className="title">Secretary</h3>
              <p style={{ marginTop: "var(--space-2)" }}><span className="chip">Role-gated</span></p>
              <p className="copy" style={{ marginTop: "var(--space-3)", lineHeight: "1.6" }}>
                Meetings with markdown minutes, and attendance tracked per member
                as present, absent, or excused.
              </p>
            </article>

            <article className="card">
              <div className="card__icon" aria-hidden="true">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <rect x="2" y="7" width="20" height="14" rx="2" ry="2" /><path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16" />
                </svg>
              </div>
              <h3 className="title">Alumni &amp; jobs</h3>
              <p style={{ marginTop: "var(--space-2)" }}><span className="chip chip--accent">Optional</span></p>
              <p className="copy" style={{ marginTop: "var(--space-3)", lineHeight: "1.6" }}>
                Alumni keep a profile &mdash; grad year, company, title, industry &mdash; and
                can mark themselves open to mentoring. Job posts go out to a
                chapter or the wider alumni network.
              </p>
            </article>
          </div>
        </div>
      </section>

      <section className="section section--tight">
        <div className="wrap" style={{ textAlign: "center" }}>
          <h2 className="title" style={{ fontSize: "clamp(22px, 2.8vw, 32px)" }}>Curious how this actually works day to day?</h2>
          <p className="lede" style={{ margin: "var(--space-4) auto 0" }}>
            Four steps take you from signing up to running your org.
          </p>
          <p style={{ marginTop: "var(--space-6)" }}>
            <Link className="btn btn--primary" to="/how-it-works">See how Chirp works</Link>
          </p>
        </div>
      </section>
    </>
  );
}
