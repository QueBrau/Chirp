import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";

import { Wordmark } from "./Wordmark";

const NAV = [
  { to: "/features", label: "Features" },
  { to: "/how-it-works", label: "How it works" },
  { to: "/about", label: "About" },
  { to: "/contact", label: "Contact" },
];

/**
 * Site chrome for the public pages.
 *
 * This is the whole reason the site is a React app rather than nine hand-copied
 * HTML files: the header and footer exist once. Adding a nav item or a footer
 * link is one edit, not nine, and the pages cannot drift apart.
 */
export function Layout() {
  const { pathname } = useLocation();
  const [menuOpen, setMenuOpen] = useState(false);

  // A client-side route change does not reset scroll the way a document
  // navigation does, so without this you land halfway down a new page.
  useEffect(() => {
    // "instant" explicitly, so this can never be turned into an animated slide
    // by a stylesheet setting scroll-behavior on html.
    window.scrollTo({ top: 0, behavior: "instant" });
    // Tapping a link in the phone menu is a route change, and the menu has to
    // close with it. Doing that here rather than in each link's onClick also
    // covers back/forward, which no click handler would see.
    setMenuOpen(false);
  }, [pathname]);

  // Escape closes the menu. Bound only while it is open, so a site that is a
  // reading surface 99% of the time carries no idle key listener.
  useEffect(() => {
    if (!menuOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMenuOpen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [menuOpen]);

  return (
    <>
      <a className="skip-link" href="#main">
        Skip to content
      </a>

      <header className="site-header">
        <div className="wrap site-header__inner">
          <Wordmark />
          <nav className="site-nav" aria-label="Primary">
            <ul>
              {NAV.map((item) => (
                <li key={item.to}>
                  {/* NavLink sets aria-current="page" itself, so the active
                      state cannot fall out of sync with the route. */}
                  <NavLink to={item.to}>{item.label}</NavLink>
                </li>
              ))}
            </ul>
          </nav>

          {/* Only rendered at phone width, by CSS. The button stays in the DOM
              at every size rather than being conditionally rendered, so the
              panel it controls always has a real aria-controls target. */}
          <button
            type="button"
            className="nav-toggle"
            aria-expanded={menuOpen}
            aria-controls="site-menu"
            onClick={() => setMenuOpen((open) => !open)}
          >
            <span className="nav-toggle__mark" aria-hidden="true">
              <span />
              <span />
            </span>
            {menuOpen ? "Close" : "Menu"}
          </button>
        </div>

        {/* `hidden` rather than conditional rendering: the links stay in the
            document for anything reading it without running the toggle, and
            display:none keeps the collapsed copy out of the accessibility tree
            so the two navs are never announced at once. */}
        <div className="site-menu" id="site-menu" hidden={!menuOpen}>
          <nav className="wrap" aria-label="Site menu">
            <ul>
              {NAV.map((item) => (
                <li key={item.to}>
                  <NavLink to={item.to}>{item.label}</NavLink>
                </li>
              ))}
            </ul>
            <ul className="site-menu__legal">
              <li><NavLink to="/privacy">Privacy policy</NavLink></li>
              <li><NavLink to="/terms">Terms of service</NavLink></li>
            </ul>
          </nav>
        </div>
      </header>

      <main id="main">
        <Outlet />
      </main>

      <footer className="site-footer">
        <div className="wrap">
          <div className="footer__grid">
            <div className="footer__col">
              <Wordmark />
              <p className="caption" style={{ marginTop: "var(--space-3)", maxWidth: "30ch" }}>
                Campus social and student org management, in one app.
              </p>
            </div>
            <div className="footer__col">
              <h2>Product</h2>
              <ul>
                <li><NavLink to="/features">Features</NavLink></li>
                <li><NavLink to="/how-it-works">How it works</NavLink></li>
              </ul>
            </div>
            <div className="footer__col">
              <h2>Company</h2>
              <ul>
                <li><NavLink to="/about">About</NavLink></li>
                <li><NavLink to="/contact">Contact</NavLink></li>
              </ul>
            </div>
            <div className="footer__col">
              <h2>Legal</h2>
              <ul>
                <li><NavLink to="/privacy">Privacy policy</NavLink></li>
                <li><NavLink to="/terms">Terms of service</NavLink></li>
              </ul>
            </div>
          </div>
          <div className="footer__base">
            <p className="caption">&copy; 2026 Chirp</p>
            <p className="caption">Made for students</p>
          </div>
        </div>
      </footer>
    </>
  );
}
