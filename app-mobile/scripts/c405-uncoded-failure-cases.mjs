/** c405 Option D: execute real socket/session/request handlers at RN's failure boundary.
 * RN emits an uncoded error BEFORE its synthetic 1006 close. No live network/device.
 */
import assert from "node:assert/strict";

export async function uncodedFailureCases({ test, flush, deferred, response, readySocket, nodes }) {
  const probes = e => e.calls.filter(([url]) => url.endsWith("/auth/campus-verification"));
  const meCalls = e => e.calls.filter(([url]) => url.endsWith("/auth/me"));
  const socket = e => e.load("src/realtime/socket.ts").chirpSocket;
  async function start(e) {
    e.emit(e.user("A")); await e.mountProvider();
    assert.equal(e.hook.value.status, "ready");
    e.calls.length = 0; // Exclude the Provider's independent initial campus lookup.
  }
  function nativeFailure(ws) {
    const queuedClose = ws.onclose;
    ws.onerror({ type: "error" });
    // RN next dispatches a close; a queued callback must also be harmless.
    ws.onclose?.({ code: 1006 });
    queuedClose({ code: 1006 });
  }

  await test("c405 journey b: suspended before handshake reaches existing suspension screen", async e => {
    await start(e);
    e.fetch = async () => response(403, { detail: "account_suspended" });
    nativeFailure(e.sockets[0]); await flush();
    assert.equal(e.hook.value.status, "suspended", "uncoded pre-accept suspension must not silently back off");
    assert.equal(probes(e).length, 1);
    assert.equal(probes(e)[0][1].method, "GET");
    assert.equal(probes(e)[0][1].headers.Authorization, "Bearer token-A");
    assert.equal(meCalls(e).length, 0, "authoritative 403 needs no second account probe");
    const layout = e.load("app/(tabs)/_layout.tsx").default();
    assert.equal(nodes(layout).find(n => n.type === "Redirect").props.href, "/suspended");
    await e.timer.tick(120_000);
    assert.equal(e.sockets.length, 1); assert.equal(e.signOutCalls, 0);
  });

  await test("c405 uncoded close uses the same authoritative probe", async e => {
    await start(e); e.fetch = async () => response(403, { detail: "account_suspended" });
    e.sockets[0].onclose({}); await flush();
    assert.equal(e.hook.value.status, "suspended"); assert.equal(probes(e).length, 1);
  });

  for (const [label, status, data] of [
    ["success", 200, { verified: false }],
    ["unrelated forbidden", 403, { detail: "campus_verification_required" }],
    ["capacity", 503, { detail: "account_suspended" }],
    ["rate limited", 429, {}],
  ]) {
    await test(`c405 ${label} probes once across the entire finite failure burst`, async e => {
      await start(e); e.fetch = async () => response(status, data);
      const delays = [1_000, 2_000, 4_000, 8_000, 16_000, 30_000];
      for (let i = 0; i <= delays.length; i++) {
        nativeFailure(e.sockets.at(-1)); await flush();
        assert.equal(probes(e).length, 1, "must not regress to one GET per reconnect attempt");
        if (i < delays.length) {
          await e.timer.tick(delays[i] - 1); assert.equal(e.sockets.length, i + 1);
          await e.timer.tick(1); assert.equal(e.sockets.length, i + 2);
        }
      }
      assert.equal(e.hook.value.realtimeStatus, "paused");
      assert.equal(e.hook.value.status, "ready"); assert.equal(meCalls(e).length, 0);
      socket(e).connect(); await e.timer.tick(120_000);
      assert.equal(e.sockets.length, 7); assert.equal(probes(e).length, 1); assert.equal(e.signOutCalls, 0);
    });
  }

  await test("c405 network failure preserves backoff without inventing suspension", async e => {
    await start(e); e.fetch = async () => { throw new TypeError("offline"); };
    nativeFailure(e.sockets[0]); await flush(); await e.timer.tick(1_000);
    assert.equal(e.sockets.length, 2); assert.equal(probes(e).length, 1);
    assert.equal(e.hook.value.status, "ready"); assert.equal(meCalls(e).length, 0);
  });

  await test("c405 401 sends exactly one probe then delegates fresh-token revalidation", async e => {
    await start(e); let forced = 0;
    e.auth.currentUser.getIdToken = async force => force ? (++forced, "fresh-A") : "token-A";
    const normal = e.fetch;
    e.fetch = async (url, options) => url.endsWith("/auth/campus-verification") ? response(401, { detail: "invalid_token" }) : normal(url, options);
    nativeFailure(e.sockets[0]); await flush();
    assert.equal(probes(e).length, 1, "the diagnostic GET must not automatically retry 401");
    assert.equal(meCalls(e).length, 1); assert.equal(forced, 1);
    assert.equal(meCalls(e)[0][1].headers.Authorization, "Bearer fresh-A");
    assert.deepEqual([...e.sockets[1].protocols], ["fresh-A"]);
    assert.equal(e.hook.value.status, "ready"); assert.equal(e.signOutCalls, 0);
  });

  await test("c405 ordinary HTTP requests still retry 401 by default", async e => {
    await start(e); let forced = 0;
    e.auth.currentUser.getIdToken = async force => force ? (++forced, "fresh-A") : "token-A";
    e.fetch = async (_url, options) => options.headers.Authorization === "Bearer token-A"
      ? response(401) : response(200, { ok: true });
    assert.equal((await e.client.request("/ordinary")).ok, true);
    assert.equal(e.calls.length, 2); assert.equal(forced, 1);
  });

  for (const code of [1006, 4503, 4401, 4403]) {
    await test(`c405 coded close ${code} keeps existing behavior without a probe`, async e => {
      await start(e); e.sockets[0].onclose({ code }); await flush();
      assert.equal(probes(e).length, 0);
      if (code === 4401 || code === 4403) {
        assert.equal(meCalls(e).length, 1); assert.equal(e.sockets.length, 2);
      } else {
        assert.equal(meCalls(e).length, 0); await e.timer.tick(1_000); assert.equal(e.sockets.length, 2);
      }
      assert.equal(e.hook.value.status, "ready");
    });
  }

  await test("c405 missing native callbacks still use the connect deadline without a probe", async e => {
    await start(e); await e.timer.tick(10_000); await e.timer.tick(1_000);
    assert.equal(e.sockets.length, 2); assert.equal(probes(e).length, 0);
  });

  await test("c405 burst survives brief ready but resets after five stable ready seconds", async e => {
    await start(e);
    nativeFailure(e.sockets[0]); await flush(); await e.timer.tick(1_000);
    readySocket(e.sockets[1]); await e.timer.tick(4_999);
    nativeFailure(e.sockets[1]); await flush();
    assert.equal(probes(e).length, 1);
    await e.timer.tick(2_000); readySocket(e.sockets[2]); await e.timer.tick(5_000);
    nativeFailure(e.sockets[2]); await flush();
    assert.equal(probes(e).length, 2); await e.timer.tick(1_000); assert.equal(e.sockets.length, 4);
  });

  await test("c405 manual retry begins a new failure burst", async e => {
    await start(e); nativeFailure(e.sockets[0]); await flush();
    await e.hook.value.retryRealtime(); await flush();
    nativeFailure(e.sockets.at(-1)); await flush();
    assert.equal(probes(e).length, 2); assert.equal(meCalls(e).length, 1);
  });

  await test("c405 pending probe coalesces duplicate callbacks and late body cannot outlive its budget", async e => {
    await start(e); const body = deferred();
    e.fetch = async () => ({ ...response(403), json: () => body.promise });
    const first = e.sockets[0], error = first.onerror, close = first.onclose;
    nativeFailure(first); error({}); close({}); socket(e).connect(); await flush();
    assert.equal(probes(e).length, 1); assert.equal(first.closed, true);
    await e.timer.tick(9_999); assert.equal(e.sockets.length, 1);
    await e.timer.tick(1); assert.equal(probes(e)[0][1].signal.aborted, true);
    await e.timer.tick(1_000); assert.equal(e.sockets.length, 2);
    body.resolve({ detail: "account_suspended" }); await flush();
    assert.equal(e.hook.value.status, "ready"); assert.equal(e.signOutCalls, 0);
  });

  await test("c405 background cancels the probe and foreground does not replenish its burst", async e => {
    await start(e); const pending = deferred(), normal = e.fetch;
    e.fetch = () => pending.promise;
    nativeFailure(e.sockets[0]); await flush(); const request = probes(e)[0];
    e.setAppState("background"); await flush(); assert.equal(request[1].signal.aborted, true);
    pending.resolve(response(403, { detail: "account_suspended" })); await flush();
    assert.equal(e.hook.value.status, "ready");
    e.fetch = normal; e.setAppState("active"); await flush();
    assert.equal(e.sockets.length, 2); nativeFailure(e.sockets[1]); await flush();
    assert.equal(probes(e).length, 1); assert.equal(meCalls(e).length, 1);
    await e.timer.tick(1_000); assert.equal(e.sockets.length, 3);
  });

  await test("c405 A's late suspension cannot alter B or B's socket", async e => {
    await start(e); const pending = deferred(), normal = e.fetch;
    e.fetch = () => pending.promise; nativeFailure(e.sockets[0]); await flush();
    const request = probes(e)[0]; e.fetch = normal; e.emit(e.user("B")); await flush();
    assert.equal(request[1].signal.aborted, true); readySocket(e.sockets.at(-1));
    const count = e.sockets.length; pending.resolve(response(403, { detail: "account_suspended" })); await flush();
    assert.equal(e.hook.value.user.firebase_uid, "B"); assert.equal(e.hook.value.status, "ready");
    await e.timer.tick(120_000); assert.equal(e.sockets.length, count); assert.equal(e.hook.value.realtimeStatus, "open");
  });

  await test("c405 authoritative suspension cancels an older successful account refresh", async e => {
    await start(e); const pending = deferred();
    e.fetch = async url => url.endsWith("/auth/me") ? pending.promise : response(403, { detail: "account_suspended" });
    const refresh = e.hook.value.refresh(); await flush(); const request = meCalls(e)[0];
    nativeFailure(e.sockets[0]); await flush();
    assert.equal(e.hook.value.status, "suspended"); assert.equal(request[1].signal.aborted, true);
    pending.resolve(response(200, { user: e.hook.value.user, memberships: [] })); await refresh; await flush();
    assert.equal(e.hook.value.status, "suspended"); assert.equal(e.sockets.length, 1);
  });

  await test("c405 unmount aborts the diagnostic request and cannot revive its socket", async e => {
    await start(e); const pending = deferred(); e.fetch = () => pending.promise;
    nativeFailure(e.sockets[0]); await flush(); const request = probes(e)[0];
    e.hook.unmount(); await flush(); assert.equal(request[1].signal.aborted, true);
    pending.resolve(response(401)); await flush(); await e.timer.tick(120_000);
    assert.equal(e.sockets.length, 1); assert.equal(meCalls(e).length, 0); assert.equal(e.signOutCalls, 0);
  });
}
