import { usePageMeta } from "../components/usePageMeta";

/**
 * Stripe Connect return_url.
 *
 * Built by backend/app/routers/payments.py:40 as
 * `${APP_PUBLIC_BASE_URL}/stripe/connect/return`. Renaming this route without
 * changing that function breaks Connect onboarding.
 *
 * This page deliberately reports nothing about success or failure. Stripe sends
 * the user here when onboarding finishes OR is abandoned, with no query
 * parameters distinguishing the two, and verification continues asynchronously
 * afterwards. The app is the source of truth: get_chapter_payments_status()
 * (payments.py:87-114) re-reads charges_enabled and details_submitted from
 * Stripe on every call rather than trusting any client-supplied "I'm done"
 * signal. So the only honest thing this page can say is "go back and let the
 * app check".
 */
export function StripeReturn() {
  usePageMeta("Back from Stripe · Chirp");

  return (
    <div className="bounce">
      <div className="bounce__card">
        <div className="bounce__mark" aria-hidden="true">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M20 6 9 17l-5-5" />
          </svg>
        </div>

        <h1 className="title">You're back from Stripe</h1>
        <p className="lede" style={{ margin: "var(--space-4) auto 0", fontSize: "var(--reading)" }}>
          We're checking your chapter's payout setup now. Verification can take a moment, and
          sometimes longer if Stripe needs another document.
        </p>

        <a className="btn btn--primary" href="chirp://">
          Open Chirp
        </a>

        <p className="copy" style={{ marginTop: "var(--space-5)" }}>
          If nothing happens, open the Chirp app and go to Treasurer, then Payments.
        </p>
      </div>
    </div>
  );
}
