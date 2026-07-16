const { test, expect } = require('@playwright/test');

const liveURL = process.env.FLEET_DASH_LIVE_URL;

test('running Fleet Dash renders both providers without console or network failures', async ({ page }, testInfo) => {
  test.skip(!liveURL, 'set FLEET_DASH_LIVE_URL for the opt-in running-daemon check');
  const failures = [];
  const expectedReadOnly = [];
  page.on('pageerror', error => failures.push(`page: ${error}`));
  page.on('console', message => {
    if (message.type() !== 'error') return;
    if (message.text().includes('status of 403 (Forbidden)')) {
      expectedReadOnly.push(message.text());
    } else {
      failures.push(`console: ${message.text()}`);
    }
  });
  page.on('requestfailed', request => failures.push(
    `network: ${request.method()} ${request.url()} ${request.failure()?.errorText || ''}`));
  await page.goto(liveURL, { waitUntil: 'domcontentloaded' });
  await expect(page.locator('#usage')).toContainText('Claude Code');
  await expect(page.locator('#usage')).toContainText('Codex CLI');
  const usageProviders = page.locator('#usage .uprovider');
  await expect(usageProviders.nth(0).locator('.uemail').first()).not.toBeEmpty();
  await expect(usageProviders.nth(0)).toContainText('local lifetime tokens');
  await expect(usageProviders.nth(1).locator('.uemail').first()).not.toBeEmpty();
  await expect(usageProviders.nth(1)).toContainText('lifetime tokens');
  await expect(page.locator('#usage')).not.toContainText('GPT-5.3-Codex-Spark');
  await page.locator('#gear').click();
  await expect(page.locator('#settingsview')).toBeVisible();
  await expect(page.locator('#settitle')).toHaveText('Settings');
  await page.evaluate(() => history.back());
  await expect(page.locator('#settingsview')).toBeHidden();
  const codex = page.locator('[data-sid^="codex:"]').first();
  if (await codex.isVisible()) {
    await expect(codex.locator('select.modesel')).toHaveCount(0);
    await codex.locator('.shead').click();
  } else {
    const history = page.locator('#history details');
    await history.locator('summary').click();
    const codexHistory = page.locator('[data-history-sid^="codex:"]').first();
    await expect(codexHistory).toBeVisible();
    await codexHistory.locator('.historyaction').click();
  }
  await expect(page.locator('#sview')).toBeVisible();
  await page.getByRole('button', { name: 'session actions' }).click();
  await expect(page.getByRole('menuitem', { name: /Appearance.*light \/ dark/ })).toBeVisible();
  await expect(page.getByRole('menuitem', { name: /Close session/ })).toBeVisible();
  await page.locator('#sclose').click();
  await expect(page.locator('#notoken')).toBeVisible();
  for (let index = 0; index < 10; index += 1) await page.evaluate(() => tick());
  await page.screenshot({ path: testInfo.outputPath('running-fleet.png'), fullPage: true });
  expect(expectedReadOnly).toHaveLength(1);
  expect(failures).toEqual([]);
});
