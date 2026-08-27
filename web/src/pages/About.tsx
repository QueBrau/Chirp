import { usePageMeta } from "../components/usePageMeta";
import { CtaSection } from "../components/CtaSection";

export function About() {
  usePageMeta(
    "About Chirp — Chirp",
    "What Chirp is, who it's for, and the product decisions behind it — campus-first, org content stays private, and your chapter owns its own books.",
  );

  return (
    <>
      <section className="page-head">
        <div className="wrap">
          <p className="eyebrow">About</p>
          <h1 className="display">Campus first. Your chapter second.</h1>
          <div className="accent-bar" aria-hidden="true"></div>
          <p className="lede">
            Chirp is a campus app that fraternities, sororities, clubs, and
            intramural teams use to run themselves &mdash; not a Greek app that
            happens to let other students in.
          </p>
        </div>
      </section>

      <section className="section section--tight">
        <div className="wrap">
          <div className="prose">
            <h2>Who it&rsquo;s for</h2>
            <p>
              Chirp is for every student at a campus, not just students in Greek
              life. Sign up and you land on your school&rsquo;s feed the same day,
              whether or not you ever join an org.
            </p>

            <h2>Campus-first, not Greek-first</h2>
            <p>
              Every student gets the same Home feed and the same campus-wide Chirp
              board. Fraternities, sororities, clubs, and intramural teams are
              orgs you can join on top of that, by invite code &mdash; never a
              requirement to use the app.
            </p>

            <h2>Org content stays the org&rsquo;s</h2>
            <p>
              Nothing posted inside an org&rsquo;s private feed reaches the campus feed.
              The two are separate by design, not by a setting someone could flip.
              An org&rsquo;s events, its family tree, and its member roster are visible
              only to people inside that org.
            </p>

            <h2>The chapter owns its own money and its own books</h2>
            <p>
              Dues are processed into an account your chapter owns, not an account
              Chirp controls. The ledger behind it is append-only, so when one
              treasurer hands the books to the next, the full history goes with
              them.
            </p>

            <h2>Who&rsquo;s behind it</h2>
            {/* JOSE: replace this paragraph with real company/team details before launch */}
            <p>
              Team and company details aren&rsquo;t public yet. This paragraph is a
              placeholder for that information before Chirp launches.
            </p>
          </div>
        </div>
      </section>

      <CtaSection
        heading="Bringing Chirp to your campus?"
        body="If you run a chapter or a student org and want in early, tell us."
        buttonLabel="Get in touch"
        buttonTo="/contact"
      />
    </>
  );
}
