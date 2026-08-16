import { useEffect, useState } from "react";

import { getCampus, type CampusOut } from "@/api/auth";

import { useSession } from "./SessionProvider";

/**
 * The signed-in user's campus, resolved through GET /campuses/{id} (added with
 * c46 precisely because users.campus_id existed with no way to turn it into a
 * name — which is why three screens hardcoded a mock campus instead).
 *
 * Fails soft to null on purpose. The campus name is a cosmetic label in every
 * one of its call sites, so a failed lookup must render an absent eyebrow rather
 * than take down the screen around it. An absent label beats a wrong one, and a
 * wrong one is what the mock was.
 *
 * Returns null while loading too — callers cannot distinguish "still fetching"
 * from "failed", and deliberately should not: both mean "do not render a name
 * yet", and giving them a third state only invites a spinner where a quietly
 * absent label is correct.
 */
export function useCampus(): CampusOut | null {
  const { user } = useSession();
  const campusId = user?.campus_id ?? null;
  const [campus, setCampus] = useState<CampusOut | null>(null);

  useEffect(() => {
    if (campusId === null) {
      setCampus(null);
      return;
    }

    // Guards against a stale response overwriting a newer one if campusId
    // changes while a request is in flight.
    let active = true;
    getCampus(campusId)
      .then((value) => {
        if (active) setCampus(value);
      })
      .catch(() => {
        if (active) setCampus(null);
      });

    return () => {
      active = false;
    };
  }, [campusId]);

  return campus;
}
