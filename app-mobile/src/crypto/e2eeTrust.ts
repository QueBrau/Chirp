/** Pure helpers of the trust UX (board c463, E2EE milestone M5).
 *
 * No imports of Expo, React or the network, so `npm run verify:e2ee-trust` can run them
 * (and the engine that uses them) under node. Nothing here touches a key or a message.
 */

import type { DirectoryDevice } from "./native";

/**
 * A device comparison code spaced for reading aloud and comparing: `123456789` becomes
 * `123 456 789`. Anything that is not exactly nine digits is returned unchanged rather than
 * guessed at, so a malformed value is visibly wrong instead of looking plausible.
 */
export function groupComparisonCode(code: string): string {
  return /^\d{9}$/.test(code) ? `${code.slice(0, 3)} ${code.slice(3, 6)} ${code.slice(6, 9)}` : code;
}

/**
 * The 60-digit safety number as twelve groups of five, in the core's order. Returns an empty
 * list for anything that is not exactly 60 digits, so a screen never draws half a number.
 */
export function groupSafetyNumber(number: string): string[] {
  if (!/^\d{60}$/.test(number)) return [];
  const groups: string[] = [];
  for (let i = 0; i < 60; i += 5) groups.push(number.slice(i, i + 5));
  return groups;
}

/**
 * A canonical string for one user's directory: every listed device's id, both identities and
 * generation, sorted. Two directories have the same fingerprint exactly when the core would
 * pin the same thing for them, so it identifies "what the person was shown".
 */
export function directoryFingerprint(devices: readonly DirectoryDevice[]): string {
  return devices
    .map((device) => [device.deviceId, device.generation, device.identityEd25519, device.identityCurve25519].join(":"))
    .sort()
    .join("|");
}

/** The fingerprint of a conversation's two directories: the contact's, then this account's. */
export function pairFingerprint(peer: readonly DirectoryDevice[], mine: readonly DirectoryDevice[]): string {
  return `${directoryFingerprint(peer)}#${directoryFingerprint(mine)}`;
}

// ---------------------------------------------------------------------------
// Bounded polling
// ---------------------------------------------------------------------------

export interface PollTimers {
  set(run: () => void, ms: number): unknown;
  clear(handle: unknown): void;
}

const realTimers: PollTimers = {
  set: (run, ms) => setTimeout(run, ms),
  clear: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
};

export interface BoundedPoll {
  stop(): void;
  /** How many times `tick` has been started. */
  readonly ticks: number;
}

/**
 * Call `tick` every `intervalMs` until it answers `true` (done), `stop()` is called, or
 * `budget` ticks have been made, whichever comes first. The next tick is scheduled only after
 * the previous one has finished, so a slow network never stacks calls, and a tick that throws
 * counts against the budget like any other. The first tick is one interval away: the caller
 * does its own immediate check.
 */
export function startBoundedPoll(options: {
  intervalMs: number;
  budget: number;
  tick: () => Promise<boolean>;
  timers?: PollTimers;
}): BoundedPoll {
  const timers = options.timers ?? realTimers;
  let stopped = false;
  let ticks = 0;
  let handle: unknown = null;

  const schedule = () => {
    if (stopped || ticks >= options.budget) return;
    handle = timers.set(() => {
      handle = null;
      if (stopped) return;
      ticks += 1;
      let finished: Promise<boolean>;
      try {
        finished = options.tick();
      } catch {
        finished = Promise.resolve(false);
      }
      void finished.then(
        (done) => {
          if (!done) schedule();
        },
        () => schedule(),
      );
    }, options.intervalMs);
  };
  schedule();

  return {
    stop() {
      stopped = true;
      if (handle !== null) timers.clear(handle);
      handle = null;
    },
    get ticks() {
      return ticks;
    },
  };
}
