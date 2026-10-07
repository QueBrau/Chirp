/**
 * Expo config plugin (c441): make @stripe/stripe-react-native 0.50.3 compile under
 * Xcode 26.4+ (clang 21).
 *
 * The bug. ios/StripeSwiftInterop.h forward-declares
 *     typedef NS_ENUM(NSUInteger, STPPaymentStatus);
 * but STPPaymentStatus is a Swift `@objc public enum STPPaymentStatus: Int` in the
 * Stripe iOS SDK, which the generated stripe_react_native-Swift.h correctly emits as
 * NSInteger. Older clang tolerated the mismatch; clang 21 rejects it as a hard error
 * ("enumeration redeclared with different underlying type"), with no warning flag to
 * downgrade it, so it can only be fixed in the header. Upstream fixed the header in
 * @stripe/stripe-react-native 0.61.0 (stripe/stripe-react-native#2357, #2355); Expo
 * SDK 54 still pins 0.50.3, so we patch the one line at prebuild time instead of
 * bumping the payments SDK.
 *
 * This runs during `expo prebuild` (and therefore `expo run:ios` and EAS builds),
 * BEFORE `pod install`, so ios/ can stay generated and gitignored. It is idempotent
 * and a no-op on 0.61.0+ (header already fixed). Switching the declaration to the
 * type Swift actually uses is also correct on older Xcode, so it cannot regress EAS.
 *
 * Delete this plugin (and its app.json entry) when @stripe/stripe-react-native is
 * bumped to >= 0.61.0.
 */
const fs = require("fs");
const path = require("path");
const { withDangerousMod } = require("expo/config-plugins");

const BAD = "typedef NS_ENUM(NSUInteger, STPPaymentStatus);";
const GOOD = "typedef NS_ENUM(NSInteger, STPPaymentStatus);";

function patchHeader(projectRoot) {
  let pkgJson;
  try {
    pkgJson = require.resolve("@stripe/stripe-react-native/package.json", {
      paths: [projectRoot],
    });
  } catch {
    // Stripe is not installed in this project; nothing to patch.
    return;
  }
  const header = path.join(path.dirname(pkgJson), "ios", "StripeSwiftInterop.h");
  if (!fs.existsSync(header)) {
    console.warn(`[withStripeSwiftInteropFix] ${header} not found; skipping`);
    return;
  }
  const src = fs.readFileSync(header, "utf8");
  if (src.includes(GOOD)) return; // already patched, or fixed upstream
  if (!src.includes(BAD)) {
    // Neither form is present: upstream restructured the header. Say so loudly
    // rather than silently shipping an unpatched build.
    console.warn(
      "[withStripeSwiftInteropFix] STPPaymentStatus declaration not found in " +
        "StripeSwiftInterop.h; the Xcode 26.4+ fix was NOT applied. If Stripe was " +
        "upgraded to >= 0.61.0, remove this plugin from app.json.",
    );
    return;
  }
  fs.writeFileSync(header, src.replace(BAD, GOOD));
  console.log("[withStripeSwiftInteropFix] patched STPPaymentStatus to NSInteger in StripeSwiftInterop.h");
}

module.exports = function withStripeSwiftInteropFix(config) {
  return withDangerousMod(config, [
    "ios",
    (cfg) => {
      patchHeader(cfg.modRequest.projectRoot);
      return cfg;
    },
  ]);
};
