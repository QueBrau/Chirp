import { Link } from "react-router-dom";

import { usePageMeta } from "../components/usePageMeta";
import { CONTACT_EMAIL, LEGAL_AUDIENCE, LEGAL_LAST_UPDATED, LEGAL_OPERATORS, MIN_AGE, OPERATING_STATE } from "../siteConfig";

// Source map: docs/legal-data-inventory.md. Publication requires the operational
// review in docs/legal-publication-checklist.md; copy does not implement erasure.
export function Privacy() {
  usePageMeta(
    "Privacy policy · Chirp",
    "What Chirp collects, why it is used, who can see it, how long it is kept and how to request access or deletion.",
  );

  return (
    <>
      <section className="wrap page-head">
        <p className="eyebrow">Legal</p>
        <h1 className="display">Privacy policy</h1>
        <div className="accent-bar" aria-hidden="true" />
        <p className="lede">How your information is used across Chirp and your organizations.</p>
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
              <strong>Key points.</strong> Chirp stores account, campus and organization information,
              content you submit, and records needed to operate its features. Anonymous posts are
              linked to their authors in our records. Messaging does not currently provide
              end-to-end encryption. We do not sell personal information or share it for advertising.
              You can <Link to="/data-requests">request access, correction or deletion</Link> without
              creating another account.
            </p>
          </div>

          <h2>1. Who and what this policy covers</h2>
          <p>
            This policy covers the Chirp app and website, an unincorporated service operated from{" "}
            {OPERATING_STATE} by {LEGAL_OPERATORS}.
            Chirp is intended for students and alumni of participating U.S. colleges and universities;
            availability varies by campus and feature. Contact us at{" "}
            <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a>.
          </p>
          <p>
            We receive information from you, from other users and organization officers, from
            sign-in and payment providers, and through operating the service. Organizations may
            enter records about people who do not have accounts. This policy also explains their
            request options. Chirp is independent of schools and organizations, whose own use of
            information may be governed by their policies and obligations.
          </p>

          <h2>2. Accounts and school-email verification</h2>
          <p>
            We store your email, display name, sign-in provider identifier, optional avatar,
            chosen account type, campus association, account status and relevant dates. Our
            authentication provider handles sign-in credentials. If you use an available Apple or
            Google sign-in option, the provider supplies the identity information authorized in
            that flow.
          </p>
          <p>
            To verify a supported school email, we store the address, a protected representation
            of the verification code, attempts, expiration and outcome. Access to that mailbox
            determines campus access; it does not prove current enrollment. Code expiration does
            not itself delete the verification record. We do not ask for a student ID number.
          </p>

          <h2>3. Organizations, alumni and records about other people</h2>
          <p>Depending on the features used, we store:</p>
          <ul>
            <li>Organization details, membership, roles, status, pledge class, officer history and relevant dates.</li>
            <li>Invites and their creator, assigned role, use, expiration and revocation information.</li>
            <li>Events, descriptions, locations, hosts, invitees, inviters and RSVP responses.</li>
            <li>Meeting dates, minutes and attendance; polls, options, votes and results.</li>
            <li>Big/little relationships, families, pledge classes and who entered those records, including placeholders for people without accounts.</li>
            <li>Optional alumni graduation year, company, job title, industry, location, LinkedIn link and mentoring preference, and job listings you submit.</li>
          </ul>
          <p>
            These records support membership, communication, events and organization administration.
            Access depends on the feature and the viewer&rsquo;s role. Officers can manage records
            within their permitted scope; some directories and activities are visible to other
            eligible members. Authorized officers can export supported attendance and financial
            records. An export creates a copy outside Chirp&rsquo;s control.
          </p>
          <p>
            Poll and weekly chapter-ranking results may show aggregates, but the underlying vote
            records identify the account and choice. Aggregate results do not mean the underlying
            votes are anonymous to Chirp. Avoid putting unnecessary sensitive information in shared
            records. If someone enters information about you, you can request correction or removal
            even without an account.
          </p>

          <h2>4. Posts, anonymous Chirps and photos</h2>
          <p>
            We store submitted text and media references, authorship, audience, timestamps, comments,
            likes and votes. Organization posts stay in the organization space rather than appearing
            on the campus feed. Campus content follows that feature&rsquo;s visibility rules.
          </p>
          <p>
            Anonymous Chirps hide authorship from student-facing board responses. Our database
            retains the author and voter connections for service operation and authorized safety
            work. They are not anonymous to Chirp. Other people can save or share content they see;
            removing it from Chirp cannot retrieve those copies.
          </p>
          <p>
            If you choose a photo, it is uploaded to our storage provider. The original temporary
            upload may contain embedded metadata. Finalized display images are resized and
            re-encoded with embedded EXIF metadata removed. This does not remove information visible
            in the image itself. Temporary and finalized files have separate retention, described below.
          </p>

          <h2>5. Messaging and delivery records</h2>
          <p>
            Sending messages in the mobile app is currently unavailable. The current messaging
            system does not provide end-to-end encryption. Do not rely on it to keep message
            content unreadable to Chirp.
          </p>
          <p>
            The backend can store conversation participants and titles, submitted message data,
            message times, device identifiers, labels, public keys and revocation dates,
            delivery receipts and pending or failed delivery
            records. It does not currently store a separate read receipt. These records can reveal
            who communicates and when. Push-notification registration and delivery are not currently
            active.
          </p>

          <h2>6. Dues and financial records</h2>
          <p>
            Stripe collects payment-method details through its payment interface. Chirp&rsquo;s
            payment records contain amounts, dates, dues cycles, member and chapter associations,
            payment status and provider identifiers, rather than full card or bank account numbers.
            We also store installment schedules, manual payment entries, financial notes, approvals,
            corrections and records used to reconcile payment events and investigate mismatches.
            Do not put full payment details in posts, notes or support emails.
          </p>
          <p>
            Authorized members and officers see financial information within their permitted scope.
            Your chapter is the merchant for dues and Stripe processes payments on its connected
            account. Our <Link to="/payments">organization and payment terms</Link> explain fees,
            responsibilities and disputes. Stripe handles information under its own{" "}
            <a href="https://stripe.com/privacy">privacy policy</a>.
          </p>

          <h2>7. Reports, blocks and support</h2>
          <p>
            Reports include the reporter, affected content or account, reason and review information.
            A report can include a copy of a reported message for a moderator to read. We also store
            block relationships and account restrictions. Eligible officers of approved chapters
            review reports only within the scope their permissions allow; being an officer alone
            does not grant campus-wide access. Chirp&rsquo;s team can access retained records for
            authorized operations, safety and legal requests.
          </p>
          <p>
            If you contact us, we receive your email address, message and any information you provide
            to locate or handle the request. Provide only what is needed. Our{" "}
            <Link to="/safety">safety and removal process</Link> explains urgent reports and intimate-image
            removal requests. Do not email passwords, verification codes, full payment details or
            copies of intimate images.
          </p>

          <h2>8. Device storage and operational information</h2>
          <p>
            Sign-in software stores session information on your device, or in browser storage when
            you use a browser, so you can remain signed in. Device settings can control photo-library
            permission. Exported CSV files may remain in device caches or in apps you share them with.
            Removing a local copy or uninstalling Chirp does not delete server records.
          </p>
          <p>
            Our infrastructure records request and error information such as IP address, request
            time, endpoint, client information and operational events. We use this information to
            maintain reliability, investigate failures, prevent fraud and abuse, and enforce access
            and rate limits. Temporary rate-limit records expire separately from account data.
          </p>
          <p>
            The current marketing website does not include advertising trackers or an analytics SDK.
            This is separate from hosting logs, app sign-in storage and the practices of external
            sites or payment and sign-in providers. The current app does not request access to your
            phone contacts or precise device location; you or your organization may enter locations
            in profiles, events or other content.
          </p>

          <h2>9. Why we use information and who receives it</h2>
          <p>
            We record selected actions, such as account setup, campus verification, posts, event
            responses and payment status, with times and relevant account or record identifiers.
            Those identifiers can be linked to your account. We use these events to understand
            feature use and improve the service. These records are separate from advertising
            tracking and can be processed in Google Cloud logs and BigQuery for analysis.
          </p>
          <p>
            We use information to authenticate accounts, verify campus access, provide the features
            you use, process and reconcile payments, communicate about accounts and requests, protect
            users, and maintain and improve service reliability. We do not sell personal information,
            share it for targeted advertising or build advertising profiles from service logs.
          </p>
          <p>In addition to the audiences described above, our providers include:</p>
          <ul>
            <li><strong>Google Firebase:</strong> authentication and website hosting.</li>
            <li><strong>Google Cloud:</strong> application hosting, databases, media storage, caching, logs and usage analysis through BigQuery.</li>
            <li><strong>Stripe:</strong> connected-account setup, dues processing and payment records.</li>
            <li><strong>Resend:</strong> transactional emails, including school-email verification.</li>
            <li><strong>Apple and Google:</strong> identity information when you use their available sign-in options.</li>
            <li><strong>Google Gmail:</strong> correspondence sent to our published support address.</li>
          </ul>
          <p>
            Providers receive information needed for their role and may also process information
            under their own terms and privacy notices. Following a job, profile or other external
            link takes you to another service. Some images are loaded directly from external
            hosts, which receive normal request information, such as your IP address, when an
            image loads. We also disclose information when legally required
            or reasonably necessary to investigate abuse, protect safety or establish, exercise or
            defend legal rights. Our service is intended for U.S. users; that does not guarantee
            that every provider processes or stores information only in the United States.
          </p>

          <h2>10. Retention and deletion</h2>
          <p>
            Retention varies by record and purpose. Information that remains in use for your account
            or organization is generally kept while needed for that function, subject to requests
            and applicable law. We also consider security, unresolved disputes, accounting and legal
            obligations. There is no single deletion period for every category.
          </p>
          <ul>
            <li>
              <strong>Removed posts, comments and Chirps:</strong> removal hides them from normal feeds.
              Records removed more than 30 days ago become eligible for permanent deletion.
              A scheduled daily cleanup deletes eligible records from the active database,
              with related comments, likes and votes as applicable.
              Completion depends on successful processing; it is not an exact erasure date.
            </li>
            <li>
              <strong>Photos:</strong> temporary uploads are subject to a one-day storage lifecycle
              rule, whose processing may take additional time. Finalized photos are stored separately.
              Deleting a database post does not automatically delete its photo; permanent-media
              cleanup is currently reviewed separately. Contact us about removing a photo. Previously
              issued links, caches and copies saved by other people may outlast its feed visibility.
            </li>
            <li>
              <strong>Messaging keys:</strong> eligible consumed, superseded or revoked temporary
              prekey records have a seven-day cleanup period. This does not delete account identities,
              all device keys, conversations or messages, or establish end-to-end encryption.
            </li>
            <li>
              <strong>Other records:</strong> accounts, memberships, verification history, invites,
              conversations, delivery records, events, votes, lineage, alumni information, reports
              and financial records are not all covered by the 30-day content cleanup. Removal
              depends on the relevant feature or a support request. Financial books, shared
              organization records and safety or dispute evidence may require separate review or
              retention; they are not categorically exempt from applicable privacy rights.
            </li>
            <li>
              <strong>Backups, logs, analytics and providers:</strong> these have separate retention and deletion
              processes. A change in the active database does not immediately erase backup copies,
              usage-event copies in the analysis system, provider records, support correspondence
              or organization exports. We will explain
              relevant limits when responding to a request.
            </li>
          </ul>

          <h2>11. Your choices and requests</h2>
          <p>
            You can edit supported profile fields, choose what to share and remove supported content
            in the app. For a copy of your data, correction, account deletion, membership removal or
            a record entered about you, use our <Link to="/data-requests">data-request instructions</Link>
            {" "}or email <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a>. The app does not
            currently provide self-service account deletion or a complete account export.
          </p>
          <p>
            If you are named in a family-tree placeholder and do not have an account, email us with
            the organization and the name. We will remove that placeholder entry after locating
            it and verifying the request. You do not need to create an account.
          </p>
          <p>
            We may verify your identity and authority using information proportionate to the request.
            We will respond within 30 days and meet any shorter deadline that applies. A response
            may explain the status, information needed, or records that must be retained; it does
            not mean every deletion completes within that period.
          </p>
          <p>
            Depending on where you live and which laws apply, you may have rights to know, access,
            correct, delete or obtain a copy of personal information, limit certain processing, or
            appeal a decision. Contact us to exercise a right or ask us to reconsider a response.
            Where applicable, an authorized agent may act for you after appropriate verification.
            We will not discriminate against you for exercising applicable privacy rights. Some
            features cannot work without the information they need.
          </p>

          <h2>12. Security, age and changes</h2>
          <p>
            We use encrypted connections, managed credentials and server-side permissions to protect
            information. No system can guarantee absolute security, and these measures do not make
            messaging end-to-end encrypted. We will provide breach notices when required by applicable
            law. Report a security concern to our contact address; do not access other people&rsquo;s
            information to demonstrate it. We will not pursue you merely for reporting a problem
            you found in good faith.
          </p>
          <p>
            You must be at least {MIN_AGE} to hold an account. Chirp is not directed to children under
            that age. The app does not currently verify age at signup. If you believe a younger
            person has an account, contact us so we can investigate and remove it. The{" "}
            <Link to="/terms">terms</Link> explain the requirement for a parent or guardian&rsquo;s
            permission when a user is below the age of legal adulthood.
          </p>
          <p>
            We update the date on this page when the policy changes. If a change materially affects
            what we collect or who can see it, we will announce it in the app, not only here, and
            obtain additional consent where required. Contact{" "}
            <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a> with questions about this policy.
          </p>
        </div>
      </section>
    </>
  );
}
