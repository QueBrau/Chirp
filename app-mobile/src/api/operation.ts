/** One cancellable budget across every await in a network operation (c343). */
import { currentIdentity, requireIdentity, type AuthIdentity } from "../auth/identity";

export const REQUEST_TIMEOUT_MS = 15_000;
export const UPLOAD_TIMEOUT_MS = 60_000;

export class OperationCancelledError extends Error {
  constructor() { super("Request cancelled."); this.name = "OperationCancelledError"; }
}
export class OperationTimeoutError extends Error {
  constructor() { super("The request timed out. Please try again."); this.name = "OperationTimeoutError"; }
}

/** Safe local copy only; never display an arbitrary SDK/server error.message. */
export function operationErrorMessage(error: unknown): string | null {
  return error instanceof OperationTimeoutError ? "The request timed out. Please try again." : null;
}

export interface OperationOptions { signal?: AbortSignal; timeoutMs?: number }

/** Race each await as well as passing a signal: SDK token promises cannot be aborted. */
export class Operation {
  readonly owner: AuthIdentity;
  readonly signal: AbortSignal;
  private controller = new AbortController();
  private timer: ReturnType<typeof setTimeout>;
  private cleanups: (() => void)[] = [];
  private error: Error | null = null;

  constructor(options: OperationOptions = {}, owner = currentIdentity()) {
    const timeout = options.timeoutMs ?? REQUEST_TIMEOUT_MS;
    if (!Number.isFinite(timeout) || timeout <= 0) throw new Error("Invalid request timeout.");
    this.owner = owner;
    this.signal = this.controller.signal;
    this.timer = setTimeout(() => this.cancel(new OperationTimeoutError()), timeout);
    for (const source of [owner.signal, options.signal]) {
      if (!source) continue;
      const abort = () => this.cancel(new OperationCancelledError());
      source.addEventListener("abort", abort, { once: true });
      this.cleanups.push(() => source.removeEventListener("abort", abort));
      if (source.aborted) abort();
    }
  }

  assertCurrent(): void {
    requireIdentity(this.owner);
    if (this.error) throw this.error;
  }

  async wait<T>(promise: Promise<T>): Promise<T> {
    // Always attach handlers, including when already cancelled, so a late SDK
    // rejection is consumed instead of becoming an unhandled rejection.
    return new Promise<T>((resolve, reject) => {
      const abort = () => { cleanup(); reject(this.error ?? new OperationCancelledError()); };
      const cleanup = () => this.signal.removeEventListener("abort", abort);
      promise.then(value => {
        cleanup();
        try { this.assertCurrent(); resolve(value); } catch (error) { reject(error); }
      }, error => { cleanup(); reject(error); });
      this.signal.addEventListener("abort", abort, { once: true });
      if (this.signal.aborted) abort();
    });
  }

  cancel(error: Error = new OperationCancelledError()): void {
    if (this.error) return;
    this.error = error;
    this.controller.abort();
  }

  dispose(): void {
    clearTimeout(this.timer);
    for (const cleanup of this.cleanups) cleanup();
    this.cleanups = [];
  }
}
