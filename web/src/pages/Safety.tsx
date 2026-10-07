import { Link } from "react-router-dom";

import { usePageMeta } from "../components/usePageMeta";
import { CONTACT_EMAIL, LEGAL_LAST_UPDATED } from "../siteConfig";

export function Safety() {
  usePageMeta("Safety and removal requests · Chirp", "Report abuse, child exploitation or intimate images shared without consent, without creating a Chirp account.");

  return (
    <>
      <section className="wrap page-head">
        <p className="eyebrow">Safety</p>
        <h1 className="display">Report harm. Request removal.</h1>
        <div className="accent-bar" aria-hidden="true" />
        <p className="lede">You can contact Chirp about harmful content even if you do not have an account.</p>
        <div className="legal-meta"><span className="chip">Last updated {LEGAL_LAST_UPDATED}</span></div>
      </section>
      <section className="wrap section--tight">
        <div className="prose">
          <div className="note">
            <p>
              If someone is in immediate danger, contact emergency services. Chirp is not an
              emergency service and does not continuously monitor all content. For reports to
              Chirp, email <a href={`mailto:${CONTACT_EMAIL}?subject=Chirp%20safety%20report`}>{CONTACT_EMAIL}</a>
              {" "}or use an available in-app report control. Copy the address into your email app
              if the link does not open.
            </p>
          </div>
          <h2>Intimate images shared without consent</h2>
          <p>
            We prohibit intimate images shared without the depicted person&rsquo;s consent, including
            digitally created or altered images and sexual deepfakes. The person depicted or their
            authorized representative can request removal without an account. You do not need a
            police report, court order or government ID to submit a request.
          </p>
          <p>
            Email <a href={`mailto:${CONTACT_EMAIL}?subject=Urgent%20intimate-image%20removal`}>{CONTACT_EMAIL}</a>
            {" "}with the subject &ldquo;Urgent intimate-image removal.&rdquo; That subject helps us
            route the message but is not required for a valid request. Include:
          </p>
          <ul>
            <li>Your physical or electronic signature, such as your typed name, or that of the person authorized to act for you.</li>
            <li>Identification of the image and enough information to locate it on Chirp, such as a link, content identifier, organization or campus, account and approximate posting time.</li>
            <li>A brief statement that you believe in good faith the image was shared without the depicted person&rsquo;s consent, with relevant facts supporting that statement.</li>
            <li>Contact information so we can respond, such as an email address.</li>
          </ul>
          <p>
            <strong>Do not attach or resend the intimate image.</strong> Describe where it appears
            instead. Consent to create an image or share it privately does not mean consent to
            publish it on Chirp. You do not need to find every copy before reporting it.
          </p>
          <p>
            After receiving a valid removal request under the TAKE IT DOWN Act, we will remove the
            reported intimate image as soon as possible and within 48 hours, and make reasonable
            efforts within that same 48-hour period to identify and remove known identical copies.
            We will provide a request
            reference and reply about the outcome or any information needed to locate the content.
            The general 30-day privacy-response period does not apply to this removal deadline.
          </p>
          <p>
            We can act on content controlled by Chirp. We cannot remove copies held by other people
            or services. Tell us if content reappears or you believe our decision was mistaken;
            you do not need to create an account to follow up.
          </p>

          <h2>Child safety</h2>
          <p>
            Our <Link to="/community">community guidelines</Link> prohibit child sexual abuse and
            exploitation, including sexual material involving anyone under 18, grooming, trafficking
            and sextortion. Report suspected exploitation through an available in-app control or
            email our safety contact above. Identify the content or conduct and where it appears.
            Do not download, forward or attach suspected child sexual abuse material.
          </p>
          <p>
            We remove prohibited material and make reports to the appropriate authorities when
            legally required. You can also report suspected child exploitation directly to the{" "}
            <a href="https://report.cybertip.org/">National Center for Missing &amp; Exploited Children&rsquo;s CyberTipline</a>.
            A direct report there does not notify Chirp, so contact us separately if content needs
            to be located on our service.
          </p>

          <h2>Other abuse, privacy and intellectual-property concerns</h2>
          <p>
            For harassment, threats, impersonation, doxxing or other rule violations, include enough
            information to locate the content and explain the concern. Use available block controls
            to limit supported interactions. Do not investigate by accessing someone else&rsquo;s
            account or collecting additional private information.
          </p>
          <p>
            For a copyright or other rights concern, identify the material, your rights or authority
            to act and a reply address. For routine access, correction and account-deletion requests,
            see <Link to="/data-requests">your data and account</Link>.
          </p>
          <p>
            Reports and correspondence contain personal information. We use them to investigate,
            respond and meet legal obligations as described in our <Link to="/privacy">privacy policy</Link>.
            Provide only what is needed; never send passwords or verification codes.
          </p>
        </div>
      </section>
    </>
  );
}
