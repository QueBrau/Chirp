#!/usr/bin/env bash
# Build the Rust crypto core for iOS (board c448, milestone M3).
#
#   app-mobile/modules/chirp-crypto/scripts/build-ios.sh
#
# Produces, under app-mobile/modules/chirp-crypto/ios/:
#   Frameworks/ChirpCryptoCore.xcframework   device (arm64) + simulator slices
#   Generated/chirp_crypto_core.swift        uniffi's Swift bindings
# Both are GITIGNORED build artifacts. The ChirpCrypto podspec refuses to resolve
# until they exist, so `pod install` (and therefore `expo prebuild` / an EAS build)
# fails loudly instead of building an app that cannot link.
#
# Idempotent: when the Rust sources, lockfile, this script and the settings below are
# unchanged and the outputs exist, it prints "up to date" and exits. CHIRP_CRYPTO_FORCE=1
# rebuilds anyway. Any failure stops the script (set -e) with a message that says what to do.
#
# Settings (environment):
#   CHIRP_CRYPTO_SIM_ARCHS          simulator slices, default "arm64"; "arm64 x86_64" for a
#                                   universal simulator slice (needs the x86_64-apple-ios target)
#   CHIRP_IOS_DEPLOYMENT_TARGET     minimum iOS version, default 15.1 (Expo SDK 54's)
#   CHIRP_CRYPTO_CARGO_TARGET_DIR   cargo output dir; defaults to $CARGO_TARGET_DIR, else a
#                                   cache dir OUTSIDE the repo (Metro would crawl a target/
#                                   dir inside the project)
#   CHIRP_CRYPTO_FORCE=1            ignore the up-to-date stamp

set -euo pipefail

die() {
  echo "build-ios.sh: $*" >&2
  exit 1
}

if [ "$(uname -s)" != "Darwin" ]; then
  die "this builds iOS artifacts and needs macOS with Xcode (found $(uname -s))"
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODULE_DIR="$(cd "$HERE/.." && pwd)"
RUST_DIR="$MODULE_DIR/rust"
OUT_FRAMEWORKS="$MODULE_DIR/ios/Frameworks"
OUT_GENERATED="$MODULE_DIR/ios/Generated"
XCFRAMEWORK="$OUT_FRAMEWORKS/ChirpCryptoCore.xcframework"
SWIFT_OUT="$OUT_GENERATED/chirp_crypto_core.swift"
STAMP="$OUT_FRAMEWORKS/.build-stamp"

LIB_NAME="libchirp_crypto_core.a"
DEVICE_TARGET="aarch64-apple-ios"
SIM_ARCHS="${CHIRP_CRYPTO_SIM_ARCHS:-arm64}"
export IPHONEOS_DEPLOYMENT_TARGET="${CHIRP_IOS_DEPLOYMENT_TARGET:-15.1}"

SIM_TARGETS=()
for arch in $SIM_ARCHS; do
  case "$arch" in
    arm64) SIM_TARGETS+=("aarch64-apple-ios-sim") ;;
    x86_64) SIM_TARGETS+=("x86_64-apple-ios") ;;
    *) die "CHIRP_CRYPTO_SIM_ARCHS: unknown architecture '$arch' (use arm64 and/or x86_64)" ;;
  esac
done
[ "${#SIM_TARGETS[@]}" -gt 0 ] || die "CHIRP_CRYPTO_SIM_ARCHS is empty"

# ---- tooling ----------------------------------------------------------------------------

command -v rustup >/dev/null 2>&1 \
  || die "rustup not found. Install it (brew install rustup, or https://rustup.rs) and re-run."
command -v cargo >/dev/null 2>&1 \
  || die "cargo not found on PATH. With Homebrew rustup: export PATH=\"/opt/homebrew/opt/rustup/bin:\$PATH\""
command -v xcrun >/dev/null 2>&1 && command -v xcodebuild >/dev/null 2>&1 \
  || die "Xcode command line tools not found (xcrun / xcodebuild). Install Xcode."
xcrun --sdk iphoneos --show-sdk-path >/dev/null 2>&1 \
  || die "the iphoneos SDK is not available. Install Xcode and run: sudo xcode-select -s /Applications/Xcode.app"
xcrun --sdk iphonesimulator --show-sdk-path >/dev/null 2>&1 \
  || die "the iphonesimulator SDK is not available. Install Xcode's iOS Simulator support."

# Every cargo/rustc call below runs inside rust/, where rust-toolchain.toml pins the
# toolchain. If rustup is not the one answering (a Homebrew `rust` earlier on PATH), the
# pin is ignored and the build would silently use another compiler, so check it.
cd "$RUST_DIR"
PINNED="$(sed -n 's/^channel *= *"\(.*\)"/\1/p' rust-toolchain.toml)"
[ -n "$PINNED" ] || die "could not read the pinned channel from $RUST_DIR/rust-toolchain.toml"
ACTIVE="$(rustc --version 2>/dev/null || true)"
case "$ACTIVE" in
  "rustc $PINNED "*) ;;
  *) die "expected rustc $PINNED (rust/rust-toolchain.toml) but got '${ACTIVE:-no rustc}'. Install it with: rustup toolchain install $PINNED --profile minimal" ;;
esac

INSTALLED="$(rustup target list --installed 2>/dev/null || true)"
MISSING=()
for target in "$DEVICE_TARGET" "${SIM_TARGETS[@]}"; do
  if ! printf '%s\n' "$INSTALLED" | grep -qx "$target"; then
    MISSING+=("$target")
  fi
done
if [ "${#MISSING[@]}" -gt 0 ]; then
  die "missing Rust target(s) for $PINNED: ${MISSING[*]}. Install with: (cd app-mobile/modules/chirp-crypto/rust && rustup target add ${MISSING[*]})"
fi

# ---- up-to-date check ---------------------------------------------------------------------

inputs_hash() {
  {
    echo "sim=$SIM_ARCHS deploy=$IPHONEOS_DEPLOYMENT_TARGET rustc=$ACTIVE"
    (cd "$MODULE_DIR" && find rust/src rust/Cargo.toml rust/Cargo.lock rust/uniffi-bindgen.rs \
      rust/rust-toolchain.toml scripts/build-ios.sh -type f | LC_ALL=C sort \
      | while read -r f; do shasum -a 256 "$f"; done)
  } | shasum -a 256 | cut -d' ' -f1
}

WANT="$(inputs_hash)"
if [ "${CHIRP_CRYPTO_FORCE:-0}" != "1" ] && [ -f "$STAMP" ] && [ -d "$XCFRAMEWORK" ] \
  && [ -f "$SWIFT_OUT" ] && [ "$(cat "$STAMP")" = "$WANT" ]; then
  echo "build-ios.sh: up to date ($XCFRAMEWORK)"
  exit 0
fi

# ---- build --------------------------------------------------------------------------------

if [ -n "${CHIRP_CRYPTO_CARGO_TARGET_DIR:-}" ]; then
  export CARGO_TARGET_DIR="$CHIRP_CRYPTO_CARGO_TARGET_DIR"
elif [ -z "${CARGO_TARGET_DIR:-}" ]; then
  export CARGO_TARGET_DIR="${XDG_CACHE_HOME:-$HOME/Library/Caches}/chirp-crypto/cargo-target"
fi
mkdir -p "$CARGO_TARGET_DIR"
STAGE="$CARGO_TARGET_DIR/chirp-ios-stage"
rm -rf "$STAGE"
mkdir -p "$STAGE/sim" "$STAGE/Headers" "$STAGE/bindings"

echo "build-ios.sh: $ACTIVE, iOS >= $IPHONEOS_DEPLOYMENT_TARGET, simulator: $SIM_ARCHS"

for target in "$DEVICE_TARGET" "${SIM_TARGETS[@]}"; do
  echo "build-ios.sh: cargo build --release --target $target"
  cargo build --release --locked --lib --target "$target"
  [ -f "$CARGO_TARGET_DIR/$target/release/$LIB_NAME" ] \
    || die "cargo reported success but $CARGO_TARGET_DIR/$target/release/$LIB_NAME is missing"
done

# Simulator slice: one library, fat when both architectures were requested.
SIM_LIBS=()
for target in "${SIM_TARGETS[@]}"; do
  SIM_LIBS+=("$CARGO_TARGET_DIR/$target/release/$LIB_NAME")
done
if [ "${#SIM_LIBS[@]}" -eq 1 ]; then
  cp "${SIM_LIBS[0]}" "$STAGE/sim/$LIB_NAME"
else
  lipo -create "${SIM_LIBS[@]}" -output "$STAGE/sim/$LIB_NAME"
fi

# The bindings generator is the same pinned uniffi as the library (see Cargo.toml). It is
# a build tool for this machine, so it builds for the host in the dev profile.
echo "build-ios.sh: building uniffi-bindgen"
cargo build --locked --features bindgen-cli --bin uniffi-bindgen
BINDGEN="$CARGO_TARGET_DIR/debug/uniffi-bindgen"
[ -x "$BINDGEN" ] || die "uniffi-bindgen was not produced at $BINDGEN"

echo "build-ios.sh: generating Swift bindings"
"$BINDGEN" generate "$CARGO_TARGET_DIR/$DEVICE_TARGET/release/$LIB_NAME" \
  --language swift --no-format --out-dir "$STAGE/bindings"
for f in chirp_crypto_core.swift chirp_crypto_coreFFI.h chirp_crypto_coreFFI.modulemap; do
  [ -s "$STAGE/bindings/$f" ] || die "uniffi-bindgen did not produce $f"
done

# An xcframework wants the header and a module map literally named module.modulemap.
cp "$STAGE/bindings/chirp_crypto_coreFFI.h" "$STAGE/Headers/"
cp "$STAGE/bindings/chirp_crypto_coreFFI.modulemap" "$STAGE/Headers/module.modulemap"

# ---- assemble -----------------------------------------------------------------------------

mkdir -p "$OUT_FRAMEWORKS" "$OUT_GENERATED"
rm -rf "$XCFRAMEWORK"
rm -f "$STAMP"
xcodebuild -create-xcframework \
  -library "$CARGO_TARGET_DIR/$DEVICE_TARGET/release/$LIB_NAME" -headers "$STAGE/Headers" \
  -library "$STAGE/sim/$LIB_NAME" -headers "$STAGE/Headers" \
  -output "$XCFRAMEWORK"
[ -f "$XCFRAMEWORK/Info.plist" ] || die "xcodebuild -create-xcframework did not produce $XCFRAMEWORK"

# Swift: replace atomically so a half-written file is never what Xcode compiles.
cp "$STAGE/bindings/chirp_crypto_core.swift" "$SWIFT_OUT.tmp"
mv "$SWIFT_OUT.tmp" "$SWIFT_OUT"

echo "$WANT" > "$STAMP"
rm -rf "$STAGE"
echo "build-ios.sh: wrote $XCFRAMEWORK and $SWIFT_OUT"
