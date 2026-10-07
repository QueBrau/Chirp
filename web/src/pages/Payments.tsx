import { Link } from "react-router-dom";

import { usePageMeta } from "../components/usePageMeta";
import { CONTACT_EMAIL, LEGAL_LAST_UPDATED } from "../siteConfig";

export function Payments() {
  usePageMeta(
    "Organization and payment terms · Chirp",
    "Responsibilities for student organization records, Stripe setup, dues, fees and payment disputes.",
  );

  return (
    <>
      <section className="wrap page-head">
        <p className="eyebrow">Legal</p>
        <h1 className="display">Organization and payment terms</h1>
        <div className="accent-bar" aria-hidden="true"></div>
        <p className="lede">How organization tools and dues payments work, and who handles them.</p>
        <div className="legal-meta">
          <span className="chip">Last updated {LEGAL_LAST_UPDATED}</span>
          <span className="chip">Part of our terms of service</span>
        </div>
      </section>

      <section className="wrap section--tight">
        <div className="prose">
          <p>
            These terms supplement the <Link to="/terms">terms of service</Link> for people who
            manage an organization or use Chirp&rsquo;s financial tools. They apply when the relevant
            feature is available. Describing a payment feature here does not mean it is enabled
            for every organization or that a test payment moves real money.
          </p>

          <h2>1. Authority to manage an organization</h2>
          <p>
            If you set up or manage an organization, you must be authorized to act for it. Use
            invites, roles, roster records and exports only for the organization&rsquo;s legitimate
            purposes. Keep its information accurate and arrange appropriate access changes when
            officers leave or change roles. Do not connect a bank or Stripe account you are not
            authorized to use.
          </p>
          <p>
            You must have the authority and any permission required to enter member information,
            attendance, officer history, event invitations, family relationships and financial
            records. Handle exported data securely and respect members&rsquo; privacy rights. Chirp
            cannot recall copies an authorized user has downloaded.
          </p>

          <h2>2. Your chapter and Stripe</h2>
          <p>
            Your chapter sets dues and is the merchant for payments made to it. Stripe processes
            those payments on the chapter&rsquo;s connected account. The chapter is responsible for
            explaining what dues cover and handling its members&rsquo; refund requests and payment
            disputes, subject to applicable law and its agreements with Stripe. Chirp does not
            hold chapter dues in a Chirp bank account.
          </p>
          <p>
            The authorized representative completes Stripe&rsquo;s hosted setup and must review
            and accept the applicable{" "}
            <a href="https://stripe.com/legal/connect-account">Stripe Connected Account Agreement</a>
            {" "}and other terms presented by Stripe. Stripe may request identity, business, tax and
            bank information, determine eligibility and restrict payments or payouts. Stripe&rsquo;s
            terms govern its services. These Chirp terms do not replace them.
          </p>
          <p>
            By authorizing your chapter to use Chirp&rsquo;s connected payment features, you authorize
            Chirp to create and link its connected account, request setup links and account status,
            create customer and payment records, submit member-initiated dues payments, retrieve
            or cancel supported payment attempts, collect the platform fees below, and receive
            payment updates needed to reconcile the chapter&rsquo;s records. This authorization is
            limited to providing those features. It is not permission to make unrelated charges
            or withdrawals.
          </p>
          <p>
            You also authorize the exchange with Stripe of the organization, member and payment
            information needed for those functions, as described in our{" "}
            <Link to="/privacy">privacy policy</Link> and{" "}
            <a href="https://stripe.com/privacy">Stripe&rsquo;s privacy policy</a>. Contact us if your
            organization wants to stop using the connection. Closing a Chirp account does not
            itself close the organization&rsquo;s Stripe account.
          </p>

          <h2>3. Platform fees</h2>
          <p>
            Chirp&rsquo;s platform fee is <strong>1% of each card dues payment</strong> and{" "}
            <strong>2% of each ACH bank-transfer dues payment</strong>, rounded down to the nearest
            cent. The fee is deducted from the chapter&rsquo;s payment proceeds. Stripe&rsquo;s own
            processing fees are separate and are governed by the chapter&rsquo;s Stripe arrangements.
          </p>
          <p>
            The chapter sets the amount its member owes. The platform fee described here is charged
            to the chapter and is not a separate Chirp charge added to the member&rsquo;s dues. We
            will disclose a change to Chirp&rsquo;s fee before applying it to future payments.
          </p>

          <h2>4. Making a payment</h2>
          <p>
            Check the chapter, dues cycle, amount and payment method before confirming. Use only
            a payment method you are authorized to use. Stripe collects payment details through
            its payment interface. Any bank-debit authorization or saved-payment-method choice is
            presented in the payment flow. Accepting these terms by itself does not authorize a
            bank debit or a recurring charge.
          </p>
          <p>
            A submitted payment may still be processing, particularly for bank transfers. An app
            screen, a saved payment method or an officer&rsquo;s manual ledger entry is not by itself
            confirmation that money settled. If a payment result is unclear, check with your chapter
            or contact us before making another payment. Provider delays and restrictions can affect
            when funds become available or reach the chapter&rsquo;s bank.
          </p>

          <h2>5. Installments and financial records</h2>
          <p>
            Officers can record a dues installment plan with amounts, due dates and payments received.
            The current installment tool is a recordkeeping feature. Creating a plan does not
            schedule automatic withdrawals or create a loan from Chirp. Any arrangement to pay dues
            over time is between the member and the chapter.
          </p>
          <p>
            Ledger entries are append-only in the app. An authorized officer corrects a mistake
            through a separate entry linked to the original. Recording a correction or marking an
            installment paid does not itself issue a refund, move money or settle a dispute with
            Stripe. Officers must reconcile records with actual transactions.
          </p>
          <p>
            Financial records can contain personal information and may be retained after a member
            leaves. The <Link to="/privacy">privacy policy</Link> describes retention and access.
            Keeping an organization&rsquo;s books does not remove rights that applicable law gives
            to the people identified in them.
          </p>

          <h2>6. Refunds, disputes and help</h2>
          <p>
            Contact your treasurer about whether dues were owed, what they cover or a refund.
            Contact <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a> about a technical
            problem with Chirp. Include the chapter, approximate payment date and relevant reference
            if available. Do not email full card or bank numbers, passwords or payment secrets.
          </p>
          <p>
            Refunds and chargebacks are subject to the payment provider&rsquo;s processes and the
            chapter&rsquo;s obligations. Ask your chapter about the amount and status of a requested
            refund. Nothing here limits your rights to dispute a payment with
            your bank, card issuer or payment provider, or any rights under applicable law. You
            do not have to wait for a reply from Chirp or your chapter to protect those rights.
          </p>
        </div>
      </section>
    </>
  );
}
