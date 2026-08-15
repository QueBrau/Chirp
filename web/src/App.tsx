import { Route, Routes } from "react-router-dom";

import { Layout } from "./components/Layout";
import { Bare } from "./components/Bare";
import { Home } from "./pages/Home";
import { Features } from "./pages/Features";
import { HowItWorks } from "./pages/HowItWorks";
import { About } from "./pages/About";
import { Contact } from "./pages/Contact";
import { Privacy } from "./pages/Privacy";
import { Terms } from "./pages/Terms";
import { NotFound } from "./pages/NotFound";
import { JoinChapter } from "./pages/JoinChapter";
import { StripeReturn } from "./pages/StripeReturn";
import { StripeRefresh } from "./pages/StripeRefresh";

/**
 * Two route groups, and the split matters.
 *
 * `Layout` routes are the public site: shared header, nav and footer.
 *
 * `Bare` routes are the three functional pages — the two Stripe Connect bounces
 * and the invite hand-off. They get no site chrome because they are not pages
 * anyone browses to; a user lands on them mid-flow, from Stripe's hosted
 * onboarding or from a link in a text message, and the only useful thing on
 * screen is the way back into the app.
 *
 * The two Stripe paths are a contract with backend/app/routers/payments.py:40,
 * which builds them by string concatenation. Renaming them here without
 * changing that function breaks Connect onboarding.
 */
export function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route path="/" element={<Home />} />
        <Route path="/features" element={<Features />} />
        <Route path="/how-it-works" element={<HowItWorks />} />
        <Route path="/about" element={<About />} />
        <Route path="/contact" element={<Contact />} />
        <Route path="/privacy" element={<Privacy />} />
        <Route path="/terms" element={<Terms />} />
        <Route path="*" element={<NotFound />} />
      </Route>

      <Route element={<Bare />}>
        <Route path="/join-chapter" element={<JoinChapter />} />
        <Route path="/stripe/connect/return" element={<StripeReturn />} />
        <Route path="/stripe/connect/refresh" element={<StripeRefresh />} />
      </Route>
    </Routes>
  );
}
