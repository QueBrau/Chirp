import { usePageMeta } from "../components/usePageMeta";

/**
 * Stripe Connect refresh_url.
 *
 * Built by backend/app/routers/payments.py:40 as
 * `${APP_PUBLIC_BASE_URL}/stripe/connect/refresh`. Stripe sends the user here
 * when the single-use Account Link has expired or was already consumed.
 *
 * A replacement link can only be minted by POST
 * /payments/connect/onboarding-link (payments.py:43-84), which requires an
 * authenticated treasurer or president. This page has no auth by design, so it
 * cannot mint one — it can only send the user back into the app to retry.
 * Promising anything more would be a lie about what this page can do.
 */
export function StripeRefresh() {
  usePageMeta("That setup link expired — Chirp");

  return (
    <div className="bounce">
      <div className="bounce__card">
        <div className="bounce__mark" aria-hidden="true">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M23 4v6h-6" />
            <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" />
          </svg>
        </div>

        <h1 className="title">That setup link expired</h1>
        <p className="lede" style={{ margin: "var(--space-4) auto 0", fontSize: 15 }}>
          Stripe setup links are single-use and short-lived. Open Chirp and start payout setup
          again to get a fresh one.
        </p>

        <a className="btn btn--primary" href="chirp://">
          Open Chirp
        </a>

        <p className="caption" style={{ marginTop: "var(--space-5)" }}>
          In the app: Treasurer, then Payments, then Set up payments.
        </p>
      </div>
    </div>
  );
}
