#!/usr/bin/env node
/* Repeatable first-useful-render baseline against the running read-only app. */
const { chromium } = require('playwright');

const url = process.argv[2] || 'http://127.0.0.1:8377/';
const samples = Math.max(1, Number(process.argv[3] || 8));
const assertContract = process.argv.includes('--assert-contract');
const viewports = [
  { name: 'desktop', width: 1440, height: 1000 },
  { name: 'mobile-390x844', width: 390, height: 844 },
];

const percentile = (values, quantile) => {
  const ordered = [...values].sort((a, b) => a - b);
  const index = Math.min(ordered.length - 1,
    Math.max(0, Math.round((ordered.length - 1) * quantile)));
  return Number(ordered[index].toFixed(3));
};

(async () => {
  const browser = await chromium.launch();
  const output = { url, samples, viewports: {} };
  for (const viewport of viewports) {
    const timings = [];
    const renders = [], polls = [], payloads = [], heaps = [];
    for (let index = 0; index < samples; index += 1) {
      const page = await browser.newPage({ viewport });
      const started = performance.now();
      await page.goto(url, { waitUntil: 'domcontentloaded' });
      await page.locator('#usagechip').waitFor({ state: 'visible' });
      await page.locator('#pinned, #needs, #working, #sessions, #history').first()
        .waitFor({ state: 'attached' });
      timings.push(performance.now() - started);
      await page.waitForTimeout(100);
      const metrics = await page.evaluate(() => ({
        summary: window.__fleetPerf?.summary?.() || {},
        heap: performance.memory?.usedJSHeapSize || null,
      }));
      const render = metrics.summary.render_ms?.last;
      const poll = metrics.summary.poll_ms?.last;
      const payload = metrics.summary.poll_payload_bytes?.last;
      if (Number.isFinite(render)) renders.push(render);
      if (Number.isFinite(poll)) polls.push(poll);
      if (Number.isFinite(payload)) payloads.push(payload);
      if (Number.isFinite(metrics.heap)) heaps.push(metrics.heap);
      await page.close();
    }
    output.viewports[viewport.name] = {
      p50_ms: percentile(timings, .50), p95_ms: percentile(timings, .95),
      render_p95_ms: renders.length ? percentile(renders, .95) : null,
      poll_p95_ms: polls.length ? percentile(polls, .95) : null,
      payload_p50_bytes: payloads.length ? percentile(payloads, .50) : null,
      js_heap_p95_bytes: heaps.length ? percentile(heaps, .95) : null,
    };
  }
  await browser.close();
  output.contract = Object.fromEntries(Object.entries(output.viewports).map(([name, metrics]) => [name, {
    first_useful: { budget_ms: 350, p95_ms: metrics.p95_ms, pass: metrics.p95_ms < 350 },
    render: { budget_ms: 100, p95_ms: metrics.render_p95_ms,
      pass: metrics.render_p95_ms != null && metrics.render_p95_ms < 100 },
    poll: { budget_ms: 250, p95_ms: metrics.poll_p95_ms,
      pass: metrics.poll_p95_ms != null && metrics.poll_p95_ms < 250 },
  }]));
  console.log(JSON.stringify(output, null, 2));
  if (assertContract) {
    if (samples < 8) throw new Error('--assert-contract requires at least 8 samples');
    const failed = Object.entries(output.contract).flatMap(([viewport, phases]) =>
      Object.entries(phases).filter(([, result]) => !result.pass)
        .map(([phase, result]) => `${viewport}.${phase}=${result.p95_ms}ms`));
    if (failed.length) throw new Error(`latency contract failed: ${failed.join(', ')}`);
  }
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
