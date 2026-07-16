#!/usr/bin/env node
/* Repeatable first-useful-render baseline against the running read-only app. */
const { chromium } = require('playwright');

const url = process.argv[2] || 'http://127.0.0.1:8377/';
const samples = Math.max(1, Number(process.argv[3] || 8));
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
    for (let index = 0; index < samples; index += 1) {
      const page = await browser.newPage({ viewport });
      const started = performance.now();
      await page.goto(url, { waitUntil: 'domcontentloaded' });
      await page.locator('#usage').waitFor({ state: 'visible' });
      await page.locator('#pinned, #needs, #working, #sessions, #history').first()
        .waitFor({ state: 'attached' });
      timings.push(performance.now() - started);
      await page.close();
    }
    output.viewports[viewport.name] = {
      p50_ms: percentile(timings, .50), p95_ms: percentile(timings, .95),
    };
  }
  await browser.close();
  console.log(JSON.stringify(output, null, 2));
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
