/**
 * OwnChapterProvider: single source of truth for "the signed-in user's
 * chapter" — fuses SessionProvider's memberships with one getChapter() fetch,
 * so chapter/_layout.tsx's OrgAccentScope lookup and every chapter screen
 * below it (index.tsx, members.tsx) share the same membership derivation and
 * the same fetch instead of each re-deriving memberships[0] and re-fetching
 * getChapter() on their own.
 *
 * Single-org world for now — memberships[0] is the only chapter a signed-in
 * user can belong to. This is the one place that comment lives.
 *
 * Mounted once in app/(tabs)/chapter/_layout.tsx, wrapping the chapter Stack.
 */

import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

import { getChapter, type ChapterOut, type MembershipOut } from "@/api/chapters";
import { useSession, type SessionStatus } from "@/auth";

export interface OwnChapterContextValue {
  sessionStatus: SessionStatus;
  membership: MembershipOut | null;
  chapter: ChapterOut | null;
  chapterLoading: boolean;
}

const OwnChapterContext = createContext<OwnChapterContextValue | null>(null);

export function OwnChapterProvider({ children }: { children: ReactNode }) {
  const { status: sessionStatus, memberships } = useSession();
  // Single-org world for now: memberships[0] is the only chapter a signed-in
  // user can belong to.
  const membership = memberships[0] ?? null;
  const chapterId = membership?.chapter_id ?? null;
  const [chapter, setChapter] = useState<ChapterOut | null>(null);
  const [chapterLoading, setChapterLoading] = useState(chapterId !== null);

  useEffect(() => {
    if (chapterId === null) {
      setChapter(null);
      setChapterLoading(false);
      return;
    }
    let cancelled = false;
    setChapterLoading(true);
    // Fail soft: an errored fetch just leaves chapter null (default org
    // colors, "couldn't load" treatment) rather than blocking the stack.
    getChapter(chapterId)
      .then((result) => {
        if (!cancelled) setChapter(result);
      })
      .catch(() => {
        if (!cancelled) setChapter(null);
      })
      .finally(() => {
        if (!cancelled) setChapterLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [chapterId]);

  const value = useMemo<OwnChapterContextValue>(
    () => ({ sessionStatus, membership, chapter, chapterLoading }),
    [sessionStatus, membership, chapter, chapterLoading],
  );

  return <OwnChapterContext.Provider value={value}>{children}</OwnChapterContext.Provider>;
}

/** Read the caller's own chapter state. Must be called under OwnChapterProvider (chapter/_layout.tsx). */
export function useOwnChapter(): OwnChapterContextValue {
  const ctx = useContext(OwnChapterContext);
  if (!ctx) throw new Error("useOwnChapter() must be called within an OwnChapterProvider");
  return ctx;
}
