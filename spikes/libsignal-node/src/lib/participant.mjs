// A single simulated user+device: real backend registration + local libsignal state.
//
// WORKAROUND (see FINDINGS.md #1): @signalapp/libsignal-client's PreKeyBundle.new()
// requires a Kyber (PQXDH) prekey — kyber_prekey_id / kyber_prekey / kyber_prekey_signature
// are NOT nullable in this SDK version. The Chirp `signed_prekeys` / `one_time_prekeys`
// tables have no Kyber column at all, so the backend cannot round-trip a Kyber prekey.
// This spike generates one Kyber prekey per device locally and exchanges it out-of-band
// via `KYBER_DIRECTORY` below (in-process only, standing in for a backend column/endpoint
// that does not exist) so the X3DH exercise can proceed. Every other key (identity key,
// EC signed prekey + signature, EC one-time prekeys) is the real thing, registered with
// and fetched from the real backend over HTTP.

import * as Signal from '@signalapp/libsignal-client';
import { backend } from './backend.mjs';
import { makeStoreBundle } from './signal-stores.mjs';
import { toB64 } from './util.mjs';

/** device_row_id -> { record, publicKeyB64 } — stands in for a missing backend kyber directory. */
export const KYBER_DIRECTORY = new Map();

const KYBER_PREKEY_ID = 1;
const SIGNED_PREKEY_ID = 1;
const OTK_BATCH_SIZE = 10;

function randomRegistrationId() {
  // libsignal registration ids are 14-bit (1..16383).
  return 1 + Math.floor(Math.random() * 16382);
}

export class Participant {
  constructor(label, uid) {
    this.label = label;
    this.uid = uid;
    this.identityKeyPair = Signal.IdentityKeyPair.generate();
    this.registrationId = randomRegistrationId();
    this.stores = makeStoreBundle(this.identityKeyPair, this.registrationId);
    /** libsignal ProtocolAddress device id (1-127 range) — distinct from the backend's device UUID. */
    this.libsignalDeviceIndex = 1;
  }

  /** ProtocolAddress this participant is addressed BY, once backend user_id is known. */
  address() {
    return Signal.ProtocolAddress.new(this.userId, this.libsignalDeviceIndex);
  }

  async bootstrap({ email, accountType = 'greek' }) {
    const user = await backend.bootstrap(this.uid, { email, display_name: this.label, account_type: accountType });
    this.userId = user.id;
    this.user = user;
    return user;
  }

  /** Generate a signed prekey + OTK batch + a local-only Kyber prekey, register the real ones with backend. */
  async registerDevice() {
    const signedPriv = Signal.PrivateKey.generate();
    const signedPub = signedPriv.getPublicKey();
    const signedSignature = this.identityKeyPair.privateKey.sign(signedPub.serialize());
    await this.stores.signedPreKey.saveSignedPreKey(
      SIGNED_PREKEY_ID,
      Signal.SignedPreKeyRecord.new(SIGNED_PREKEY_ID, Date.now(), signedPub, signedPriv, signedSignature),
    );

    const otkPayload = [];
    for (let keyId = 1; keyId <= OTK_BATCH_SIZE; keyId += 1) {
      const priv = Signal.PrivateKey.generate();
      const pub = priv.getPublicKey();
      await this.stores.preKey.savePreKey(keyId, Signal.PreKeyRecord.new(keyId, pub, priv));
      otkPayload.push({ key_id: keyId, public_key_b64: toB64(pub.serialize()) });
    }

    const device = await backend.registerDevice(this.uid, {
      device_label: `${this.label}-spike-device`,
      registration_id: this.registrationId,
      identity_key_b64: toB64(this.identityKeyPair.publicKey.serialize()),
      signed_prekey: {
        key_id: SIGNED_PREKEY_ID,
        public_key_b64: toB64(signedPub.serialize()),
        signature_b64: toB64(signedSignature),
      },
      one_time_prekeys: otkPayload,
    });
    this.device = device;
    this.deviceId = device.id;

    // Local-only Kyber prekey (see module docstring) — never sent to the backend.
    const kyberKeyPair = Signal.KEMKeyPair.generate();
    const kyberSignature = this.identityKeyPair.privateKey.sign(kyberKeyPair.getPublicKey().serialize());
    const kyberRecord = Signal.KyberPreKeyRecord.new(KYBER_PREKEY_ID, Date.now(), kyberKeyPair, kyberSignature);
    await this.stores.kyberPreKey.saveKyberPreKey(KYBER_PREKEY_ID, kyberRecord);
    KYBER_DIRECTORY.set(device.id, {
      keyId: KYBER_PREKEY_ID,
      publicKey: kyberKeyPair.getPublicKey(),
      signature: kyberSignature,
    });

    return device;
  }

  /**
   * Fetch this participant's prekey bundle from the REAL backend (GET /users/{id}/prekey-bundle,
   * consumes one server-side OTK) and turn the first device entry into a libsignal PreKeyBundle,
   * patched with the local-only Kyber prekey (see module docstring).
   */
  static async fetchPreKeyBundleFor(fetcherUid, targetUserId) {
    const bundle = await backend.prekeyBundle(fetcherUid, targetUserId);
    if (bundle.devices.length === 0) {
      throw new Error(`prekey-bundle: user ${targetUserId} has no usable devices`);
    }
    const deviceBundle = bundle.devices[0];
    const kyber = KYBER_DIRECTORY.get(deviceBundle.device_id);
    if (!kyber) {
      throw new Error(`no local Kyber prekey registered for device ${deviceBundle.device_id}`);
    }

    const identityKey = Signal.PublicKey.deserialize(Buffer.from(deviceBundle.identity_key_b64, 'base64'));
    const signedPub = Signal.PublicKey.deserialize(
      Buffer.from(deviceBundle.signed_prekey.public_key_b64, 'base64'),
    );
    const signedSig = Buffer.from(deviceBundle.signed_prekey.signature_b64, 'base64');
    const otk = deviceBundle.one_time_prekey;
    const otkId = otk ? otk.key_id : null;
    const otkPub = otk ? Signal.PublicKey.deserialize(Buffer.from(otk.public_key_b64, 'base64')) : null;

    const preKeyBundle = Signal.PreKeyBundle.new(
      deviceBundle.registration_id,
      1, // libsignal device index for the recipient (see Participant.libsignalDeviceIndex)
      otkId,
      otkPub,
      deviceBundle.signed_prekey.key_id,
      signedPub,
      signedSig,
      identityKey,
      kyber.keyId,
      kyber.publicKey,
      kyber.signature,
    );
    return { preKeyBundle, deviceBundle, raw: bundle };
  }

  async currentOtkCount() {
    const out = await backend.prekeyCount(this.uid, this.deviceId);
    return out.one_time_prekeys_available;
  }
}
