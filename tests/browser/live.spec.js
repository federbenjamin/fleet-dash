const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');

const liveURL = process.env.FLEET_DASH_LIVE_URL;

function authenticatedLiveURL() {
  if (!liveURL || process.env.FLEET_DASH_LIVE_AUTH !== '1') return null;
  const config = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'config.json'), 'utf8'));
  if (!config.act_token) return null;
  const target = new URL(liveURL);
  target.searchParams.set('token', config.act_token);
  return target.toString();
}

async function goTo(page, route) {
  let control = page.locator(`button[data-route="${route}"]:visible`);
  if (await control.count() === 0) {
    await page.locator('button[data-route="more"]:visible').click();
    control = page.locator(`#mobilemore button[onclick*="'${route}'"]:visible`);
  }
  await control.click();
  if (route === 'settings') await expect(page.locator('#settingsview')).toBeVisible();
  else await expect(page.locator(`[data-destination="${route}"]`)).toBeVisible();
}

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
  await goTo(page, 'settings');
  await expect(page.locator('#settingsview')).toBeVisible();
  await expect(page.locator('#settitle')).toHaveText('Settings');
  await page.evaluate(() => history.back());
  await expect(page.locator('#settingsview')).toBeHidden();
  await goTo(page, 'workstreams');
  await expect(page.locator('#workstreams .workstream').first()).toBeVisible();
  await expect(page.locator('#workstreams')).toContainText(/not observed|not configured/);
  await goTo(page, 'now');
  const codex = page.locator('[data-sid^="codex:"]').first();
  if (await codex.isVisible()) {
    await expect(codex.locator('select.modesel')).toHaveCount(0);
    await codex.locator('.shead').click();
  } else {
    await goTo(page, 'history');
    const history = page.locator('#history');
    const codexHistory = page.locator('[data-history-sid^="codex:"]').first();
    await expect(codexHistory).toBeVisible();
    await codexHistory.locator('.historyaction').click();
  }
  await expect(page.locator('#sview')).toBeVisible();
  await page.getByRole('button', { name: 'Why here?' }).click();
  await expect(page.locator('#sevidence')).toBeVisible();
  await expect(page.locator('#sevidence')).toContainText('Winning rule');
  await expect(page.locator('#sevidence .evidenceevent').first()).toBeVisible();
  await page.getByRole('button', { name: 'close state evidence' }).click();
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

test('running Fleet Dash searches indexed transcripts with exact context', async ({ page }, testInfo) => {
  const target = authenticatedLiveURL();
  test.skip(!target, 'set FLEET_DASH_LIVE_URL and FLEET_DASH_LIVE_AUTH=1');
  const failures = [];
  page.on('pageerror', error => failures.push(`page: ${error}`));
  page.on('console', message => {
    if (message.type() === 'error') failures.push(`console: ${message.text()}`);
  });
  page.on('requestfailed', request => failures.push(
    `network: ${request.method()} ${request.url()} ${request.failure()?.errorText || ''}`));
  await page.goto(target, { waitUntil: 'domcontentloaded' });
  await goTo(page, 'search');
  await expect(page.locator('#searchstatus')).toContainText(/Checking|Indexing|Indexed/);
  await page.locator('#searchquery').fill('fleet');
  // The real multi-gigabyte index may be committing a batch; wait through one
  // bounded writer-contention window while still requiring a real result.
  await expect(page.locator('#searchresults .searchresult').first()).toBeVisible({ timeout: 15_000 });
  await page.locator('#searchresults .searchresult').first().click();
  await expect(page.locator('#searchview')).toBeVisible();
  await expect(page.locator('#searchviewbody .searchcontext')).toBeVisible({ timeout: 15_000 });
  await page.screenshot({ path: testInfo.outputPath('running-search.png'), fullPage: true });
  expect(failures).toEqual([]);
});
