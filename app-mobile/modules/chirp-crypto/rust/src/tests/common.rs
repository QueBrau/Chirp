//! Shared test fixtures: a throwaway directory, and `Dev`, a device with its own store
//! that also plays "the server" for its own published keys (key claiming).

use std::collections::HashSet;
use std::path::{Path, PathBuf};

use crate::*;

pub struct TestDir(PathBuf);

impl TestDir {
    pub fn new() -> TestDir {
        let mut random = [0u8; 8];
        getrandom::fill(&mut random).unwrap();
        let path = std::env::temp_dir().join(format!(
            "chirp-crypto-core-test-{}",
            crate::envelope::hex(&random)
        ));
        std::fs::create_dir_all(&path).unwrap();
        TestDir(path)
    }

    pub fn path(&self) -> &Path {
        &self.0
    }
}

impl Drop for TestDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

pub fn store_key(n: u8) -> [u8; 32] {
    [n; 32]
}

pub const CONV: ConversationId = [0xc0; 16];

pub fn cmid(n: u8) -> ClientMessageId {
    [n; 16]
}

pub fn msg_id(n: u8) -> MessageId {
    [0xe0 ^ n; 16]
}

pub struct Dev {
    pub dir: TestDir,
    pub core: Core,
    pub ns: Namespace,
    pub device_id: DeviceId,
    pub key: [u8; 32],
    pub created: CreatedDevice,
    pub approval: Option<DeviceApproval>,
    /// Published, unclaimed one-time keys (what the "server" would hold for us).
    pub claimable: Vec<SignedKey>,
    /// Devices this one has already claimed a key for (i.e. has a session with).
    pub established: HashSet<DeviceId>,
}

impl Dev {
    /// A new device of `user` (user id is 16 copies of `user`). It starts as a self-approved
    /// root; use `approved_by` to make it a later device.
    pub fn new(user: u8, device: u8) -> Dev {
        Dev::with_otks(user, device, 10)
    }

    pub fn with_otks(user: u8, device: u8, otks: usize) -> Dev {
        let dir = TestDir::new();
        let ns = Namespace {
            user_id: [user; 16],
            generation: 1,
        };
        let key = store_key(device);
        let core = Core::unlock(dir.path(), ns, &key).unwrap();
        let created = core.create_device(ns.user_id, ns.generation, otks).unwrap();
        let device_id = [device; 16];
        let mut ids: Vec<u64> = created.one_time_keys.iter().map(|k| k.key_id).collect();
        ids.push(created.fallback_key.key_id);
        core.mark_published(device_id, &ids).unwrap();
        Dev {
            dir,
            core,
            ns,
            device_id,
            key,
            claimable: created.one_time_keys.clone(),
            created,
            approval: None,
            established: HashSet::new(),
        }
    }

    pub fn user(&self) -> UserId {
        self.ns.user_id
    }

    pub fn identity(&self) -> PublicIdentity {
        self.created.identity
    }

    pub fn directory(&self) -> DirectoryDevice {
        DirectoryDevice {
            user_id: self.ns.user_id,
            device_id: self.device_id,
            generation: self.ns.generation,
            identity_curve25519: self.created.identity.curve25519,
            identity_ed25519: self.created.identity.ed25519,
            binding_signature: self.created.binding_signature,
            approval: self.approval,
        }
    }

    pub fn new_device_binding(&self) -> NewDeviceBinding {
        NewDeviceBinding {
            user_id: self.ns.user_id,
            generation: self.ns.generation,
            identity_curve25519: self.created.identity.curve25519,
            identity_ed25519: self.created.identity.ed25519,
            binding_signature: self.created.binding_signature,
        }
    }

    /// Make this device a non-root device vouched for by `approver`.
    pub fn approved_by(mut self, approver: &Dev) -> Dev {
        self.approval = Some(
            approver
                .core
                .approve_device(&self.new_device_binding())
                .unwrap(),
        );
        self
    }

    /// What the server's claim endpoint would hand a sender: a one-time key while any
    /// remain, otherwise the fallback key.
    pub fn claim(&mut self) -> SignedKey {
        if self.claimable.is_empty() {
            self.created.fallback_key
        } else {
            self.claimable.remove(0)
        }
    }

    /// Recipient entry for `self`, claiming a key only if `from` has no session yet.
    pub fn recipient_for(&mut self, from: &Dev) -> Recipient {
        let first = !from.established.contains(&self.device_id);
        Recipient {
            device: self.directory(),
            claimed_key: if first { Some(self.claim()) } else { None },
        }
    }

    /// Send `body` from `self` to `targets` (the real call, claiming keys as needed).
    /// Sessions are only recorded as established if the call succeeded.
    pub fn send(
        &mut self,
        targets: &mut [&mut Dev],
        client_id: u8,
        body: &str,
    ) -> Result<Vec<Leg>> {
        let recipients: Vec<Recipient> =
            targets.iter_mut().map(|t| t.recipient_for(self)).collect();
        let legs =
            self.core
                .encrypt(CONV, cmid(client_id), body, &self.directory(), &recipients)?;
        for target in targets.iter() {
            self.established.insert(target.device_id);
        }
        Ok(legs)
    }

    pub fn expected(&self, from: &Dev, client_id: u8) -> ExpectedEnvelope {
        ExpectedEnvelope {
            conversation_id: CONV,
            client_message_id: cmid(client_id),
            sender_user_id: from.user(),
            sender_device_id: from.device_id,
            recipient_user_id: self.user(),
            recipient_device_id: self.device_id,
        }
    }

    pub fn receive(
        &mut self,
        from: &Dev,
        leg: &Leg,
        client_id: u8,
        server_id: u8,
    ) -> Result<PlaintextMessage> {
        let result = self.core.decrypt(
            msg_id(server_id),
            leg,
            &from.directory(),
            &self.expected(from, client_id),
        );
        if result.is_ok() {
            // We now hold a session with the sender, so we never need its keys.
            self.established.insert(from.device_id);
        }
        result
    }

    /// Drop the handle and unlock the same store again (what an app restart does).
    pub fn reopen(&mut self) {
        self.core.lock();
        self.core = Core::unlock(self.dir.path(), self.ns, &self.key).unwrap();
    }

    pub fn leg_for<'a>(&self, legs: &'a [Leg]) -> &'a Leg {
        legs.iter()
            .find(|l| l.recipient_device_id == self.device_id)
            .expect("leg for device")
    }
}
