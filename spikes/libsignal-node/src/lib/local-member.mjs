// Local-only (no backend) participant for the sender-key group demo. SPEC §6.3 says
// "backend round-trip optional" for groups, so this generates its own key material
// entirely in-process — no HTTP calls — while still using the real libsignal crypto
// (identity keys, EC signed/one-time prekeys, a local Kyber prekey for PQXDH, exactly
// as participant.mjs does for the backend-integrated DM flow).

import * as Signal from '@signalapp/libsignal-client';
import { makeStoreBundle } from './signal-stores.mjs';

const SIGNED_PREKEY_ID = 1;
const KYBER_PREKEY_ID = 1;

function randomRegistrationId() {
  return 1 + Math.floor(Math.random() * 16382);
}

export class LocalMember {
  constructor(label, otkCount = 5) {
    this.label = label;
    this.identityKeyPair = Signal.IdentityKeyPair.generate();
    this.registrationId = randomRegistrationId();
    this.stores = makeStoreBundle(this.identityKeyPair, this.registrationId);
    this.libsignalDeviceIndex = 1;
    this._otkQueue = [];

    const signedPriv = Signal.PrivateKey.generate();
    const signedPub = signedPriv.getPublicKey();
    const signedSig = this.identityKeyPair.privateKey.sign(signedPub.serialize());
    this.stores.signedPreKey.saveSignedPreKey(
      SIGNED_PREKEY_ID,
      Signal.SignedPreKeyRecord.new(SIGNED_PREKEY_ID, Date.now(), signedPub, signedPriv, signedSig),
    );
    this._signedPrekey = { id: SIGNED_PREKEY_ID, pub: signedPub, sig: signedSig };

    for (let keyId = 1; keyId <= otkCount; keyId += 1) {
      const priv = Signal.PrivateKey.generate();
      const pub = priv.getPublicKey();
      this.stores.preKey.savePreKey(keyId, Signal.PreKeyRecord.new(keyId, pub, priv));
      this._otkQueue.push({ id: keyId, pub });
    }

    const kyberKeyPair = Signal.KEMKeyPair.generate();
    const kyberSig = this.identityKeyPair.privateKey.sign(kyberKeyPair.getPublicKey().serialize());
    this.stores.kyberPreKey.saveKyberPreKey(
      KYBER_PREKEY_ID,
      Signal.KyberPreKeyRecord.new(KYBER_PREKEY_ID, Date.now(), kyberKeyPair, kyberSig),
    );
    this._kyber = { id: KYBER_PREKEY_ID, pub: kyberKeyPair.getPublicKey(), sig: kyberSig };
  }

  address() {
    return Signal.ProtocolAddress.new(this.label, this.libsignalDeviceIndex);
  }

  /** Hand out a PreKeyBundle for this member, consuming one local OTK (mirrors the backend's behavior). */
  issuePreKeyBundle() {
    const otk = this._otkQueue.shift() ?? null;
    return Signal.PreKeyBundle.new(
      this.registrationId,
      this.libsignalDeviceIndex,
      otk ? otk.id : null,
      otk ? otk.pub : null,
      this._signedPrekey.id,
      this._signedPrekey.pub,
      this._signedPrekey.sig,
      this.identityKeyPair.publicKey,
      this._kyber.id,
      this._kyber.pub,
      this._kyber.sig,
    );
  }
}
