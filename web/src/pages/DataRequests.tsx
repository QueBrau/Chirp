import { Link } from "react-router-dom";

import { usePageMeta } from "../components/usePageMeta";
import { CONTACT_EMAIL, LEGAL_LAST_UPDATED } from "../siteConfig";

export function DataRequests() {
  usePageMeta("Data and account-deletion requests · Chirp", "Request access, correction or deletion of Chirp data, including records about people without accounts.");

  return (
    <>
      <section className="wrap page-head">
        <p className="eyebrow">Privacy support</p>
        <h1 className="display">Your data and account</h1>
        <div className="accent-bar" aria-hidden="true" />
        <p className="lede">Request a copy, a correction, account deletion or removal of a record about you.</p>
        <div className="legal-meta"><span className="chip">Last updated {LEGAL_LAST_UPDATED}</span></div>
      </section>
      <section className="wrap section--tight">
        <div className="prose">
          <p>
            If the signed-in app shows the account-data screen, you can submit an authenticated
            export or deletion request there and check its status. During the staged rollout, or
            if you cannot sign in, email <a href={`mailto:${CONTACT_EMAIL}?subject=Chirp%20data%20request`}>{CONTACT_EMAIL}</a>
            {" "}with the subject &ldquo;Chirp data request.&rdquo; You do not need to pay or create
            an account to make a request. If you cannot use the email link, copy the address into
            your email app.
          </p>
          <h2>What to include</h2>
          <ul>
            <li>What you want: access or a copy, correction, account deletion, membership removal, or help with a specific record.</li>
            <li>The email associated with your account, if you have one. Contact us from that address if you can; tell us if you no longer have access.</li>
            <li>Enough context to locate the record, such as the organization, your name in a family tree, or a content link or identifier.</li>
            <li>A reply address and, if you are acting for someone else, your relationship and authority to make the request.</li>
          </ul>
          <p>
            Do not send passwords, sign-in codes, full card or bank numbers, government ID documents,
            or intimate images. We may ask for a proportionate way to verify identity or authority
            before sharing information or acting on a request.
          </p>
          <h2>What happens next</h2>
          <p>
            We will respond within 30 days and meet any shorter deadline that applies. We may ask
            for information needed to locate a record or verify the request, explain what can be
            removed and what must be retained, or give the request&rsquo;s status. If we cannot
            fulfill part of a request, we will explain the reason. Reply to ask us to reconsider
            or to make an appeal available under applicable law.
          </p>
          <h2>Account deletion and retained records</h2>
          <p>
            The app request starts a scoped review; it does not erase records automatically or
            guarantee that every provider copy, backup, message, shared organization record or
            financial record can be removed. Signing out, uninstalling the app, leaving an
            organization or removing a post does not delete your whole account. Support remains
            available for requests that need identity verification, provider coordination or a
            fuller explanation of retained records.
          </p>
          <p>
            Deletion requests include review of associated account data, rather than merely disabling
            sign-in. Some financial records, shared organization records, safety evidence and records
            needed for legal obligations may need to remain. There is no blanket exception for all
            organization data. We will explain the scope and status of your request, including
            relevant retention limits.
          </p>
          <p>
            The <Link to="/privacy">privacy policy</Link> describes the separate handling of removed
            content, photos, backups, provider records and exported copies. A 30-day response is not
            a promise that every copy is erased within 30 days. You may also need to contact your
            organization about records it holds outside Chirp.
          </p>
          <h2>Records about someone without an account</h2>
          <p>
            You can request correction or removal of a family-tree placeholder or another record
            about you without signing up. Include the organization and enough information to locate
            it. We will remove a family-tree placeholder about you after locating it and verifying
            the request. We will not require you to create an account to handle the request.
          </p>
          <div className="note">
            <p>
              For intimate images shared without consent, suspected child exploitation or an
              urgent safety issue, use our <Link to="/safety">safety and removal instructions</Link>.
              The general support response time above does not replace urgent removal deadlines.
              For immediate danger, contact emergency services.
            </p>
          </div>
        </div>
      </section>
    </>
  );
}
