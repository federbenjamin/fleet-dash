#!/usr/bin/env node
'use strict';

const dns = require('node:dns').promises;
const https = require('node:https');
const net = require('node:net');
const webPush = require('web-push');

const MAX_INPUT = 16_384;
const MAX_OUTPUT = 4_096;
const BUILTIN_ORIGINS = new Set([
  'https://fcm.googleapis.com',
  'https://updates.push.services.mozilla.com',
  'https://web.push.apple.com',
]);
const GENERATED_HEADERS = new Set([
  'authorization', 'content-encoding', 'content-length', 'content-type',
  'crypto-key', 'encryption', 'ttl', 'urgency', 'topic',
]);

function fail(message) {
  const error = new Error(message);
  error.safe = true;
  throw error;
}

function exactKeys(value, allowed) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) fail('invalid object');
  for (const key of Object.keys(value)) if (!allowed.has(key)) fail('unknown field');
}

function decodedKey(value, bytes, label) {
  if (typeof value !== 'string' || !/^[A-Za-z0-9_-]+$/.test(value)) fail(`invalid ${label}`);
  const decoded = Buffer.from(value, 'base64url');
  if (decoded.length !== bytes) fail(`invalid ${label}`);
  return decoded;
}

function normalizeExtraOrigin(value) {
  let url;
  try { url = new URL(String(value)); } catch (_) { return null; }
  if (url.protocol !== 'https:' || url.port && url.port !== '443' || url.username
      || url.password || url.pathname !== '/' || url.search || url.hash) return null;
  return `https://${url.hostname.toLowerCase()}`;
}

function validateSubscription(value, extraOrigins = []) {
  exactKeys(value, new Set(['endpoint', 'expirationTime', 'keys']));
  if (typeof value.endpoint !== 'string' || value.endpoint.length > 2_048
      || /[\x00-\x20\x7f]/.test(value.endpoint)) fail('invalid endpoint');
  let endpoint;
  try { endpoint = new URL(value.endpoint); } catch (_) { fail('invalid endpoint'); }
  const host = endpoint.hostname.toLowerCase().replace(/\.$/, '');
  if (endpoint.protocol !== 'https:' || endpoint.port && endpoint.port !== '443'
      || endpoint.username || endpoint.password || endpoint.hash || !host
      || host.length > 253 || endpoint.pathname.length > 1_536
      || endpoint.search.length > 1_025 || net.isIP(host)) fail('invalid endpoint');
  const allowed = new Set(BUILTIN_ORIGINS);
  for (const item of extraOrigins) {
    const normalized = normalizeExtraOrigin(item);
    if (normalized) allowed.add(normalized);
  }
  const origin = `https://${host}`;
  if (!allowed.has(origin) && !host.endsWith('.notify.windows.com')) fail('endpoint not allowed');
  exactKeys(value.keys, new Set(['p256dh', 'auth']));
  const p256dh = decodedKey(value.keys.p256dh, 65, 'p256dh');
  if (p256dh[0] !== 4) fail('invalid p256dh');
  decodedKey(value.keys.auth, 16, 'auth');
  if (value.expirationTime !== null && value.expirationTime !== undefined
      && (!Number.isFinite(value.expirationTime) || value.expirationTime < 0
          || value.expirationTime > 9e15)) fail('invalid expiration');
  return {endpoint: value.endpoint, expirationTime: value.expirationTime ?? null,
    keys: {p256dh: value.keys.p256dh, auth: value.keys.auth}};
}

function ipv4Parts(address) {
  const parts = address.split('.').map(Number);
  return parts.length === 4 && parts.every(part => Number.isInteger(part) && part >= 0 && part <= 255)
    ? parts : null;
}

function isGlobalAddress(address) {
  const family = net.isIP(address);
  if (family === 4) {
    const p = ipv4Parts(address);
    if (!p) return false;
    if (p[0] === 0 || p[0] === 10 || p[0] === 127 || p[0] >= 224
        || (p[0] === 100 && p[1] >= 64 && p[1] <= 127)
        || (p[0] === 169 && p[1] === 254)
        || (p[0] === 172 && p[1] >= 16 && p[1] <= 31)
        || (p[0] === 192 && p[1] === 168)
        || (p[0] === 192 && p[1] === 0 && p[2] === 0)
        || (p[0] === 192 && p[1] === 0 && p[2] === 2)
        || (p[0] === 198 && (p[1] === 18 || p[1] === 19))
        || (p[0] === 198 && p[1] === 51 && p[2] === 100)
        || (p[0] === 203 && p[1] === 0 && p[2] === 113)) return false;
    return true;
  }
  if (family === 6) {
    const normalized = address.toLowerCase().split('%')[0];
    if (normalized === '::' || normalized === '::1' || normalized.startsWith('fc')
        || normalized.startsWith('fd') || /^fe[89ab]/.test(normalized)
        || normalized.startsWith('ff') || normalized.startsWith('2001:db8:')) return false;
    if (normalized.startsWith('::ffff:')) {
      const mapped = normalized.slice(7);
      return net.isIP(mapped) === 4 && isGlobalAddress(mapped);
    }
    return true;
  }
  return false;
}

async function resolveGlobal(hostname, lookup = dns.lookup) {
  const records = await lookup(hostname, {all: true, verbatim: true});
  const global = records.filter(record => isGlobalAddress(record.address));
  if (!global.length) fail('endpoint did not resolve to a global address');
  // Apple commonly returns AAAA records first. Pinning that first record breaks
  // delivery on otherwise-online Macs without an IPv6 default route. Prefer a
  // validated IPv4 address when both families exist, while retaining an IPv6
  // fallback for IPv6-only networks.
  return global.find(record => record.family === 4) || global[0];
}

function pinnedLookup(record) {
  return (_host, options, callback) => {
    if (options && options.all) callback(null, [record]);
    else callback(null, record.address, record.family);
  };
}

function retryAfter(value, now = Date.now()) {
  if (typeof value !== 'string' || value.length > 80) return null;
  if (/^\d+$/.test(value)) return Math.min(600, Number(value));
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed)) return null;
  return Math.min(600, Math.max(0, Math.ceil((parsed - now) / 1000)));
}

function validatePayload(value) {
  if (typeof value !== 'string' || Buffer.byteLength(value) > 2_048) fail('invalid payload');
  let parsed;
  try { parsed = JSON.parse(value); } catch (_) { fail('invalid payload'); }
  exactKeys(parsed, new Set(['version', 'event_id', 'title', 'body', 'url', 'unread', 'capabilities']));
  if (parsed.version !== 1 || typeof parsed.event_id !== 'string' || parsed.event_id.length > 100
      || typeof parsed.title !== 'string' || parsed.title.length > 80
      || typeof parsed.body !== 'string' || parsed.body.length > 180
      || typeof parsed.url !== 'string' || parsed.url.length > 180
      || !Number.isInteger(parsed.unread) || parsed.unread < 0 || parsed.unread > 999) {
    fail('invalid payload');
  }
  if (parsed.capabilities !== undefined) {
    exactKeys(parsed.capabilities, new Set(['snooze', 'mute']));
    for (const value of Object.values(parsed.capabilities)) {
      if (typeof value !== 'string' || value.length > 2_048) fail('invalid capability');
    }
  }
  return value;
}

function requestOnce(options, body, request = https.request) {
  return new Promise(resolve => {
    let settled = false;
    const finish = result => { if (!settled) { settled = true; resolve(result); } };
    const outgoing = request(options, response => {
      let bytes = 0;
      response.on('data', chunk => {
        bytes += chunk.length;
        if (bytes > MAX_OUTPUT) response.destroy();
      });
      response.on('end', () => {
        const status = Number(response.statusCode || 0);
        finish({type: 'result', ok: status >= 200 && status < 300, status,
          retry_after: retryAfter(response.headers['retry-after']), code: `http_${status}`});
      });
      response.on('error', () => finish({type: 'result', ok: false,
        retryable: true, code: 'response_error'}));
    });
    outgoing.setTimeout(10_000, () => {
      outgoing.destroy();
      finish({type: 'result', ok: false, retryable: true, code: 'timeout'});
    });
    outgoing.on('error', () => finish({type: 'result', ok: false,
      retryable: true, code: 'network_error'}));
    outgoing.end(body);
  });
}

async function sendJob(job, state, dependencies = {}) {
  exactKeys(job, new Set(['type', 'request_id', 'subscription', 'payload', 'ttl', 'urgency', 'topic']));
  if (job.type !== 'send' || typeof job.request_id !== 'string' || job.request_id.length > 100
      || !Number.isInteger(job.ttl) || job.ttl < 1 || job.ttl > 86_400
      || !['very-low', 'low', 'normal', 'high'].includes(job.urgency)
      || typeof job.topic !== 'string' || !/^[A-Za-z0-9_-]{1,32}$/.test(job.topic)) fail('invalid job');
  const subscription = validateSubscription(job.subscription, state.allowedOrigins);
  const payload = validatePayload(job.payload);
  const endpoint = new URL(subscription.endpoint);
  const resolved = await resolveGlobal(endpoint.hostname, dependencies.lookup || dns.lookup);
  const details = webPush.generateRequestDetails(subscription, payload, {
    vapidDetails: {subject: state.subject, publicKey: state.publicKey,
      privateKey: state.privateKey},
    TTL: job.ttl, urgency: job.urgency, topic: job.topic,
  });
  if (details.endpoint !== subscription.endpoint || details.method !== 'POST') fail('request mismatch');
  const headers = {};
  for (const [key, value] of Object.entries(details.headers || {})) {
    if (GENERATED_HEADERS.has(key.toLowerCase())) headers[key] = value;
  }
  const options = {protocol: 'https:', hostname: endpoint.hostname, port: 443,
    path: endpoint.pathname + endpoint.search, method: 'POST', headers,
    lookup: pinnedLookup(resolved),
    servername: endpoint.hostname, rejectUnauthorized: true};
  return requestOnce(options, details.body, dependencies.request || https.request);
}

function initialize(message) {
  exactKeys(message, new Set(['type', 'request_id', 'vapid', 'subject', 'allowed_origins']));
  if (message.type !== 'init' || message.request_id !== 'init') fail('invalid initialization');
  exactKeys(message.vapid, new Set(['public_key', 'private_key']));
  decodedKey(message.vapid.public_key, 65, 'VAPID public key');
  decodedKey(message.vapid.private_key, 32, 'VAPID private key');
  if (typeof message.subject !== 'string' || message.subject.length > 320) fail('invalid subject');
  const subject = new URL(message.subject);
  if (!['https:', 'mailto:'].includes(subject.protocol)) fail('invalid subject');
  if (!Array.isArray(message.allowed_origins) || message.allowed_origins.length > 20) fail('invalid origins');
  const allowedOrigins = message.allowed_origins.map(normalizeExtraOrigin);
  if (allowedOrigins.some(value => !value)) fail('invalid origins');
  return {publicKey: message.vapid.public_key, privateKey: message.vapid.private_key,
    subject: message.subject, allowedOrigins};
}

function emit(value) {
  const line = JSON.stringify(value);
  if (Buffer.byteLength(line) > MAX_OUTPUT) process.exit(2);
  process.stdout.write(line + '\n');
}

async function runProtocol() {
  let buffer = Buffer.alloc(0);
  let state = null;
  let chain = Promise.resolve();
  process.stdin.on('data', chunk => {
    buffer = Buffer.concat([buffer, chunk]);
    if (buffer.length > MAX_INPUT && buffer.indexOf(10) < 0) process.exit(2);
    let newline;
    while ((newline = buffer.indexOf(10)) >= 0) {
      const line = buffer.subarray(0, newline);
      buffer = buffer.subarray(newline + 1);
      if (!line.length || line.length > MAX_INPUT) process.exit(2);
      chain = chain.then(async () => {
        let message;
        try { message = JSON.parse(line.toString('utf8')); } catch (_) { process.exit(2); }
        try {
          if (!state) {
            state = initialize(message);
            emit({type: 'ready', request_id: message.request_id});
          } else {
            const result = await sendJob(message, state);
            emit({...result, request_id: message.request_id});
          }
        } catch (error) {
          emit({type: 'result', request_id: message && message.request_id,
            ok: false, retryable: false, code: error.safe ? 'invalid_job' : 'helper_error'});
        }
      });
    }
  });
  process.stdin.on('end', () => chain.finally(() => process.exit(0)));
}

if (require.main === module) {
  if (process.argv.includes('--generate')) {
    const keys = webPush.generateVAPIDKeys();
    emit({public_key: keys.publicKey, private_key: keys.privateKey});
  } else {
    runProtocol();
  }
}

module.exports = {initialize, isGlobalAddress, normalizeExtraOrigin, pinnedLookup, resolveGlobal,
  retryAfter, sendJob, validatePayload, validateSubscription};
