/**
 * Single source of truth for the values the legal pages reference.
 *
 * These live in one place because Privacy and Terms both cite them, and a
 * privacy policy that lists a contact address the company does not read is
 * worse than one with no address at all.
 */

/**
 * Where privacy requests, deletion requests and takedowns actually arrive.
 *
 * Jose's call, Aug 2026, and fine for launch. Worth swapping before Chirp is
 * publicised widely: this is also the Google account that owns the GCP project,
 * Firebase and the Stripe dashboard, so publishing it points phishing at the
 * one mailbox that can reset production. A forwarding alias costs nothing and
 * separates "the public can reach us" from "this inbox owns the infrastructure".
 */
export const CONTACT_EMAIL = "chirp.shared@gmail.com";

/** Shown as the "last updated" line on both legal pages. */
export const LEGAL_LAST_UPDATED = "August 2026";

/**
 * Chirp is launching at UNC Greensboro only, so both legal documents are
 * written for North Carolina rather than hedged across fifty states. Revisit
 * both when the first campus outside NC is onboarded.
 */
export const LAUNCH_CAMPUS = "UNC Greensboro";
export const GOVERNING_STATE = "North Carolina";
export const GOVERNING_COUNTY = "Guilford County, North Carolina";

/** Minimum age to hold an account. See the note in Terms section 1. */
export const MIN_AGE = 17;
