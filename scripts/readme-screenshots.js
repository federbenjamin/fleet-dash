#!/usr/bin/env node
// The README's screenshots, taken against the browser fixture server: synthetic sessions only,
// never a live dashboard. Start the fixture first, then run this from the repo root:
//
//   python3 tests/browser_fixture_server.py &
//   node scripts/readme-screenshots.js
//
// Each shot resets the fixture to one scenario (tests/browser_fixture_server.py, set_scenario).
const { chromium, devices } = require('@playwright/test');
const path = require('node:path');

const BASE = 'http://127.0.0.1:8399';
const TOKEN = 'abcdef123456';
const OUT = path.join(__dirname, '..', 'docs', 'images');
const DESKTOP = { viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 };
const MOBILE = { ...devices['iPhone 13'], viewport: { width: 390, height: 844 }, deviceScaleFactor: 3 };

const openAction = async (page) => {
  await page.locator('[data-action-sid] .actionopen').first().click();
};
const openChat = (title) => async (page) => {
  await page.getByRole('button', { name: `Open chat: ${title}` }).first().click();
};

const SHOTS = [
  ['needs-you-desktop', 'mobile-needs-you', DESKTOP, openAction],
  ['working-desktop', 'subagent', DESKTOP, openChat('Codex parity work')],
  ['now-mobile', 'mobile-needs-you', MOBILE, null],
  ['question-mobile', 'mobile-needs-you', MOBILE, openAction],
  ['approval-mobile', 'claude-permission', MOBILE, openAction],
];

(async () => {
  const browser = await chromium.launch();
  for (const [name, scenario, device, open] of SHOTS) {
    const context = await browser.newContext({ colorScheme: 'dark', ...device });
    const page = await context.newPage();
    await page.request.post(`${BASE}/test/reset`, { data: { scenario } });
    await page.goto(`${BASE}/?token=${TOKEN}`);
    await page.waitForTimeout(1800);
    if (open) {
      await open(page);
      await page.waitForTimeout(1500);
    }
    await page.screenshot({ path: path.join(OUT, `${name}.png`) });
    console.log(`${name}.png`);
    await context.close();
  }
  await browser.close();
})();
