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
 * Jose's call, Aug 2026, and fine for launch. A forwarding alias is the
 * planned upgrade before Chirp is publicised widely — the rest of the
 * reasoning lives with the account notes in the private infra doc (c185).
 */
export const CONTACT_EMAIL = "chirp.shared@gmail.com";
export const LEGAL_OPERATORS = "Jose Perdomo and Braulio Pantoja Esquina";
export const OPERATING_STATE = "Florida";

/** Draft revision date; publication is tracked separately in c435. */
export const LEGAL_LAST_UPDATED = "October 6, 2026";

/**
 * The launch campus is a product fact, not the scope of the legal documents.
 * Florida is the proposed governing law for this unpublished draft, based on
 * the confirmed operating location. Legal allocation remains subject to c75.
 */
export const LAUNCH_CAMPUS = "UNC Greensboro";
export const LEGAL_AUDIENCE = "U.S. colleges and alumni";
export const GOVERNING_STATE = "Florida";

/** Minimum age to hold an account. See the note in Terms section 1. */
export const MIN_AGE = 17;
