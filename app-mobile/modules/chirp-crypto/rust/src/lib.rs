//! Chirp's end-to-end encrypted direct-message core (board c443, milestone M1).
//!
//! Host-only: no mobile bindings, no Expo module, no app or backend coupling. The
//! design is `E2EE-DESIGN.md` (c442) sections 2, 3, 6, 7 and 8; the requirements it
//! honors are in `spikes/vodozemac-native-boundary/PROPOSED-CONTRACT.md`.
//!
//! Layers, bottom to top:
//!  - [`encoding`]: the signed byte formats shared with the backend.
//!  - `store`: an encrypted, transactional, per-namespace SQLite store.
//!  - [`envelope`]: the authenticated inner message format and body limits.
//!  - `trust` and [`safety`]: pinning, approval chains, safety numbers.
//!  - [`Core`]: the API the platform layer will wrap.
//!
//! Private keys never leave the store; JavaScript will only ever see the public
//! values and committed plaintext in [`types`].

pub mod core;
pub mod encoding;
pub mod envelope;
pub mod error;
pub mod safety;
pub mod types;

mod records;
mod store;
mod trust;

#[cfg(test)]
mod tests;

pub use crate::core::{
    Core, MAX_KEYS_PER_CALL, MAX_LEG_BYTES, MAX_LIST_LIMIT, MAX_RECIPIENTS,
    MAX_STORED_ONE_TIME_KEYS,
};
pub use crate::error::{Error, Result};
pub use crate::safety::safety_number;
pub use crate::types::*;
