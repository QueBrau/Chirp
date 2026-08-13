// In-memory implementations of the libsignal protocol store interfaces.
//
// @signalapp/libsignal-client ships abstract classes (SessionStore, IdentityKeyStore,
// PreKeyStore, SignedPreKeyStore, KyberPreKeyStore, SenderKeyStore) — the caller must
// supply persistence. Real clients back these with SQLite/Keychain; here it's plain
// Maps since this spike runs one Node process per "conversation", not per device.

import * as Signal from '@signalapp/libsignal-client';

const { IdentityChange } = Signal;

export class InMemorySessionStore extends Signal.SessionStore {
  #sessions = new Map();

  async saveSession(address, record) {
    this.#sessions.set(address.toString(), record);
  }

  async getSession(address) {
    return this.#sessions.get(address.toString()) ?? null;
  }

  async getExistingSessions(addresses) {
    return addresses
      .map((a) => this.#sessions.get(a.toString()))
      .filter((s) => s !== undefined);
  }
}

export class InMemoryIdentityKeyStore extends Signal.IdentityKeyStore {
  #identityKeyPair;
  #registrationId;
  #trusted = new Map();

  constructor(identityKeyPair, registrationId) {
    super();
    this.#identityKeyPair = identityKeyPair;
    this.#registrationId = registrationId;
  }

  async getIdentityKey() {
    return this.#identityKeyPair.privateKey;
  }

  async getLocalRegistrationId() {
    return this.#registrationId;
  }

  async saveIdentity(address, key) {
    const existing = this.#trusted.get(address.toString());
    this.#trusted.set(address.toString(), key);
    if (existing && !existing.equals(key)) {
      return IdentityChange.ReplacedExisting;
    }
    return IdentityChange.NewOrUnchanged;
  }

  async isTrustedIdentity(address, key) {
    const existing = this.#trusted.get(address.toString());
    if (!existing) return true;
    return existing.equals(key);
  }

  async getIdentity(address) {
    return this.#trusted.get(address.toString()) ?? null;
  }
}

export class InMemoryPreKeyStore extends Signal.PreKeyStore {
  #keys = new Map();

  async savePreKey(id, record) {
    this.#keys.set(id, record);
  }

  async getPreKey(id) {
    const record = this.#keys.get(id);
    if (!record) throw new Error(`InMemoryPreKeyStore: no one-time prekey id=${id}`);
    return record;
  }

  async removePreKey(id) {
    this.#keys.delete(id);
  }
}

export class InMemorySignedPreKeyStore extends Signal.SignedPreKeyStore {
  #keys = new Map();

  async saveSignedPreKey(id, record) {
    this.#keys.set(id, record);
  }

  async getSignedPreKey(id) {
    const record = this.#keys.get(id);
    if (!record) throw new Error(`InMemorySignedPreKeyStore: no signed prekey id=${id}`);
    return record;
  }
}

export class InMemoryKyberPreKeyStore extends Signal.KyberPreKeyStore {
  #keys = new Map();
  #used = new Set();

  async saveKyberPreKey(id, record) {
    this.#keys.set(id, record);
  }

  async getKyberPreKey(id) {
    const record = this.#keys.get(id);
    if (!record) throw new Error(`InMemoryKyberPreKeyStore: no kyber prekey id=${id}`);
    return record;
  }

  async markKyberPreKeyUsed(id) {
    this.#used.add(id);
  }

  isUsed(id) {
    return this.#used.has(id);
  }
}

export class InMemorySenderKeyStore extends Signal.SenderKeyStore {
  #keys = new Map();

  async saveSenderKey(sender, distributionId, record) {
    this.#keys.set(`${sender.toString()}::${distributionId}`, record);
  }

  async getSenderKey(sender, distributionId) {
    return this.#keys.get(`${sender.toString()}::${distributionId}`) ?? null;
  }
}

/** Bundle of all five per-device stores a full protocol participant needs. */
export function makeStoreBundle(identityKeyPair, registrationId) {
  return {
    session: new InMemorySessionStore(),
    identity: new InMemoryIdentityKeyStore(identityKeyPair, registrationId),
    preKey: new InMemoryPreKeyStore(),
    signedPreKey: new InMemorySignedPreKeyStore(),
    kyberPreKey: new InMemoryKyberPreKeyStore(),
    senderKey: new InMemorySenderKeyStore(),
  };
}
