//! Trust: pinning, approval chains, hard stop, accepting a change, approving a device.

use super::common::*;
use crate::encoding::verify_device_approval;
use crate::*;

fn recipient(device: &Dev, claim: Option<SignedKey>) -> Recipient {
    Recipient {
        device: device.directory(),
        claimed_key: claim,
    }
}

fn encrypt_to(alice: &Dev, client_id: u8, recipients: &[Recipient]) -> Result<Vec<Leg>> {
    alice.core.encrypt(
        CONV,
        cmid(client_id),
        "hello",
        &alice.directory(),
        recipients,
    )
}

#[test]
fn first_contact_pins_and_an_unknown_peer_has_no_state() {
    let alice = Dev::new(0xa1, 1);
    let bob = Dev::new(0xb1, 2);
    assert_eq!(
        alice.core.trust_state(bob.user()).err(),
        Some(Error::UnknownPeer)
    );
    assert_eq!(
        alice.core.observe_directory(bob.user(), &[bob.directory()]),
        Ok(TrustState::Trusted)
    );
    assert_eq!(alice.core.trust_state(bob.user()), Ok(TrustState::Trusted));
    assert_eq!(
        alice.core.observe_directory(bob.user(), &[bob.directory()]),
        Ok(TrustState::Trusted),
        "observing again is stable"
    );
}

#[test]
fn a_root_and_approved_devices_pin_together_and_chains_of_approval_are_followed() {
    let alice = Dev::new(0xa1, 1);
    let b1 = Dev::new(0xb1, 2);
    let b2 = Dev::new(0xb1, 3).approved_by(&b1);
    let b3 = Dev::new(0xb1, 4).approved_by(&b2); // approved by a device that was itself approved
    assert_eq!(
        alice
            .core
            .observe_directory(b1.user(), &[b1.directory(), b2.directory()]),
        Ok(TrustState::Trusted)
    );
    assert_eq!(
        alice
            .core
            .observe_directory(b1.user(), &[b1.directory(), b2.directory(), b3.directory()]),
        Ok(TrustState::Trusted)
    );
    // Revoking the root is not a security event.
    assert_eq!(
        alice
            .core
            .observe_directory(b1.user(), &[b2.directory(), b3.directory()]),
        Ok(TrustState::Trusted)
    );
}

#[test]
fn first_contact_with_two_roots_or_no_root_is_unapproved() {
    let alice = Dev::new(0xa1, 1);
    let b1 = Dev::new(0xb1, 2);
    let b2 = Dev::new(0xb1, 3); // also self-approved: two roots
    assert_eq!(
        alice
            .core
            .observe_directory(b1.user(), &[b1.directory(), b2.directory()]),
        Ok(TrustState::UnapprovedDevice)
    );
    // No root at all (only an approved device whose approver is not listed).
    let c1 = Dev::new(0xc1, 4);
    let c2 = Dev::new(0xc1, 5).approved_by(&c1);
    assert_eq!(
        alice.core.observe_directory(c1.user(), &[c2.directory()]),
        Ok(TrustState::UnapprovedDevice)
    );
    // Nothing is pinned, so sending to it is blocked.
    assert_eq!(
        encrypt_to(&alice, 1, &[recipient(&c2, Some(c2.created.fallback_key))]).err(),
        Some(Error::TrustHardStop)
    );
}

#[test]
fn an_unapproved_new_device_is_a_hard_stop_until_it_is_properly_approved() {
    let mut alice = Dev::new(0xa1, 1);
    let mut b1 = Dev::new(0xb1, 2);
    let mut b2 = Dev::new(0xb1, 3).approved_by(&b1);
    let legs = alice.send(&mut [&mut b1, &mut b2], 1, "before").unwrap();
    b1.receive(&alice, &legs[0], 1, 1).unwrap();

    // A third device appears with NO approval.
    let rogue = Dev::new(0xb1, 4);
    let directory = [b1.directory(), b2.directory(), rogue.directory()];
    assert_eq!(
        alice.core.observe_directory(b1.user(), &directory),
        Ok(TrustState::UnapprovedDevice)
    );
    assert_eq!(
        alice.core.trust_state(b1.user()),
        Ok(TrustState::UnapprovedDevice)
    );

    // Hard stop: one fixed error, and no outbox row, message or session is written.
    let history = alice.core.list_messages(CONV, None, 10).unwrap().len();
    let all = [
        recipient(&b1, None),
        recipient(&b2, None),
        recipient(&rogue, Some(rogue.created.fallback_key)),
    ];
    assert_eq!(
        encrypt_to(&alice, 2, &all).err(),
        Some(Error::TrustHardStop)
    );
    assert_eq!(
        alice.core.list_messages(CONV, None, 10).unwrap().len(),
        history
    );

    // A garbage approval changes nothing.
    let mut garbage = rogue.directory();
    garbage.approval = Some(DeviceApproval {
        approver_ed25519: b2.identity().ed25519,
        signature: [7u8; 64],
    });
    assert_eq!(
        alice
            .core
            .observe_directory(b1.user(), &[b1.directory(), b2.directory(), garbage]),
        Ok(TrustState::UnapprovedDevice)
    );
    // A VALID approval from a device that is not pinned (an outsider of the same
    // account) changes nothing either.
    let outsider = Dev::new(0xb1, 8);
    let mut by_outsider = rogue.directory();
    by_outsider.approval = Some(
        outsider
            .core
            .approve_device(&rogue.new_device_binding())
            .unwrap(),
    );
    assert_eq!(
        alice
            .core
            .observe_directory(b1.user(), &[b1.directory(), b2.directory(), by_outsider]),
        Ok(TrustState::UnapprovedDevice)
    );

    // A real approval from a pinned, still-listed device clears it by itself.
    let approved = rogue.approved_by(&b2);
    assert_eq!(
        alice.core.observe_directory(
            b1.user(),
            &[b1.directory(), b2.directory(), approved.directory()]
        ),
        Ok(TrustState::Trusted)
    );
    assert_eq!(alice.core.trust_state(b1.user()), Ok(TrustState::Trusted));
}

#[test]
fn accepting_an_unapproved_device_unblocks_sending() {
    let mut alice = Dev::new(0xa1, 1);
    let mut b1 = Dev::new(0xb1, 2);
    alice.send(&mut [&mut b1], 1, "before").unwrap();
    let mut rogue = Dev::new(0xb1, 3); // self-approved root: a second root, never vouched for
    let claim = rogue.claim();
    let both =
        |rogue_claim: Option<SignedKey>| [recipient(&b1, None), recipient(&rogue, rogue_claim)];
    assert_eq!(
        encrypt_to(&alice, 2, &both(Some(claim))).err(),
        Some(Error::TrustHardStop)
    );
    assert_eq!(
        alice.core.trust_state(b1.user()),
        Ok(TrustState::UnapprovedDevice)
    );
    // The user verified it out of band and accepts.
    alice.core.accept_identity_change(b1.user()).unwrap();
    assert_eq!(alice.core.trust_state(b1.user()), Ok(TrustState::Trusted));
    assert_eq!(
        encrypt_to(&alice, 3, &both(Some(claim))).map(|l| l.len()),
        Ok(2)
    );
    // There is nothing left to accept, and an unseen peer cannot be accepted.
    assert_eq!(
        alice.core.accept_identity_change(b1.user()).err(),
        Some(Error::NothingToAccept)
    );
    assert_eq!(
        alice.core.accept_identity_change([0x44; 16]).err(),
        Some(Error::UnknownPeer)
    );
}

#[test]
fn an_identity_change_is_a_hard_stop_for_sending_but_not_for_receiving() {
    let mut alice = Dev::new(0xa1, 1);
    let mut bob = Dev::new(0xb1, 2);
    alice
        .core
        .observe_directory(bob.user(), &[bob.directory()])
        .unwrap();
    let legs = alice.send(&mut [&mut bob], 1, "hi bob").unwrap();
    bob.receive(&alice, &legs[0], 1, 1).unwrap();
    let r = bob.send(&mut [&mut alice], 2, "hi alice").unwrap();
    alice.receive(&bob, &r[0], 2, 2).unwrap();

    // The same device id comes back with brand new keys (a reinstall, or a substituted
    // directory entry).
    let mut impostor = Dev::new(0xb1, 2);
    assert_eq!(impostor.device_id, bob.device_id);
    assert_ne!(impostor.identity(), bob.identity());
    assert_eq!(
        alice
            .core
            .observe_directory(bob.user(), &[impostor.directory()]),
        Ok(TrustState::IdentityChanged)
    );
    assert_eq!(
        alice.core.trust_state(bob.user()),
        Ok(TrustState::IdentityChanged)
    );

    // Sending: blocked, with nothing written.
    let before = alice.core.list_messages(CONV, None, 10).unwrap().len();
    let claim = impostor.claim();
    assert_eq!(
        encrypt_to(&alice, 3, &[recipient(&impostor, Some(claim))]).err(),
        Some(Error::TrustHardStop)
    );
    assert_eq!(
        alice.core.list_messages(CONV, None, 10).unwrap().len(),
        before
    );

    // Receiving: the old device still decrypts as Trusted; the new identity decrypts but is
    // flagged IdentityChanged. Neither is silently accepted.
    let r = bob.send(&mut [&mut alice], 5, "still me").unwrap();
    let got = alice.receive(&bob, &r[0], 5, 5).unwrap();
    assert_eq!(got.sender_trust, TrustState::Trusted);
    let legs = impostor.send(&mut [&mut alice], 6, "new keys").unwrap();
    let got = alice.receive(&impostor, &legs[0], 6, 6).unwrap();
    assert_eq!(got.body, "new keys");
    assert_eq!(got.sender_trust, TrustState::IdentityChanged);
    assert_eq!(
        alice
            .core
            .list_messages(CONV, None, 10)
            .unwrap()
            .iter()
            .find(|m| m.body == "new keys")
            .map(|m| m.sender_trust),
        Some(TrustState::IdentityChanged),
        "the flag is stored with the message"
    );

    // The block follows the CURRENT directory, not a sticky flag: if the server shows the
    // pinned identity again, sending to exactly that identity is fine. Then it changes
    // again.
    assert_eq!(
        alice.core.observe_directory(bob.user(), &[bob.directory()]),
        Ok(TrustState::Trusted)
    );
    assert_eq!(
        alice
            .core
            .observe_directory(bob.user(), &[impostor.directory()]),
        Ok(TrustState::IdentityChanged)
    );

    // Accepting the change re-pins the new identity and sending works again.
    alice.core.accept_identity_change(bob.user()).unwrap();
    assert_eq!(alice.core.trust_state(bob.user()), Ok(TrustState::Trusted));
    assert_eq!(
        alice
            .core
            .observe_directory(bob.user(), &[impostor.directory()]),
        Ok(TrustState::Trusted)
    );
    assert!(encrypt_to(&alice, 7, &[recipient(&impostor, None)]).is_ok());
    // ... and now the ORIGINAL identity is the one that no longer matches.
    assert_eq!(
        alice.core.observe_directory(bob.user(), &[bob.directory()]),
        Ok(TrustState::IdentityChanged)
    );
}

#[test]
fn re_binding_a_pinned_identity_is_rejected_or_flagged_never_quietly_accepted() {
    // Same device id, same Ed25519 identity, but a different Curve25519 key (and the
    // reverse): both are identity changes, never a quiet re-pin.
    let alice = Dev::new(0xa1, 1);
    let bob = Dev::new(0xb1, 2);
    alice
        .core
        .observe_directory(bob.user(), &[bob.directory()])
        .unwrap();
    let other = Dev::new(0xb1, 3);
    let mut swapped_curve = bob.directory();
    swapped_curve.identity_curve25519 = other.identity().curve25519;
    // (The binding no longer verifies; the change is caught by signature first.)
    assert_eq!(
        alice
            .core
            .observe_directory(bob.user(), &[swapped_curve])
            .err(),
        Some(Error::InvalidSignature)
    );
    let mut new_generation = bob.directory();
    new_generation.generation = 2;
    assert_eq!(
        alice
            .core
            .observe_directory(bob.user(), &[new_generation])
            .err(),
        Some(Error::InvalidSignature)
    );
    // A different device id presenting an already pinned Ed25519 identity is also flagged.
    let mut relabeled = bob.directory();
    relabeled.device_id = [0x66; 16];
    assert_eq!(
        alice.core.observe_directory(bob.user(), &[relabeled]),
        Ok(TrustState::IdentityChanged)
    );
}

#[test]
fn trust_state_survives_a_restart() {
    let mut alice = Dev::new(0xa1, 1);
    let bob = Dev::new(0xb1, 2);
    let impostor = Dev::new(0xb1, 2);
    alice
        .core
        .observe_directory(bob.user(), &[bob.directory()])
        .unwrap();
    alice
        .core
        .observe_directory(bob.user(), &[impostor.directory()])
        .unwrap();
    alice.reopen();
    assert_eq!(
        alice.core.trust_state(bob.user()),
        Ok(TrustState::IdentityChanged)
    );
    alice.core.accept_identity_change(bob.user()).unwrap();
    alice.reopen();
    assert_eq!(alice.core.trust_state(bob.user()), Ok(TrustState::Trusted));
}

#[test]
fn a_revoked_device_cannot_vouch_for_a_new_one() {
    let alice = Dev::new(0xa1, 1);
    let b1 = Dev::new(0xb1, 2);
    let b2 = Dev::new(0xb1, 3).approved_by(&b1);
    alice
        .core
        .observe_directory(b1.user(), &[b1.directory(), b2.directory()])
        .unwrap();
    // b3 is approved by b1, but b1 is no longer listed (revoked): its signature cannot
    // admit new devices any more.
    let b3 = Dev::new(0xb1, 4).approved_by(&b1);
    assert_eq!(
        alice
            .core
            .observe_directory(b1.user(), &[b2.directory(), b3.directory()]),
        Ok(TrustState::UnapprovedDevice)
    );
    // The same device approved by a device that is still listed is fine.
    let b3 = Dev::new(0xb1, 4).approved_by(&b2);
    assert_eq!(
        alice
            .core
            .observe_directory(b1.user(), &[b2.directory(), b3.directory()]),
        Ok(TrustState::Trusted)
    );
}

#[test]
fn a_directory_cannot_empty_the_pins_or_mix_users() {
    let alice = Dev::new(0xa1, 1);
    let b1 = Dev::new(0xb1, 2);
    alice
        .core
        .observe_directory(b1.user(), &[b1.directory()])
        .unwrap();
    assert_eq!(
        alice.core.observe_directory(b1.user(), &[]).err(),
        Some(Error::InvalidInput)
    );
    let c1 = Dev::new(0xc1, 3);
    assert_eq!(
        alice
            .core
            .observe_directory(b1.user(), &[b1.directory(), c1.directory()])
            .err(),
        Some(Error::InvalidInput)
    );
    assert_eq!(
        alice
            .core
            .observe_directory(b1.user(), &[b1.directory(), b1.directory()])
            .err(),
        Some(Error::InvalidInput)
    );
    // A directory entry whose binding does not verify is an error, not a trust state.
    let mut forged = b1.directory();
    forged.binding_signature[10] ^= 1;
    assert_eq!(
        alice.core.observe_directory(b1.user(), &[forged]).err(),
        Some(Error::InvalidSignature)
    );
    // A new root cannot be slipped in by hiding the old one: nothing vouches for it.
    let fresh_root = Dev::new(0xb1, 9);
    assert_eq!(
        alice
            .core
            .observe_directory(b1.user(), &[fresh_root.directory()]),
        Ok(TrustState::UnapprovedDevice)
    );
    assert_eq!(
        alice.core.trust_state(b1.user()),
        Ok(TrustState::UnapprovedDevice)
    );
}

#[test]
fn the_senders_own_account_is_held_to_the_same_rules() {
    let mut a1 = Dev::new(0xa1, 1);
    let mut b1 = Dev::new(0xb1, 2);
    // A second device of Alice's that nothing vouches for appears in her own directory.
    let a_rogue = Dev::new(0xa1, 3);
    let b1_claim = b1.claim();
    let blocked = [
        recipient(&b1, Some(b1_claim)),
        recipient(&a_rogue, Some(a_rogue.created.fallback_key)),
    ];
    assert_eq!(
        encrypt_to(&a1, 1, &blocked).err(),
        Some(Error::TrustHardStop),
        "an unvouched device of our own account is a second root"
    );
    assert_eq!(
        a1.core.trust_state(a1.user()),
        Ok(TrustState::UnapprovedDevice)
    );
    // Properly approved by this device, a device of the same account is accepted.
    let mut a2 = Dev::new(0xa1, 3).approved_by(&a1);
    assert!(a1.send(&mut [&mut b1, &mut a2], 2, "ok").is_ok());
    assert_eq!(a1.core.trust_state(a1.user()), Ok(TrustState::Trusted));
}

#[test]
fn approve_device_signs_only_valid_bindings_of_our_own_account() {
    let a1 = Dev::new(0xa1, 1);
    let a2 = Dev::new(0xa1, 2);
    let nd = a2.new_device_binding();
    let approval = a1.core.approve_device(&nd).unwrap();
    assert_eq!(approval.approver_ed25519, a1.identity().ed25519);
    assert!(
        verify_device_approval(
            &approval.approver_ed25519,
            &nd.user_id,
            nd.generation,
            &nd.identity_curve25519,
            &nd.identity_ed25519,
            &approval.signature
        )
        .is_ok()
    );
    // Ed25519 is deterministic: the same device signs to the same bytes.
    assert_eq!(
        a1.core.approve_device(&nd).unwrap().signature,
        approval.signature
    );

    // The approval is bound to every field of the new device.
    let mutations: [fn(&mut NewDeviceBinding); 4] = [
        |n| n.generation += 1,
        |n| n.identity_curve25519[0] ^= 1,
        |n| n.identity_ed25519[0] ^= 1,
        |n| n.user_id[0] ^= 1,
    ];
    for mutate in mutations {
        let mut changed = nd;
        mutate(&mut changed);
        assert!(
            verify_device_approval(
                &approval.approver_ed25519,
                &changed.user_id,
                changed.generation,
                &changed.identity_curve25519,
                &changed.identity_ed25519,
                &approval.signature
            )
            .is_err()
        );
    }
    // Refusals.
    let other_user = Dev::new(0xb1, 3);
    assert_eq!(
        a1.core
            .approve_device(&other_user.new_device_binding())
            .err(),
        Some(Error::InvalidInput)
    );
    let mut forged = nd;
    forged.binding_signature[0] ^= 1;
    assert_eq!(
        a1.core.approve_device(&forged).err(),
        Some(Error::InvalidSignature)
    );
    assert_eq!(
        a1.core.approve_device(&a1.new_device_binding()).err(),
        Some(Error::InvalidInput),
        "a device does not approve itself"
    );
    let mut wrong_generation = nd;
    wrong_generation.generation = 9;
    assert_eq!(
        a1.core.approve_device(&wrong_generation).err(),
        Some(Error::InvalidSignature),
        "the binding signature covers the generation"
    );
}

#[test]
fn a_message_from_a_never_observed_user_is_flagged_unapproved() {
    let mut alice = Dev::new(0xa1, 1);
    let mut stranger = Dev::new(0xee, 9);
    let legs = stranger.send(&mut [&mut alice], 1, "who am i").unwrap();
    let got = alice.receive(&stranger, &legs[0], 1, 1).unwrap();
    assert_eq!(got.body, "who am i");
    assert_eq!(got.sender_trust, TrustState::UnapprovedDevice);
}
