import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

/**
 * Builds to dist/, which is what firebase.json serves.
 *
 * One constraint drives the rest of this config: the two Stripe Connect paths
 * are a contract with the backend. backend/app/routers/payments.py:40 builds
 * `${APP_PUBLIC_BASE_URL}/stripe/connect/return` and `.../refresh` by string
 * concatenation, and Stripe hits them exactly as written. They must resolve
 * with a single 200 — a redirect hop there fails confusingly, long after a real
 * user has finished KYC. Firebase serves the SPA shell for every path and
 * react-router resolves the route client-side, so no path here can 404 or
 * bounce as long as the router knows about it.
 */
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    // The site is small and mostly text. One bundle beats a waterfall of
    // chunks for a first-visit marketing page.
    chunkSizeWarningLimit: 700,
  },
});
