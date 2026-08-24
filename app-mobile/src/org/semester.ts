/**
 * The academic term the officer dashboards mean by "this semester".
 *
 * A chapter calendar does not exist in the schema, so this is a CLIENT-SIDE
 * convention rather than a fact the server knows — which is why the Secretary
 * dashboard keeps an "All time" toggle beside it and always captions the meeting
 * count its numbers are drawn from.
 *
 * It lives here, and not inside a screen, for the same reason app.core.windows
 * exists on the server: the Secretary attendance report (c82/c156) and the
 * President overview (c171) both show a meeting count for "this semester", side by
 * side in the same app. Two screens computing that boundary separately would drift
 * into disagreeing by one meeting, and the numbers would simply be wrong on one of
 * them with nothing to indicate which.
 */

import type { AttendanceWindow } from "@/api/meetings";

/**
 * The current term as an inclusive ISO window: fall is August through December,
 * spring is January through July.
 */
export function currentSemesterWindow(now: Date): AttendanceWindow {
  const year = now.getFullYear();
  const isFall = now.getMonth() >= 7;
  return {
    start: new Date(Date.UTC(year, isFall ? 7 : 0, 1)).toISOString(),
    end: new Date(Date.UTC(year, isFall ? 11 : 6, 31, 23, 59, 59)).toISOString(),
  };
}
