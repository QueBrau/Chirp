// Milestone-3 spike: sender-key group flow (SPEC §6.3/§6.4), in-process, 3 simulated
// members. Backend round-trip is explicitly optional for this part per the spike brief
// — the crypto is the risk being de-risked here, not the HTTP plumbing (already proven
// end to end in dm-flow.mjs). No mocked crypto: real X3DH pairwise sessions carry the
// real SenderKeyDistributionMessage; real groupEncrypt/groupDecrypt do the group cipher.

import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import * as Signal from '@signalapp/libsignal-client';
import { LocalMember } from './lib/local-member.mjs';
import { log, section, decryptInbound } from './lib/util.mjs';

async function establishPairwiseSession(initiator, peer) {
  const bundle = peer.issuePreKeyBundle();
  await Signal.processPreKeyBundle(
    bundle,
    peer.address(),
    initiator.address(),
    initiator.stores.session,
    initiator.stores.identity,
  );
}

/** Alice -> peer: wrap `payloadBytes` in a real pairwise Double Ratchet message and deliver it in-process. */
async function sendPairwise(sender, receiver, payloadBytes) {
  const ciphertext = await Signal.signalEncrypt(
    Buffer.from(payloadBytes),
    receiver.address(),
    sender.address(),
    sender.stores.session,
    sender.stores.identity,
  );
  const { plaintext } = await decryptInbound(ciphertext.serialize(), sender.address(), receiver.address(), receiver.stores);
  return plaintext;
}

async function distributeSenderKey(creator, distributionId, members) {
  const distributionMsg = await Signal.SenderKeyDistributionMessage.create(
    creator.address(),
    distributionId,
    creator.stores.senderKey,
  );
  for (const member of members) {
    const received = await sendPairwise(creator, member, distributionMsg.serialize());
    const parsed = Signal.SenderKeyDistributionMessage.deserialize(received);
    await Signal.processSenderKeyDistributionMessage(creator.address(), parsed, member.stores.senderKey);
    log(`  distributed sender key ${distributionId} to ${member.label} (via real pairwise Double Ratchet session)`);
  }
}

async function main() {
  section('1. create 3 members + pairwise X3DH sessions (Alice <-> Bob, Alice <-> Carol)');
  const alice = new LocalMember('alice-group');
  const bob = new LocalMember('bob-group');
  const carol = new LocalMember('carol-group');
  await establishPairwiseSession(alice, bob);
  await establishPairwiseSession(alice, carol);
  log('pairwise sessions established: alice->bob, alice->carol');

  section('2. Alice creates a sender key and distributes it to Bob + Carol');
  const distributionId1 = crypto.randomUUID();
  await distributeSenderKey(alice, distributionId1, [bob, carol]);

  section('3. Alice encrypts ONE group message with the sender key');
  const groupPlaintext1 = 'Chapter meeting moved to 7pm — sender-key group message #1';
  const groupCiphertext1 = await Signal.groupEncrypt(
    alice.address(),
    distributionId1,
    alice.stores.senderKey,
    Buffer.from(groupPlaintext1, 'utf8'),
  );
  log('group ciphertext #1 produced once, wire type =', groupCiphertext1.type(), '(7 = SenderKeyMessage)');
  const groupCiphertext1Bytes = groupCiphertext1.serialize();

  section('4. Both Bob and Carol decrypt the same ciphertext independently');
  const bobPlaintext1 = Buffer.from(
    await Signal.groupDecrypt(alice.address(), bob.stores.senderKey, groupCiphertext1Bytes),
  ).toString('utf8');
  const carolPlaintext1 = Buffer.from(
    await Signal.groupDecrypt(alice.address(), carol.stores.senderKey, groupCiphertext1Bytes),
  ).toString('utf8');
  log('bob   decrypted:', JSON.stringify(bobPlaintext1));
  log('carol decrypted:', JSON.stringify(carolPlaintext1));
  assert.equal(bobPlaintext1, groupPlaintext1);
  assert.equal(carolPlaintext1, groupPlaintext1);
  log('GROUP ROUND TRIP OK — one ciphertext, two independent recipients decrypt correctly');

  section('5. member-leave rotation (SPEC §6.4): Carol leaves, Alice rotates + redistributes to Bob ONLY');
  const distributionId2 = crypto.randomUUID();
  log('old distributionId =', distributionId1);
  log('new distributionId =', distributionId2, '(fresh sender key chain; old one is abandoned)');
  await distributeSenderKey(alice, distributionId2, [bob]); // NOTE: carol intentionally excluded

  section('6. Alice encrypts group message #2 with the ROTATED sender key');
  const groupPlaintext2 = 'New treasurer announced — sender-key group message #2 (post-rotation)';
  const groupCiphertext2 = await Signal.groupEncrypt(
    alice.address(),
    distributionId2,
    alice.stores.senderKey,
    Buffer.from(groupPlaintext2, 'utf8'),
  );
  const groupCiphertext2Bytes = groupCiphertext2.serialize();

  section('7. Bob (still a member) decrypts fine; Carol (left) cannot decrypt the new sender key');
  const bobPlaintext2 = Buffer.from(
    await Signal.groupDecrypt(alice.address(), bob.stores.senderKey, groupCiphertext2Bytes),
  ).toString('utf8');
  log('bob decrypted post-rotation message:', JSON.stringify(bobPlaintext2));
  assert.equal(bobPlaintext2, groupPlaintext2);

  let carolRejected = false;
  let carolError = null;
  try {
    await Signal.groupDecrypt(alice.address(), carol.stores.senderKey, groupCiphertext2Bytes);
  } catch (err) {
    carolRejected = true;
    carolError = String(err.message ?? err);
  }
  assert.ok(carolRejected, 'SECURITY BUG: Carol (left the group) could still decrypt a post-rotation message');
  log('carol groupDecrypt correctly THREW:', carolError);
  log('MEMBER-LEAVE ROTATION OK — removed member cannot read messages sent after rotation');

  section('ALL GROUP-FLOW ASSERTIONS PASSED');
}

main().catch((err) => {
  console.error('\nGROUP FLOW FAILED:', err);
  process.exitCode = 1;
});
