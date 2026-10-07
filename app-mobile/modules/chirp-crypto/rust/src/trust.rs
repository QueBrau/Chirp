//! Trust evaluation (design section 8): pure functions over pins and a verified
//! device directory. No I/O, no clock, no randomness.
//!
//! Model, per user:
//!  - Pins are the set of device identities we trust. A pin is
//!    `(device_id, ed25519, curve25519, generation)` plus who approved it.
//!  - FIRST CONTACT (no pins): the directory must contain exactly one self-approved
//!    "root" device, and every other device must be approved, through a chain of valid
//!    approval signatures, by that root. Then every device is pinned (TOFU). Anything
//!    else (no root, two roots, an unvouched device) is `UnapprovedDevice` and pins
//!    nothing until the user accepts it.
//!  - AFTER THAT: a device that matches a pin exactly is trusted. A device that shares
//!    a device id, an Ed25519 key or a Curve25519 key with a pin but is not identical
//!    is `IdentityChanged` (this wins over everything). A device unknown to us must
//!    carry an approval signed by a device we already trust AND that is still listed in
//!    the directory (a revoked approver cannot vouch for anyone new), directly or
//!    through a chain of such newly approved devices; otherwise `UnapprovedDevice`.
//!  - Devices that disappear from the directory simply stop being pinned (revocation
//!    is not a security event). The pin set is never emptied by an empty directory.

use crate::encoding::verify_device_approval;
use crate::error::{Error, Result};
use crate::records::PinnedDevice;
use crate::types::{DirectoryDevice, TrustState};

pub(crate) fn pin_of(device: &DirectoryDevice) -> PinnedDevice {
    PinnedDevice {
        device_id: device.device_id,
        ed25519: device.identity_ed25519,
        curve25519: device.identity_curve25519,
        generation: device.generation,
        approver: device.approval.map(|a| a.approver_ed25519),
    }
}

fn matches(pin: &PinnedDevice, device: &DirectoryDevice) -> bool {
    pin.device_id == device.device_id
        && pin.ed25519 == device.identity_ed25519
        && pin.curve25519 == device.identity_curve25519
        && pin.generation == device.generation
}

fn conflicts(pin: &PinnedDevice, device: &DirectoryDevice) -> bool {
    !matches(pin, device)
        && (pin.device_id == device.device_id
            || pin.ed25519 == device.identity_ed25519
            || pin.curve25519 == device.identity_curve25519)
}

/// Grow `accepted` with every device in `candidates` whose approval verifies against an
/// already accepted device, until nothing more can be added. Returns what was accepted.
fn expand<'a>(
    mut accepted: Vec<&'a DirectoryDevice>,
    mut candidates: Vec<&'a DirectoryDevice>,
) -> (Vec<&'a DirectoryDevice>, Vec<&'a DirectoryDevice>) {
    loop {
        let mut progressed = false;
        let mut rest = Vec::new();
        for candidate in candidates {
            let approved = candidate.approval.is_some_and(|approval| {
                accepted
                    .iter()
                    .any(|a| a.identity_ed25519 == approval.approver_ed25519)
                    && verify_device_approval(
                        &approval.approver_ed25519,
                        &candidate.user_id,
                        candidate.generation,
                        &candidate.identity_curve25519,
                        &candidate.identity_ed25519,
                        &approval.signature,
                    )
                    .is_ok()
            });
            if approved {
                accepted.push(candidate);
                progressed = true;
            } else {
                rest.push(candidate);
            }
        }
        candidates = rest;
        if !progressed || candidates.is_empty() {
            return (accepted, candidates);
        }
    }
}

/// Evaluate a user's current directory against our pins. The directory entries must
/// already have had their binding signatures verified. On `Trusted` the second value
/// is the new pin set (exactly the directory); otherwise it is empty and the caller
/// keeps the old pins.
pub(crate) fn evaluate(
    pins: &[PinnedDevice],
    observed: &[DirectoryDevice],
) -> Result<(TrustState, Vec<PinnedDevice>)> {
    if observed.is_empty() {
        return Err(Error::InvalidInput);
    }
    for (i, a) in observed.iter().enumerate() {
        for b in &observed[i + 1..] {
            if a.device_id == b.device_id
                || a.identity_ed25519 == b.identity_ed25519
                || a.identity_curve25519 == b.identity_curve25519
            {
                return Err(Error::InvalidInput);
            }
        }
        if a.user_id != observed[0].user_id {
            return Err(Error::InvalidInput);
        }
    }
    let all_pins = || observed.iter().map(pin_of).collect::<Vec<_>>();

    if pins.is_empty() {
        let roots: Vec<&DirectoryDevice> =
            observed.iter().filter(|d| d.approval.is_none()).collect();
        if roots.len() != 1 {
            return Ok((TrustState::UnapprovedDevice, Vec::new()));
        }
        let others: Vec<&DirectoryDevice> =
            observed.iter().filter(|d| d.approval.is_some()).collect();
        let (_, left) = expand(roots, others);
        return Ok(if left.is_empty() {
            (TrustState::Trusted, all_pins())
        } else {
            (TrustState::UnapprovedDevice, Vec::new())
        });
    }

    if observed
        .iter()
        .any(|d| pins.iter().any(|p| conflicts(p, d)))
    {
        return Ok((TrustState::IdentityChanged, Vec::new()));
    }
    let (known, unknown): (Vec<&DirectoryDevice>, Vec<&DirectoryDevice>) = observed
        .iter()
        .partition(|d| pins.iter().any(|p| matches(p, d)));
    let (_, left) = expand(known, unknown);
    Ok(if left.is_empty() {
        (TrustState::Trusted, all_pins())
    } else {
        (TrustState::UnapprovedDevice, Vec::new())
    })
}

/// How a message sender stands against our pins. Never blocks receiving.
pub(crate) fn sender_standing(pins: &[PinnedDevice], sender: &DirectoryDevice) -> TrustState {
    if pins.iter().any(|p| matches(p, sender)) {
        TrustState::Trusted
    } else if pins.iter().any(|p| conflicts(p, sender)) {
        TrustState::IdentityChanged
    } else {
        // Includes a user we have never observed: nothing vouches for this device yet.
        TrustState::UnapprovedDevice
    }
}
