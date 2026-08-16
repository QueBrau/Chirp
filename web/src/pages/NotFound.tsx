import { Link } from "react-router-dom";

import { usePageMeta } from "../components/usePageMeta";

export function NotFound() {
  usePageMeta(
    "Page not found — Chirp",
    "This page doesn't exist, or it moved. Here's the way back to Chirp.",
  );

  return (
    <section className="section" style={{ textAlign: "center" }}>
      <div className="wrap">
        <p className="eyebrow">404</p>
        <h1 className="display" style={{ fontSize: "clamp(26px, 4vw, 44px)" }}>Page not found</h1>
        <div className="accent-bar" aria-hidden="true" style={{ marginInline: "auto" }}></div>
        <p className="lede" style={{ margin: "var(--space-5) auto 0" }}>
          This page doesn&rsquo;t exist, or it moved. Here&rsquo;s the way back.
        </p>
        <p style={{ marginTop: "var(--space-6)" }}>
          <Link className="btn btn--primary" to="/">Back to Chirp</Link>
        </p>
      </div>
    </section>
  );
}
