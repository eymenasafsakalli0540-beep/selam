const V = 'selam-v3';
const SHELL = ['/', '/manifest.webmanifest', '/icon-192.png', '/icon-512.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(V).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== V).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const r = e.request, u = new URL(r.url);
  if (r.method !== 'GET' || u.origin !== location.origin) return;
  if (u.pathname.startsWith('/api') || u.pathname.startsWith('/ws') || u.pathname.startsWith('/uploads')) return;
  if (r.mode === 'navigate') {
    e.respondWith(fetch(r).then(res => { const cp = res.clone(); caches.open(V).then(c => c.put('/', cp)); return res; }).catch(() => caches.match('/')));
    return;
  }
  e.respondWith(caches.match(r).then(h => h || fetch(r)));
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  e.waitUntil(self.clients.matchAll({ type: 'window' }).then(l => (l.length ? l[0].focus() : self.clients.openWindow('/'))));
});

self.addEventListener('push', e => {
  let d = {}; try { d = e.data.json(); } catch {}
  const call = d.t === 'call';
  e.waitUntil(self.registration.showNotification(d.title || 'Selam', {
    body: d.body || '', icon: '/icon-192.png', badge: '/icon-192.png', tag: d.tag || 'selam', renotify: true,
    requireInteraction: call, vibrate: call ? [400, 200, 400, 200, 400] : [120],
  }));
});

// Abonelik tarayıcı tarafından yenilenirse açık sekmeye haber ver, o da sunucuya tekrar kaydetsin.
self.addEventListener('pushsubscriptionchange', e => {
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true })
    .then(cs => cs.forEach(c => c.postMessage({ type: 'resub' }))));
});
