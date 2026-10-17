/** A bounded campus-label lookup. Cancellation and identity checks fence every await. */
export async function recoverCampus<T>(
  lookup: (signal: AbortSignal) => Promise<T>,
  options: {
    signal: AbortSignal;
    ownsRequest: () => boolean;
    delaysMs?: readonly number[];
    sleep?: (milliseconds: number, signal: AbortSignal) => Promise<void>;
  },
): Promise<T | null> {
  const delays = options.delaysMs ?? [250, 750];
  const sleep = options.sleep ?? abortableSleep;
  for (let attempt = 0; attempt <= delays.length; attempt += 1) {
    if (options.signal.aborted || !options.ownsRequest()) return null;
    try {
      const value = await lookup(options.signal);
      return !options.signal.aborted && options.ownsRequest() ? value : null;
    } catch {
      if (options.signal.aborted || !options.ownsRequest()) return null;
      if (attempt === delays.length) return null;
      try {
        await sleep(delays[attempt], options.signal);
      } catch {
        return null;
      }
    }
  }
  return null;
}

function abortableSleep(milliseconds: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.reject(new Error("cancelled"));
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", abort);
      resolve();
    }, milliseconds);
    const abort = () => {
      clearTimeout(timer);
      signal.removeEventListener("abort", abort);
      reject(new Error("cancelled"));
    };
    signal.addEventListener("abort", abort, { once: true });
    if (signal.aborted) abort();
  });
}
