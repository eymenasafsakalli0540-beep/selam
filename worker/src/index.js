// Selam - Cloudflare Worker (kartsız, siempre-on)
// Mail: Google Apps Script (Gmail). Veriler: KV + R2. Gerçek zamanlı: Durable Object.

const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Headers': 'Authorization, Content-Type',
  'Access-Control-Allow-Methods': 'GET, POST, DELETE, OPTIONS',
};

const json = (data, status = 200) =>
  new Response(JSON.stringify(data), {
    status,
    headers: { 'content-type': 'application/json; charset=utf-8', ...CORS },
  });
const fail = (message, status = 400) => json({ error: message }, status);
const uid = () => crypto.randomUUID().replaceAll('-', '');
const now = () => Date.now();
const chatKey = (a, b) => (a < b ? `${a}:${b}` : `${b}:${a}`);

async function bodyOf(request) {
  const type = request.headers.get('content-type') || '';
  if (type.includes('application/json')) {
    try { return await request.json(); } catch { return {}; }
  }
  return {};
}

function tokenOf(request) {
  const h = request.headers.get('Authorization') || '';
  return h.startsWith('Bearer ') ? h.slice(7).trim() : '';
}

async function storedUser(env, id) {
  const raw = await env.KV.get(`user:${id}`);
  return raw ? JSON.parse(raw) : null;
}

async function publicUser(env, id) {
  const u = await storedUser(env, id);
  if (!u) return { id, username: '?', online: false, last: 0 };
  return {
    id: u.id,
    username: u.username || null,
    online: (await env.KV.get(`online:${id}`)) === '1',
    last: u.last || 0,
  };
}

async function ensureUser(env, email) {
  let id = await env.KV.get(`email:${email}`);
  if (!id) {
    id = uid();
    await env.KV.put(`user:${id}`, JSON.stringify({ id, email, username: null, last: now() }));
    await env.KV.put(`email:${email}`, id);
  }
  return storedUser(env, id);
}

async function userFromRequest(env, request) {
  const token = tokenOf(request);
  if (!token) return null;
  const email = await env.KV.get(`token:${token}`);
  if (!email) return null;
  const user = await ensureUser(env, email);
  return { ...user, token };
}

async function getChat(env, a, b) {
  const raw = await env.KV.get(`chat:${chatKey(a, b)}`);
  return raw ? JSON.parse(raw) : [];
}

async function saveChat(env, a, b, messages) {
  await env.KV.put(`chat:${chatKey(a, b)}`, JSON.stringify(messages.slice(-2000)));
}

async function addChatList(env, a, b) {
  for (const [owner, peer] of [[a, b], [b, a]]) {
    const raw = await env.KV.get(`chats:${owner}`);
    const list = raw ? JSON.parse(raw) : [];
    if (!list.includes(peer)) {
      list.push(peer);
      await env.KV.put(`chats:${owner}`, JSON.stringify(list));
    }
  }
}

async function sendMailCode(env, email, code) {
  const response = await fetch(env.APPSCRIPT_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json; charset=utf-8' },
    body: JSON.stringify({
      secret: env.APPSCRIPT_SECRET,
      to: email,
      name: 'Selam',
      subject: `Selam giriş kodun: ${code}`,
      body: `Merhaba,\n\nSelam giriş kodun: ${code}\n\nBu kodu kimseyle paylaşma.\n\nSelam`,
    }),
  });
  const text = await response.text();
  if (!text.replace(/\s/g, '').includes('"ok":true')) {
    throw new Error('Mail gönderilemedi. Apps Script ayarlarını kontrol et.');
  }
}

async function api(request, env, url) {
  const path = url.pathname.slice(5); // /api/... -> ...
  if (request.method === 'OPTIONS') return json({}, 204);

  if (path === 'auth/request' && request.method === 'POST') {
    const { email } = await bodyOf(request);
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(String(email || ''))) return fail('Geçerli bir e-posta yaz.', 400);
    const code = String(crypto.getRandomValues(new Uint32Array(1))[0] % 1000000).padStart(6, '0');
    await env.KV.put(`code:${email}`, JSON.stringify({ code, exp: now() + 10 * 60 * 1000 }), { expirationTtl: 600 });
    try {
      await sendMailCode(env, email, code);
    } catch (e) {
      return fail(e.message || 'Mail gönderilemedi.', 500);
    }
    return json({ ok: true, dev: false });
  }

  if (path === 'auth/verify' && request.method === 'POST') {
    const { email, code } = await bodyOf(request);
    const raw = await env.KV.get(`code:${email}`);
    if (!raw) return fail('Kod süresi doldu. Yeniden gönder.', 400);
    const rec = JSON.parse(raw);
    if (rec.code !== String(code || '').trim() || rec.exp < now()) return fail('Kod hatalı veya süresi dolmuş.', 400);
    await env.KV.delete(`code:${email}`);
    const user = await ensureUser(env, email);
    const token = uid() + uid();
    await env.KV.put(`token:${token}`, email, { expirationTtl: 60 * 60 * 24 * 30 });
    return json({ token, user: await publicUser(env, user.id) });
  }

  const user = await userFromRequest(env, request);
  if (!user) return fail('Oturum süresi doldu. Tekrar giriş yap.', 401);

  if (path === 'me' && request.method === 'GET') return json(await publicUser(env, user.id));
  if (path === 'auth/logout' && request.method === 'POST') return json({ ok: true });

  if (path === 'me/username' && request.method === 'POST') {
    const { username } = await bodyOf(request);
    const name = String(username || '').replace(/^@/, '').trim().toLowerCase();
    if (!/^[a-z0-9_]{3,20}$/.test(name)) return fail('Kullanıcı adı 3-20 karakter, harf/rakam/_ olmalı.', 400);
    const owner = await env.KV.get(`uname:${name}`);
    if (owner && owner !== user.id) return fail('Bu kullanıcı adı alınmış.', 400);
    const old = user.username;
    if (old && old !== name) await env.KV.delete(`uname:${old}`);
    const updated = { ...user, username: name, last: now() };
    await env.KV.put(`user:${user.id}`, JSON.stringify(updated));
    await env.KV.put(`uname:${name}`, user.id);
    return json(await publicUser(env, user.id));
  }

  if (path.startsWith('users/by-username/') && request.method === 'GET') {
    const name = decodeURIComponent(path.slice(18)).toLowerCase();
    const id = await env.KV.get(`uname:${name}`);
    if (!id || id === user.id) return fail('Kullanıcı bulunamadı.', 404);
    return json(await publicUser(env, id));
  }

  if (path.startsWith('users/search') && request.method === 'GET') {
    const q = (url.searchParams.get('q') || '').trim().toLowerCase();
    if (q.length < 2) return json([]);
    const list = await env.KV.list({ prefix: 'uname:' });
    const out = [];
    for (const key of list.keys) {
      if (key.name.slice(6).includes(q)) {
        const id = await env.KV.get(key.name);
        if (id && id !== user.id) out.push(await publicUser(env, id));
      }
      if (out.length >= 30) break;
    }
    return json(out);
  }

  if (path === 'chats' && request.method === 'GET') {
    const raw = await env.KV.get(`chats:${user.id}`);
    const peers = raw ? JSON.parse(raw) : [];
    const out = [];
    for (const peer of peers) {
      const chat = await getChat(env, user.id, peer);
      out.push({ user: await publicUser(env, peer), last: chat[chat.length - 1] || { ts: 0, text: '' }, unread: 0 });
    }
    return json(out);
  }

  let m;
  if ((m = path.match(/^messages\/([^/]+)$/)) && request.method === 'GET') {
    const peerId = m[1];
    const chat = await getChat(env, user.id, peerId);
    const rawBlocks = await env.KV.get(`blocks:${user.id}`);
    const blocks = rawBlocks ? JSON.parse(rawBlocks) : [];
    const rawPeerBlocks = await env.KV.get(`blocks:${peerId}`);
    const peerBlocks = rawPeerBlocks ? JSON.parse(rawPeerBlocks) : [];
    return json({
      messages: chat,
      blocked: blocks.includes(peerId) || peerBlocks.includes(user.id),
      peer: await publicUser(env, peerId),
    });
  }

  if (path === 'search/messages' && request.method === 'GET') {
    const q = (url.searchParams.get('q') || '').toLowerCase();
    const raw = await env.KV.get(`chats:${user.id}`);
    const peers = raw ? JSON.parse(raw) : [];
    const out = [];
    for (const peer of peers) {
      const chat = await getChat(env, user.id, peer);
      for (const msg of chat) {
        if (String(msg.text || '').toLowerCase().includes(q)) out.push({ ...msg, peer: peer });
      }
    }
    return json(out.slice(-50));
  }

  if (path === 'starred' && request.method === 'GET') {
    const raw = await env.KV.get(`chats:${user.id}`);
    const peers = raw ? JSON.parse(raw) : [];
    const out = [];
    for (const peer of peers) {
      for (const msg of await getChat(env, user.id, peer)) if (msg.star) out.push({ ...msg, peer });
    }
    return json(out.slice(-100));
  }

  if (path === 'blocks' && request.method === 'GET') {
    const raw = await env.KV.get(`blocks:${user.id}`);
    if (!raw) return json([]);
    const ids = JSON.parse(raw);
    return json(await Promise.all(ids.map((id) => publicUser(env, id))));
  }

  if ((m = path.match(/^blocks\/([^/]+)$/))) {
    const raw = await env.KV.get(`blocks:${user.id}`);
    let ids = raw ? JSON.parse(raw) : [];
    if (request.method === 'POST' && !ids.includes(m[1])) ids.push(m[1]);
    if (request.method === 'DELETE') ids = ids.filter((id) => id !== m[1]);
    await env.KV.put(`blocks:${user.id}`, JSON.stringify(ids));
    return json({ ok: true });
  }

  if (path === 'report' && request.method === 'POST') return json({ ok: true });

  if ((m = path.match(/^media\/([^/]+)$/)) && request.method === 'GET') {
    const list = await env.KV.list({ prefix: `filemeta:${m[1]}:` });
    const out = [];
    for (const key of list.keys) {
      const meta = await env.KV.get(key.name);
      if (meta) out.push(JSON.parse(meta));
    }
    return json(out);
  }

  if ((path === 'upload' || path === 'upload-file') && request.method === 'POST') {
    const bytes = await request.arrayBuffer();
    if (bytes.byteLength > 8 * 1024 * 1024) return fail('Dosya çok büyük (en fazla 8 MB).', 400);
    const type = request.headers.get('content-type') || 'application/octet-stream';
    const key = `${uid()}`;
    let binary = '';
    const bytes8 = new Uint8Array(bytes);
    for (let i = 0; i < bytes8.length; i += 8192) binary += String.fromCharCode(...bytes8.subarray(i, i + 8192));
    const dataUrl = `data:${type};base64,${btoa(binary)}`;
    await env.KV.put(`file:${key}`, dataUrl);
    await env.KV.put(`filemeta:${user.id}:${key}`, JSON.stringify({
      url: `/files/${key}`, name: path === 'upload' ? 'fotoğraf' : 'dosya', size: bytes.byteLength,
    }));
    return json({ url: `/files/${key}` });
  }

  if (path === 'rtc-config' && request.method === 'GET') {
    return json({ iceServers: [{ urls: 'stun:stun.l.google.com:19302' }] });
  }

  if (path === 'push/status' && request.method === 'GET') return json({ cihaz: 0 });
  if (path === 'push/key' && request.method === 'GET') return json({ key: 'kartsiz-surum' });
  if (path.startsWith('push/') && request.method === 'POST') return json({ ok: true });

  return fail('Bulunamadı.', 404);
}

async function serveFile(request, env, url) {
  const key = decodeURIComponent(url.pathname.slice(7));
  const dataUrl = await env.KV.get(`file:${key}`);
  if (!dataUrl) return new Response('Bulunamadı', { status: 404 });
  const [head, b64] = dataUrl.split(',');
  const type = (head.match(/^data:([^;]+)/) || [])[1] || 'application/octet-stream';
  const binary = atob(b64 || '');
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return new Response(bytes, {
    headers: { 'content-type': type, 'cache-control': 'public, max-age=31536000, immutable' },
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === '/healthz') return json({ ok: true, mode: 'cloudflare-worker' });

    if (url.pathname === '/ws') {
      const room = env.CHAT.get(env.CHAT.idFromName('selam-global'));
      return room.fetch(request);
    }

    if (url.pathname.startsWith('/api/')) return api(request, env, url);
    if (url.pathname.startsWith('/files/')) return serveFile(request, env, url);

    return env.ASSETS.fetch(request);
  },
};

// ---------- Gerçek zamanlı sohbet ----------
export class ChatRoom {
  constructor(state, env) {
    this.state = state;
    this.env = env;
  }

  async fetch(request) {
    const url = new URL(request.url);
    const token = url.searchParams.get('token') || '';
    const email = await this.env.KV.get(`token:${token}`);
    if (!email) return new Response('Yetkisiz', { status: 401 });
    const user = await ensureUser(this.env, email);
    const pair = new WebSocketPair();
    this.state.acceptWebSocket(pair[1]);
    pair[1].serializeAttachment(JSON.stringify({ id: user.id }));
    await this.env.KV.put(`online:${user.id}`, '1');
    await this.broadcastPresence(user.id, true);
    return new Response(null, { status: 101, webSocket: pair[0] });
  }

  attachment(ws) {
    try { return JSON.parse(ws.deserializeAttachment() || '{}'); } catch { return {}; }
  }

  socketsOf(id) {
    return this.state.getWebSockets().filter((ws) => this.attachment(ws).id === id);
  }

  sendTo(id, data) {
    for (const ws of this.socketsOf(id)) {
      try { ws.send(JSON.stringify(data)); } catch {}
    }
  }

  async broadcastPresence(id, online) {
    const raw = await this.env.KV.get(`chats:${id}`);
    const peers = raw ? JSON.parse(raw) : [];
    const u = await publicUser(this.env, id);
    const data = { type: 'presence', id, online, last: u.last, username: u.username };
    for (const peer of peers) this.sendTo(peer, data);
  }

  async webSocketMessage(ws, raw) {
    let d;
    try { d = JSON.parse(raw); } catch { return; }
    const me = this.attachment(ws).id;
    if (!me) return;
    const user = await storedUser(this.env, me);

    if (d.type === 'vis') {
      const online = d.v !== false;
      if (online) await this.env.KV.put(`online:${me}`, '1');
      else await this.env.KV.delete(`online:${me}`);
      await this.broadcastPresence(me, online);
      return;
    }

    if (d.type === 'typing' && d.to) {
      this.sendTo(d.to, { type: 'typing', from: me });
      return;
    }

    if (d.type === 'call' && d.to) {
      this.sendTo(d.to, { ...d, from: me });
      return;
    }

    if (d.type === 'read' && d.peer) {
      const chat = await getChat(this.env, me, d.peer);
      let changed = false;
      for (const msg of chat) {
        if (msg.from === d.peer && !msg.rd) { msg.rd = true; msg.dl = true; changed = true; }
      }
      if (changed) await saveChat(this.env, me, d.peer, chat);
      const ids = chat.filter((x) => x.from === d.peer).map((x) => x.id);
      this.sendTo(d.peer, { type: 'read', reader: me, peer: d.peer, ids });
      this.sendTo(me, { type: 'read', reader: me, peer: d.peer, ids });
      return;
    }

    if (d.type === 'message' && d.to) {
      const msg = {
        id: uid(),
        from: me,
        to: d.to,
        text: String(d.text || '').slice(0, 4000),
        ts: now(),
      };
      for (const k of ['img', 'w', 'h', 'file', 'loc', 're']) if (d[k] !== undefined) msg[k] = d[k];
      const chat = await getChat(this.env, me, d.to);
      chat.push(msg);
      await saveChat(this.env, me, d.to, chat);
      await addChatList(this.env, me, d.to);
      const fromUser = await publicUser(this.env, me);
      const packet = { type: 'message', msg, fromUser };
      this.sendTo(d.to, packet);
      this.sendTo(me, packet);
      if (this.socketsOf(d.to).length) this.sendTo(me, { type: 'delivered', ids: [msg.id] });
      return;
    }

    if (d.type === 'delete' && d.id) {
      const raw = await this.env.KV.get(`chats:${me}`);
      const peers = raw ? JSON.parse(raw) : [];
      for (const peer of peers) {
        const chat = await getChat(this.env, me, peer);
        const target = chat.find((x) => x.id === d.id);
        if (!target) continue;
        const to = target.from === me ? target.to : target.from;
        target.del = true; target.text = '';
        for (const k of ['img', 'file', 'loc', 're', 'rx', 'star', 'w', 'h']) delete target[k];
        await saveChat(this.env, me, peer, chat);
        const back = await getChat(this.env, to, peer);
        const twin = back.find((x) => x.id === d.id);
        if (twin) {
          twin.del = true; twin.text = '';
          for (const k of ['img', 'file', 'loc', 're', 'rx', 'star', 'w', 'h']) delete twin[k];
          await saveChat(this.env, to, peer, back);
        }
        this.sendTo(peer, { type: 'deleted', id: d.id });
        this.sendTo(to, { type: 'deleted', id: d.id });
      }
      return;
    }

    if (d.type === 'react' && d.id) {
      const raw = await this.env.KV.get(`chats:${me}`);
      const peers = raw ? JSON.parse(raw) : [];
      for (const peer of peers) {
        const chat = await getChat(this.env, me, peer);
        const target = chat.find((x) => x.id === d.id);
        if (!target) continue;
        target.rx = d.e;
        await saveChat(this.env, me, peer, chat);
        this.sendTo(peer, { type: 'reacted', id: d.id, rx: d.e });
        this.sendTo(me, { type: 'reacted', id: d.id, rx: d.e });
      }
      return;
    }

    if (d.type === 'star' && d.id) {
      const raw = await this.env.KV.get(`chats:${me}`);
      const peers = raw ? JSON.parse(raw) : [];
      for (const peer of peers) {
        const chat = await getChat(this.env, me, peer);
        const target = chat.find((x) => x.id === d.id);
        if (!target) continue;
        target.star = !!d.on;
        await saveChat(this.env, me, peer, chat);
        this.sendTo(me, { type: 'starred', id: d.id, on: !!d.on });
      }
      return;
    }
  }

  async webSocketClose(ws) {
    const me = this.attachment(ws).id;
    if (!me) return;
    if (this.socketsOf(me).length === 0) {
      await this.env.KV.delete(`online:${me}`);
      const u = await storedUser(this.env, me);
      if (u) {
        u.last = now();
        await this.env.KV.put(`user:${me}`, JSON.stringify(u));
      }
      await this.broadcastPresence(me, false);
    }
  }
}
