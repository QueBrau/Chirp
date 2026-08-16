import { Link } from "react-router-dom";

import { usePageMeta } from "../components/usePageMeta";
import {
  CONTACT_EMAIL,
  GOVERNING_STATE,
  LAUNCH_CAMPUS,
  LEGAL_LAST_UPDATED,
  MIN_AGE,
} from "../siteConfig";

/*
  JOSE — read this before changing anything below.

  Every factual claim on this page is traceable to a model under
  backend/app/models/. That is the whole value of it: a privacy policy written
  from a template describes a product we do not have, and the first person to
  diff it against the schema finds it out. If the schema changes, this page
  changes in the same PR.

  Three statements here are deliberately unflattering, and they are the ones
  most worth keeping honest:

  1. Anonymous board posts are anonymous to other students, NOT to us. We store
     author_id. Saying otherwise would be a lie we could not keep.
  2. Reporting a private message forwards that message's text to us. That is
     what forwarded_plaintext on content_reports is.
  3. Deleted content is hidden immediately but NOT purged, because no purge job
     exists yet (grep: there is no DELETE FROM anywhere in the app). The page
     says what is true and offers a manual route. Do not upgrade that wording to
     "permanently deleted" until a job actually does it — that is board c69.

  Written for North Carolina and UNCG only, deliberately, rather than hedged
  across fifty states. Revisit when the first campus outside NC onboards.
*/
export function Privacy() {
  usePageMeta(
    "Privacy policy — Chirp",
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
              number. Your message content is stored in a form our systems are not built to read.
              Your anonymous posts are anonymous to other students, but we do record who wrote
              them &mdash; and we explain exactly why below. You can get anything removed by
              emailing <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a>.
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
            you pick (student, member of a fraternity or sorority, or alum), and the campus you
            select. We also store an identifier from our sign-in provider so we can recognise you
            when you return, and the date your account was created.
          </p>
          <p>
            Your campus and account type are self-declared. We do not currently verify that you
            attend the school you select, and we do not ask for a student ID number.
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
            We are telling you this plainly because most apps in this category do not. The author of
            a board post is never included in any response our app can request &mdash; that is
            enforced in the server code, not hidden in the interface. We keep the record so we can
            act on harassment, threats and illegal content, and so that a court order can be
            answered honestly rather than with a claim we cannot support.
          </p>

          <h2>6. Messages</h2>
          <p>
            Message content is stored only as an opaque block of data. Our servers do not parse it,
            index it, log it, or use it to target anything.
          </p>
          <p>
            What we do hold about a conversation: who is in it, the title if it is a group, when
            each message was sent, delivery and read receipts, and the public keys your devices
            publish so other devices can reach them. That is metadata, and it is real &mdash; we can
            see that two accounts are talking, and how often.
          </p>

          <h2>7. Reports and moderation</h2>
          <p>
            If you report a post, a board post, or a message, we store the report, what was
            reported, who reported it, and the reason you gave.
          </p>
          <p>
            <strong>Reporting a private message is the one case where message content becomes
            readable to us.</strong> When you submit that report, your app includes the text of the
            reported message so a person can judge it. Nothing else from the conversation is
            included, and it is attached only to that report. If you do not want a message read by a
            moderator, do not report it.
          </p>
          <p>
            Reports are reviewed by Chirp, and for org and campus content, by the officers of the
            campus the content was posted to.
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
            Chirp takes a platform fee on each dues payment &mdash; 1% on card payments and 2% on
            bank transfers &mdash; which is deducted at the time of the charge. Stripe charges its
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
            Our hosting provider records standard server logs when your app talks to us &mdash;
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
            <li><strong>Stripe</strong> &mdash; payment processing for dues.</li>
            <li><strong>Google Firebase</strong> &mdash; sign-in, and hosting for this website.</li>
            <li><strong>Google Cloud</strong> &mdash; the servers and database Chirp runs on.</li>
            <li>
              <strong>Apple, Google and Expo</strong> &mdash; delivering push notifications to your
              device.
            </li>
          </ul>
          <p>
            We also disclose information when the law requires it, and when it is necessary to
            investigate abuse or protect someone&rsquo;s safety.
          </p>

          <h2>13. How to get your things removed</h2>
          <p>You can do most of this yourself, in the app:</p>
          <ul>
            <li>edit or clear your display name, avatar and alumni profile;</li>
            <li>delete your own posts and comments;</li>
            <li>leave an org, which removes you from its roster.</li>
          </ul>
          <p>
            For anything else &mdash; a copy of your data, deletion of your whole account, or
            removal of a tree entry about someone who is not a Chirp user &mdash; email{" "}
            <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a> from the address on your account,
            or tell us enough to find the entry if you do not have an account.
          </p>
          <p>
            <strong>We will respond within 30 days.</strong> We may ask you to confirm your identity
            before we act on a request, so that someone else cannot delete your account.
          </p>
          <p>
            Deleting your account removes your profile and disconnects it from your orgs. Two things
            do not go away, and you should know that before you ask:
          </p>
          <ul>
            <li>
              <strong>Your chapter&rsquo;s financial ledger.</strong> Dues payments are part of an
              org&rsquo;s books, which are append-only by design. The entry stays; we can
              disassociate your name from it.
            </li>
            <li>
              <strong>Content someone else reported.</strong> If a report about your content is open
              or was acted on, we keep that record so the decision remains reviewable.
            </li>
          </ul>

          <h2>14. How long we keep things</h2>
          <p>
            When you delete a post or comment in the app, it is hidden from everyone immediately.
            <strong> We do not currently run an automatic job that erases it from the database
            afterwards</strong>, so a hidden copy remains until we remove it. We would rather tell
            you that than claim a deletion we do not perform.
          </p>
          <p>
            If you want content actually erased rather than hidden, email{" "}
            <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a> and we will do it by hand. We are
            building automatic purging, and this section will change when it ships.
          </p>
          <p>
            Backups of the database are retained by our hosting provider on a rolling basis, so
            deleted content can persist in a backup for a period after removal.
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
