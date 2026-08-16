import { useEffect } from "react";

/**
 * Matches the <meta name="description"> baked into index.html, so a route that
 * supplies none resets to the site default rather than inheriting whatever the
 * previously visited route left behind.
 */
const DEFAULT_DESCRIPTION =
  "Chirp is where students post, where student orgs run themselves, and where chapter dues get paid without a spreadsheet.";

/**
 * Sets the document title and meta description for a route.
 *
 * A single-page app serves one index.html for every path, so without this every
 * page would share the landing page's title. That matters most for /privacy,
 * which is the URL submitted to the App Store — a reviewer opening a tab
 * labelled with the marketing headline has reason to think they were sent to
 * the wrong page.
 */
export function usePageMeta(title: string, description?: string): void {
  // Resolved outside the effect so it can be a dependency. Deriving it inside
  // and listing it in the dep array is a reference error, since the binding
  // only exists within the callback.
  const content = description ?? DEFAULT_DESCRIPTION;

  useEffect(() => {
    document.title = title;

    // Deliberately no early return when description is undefined. The three
    // bounce pages pass none, so navigating from /privacy to an invite link
    // used to leave the privacy policy's description attached to it.
    let tag = document.querySelector<HTMLMetaElement>('meta[name="description"]');
    if (tag === null) {
      tag = document.createElement("meta");
      tag.name = "description";
      document.head.appendChild(tag);
    }
    tag.content = content;
  }, [title, content]);
}
