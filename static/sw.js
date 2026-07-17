const SHELL_CACHE = 'fleet-shell-n2-v1';
const SHELL_ASSETS = [
  '/static/fleet.css',
  '/static/app.js',
  '/static/manifest.webmanifest',
  '/static/offline.html',
  '/static/icons/fleet.svg',
  '/static/icons/fleet-192.png',
  '/static/icons/fleet-512.png',
  '/static/icons/fleet-maskable-512.png'
];
const SHELL_PATHS = new Set(SHELL_ASSETS);

self.addEventListener('install', event => {
  event.waitUntil(caches.open(SHELL_CACHE).then(cache => Promise.all(
    SHELL_ASSETS.map(path => fetch(path, {cache: 'reload'}).then(response => {
      if (!response.ok) throw new Error('shell asset unavailable');
      return cache.put(path, response);
    }))
  )).then(() => self.skipWaiting()));
});

self.addEventListener('activate', event => {
  event.waitUntil(Promise.all([
    caches.keys().then(keys => Promise.all(keys.filter(key => key !== SHELL_CACHE)
      .map(key => caches.delete(key)))),
    self.clients.claim()
  ]));
});

self.addEventListener('fetch', event => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin || url.pathname.startsWith('/api/')) return;
  if (request.mode === 'navigate') {
    event.respondWith(fetch(request).catch(() => caches.match('/static/offline.html')));
    return;
  }
  if (SHELL_PATHS.has(url.pathname)) {
    event.respondWith(caches.match(url.pathname).then(cached => cached || fetch(request)));
  }
});

function boundedText(value, limit) {
  return typeof value === 'string' && value.length <= limit ? value : '';
}

function payloadFrom(event) {
  if (!event.data) return null;
  let payload;
  try { payload = event.data.json(); } catch (_) { return null; }
  if (!payload || payload.version !== 1) return null;
  const eventId = boundedText(payload.event_id, 100);
  const title = boundedText(payload.title, 80);
  const body = boundedText(payload.body, 180);
  if (!eventId || !title || !body || !/^evt-[A-Za-z0-9_-]+$/.test(eventId)) return null;
  const target = new URL('/#notifications/' + encodeURIComponent(eventId), self.location.origin);
  if (payload.url) {
    let supplied;
    try { supplied = new URL(payload.url, self.location.origin); } catch (_) { return null; }
    if (supplied.origin !== self.location.origin || supplied.pathname !== '/' ||
        !supplied.hash.startsWith('#notifications/')) return null;
    target.hash = supplied.hash;
  }
  const capabilities = {};
  for (const action of ['snooze', 'mute']) {
    const token = payload.capabilities && boundedText(payload.capabilities[action], 2048);
    if (token) capabilities[action] = token;
  }
  return {eventId, title, body, target: target.href, capabilities,
    unread: Number.isInteger(payload.unread) && payload.unread >= 0 && payload.unread <= 999
      ? payload.unread : null};
}

self.addEventListener('push', event => {
  const payload = payloadFrom(event);
  if (!payload) return;
  const actions = Object.keys(payload.capabilities).map(action => ({
    action,
    title: action === 'snooze' ? 'Snooze 15m' : 'Mute session'
  }));
  event.waitUntil(Promise.all([
    self.registration.showNotification(payload.title, {
      body: payload.body,
      icon: '/static/icons/fleet-192.png',
      badge: '/static/icons/fleet-192.png',
      tag: 'fleet:' + payload.eventId,
      renotify: false,
      requireInteraction: true,
      actions,
      data: {eventId: payload.eventId, target: payload.target,
        capabilities: payload.capabilities}
    }),
    payload.unread !== null && self.navigator && self.navigator.setAppBadge
      ? self.navigator.setAppBadge(payload.unread) : Promise.resolve()
  ]));
});

async function openFleet(target) {
  const windows = await self.clients.matchAll({type: 'window', includeUncontrolled: true});
  for (const client of windows) {
    if (new URL(client.url).origin === self.location.origin) {
      if ('navigate' in client) await client.navigate(target);
      return client.focus();
    }
  }
  return self.clients.openWindow(target);
}

self.addEventListener('notificationclick', event => {
  const data = event.notification.data || {};
  const target = data.target || '/#notifications/' + encodeURIComponent(data.eventId || '');
  const capability = event.action && data.capabilities && data.capabilities[event.action];
  if (!capability) {
    event.notification.close();
    event.waitUntil(openFleet(target));
    return;
  }
  event.waitUntil(fetch('/api/push/capability-action', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({capability})
  }).then(async response => {
    if (!response.ok) throw new Error('capability rejected');
    const result = await response.json();
    if (!result.ok) throw new Error('capability rejected');
    event.notification.close();
  }).catch(() => openFleet(target)));
});

self.addEventListener('pushsubscriptionchange', event => {
  event.waitUntil(self.clients.matchAll({type: 'window', includeUncontrolled: true})
    .then(windows => windows.forEach(client => client.postMessage({type: 'push-subscription-change'}))));
});
