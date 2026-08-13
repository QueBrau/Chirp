/** Web has no Stripe native module, so the dues flow degrades to an explanation. */

import type { StripeSdk } from "./stripeSdk";

export type { StripeSdk };

export const stripeSdk: StripeSdk | null = null;
