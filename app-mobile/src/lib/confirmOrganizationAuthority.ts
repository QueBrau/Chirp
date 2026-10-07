import { currentIdentity, ownsIdentity } from "@/auth/identity";
import { confirmAction, showApiError } from "./alert";

/** Bind an explicit confirmation to both the account generation and org form. */
export async function confirmOrganizationAuthority<T>({ load, current, describe, act }: {
  load: () => Promise<T>;
  current: () => boolean;
  describe: (context: T) => string;
  act: (context: T) => Promise<void>;
}): Promise<void> {
  const owner = currentIdentity();
  const context = await load();
  if (!ownsIdentity(owner) || !current()) return;
  let submitted = false;
  let action: Promise<void> | undefined;
  confirmAction({
    title: "Confirm your authority",
    message: describe(context),
    confirmLabel: "I confirm and continue",
    onConfirm: () => {
      if (submitted || !ownsIdentity(owner) || !current()) return;
      submitted = true;
      action = act(context).catch((error) => {
        if (ownsIdentity(owner) && current()) showApiError(error, "Couldn't confirm authority");
      });
    },
  });
  // Web confirms synchronously; wait for that action before caller cleanup.
  // Native confirms later and the action owns its own busy/error state.
  await action;
}
