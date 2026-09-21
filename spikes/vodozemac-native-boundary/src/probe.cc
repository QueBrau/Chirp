#include "chirp-native-boundary-probe/src/lib.rs.h"
#include "probe.h"
#include "rust/cxx.h"
#include <algorithm>
#include <array>
#include <stdexcept>
#include <string>

namespace chirp_probe {
namespace {
void require(bool value, const char *diagnostic) {
  if (!value) throw std::runtime_error(diagnostic);
}

template <typename F> void fails(F action, const char *expected) {
  try {
    action();
  } catch (const rust::Error &error) {
    require(std::string(error.what()) == expected, "unexpected_error_class");
    return;
  }
  throw std::runtime_error("expected_failure_missing");
}

rust::Slice<const std::uint8_t> bytes(const rust::Vec<std::uint8_t> &value) {
  return {value.data(), value.size()};
}

rust::Vec<std::uint8_t> body() {
  rust::Vec<std::uint8_t> result;
  // Binary NUL/non-UTF8 content tests the byte boundary, not string coercion.
  for (auto value : {0, 1, 42, 128, 255}) result.push_back(static_cast<std::uint8_t>(value));
  return result;
}

bool same(const rust::Vec<std::uint8_t> &a, const rust::Vec<std::uint8_t> &b) {
  return a.size() == b.size() && std::equal(a.begin(), a.end(), b.begin());
}

Context context(const char *logical = "message-1", bool reverse = false) {
  Context c;
  c.version = 1; c.conversation = "conversation-1"; c.logical_id = logical;
  c.sender_account = reverse ? "bob" : "alice";
  c.sender_device = reverse ? "bob-phone" : "alice-phone";
  c.recipient_account = reverse ? "alice" : "bob";
  c.recipient_device = reverse ? "alice-phone" : "bob-phone";
  c.kind = 0; c.epoch = 0;
  return c;
}

WireMessage first(NativeSession &sender) {
  const auto text = body();
  return sender.encrypt(context("initial"), bytes(text));
}

struct Pair {
  rust::Box<NativeAccount> alice;
  rust::Box<NativeAccount> bob;
  IdentityBinding alice_binding;
  IdentityBinding bob_binding;
  rust::Box<NativeSession> sender;
  rust::Box<InboundSession> initial;
  rust::Box<NativeSession> receiver;

  Pair() : alice(new_account("alice", "alice-phone")), bob(new_account("bob", "bob-phone")),
      alice_binding(alice->binding()), bob_binding(bob->binding()),
      sender(alice->outbound(bob_binding, bytes(bob->next_one_time_key()))),
      initial(bob->inbound(alice_binding, first(*sender), context("initial"))),
      receiver(initial->take_session()) {
    require(same(initial->initial_plaintext(), body()), "initial_plaintext_mismatch");
    const auto text = body();
    const auto reply = receiver->encrypt(context("reply", true), bytes(text));
    require(same(sender->decrypt(context("reply", true), reply), text), "reply_mismatch");
  }
};

void roundtrip() {
  Pair pair;
  const auto text = body();
  auto message = pair.sender->encrypt(context(), bytes(text));
  require(message.subtype == 1, "expected_normal_subtype");
  require(same(pair.receiver->decrypt(context(), message), text), "normal_plaintext_mismatch");
}

void signed_binding() {
  auto account = new_account("alice", "alice-phone");
  auto binding = account->binding();
  verify_binding(binding);
  auto changed = binding;
  changed.device = "alice-tablet";
  fails([&] { verify_binding(changed); }, "invalid_signature");
  changed = binding; changed.account = "mallory";
  fails([&] { verify_binding(changed); }, "invalid_signature");
  changed = binding; changed.dh_key[0] ^= 1;
  fails([&] { verify_binding(changed); }, "invalid_signature");
  changed = binding; changed.signature[0] ^= 1;
  fails([&] { verify_binding(changed); }, "invalid_signature");
  verify_binding(binding);
}

void wrong_device() {
  Pair first_pair;
  Pair independent_pair; // Same public labels, different actual device keys.
  const auto text = body();
  auto message = first_pair.sender->encrypt(context(), bytes(text));
  fails([&] { independent_pair.receiver->decrypt(context(), message); }, "decrypt_failed");
  auto wrong = context(); wrong.recipient_device = "bob-tablet";
  fails([&] { first_pair.receiver->decrypt(wrong, message); }, "context_mismatch");
  require(same(first_pair.receiver->decrypt(context(), message), text), "correct_device_failed");
}

void tamper() {
  Pair pair;
  const auto text = body();
  auto message = pair.sender->encrypt(context(), bytes(text));
  auto changed = message;
  changed.ciphertext[changed.ciphertext.size() - 1] ^= 1;
  fails([&] { pair.receiver->decrypt(context(), changed); }, "decrypt_failed");
  require(same(pair.receiver->decrypt(context(), message), text), "tamper_poisoned_ratchet");
}

void context_failure() {
  Pair pair;
  const auto text = body();
  auto message = pair.sender->encrypt(context(), bytes(text));
  for (int field = 0; field < 4; ++field) {
    auto changed = context();
    if (field == 0) changed.conversation = "other-conversation";
    if (field == 1) changed.logical_id = "other-message";
    if (field == 2) changed.kind = 1;
    if (field == 3) changed.epoch = 1;
    fails([&] { pair.receiver->decrypt(changed, message); }, "context_mismatch");
  }
  require(same(pair.receiver->decrypt(context(), message), text), "context_poisoned_ratchet");
  auto alice = new_account("alice", "alice-phone");
  auto bob = new_account("bob", "bob-phone");
  auto sender = alice->outbound(bob->binding(), bytes(bob->next_one_time_key()));
  auto initial_message = first(*sender);
  auto wrong = context("initial"); wrong.conversation = "other-conversation";
  fails([&] { bob->inbound(alice->binding(), initial_message, wrong); }, "context_mismatch");
  auto accepted = bob->inbound(alice->binding(), initial_message, context("initial"));
  require(same(accepted->initial_plaintext(), text), "context_consumed_prekey");
}

void bounds_and_errors() {
  Pair pair;
  const auto text = body();
  auto wrong = context(); wrong.version = 0;
  fails([&] { pair.sender->encrypt(wrong, bytes(text)); }, "unsupported_context");
  wrong = context(); wrong.kind = 99;
  fails([&] { pair.sender->encrypt(wrong, bytes(text)); }, "unsupported_context");
  wrong = context(); wrong.conversation = std::string(65, 'x');
  fails([&] { pair.sender->encrypt(wrong, bytes(text)); }, "invalid_context");
  rust::Vec<std::uint8_t> at_limit;
  for (int i = 0; i < 1024; ++i) at_limit.push_back(255);
  const auto boundary_context = context("body-boundary");
  const auto boundary_message = pair.sender->encrypt(boundary_context, bytes(at_limit));
  require(same(pair.receiver->decrypt(boundary_context, boundary_message), at_limit), "body_boundary_failed");
  rust::Vec<std::uint8_t> too_large;
  for (int i = 0; i < 1025; ++i) too_large.push_back(1);
  fails([&] { pair.sender->encrypt(context(), bytes(too_large)); }, "body_too_large");
  auto message = pair.sender->encrypt(context(), bytes(text));
  auto changed = message; changed.subtype = 3;
  fails([&] { pair.receiver->decrypt(context(), changed); }, "invalid_message");
  changed = message; changed.ciphertext.clear();
  fails([&] { pair.receiver->decrypt(context(), changed); }, "invalid_ciphertext_size");
  require(same(pair.receiver->decrypt(context(), message), text), "invalid_input_poisoned_state");
}

void lock_handles() {
  Pair pair;
  const auto text = body();
  auto message = pair.sender->encrypt(context(), bytes(text));
  pair.bob->lock();
  pair.bob->lock();
  fails([&] { pair.bob->binding(); }, "account_locked");
  fails([&] { pair.receiver->decrypt(context(), message); }, "account_locked");
  fails([&] { pair.receiver->encrypt(context("reply", true), bytes(text)); }, "account_locked");
  fails([&] { pair.bob->next_one_time_key(); }, "account_locked");
  fails([&] { pair.initial->initial_plaintext(); }, "account_locked");
  fails([&] { pair.initial->take_session(); }, "account_locked");
  // The other account remains usable; no global state/shared account key exists.
  pair.alice->binding();
}

void snapshots() {
  auto alice = new_account("alice", "alice-phone");
  auto bob = new_account("bob", "bob-phone");
  auto identity = bob->binding();
  auto otk = bob->next_one_time_key();
  std::array<std::uint8_t, 32> key{}; key.fill(7);
  std::array<std::uint8_t, 32> wrong_key{}; wrong_key.fill(8);
  auto snapshot = bob->snapshot({key.data(), key.size()});
  fails([&] { restore_account("bob", "bob-phone", bytes(identity.dh_key), snapshot, {wrong_key.data(), wrong_key.size()}); }, "restore_failed");
  auto other_identity = alice->binding();
  fails([&] { restore_account("bob", "bob-phone", bytes(other_identity.dh_key), snapshot, {key.data(), key.size()}); }, "identity_mismatch");
  auto restored = restore_account("bob", "bob-phone", bytes(identity.dh_key), snapshot, {key.data(), key.size()});
  require(same(restored->binding().dh_key, identity.dh_key), "restored_identity_mismatch");
  auto sender = alice->outbound(identity, bytes(otk));
  auto inbound = restored->inbound(alice->binding(), first(*sender), context("initial"));
  require(same(inbound->initial_plaintext(), body()), "restored_private_prekey_failed");
}

void replay_and_ownership() {
  Pair pair;
  fails([&] { pair.initial->take_session(); }, "session_taken");
  const auto text = body();
  auto message = pair.sender->encrypt(context(), bytes(text));
  pair.receiver->decrypt(context(), message);
  fails([&] { pair.receiver->decrypt(context(), message); }, "decrypt_failed");
}
} // namespace

void probe_case(std::uint32_t case_id) {
  switch (case_id) {
  case 0: roundtrip(); break;
  case 1: signed_binding(); break;
  case 2: wrong_device(); break;
  case 3: tamper(); break;
  case 4: context_failure(); break;
  case 5: bounds_and_errors(); break;
  case 6: lock_handles(); break;
  case 7: snapshots(); break;
  case 8: replay_and_ownership(); break;
  default: throw std::runtime_error("unknown_probe_case");
  }
}
} // namespace chirp_probe
