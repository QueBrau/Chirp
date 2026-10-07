import { Link } from "react-router-dom";

import { usePageMeta } from "../components/usePageMeta";
import { CONTACT_EMAIL, LEGAL_LAST_UPDATED, MIN_AGE } from "../siteConfig";

export function Community() {
  usePageMeta(
    "Community guidelines · Chirp",
    "Rules for posts, messages, events and organization records, including child safety and reporting.",
  );

  return (
    <>
      <section className="wrap page-head">
        <p className="eyebrow">Legal</p>
        <h1 className="display">Community guidelines</h1>
        <div className="accent-bar" aria-hidden="true"></div>
        <p className="lede">Respect the people behind every account, post and organization record.</p>
        <div className="legal-meta">
          <span className="chip">Last updated {LEGAL_LAST_UPDATED}</span>
          <span className="chip">Part of our terms of service</span>
        </div>
      </section>

      <section className="wrap section--tight">
        <div className="prose">
          <p>
            These guidelines apply throughout Chirp, including profiles, anonymous Chirps, posts,
            comments, images, messages, events, polls, job listings and organization records. They
            form part of our <Link to="/terms">terms of service</Link>. Being anonymous, being an
            officer or posting inside a private organization does not create an exception.
          </p>

          <h2>1. Treat people safely and respectfully</h2>
          <ul>
            <li>Do not harass, bully, stalk, threaten, extort or encourage violence against anyone.</li>
            <li>Do not promote hatred or target people with abuse based on their identity.</li>
            <li>Do not organize dangerous hazing, coercion, exploitation or other unlawful activity.</li>
            <li>Do not post sexually explicit material, graphic abuse or content encouraging self-harm.</li>
            <li>Do not use events, jobs, payments or organization roles to defraud or exploit people.</li>
          </ul>

          <h2>2. Child safety</h2>
          <p>
            <strong>Chirp prohibits child sexual abuse and exploitation.</strong> This includes
            child sexual abuse material, sexualized depictions of anyone under 18, grooming,
            solicitation, trafficking, sextortion and attempts to obtain or distribute such
            material. These rules cover images created or altered with artificial intelligence
            as well as real images. They apply even if the person has a Chirp account or is above
            our minimum account age of {MIN_AGE}.
          </p>
          <p>
            Report suspected child exploitation using an available in-app report control or email{" "}
            <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a>. Identify where the content or
            conduct appears. Do not download, forward or attach suspected child sexual abuse
            material to a support email. We remove prohibited material and report to the appropriate
            authorities when legally required. Our <Link to="/safety">safety page</Link> explains
            reporting options and urgent help.
          </p>

          <h2>3. Respect privacy and consent</h2>
          <p>
            Do not post someone&rsquo;s private contact details, credentials, financial information
            or other sensitive personal information without authority and any required permission.
            Do not try to identify an anonymous author through blocking experiments, data collection,
            threats or other means, or encourage others to do so.
          </p>
          <p>
            Intimate images shared without the depicted person&rsquo;s consent are prohibited,
            including altered images and sexual deepfakes. Permission to create an image or share
            it privately is not permission to post it on Chirp. A person depicted in an intimate
            image, or someone authorized to act for them, can use our{" "}
            <Link to="/safety">intimate-image removal process</Link> without creating an account.
          </p>
          <p>
            Enter organization records only for legitimate organization purposes and with the
            authority and permission required. Do not put sensitive information in meeting minutes,
            event descriptions, family trees or other shared records just because you have editing access.
          </p>

          <h2>4. Be authentic and respect rights</h2>
          <ul>
            <li>Do not impersonate a person, school, organization or business.</li>
            <li>Do not submit fraudulent payments, fake records, scams, phishing or misleading job offers.</li>
            <li>Do not post material that infringes copyright, trademark or other rights.</li>
            <li>Do not spam, manipulate votes or reports, or evade an account restriction.</li>
            <li>Do not access accounts or data without authorization, distribute malware or disrupt the service.</li>
          </ul>
          <p>
            If you believe content infringes your rights, email{" "}
            <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a> with a description of your
            concern, enough information to locate the content and a way to contact you.
          </p>

          <h2>5. Reports, blocking and enforcement</h2>
          <p>
            Use the report and block controls available for the relevant feature, or email us if
            you cannot access them. Blocking affects supported app interactions. It cannot remove
            copies already saved by someone else or prevent contact outside Chirp. The{" "}
            <Link to="/privacy">privacy policy</Link> explains how reports and block records are
            stored and who can review them.
          </p>
          <p>
            We may remove content or restrict, suspend or close accounts that violate these rules.
            We may also act on serious safety concerns or legal requirements. Chirp&rsquo;s team
            and eligible organization moderators have different access and responsibilities.
            A report is not guaranteed to be reviewed by a particular person or produce a
            particular action.
          </p>
          <p>
            If you believe an action was mistaken, email us with the affected account or content
            and the reason you are asking us to reconsider. Do not include unnecessary sensitive
            information. For immediate danger, contact emergency services. Chirp is not an
            emergency service and does not provide continuous monitoring of all content.
          </p>

          <div className="note">
            <p>
              <Link to="/safety">Safety and removal requests</Link> provides the dedicated process
              for intimate images shared without consent. The general support response time in
              our terms does not replace that process or its deadlines.
            </p>
          </div>
        </div>
      </section>
    </>
  );
}
