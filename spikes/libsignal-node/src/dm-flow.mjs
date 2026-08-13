// Milestone-3 spike: real 1:1 Signal-protocol E2EE flow against the real Chirp backend.
//
// Alice and Bob are simulated as two Node-side "devices". Every key-directory and
// messaging call goes over real HTTP to the FastAPI backend on localhost:8000 —
// nothing is mocked except the Kyber prekey directory (see participant.mjs docstring
// and FINDINGS.md #1, since the backend schema has no column for it).

import assert from 'node:assert/strict';
import * as Signal from '@signalapp/libsignal-client';
import { backend } from './lib/backend.mjs';
import { Participant } from './lib/participant.mjs';
import { toB64, fromB64, log, section, decryptInbound } from './lib/util.mjs';

const runId = Date.now().toString(36);

async function main() {
  section('0. backend health check');
  const health = await backend.healthz();
  log('GET /healthz ->', health);
  assert.deepEqual(health, { status: 'ok' });

  section('1. bootstrap two real users via POST /auth/bootstrap');
  const alice = new Participant('Alice', `spike-alice-${runId}`);
  const bob = new Participant('Bob', `spike-bob-${runId}`);
  await alice.bootstrap({ email: `alice-${runId}@example.edu` });
  await bob.bootstrap({ email: `bob-${runId}@example.edu` });
  log('alice user_id =', alice.userId);
  log('bob   user_id =', bob.userId);

  section('2. register devices via POST /devices (real identity key + signed prekey + 10 OTKs)');
  await alice.registerDevice();
  await bob.registerDevice();
  log('alice device_id =', alice.deviceId, 'registration_id =', alice.registrationId);
  log('bob   device_id =', bob.deviceId, 'registration_id =', bob.registrationId);

  const aliceAddress = alice.address();
  const bobAddress = bob.address();

  section('3. create a dm conversation via POST /conversations');
  const conversation = await backend.createConversation(alice.uid, {
    kind: 'dm',
    member_user_ids: [bob.userId],
  });
  log('conversation_id =', conversation.id, 'members =', conversation.members.map((m) => m.user_id));

  section('4. OTK count before bundle fetch (GET /devices/{id}/prekeys/count, as Bob)');
  const otkBefore = await bob.currentOtkCount();
  log('bob one_time_prekeys_available (before) =', otkBefore);
  assert.equal(otkBefore, 10);

  section("5. Alice fetches Bob's prekey bundle via GET /users/{id}/prekey-bundle (real backend call)");
  const { preKeyBundle, deviceBundle } = await Participant.fetchPreKeyBundleFor(alice.uid, bob.userId);
  log('bundle device_id =', deviceBundle.device_id, 'otk key_id consumed =', deviceBundle.one_time_prekey?.key_id);
  assert.ok(deviceBundle.one_time_prekey, 'expected an OTK to be handed out on first fetch');

  section('6. OTK count after bundle fetch — must have dropped by exactly 1');
  const otkAfter = await bob.currentOtkCount();
  log('bob one_time_prekeys_available (after) =', otkAfter);
  assert.equal(otkAfter, otkBefore - 1, 'server-side OTK was not consumed');

  section('7. X3DH: Alice processes the bundle -> session installed in her local SessionStore');
  await Signal.processPreKeyBundle(
    preKeyBundle,
    bobAddress,
    aliceAddress,
    alice.stores.session,
    alice.stores.identity,
  );
  const aliceSessionWithBob = await alice.stores.session.getSession(bobAddress);
  assert.ok(aliceSessionWithBob, 'X3DH did not install a session');
  log('X3DH session established on Alice side for', bobAddress.toString());

  section('8. Alice encrypts message #1 (Double Ratchet, first message -> PreKeySignalMessage)');
  const plaintext1 = `Hey Bob, it's Alice (${runId}) — first message over real X3DH!`;
  const ciphertext1 = await Signal.signalEncrypt(
    Buffer.from(plaintext1, 'utf8'),
    bobAddress,
    aliceAddress,
    alice.stores.session,
    alice.stores.identity,
  );
  log('ciphertext1 wire type =', ciphertext1.type(), '(3 = PreKeySignalMessage)');
  const ciphertext1B64 = toB64(ciphertext1.serialize());

  section('9. POST ciphertext #1 to /conversations/{id}/messages (server never parses it)');
  const stored1 = await backend.sendMessage(alice.uid, conversation.id, {
    sender_device_id: alice.deviceId,
    ciphertext_b64: ciphertext1B64,
  });
  log('stored message_id =', stored1.id, 'message_type =', stored1.message_type);
  assert.equal(stored1.ciphertext_b64, ciphertext1B64, 'server round-tripped ciphertext byte-for-byte');

  section('10. Bob GETs message history and decrypts message #1');
  const bobHistory = await backend.listMessages(bob.uid, conversation.id);
  const fetched1 = bobHistory.find((m) => m.id === stored1.id);
  assert.ok(fetched1, 'message #1 not found in Bob history fetch');
  const decrypted1 = await decryptInbound(fromB64(fetched1.ciphertext_b64), aliceAddress, bobAddress, bob.stores);
  const plaintext1Out = Buffer.from(decrypted1.plaintext).toString('utf8');
  log('decrypted via', decrypted1.wireType, '->', JSON.stringify(plaintext1Out));
  assert.equal(plaintext1Out, plaintext1, 'round-trip #1 mismatch');
  log('ROUND TRIP #1 OK (Alice -> Bob, X3DH + Double Ratchet)');

  section('11. Bob replies — message #2, opposite direction, ratchet advances (-> plain SignalMessage)');
  const plaintext2 = `Got it Alice (${runId}) — replying, ratchet advances!`;
  const ciphertext2 = await Signal.signalEncrypt(
    Buffer.from(plaintext2, 'utf8'),
    aliceAddress,
    bobAddress,
    bob.stores.session,
    bob.stores.identity,
  );
  log('ciphertext2 wire type =', ciphertext2.type(), '(2 = SignalMessage/Whisper)');
  const ciphertext2B64 = toB64(ciphertext2.serialize());
  const stored2 = await backend.sendMessage(bob.uid, conversation.id, {
    sender_device_id: bob.deviceId,
    ciphertext_b64: ciphertext2B64,
  });
  log('stored message_id =', stored2.id);

  const aliceHistory = await backend.listMessages(alice.uid, conversation.id);
  const fetched2 = aliceHistory.find((m) => m.id === stored2.id);
  assert.ok(fetched2, 'message #2 not found in Alice history fetch');
  const decrypted2 = await decryptInbound(fromB64(fetched2.ciphertext_b64), bobAddress, aliceAddress, alice.stores);
  const plaintext2Out = Buffer.from(decrypted2.plaintext).toString('utf8');
  log('decrypted via', decrypted2.wireType, '->', JSON.stringify(plaintext2Out));
  assert.equal(plaintext2Out, plaintext2, 'round-trip #2 mismatch');
  log('ROUND TRIP #2 OK (Bob -> Alice, ratchet advanced)');

  section('12. history sanity: GET /conversations/{id}/messages returns both, newest first');
  assert.equal(aliceHistory.length, 2);
  assert.equal(aliceHistory[0].id, stored2.id, 'expected newest-first ordering');

  section('ALL DM-FLOW ASSERTIONS PASSED');
  log(JSON.stringify({
    conversation_id: conversation.id,
    alice_user_id: alice.userId,
    bob_user_id: bob.userId,
    alice_device_id: alice.deviceId,
    bob_device_id: bob.deviceId,
    otk_before: otkBefore,
    otk_after: otkAfter,
    message1_id: stored1.id,
    message2_id: stored2.id,
  }, null, 2));
}

main().catch((err) => {
  console.error('\nDM FLOW FAILED:', err);
  process.exitCode = 1;
});
