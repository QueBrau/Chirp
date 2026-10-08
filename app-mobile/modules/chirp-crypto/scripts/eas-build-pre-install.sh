#!/usr/bin/env bash
# EAS Build hook (board c448): compile the Rust crypto core before the iOS project is made.
#
# UNVERIFIED. Written from the EAS docs, never run on EAS. M6 proves it with a real build.
#
# Why eas-build-pre-install and not eas-build-post-install: for an iOS build of a managed
# (prebuild) project, EAS runs `npm install`, `npx expo prebuild` and `pod install`, and ONLY
# THEN eas-build-post-install. The ChirpCrypto podspec needs the xcframework when
# `pod install` runs, so the post-install hook is too late. eas-build-pre-install runs before
# `npm install`, which is fine here: build-ios.sh needs Rust and Xcode, not node_modules.
# (docs.expo.dev/build-reference/npm-hooks, checked Oct 7 2026)
#
# EAS cloud machines have Xcode but no Rust, so this installs rustup and the iOS targets,
# then runs the same build-ios.sh developers run locally. The pinned toolchain comes from
# rust/rust-toolchain.toml, so the version is decided in one place.
#
# Wired in app-mobile/package.json:  "eas-build-pre-install": "bash modules/chirp-crypto/scripts/eas-build-pre-install.sh"

set -euo pipefail

if [ "${EAS_BUILD_PLATFORM:-ios}" != "ios" ]; then
  echo "eas-build-pre-install: platform is '${EAS_BUILD_PLATFORM}', the Rust core is iOS only; skipping"
  exit 0
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUST_DIR="$(cd "$HERE/../rust" && pwd)"

PINNED="$(sed -n 's/^channel *= *"\(.*\)"/\1/p' "$RUST_DIR/rust-toolchain.toml")"
if [ -z "$PINNED" ]; then
  echo "eas-build-pre-install: could not read the pinned channel from $RUST_DIR/rust-toolchain.toml" >&2
  exit 1
fi

export PATH="$HOME/.cargo/bin:$PATH"

if ! command -v rustup >/dev/null 2>&1; then
  echo "eas-build-pre-install: installing rustup"
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain none
fi

rustup toolchain install "$PINNED" --profile minimal
# The two simulator targets cover both Mac architectures; device is arm64.
rustup target add --toolchain "$PINNED" aarch64-apple-ios aarch64-apple-ios-sim x86_64-apple-ios

CHIRP_CRYPTO_SIM_ARCHS="arm64 x86_64" "$HERE/build-ios.sh"
