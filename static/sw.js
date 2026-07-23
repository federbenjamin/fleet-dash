const SHELL_CACHE = 'fleet-shell-n7-v1';
const RUNTIME_CACHE = 'fleet-runtime-n6-v1';
const SHELL_ASSETS = [
  '/',
  '/static/fleet.css',
  '/static/js/main.js',
  '/static/js/state-store.js',
  '/static/js/nav.js',
  '/static/js/search.js',
  '/static/js/ui-utils.js',
  '/static/js/outbox.js',
  '/static/js/push.js',
  '/static/js/notifications.js',
  '/static/js/context.js',
  '/static/js/viewer-handoff.js',
  '/static/js/overlays.js',
  '/static/js/workspace.js',
  '/static/js/cards.js',
  '/static/js/settings-actions.js',
  '/static/js/history-spawn.js',
  '/static/js/insights.js',
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
    caches.keys().then(keys => Promise.all(keys.filter(key => ![SHELL_CACHE,RUNTIME_CACHE].includes(key))
      .map(key => caches.delete(key)))),
    self.clients.claim()
  ]));
});

self.addEventListener('fetch', event => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname === '/api/fleet') {
    event.respondWith((async () => {
      const cache = await caches.open(RUNTIME_CACHE);
      try {
        const response = await fetch(request);
        if (response.ok) {
          event.waitUntil(cache.put('/api/fleet', response.clone()));
          return response;
        }
        const cached = await cache.match('/api/fleet');
        if (!cached) return response;
        const headers = new Headers(cached.headers);
        headers.set('X-Fleet-Offline', '1');headers.delete('Content-Length');
        return new Response(await cached.blob(), {status: 200, headers});
      } catch (_) {
        const cached = await cache.match('/api/fleet');
        if (!cached) return new Response(JSON.stringify({ok:false,error:'Fleet is offline and has no cached snapshot'}),
          {status:503,headers:{'Content-Type':'application/json'}});
        const headers = new Headers(cached.headers);
        headers.set('X-Fleet-Offline', '1');headers.delete('Content-Length');
        return new Response(await cached.blob(), {status: 200, headers});
      }
    })());
    return;
  }
  if (url.pathname.startsWith('/api/')) return;
  if (request.mode === 'navigate') {
    event.respondWith(fetch(request).then(response => {
      if (response.ok) event.waitUntil(caches.open(SHELL_CACHE).then(cache => cache.put('/', response.clone())));
      return response;
    }).catch(async () => (await caches.match('/')) || caches.match('/static/offline.html')));
    return;
  }
  if (!url.search && !url.hash && SHELL_PATHS.has(url.pathname)) {
    event.respondWith(fetch(request).then(response => {
      if (response.ok) {
        const copy = response.clone();
        event.waitUntil(caches.open(SHELL_CACHE).then(cache => cache.put(url.pathname, copy)));
      }
      return response;
    }).catch(() => caches.match(url.pathname)));
  }
});

function boundedText(value, limit) {
  return typeof value === 'string' && value.length <= limit ? value : '';
}

function genericCopy(kind, title, body) {
  if (['question','approval','form','reply'].includes(kind))
    return title === 'Fleet needs you' && body === 'A coding session needs your response.';
  if (kind === 'stall')
    return title === 'Fleet needs attention' && body === 'A coding session may be stalled.';
  if (kind === 'failure')
    return title === 'Fleet needs attention' && body === 'A provider or delivery needs review.';
  return kind === 'notification' && (
    (title === 'Fleet notification test' && body === 'Web Push delivery is working.') ||
    (title === 'Fleet needs attention' && body === 'A provider or delivery needs review.'));
}

function payloadFrom(event) {
  if (!event.data) return null;
  let payload,raw;
  try {
    raw = event.data.text();
    if (new TextEncoder().encode(raw).length > 2048) return null;
    payload = JSON.parse(raw);
  } catch (_) { return null; }
  if (!payload || payload.version !== 1) return null;
  const eventId = boundedText(payload.event_id, 100);
  const kind = boundedText(payload.kind, 20);
  const title = boundedText(payload.title, 80);
  const body = boundedText(payload.body, 180);
  const tag = boundedText(payload.tag, 24);
  if (!eventId || !title || !body || !/^evt-[A-Za-z0-9_-]+$/.test(eventId)
      || !['question','approval','form','reply','failure','stall','notification'].includes(kind)
      || !genericCopy(kind,title,body)
      || !/^[0-9a-f]{24}$/.test(tag)) return null;
  const target = new URL('/#notifications/' + encodeURIComponent(eventId), self.location.origin);
  let supplied;
  try { supplied = new URL(payload.url, self.location.origin); } catch (_) { return null; }
  if (supplied.origin !== self.location.origin || supplied.pathname !== '/' || supplied.search ||
      supplied.hash !== '#notifications/' + encodeURIComponent(eventId)) return null;
  if (!Array.isArray(payload.actions) || payload.actions.length > 2 ||
      new Set(payload.actions).size !== payload.actions.length ||
      payload.actions.some(action => !['snooze','mute'].includes(action))) return null;
  if (!payload.capabilities || typeof payload.capabilities !== 'object' ||
      Array.isArray(payload.capabilities) ||
      Object.keys(payload.capabilities).some(action => !payload.actions.includes(action)) ||
      Object.keys(payload.capabilities).length !== payload.actions.length) return null;
  const capabilities = {};
  for (const action of payload.actions) {
    const token = boundedText(payload.capabilities[action], 2048);
    if (!token) return null;
    capabilities[action] = token;
  }
  return {eventId, kind, title, body, tag, target: target.href, capabilities,
    unread: Number.isInteger(payload.unread) && payload.unread >= 0 && payload.unread <= 999
      ? payload.unread : null,
    cursor: Number.isInteger(payload.cursor) && payload.cursor >= 0 ? payload.cursor : null};
}

let badgeCursor = 0;
function applyBadge(unread,cursor) {
  if (unread === null || cursor === null || cursor < badgeCursor || !self.navigator) return Promise.resolve();
  badgeCursor = cursor;
  if (unread === 0 && self.navigator.clearAppBadge) return self.navigator.clearAppBadge();
  if (self.navigator.setAppBadge) return self.navigator.setAppBadge(unread);
  return Promise.resolve();
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
      tag: 'fleet:' + payload.tag,
      renotify: false,
      requireInteraction: true,
      actions,
      data: {eventId: payload.eventId, target: payload.target,
        capabilities: payload.capabilities}
    }),
    applyBadge(payload.unread,payload.cursor)
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
    credentials: 'omit',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({capability})
  }).then(async response => {
    if (!response.ok) throw new Error('capability rejected');
    const result = await response.json();
    if (!result.ok) throw new Error('capability rejected');
    event.notification.close();
  }).catch(() => {
    const fallback = new URL(target);
    fallback.searchParams.set('push_action',event.action);
    return openFleet(fallback.href);
  }));
});

self.addEventListener('notificationclose', () => {});

self.addEventListener('message', event => {
  const data = event.data || {};
  if (data.type === 'fleet-displayed-notifications') {
    event.waitUntil(self.registration.getNotifications().then(notifications => {
      const ids = notifications.map(item => item.data && item.data.eventId).filter(Boolean).slice(0,20);
      if (event.ports && event.ports[0]) event.ports[0].postMessage({eventIds: ids});
    }));
    return;
  }
  if (data.type !== 'fleet-notification-state' || !Array.isArray(data.resolvedIds)) return;
  const resolved = new Set(data.resolvedIds.filter(id => typeof id === 'string' &&
    /^evt-[A-Za-z0-9_-]+$/.test(id)).slice(0,20));
  event.waitUntil(self.registration.getNotifications().then(notifications => notifications.forEach(item => {
    if (resolved.has(item.data && item.data.eventId)) item.close();
  })));
});

self.addEventListener('pushsubscriptionchange', event => {
  event.waitUntil(self.clients.matchAll({type: 'window', includeUncontrolled: true})
    .then(windows => windows.forEach(client => client.postMessage({type: 'push-subscription-change'}))));
});
