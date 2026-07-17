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

function recordUnexpectedRequestFailures(page, failures) {
  page.on('requestfailed', request => {
    const error = request.failure()?.errorText || '';
    // Fleet aborts stale polls/forecasts when a newer request supersedes them.
    // That cancellation is the expected race-safety path, not an outage.
    if (error === 'net::ERR_ABORTED') return;
    failures.push(`network: ${request.method()} ${request.url()} ${error}`);
  });
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
  recordUnexpectedRequestFailures(page, failures);
  await page.goto(liveURL, { waitUntil: 'domcontentloaded' });
  await page.locator('#usagechip').click();
  await expect(page.locator('#usagepanel')).toBeVisible();
  await expect(page.locator('#usagebody')).toContainText('Claude Code');
  await expect(page.locator('#usagebody')).toContainText('Codex CLI');
  const usageProviders = page.locator('#usagebody .uprovider');
  await expect(usageProviders.nth(0).locator('.uemail').first()).not.toBeEmpty();
  await expect(usageProviders.nth(0)).toContainText('local lifetime tokens');
  await expect(usageProviders.nth(1).locator('.uemail').first()).not.toBeEmpty();
  await expect(usageProviders.nth(1)).toContainText('lifetime tokens');
  await expect(page.locator('#usagebody')).not.toContainText('GPT-5.3-Codex-Spark');
  await page.locator('#usagepanel').getByRole('button', { name: 'close usage' }).click();
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
  expect(expectedReadOnly.length).toBeGreaterThanOrEqual(1);
  expect(expectedReadOnly.length).toBeLessThanOrEqual(2);
  expect(failures).toEqual([]);
});

test('running Fleet Dash opens the authenticated Outbox without dispatching work', async ({ page }, testInfo) => {
  const target = authenticatedLiveURL();
  test.skip(!target, 'set FLEET_DASH_LIVE_URL and FLEET_DASH_LIVE_AUTH=1');
  const failures = [];
  page.on('pageerror', error => failures.push(`page: ${error}`));
  page.on('console', message => { if (message.type() === 'error') failures.push(`console: ${message.text()}`); });
  recordUnexpectedRequestFailures(page, failures);
  await page.goto(target, { waitUntil: 'domcontentloaded' });
  await page.locator('#outboxchip').click();
  await expect(page.locator('#outboxview')).toBeVisible();
  await expect(page.locator('#outboxbody')).toContainText(/No messages|Scheduled|Waiting|Sent|Cancelled|Blocked/);
  await page.screenshot({ path: testInfo.outputPath('running-outbox.png'), fullPage: true });
  expect(failures).toEqual([]);
});

test('running Fleet Dash reads briefings, budgets, digest settings, and spawn forecasts without mutation', async ({ page }, testInfo) => {
  const target = authenticatedLiveURL();
  test.skip(!target, 'set FLEET_DASH_LIVE_URL and FLEET_DASH_LIVE_AUTH=1');
  const failures = [];
  page.on('pageerror', error => failures.push(`page: ${error}`));
  page.on('console', message => { if (message.type() === 'error') failures.push(`console: ${message.text()}`); });
  recordUnexpectedRequestFailures(page, failures);
  await page.goto(target, { waitUntil: 'domcontentloaded' });
  const briefing = await (await page.request.get('/api/briefing?device=playwright-live')).json();
  expect(briefing.ok).toBe(true);
  const budgets = await (await page.request.get('/api/budgets')).json();
  expect(budgets.ok).toBe(true);
  await goTo(page, 'notifications');
  await page.locator('.notificationbar').getByRole('button', { name: /^Briefing/ }).click();
  await expect(page.locator('#notifications .briefbody')).toBeVisible();
  await goTo(page, 'settings');
  await expect(page.locator('.setrow:has-text("daily briefing push")')).toBeVisible();
  await expect(page.locator('.digestsettings input[type="time"]')).toBeVisible();
  await expect(page.locator('.budgetsettingsfold')).toBeVisible();
  await page.locator('#setclose').click();
  await goTo(page, 'insights');
  await expect(page.locator('#budgets')).toContainText(/Budgets|No budgets configured/);
  await goTo(page, 'now');
  await page.getByRole('button', { name: '+ new coding session' }).click();
  const dirs = page.locator('#newsess select').nth(1).locator('option');
  if (await dirs.count() > 1) {
    await page.locator('#newsess select').nth(1).selectOption({ index: 1 });
    await expect(page.locator('.spawnforecast')).toContainText(/history|confidence|currency unavailable/, { timeout: 10_000 });
  }
  await page.screenshot({ path: testInfo.outputPath('running-briefing-budgets.png'), fullPage: true });
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
  recordUnexpectedRequestFailures(page, failures);
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

test('running Fleet Dash builds an editable authenticated handoff without sending it', async ({ page }, testInfo) => {
  const target = authenticatedLiveURL();
  test.skip(!target, 'set FLEET_DASH_LIVE_URL and FLEET_DASH_LIVE_AUTH=1');
  const failures = [];
  page.on('pageerror', error => failures.push(`page: ${error}`));
  page.on('console', message => {
    if (message.type() === 'error') failures.push(`console: ${message.text()}`);
  });
  recordUnexpectedRequestFailures(page, failures);
  await page.goto(target, { waitUntil: 'domcontentloaded' });
  await page.evaluate(() => tick());
  await expect.poll(() => page.evaluate(() =>
    (last?.sessions || []).length + (last?.closed || []).length)).toBeGreaterThan(0);
  const source = await page.evaluate(() => {
    const live = (last?.sessions || [])[0];
    if (live) { openSession(live.session_id); return { provider: live.provider, closed: false }; }
    const closed = (last?.closed || [])[0];
    if (closed) { openClosed(closed.session_id); return { provider: closed.provider, closed: true }; }
    return null;
  });
  expect(source).not.toBeNull();
  await expect(page.locator('#sview')).toBeVisible();
  await page.locator('#sctrl .ovbtn').click();
  const targetProvider = source.provider === 'claude' ? 'Codex' : 'Claude';
  await page.getByRole('menuitem', { name: new RegExp(`Continue in ${targetProvider}`) }).click();
  await expect(page.locator('#handoffview')).toBeVisible();
  await expect(page.locator('#handoffpreview')).toHaveValue(/Independent|independent/);
  await expect(page.locator('.handoffsubmit')).toBeEnabled();
  await page.screenshot({ path: testInfo.outputPath('running-handoff-preview.png'), fullPage: true });
  await page.locator('#handoffclose').click();
  await expect(page.locator('#handoffview')).toBeHidden();
  await expect(page.locator('#sview')).toBeVisible();
  expect(failures).toEqual([]);
});

test('running Fleet Dash exposes an external GitHub repository link', async ({ page }) => {
  const target = authenticatedLiveURL();
  test.skip(!target, 'set FLEET_DASH_LIVE_URL and FLEET_DASH_LIVE_AUTH=1');
  const failures = [];
  page.on('pageerror', error => failures.push(`page: ${error}`));
  page.on('console', message => {
    if (message.type() === 'error') failures.push(`console: ${message.text()}`);
  });
  recordUnexpectedRequestFailures(page, failures);
  await page.goto(target, { waitUntil: 'domcontentloaded' });
  await goTo(page, 'workstreams');
  const git = page.locator('#workstreams .workstream').filter({ has: page.getByRole('link', { name: 'GitHub ↗' }) }).first();
  await expect(git).toBeVisible({ timeout: 15_000 });
  await expect(git.getByRole('link', { name: 'GitHub ↗' })).toHaveAttribute('href',/^https:\/\//);
  await expect(page.locator('#repoview')).toHaveCount(0);
  expect(failures).toEqual([]);
});
