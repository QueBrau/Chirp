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
 * This runs during local `expo prebuild` (and therefore `expo run:ios`), BEFORE
 * `pod install`, so ios/ can stay generated and gitignored. It is idempotent and a
 * no-op on 0.61.0+ (header already fixed).
 *
 * It is LOCAL-ONLY ON PURPOSE and does nothing when EAS_BUILD is set. EAS builds
 * compile today, which means the EAS image's Xcode is older than 26.4. The same Xcode
 * 26.4 that turns this header into a compile error also has a PaymentSheetLoader.load
 * runtime crash with Stripe iOS 24.x (stripe-react-native#2364), so on EAS the compile
 * error is currently a free guard against shipping that crash. Patching it there would
 * remove the guard silently the day the EAS image moves to Xcode 26.4. Local builds
 * are dev-only (Xcode 26.6 here, where a PaymentSheet probe showed no crash in the
 * failure path), so the patch is safe to apply for them.
 *
 * The real fix is bumping @stripe/stripe-react-native to >= 0.61.0 (a separate,
 * payments-gated card); delete this plugin and its app.json entry when that lands.
 */
const fs = require("fs");
const path = require("path");
const { withDangerousMod } = require("expo/config-plugins");

const BAD = "typedef NS_ENUM(NSUInteger, STPPaymentStatus);";
const GOOD = "typedef NS_ENUM(NSInteger, STPPaymentStatus);";

let loggedEasSkip = false;

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
  if (process.env.EAS_BUILD) {
    // See the header comment: on EAS the unpatched compile error is the guard.
    // Expo evaluates plugins several times per prebuild, so log once per process.
    if (!loggedEasSkip) {
      loggedEasSkip = true;
      console.log("[withStripeSwiftInteropFix] EAS_BUILD set; leaving StripeSwiftInterop.h unpatched (local-only fix)");
    }
    return config;
  }
  return withDangerousMod(config, [
    "ios",
    (cfg) => {
      patchHeader(cfg.modRequest.projectRoot);
      return cfg;
    },
  ]);
};
