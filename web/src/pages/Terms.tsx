import { Link } from "react-router-dom";

import { usePageMeta } from "../components/usePageMeta";

/*
  JOSE — THIS DOCUMENT IS NOT READY TO PUBLISH AS-IS.

  Sections 1-7 describe how the product actually works and are safe: each is a
  factual statement traceable to the backend. Sections 8, 9 and 10 are the
  legally-loaded ones (liability, disputes, governing law) and are deliberately
  left as marked placeholders rather than drafted here. An arbitration or
  limitation-of-liability clause written by an agent is worse than none: it reads
  as enforceable and is not tailored to your entity, your state, or your risk.

  Get a lawyer to fill 8-10 and to review 1-7. Also needed before launch:
   - the legal entity name, if Chirp is incorporated
   - the minimum age (there is no age gate in the code today)
   - replacing every CONTACT_EMAIL_PLACEHOLDER
*/
export function Terms() {
  usePageMeta(
    "Terms of service — Chirp",
    "The rules for using Chirp, including how dues payments work and who is responsible for them.",
  );

  return (
    <>
      <section className="wrap page-head">
        <p className="eyebrow">Legal</p>
        <h1 className="display">Terms of service</h1>
        <div className="accent-bar" aria-hidden="true"></div>
        <p className="lede">
          The rules for using Chirp, and who is responsible for what &mdash; particularly
          where money is involved.
        </p>
        <div className="legal-meta">
          <span className="chip">Last updated August 2026</span>
          <span className="chip chip--accent">Applies to the Chirp app</span>
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
          {/* NEEDS LEGAL: minimum age. There is no age gate in the code today, so
              this sentence must be written to match whatever you decide to enforce,
              and the app has to enforce it. */}
          <p>
            You must meet the minimum age stated in this section to hold an account.
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
            to store your content and show it to the people you posted it to &mdash; your
            org, your campus, or the person you messaged &mdash; and nothing broader.
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
            chapter &mdash; not Chirp &mdash; is responsible for what the dues cover, for refunds,
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
            Records that belong to an org rather than to you alone &mdash; ledger entries,
            meeting attendance &mdash; remain with the org after you leave, because they are
            the org&rsquo;s books.
          </p>

          {/* NEEDS LEGAL — DO NOT SHIP WITHOUT A LAWYER FILLING THIS IN.
              Deliberately not drafted here. */}
          <h2>8. Disclaimers and limitation of liability</h2>
          <p>
            This section has not been finalised and requires legal review before
            launch.
          </p>

          {/* NEEDS LEGAL — arbitration, class-action waiver, dispute process.
              Deliberately not drafted here. */}
          <h2>9. Disputes</h2>
          <p>
            This section has not been finalised and requires legal review before
            launch.
          </p>

          {/* NEEDS LEGAL — governing law and jurisdiction; depends on the entity. */}
          <h2>10. Governing law</h2>
          <p>
            This section has not been finalised and requires legal review before
            launch.
          </p>

          <h2>11. Changes to these terms</h2>
          <p>
            If we change these terms we will update the date at the top of this page,
            and we will tell you in the app if the change is material.
          </p>

          <h2>12. Contact</h2>
          <p>Questions about these terms: CONTACT_EMAIL_PLACEHOLDER.</p>

        </div>
      </section>
    </>
  );
}
