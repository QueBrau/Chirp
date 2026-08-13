// A single simulated user+device: real backend registration + local libsignal state.
//
// FINDINGS.md #1 RESOLVED: the backend now has a `kyber_prekeys` table and accepts
// `kyber_last_resort` / `kyber_one_time` on POST /devices and POST /devices/{id}/prekeys,
// returning `kyber_prekey` on GET /users/{id}/prekey-bundle (a one-time Kyber prekey is
// consumed atomically when the pool has one; once exhausted, the signed last-resort Kyber
// prekey is returned WITHOUT being consumed). This spike now generates real Kyber
// keypairs (`Signal.KEMKeyPair.generate()`), uploads them to the real backend exactly
// like every other key, and builds `PreKeyBundle` entirely from server-returned values —
// no local Kyber directory, no workaround.

import * as Signal from '@signalapp/libsignal-client';
import { backend } from './backend.mjs';
import { makeStoreBundle } from './signal-stores.mjs';
import { toB64 } from './util.mjs';

const KYBER_LAST_RESORT_ID = 1;
const KYBER_OTK_BATCH_SIZE = 5;
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

  /**
   * Generate a signed prekey + EC OTK batch + a signed last-resort Kyber prekey + a
   * one-time Kyber batch, and register ALL of them with the real backend. Every private
   * half (EC and Kyber alike) is kept locally in this participant's stores, keyed by the
   * same key_id the server will later hand back in a prekey bundle.
   */
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

    // Signed last-resort Kyber prekey (real KEM keypair, never rotated away in this spike).
    const kyberLastResortPair = Signal.KEMKeyPair.generate();
    const kyberLastResortSignature = this.identityKeyPair.privateKey.sign(
      kyberLastResortPair.getPublicKey().serialize(),
    );
    await this.stores.kyberPreKey.saveKyberPreKey(
      KYBER_LAST_RESORT_ID,
      Signal.KyberPreKeyRecord.new(KYBER_LAST_RESORT_ID, Date.now(), kyberLastResortPair, kyberLastResortSignature),
    );

    // One-time Kyber batch — distinct key ids from the last-resort slot.
    const kyberOtkPayload = [];
    for (let i = 0; i < KYBER_OTK_BATCH_SIZE; i += 1) {
      const keyId = KYBER_LAST_RESORT_ID + 1 + i;
      const keyPair = Signal.KEMKeyPair.generate();
      const signature = this.identityKeyPair.privateKey.sign(keyPair.getPublicKey().serialize());
      await this.stores.kyberPreKey.saveKyberPreKey(
        keyId,
        Signal.KyberPreKeyRecord.new(keyId, Date.now(), keyPair, signature),
      );
      kyberOtkPayload.push({
        key_id: keyId,
        public_key_b64: toB64(keyPair.getPublicKey().serialize()),
        signature_b64: toB64(signature),
      });
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
      kyber_last_resort: {
        key_id: KYBER_LAST_RESORT_ID,
        public_key_b64: toB64(kyberLastResortPair.getPublicKey().serialize()),
        signature_b64: toB64(kyberLastResortSignature),
      },
      kyber_one_time: kyberOtkPayload,
    });
    this.device = device;
    this.deviceId = device.id;

    return device;
  }

  /**
   * Fetch this participant's prekey bundle from the REAL backend (GET /users/{id}/prekey-bundle,
   * consumes one server-side EC OTK and one server-side one-time Kyber prekey — or the
   * last-resort Kyber prekey once the one-time pool is exhausted) and turn the response
   * into a libsignal PreKeyBundle, built entirely from server-returned values.
   */
  static async fetchPreKeyBundleFor(fetcherUid, targetUserId) {
    const bundle = await backend.prekeyBundle(fetcherUid, targetUserId);
    if (bundle.devices.length === 0) {
      throw new Error(`prekey-bundle: user ${targetUserId} has no usable devices`);
    }
    const deviceBundle = bundle.devices[0];
    const kyber = deviceBundle.kyber_prekey;
    if (!kyber) {
      throw new Error(`prekey-bundle: device ${deviceBundle.device_id} has no kyber_prekey (server should always return one for a device registered with kyber_last_resort)`);
    }

    const identityKey = Signal.PublicKey.deserialize(Buffer.from(deviceBundle.identity_key_b64, 'base64'));
    const signedPub = Signal.PublicKey.deserialize(
      Buffer.from(deviceBundle.signed_prekey.public_key_b64, 'base64'),
    );
    const signedSig = Buffer.from(deviceBundle.signed_prekey.signature_b64, 'base64');
    const otk = deviceBundle.one_time_prekey;
    const otkId = otk ? otk.key_id : null;
    const otkPub = otk ? Signal.PublicKey.deserialize(Buffer.from(otk.public_key_b64, 'base64')) : null;
    const kyberPub = Signal.KEMPublicKey.deserialize(Buffer.from(kyber.public_key_b64, 'base64'));
    const kyberSig = Buffer.from(kyber.signature_b64, 'base64');

    const preKeyBundle = Signal.PreKeyBundle.new(
      deviceBundle.registration_id,
      1, // libsignal device index for the recipient (see Participant.libsignalDeviceIndex)
      otkId,
      otkPub,
      deviceBundle.signed_prekey.key_id,
      signedPub,
      signedSig,
      identityKey,
      kyber.key_id,
      kyberPub,
      kyberSig,
    );
    return { preKeyBundle, deviceBundle, raw: bundle };
  }

  async currentOtkCount() {
    const out = await backend.prekeyCount(this.uid, this.deviceId);
    return out.one_time_prekeys_available;
  }
}
