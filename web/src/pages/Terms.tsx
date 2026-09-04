import { Link } from "react-router-dom";

import { usePageMeta } from "../components/usePageMeta";
import {
  CONTACT_EMAIL,
  GOVERNING_COUNTY,
  GOVERNING_STATE,
  LAUNCH_CAMPUS,
  LEGAL_LAST_UPDATED,
  MIN_AGE,
} from "../siteConfig";

/*
  JOSE — status of this document, Aug 2026.

  Sections 1-7 describe how the product actually works; each is traceable to the
  backend. Sections 8-10 are now written too, in plain English and scoped to
  North Carolina, on your call to launch at UNCG without a lawyer first.

  What that means honestly: this is a clear, good-faith agreement, NOT a
  lawyer-reviewed one. Deliberate choices worth knowing:

  - NO arbitration clause and NO class-action waiver. Those are the clauses that
    most need real drafting, are most often thrown out when drafted badly, and
    buy a single-campus app almost nothing. Silence is safer than a bad one.
  - Liability is capped at what the user actually paid Chirp, which for almost
    everyone is nothing. Simple and defensible.
  - Governing law is North Carolina, venue Guilford County, which is where UNCG
    is. That matches the launch and avoids claiming a jurisdiction unrelated to
    the only users we have.
  - No legal entity is named, because Chirp is not incorporated yet. When it is,
    that name goes in sections 8-10 and 12.

  Get these reviewed before the first campus outside NC onboards. That is the
  point where fifty-state hedging starts to matter and this document stops being
  adequate.
*/
export function Terms() {
  usePageMeta(
    "Terms of service · Chirp",
    "The rules for using Chirp, including how dues payments work and who is responsible for them.",
  );

  return (
    <>
      <section className="wrap page-head">
        <p className="eyebrow">Legal</p>
        <h1 className="display">Terms of service</h1>
        <div className="accent-bar" aria-hidden="true"></div>
        <p className="lede">
          The rules for using Chirp, and who is responsible for what, particularly
          where money is involved.
        </p>
        <div className="legal-meta">
          <span className="chip">Last updated {LEGAL_LAST_UPDATED}</span>
          <span className="chip chip--accent">{LAUNCH_CAMPUS}</span>
          <span className="chip">{GOVERNING_STATE}</span>
        </div>
      </section>

      <section className="wrap section--tight">
        <div className="prose">

          <div className="note">
            <p><strong>The part most people should read.</strong> When your chapter
            collects dues through Chirp, the money goes into your chapter&rsquo;s own
            account. Chirp processes the payment and takes a small fee; we do not hold
            your chapter&rsquo;s funds. Refunds, disputes and how much dues cost are between
            you and your chapter.</p>
          </div>

          <h2>1. Who can use Chirp</h2>
          <p>
            Chirp is built for students and alumni of the campuses we support. You
            choose your campus and account type when you sign up, and you are
            responsible for those being accurate. We do not currently verify school
            affiliation.
          </p>
          <p>
            <strong>You must be at least {MIN_AGE} years old to hold a Chirp account.</strong> There
            is no age check at sign-up today, so this is a rule you agree to rather than one the app
            enforces for you. If we learn an account belongs to someone younger, we will remove it.
          </p>

          <h2>2. Your account</h2>
          <p>
            You are responsible for what happens under your account and for keeping
            access to it secure. Tell us if you believe someone else is using it.
          </p>

          <h2>3. Orgs, roles and invites</h2>
          <p>
            Chapters and student orgs on Chirp are run by their own officers, not by
            Chirp. An officer creates an invite code, and the role that code grants is
            fixed when the code is created. Officers can see and manage org data
            appropriate to their role, including the roster, the ledger and meeting
            records.
          </p>
          <p>
            Chirp does not adjudicate who should be a member of your org, who should
            hold which office, or internal org disputes.
          </p>

          <h2>4. What you post</h2>
          <p>
            You keep ownership of what you post. You grant Chirp the permission needed
            to store your content and show it to the people you posted it to (your
            org, your campus, or the person you messaged) and nothing broader.
          </p>
          <p>
            Org content stays inside the org. A post made to an org is not shown on
            the campus feed.
          </p>

          <h2>5. Acceptable use</h2>
          <p>You may not use Chirp to:</p>
          <ul>
            <li>harass, threaten, or bully anyone, including anonymously;</li>
            <li>post content that is illegal, or that sexualises minors;</li>
            <li>impersonate another person or misrepresent your affiliation with an org;</li>
            <li>share someone&rsquo;s private information without their consent;</li>
            <li>attempt to identify the author of an anonymous post;</li>
            <li>break into, scrape, overload, or probe our systems or anyone&rsquo;s account.</li>
          </ul>
          <p>
            Posting anonymously does not make you unaccountable. As our{" "}
            <Link to="/privacy">privacy policy</Link> explains plainly, we record who
            wrote each anonymous post so we can act on abuse and answer lawful
            requests.
          </p>
          <p>
            We can remove content and suspend accounts that break these rules.
          </p>

          <h2>6. Dues and payments</h2>
          <p>
            Dues are set by your chapter, not by Chirp. Payments are processed by
            Stripe into an account your chapter controls.
          </p>
          <p>
            <strong>Your chapter is the merchant of record.</strong> That means your
            chapter, not Chirp, is responsible for what the dues cover, for refunds,
            and for resolving disputes and chargebacks. Chirp charges a platform fee
            on each payment and does not take custody of your chapter&rsquo;s funds.
          </p>
          <p>
            If you dispute a dues charge, raise it with your chapter first. We can help
            with a technical failure of the payment itself; we cannot decide whether
            you owed the dues.
          </p>
          <p>
            A chapter&rsquo;s ledger is append-only. Officers can record a correction, but
            entries cannot be edited or deleted after the fact.
          </p>

          <h2>7. Ending your use of Chirp</h2>
          <p>
            You can delete your account at any time. We can suspend or close an account
            that breaks these terms, or where we are required to.
          </p>
          <p>
            Records that belong to an org rather than to you alone (ledger entries,
            meeting attendance) remain with the org after you leave, because they are
            the org&rsquo;s books.
          </p>

          <h2>8. Disclaimers and limitation of liability</h2>
          <p>
            Chirp is provided as it is. We work to keep it running and accurate, but we do not
            promise it will always be available, error-free, or that it will never lose data. Use it
            accordingly: do not make Chirp the only copy of something that matters.
          </p>
          <p>
            Chirp is a tool your org uses to run itself. We are not responsible for what your org
            decides, what its officers write in minutes, what dues it sets, how it treats its
            members, or what any user posts. Those are between you and your org.
          </p>
          <p>
            <strong>To the extent the law allows, our total liability to you for any claim relating
            to Chirp is limited to the amount you have actually paid Chirp in platform fees in the
            twelve months before the claim.</strong> For nearly every user that is zero, because
            the platform fee is charged to your chapter, not to you.
          </p>
          <p>
            Nothing here limits liability that cannot legally be limited, including liability for
            fraud or for death or personal injury caused by negligence.
          </p>

          <h2>9. Disputes</h2>
          <p>
            If you have a problem with Chirp, email us first at{" "}
            <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a>. Most things are a
            misunderstanding or a bug, and we would rather fix it than argue about it. We will
            respond within 30 days.
          </p>
          <p>
            <strong>We are not asking you to give up your right to go to court, and there is no
            arbitration requirement and no class-action waiver in these terms.</strong> If we cannot
            resolve something between us, either of us can bring a claim in the courts described in
            section 10. Small claims court remains available to you for claims that qualify.
          </p>
          <p>
            Disputes about dues themselves are between you and your chapter, which is the merchant
            for those payments (see section 6).
          </p>

          <h2>10. Governing law</h2>
          <p>
            These terms are governed by the laws of the State of {GOVERNING_STATE}, without regard
            to its conflict-of-laws rules. Any claim relating to Chirp that is not resolved between
            us may be brought in the state or federal courts located in {GOVERNING_COUNTY}, and we
            each agree those courts may hear it.
          </p>
          <p>
            Chirp is operated from {GOVERNING_STATE} and is currently offered only at{" "}
            {LAUNCH_CAMPUS}. If a court decides part of these terms cannot be enforced, the rest
            still applies.
          </p>

          <h2>11. Changes to these terms</h2>
          <p>
            If we change these terms we will update the date at the top of this page,
            and we will tell you in the app if the change is material.
          </p>

          <h2>12. Contact</h2>
          <p>
            Questions about these terms:{" "}
            <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a>. Chirp is operated from{" "}
            {GOVERNING_STATE}.
          </p>

        </div>
      </section>
    </>
  );
}
