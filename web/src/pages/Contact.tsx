import { Link } from "react-router-dom";

import { usePageMeta } from "../components/usePageMeta";
import { CONTACT_EMAIL } from "../siteConfig";

export function Contact() {
  usePageMeta(
    "Contact Chirp — Chirp",
    "Reach Chirp for early access, press, privacy and data requests, or to report a security issue.",
  );

  return (
    <>

      <section className="page-head">
        <div className="wrap">
          <p className="eyebrow">Contact</p>
          <h1 className="display">Get in touch.</h1>
          <div className="accent-bar" aria-hidden="true"></div>
          <p className="lede">
            Chirp is rolling out campus by campus. Here&rsquo;s where to reach us
            depending on what you need.
          </p>
        </div>
      </section>

      <section className="section section--tight">
        <div className="wrap">
          <div className="grid grid--2">
            <article className="card">
              <h2 className="title">Chapters &amp; student orgs</h2>
              <p className="caption" style={{ marginTop: "var(--space-2)", lineHeight: "1.6" }}>
                Run a fraternity, sorority, club, or intramural team and want
                early access for your org? Email{" "}
                <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a>.
              </p>
            </article>

            <article className="card">
              <h2 className="title">Press</h2>
              <p className="caption" style={{ marginTop: "var(--space-2)", lineHeight: "1.6" }}>
                Working on a story about Chirp? Reach us at{" "}
                <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a>.
              </p>
            </article>

            <article className="card">
              <h2 className="title">Privacy &amp; data requests</h2>
              <p className="caption" style={{ marginTop: "var(--space-2)", lineHeight: "1.6" }}>
                For questions about your data, or to make a request under our
                privacy policy, email <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a>.
                The full policy is at <Link to="/privacy">/privacy</Link>.
              </p>
            </article>

            <article className="card">
              <h2 className="title">Security reports</h2>
              <p className="caption" style={{ marginTop: "var(--space-2)", lineHeight: "1.6" }}>
                Found a security issue? Report it to{" "}
                <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a>.
              </p>
            </article>
          </div>
        </div>
      </section>
    </>
  );
}
