import { useEffect } from "react";
import { Outlet } from "react-router-dom";

/**
 * Chrome-free shell for the three functional pages: the two Stripe Connect
 * bounces and the invite hand-off.
 *
 * No nav and no footer on purpose. Nobody browses to these — you arrive
 * mid-flow from Stripe's hosted onboarding or from a link someone texted you,
 * and the only useful thing on screen is the way back into the app. A marketing
 * header here would just add somewhere else to click at the exact moment the
 * user is trying to finish something.
 *
 * The dark canvas is pinned in both colour schemes (DESIGN.md section 7 treats
 * sign-in as a brand moment; these are its web equivalent), so `on-brand` goes
 * on <body> rather than a wrapper div — otherwise the page background outside
 * the card follows the system theme and the card floats on the wrong colour.
 */
export function Bare() {
  useEffect(() => {
    document.body.classList.add("on-brand");
    return () => document.body.classList.remove("on-brand");
  }, []);

  return <Outlet />;
}
