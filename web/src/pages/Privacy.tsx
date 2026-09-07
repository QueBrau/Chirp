import { Link } from "react-router-dom";

import { usePageMeta } from "../components/usePageMeta";
import {
  CONTACT_EMAIL,
  GOVERNING_STATE,
  LAUNCH_CAMPUS,
  LEGAL_LAST_UPDATED,
  MIN_AGE,
} from "../siteConfig";

/* Product facts are mapped to code in docs/public-claims-review.md (c367).
   Counsel review remains c75. Do not promise implemented E2EE, automatic account
   deletion or a successful retention job from schema names or intended policy. */
export function Privacy() {
  usePageMeta(
    "Privacy policy · Chirp",
    "Exactly what Chirp stores, what it never sees, how to get your data removed, and how chapter dues are handled.",
  );

  return (
    <>
      <section className="wrap page-head">
        <p className="eyebrow">Legal</p>
        <h1 className="display">Privacy policy</h1>
        <div className="accent-bar" aria-hidden="true" />
        <p className="lede">
          Written against Chirp&rsquo;s actual database rather than a template, because a privacy
          policy that does not match the code is worth nothing.
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
            <p>
              <strong>The short version.</strong> We store what the app needs to work: who you are,
              which campus and orgs you belong to, and what you post. We never see your card or bank
              number. Messaging does not currently provide end-to-end encryption, and sending
              messages in the mobile app is currently unavailable. Your anonymous posts are
              anonymous to other students, but we do record who wrote them. For data access or
              removal requests, email <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a>.
            </p>
          </div>

          <h2>1. Who this covers</h2>
          <p>
            Chirp is operated from {GOVERNING_STATE} and is currently available only to students and
            alumni of {LAUNCH_CAMPUS}. This policy is written for that. If we open Chirp to campuses
            in other states we will update it before we do, not after.
          </p>
          <p>
            Chirp is not affiliated with, endorsed by, or operated by {LAUNCH_CAMPUS} or any
            fraternity or sorority&rsquo;s national organization. We are not a school official, and
            nothing you put in Chirp becomes part of an education record.
          </p>

          <h2>2. What we collect when you sign up</h2>
          <p>
            Your email address, your display name, an optional avatar image link, the account type
            you pick (student, member of a fraternity or sorority, or alum). We also store
            an identifier from our sign-in provider so we can recognise you
            when you return, and the date your account was created.
          </p>
          <p>
            Your account type is self-declared. To unlock campus features, you verify access to
            a supported school email address using a code. The email domain determines your campus.
            We store that address, the verification attempt and its outcome. This checks access to
            the mailbox, not current enrollment or student status. We do not ask for a student ID number.
          </p>

          <h2>3. Your orgs</h2>
          <p>
            If you join a chapter or student org we store your membership: which org, your role in
            it, your membership status, your pledge class if you enter one, and when you joined.
            Invite codes you create or redeem are stored with the org they belong to, the role they
            grant, who created them, and when they expire.
          </p>
          <p>
            Officers of your org can see this. That is the point of a roster, but it is worth
            saying plainly: your role, status and pledge class are visible to your org&rsquo;s
            leadership, not just to us.
          </p>

          <h2>4. What you post</h2>
          <p>
            Posts, comments and likes are stored with your account, the org or campus they were
            posted to, and when they were created. Visibility follows the audience chosen when the
            post was written: an org post is private to that org and never appears on the campus
            feed.
          </p>

          <h2>5. The anonymous board</h2>
          <p>
            Posts on Chirp&rsquo;s campus board are anonymous <strong>to other students</strong>.
            They are not anonymous to Chirp: our database records which account wrote each post and
            which account cast each vote.
          </p>
          <p>
            Student-facing board responses omit the author identity. Chirp retains the underlying
            authorship record for authorized safety and operational work. We keep the record so we can
            act on harassment, threats and illegal content, and so that a court order can be
            answered honestly rather than with a claim we cannot support.
          </p>

          <h2>6. Messages</h2>
          <p>
            Sending messages in the mobile app is currently unavailable. The current messaging
            system uses encrypted connections to our servers; it does not provide end-to-end
            encryption. Do not rely on it to keep message content unreadable to Chirp.
          </p>
          <p>
            Conversation records include participants, group titles and message times. Our
            messaging backend also stores submitted message data, device public keys and delivery
            receipts. It does not currently store a separate read receipt. These records can show
            which accounts communicate and when.
          </p>

          <h2>7. Reports and moderation</h2>
          <p>
            If you report content, we store what was reported, who reported it and the reason you
            gave. A report can include a copy of the reported message for a moderator to read.
          </p>
          <p>
            In-app reports are reviewed by eligible officers of approved chapters, within the
            content scope their permissions allow. Holding an officer role alone does not grant
            campus-wide access. Chirp can also access retained records for authorized safety and
            operational work.
          </p>

          <h2>8. Family tree and lineage</h2>
          <p>
            If your org uses the family tree, we store the big and little relationships entered, the
            family a member belongs to, the pledge class, and who recorded the relationship.
          </p>
          <p>
            Officers can add placeholder entries for former members who do not have a Chirp account,
            so a tree is not full of gaps. <strong>If you are named in a tree and do not have a
            Chirp account, you can still have that entry removed.</strong> Email{" "}
            <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a> with the org and the name, and we
            will remove it. You do not need an account to make that request, and we will not require
            you to create one.
          </p>

          <h2>9. Dues, payments and how payouts work</h2>
          <p>
            <strong>Chirp never stores your card number or your bank account number.</strong> Those
            go directly from your device to our payment processor, Stripe, and never reach our
            servers.
          </p>
          <p>What we store is the record of a payment:</p>
          <ul>
            <li>the amount and the date;</li>
            <li>which dues cycle it was for, and which member it relates to;</li>
            <li>a reference identifier that lets us match it to Stripe;</li>
            <li>
              a customer identifier Stripe issues, so a saved payment method works next time;
            </li>
            <li>
              the identifiers of payment events we have already processed, so a repeated message
              from Stripe cannot be counted twice.
            </li>
          </ul>
          <p>
            <strong>Your chapter is the merchant, not Chirp.</strong> When you pay dues, the money
            is charged into a Stripe account your chapter owns and controls. It does not pass
            through a Chirp bank account, and we cannot move it. Stripe pays your chapter out on
            Stripe&rsquo;s schedule, to the bank account your chapter&rsquo;s treasurer connected.
          </p>
          <p>
            Chirp takes a platform fee on each dues payment (1% on card payments and 2% on
            bank transfers), which is deducted at the time of the charge. Stripe charges its
            own processing fee on top of that, to your chapter. Everything else is your
            chapter&rsquo;s money from the moment it settles.
          </p>
          <p>
            Because your chapter is the merchant, your chapter also handles refunds and payment
            disputes. If you think you were charged in error, raise it with your treasurer first. We
            can help with a technical failure of the payment itself; we cannot decide whether you
            owed the dues.
          </p>
          <p>
            A chapter&rsquo;s financial ledger is append-only. Entries can be added, and a mistake
            can be corrected by recording a correcting entry, but nothing can be edited or erased
            after the fact. That is deliberate: it means the books a treasurer hands to next
            year&rsquo;s treasurer are complete.
          </p>
          <p>
            Stripe handles your payment details under its own privacy policy, which you should read
            if you pay dues through Chirp.
          </p>

          <h2>10. Meetings, roster and alumni</h2>
          <p>
            For orgs that use them, we store meetings with their date and minutes, and attendance
            status per member. Minutes are written by your officers; Chirp does not review or
            approve them, and they can name members. Alumni profiles are opt-in and store only what
            you enter: graduation year, company, job title, industry, location, a LinkedIn link, and
            whether you are open to mentoring. Job posts store what the poster writes.
          </p>

          <h2>11. What we collect automatically</h2>
          <p>
            Our hosting provider records standard server logs when your app talks to us,
            including your IP address, the time of the request, and which endpoint was called. We
            use these to keep the service running and to investigate abuse. We do not use them to
            build advertising profiles, and we do not sell them.
          </p>
          <p>
            Push notifications are deliberately content-free by design. A notification tells you
            that something happened and who it involves; it never carries the text of a message or
            post.
          </p>
          <p>
            This website uses no analytics, no advertising trackers and no cookies.
          </p>

          <h2>12. Who we share information with</h2>
          <p>
            We do not sell your personal information, and we do not share it for advertising. We use
            these providers to run Chirp:
          </p>
          <ul>
            <li><strong>Stripe</strong>: payment processing for dues.</li>
            <li><strong>Google Firebase</strong>: sign-in, and hosting for this website.</li>
            <li><strong>Google Cloud</strong>: the servers and database Chirp runs on.</li>
            <li>
              <strong>Apple, Google and Expo</strong>: delivering push notifications to your
              device.
            </li>
          </ul>
          <p>
            We also disclose information when the law requires it, and when it is necessary to
            investigate abuse or protect someone&rsquo;s safety.
          </p>

          <h2>13. How to request access or removal</h2>
          <p>
            The app lets you edit your profile and remove supported content. For a copy of your
            data, account deletion, membership removal, or help with a tree entry, email{" "}
            <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a>. Account deletion and data export
            are currently handled through support; there is no self-service account deletion flow.
          </p>
          <p>
            Contact us from the address on your account, or provide enough information to locate
            a tree entry if you do not have an account. We may ask you to confirm your identity
            before acting. <strong>We will respond within 30 days.</strong>
          </p>
          <p>
            A removal request does not erase every related record. Chapter financial ledgers are
            retained as the org&rsquo;s books, and moderation records may need to remain for review.
            We will explain what can be removed and what must be retained when handling your request.
          </p>

          <h2>14. How long we keep things</h2>
          <p>
            Removed posts, comments and Chirps are hidden from normal feeds immediately. Our
            retention policy makes this content eligible for deletion from the active database
            after 30 days. Deletion depends on the retention process completing successfully;
            it is not a guarantee of erasure on an exact date.
          </p>
          <p>
            Removing a post also makes its attached comments and likes eligible for deletion with
            it. A comment removed on its own follows its own removal date. Associated moderation
            and financial records can be retained separately.
          </p>
          <p>
            Contact <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a> for help with removal
            or questions about retained data. Backup copies can retain deleted content until
            they expire under the backup retention schedule.
          </p>

          <h2>15. Security</h2>
          <p>
            Credentials are held in a managed secret store rather than in our code. Payment event
            payloads are never written to our logs, because they carry personal information. Access
            to org data is scoped on the server to the org and campus you belong to, rather than
            filtered in the app after the fact.
          </p>
          <p>
            No system is perfect. If a breach affects your personal information, we will notify you
            as required by {GOVERNING_STATE} law, including the notice requirements of North
            Carolina&rsquo;s Identity Theft Protection Act.
          </p>
          <p>
            If you find a security problem in Chirp, please tell us at{" "}
            <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a> before disclosing it publicly. We
            will not pursue you for reporting something you found in good faith.
          </p>

          <h2>16. Age</h2>
          <p>
            You must be at least {MIN_AGE} years old to create a Chirp account. Chirp is not
            directed to children, and we do not knowingly collect information from anyone under{" "}
            {MIN_AGE}. If you believe a younger person has an account, email{" "}
            <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a> and we will remove it.
          </p>

          <h2>17. Changes</h2>
          <p>
            If we change this policy we will update the date at the top of this page. If a change
            materially affects what we collect or who can see it, we will say so in the app rather
            than only here.
          </p>

          <h2>18. Contact</h2>
          <p>
            Questions about this policy, a privacy request, or a removal request:{" "}
            <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a>. Chirp is operated from{" "}
            {GOVERNING_STATE}.
          </p>

          <div className="note">
            <p>
              Chirp&rsquo;s <Link to="/terms">Terms of Service</Link> cover the rules for using the
              app, including dues and disputes.
            </p>
          </div>
        </div>
      </section>
    </>
  );
}
