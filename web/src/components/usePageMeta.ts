import { useEffect } from "react";

/**
 * Sets the document title and meta description for a route.
 *
 * A single-page app serves one index.html for every path, so without this every
 * page would share the landing page's title. That is a real problem for two of
 * these pages specifically: /privacy is the URL submitted to the App Store, and
 * a reviewer opening a tab labelled with the marketing headline has reason to
 * think they were sent to the wrong page.
 */
export function usePageMeta(title: string, description?: string): void {
  useEffect(() => {
    document.title = title;

    if (description === undefined) return;
    let tag = document.querySelector<HTMLMetaElement>('meta[name="description"]');
    if (tag === null) {
      tag = document.createElement("meta");
      tag.name = "description";
      document.head.appendChild(tag);
    }
    tag.content = description;
  }, [title, description]);
}
