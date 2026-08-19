import { Link } from "react-router-dom";

/**
 * The closing CTA block that ends Home, About, Features and HowItWorks.
 *
 * Heading, body copy, and the button's label/target are props rather than
 * fixed text because they genuinely differ per page, not by accident: Home
 * and About share a heading but not identical body copy (Home adds "Chirp is
 * rolling out campus by campus" up front), Features and HowItWorks each have
 * their own heading, and Features' button breaks the "Get in touch" -> /contact
 * pattern the other three pages use, pointing at "See how Chirp works" ->
 * /how-it-works instead. Collapsing any of that to a shared default would
 * flatten a real difference into sameness.
 */
interface CtaSectionProps {
  heading: string;
  body: string;
  buttonLabel: string;
  buttonTo: string;
}

export function CtaSection({ heading, body, buttonLabel, buttonTo }: CtaSectionProps) {
  return (
    <section className="section section--tight">
      <div className="wrap cta">
        <h2 className="title cta__heading">{heading}</h2>
        <p className="lede cta__body">{body}</p>
        <p className="cta__action">
          <Link className="btn btn--primary" to={buttonTo}>
            {buttonLabel}
          </Link>
        </p>
      </div>
    </section>
  );
}
