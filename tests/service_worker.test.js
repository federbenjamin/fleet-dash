const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'sw.js'), 'utf8');

function payload(overrides = {}) {
  return JSON.stringify({version: 1, event_id: 'evt-safe', kind: 'question',
    title: 'Fleet needs you', body: 'A coding session needs your response.',
    tag: '0123456789abcdef01234567', url: '/#notifications/evt-safe',
    actions: ['snooze', 'mute'], capabilities: {snooze: 'signed-snooze', mute: 'signed-mute'},
    unread: 2, cursor: 8, ...overrides});
}

function harness(fetchImpl = async () => ({ok: true, json: async () => ({ok: true})})) {
  const listeners = new Map(), shown = [], opened = [], badge = [], displayed = [], cacheWrites = [];
  const self = {
    location: {origin: 'https://fleet.test'},
    addEventListener(type, listener) { listeners.set(type, listener); },
    skipWaiting: async () => {},
    navigator: {
      setAppBadge: async value => badge.push(value),
      clearAppBadge: async () => badge.push(0)
    },
    registration: {
      showNotification: async (title, options) => shown.push({title, options}),
      getNotifications: async () => displayed
    },
    clients: {
      claim: async () => {}, matchAll: async () => [],
      openWindow: async target => opened.push(target)
    }
  };
  const context = {self, URL, URLSearchParams, TextEncoder, Set, Promise,
    fetch: fetchImpl, caches: {open: async () => ({
      put: async key => cacheWrites.push(typeof key === 'string' ? key : key.url)}),
      keys: async () => [], match: async () => null}, console};
  vm.runInNewContext(source, context, {filename: 'sw.js'});
  async function dispatch(type, event = {}) {
    const waits = [],responses = [];
    event.waitUntil = promise => waits.push(Promise.resolve(promise));
    event.respondWith = promise => responses.push(Promise.resolve(promise));
    listeners.get(type)(event);
    await Promise.all(responses);await Promise.all(waits);
  }
  return {dispatch, shown, opened, badge, displayed, cacheWrites};
}

test('push validates and renders only the generic exact-event snapshot', async () => {
  const app = harness();
  await app.dispatch('push', {data: {text: () => payload()}});
  assert.equal(app.shown.length, 1);
  assert.deepEqual(app.shown[0].title, 'Fleet needs you');
  assert.equal(app.shown[0].options.tag, 'fleet:0123456789abcdef01234567');
  assert.deepEqual(Array.from(app.shown[0].options.actions, item => item.action),
    ['snooze', 'mute']);
  assert.equal(app.shown[0].options.data.target,
    'https://fleet.test/#notifications/evt-safe');
  assert.deepEqual(app.badge, [2]);

  await app.dispatch('push', {data: {text: () => payload({
    url: '/#notifications/evt-other', body: 'private /path main $84 account'})}});
  await app.dispatch('push', {data: {text: () => payload({
    body: 'A private prompt that still uses a valid exact event URL.'})}});
  await app.dispatch('push', {data: {text: () => 'x'.repeat(2049)}});
  assert.equal(app.shown.length, 1);
});

test('shell caching rejects query-bearing keys', async () => {
  const response = {ok: true, clone: () => response};
  const app = harness(async () => response);
  await app.dispatch('fetch', {request: {method: 'GET', mode: 'no-cors',
    url: 'https://fleet.test/static/app.js?token=private'}});
  assert.deepEqual(app.cacheWrites, []);
  await app.dispatch('fetch', {request: {method: 'GET', mode: 'no-cors',
    url: 'https://fleet.test/static/app.js'}});
  assert.deepEqual(app.cacheWrites, ['/static/app.js']);
});

test('capability action omits credentials and closes only after success', async () => {
  let request;
  const app = harness(async (url, options) => {
    request = {url, options}; return {ok: true, json: async () => ({ok: true})};
  });
  let closed = 0;
  await app.dispatch('notificationclick', {action: 'snooze', notification: {
    data: {eventId: 'evt-safe', target: 'https://fleet.test/#notifications/evt-safe',
      capabilities: {snooze: 'signed-snooze'}}, close: () => { closed += 1; }
  }});
  assert.equal(request.url, '/api/push/capability-action');
  assert.equal(request.options.credentials, 'omit');
  assert.deepEqual(JSON.parse(request.options.body), {capability: 'signed-snooze'});
  assert.equal(closed, 1);
});

test('failed capability stays visible and opens exact recoverable fallback', async () => {
  const app = harness(async () => { throw new Error('offline'); });
  let closed = 0;
  await app.dispatch('notificationclick', {action: 'mute', notification: {
    data: {eventId: 'evt-safe', target: 'https://fleet.test/#notifications/evt-safe',
      capabilities: {mute: 'signed-mute'}}, close: () => { closed += 1; }
  }});
  assert.equal(closed, 0);
  assert.equal(app.opened.length, 1);
  assert.match(app.opened[0], /\?push_action=mute#notifications\/evt-safe$/);
  assert.doesNotMatch(app.opened[0], /signed-mute/);
});

test('canonical reconciliation closes only explicitly resolved events', async () => {
  const app = harness();
  let activeClosed = 0, resolvedClosed = 0;
  app.displayed.push(
    {data: {eventId: 'evt-active'}, close: () => { activeClosed += 1; }},
    {data: {eventId: 'evt-resolved'}, close: () => { resolvedClosed += 1; }});
  await app.dispatch('message', {data: {type: 'fleet-notification-state',
    resolvedIds: ['evt-resolved'], unread: 0, cursor: 9}});
  assert.equal(activeClosed, 0);
  assert.equal(resolvedClosed, 1);
  assert.deepEqual(app.badge, []);
});
