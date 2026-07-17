'use strict';

const assert = require('node:assert/strict');
const {createECDH, randomBytes} = require('node:crypto');
const {EventEmitter} = require('node:events');
const test = require('node:test');
const webPush = require('web-push');

const worker = require('../web_push_worker.js');

function subscription(endpoint = 'https://fcm.googleapis.com/fcm/send/fixture') {
  const recipient = createECDH('prime256v1');
  recipient.generateKeys();
  return {endpoint, expirationTime: null, keys: {
    p256dh: recipient.getPublicKey().toString('base64url'),
    auth: randomBytes(16).toString('base64url'),
  }};
}

function state() {
  const keys = webPush.generateVAPIDKeys();
  return {publicKey: keys.publicKey, privateKey: keys.privateKey,
    subject: 'mailto:fleet-dash@localhost.invalid', allowedOrigins: []};
}

function payload() {
  return JSON.stringify({version: 1, event_id: 'evt-fixture',
    title: 'Fleet needs you', body: 'A coding session needs your response.',
    url: '/#notifications/evt-fixture', unread: 3});
}

function fakeRequest(status, headers = {}, capture = {}) {
  return (options, callback) => {
    capture.options = options;
    const request = new EventEmitter();
    request.setTimeout = (_timeout, handler) => { request.timeoutHandler = handler; };
    request.destroy = () => {};
    request.end = body => {
      capture.body = body;
      const response = new EventEmitter();
      response.statusCode = status;
      response.headers = headers;
      callback(response);
      process.nextTick(() => response.emit('end'));
    };
    return request;
  };
}

function failingRequest(kind) {
  return (_options, _callback) => {
    const request = new EventEmitter();
    request.setTimeout = (_timeout, handler) => { request.timeoutHandler = handler; };
    request.destroy = () => {};
    request.end = () => process.nextTick(() => {
      if (kind === 'timeout') request.timeoutHandler();
      else request.emit('error', new Error('fixture network failure'));
    });
    return request;
  };
}

const lookup = async () => [{address: '8.8.8.8', family: 4}];
const job = () => ({type: 'send', request_id: 'push-fixture',
  subscription: subscription(), payload: payload(), ttl: 300,
  urgency: 'high', topic: 'fleet-fixture'});

test('global address validation rejects SSRF destinations', () => {
  for (const address of ['127.0.0.1', '10.1.2.3', '169.254.1.1', '192.168.1.1',
    '::1', 'fd00::1', 'fe80::1', '2001:db8::1']) {
    assert.equal(worker.isGlobalAddress(address), false, address);
  }
  assert.equal(worker.isGlobalAddress('8.8.8.8'), true);
  assert.equal(worker.isGlobalAddress('2606:4700:4700::1111'), true);
});

test('subscription and payload validation reject hostile fields', () => {
  for (const endpoint of ['http://fcm.googleapis.com/push', 'https://127.0.0.1/push',
    'https://user:pass@fcm.googleapis.com/push', 'https://internal.example.test/push',
    'https://fcm.googleapis.com:444/push', 'https://fcm.googleapis.com/push#fragment']) {
    assert.throws(() => worker.validateSubscription(subscription(endpoint), []));
  }
  const extra = JSON.parse(payload());
  extra.prompt = 'secret';
  assert.throws(() => worker.validatePayload(JSON.stringify(extra)));
  assert.throws(() => worker.validatePayload('x'.repeat(2_049)));
});

test('send builds an encrypted pinned HTTPS request with generated headers only', async () => {
  const capture = {};
  const result = await worker.sendJob(job(), state(), {
    lookup, request: fakeRequest(201, {'x-request-id': 'ignored'}, capture),
  });
  assert.equal(result.ok, true);
  assert.equal(result.status, 201);
  assert.equal(capture.options.hostname, 'fcm.googleapis.com');
  assert.equal(capture.options.port, 443);
  assert.equal(capture.options.method, 'POST');
  assert.equal(capture.options.rejectUnauthorized, true);
  assert.match(String(capture.options.headers.Authorization), /vapid/i);
  assert.equal(capture.options.headers['Content-Encoding'], 'aes128gcm');
  assert.equal(capture.options.headers.Topic, 'fleet-fixture');
  assert.ok(Buffer.isBuffer(capture.body));
  assert.equal(capture.body.includes(Buffer.from('Fleet needs you')), false);
  assert.deepEqual(await new Promise((resolve, reject) => capture.options.lookup(
    'fcm.googleapis.com', {}, (error, address, family) => error ? reject(error) :
      resolve({address, family}))), {address: '8.8.8.8', family: 4});
});

test('retry-after and permanent subscription statuses map without response bodies', async () => {
  const throttled = await worker.sendJob(job(), state(), {
    lookup, request: fakeRequest(429, {'retry-after': '17'}),
  });
  assert.deepEqual({ok: throttled.ok, status: throttled.status,
    retry_after: throttled.retry_after, code: throttled.code},
  {ok: false, status: 429, retry_after: 17, code: 'http_429'});
  const expired = await worker.sendJob(job(), state(), {
    lookup, request: fakeRequest(410, {'set-cookie': 'never-return-this'}),
  });
  assert.equal(expired.status, 410);
  assert.equal(JSON.stringify(expired).includes('never-return-this'), false);
});

test('server, timeout, and network failures map to bounded retryable results', async () => {
  const server = await worker.sendJob(job(), state(), {
    lookup, request: fakeRequest(503),
  });
  assert.equal(server.status, 503);
  assert.equal(server.ok, false);
  const timeout = await worker.sendJob(job(), state(), {
    lookup, request: failingRequest('timeout'),
  });
  assert.deepEqual({retryable: timeout.retryable, code: timeout.code},
    {retryable: true, code: 'timeout'});
  const network = await worker.sendJob(job(), state(), {
    lookup, request: failingRequest('network'),
  });
  assert.deepEqual({retryable: network.retryable, code: network.code},
    {retryable: true, code: 'network_error'});
});
