#!/usr/bin/env node
/**
 * Disposable Web Push compatibility probe for M12/N0.
 *
 * Default: serves a tiny installable PWA on 127.0.0.1:8391. It keeps the
 * subscription and VAPID private key in memory, never logs them, and sends
 * only an explicit user-requested test notification.
 *
 *   node tests/web_push_probe.mjs
 *   node tests/web_push_probe.mjs --self-test
 */
import { createECDH, randomBytes } from 'node:crypto';
import http from 'node:http';
import process from 'node:process';
import webPush from 'web-push';

const SELF_TEST = process.argv.includes('--self-test');
const PORT_ARG = process.argv.indexOf('--port');
const PORT = PORT_ARG >= 0 ? Number(process.argv[PORT_ARG + 1]) : 8391;
const HOST_ARG = process.argv.indexOf('--host');
const HOST = HOST_ARG >= 0 ? process.argv[HOST_ARG + 1] : '127.0.0.1';
const SUBJECT_ARG = process.argv.indexOf('--subject');
const SUBJECT = SUBJECT_ARG >= 0
  ? process.argv[SUBJECT_ARG + 1]
  : 'mailto:fleet-dash@localhost.invalid';

const vapid = webPush.generateVAPIDKeys();
let subscription = null;
let lastResult = { state: 'not_registered' };

const json = value => Buffer.from(JSON.stringify(value));
const reply = (response, status, contentType, body) => {
  response.writeHead(status, {
    'Content-Type': contentType,
    'Content-Length': body.length,
    'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff',
  });
  response.end(body);
};

const readJson = request => new Promise((resolve, reject) => {
  let size = 0;
  const chunks = [];
  request.on('data', chunk => {
    size += chunk.length;
    if (size > 16_384) {
      reject(new Error('request too large'));
      request.destroy();
      return;
    }
    chunks.push(chunk);
  });
  request.on('end', () => {
    try {
      resolve(JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}'));
    } catch {
      reject(new Error('bad json'));
    }
  });
  request.on('error', reject);
});

const HTML = Buffer.from(`<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#15181d">
<link rel="manifest" href="/manifest.webmanifest">
<title>Fleet Web Push probe</title>
<style>
:root{color-scheme:dark;font:16px/1.45 ui-sans-serif,-apple-system,BlinkMacSystemFont,sans-serif;background:#15181d;color:#f3f5f7}
body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;box-sizing:border-box}
main{width:min(560px,100%);border:1px solid #343b45;border-radius:18px;background:#1d2128;padding:24px;box-shadow:0 24px 70px #0008}
h1{font-size:24px;margin:0 0 6px}p{color:#adb7c4;margin:0 0 20px}button{appearance:none;border:0;border-radius:10px;padding:12px 16px;margin:0 8px 8px 0;background:#77d5b5;color:#102019;font-weight:750;font-size:15px}button:disabled{opacity:.45}#send{background:#a7b8ff;color:#151a31}pre{white-space:pre-wrap;background:#14171c;border-radius:10px;padding:14px;min-height:46px;color:#cbd4df}
</style></head><body><main>
<h1>Fleet Web Push probe</h1>
<p>Install this page, enable notifications, close it, then send one minimal test.</p>
<button id="enable">Enable notifications</button><button id="send" disabled>Send test</button>
<pre id="status">Not registered</pre>
<script>
const status=document.getElementById('status'),send=document.getElementById('send');
const b64=value=>{const pad='='.repeat((4-value.length%4)%4);const raw=atob((value+pad).replace(/-/g,'+').replace(/_/g,'/'));return Uint8Array.from(raw,c=>c.charCodeAt(0))};
async function refresh(){const out=await fetch('/api/status').then(r=>r.json());status.textContent=JSON.stringify(out,null,2);send.disabled=!out.registered}
document.getElementById('enable').onclick=async()=>{try{status.textContent='Requesting permission…';const reg=await navigator.serviceWorker.register('/sw.js');await navigator.serviceWorker.ready;const cfg=await fetch('/api/config').then(r=>r.json());const permission=await Notification.requestPermission();if(permission!=='granted')throw new Error('Notification permission: '+permission);let sub=await reg.pushManager.getSubscription();if(!sub)sub=await reg.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:b64(cfg.public_key)});const saved=await fetch('/api/subscription',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(sub)});if(!saved.ok)throw new Error('Registration failed');await refresh()}catch(error){status.textContent=String(error)}};
send.onclick=async()=>{send.disabled=true;status.textContent='Sending…';try{const response=await fetch('/api/send',{method:'POST'});const out=await response.json();if(!response.ok)throw new Error(out.error||'Send failed');await refresh()}catch(error){status.textContent=String(error);send.disabled=false}};
refresh();
</script></main></body></html>`);

const MANIFEST = json({
  id: '/web-push-compat-probe',
  name: 'Fleet Web Push probe',
  short_name: 'Fleet probe',
  start_url: '/',
  display: 'standalone',
  background_color: '#15181d',
  theme_color: '#15181d',
  icons: [{ src: '/icon.svg', sizes: 'any', type: 'image/svg+xml', purpose: 'any maskable' }],
});

const ICON = Buffer.from(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" rx="112" fill="#15181d"/><path fill="#77d5b5" d="M122 96h280v72H206v62h166v70H206v116h-84z"/></svg>`);

const SERVICE_WORKER = Buffer.from(`
self.addEventListener('install',event=>event.waitUntil(self.skipWaiting()));
self.addEventListener('activate',event=>event.waitUntil(self.clients.claim()));
self.addEventListener('push',event=>{let data={};try{data=event.data?.json()||{}}catch{}event.waitUntil(self.registration.showNotification(data.title||'Fleet needs you',{body:data.body||'Web Push compatibility test',tag:data.tag||'fleet-probe',data:{url:data.url||'/?event=compat-probe'},actions:[{action:'snooze',title:'Snooze'},{action:'mute',title:'Mute'}]}))});
self.addEventListener('notificationclick',event=>{event.notification.close();event.waitUntil((async()=>{if(event.action){try{await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:event.action})});return}catch{}}const all=await clients.matchAll({type:'window',includeUncontrolled:true});const target=new URL(event.notification.data?.url||'/',self.location.origin).href;for(const client of all){if('focus'in client){await client.navigate(target);return client.focus()}}return clients.openWindow(target)})())});
`);

const validateSubscription = value => {
  if (!value || typeof value !== 'object') throw new Error('subscription required');
  const endpoint = new URL(String(value.endpoint || ''));
  if (endpoint.protocol !== 'https:') throw new Error('HTTPS endpoint required');
  const keys = value.keys || {};
  if (!/^[A-Za-z0-9_-]{20,200}$/.test(String(keys.p256dh || ''))
      || !/^[A-Za-z0-9_-]{10,100}$/.test(String(keys.auth || ''))) {
    throw new Error('invalid subscription keys');
  }
  return { endpoint: endpoint.href, expirationTime: value.expirationTime || null,
    keys: { p256dh: keys.p256dh, auth: keys.auth } };
};

const payload = json({ version: 1, event_id: 'compat-probe', kind: 'question',
  title: 'Fleet needs you', body: 'Fleet needs a response', tag: 'fleet-probe',
  url: '/?event=compat-probe', actions: ['snooze', 'mute'] }).toString('utf8');

const sendProbe = async () => {
  if (!subscription) throw new Error('register this device first');
  const result = await webPush.sendNotification(subscription, payload, {
    vapidDetails: { subject: SUBJECT, publicKey: vapid.publicKey,
      privateKey: vapid.privateKey },
    TTL: 300,
    urgency: 'high',
    topic: 'fleet-probe',
    timeout: 10_000,
  });
  lastResult = { state: 'sent', status_code: result.statusCode, sent_at: Date.now() };
};

const server = http.createServer(async (request, response) => {
  const url = new URL(request.url, `http://${request.headers.host || 'localhost'}`);
  try {
    if (request.method === 'GET' && url.pathname === '/') return reply(response, 200, 'text/html; charset=utf-8', HTML);
    if (request.method === 'GET' && url.pathname === '/manifest.webmanifest') return reply(response, 200, 'application/manifest+json', MANIFEST);
    if (request.method === 'GET' && url.pathname === '/sw.js') return reply(response, 200, 'text/javascript; charset=utf-8', SERVICE_WORKER);
    if (request.method === 'GET' && url.pathname === '/icon.svg') return reply(response, 200, 'image/svg+xml', ICON);
    if (request.method === 'GET' && url.pathname === '/api/config') return reply(response, 200, 'application/json', json({ public_key: vapid.publicKey }));
    if (request.method === 'GET' && url.pathname === '/api/status') return reply(response, 200, 'application/json', json({ registered: Boolean(subscription), ...lastResult }));
    if (request.method === 'POST' && url.pathname === '/api/subscription') {
      subscription = validateSubscription(await readJson(request));
      lastResult = { state: 'registered' };
      return reply(response, 200, 'application/json', json({ ok: true }));
    }
    if (request.method === 'POST' && url.pathname === '/api/send') {
      await sendProbe();
      return reply(response, 200, 'application/json', json({ ok: true }));
    }
    if (request.method === 'POST' && url.pathname === '/api/action') {
      const action = String((await readJson(request)).action || '');
      if (!['snooze', 'mute'].includes(action)) throw new Error('invalid action');
      lastResult = { state: 'action_received', action, received_at: Date.now() };
      return reply(response, 200, 'application/json', json({ ok: true }));
    }
    return reply(response, 404, 'text/plain; charset=utf-8', Buffer.from('not found'));
  } catch (error) {
    lastResult = { state: 'failed', error: String(error.message || error).slice(0, 200) };
    return reply(response, 400, 'application/json', json({ ok: false, error: lastResult.error }));
  }
});

const selfTest = () => {
  const recipient = createECDH('prime256v1');
  recipient.generateKeys();
  const fake = validateSubscription({
    endpoint: 'https://fcm.googleapis.com/fcm/send/fleet-dash-compat-probe',
    keys: {
      p256dh: recipient.getPublicKey().toString('base64url'),
      auth: randomBytes(16).toString('base64url'),
    },
  });
  const details = webPush.generateRequestDetails(fake, payload, {
    vapidDetails: { subject: SUBJECT, publicKey: vapid.publicKey,
      privateKey: vapid.privateKey },
    TTL: 300,
    urgency: 'high',
    topic: 'fleet-probe',
  });
  const forbidden = ['prompt', 'command', 'file_path', 'branch', 'cost', 'account'];
  for (const key of forbidden) {
    if (payload.includes(key)) throw new Error(`payload leaks forbidden field: ${key}`);
  }
  if (details.endpoint !== fake.endpoint || !details.body?.length
      || details.headers['Content-Encoding'] !== 'aes128gcm'
      || !details.headers.Authorization || details.headers.Topic !== 'fleet-probe') {
    throw new Error('Web Push request construction failed');
  }
  process.stdout.write(JSON.stringify({ ok: true, package: 'web-push',
    encrypted_bytes: details.body.length, content_encoding: details.headers['Content-Encoding'],
    vapid: Boolean(details.headers.Authorization), topic: details.headers.Topic }) + '\n');
};

if (SELF_TEST) {
  selfTest();
} else {
  server.listen(PORT, HOST, () => {
    process.stdout.write(`Fleet Web Push probe: http://${HOST}:${PORT}\n`);
  });
}
