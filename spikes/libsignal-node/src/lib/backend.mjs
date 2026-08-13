// Thin HTTP client for the real Chirp backend (FastAPI on localhost:8000).
// Every field name matches CONVENTIONS.md exactly (base64 fields suffixed `_b64`).

const BASE_URL = process.env.CHIRP_API_URL ?? 'http://localhost:8000';

class BackendError extends Error {
  constructor(method, path, status, body) {
    super(`${method} ${path} -> HTTP ${status}: ${typeof body === 'string' ? body : JSON.stringify(body)}`);
    this.name = 'BackendError';
    this.status = status;
    this.body = body;
  }
}

async function request(method, path, { uid, body } = {}) {
  const headers = { 'Content-Type': 'application/json' };
  if (uid) headers['X-Debug-Firebase-Uid'] = uid;
  const res = await fetch(`${BASE_URL}${path}`, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  const parsed = text ? JSON.parse(text) : null;
  if (!res.ok) {
    throw new BackendError(method, path, res.status, parsed ?? text);
  }
  return parsed;
}

export const backend = {
  baseUrl: BASE_URL,

  healthz: () => request('GET', '/healthz'),

  bootstrap: (uid, { email, display_name, account_type }) =>
    request('POST', '/auth/bootstrap', {
      uid,
      body: { email, display_name, account_type },
    }),

  registerDevice: (uid, { device_label, registration_id, identity_key_b64, signed_prekey, one_time_prekeys }) =>
    request('POST', '/devices', {
      uid,
      body: { device_label, registration_id, identity_key_b64, signed_prekey, one_time_prekeys },
    }),

  replenishPrekeys: (uid, deviceId, body) =>
    request('POST', `/devices/${deviceId}/prekeys`, { uid, body }),

  prekeyCount: (uid, deviceId) => request('GET', `/devices/${deviceId}/prekeys/count`, { uid }),

  prekeyBundle: (uid, userId) => request('GET', `/users/${userId}/prekey-bundle`, { uid }),

  createConversation: (uid, { chapter_id = null, kind, title = null, member_user_ids }) =>
    request('POST', '/conversations', { uid, body: { chapter_id, kind, title, member_user_ids } }),

  sendMessage: (uid, conversationId, { sender_device_id, ciphertext_b64, message_type = 'signal' }) =>
    request('POST', `/conversations/${conversationId}/messages`, {
      uid,
      body: { sender_device_id, ciphertext_b64, message_type },
    }),

  listMessages: (uid, conversationId, { before, limit } = {}) => {
    const qs = new URLSearchParams();
    if (before) qs.set('before', before);
    if (limit) qs.set('limit', String(limit));
    const suffix = qs.toString() ? `?${qs}` : '';
    return request('GET', `/conversations/${conversationId}/messages${suffix}`, { uid });
  },
};

export { BackendError };
