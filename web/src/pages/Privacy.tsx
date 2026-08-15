import { Link } from "react-router-dom";

import { usePageMeta } from "../components/usePageMeta";

/*
  JOSE — BEFORE THIS GOES LIVE, these need a human (and in places a lawyer):

  1. Sections marked with an HTML comment "NEEDS DECISION" below. Each one is a
     product/legal call I cannot make for you: data retention, ghost profiles,
     the age minimum, and which privacy regimes apply.
  2. The contact address (CONTACT_EMAIL_PLACEHOLDER) must be replaced everywhere.
  3. The legal entity name, if Chirp is incorporated.

  Everything NOT marked is a factual statement about what the code actually does,
  traced to a model file under backend/app/models/. Those are safe as written; if
  the schema changes, this page has to change with it.
*/
export function Privacy() {
  usePageMeta(
    "Privacy policy — Chirp",
    "What Chirp collects, what it does not, and who can see it. Written against the actual database, not a template.",
  );

  return (
    <>
      <section className="wrap page-head">
        <p className="eyebrow">Legal</p>
        <h1 className="display">Privacy policy</h1>
        <div className="accent-bar" aria-hidden="true"></div>
        <p className="lede">
          Written against Chirp&rsquo;s actual database rather than a template, because a
          privacy policy that does not match the code is worth nothing.
        </p>
        <div className="legal-meta">
          <span className="chip">Last updated August 2026</span>
          <span className="chip chip--accent">Applies to the Chirp app</span>
        </div>
      </section>

      <section className="wrap section--tight">
        <div className="prose">

          <div className="note">
            <p><strong>The short version.</strong> We store what the app needs to work:
            who you are, which campus and orgs you belong to, and what you post. We
            never see your card or bank number. Your message content is stored in a
            form our systems are not built to read. Your anonymous posts are anonymous
            to other students, but we do record who wrote them, and we explain exactly
            why below.</p>
          </div>

          <h2>1. What we collect when you sign up</h2>
          <p>
            To create an account we store your email address, your display name, an
            optional avatar image link, the account type you pick (student, member of
            a fraternity or sorority, or alum), and the campus you select. We also
            store an identifier from our sign-in provider so we can recognise you when
            you come back, and the date your account was created.
          </p>
          {/* NEEDS DECISION: there is no .edu verification anywhere in the auth code
              and no age gate. Legal should confirm the eligibility and minimum-age
              language before launch. Do not imply we verify school affiliation. */}
          <p>
            Your campus and account type are self-declared. We do not currently verify
            that you attend the school you select.
          </p>

          <h2>2. Your orgs</h2>
          <p>
            If you join a chapter or student org we store your membership: which org,
            your role in it, your membership status, your pledge class if you enter
            one, and when you joined. Invite codes you create or redeem are stored
            with the org they belong to, the role they grant, who created them, and
            when they expire.
          </p>

          <h2>3. What you post</h2>
          <p>
            Posts, comments and likes are stored with your account, the org or campus
            they were posted to, and when they were created. Posts are visible
            according to the audience chosen when they were written: an org post is
            private to that org and never appears on the campus feed.
          </p>
          {/* NEEDS DECISION: posts, comments and yaks all use soft-delete
              (deleted_at / removed_at) and there is no purge job anywhere in the
              codebase, so "deleted" content is currently retained indefinitely.
              Either ship a purge job or keep the honest wording below. Do not
              promise deletion we do not perform. */}
          <p>
            When you delete a post or comment it is removed from view in the app. We
            retain a copy for a limited period so that content can be restored after
            an error and so that moderation reports remain reviewable.
          </p>

          <h2>4. The anonymous board</h2>
          <p>
            Posts on Chirp&rsquo;s campus board are anonymous <strong>to other students</strong>.
            They are not anonymous to Chirp: our database records which account wrote
            each post and which account cast each vote.
          </p>
          <p>
            We are telling you this plainly because most apps in this category do not.
            The author of a board post is never included in any response our app can
            request, which is enforced in the server code rather than hidden in the
            interface. We keep the record so that we can act on abuse, threats and
            illegal content, and so that a court order can be answered honestly rather
            than with a claim we cannot support.
          </p>

          <h2>5. Messages</h2>
          <p>
            Message content is stored only as an opaque block of data. Our servers do
            not parse it, index it, log it, or use it to target anything.
          </p>
          <p>
            What we do hold about a conversation: who is in it, the title if it is a
            group, when each message was sent, delivery and read receipts, and the
            public keys your devices publish so other devices can reach them. That is
            metadata, and it is real &mdash; we can see that two accounts are talking, and
            how often.
          </p>

          <h2>6. Reports and moderation</h2>
          <p>
            If you report a post, a board post, or a message, we store the report, what
            was reported, who reported it, and the reason given.
          </p>
          <p>
            <strong>Reporting a private message is the one case where message content
            becomes readable to us.</strong> When you submit that report, your app
            includes the text of the reported message so a human can judge it. Nothing
            else in the conversation is included, and it is only attached to that
            report. If you do not want a message read by a moderator, do not report it.
          </p>

          <h2>7. Family tree and lineage</h2>
          <p>
            If your org uses the family tree, we store the big and little relationships
            entered, the family a member belongs to, the pledge class, and who recorded
            the relationship.
          </p>
          {/* NEEDS DECISION: User.is_ghost lets an officer create a placeholder
              profile for someone who is NOT a Chirp user. That means data about a
              non-user, entered by a third party. Needs a stated stance: who may
              request removal, and on whose behalf. */}
          <p>
            Officers can add placeholder entries for former members who do not have a
            Chirp account, so that a tree is not full of gaps. If you are named in a
            tree without having a Chirp account and want that entry removed, contact
            us at CONTACT_EMAIL_PLACEHOLDER and we will remove it.
          </p>

          <h2>8. Dues and payments</h2>
          <p>
            <strong>Chirp never stores your card number or your bank account number.</strong>
            Those go directly from your device to our payment processor, Stripe, and
            never reach our servers.
          </p>
          <p>
            What we store is the record of a payment: the amount, the date, which dues
            cycle it was for, which member it relates to, and a reference identifier
            that lets us match it to Stripe. We also store a customer identifier that
            Stripe issues so that a saved payment method works next time, and the
            identifiers of the payment events we have already processed, so a repeated
            message from Stripe cannot be counted twice.
          </p>
          <p>
            <strong>Your chapter is the merchant, not Chirp.</strong> Dues are charged
            into an account your chapter controls. We take a platform fee on each
            payment and never take custody of the rest. That also means your chapter,
            not Chirp, is responsible for refunds and disputes.
          </p>
          <p>
            A chapter&rsquo;s financial ledger is append-only. Entries can be added and
            corrected with a new entry, but not edited or erased, so the history stays
            auditable when officers change over.
          </p>

          <h2>9. Meetings, roster and alumni</h2>
          <p>
            For orgs that use them, we store meetings with their date and minutes, and
            attendance status per member. Minutes are written by your officers; Chirp
            does not review them, and they can name members. Alumni profiles are
            opt-in and store only what you enter: graduation year, company, job title,
            industry, location, a LinkedIn link, and whether you are open to
            mentoring. Job posts store what the poster writes.
          </p>

          <h2>10. Notifications</h2>
          <p>
            Push notifications are deliberately content-free. A notification tells you
            that something happened and who it involves; it never carries the text of
            a message or post in its payload.
          </p>

          <h2>11. Who we share data with</h2>
          <p>
            We do not sell your personal information, and we do not share it for
            advertising. We use these service providers to run Chirp:
          </p>
          <ul>
            <li><strong>Stripe</strong> &mdash; payment processing for dues.</li>
            <li><strong>Google Firebase</strong> &mdash; sign-in, and hosting for this website.</li>
            <li><strong>Google Cloud</strong> &mdash; the servers and database Chirp runs on.</li>
            <li><strong>Push notification services</strong> operated by Apple, Google and Expo, to deliver notifications to your device.</li>
          </ul>
          {/* NEEDS DECISION: legal should confirm this subprocessor list is complete
              and sign off on the disclosure wording. */}
          <p>
            We also disclose information when we are legally required to, and when it
            is necessary to investigate abuse or protect someone&rsquo;s safety.
          </p>

          <h2>12. Your choices</h2>
          <p>
            You can edit or remove most of what you have entered from inside the app:
            your display name and avatar, your alumni profile, your posts and
            comments. To request a copy of your data, or deletion of your account,
            contact us at CONTACT_EMAIL_PLACEHOLDER.
          </p>
          {/* NEEDS DECISION: whether CCPA / GDPR / other regimes apply depends on
              where real users are. Legal call, and it changes what this section must
              promise and how fast. */}
          <p>
            Deleting your account removes your profile and disconnects it from your
            orgs. Content that belongs to an org&rsquo;s shared record &mdash; a chapter&rsquo;s
            financial ledger entries, meeting attendance &mdash; is retained by that org,
            because those are the org&rsquo;s books rather than your personal data alone.
          </p>

          <h2>13. Security</h2>
          <p>
            Credentials are held in a managed secret store rather than in our code.
            Payment event payloads are never written to our logs, because they carry
            personal information. Access to org data is scoped on the server to the
            org and campus you belong to, not filtered in the app after the fact.
          </p>

          <h2>14. Changes</h2>
          <p>
            If we change this policy we will update the date at the top of this page.
            If a change materially affects what we collect or who sees it, we will say
            so in the app rather than only here.
          </p>

          <h2>15. Contact</h2>
          <p>
            Questions about this policy, or a privacy request:
            CONTACT_EMAIL_PLACEHOLDER.
          </p>

          <div className="note">
            <p>Chirp&rsquo;s <Link to="/terms">Terms of Service</Link> cover the rules for using
            the app, including dues and disputes.</p>
          </div>

        </div>
      </section>
    </>
  );
}
