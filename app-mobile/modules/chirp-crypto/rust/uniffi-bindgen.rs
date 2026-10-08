//! The uniffi bindings generator, built from the same pinned `uniffi` as the library.
//! Only compiled with `--features bindgen-cli` (see `scripts/build-ios.sh`).

fn main() {
    uniffi::uniffi_bindgen_main()
}
