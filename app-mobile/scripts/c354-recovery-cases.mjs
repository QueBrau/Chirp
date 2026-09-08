/** Actual screen -> API -> Operation -> fetch and socket callback regressions.
 * Native UI primitives/navigation and network are deterministic test boundaries.
 * No raw event row is used as an authoritative HTTP fixture automatically.
 */
import assert from 'node:assert/strict';
const id = n => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
const time = n => new Date(Date.UTC(2026, 8, 1, 0, 0, n)).toISOString();
const THREAD = id(9000), CHAPTER = id(9001);
const message = n => ({ id: id(n), conversation_id: THREAD, sender_device_id: id(8000), ciphertext_b64: 'eA==', message_type: 'signal', created_at: time(n) });
const conversation = n => ({ id: id(n), kind: 'group', title: `Group ${n}`, chapter_id: CHAPTER, protocol_version: 1, created_at: time(n), members: [], last_message_at: time(n), has_messages: true });
const poll = n => ({ id: id(n), chapter_id: CHAPTER, meeting_id: null, question: `Poll ${n}`, status: 'open', created_by: 'id-A', created_at: time(n), closed_at: null, options: [], total_votes: 1, my_option_id: id(7000) });
const threadPath = 'app/(tabs)/messages/[id].tsx', inboxPath = 'app/(tabs)/messages/index.tsx', secretaryPath = 'app/(tabs)/chapter/secretary.tsx';

export async function recoveryCases({ test, flush, deferred, response, readySocket, nodes }) {
  const mount = async (e, path) => { const view = await e.mountScreen(path); for (let i = 0; i < 4; i++) await flush(); return view; };
  const rows = (view, type = 'View') => nodes(view.tree).filter(node => node.type === type && node.key).map(node => node.key);
  const has = (view, title) => nodes(view.tree).some(node => node.props?.title === title);
  const button = (view, label) => nodes(view.tree).find(node => node.props?.label === label || node.props?.accessibilityLabel === label);
  const cards = view => nodes(view.tree).filter(node => node.type === 'PollCard');
  const frame = (e, event) => e.sockets.at(-1).onmessage({ data: JSON.stringify(event) });
  const hint = (e, n, conversationId = THREAD) => frame(e, { type: 'message', message_id: id(n), conversation_id: conversationId, ciphertext_b64: 'UNTRUSTED', created_at: time(n) });
  const page = (all, url) => { const before = url.searchParams.get('before_id'), start = before ? all.findIndex(row => row.id === before) + 1 : 0; return all.slice(start, start + Number(url.searchParams.get('limit'))); };
  async function fixture(e, { ready = true } = {}) {
    const normal = e.fetch;
    const f = { messages: [message(1)], conversations: [conversation(1)], polls: [poll(1)], requests: [], transmittedRows: 0, transmittedBytes: 0, override: null };
    e.route.id = THREAD;
    e.fetch = async (raw, options) => {
      const url = new URL(raw), path = url.pathname;
      if (path.startsWith('/auth/') || path.startsWith('/campuses/')) return normal(raw, options);
      f.requests.push({ url, options });
      const overridden = f.override?.(url, options);
      if (overridden !== undefined) return overridden;
      let data;
      if (path === '/conversations') data = page(f.conversations, url);
      else if (path.endsWith('/messages/by-id')) data = f.messages.filter(row => url.searchParams.get('ids').split(',').includes(row.id));
      else if (path.endsWith('/messages')) data = page(f.messages, url);
      else if (path.endsWith('/leave')) return response(204);
      else if (path.startsWith('/conversations/')) data = { ...conversation(9000), id: path.split('/')[2] };
      else if (path === '/me/memberships') data = [{ chapter_id: CHAPTER, role: 'secretary', chapter_name: 'Test chapter' }];
      else if (path.endsWith('/polls')) data = page(f.polls, url);
      else if (path.endsWith('/attendance-summary')) data = { members: [] };
      else if (path.endsWith('/members') || path.endsWith('/with-attendance')) data = [];
      else throw new Error(`Unexpected test route: ${path}`);
      if (Array.isArray(data)) f.transmittedRows += data.length;
      f.transmittedBytes += Buffer.byteLength(JSON.stringify(data));
      return response(200, data);
    };
    f.calls = suffix => f.requests.filter(row => row.url.pathname.endsWith(suffix));
    e.emit(e.user('A')); await e.mountProvider();
    if (ready) { readySocket(e.sockets[0]); await flush(); }
    return f;
  }

  await test('c354 thread first ACK closes the HTTP gap; mount-ready fetches once', async e => {
    const f = await fixture(e, { ready: false }), view = await mount(e, threadPath);
    assert.deepEqual(rows(view), [id(1)]); assert.equal(f.calls('/messages').length, 1);
    f.messages = [message(2)]; e.sockets[0].onopen(); await flush();
    assert.equal(f.calls('/messages').length, 1);
    e.sockets[0].onmessage({ data: '{"type":"ready"}' }); await flush();
    assert.deepEqual(rows(view), [id(2)]); assert.equal(f.calls('/messages').length, 2);
    e.sockets[0].onmessage({ data: '{"type":"ready"}' }); await flush();
    assert.equal(f.calls('/messages').length, 2);
    await view.focus(false); const second = await mount(e, threadPath);
    assert.deepEqual(rows(second), [id(2)]); assert.equal(f.calls('/messages').length, 3);
  });

  await test('c354 a hint during initial fetch uses filtered ID lookup, never its payload', async e => {
    const f = await fixture(e), first = deferred();
    f.override = url => url.pathname.endsWith('/messages') ? first.promise : undefined;
    const view = await mount(e, threadPath); hint(e, 2); await flush();
    f.override = null; f.messages = [message(2)]; first.resolve(response(200, [message(1)])); await flush();
    await e.timer.tick(500);
    assert.deepEqual(rows(view), [id(2), id(1)]); assert.equal(f.calls('/messages').length, 1);
    assert.equal(f.calls('/messages/by-id').length, 1);
  });

  await test('c354 authoritative refresh removes newly hidden history and buffered old events cannot revive it', async e => {
    const f = await fixture(e), view = await mount(e, threadPath), refresh = deferred();
    f.override = url => url.pathname.endsWith('/messages') ? refresh.promise : undefined;
    const pending = view.tree.props.onRefresh(); await flush(); hint(e, 1);
    f.override = null; f.messages = []; refresh.resolve(response(200, [])); await pending; await e.timer.tick(500);
    assert.deepEqual(rows(view), []); assert.equal(f.calls('/messages/by-id').length, 1);
  });

  await test('c354 more than 200 missed messages stops at four pages with explicit continuation', async e => {
    const f = await fixture(e); f.messages = Array.from({ length: 260 }, (_, i) => message(300 - i));
    const view = await mount(e, threadPath);
    assert.equal(rows(view).length, 200); assert.equal(f.calls('/messages').length, 4);
    assert.ok(button(view, 'Load older messages'));
    assert.ok(nodes(view.tree).some(node => String(node.props?.children).includes('missed while offline')));
    button(view, 'Load older messages').props.onPress(); button(view, 'Load older messages').props.onPress(); await flush();
    assert.equal(f.calls('/messages').length, 5); assert.equal(rows(view).length, 250);
    assert.equal(f.calls('/messages').at(-1).url.searchParams.get('before_id'), id(101));
  });

  await test('c354 live ID duplicates, microsecond ties and late commits keep canonical order and raw paging cursor', async e => {
    const f = await fixture(e); f.messages = Array.from({ length: 210 }, (_, i) => message(300 - i));
    const view = await mount(e, threadPath);
    const a = { ...message(600), created_at: '2026-09-01T00:00:00.000001Z' }, b = { ...message(599), created_at: '2026-09-01T00:00:00.000002Z' };
    const tied = { ...a, id: id(601) };
    f.messages.push(a, b, tied); hint(e, 600); hint(e, 600); hint(e, 599); hint(e, 601); await e.timer.tick(500);
    assert.equal(f.calls('/messages/by-id').length, 1); assert.equal(f.calls('/messages/by-id')[0].url.searchParams.get('ids').split(',').length, 3);
    const displayed = rows(view); assert.ok(displayed.indexOf(id(599)) < displayed.indexOf(id(601))); assert.ok(displayed.indexOf(id(601)) < displayed.indexOf(id(600))); assert.equal(displayed.filter(x => x === id(600)).length, 1);
    button(view, 'Load older messages').props.onPress(); await flush();
    assert.equal(f.calls('/messages').at(-1).url.searchParams.get('before_id'), id(101));
  });

  await test('c354 isolated live hint costs one canonical row, not another 200-row refresh', async e => {
    const f = await fixture(e); f.messages = Array.from({ length: 200 }, (_, i) => ({ ...message(300 - i), ciphertext_b64: 'x'.repeat(65_536) }));
    await mount(e, threadPath); const bytes = f.transmittedBytes, count = f.transmittedRows;
    f.messages.unshift({ ...message(400), ciphertext_b64: 'x'.repeat(65_536) }); hint(e, 400); await e.timer.tick(500);
    assert.equal(f.calls('/messages').length, 4); assert.equal(f.calls('/messages/by-id').length, 1);
    assert.equal(f.transmittedRows - count, 1); assert.ok(f.transmittedBytes - bytes < 66_000);
    assert.ok(bytes > 13_000_000); // Application JSON bytes, not a claimed wire-cost measurement.
  });

  await test('c354 live burst permits one follow-up, then exposes unresolved activity', async e => {
    const f = await fixture(e), view = await mount(e, threadPath), first = deferred(), second = deferred();
    let reads = 0; f.override = url => url.pathname.endsWith('/messages/by-id') ? (++reads === 1 ? first.promise : second.promise) : undefined;
    hint(e, 2); await e.timer.tick(500); hint(e, 3); first.resolve(response(200, [message(2)])); await flush();
    await e.timer.tick(499); assert.equal(reads, 1); await e.timer.tick(1); assert.equal(reads, 2);
    hint(e, 4); second.resolve(response(200, [message(3)])); await flush(); await e.timer.tick(30_000);
    assert.equal(reads, 2); assert.ok(has(view, 'Some updates may be missing'));
  });

  await test('c354 more than 50 pending IDs is bounded and reported instead of silent catch-up', async e => {
    const f = await fixture(e), view = await mount(e, threadPath);
    for (let n = 1; n <= 51; n++) hint(e, n);
    await e.timer.tick(500);
    assert.ok(has(view, 'Some updates may be missing'));
    assert.ok(f.calls('/messages/by-id').every(row => row.url.searchParams.get('ids').split(',').length <= 50));
    assert.ok(f.calls('/messages/by-id').length <= 1);
  });

  await test('c354 inbox summary refresh retains only uncovered older rows and marks an old unresolved hint', async e => {
    const f = await fixture(e); f.conversations = Array.from({ length: 31 }, (_, i) => conversation(100 - i));
    const view = await mount(e, inboxPath); button(view, 'Load older conversations').props.onPress(); await flush();
    assert.equal(rows(view, 'ListRow').length, 31);
    hint(e, 1, id(70)); await e.timer.tick(500);
    assert.equal(rows(view, 'ListRow').length, 31); assert.ok(has(view, 'Some activity may be missing'));
    assert.equal(f.calls('/messages').length, 0); assert.equal(f.calls('/messages/by-id').length, 0);
    assert.equal(f.calls('/conversations').length, 3);
  });

  await test('c354 inbox fresh head retires stale older request and its cursor', async e => {
    const f = await fixture(e); f.conversations = Array.from({ length: 35 }, (_, i) => conversation(100 - i));
    const view = await mount(e, inboxPath), older = deferred();
    f.override = url => url.pathname === '/conversations' && url.searchParams.has('before') ? older.promise : undefined;
    button(view, 'Load older conversations').props.onPress(); await flush();
    const request = f.calls('/conversations').at(-1); f.conversations = [conversation(500)];
    await view.tree.props.onRefresh(); assert.equal(request.options.signal.aborted, true);
    older.resolve(response(200, [conversation(1)])); await flush();
    assert.deepEqual(rows(view, 'ListRow'), [id(500)]); assert.equal(button(view, 'Load older conversations'), undefined);
  });

  await test('c354 route blur cancels actual fetch; offscreen ready/hints do no work; refocus and replay recover', async e => {
    const f = await fixture(e), view = await mount(e, threadPath), stalled = deferred();
    f.override = url => url.pathname.endsWith('/messages') ? stalled.promise : undefined;
    const pending = view.tree.props.onRefresh(); await flush(); const request = f.calls('/messages').at(-1), oldRefresh = view.tree.props.onRefresh;
    await view.focus(false); assert.equal(request.options.signal.aborted, true);
    const count = f.requests.length; hint(e, 2); const socket = e.sockets.at(-1); socket.onclose({ code: 4503 }); await e.timer.tick(1_000); readySocket(e.sockets.at(-1)); await flush();
    assert.equal(f.requests.length, count);
    f.override = null; f.messages = [message(3)]; await view.focus(true); stalled.resolve(response(200, [message(2)])); await pending; await flush();
    assert.deepEqual(rows(view), [id(3)]); const currentCount = f.requests.length;
    await oldRefresh(); assert.equal(f.requests.length, currentCount);
    view.replayEffects(); await flush(); assert.deepEqual(rows(view), [id(3)]); assert.equal(f.requests.length, currentCount + 2);
  });

  await test('c354 route and account replacement reject stale completions and captured callbacks', async e => {
    const f = await fixture(e), view = await mount(e, threadPath), stalled = deferred();
    f.override = url => url.pathname.endsWith('/messages') ? stalled.promise : undefined;
    const pending = view.tree.props.onRefresh(); await flush(); const old = view.tree.props.onRefresh;
    f.override = null; e.route.id = id(9100); f.messages = [{ ...message(8), conversation_id: id(9100) }]; view.update({}); await flush();
    stalled.resolve(response(200, [message(1)])); await pending; await flush(); assert.deepEqual(rows(view), [id(8)]);
    const count = f.requests.length; await old(); assert.equal(f.requests.length, count);
    e.emit(e.user('B')); view.update({}); await flush(); await old(); assert.equal(e.signOutCalls, 0);
    assert.ok(f.requests.slice(count).length > 0);
    assert.ok(f.requests.slice(count).every(row => !row.options.headers.Authorization || row.options.headers.Authorization === 'Bearer token-B')); // A token must never cross the generation; an initial missing bearer may get the ordinary401 retry.
  });

  for (const status of [401, 403, 404]) await test(`c354 final authoritative ${status} clears cached thread without Firebase logout`, async e => {
    const f = await fixture(e), view = await mount(e, threadPath);
    f.override = url => url.pathname.startsWith('/conversations/') ? response(status, { detail: status === 401 ? 'user_not_registered' : 'not_found' }) : undefined;
    await view.tree.props.onRefresh(); await flush(); assert.deepEqual(rows(view), []); assert.ok(has(view, 'Conversation unavailable')); assert.equal(view.tree.props.title, 'Conversation');
    const count = f.requests.length; hint(e, 2); await e.timer.tick(500); assert.equal(f.requests.length, count); assert.equal(e.signOutCalls, 0);
  });

  await test('c354 thread final401 has neutral explicit retry, cleared title while retrying, and stale-action ownership', async e => {
    const f = await fixture(e), view = await mount(e, threadPath);
    f.override = url => url.pathname.startsWith('/conversations/') ? response(401, { detail: 'user_not_registered' }) : undefined;
    await view.tree.props.onRefresh(); await flush();
    const unavailable = nodes(view.tree).find(node => node.props?.title === 'Conversation unavailable');
    assert.match(unavailable.props.message, /couldn't verify access/); assert.equal(unavailable.props.actionLabel, 'Try again');
    const retry = unavailable.props.onAction, confirmed = deferred();
    f.override = url => url.pathname === `/conversations/${THREAD}` ? confirmed.promise : undefined;
    f.messages = [message(2)]; retry(); await flush();
    assert.equal(view.tree.props.title, 'Conversation'); assert.deepEqual(rows(view), []);
    f.override = null; confirmed.resolve(response(200, conversation(9000))); await flush();
    assert.deepEqual(rows(view), [id(2)]); assert.equal(has(view, 'Conversation unavailable'), false);
    await view.focus(false); const count = f.requests.length; retry(); await flush(); assert.equal(f.requests.length, count);
    await view.focus(true); const current = f.requests.length; retry(); await flush(); assert.equal(f.requests.length, current);
    e.emit(e.user('B')); view.update({}); await flush(); const afterAccount = f.requests.length;
    retry(); await flush(); assert.equal(f.requests.length, afterAccount); assert.equal(e.signOutCalls, 0);
  });

  await test('c354 captured leave confirmation and in-flight leave cannot navigate another focus', async e => {
    const f = await fixture(e), view = await mount(e, threadPath);
    const leaveButton = () => nodes(view.tree).find(node => typeof node.type === 'function' && node.type.name === 'LeaveConversationButton');
    leaveButton().props.onPress(); const old = e.confirmation.onConfirm;
    await view.focus(false); await view.focus(true); old(); await flush(); assert.equal(f.calls('/leave').length, 0);
    const leave = deferred(); f.override = url => url.pathname.endsWith('/leave') ? leave.promise : undefined;
    leaveButton().props.onPress(); e.confirmation.onConfirm(); await flush(); const request = f.calls('/leave')[0];
    await view.focus(false); assert.equal(request.options.signal.aborted, true); leave.resolve(response(204)); await flush(); assert.deepEqual(e.navigation, []);
  });

  await test('c354 a stalled history body shares the 15-second deadline and aborts fetch', async e => {
    const f = await fixture(e), body = deferred(); f.override = url => url.pathname.endsWith('/messages') ? { ...response(), json: () => body.promise } : undefined;
    const view = await mount(e, threadPath); const request = f.calls('/messages')[0];
    await e.timer.tick(15_000); assert.equal(request.options.signal.aborted, true); assert.ok(has(view, "Couldn't load this conversation"));
    body.resolve([message(1)]); await flush(); assert.deepEqual(rows(view), []);
  });

  await test('c354 a failed manual thread retry remains handled beside cached rows and the parked composer', async e => {
    const f = await fixture(e), view = await mount(e, threadPath);
    f.override = url => url.pathname.endsWith('/messages') ? response(503, { detail: 'unavailable' }) : undefined;
    await view.tree.props.onRefresh(); await flush();
    assert.ok(has(view, "Couldn't load this conversation")); assert.deepEqual(rows(view), [id(1)]);
    assert.ok(nodes(view.tree).some(node => node.type === 'TextInput' && node.props.editable === false));
    nodes(view.tree).find(node => node.props?.title === "Couldn't load this conversation").props.onAction(); await flush();
    assert.ok(has(view, "Couldn't load this conversation")); assert.equal(f.calls('/messages').length, 3);
    f.override = null; f.messages = [message(2)];
    nodes(view.tree).find(node => node.props?.title === "Couldn't load this conversation").props.onAction(); await flush();
    assert.equal(has(view, "Couldn't load this conversation"), false); assert.deepEqual(rows(view), [id(2)]);
  });

  await test('c354 ID adapter rejects oversized input before HTTP and serializes one bounded canonical CSV', async e => {
    const f = await fixture(e), api = e.load('src/api/messages.ts');
    await assert.rejects(api.getMessagesById(THREAD, Array(51).fill(id(1))));
    await assert.rejects(api.getMessagesById(THREAD, ['not-a-uuid'])); assert.equal(f.requests.length, 0);
    await api.getMessagesById(THREAD, [id(1).toUpperCase(), id(1)]);
    assert.equal(f.calls('/messages/by-id')[0].url.searchParams.get('ids'), id(1));
  });

  await test('c354 secretary initial fetch overlap receives one authoritative follow-up', async e => {
    const f = await fixture(e), first = deferred(); let reads = 0;
    f.override = url => url.pathname.endsWith('/polls') ? (++reads === 1 ? first.promise : response(200, [])) : undefined;
    const view = await mount(e, secretaryPath);
    frame(e, { type: 'poll', action: 'opened', chapter_id: CHAPTER, poll_id: id(1), poll: poll(1) }); await flush();
    first.resolve(response(200, [])); await flush(); assert.equal(cards(view).length, 0); assert.equal(reads, 2);
  });

  await test('c354 delayed vote cannot revive a poll closed by overlapping ready refresh', async e => {
    const f = await fixture(e), view = await mount(e, secretaryPath), vote = deferred();
    f.override = url => url.pathname.endsWith('/vote') ? vote.promise : undefined;
    cards(view)[0].props.onVote(id(7001)); await flush();
    f.polls = [{ ...poll(1), status: 'closed', total_votes: 9, my_option_id: id(7001) }];
    e.sockets.at(-1).onclose({ code: 4503 }); await e.timer.tick(1_000); readySocket(e.sockets.at(-1)); await flush();
    vote.resolve(response(200, { ...poll(1), total_votes: 2, my_option_id: id(7001) })); await flush();
    assert.equal(cards(view)[0].props.poll.status, 'closed'); assert.equal(cards(view)[0].props.poll.total_votes, 9);
    assert.equal(cards(view)[0].props.poll.my_option_id, id(7001)); assert.equal(f.calls('/polls').length, 3);
  });

  await test('c354 secretary deletion tombstone survives a stale ready response and vote completion', async e => {
    const f = await fixture(e), view = await mount(e, secretaryPath), vote = deferred(), read = deferred();
    f.override = url => url.pathname.endsWith('/vote') ? vote.promise : url.pathname.endsWith('/polls') ? read.promise : undefined;
    cards(view)[0].props.onVote(id(7001)); await flush(); e.sockets.at(-1).onclose({ code: 4503 }); await e.timer.tick(1_000); readySocket(e.sockets.at(-1)); await flush();
    frame(e, { type: 'poll', action: 'deleted', chapter_id: CHAPTER, poll_id: id(1) });
    f.override = null; f.polls = []; read.resolve(response(200, [poll(1)])); vote.resolve(response(200, poll(1))); await flush();
    assert.equal(cards(view).length, 0); assert.ok(f.calls('/polls').length <= 3);
  });

  await test('c354 secretary continuous overlapping votes cap the refresh at one follow-up', async e => {
    const f = await fixture(e), view = await mount(e, secretaryPath), first = deferred(), second = deferred(); let reads = 0;
    f.override = url => url.pathname.endsWith('/polls') ? (++reads === 1 ? first.promise : second.promise) : undefined;
    e.sockets.at(-1).onclose({ code: 4503 }); await e.timer.tick(1_000); readySocket(e.sockets.at(-1)); await flush();
    frame(e, { type: 'poll', action: 'updated', chapter_id: CHAPTER, poll_id: id(1), poll: { ...poll(1), total_votes: 2 } }); first.resolve(response(200, [poll(1)])); await flush();
    frame(e, { type: 'poll', action: 'updated', chapter_id: CHAPTER, poll_id: id(1), poll: { ...poll(1), total_votes: 3 } }); second.resolve(response(200, [poll(1)])); await flush(); await e.timer.tick(30_000);
    assert.equal(reads, 2); assert.ok(has(view, 'Poll updates may be incomplete'));
    assert.equal(cards(view)[0].props.poll.my_option_id, id(7000));
  });

  for (const retirement of ['blur', 'account', 'timeout']) await test(`c354 secretary ${retirement} aborts the actual poll fetch signal`, async e => {
    const f = await fixture(e), stalled = deferred();
    f.override = url => url.pathname.endsWith('/polls') ? stalled.promise : undefined;
    const view = await mount(e, secretaryPath), request = f.calls('/polls')[0]; assert.ok(request);
    if (retirement === 'blur') await view.focus(false);
    if (retirement === 'account') { e.emit(e.user('B')); view.update({}); await flush(); }
    if (retirement === 'timeout') await e.timer.tick(15_000);
    assert.equal(request.options.signal.aborted, true);
    stalled.resolve(response(200, [poll(7)])); await flush();
    if (retirement !== 'account') assert.equal(cards(view).length, 0);
  });

  for (const retirement of ['blur', 'account', 'timeout']) await test(`c354 secretary ${retirement} also aborts the reconnect refresh fetch`, async e => {
    const f = await fixture(e), view = await mount(e, secretaryPath), stalled = deferred();
    f.override = url => url.pathname.endsWith('/polls') ? stalled.promise : undefined;
    e.sockets.at(-1).onclose({ code: 4503 }); await e.timer.tick(1_000); readySocket(e.sockets.at(-1)); await flush();
    const request = f.calls('/polls').at(-1); assert.equal(f.calls('/polls').length, 2);
    if (retirement === 'blur') await view.focus(false);
    if (retirement === 'account') { e.emit(e.user('B')); view.update({}); await flush(); }
    if (retirement === 'timeout') await e.timer.tick(15_000);
    assert.equal(request.options.signal.aborted, true);
    stalled.resolve(response(200, [poll(7)])); await flush();
    if (retirement === 'timeout') { assert.ok(has(view, 'Poll updates may be incomplete')); assert.equal(cards(view)[0].props.poll.id, id(1)); }
  });

  await test('c354 secretary visible window refresh is capped at four pages and preserves canonical own ballots', async e => {
    const f = await fixture(e); f.polls = Array.from({ length: 260 }, (_, i) => poll(300 - i));
    const view = await mount(e, secretaryPath);
    for (let i = 0; i < 4; i++) { button(view, 'Load earlier polls').props.onPress(); await flush(); }
    assert.equal(cards(view).length, 250);
    f.polls = f.polls.map(row => ({ ...row, my_option_id: id(7002) })); const before = f.calls('/polls').length;
    e.sockets.at(-1).onclose({ code: 4503 }); await e.timer.tick(1_000); readySocket(e.sockets.at(-1));
    for (let i = 0; i < 5; i++) await flush();
    assert.equal(f.calls('/polls').length - before, 4); assert.equal(cards(view).length, 200);
    assert.ok(cards(view).every(card => card.props.poll.my_option_id === id(7002))); assert.ok(button(view, 'Load earlier polls'));
  });

  await test('c354 secretary off-focus events/ready do not fetch; refocus rechecks membership', async e => {
    const f = await fixture(e), view = await mount(e, secretaryPath); await view.focus(false); const count = f.requests.length;
    frame(e, { type: 'poll', action: 'opened', chapter_id: CHAPTER, poll_id: id(2), poll: poll(2) });
    e.sockets.at(-1).onclose({ code: 4503 }); await e.timer.tick(1_000); readySocket(e.sockets.at(-1)); await flush(); assert.equal(f.requests.length, count);
    f.override = url => url.pathname === '/me/memberships' ? response(200, []) : undefined;
    await view.focus(true); assert.equal(cards(view).length, 0); assert.equal(f.calls('/polls').length, 1);
  });

  await test('c354 secretary final401 offers a focused explicit retry and rejects retired retry actions', async e => {
    const f = await fixture(e), view = await mount(e, secretaryPath);
    f.override = url => url.pathname.endsWith('/polls') ? response(401, { detail: 'user_not_registered' }) : undefined;
    e.sockets.at(-1).onclose({ code: 4503 }); await e.timer.tick(1_000); readySocket(e.sockets.at(-1)); await flush();
    const unavailable = nodes(view.tree).find(node => node.props?.title === 'Dashboard unavailable');
    assert.ok(unavailable); assert.match(unavailable.props.message, /couldn't verify access/); assert.equal(cards(view).length, 0);
    assert.equal(unavailable.props.actionLabel, 'Try again'); const retry = unavailable.props.onAction;
    f.override = null; f.polls = [poll(2)]; retry(); for (let i = 0; i < 3; i++) await flush();
    assert.equal(cards(view)[0].props.poll.id, id(2)); assert.equal(has(view, 'Dashboard unavailable'), false);
    const current = f.requests.length; retry(); await flush(); assert.equal(f.requests.length, current);
    await view.focus(false); retry(); await flush(); assert.equal(f.requests.length, current);
    await view.focus(true); e.emit(e.user('B')); view.update({}); for (let i = 0; i < 3; i++) await flush();
    const afterAccount = f.requests.length; retry(); await flush(); assert.equal(f.requests.length, afterAccount); assert.equal(e.signOutCalls, 0);
  });

  await test('c354 secretary authoritative loss of access retires cached rows and listeners', async e => {
    const f = await fixture(e), view = await mount(e, secretaryPath);
    f.override = url => url.pathname.endsWith('/polls') ? response(403, { detail: 'membership_required' }) : undefined;
    e.sockets.at(-1).onclose({ code: 4503 }); await e.timer.tick(1_000); readySocket(e.sockets.at(-1)); await flush();
    assert.equal(cards(view).length, 0); assert.ok(has(view, 'Dashboard unavailable'));
    frame(e, { type: 'poll', action: 'opened', chapter_id: CHAPTER, poll_id: id(3), poll: poll(3) }); await flush(); assert.equal(cards(view).length, 0);
  });
}
