import { Link } from "react-router-dom";

import { usePageMeta } from "../components/usePageMeta";
import {
  CONTACT_EMAIL,
  GOVERNING_STATE,
  LEGAL_AUDIENCE,
  LEGAL_LAST_UPDATED,
  LEGAL_OPERATORS,
  MIN_AGE,
  OPERATING_STATE,
} from "../siteConfig";

// Factual and policy refresh, not counsel approval. The existing liability cap,
// no-arbitration/no-class-waiver choices and proposed Florida law require c75 review.
// Publishing these pages does not establish versioned acceptance in the app.
export function Terms() {
  usePageMeta(
    "Terms of service · Chirp",
    "The rules for using Chirp, sharing content, managing student organizations and paying dues.",
  );

  return (
    <>
      <section className="wrap page-head">
        <p className="eyebrow">Legal</p>
        <h1 className="display">Terms of service</h1>
        <div className="accent-bar" aria-hidden="true"></div>
        <p className="lede">The rules for using Chirp and who is responsible for what.</p>
        <div className="legal-meta">
          <span className="chip">Last updated {LEGAL_LAST_UPDATED}</span>
          <span className="chip chip--accent">{LEGAL_AUDIENCE}</span>
          <span className="chip">{MIN_AGE} and older</span>
        </div>
      </section>

      <section className="wrap section--tight">
        <div className="prose">
          <div className="note">
            <p>
              <strong>Before you use Chirp.</strong> Anonymous posts are linked to their authors
              in our records. Messaging does not currently provide end-to-end encryption, and
              sending messages in the mobile app is unavailable. Your chapter sets its dues and
              manages its own organization. The <Link to="/privacy">privacy policy</Link> explains
              what we collect and retain.
            </p>
          </div>

          <h2>1. About Chirp and these terms</h2>
          <p>
            Chirp is an unincorporated service operated from {OPERATING_STATE} by {LEGAL_OPERATORS}.
            In these terms,
            &ldquo;Chirp,&rdquo; &ldquo;we&rdquo; and &ldquo;us&rdquo; refer to those operators.
            You can contact us at{" "}
            <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a>.
          </p>
          <p>
            These terms cover the Chirp app and website. Our{" "}
            <Link to="/community">community guidelines</Link> form part of the rules for using
            Chirp. Our <Link to="/payments">organization and payment terms</Link> also apply when
            you manage an organization or use its financial tools. Our privacy policy describes
            data handling. It does not give us permission to use data for purposes that require
            separate consent.
          </p>
          <p>
            Chirp is independent of the colleges, universities, fraternities, sororities and
            other organizations whose members use it. Their names or colors do not mean they
            operate, sponsor or endorse Chirp.
          </p>

          <h2>2. Who can use Chirp</h2>
          <p>
            Chirp is intended for students and alumni of participating U.S. colleges and
            universities. Availability varies by campus and feature. You choose your account
            type and must describe yourself and your affiliations accurately. Campus features
            require access to a supported school email address. Verifying that mailbox does not
            establish current enrollment or endorsement by the school.
          </p>
          <p>
            <strong>You must be at least {MIN_AGE} years old to hold a Chirp account.</strong> If
            you are under the age of legal adulthood where you live, you must have a parent or
            legal guardian&rsquo;s permission to use Chirp and review these terms with them.
            This does not represent that Chirp has verified your age or that parent or guardian&rsquo;s
            permission. The app does not currently perform an age check at sign-up. If we learn
            that an account belongs to someone under {MIN_AGE}, we will remove it.
          </p>

          <h2>3. Your account</h2>
          <p>
            Keep your sign-in credentials secure and use only an account you are authorized to
            use. You are responsible for your own activity and for activity you authorize through
            your account. Tell us promptly if you believe someone else has accessed it. Do not
            share access to bypass a suspension, campus requirement or organization permission.
          </p>

          <h2>4. Organizations and records about other people</h2>
          <p>
            Organizations are run by their own officers. Authorized roles determine who can
            manage membership, invites, officer history, finances, meetings, events, polls and
            family trees. A role in Chirp does not by itself establish authority to act for an
            organization outside the app.
          </p>
          <p>
            Only enter records about other people when you have the authority and any permission
            needed to do so. This includes roster entries, meeting minutes, attendance, dues
            records, poll records and family-tree placeholders for people without accounts.
            Keep those records accurate and limit them to information needed for the organization&rsquo;s
            activities. Do not upload school records you are not authorized to share.
          </p>
          <p>
            Chirp does not decide who should hold office, belong to an organization or owe dues.
            That does not prevent us from enforcing our rules or responding to privacy, safety
            and legal requests. People named in organization records may contact us even if they
            do not have a Chirp account.
          </p>

          <h2>5. Your content and its audience</h2>
          <p>
            You keep ownership of your content. You give Chirp permission to store, copy and
            display it as needed to provide the features you use, including processing images
            for display and using service providers to host and deliver content. This permission
            is limited to operating those features, handling reports and meeting legal obligations
            described in our privacy policy. It does not grant us permission to sell your content
            or use it in advertising.
          </p>
          <p>
            You must have the rights needed to share the material you submit. Respect copyright,
            privacy and other people&rsquo;s rights. Content permissions remain subject to the
            removal and retention practices in our privacy policy.
          </p>
          <p>
            An organization post is shown in that organization&rsquo;s space rather than the campus
            feed. Other content follows the audience and permissions for its feature. Authorized
            operational and safety access is described in our privacy policy. People who can see
            content may copy or share it, so audience controls cannot promise confidentiality or
            retrieve copies someone has already saved.
          </p>
          <p>
            Anonymous Chirps hide authorship from student-facing board responses. Chirp retains
            the underlying account connection. Do not use anonymous posts or the current messaging
            system as a way to keep information unreadable to Chirp.
          </p>

          <h2>6. Community rules and reporting</h2>
          <p>
            Follow the <Link to="/community">community guidelines</Link>. Harassment, threats,
            discrimination, unlawful content, child sexual abuse or exploitation, nonconsensual
            intimate images, impersonation, fraud and unauthorized disclosure of personal information
            are prohibited. Do not try to expose anonymous authors or access, scrape, overload or
            interfere with systems or accounts without authorization.
          </p>
          <p>
            You can report supported content in the app or email us. Our{" "}
            <Link to="/safety">safety and removal page</Link> explains how to report urgent concerns
            and request removal of intimate images shared without consent. That process has its own
            deadlines. The general 30-day support response below does not apply to those deadlines.
            Chirp is not an emergency service.
          </p>
          <p>
            We may remove content or restrict, suspend or close accounts for violations of these
            rules or as required by law. Contact us if you believe we made a mistake. A report does
            not guarantee a particular outcome, and the app does not continuously monitor all content.
          </p>

          <h2>7. Dues and financial tools</h2>
          <p>
            Your chapter sets dues and is the merchant for dues payments. Stripe processes payments
            on the chapter&rsquo;s connected account. Chirp charges the platform fees described in
            our <Link to="/payments">organization and payment terms</Link>. Chirp does not hold
            dues in a Chirp bank account.
          </p>
          <p>
            Ask your chapter about what dues cover, refunds and financial records. Contact Chirp
            about technical payment problems. Nothing in these terms limits rights you may have
            with your bank, card issuer or payment provider, or under applicable law.
          </p>

          <h2>8. Ending your use and requesting deletion</h2>
          <p>
            You can stop using Chirp and request account deletion at{" "}
            <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a>. Our{" "}
            <Link to="/data-requests">data-request page</Link> explains the current support process.
            The app does not currently offer self-service account deletion. Uninstalling the app
            or signing out does not delete your account or cancel an obligation to your organization.
          </p>
          <p>
            Deletion does not necessarily remove every related record. Financial records, shared
            organization records, safety reports and records needed for legal obligations may be
            retained as explained in our privacy policy. We will explain the scope and status of
            your request. An organization&rsquo;s use of a record does not remove privacy rights
            that apply to that record.
          </p>

          <h2>9. Disclaimers and limitation of liability</h2>
          <p>
            Chirp is provided as it is. We work to keep it running and accurate, but do not promise
            uninterrupted availability, error-free operation or that data will never be lost. Keep
            a separate copy of records that matter to you or your organization.
          </p>
          <p>
            Organizations and users are responsible for their own decisions, content and conduct.
            Chirp does not verify every record, event, job listing or statement submitted by a user.
            This does not exclude responsibilities that applicable law places on Chirp.
          </p>
          <p>
            <strong>To the extent the law allows, our total liability to you for any claim relating
            to Chirp is limited to the amount you have actually paid Chirp in platform fees in the
            twelve months before the claim.</strong> Dues paid to your chapter are not platform
            fees paid to Chirp.
          </p>
          <p>
            Nothing here limits liability that cannot legally be limited, including liability for
            fraud or for death or personal injury caused by negligence, or takes away consumer
            rights or remedies that applicable law does not allow you to waive.
          </p>

          <h2>10. Disputes and governing law</h2>
          <p>
            If you have a problem with Chirp, contact{" "}
            <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a>. We will respond within 30 days.
            This is a response commitment, not a promise that every dispute will be resolved within
            that time. You do not have to wait to protect your legal rights or meet a legal deadline.
          </p>
          <p>
            <strong>There is no arbitration requirement and no class-action waiver in these
            terms.</strong> Small claims court remains available for claims that qualify.
          </p>
          <p>
            The laws of {GOVERNING_STATE} govern these terms, except where applicable law requires
            otherwise. Nothing in these terms removes mandatory consumer protections or other
            rights that cannot be waived. Disputes may be brought before courts having jurisdiction
            under applicable law. If part of these terms cannot be enforced, the remaining
            provisions still apply to the extent permitted by law.
          </p>

          <h2>11. Changes</h2>
          <p>
            We will update the date on these terms when they change and tell you in the app about
            material changes. If a change requires your agreement or consent, we will obtain it
            before applying that change. Updating this page alone does not authorize a new use of
            previously collected information that conflicts with our earlier privacy commitments.
          </p>

          <h2>12. Contact</h2>
          <p>
            Questions about these terms or a request for help:{" "}
            <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a>. For privacy requests, see{" "}
            <Link to="/data-requests">data requests</Link>. For safety concerns and removal requests,
            see <Link to="/safety">safety and removal</Link>.
          </p>
        </div>
      </section>
    </>
  );
}
