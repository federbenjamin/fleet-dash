const { test, expect } = require('@playwright/test');

async function reset(page, scenario = 'base') {
  await page.request.post('/test/reset', { data: { scenario } });
  await page.goto('/?token=abcdef123456');
  await expect(page.locator('#route-now')).toBeVisible();
  await expect(page.locator('#usagechip')).toBeVisible();
}

async function refresh(page) {
  await page.evaluate(() => tick());
}

async function fixtureState(page) {
  return (await page.request.get('/test/state')).json();
}

async function sendModifiedReturn(page, locator) {
  const mac = await page.evaluate(() => /Mac|iPhone|iPad|iPod/.test(navigator.platform || ''));
  await locator.press(mac ? 'Meta+Enter' : 'Control+Enter');
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

async function openAction(page, sid) {
  const row = page.locator(`[data-action-sid="${sid}"]`);
  await expect(row).toBeVisible();
  await row.locator('.primarybtn').click();
  await expect(page.locator('#sview')).toBeVisible();
  return row;
}

test.beforeEach(async ({ page }) => {
  const failures = [];
  page.on('pageerror', error => failures.push(String(error)));
  page.on('console', message => {
    if (message.type() === 'error') failures.push(message.text());
  });
  page.__failures = failures;
});

test.afterEach(async ({ page }) => {
  expect(page.__failures, 'browser console/runtime errors').toEqual([]);
});

test('staging is unmistakable and controls only staging-owned sessions', async ({ page }) => {
  await reset(page, 'staging');
  await expect(page.locator('#instancebanner')).toBeVisible();
  await expect(page.locator('#instancebanner')).toContainText('STAGING');
  await expect(page).toHaveTitle(/Fleet Staging/);
  await expect(page.locator('[data-sid="claude-one"] .accessbadge')).toHaveText('view only');
  await page.locator('[data-sid="claude-one"] .shead').click();
  await expect(page.locator('#sact .composer')).toHaveCount(0);
  await page.locator('#sclose').click();
  await page.locator('[data-sid="codex:thread-one"] .shead').click();
  await expect(page.locator('#sact .composer')).toBeVisible();
  await page.locator('#sclose').click();
  await page.locator('.newbtn').click();
  await expect(page.locator('#newsess')).toContainText('Isolated staging worktree');
  await expect(page.locator('#newsess input[data-draft-key="new:directory"]')).toHaveCount(0);
});

test('responsive application shell routes, filters, and follows browser back', async ({ page }, testInfo) => {
  await reset(page);
  const mobile = testInfo.project.name.startsWith('mobile');
  if (mobile) {
    await expect(page.locator('#sidenav')).toBeHidden();
    await expect(page.locator('#bottomnav')).toBeVisible();
  } else {
    await expect(page.locator('#sidenav')).toBeVisible();
    await expect(page.locator('#bottomnav')).toBeHidden();
  }
  const nowControl = page.locator('button[data-route="now"]:visible');
  await nowControl.focus();
  await expect(nowControl).toBeFocused();
  await page.keyboard.press('Tab');
  expect(await page.evaluate(() => document.activeElement?.dataset?.route)).toBe('notifications');

  await goTo(page, 'search');
  await expect(page).toHaveURL(/#search$/);
  await page.reload();
  await expect(page.locator('[data-destination="search"]')).toBeVisible();
  await goTo(page, 'workstreams');
  await goTo(page, 'history');
  await expect(page.locator('#historyrows .historyrow')).toHaveCount(1);
  await page.goBack();
  await expect(page.locator('[data-destination="workstreams"]')).toBeVisible();

  await goTo(page, 'settings');
  await page.goBack();
  await expect(page.locator('#settingsview')).toBeHidden();
  await expect(page.locator('[data-destination="workstreams"]')).toBeVisible();

  await goTo(page, 'insights');
  await expect(page.locator('#rollup')).toContainText(/crunching|agents/);
  await goTo(page, 'now');
  await page.locator('#nowfilter').fill('parity work');
  await expect(page.locator('[data-sid="codex:thread-one"]')).toBeVisible();
  await expect(page.locator('[data-sid="claude-one"]')).toBeHidden();
  await page.locator('[data-now-filter="working"]').click();
  await expect(page.locator('#actioninbox .actionrow, #working .card, #sessions .card, #pinned .card')).toHaveCount(0);
  await page.locator('[data-now-filter="all"]').click();
  await page.locator('#nowfilter').fill('');
  await expect(page.locator('[data-sid="claude-one"]')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath(`application-shell-${mobile ? 'mobile' : 'desktop'}.png`), fullPage: true });
});

test('out-of-order fleet and Insights responses cannot overwrite newer state', async ({ page }) => {
  await reset(page);
  const baseFleet = await (await page.request.get('/api/fleet')).json();
  let fleetCalls = 0;
  await page.route('**/api/fleet', async route => {
    fleetCalls += 1;
    const snapshot = JSON.parse(JSON.stringify(baseFleet));
    snapshot.sessions[0].title = fleetCalls === 1 ? 'stale poll result' : 'fresh poll result';
    if (fleetCalls === 1) await new Promise(resolve => setTimeout(resolve, 350));
    try {
      await route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify(snapshot) });
    } catch (_) { /* the stale request is intentionally aborted */ }
  });
  await page.evaluate(() => {
    tick();
    setTimeout(() => tick(), 20);
  });
  await expect.poll(() => page.evaluate(() =>
    last.sessions.find(item => item.session_id === 'claude-one')?.title)).toBe('fresh poll result');
  expect(fleetCalls).toBeGreaterThanOrEqual(2);
  await page.unroute('**/api/fleet');

  const insightsPayload = days => ({ ok: true,
    totals: { agent_cost: days, session_cost: 0, bust_cost: 0 },
    token_mix: [], cache_busts: [], agents: [], skills: [], tools: [], models: [],
    projects: [], by_day: [], top_sessions: [] });
  let failNinety = true;
  await page.route('**/api/insights?*', async route => {
    const days = Number(new URL(route.request().url()).searchParams.get('days'));
    if (days === 7) await new Promise(resolve => setTimeout(resolve, 350));
    if (days === 90 && failNinety) {
      failNinety = false;
      await route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ ok: false, error: 'fixture insights failure' }) });
      return;
    }
    await route.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify(insightsPayload(days)) });
  });
  await goTo(page, 'insights');
  await page.evaluate(() => {
    delete insightsCache[7]; delete insightsCache[30];
    insightsDays = 7; loadInsights(true); setInsightsDays(30);
  });
  await expect.poll(() => page.evaluate(() => insightsCache[30]?.data?.totals?.agent_cost)).toBe(30);
  await page.waitForTimeout(400);
  expect(await page.evaluate(() => insightsCache[7]?.data?.totals?.agent_cost)).toBe(7);
  expect(await page.evaluate(() => insightsCache[30]?.data?.totals?.agent_cost)).toBe(30);

  await page.evaluate(() => setInsightsDays(90));
  await expect(page.locator('#rollup [role="alert"]')).toContainText('fixture insights failure');
  await page.locator('#rollup').getByRole('button', { name: 'retry' }).click();
  await expect.poll(() => page.evaluate(() => insightsCache[90]?.data?.totals?.agent_cost)).toBe(90);
});

test('native decisions lock double taps and relay/Outbox failures keep recovery', async ({ page }) => {
  await reset(page, 'claude-question-slow');
  await page.evaluate(() => openSession('claude-one'));
  await expect(page.locator('#sact .optbtn').first()).toBeVisible();
  await page.locator('#sact .optbtn').first().evaluate(button => { button.click(); button.click(); });
  await expect(page.locator('#sbody .optimistic')).toHaveCount(1);
  await expect(page.locator('#sact .optbtn').first()).toBeDisabled();
  await page.waitForTimeout(850);
  expect((await fixtureState(page)).actions.filter(item => item.type === 'option')).toHaveLength(1);

  await reset(page, 'subagent');
  await page.evaluate(() => openAgent('codex:thread-one', 'child-one'));
  await expect(page.locator('#aft')).toBeVisible();
  await page.route('**/api/act', async route => {
    const payload = route.request().postDataJSON();
    if (payload.type !== 'relay') return route.continue();
    await new Promise(resolve => setTimeout(resolve, 250));
    return route.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify({ ok: false, error: 'relay path unavailable' }) });
  });
  await page.locator('#aft').fill('Preserve this exact relay');
  await page.locator('#aact').getByRole('button', { name: 'relay' }).click();
  await expect(page.locator('#aact .quickfeedback')).toContainText('Relaying');
  await expect(page.locator('#aact .quickfeedback')).toContainText('relay path unavailable');
  await page.locator('#aact .quickfeedback').getByRole('button', { name: 'restore' }).click();
  await expect(page.locator('#aft')).toHaveValue('Preserve this exact relay');
  await page.unroute('**/api/act');

  const created = await page.request.post('/api/act', { data: { type: 'outbox_create',
    kind: 'when_available', target_provider: 'codex', target_session_id: 'codex:thread-one',
    message: 'Queued failure recovery', created_zone: 'UTC' } });
  expect((await created.json()).ok).toBe(true);
  await page.evaluate(() => { closeAgent(); openOutbox(); });
  await expect(page.locator('.outboxrow')).toBeVisible();
  await page.route('**/api/act', async route => {
    const payload = route.request().postDataJSON();
    if (payload.type !== 'outbox_send_now') return route.continue();
    await new Promise(resolve => setTimeout(resolve, 250));
    return route.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify({ ok: false, error: 'provider did not accept message' }) });
  });
  const row = page.locator('.outboxrow').first();
  await row.getByRole('button', { name: 'Send now' }).click();
  await expect(row).toContainText('working…');
  await expect(row).toContainText('provider did not accept message');
  await expect(row.getByRole('button', { name: 'Send now' })).toBeEnabled();
});

test('unchanged and focused conversations repaint only when their content revision changes', async ({ page }) => {
  await reset(page, 'large-conversation');
  await page.evaluate(() => openSession('codex:thread-one'));
  await expect(page.locator('#sbody .cmsg')).toHaveCount(50);
  await page.evaluate(() => { window.__stableConversation = document.querySelector('#sbody .aconvo'); });
  await refresh(page);
  expect(await page.evaluate(() => document.querySelector('#sbody .aconvo') === window.__stableConversation)).toBe(true);

  await reset(page, 'subagent');
  await page.evaluate(() => openAgent('codex:thread-one', 'child-one'));
  const relay = page.locator('#aft');
  await relay.fill('draft survives transcript repaint');
  await page.evaluate(() => {
    const key = agentCacheKey('codex:thread-one', 'child-one');
    agentCache[key].messages.push({ role: 'assistant', text: 'New row while typing' });
    renderAgent();
  });
  await expect(page.locator('#abody')).toContainText('New row while typing');
  await expect(relay).toHaveValue('draft survives transcript repaint');

  await reset(page, 'large-conversation');
  let closedRequests = 0;
  await page.route('**/api/closed_context?*', async route => {
    closedRequests += 1;
    await new Promise(resolve => setTimeout(resolve, 300));
    return route.continue();
  });
  await page.evaluate(() => openClosed('codex:closed'));
  await page.evaluate(() => { renderClosed(); renderClosed(); });
  await expect(page.locator('#sbody .cmsg')).toHaveCount(50);
  expect(closedRequests).toBe(1);
  await page.locator('#sbody').evaluate(element => { element.scrollTop = 0; });
  await page.evaluate(() => { window.__closedConversation = document.querySelector('#sbody .aconvo'); });
  await refresh(page);
  expect(await page.locator('#sbody').evaluate(element => element.scrollTop)).toBe(0);
  expect(await page.evaluate(() => document.querySelector('#sbody .aconvo') === window.__closedConversation)).toBe(true);
});

test('full chat status strips are adaptive, provider-honest, and frozen for history', async ({ page }, testInfo) => {
  await reset(page);
  const mobile = testInfo.project.name.startsWith('mobile');
  await page.evaluate(() => openSession('claude-one'));
  const strip = page.locator('#sact .statusstrip');
  await expect(strip).toBeVisible();
  await expect(strip.locator('.status-primary')).toContainText('⎇ status-strip ↑3 ↓10');
  await expect(strip.locator('.status-primary')).toContainText('fleet-dash');
  await expect(strip.locator('.status-secondary')).toContainText('Opus 4.8 · high');
  await expect(strip.locator('.status-secondary')).toContainText('Ctx: 47%  →174k');
  await expect(page.locator('#stitle2')).not.toContainText('fleet-dash');
  expect(await page.evaluate(() => Boolean(document.querySelector('#sact .statusstrip')
    .compareDocumentPosition(document.querySelector('#sact .composer')) & Node.DOCUMENT_POSITION_FOLLOWING))).toBe(true);
  await expect(strip.locator('.status-graph i')).toHaveCount(50);
  expect(await page.evaluate(() => [1000,1001,2500,2501,5000,5001,7500,7501,
    10000,10001,15000,15001,20000,20001].map(value => statusGraphPoint(value)[0]).join('')))
    .toBe('▁▂▂▃▃▄▄▅▅▆▆▇▇█');
  if(mobile){
    await expect(strip.locator('.status-details')).toBeHidden();
    await strip.getByRole('button', { name: 'expand status details' }).click();
    await expect(strip.locator('.status-details')).toBeVisible();
  }else{
    await expect(strip.locator('.status-details')).toBeVisible();
    await expect(strip.locator('.status-expand')).toBeHidden();
  }
  await expect(strip.locator('.status-cache')).toContainText('♻ 99%');
  await expect(strip.locator('.status-cache')).toContainText('✎ 855 · spikes 9 · peak 278k');
  await strip.locator('.status-cost summary').click();
  await expect(strip.locator('.status-cost-breakdown')).toContainText('Main session');
  await expect(strip.locator('.status-cost-breakdown')).toContainText('Review protocol mapping');
  await page.locator('#sclose').click();

  const codexCard=page.locator('[data-sid="codex:thread-one"]');
  await codexCard.getByRole('button', { name: /more/ }).click();
  await codexCard.getByText(/completed agents/).click();
  await codexCard.getByText('reviewer', { exact: true }).click();
  const agentStrip=page.locator('#aact .statusstrip');
  await expect(agentStrip).toBeVisible();
  await expect(agentStrip).toHaveClass(/frozen/);
  await expect(agentStrip).toContainText('GPT-5.4 · high');
  await expect(agentStrip).not.toContainText('$0.00');
  await expect(agentStrip.locator('.status-cache')).toHaveCount(0);
  await page.locator('#aclose').click();

  await goTo(page,'history');
  await page.locator('[data-history-sid="codex:closed"]').getByRole('button',{name:'View'}).click();
  const closedStrip=page.locator('#sact .statusstrip');
  await expect(closedStrip).toBeVisible();
  await expect(closedStrip).toHaveClass(/frozen/);
  await expect(page.locator('#sact textarea')).toHaveCount(0);
});

test('Now hierarchy, Usage chip, active-subagent filter, and Claude card actions are unambiguous', async ({ page }, testInfo) => {
  await reset(page);
  await expect(page.locator('#route-now #briefing')).toHaveCount(0);
  await expect(page.locator('#pinned')).toHaveCount(1);
  await expect(page.locator('[data-now-filter="needs_you"]')).toHaveText('Needs you · 0');
  await expect(page.locator('[data-now-filter="working"]')).toHaveText('Working · 0');
  await expect(page.locator('[data-now-filter="available"]')).toHaveText('Available · 2');
  await expect(page.locator('[data-now-filter="subagents"]')).toHaveText('Subagents · 0');

  const claude = page.locator('[data-sid="claude-one"]');
  await expect(claude.getByRole('button', { name: 'Continue', exact: true })).toHaveCount(0);
  if (testInfo.project.name === 'desktop') {
    await expect(claude.getByRole('button', { name: 'Terminal', exact: true })).toBeVisible();
  }

  await reset(page, 'usage-warning');
  await expect(page.locator('#usagechip')).toHaveText('Usage · 96%');
  await expect(page.locator('#usagechip')).toHaveClass(/usagedanger/);
  await page.locator('#usagechip').click();
  await expect(page.locator('#usagepanel')).toBeVisible();
  await expect(page.locator('#usagebody')).toContainText('Fable weekly');
  await expect(page.locator('#usagebody')).toContainText('96%');
  await expect(page.locator('#usagebody')).toContainText('second@example.com');
  await page.locator('#usagepanel').getByRole('button', { name: 'close usage' }).click();
  await expect(page.locator('#usagepanel')).toBeHidden();

  await reset(page, 'subagent');
  await expect(page.locator('[data-sid="codex:thread-one"]')).not.toHaveClass(/fixedpeek/);
  await expect(page.locator('[data-now-filter="subagents"]')).toHaveText('Subagents · 1');
  await page.locator('[data-now-filter="subagents"]').click();
  const child = page.locator('#subagents .activeagentcard');
  await expect(child).toHaveCount(1);
  await expect(child).toContainText('Review protocol mapping');
  await expect(child).toContainText('fleet-dash · Codex parity work · codex-integration');
  await expect(page.locator('#pinned > *, #actioninbox > *, #needsyou > *, #working > *, #sessions > *')).toHaveCount(0);
  await child.click();
  await expect(page.locator('#aview')).toBeVisible();
  await expect(page.locator('#atitle')).toContainText('Review protocol mapping');
});

test('desktop navigation side and nested Settings preserve the full chat state', async ({ page }, testInfo) => {
  await reset(page);
  await goTo(page, 'settings');
  await page.getByRole('button', { name: 'Right side' }).click();
  await expect(page.locator('html')).toHaveAttribute('data-nav-side', 'right');
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('data-nav-side', 'right');
  if (testInfo.project.name === 'desktop') {
    const positions = await page.evaluate(() => ({nav: document.querySelector('#sidenav').getBoundingClientRect().x,
      main: document.querySelector('#appmain').getBoundingClientRect().x}));
    expect(positions.nav).toBeGreaterThan(positions.main);
  } else {
    await expect(page.locator('#sidenav')).toBeHidden();
    await expect(page.locator('#bottomnav')).toBeVisible();
  }
  await page.evaluate(() => setNavSide('left'));
  await reset(page, 'large-conversation');

  await page.evaluate(() => openSession('codex:thread-one'));
  await expect(page.locator('#sbody')).toContainText('Conversation message 204');
  await page.getByRole('button', { name: 'Why here?' }).click();
  await expect(page.locator('#sevidence')).toBeVisible();
  await page.evaluate(() => { document.querySelector('#sbody').scrollTop = 125; });
  const before = await page.locator('#sbody').evaluate(element => element.scrollTop);
  await page.evaluate(() => navigateTo('settings'));
  await expect(page.locator('#settingsview')).toBeVisible();
  await expect(page.locator('#sevidence')).toBeVisible();
  await page.goBack();
  await expect(page.locator('#settingsview')).toBeHidden();
  await expect(page.locator('#sview')).toBeVisible();
  await expect(page.locator('#sevidence')).toBeVisible();
  expect(await page.locator('#sbody').evaluate(element => element.scrollTop)).toBe(before);

  await page.evaluate(() => viewFile('codex:thread-one', encodeURIComponent('/fixture/artifact.md'),
    encodeURIComponent('artifact.md'), 'text', encodeURIComponent('artifact')));
  await expect(page.locator('#vtitle')).toHaveText('📄 artifact.md — artifact');
  await expect(page.locator('#vtitle')).not.toContainText('Codex parity work');
  await expect(page.locator('#vtitle .vfsep')).toHaveCount(0);
});

test('cross-provider search filters, exact context, live handoff, and rebuild', async ({ page }, testInfo) => {
  await reset(page);
  await goTo(page, 'search');
  await expect(page.locator('#searchstatus')).toContainText('Indexed 37 items');
  await expect(page.locator('#searchstatus')).toContainText('1 warning');
  await expect(page.locator('#searchresults')).toContainText('Type a search or choose a filter.');
  await page.locator('#searchquery').fill('protocol regression');
  await expect(page.locator('#searchresults .searchresult')).toHaveCount(1);
  await expect(page.locator('#searchproject')).toContainText('fleet-dash');

  await expect(page.locator('#searchresults')).toContainText('Codex parity work');
  await page.locator('#searchresults .searchresult').click();
  await expect(page.locator('#searchview')).toBeVisible();
  await expect(page.locator('#searchviewbody .hit')).toContainText('Indexed exact context');
  await expect(page.locator('#searchviewaction')).toContainText('Open live session');
  await page.goBack();
  await expect(page.locator('#searchview')).toBeHidden();
  await expect(page.locator('[data-destination="search"]')).toBeVisible();

  await page.locator('#searchquery').fill('');
  await page.locator('#searchprovider').selectOption('claude');
  await expect(page.locator('#searchresults .searchresult')).toHaveCount(1);
  await expect(page.locator('#searchresults')).toContainText('Claude review agent');
  await page.locator('#searchkind').selectOption('reasoning');
  await expect(page.locator('#searchresults .searchresult')).toHaveCount(1);
  await page.locator('#searchproject').selectOption('fleet-dash');
  await expect(page.locator('#searchresults .searchresult')).toHaveCount(1);
  await page.screenshot({ path: testInfo.outputPath('cross-provider-search.png'), fullPage: true });

  await page.locator('#searchprovider').selectOption('');
  await page.locator('#searchkind').selectOption('artifact');
  await page.locator('#searchquery').fill('artifact preview');
  await expect(page.locator('#searchresults .searchresult')).toHaveCount(1);
  await page.locator('#searchresults .searchresult').click();
  await expect(page.locator('#searchviewaction')).toContainText('Open artifact');
  await page.locator('#searchviewaction').getByRole('button', { name: 'Open artifact' }).click();
  await expect(page.locator('#viewer')).toBeVisible();
  await expect(page.locator('#vbody')).toContainText('Safe preview');
  await page.locator('#vclose').click();

  await page.locator('#search-rebuild').click();
  await expect(page.locator('#confirm')).toBeVisible();
  await page.locator('#confirm').getByRole('button', { name: 'rebuild index' }).click();
  await expect.poll(async () => (await fixtureState(page)).actions.at(-1).type)
    .toBe('search_rebuild');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test('editable exact provider handoff works from chat and Markdown with nested back', async ({ page }, testInfo) => {
  await reset(page);
  await page.evaluate(() => openSession('codex:thread-one'));
  await page.locator('#sctrl .ovbtn').click();
  await page.getByRole('menuitem', { name: /Continue in Claude/ }).click();
  await expect(page.locator('#handoffview')).toBeVisible();
  await expect(page.locator('#handoffpreview')).toHaveValue(/Source session: codex · codex:thread-one/);
  await page.locator('#handoffpreview').fill((await page.locator('#handoffpreview').inputValue()) + '\n\nEdited by the user.');
  const artifact = page.locator('.handoffartifact input');
  await artifact.uncheck();
  await expect(page.locator('#handoffpreview')).not.toHaveValue(/\/fixture\/artifact\.md/);
  await artifact.check();
  await expect(page.locator('#handoffpreview')).toHaveValue(/\/fixture\/artifact\.md/);
  await page.locator('.handoffoptions > .nfsel').selectOption('codex');
  await expect(page.locator('.handoffsubmit')).toContainText('Start Codex');
  await page.locator('.handoffoptions > .nfsel').selectOption('claude');
  await expect(page.locator('#handoffpreview')).toHaveValue(/Edited by the user/);
  await page.locator('.handoffadvanced summary').click();
  await page.locator('.handoffadvanced .nfsel').nth(0).selectOption('sonnet');
  await page.locator('.handoffadvanced .nfsel').nth(1).selectOption('high');
  await page.locator('.handoffadvanced input[type="checkbox"]').check();
  await page.locator('.handoffadvanced input[placeholder="optional"]').fill('handoff-ui');
  await page.locator('#handoffclose').click();
  await expect(page.locator('#handoffview')).toBeHidden();
  await expect(page.locator('#sview')).toBeVisible();

  await page.evaluate(() => openClosed('codex:closed'));
  await page.locator('#sctrl .ovbtn').click();
  await page.getByRole('menuitem', { name: /Continue in Claude/ }).click();
  await expect(page.locator('#handoffpreview')).toHaveValue(/Source session: codex · codex:closed/);
  await page.locator('#handoffclose').click();
  await expect(page.locator('#sview')).toContainText('Durable closed conversation');

  await page.evaluate(() => viewFile('codex:thread-one', encodeURIComponent('/fixture/artifact.md'),
    encodeURIComponent('artifact.md'), 'text', encodeURIComponent('artifact')));
  await expect(page.locator('#viewer')).toBeVisible();
  await page.locator('#vctrl .ovbtn').click();
  await expect(page.getByRole('menuitem', { name: /Continue in Codex/ })).toBeVisible();
  await page.getByRole('menuitem', { name: /Continue in Claude/ }).click();
  await page.locator('#handoffpreview').fill((await page.locator('#handoffpreview').inputValue()) + '\n\nAccepted edit.');
  await page.locator('.handoffsubmit').click();
  await expect(page.locator('#handoffview')).toBeHidden();
  await expect(page.locator('#sview')).toBeVisible();
  await expect(page.locator('#stitle2')).toContainText('Continued from Codex parity work');
  await expect(page.locator('#sact .handofflink')).toContainText('continued from codex · delivered');
  const state = await fixtureState(page);
  const action = state.actions.filter(item => item.type === 'handoff').at(-1);
  expect(action.provider).toBe('claude');
  expect(action.preview).toContain('Accepted edit.');
  expect(state.sessions.filter(item => item.session_id === 'handoff-1')).toHaveLength(1);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('provider-handoff.png'), fullPage: true });
});

test('handoff failure retries the same destination without creating a duplicate', async ({ page }) => {
  await reset(page, 'handoff-failure');
  await page.evaluate(() => openSession('claude-one'));
  await page.locator('#sctrl .ovbtn').click();
  await page.getByRole('menuitem', { name: /Continue in Codex/ }).click();
  await page.locator('.handoffsubmit').click();
  await expect(page.locator('.handoffstatus')).toContainText('fixture delivery failure');
  await expect(page.getByRole('button', { name: 'Retry delivery to the same session' })).toBeVisible();
  let state = await fixtureState(page);
  expect(state.sessions.filter(item => item.session_id === 'codex:handoff-1')).toHaveLength(1);
  await page.getByRole('button', { name: 'Retry delivery to the same session' }).click();
  await expect(page.locator('#handoffview')).toBeHidden();
  await expect(page.locator('#stitle2')).toContainText('Continued from Claude parser fix');
  state = await fixtureState(page);
  expect(state.sessions.filter(item => item.session_id === 'codex:handoff-1')).toHaveLength(1);
  const actions = state.actions.filter(item => item.type === 'handoff');
  expect(actions).toHaveLength(2);
  expect(actions[1].destination_session_id).toBe('codex:handoff-1');
});

test('handoff preview is action-token protected', async ({ page }) => {
  await reset(page);
  await page.context().clearCookies();
  await page.goto('/');
  await expect(page.locator('#route-now')).toBeVisible();
  await page.evaluate(() => openSession('claude-one'));
  await page.locator('#sctrl .ovbtn').click();
  await page.getByRole('menuitem', { name: /Continue in Codex/ }).click();
  await expect(page.locator('#handoffbody')).toContainText('needs Fleet’s action token');
  await expect(page.locator('#handoffbody .handoffsubmit')).toHaveCount(0);
  page.__failures.length = 0; // the two intentional 403 responses are the behavior under test
});

test('transcript search is action-token protected', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('#route-now')).toBeVisible();
  await goTo(page, 'search');
  await expect(page.locator('#searchstatus')).toContainText('action token');
  await expect(page.locator('#searchresults')).toContainText('Type a search or choose a filter.');
  await page.locator('#searchquery').fill('fleet');
  await expect(page.locator('#searchresults')).toContainText('action token');
  page.__failures = page.__failures.filter(message => !message.includes('403 (Forbidden)'));
});

test('GitHub repository links stay external without an action token', async ({ page }) => {
  await reset(page, 'workstreams');
  await page.context().clearCookies();
  await page.goto('/');
  await goTo(page, 'workstreams');
  await expect(page.locator('#workstreams')).not.toContainText('repo_center.py');
  const github=page.locator('[data-workstream-id="ws-fleet"] a.repoopen');
  await expect(github).toHaveText('GitHub ↗');
  await expect(github).toHaveAttribute('href','https://github.com/federbenjamin/fleet-dash');
  await expect(github).toHaveAttribute('target','_blank');
  await expect(page.locator('#repoview')).toHaveCount(0);
  page.__failures = page.__failures.filter(message => !message.includes('403 (Forbidden)'));
});

test('message Outbox records are action-token protected while fleet exposes counts only', async ({ page }) => {
  await page.request.post('/test/reset', { data: { scenario: 'base' } });
  const denied = await page.request.get('/api/outbox');
  expect(denied.status()).toBe(403);
  expect((await denied.json()).ok).toBe(false);
  const data = await (await page.request.get('/api/fleet')).json();
  expect(data.outbox_summary).toEqual({pending: 0, attention: 0, states: {}});
  expect(JSON.stringify(data)).not.toContain('scheduled message body');
});

test('shared fleet, spawn controls, usage, files, and capability-aware cost', async ({ page }, testInfo) => {
  await reset(page);
  await expect(page.locator('[data-now-filter="needs_you"]')).toHaveText('Needs you · 0');
  await expect(page.locator('[data-now-filter="working"]')).toHaveText('Working · 0');
  await expect(page.locator('[data-now-filter="available"]')).toHaveText('Available · 2');
  await expect(page.locator('[data-now-filter="subagents"]')).toHaveText('Subagents · 0');
  await expect(page.locator('#totals')).toHaveCount(0);
  await expect(page.locator('[data-sid="claude-one"]')).toContainText('Claude parser fix');
  const codex = page.locator('[data-sid="codex:thread-one"]');
  await expect(codex).toContainText('Codex parity work');
  await expect(page.locator('#usagechip')).toHaveText('Usage');
  await page.locator('#usagechip').click();
  await expect(page.locator('#usagepanel')).toBeVisible();
  await expect(page.locator('#usagebody')).toContainText('Claude Code');
  await expect(page.locator('#usagebody')).toContainText('Codex CLI');
  await expect(page.locator('#usagebody')).toContainText('12k lifetime tokens');
  const usageHeads = page.locator('#usagebody .uhead');
  await expect(usageHeads.nth(0).locator('.useg')).toHaveText([
    '·claude@example.com', '·active', '·59.59B local lifetime tokens']);
  await expect(usageHeads.nth(1).locator('.useg')).toHaveText([
    '·second@example.com']);
  await expect(usageHeads.nth(2).locator('.useg')).toHaveText([
    '·codex@example.com', '·pro', '·12k lifetime tokens']);
  await expect(page.locator('#usagebody .uaccount')).toHaveCount(2);
  await expect(page.locator('#usagebody')).toContainText('weekly');
  await expect(page.locator('#usagebody')).toContainText('Fable weekly');
  await expect(page.locator('#usagebody .uaccount').nth(0)).toContainText('41%');
  await expect(page.locator('#usagebody')).not.toContainText('GPT-5.3-Codex-Spark');
  await page.locator('#usagepanel').getByRole('button', { name: 'close usage' }).click();
  await expect(codex.locator('select.modesel')).toHaveCount(0);
  if (testInfo.project.name === 'desktop') {
    const terminal = codex.getByRole('button', { name: 'Attach' });
    const pin = codex.getByRole('button', { name: 'pin session', exact: true });
    await expect(terminal).toBeEnabled();
    await expect(pin).toBeVisible();
    expect(await terminal.evaluate((el) => el.nextElementSibling === document.querySelector(
      '[data-sid="codex:thread-one"] .spin'))).toBe(true);
    expect(await pin.evaluate((el) => getComputedStyle(el).borderStyle)).toBe('solid');
    await terminal.click();
    await expect.poll(async () => (await fixtureState(page)).actions.at(-1).type).toBe('focus');
  }

  for (let index = 0; index < 20; index += 1) await refresh(page);
  await expect(page.locator('[data-sid="claude-one"]')).toBeVisible();
  await expect(codex).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('fleet-overview.png'), fullPage: true });

  await page.getByRole('button', { name: '+ new coding session' }).click();
  const form = page.locator('.newform');
  await form.locator('select').first().selectOption('codex');
  await expect(form).toContainText('mode');
  await expect(form.locator('select').filter({ has: page.locator('option[value="plan"]') })).toHaveValue('plan');
  await expect(form).toContainText('gpt-5.4');

  await codex.getByRole('button', { name: /more/ }).click();
  await expect(codex).toContainText('unavailable — App Server reports tokens, not currency');
  await codex.getByText(/changed \/ generated files/).click();
  await codex.getByRole('button', { name: /artifact.md/ }).click();
  await expect(page.locator('#viewer')).toBeVisible();
  await expect(page.locator('#vbody')).toContainText('Safe preview');
  await page.screenshot({ path: testInfo.outputPath('artifact-preview.png'), fullPage: true });
});

test('session placement evidence is visible on cards and in a paged desktop/mobile rail', async ({ page }, testInfo) => {
  await reset(page);
  const card = page.locator('[data-sid="claude-one"]');
  await card.getByRole('button', { name: /more/ }).click();
  await card.locator('details.statewhy > summary').click();
  await expect(card.locator('.statewhybody')).toContainText('placement.default.available');
  await expect(card.locator('.statewhybody')).toContainText('Provider signal');

  await card.locator('.shead').click();
  await expect(page.locator('#sview')).toBeVisible();
  await page.getByRole('button', { name: /Why here/ }).click();
  const rail = page.locator('#sevidence');
  await expect(rail).toBeVisible();
  await expect(rail).toContainText('Why Fleet put this here');
  await expect(rail).toContainText('Available');
  await expect(rail).toContainText('placement.default.available');
  await expect(rail.locator('.evidenceevent')).toHaveCount(2);
  await expect(rail).toContainText('Working');
  if (testInfo.project.name.startsWith('mobile')) {
    expect(await rail.evaluate(element => element.getBoundingClientRect().width <= window.innerWidth)).toBe(true);
  }
  await page.screenshot({ path: testInfo.outputPath(`state-evidence-${testInfo.project.name}.png`) });
  await rail.getByRole('button', { name: 'close state evidence' }).click();
  await expect(rail).toBeHidden();
});

test('context gauge, Markdown peek, and shared reading width stay legible', async ({ page }, testInfo) => {
  await reset(page, 'markdown-peek');
  let card = page.locator('[data-sid="codex:thread-one"]');
  const peek = card.locator('.peekmd');
  await expect(peek.locator('h3')).toHaveText('Default width');
  await expect(peek.locator('b')).toHaveText('Fit the screen');
  await expect(peek.locator('code')).toHaveText('compact code');
  await expect(peek.locator('li')).toHaveCount(2);
  const peekRow = card.locator('.sessionpeek');
  await expect(peekRow).toHaveClass(/truncated/);
  const expand = peekRow.getByRole('button', { name: 'expand latest message' });
  await expect(expand).toHaveText('...');
  const peekBox = await peek.evaluate(el => ({ height: el.getBoundingClientRect().height,
    line: parseFloat(getComputedStyle(el).lineHeight) }));
  expect(peekBox.height).toBeLessThanOrEqual(peekBox.line * 2 + 1);
  await expect(card).toHaveClass(/fixedpeek/);
  await expand.click();
  await expect(peekRow).toHaveClass(/expanded/);
  await expect(peekRow.getByRole('button', { name: 'collapse latest message' })).toHaveText('Less');
  const expandedBox = await peek.evaluate(el => el.getBoundingClientRect().height);
  expect(expandedBox).toBeGreaterThan(peekBox.height);
  const expandedText = await peek.innerText();
  expect(expandedText.length).toBeLessThanOrEqual(500);
  expect(expandedText.endsWith('…')).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('markdown-peek-expanded.png'), fullPage: true });
  await peekRow.getByRole('button', { name: 'collapse latest message' }).click();
  await expect(peekRow).toHaveClass(/truncated/);
  await expect(card).toHaveClass(/fixedpeek/);
  const collapsedHeight = await card.evaluate(el => el.getBoundingClientRect().height);
  await page.evaluate(() => setNum('preview_session_lines', 5));
  await expect.poll(async () => card.evaluate(el => el.getBoundingClientRect().height))
    .toBeGreaterThan(collapsedHeight + 45);
  const fiveLineHeight = await card.evaluate(el => el.getBoundingClientRect().height);
  expect(fiveLineHeight - collapsedHeight).toBeLessThan(60);
  await page.screenshot({ path: testInfo.outputPath('markdown-peek-fixed.png'), fullPage: true });

  const gauge = await card.locator('.ctxbar').evaluate(el => ({
    background: getComputedStyle(el).backgroundColor,
    border: getComputedStyle(el).borderColor,
    borderWidth: getComputedStyle(el).borderTopWidth,
    card: getComputedStyle(el.closest('.card')).backgroundColor,
  }));
  expect(gauge.background).not.toBe(gauge.card);
  expect(gauge.border).not.toBe(gauge.card);
  expect(gauge.borderWidth).toBe('1px');
  await page.screenshot({ path: testInfo.outputPath('markdown-peek-context.png'), fullPage: true });

  await card.locator('.shead').click();
  let chat = page.locator('#sbody > .aconvo');
  const fit = await chat.evaluate(el => ({ width: el.getBoundingClientRect().width,
    available: el.parentElement.clientWidth - 28 }));
  expect(Math.abs(fit.width - fit.available)).toBeLessThan(2);
  await page.locator('#sclose').click();

  await goTo(page, 'settings');
  const settingsPage = page.locator('#settingsview');
  await expect(settingsPage).toBeVisible();
  await expect(settingsPage.locator('#settitle')).toHaveText('Settings');
  await expect(settingsPage.getByRole('button', { name: 'back to fleet' })).toBeVisible();
  expect(await settingsPage.evaluate(el => getComputedStyle(el).position)).toBe('fixed');
  await page.screenshot({ path: testInfo.outputPath('settings-page.png'), fullPage: true });
  await settingsPage.getByRole('button', { name: 'back to fleet' }).click();
  await expect(settingsPage).toBeHidden();

  await goTo(page, 'settings');
  await page.evaluate(() => history.back());
  await expect(settingsPage).toBeHidden();
  await expect(page.locator('[data-sid="codex:thread-one"]')).toBeVisible();

  await goTo(page, 'settings');
  const widthGroup = page.getByRole('group', { name: 'Full-screen reading width' });
  await expect(widthGroup.getByRole('button', { name: 'Fit the screen' }))
    .toHaveAttribute('aria-pressed', 'true');
  await widthGroup.getByRole('button', { name: /Centered/ }).click();
  await expect(page.locator('html')).toHaveAttribute('data-reader-width', 'centered');
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('data-reader-width', 'centered');

  card = page.locator('[data-sid="codex:thread-one"]');
  await card.locator('.shead').click();
  chat = page.locator('#sbody > .aconvo');
  const centeredChat = await chat.evaluate(el => ({ width: el.getBoundingClientRect().width,
    left: el.getBoundingClientRect().left,
    right: innerWidth - el.getBoundingClientRect().right }));
  expect(centeredChat.width).toBeLessThanOrEqual(760);
  expect(Math.abs(centeredChat.left - centeredChat.right)).toBeLessThan(2);
  await page.locator('#sclose').click();

  await card.getByRole('button', { name: /more/ }).click();
  await card.getByText(/changed \/ generated files/).click();
  await card.getByRole('button', { name: /artifact.md/ }).click();
  const doc = page.locator('#vbody > .mdoc');
  const centeredDoc = await doc.evaluate(el => ({ width: el.getBoundingClientRect().width,
    left: el.getBoundingClientRect().left,
    right: innerWidth - el.getBoundingClientRect().right }));
  expect(centeredDoc.width).toBeLessThanOrEqual(760);
  expect(Math.abs(centeredDoc.left - centeredDoc.right)).toBeLessThan(2);
  await page.locator('#vclose').click();

  await card.getByText(/completed agents/).click();
  await card.getByText('reviewer', { exact: true }).click();
  const agent = page.locator('#abody > .aconvo');
  const centeredAgent = await agent.evaluate(el => ({ width: el.getBoundingClientRect().width,
    left: el.getBoundingClientRect().left,
    right: innerWidth - el.getBoundingClientRect().right }));
  expect(centeredAgent.width).toBeLessThanOrEqual(760);
  expect(Math.abs(centeredAgent.left - centeredAgent.right)).toBeLessThan(2);
  await page.screenshot({ path: testInfo.outputPath('centered-reading-width.png'), fullPage: true });
});

test('large conversations load newest-first in bounded pages without losing older turns', async ({ page }) => {
  await reset(page, 'large-conversation');
  const initial = await (await page.request.get('/api/context?sid=codex%3Athread-one&limit=50')).json();
  expect(initial.messages).toHaveLength(50);
  expect(initial.message_total).toBe(205);
  expect(initial.next_cursor).toBe(155);
  await page.locator('[data-sid="codex:thread-one"] .shead').click();
  await expect(page.locator('#sbody')).toContainText('Conversation message 204');
  await expect(page.locator('#sbody')).not.toContainText('Conversation message 154');
  for (let pageIndex = 0; pageIndex < 4; pageIndex += 1) {
    await page.locator('#sbody .oldermsgs').click();
  }
  await expect(page.locator('#sbody .oldermsgs')).toHaveCount(0);
  await expect(page.locator('#sbody .cmsg')).toHaveCount(205);
  await expect(page.locator('#sbody')).toContainText('Conversation message 000');
  await page.locator('#sclose').click();

  const card = page.locator('[data-sid="codex:thread-one"]');
  await card.getByRole('button', { name: /more/ }).click();
  await card.getByText(/completed agents/).click();
  await card.getByText('reviewer', { exact: true }).click();
  await expect(page.locator('#abody')).toContainText('Subagent report 204');
  for (let pageIndex = 0; pageIndex < 4; pageIndex += 1) {
    await page.locator('#abody .oldermsgs').click();
  }
  await expect(page.locator('#abody .cmsg')).toHaveCount(205);
  await expect(page.locator('#abody')).toContainText('Subagent report 000');
  await page.locator('#aclose').click();

  await goTo(page, 'history');
  const closed = page.locator('[data-history-sid="closed-large"]');
  await closed.getByRole('button', { name: 'View' }).click();
  await expect(page.locator('#sbody')).toContainText('Closed report 204');
  for (let pageIndex = 0; pageIndex < 4; pageIndex += 1) {
    await page.locator('#sbody .oldermsgs').click();
  }
  await expect(page.locator('#sbody .cmsg')).toHaveCount(205);
  await expect(page.locator('#sbody')).toContainText('Closed report 000');
  await page.locator('#sclose').click();

  const invalid = await (await page.request.get(
    '/api/context?sid=codex%3Athread-one&limit=50&cursor=999')).json();
  expect(invalid.ok).toBe(false);
  for (const route of ['/api/closed_context?sid=closed-large',
    '/api/agent_context?sid=codex%3Athread-one&aid=child-one']) {
    const response = await (await page.request.get(route+'&limit=50&cursor=999')).json();
    expect(response.ok).toBe(false);
  }
});

test('session card surfaces distinguish active, available, and expanded information', async ({ page }, testInfo) => {
  const themeSurfaces = () => page.evaluate(() => {
    const probe = document.createElement('span');
    document.body.appendChild(probe);
    probe.style.background = 'var(--card)';
    const card = getComputedStyle(probe).backgroundColor;
    probe.style.background = 'var(--card2)';
    const card2 = getComputedStyle(probe).backgroundColor;
    probe.remove();
    return { card, card2 };
  });
  const cardStyle = locator => locator.evaluate(el => ({
    background: getComputedStyle(el).backgroundColor,
    opacity: getComputedStyle(el).opacity,
  }));

  await reset(page);
  let surfaces = await themeSurfaces();
  const idle = page.locator('[data-sid="codex:thread-one"]');
  await expect.poll(async () => (await cardStyle(idle)).background).toBe(surfaces.card);
  expect((await cardStyle(idle)).opacity).toBe('1');
  const peekFrame = await idle.evaluate(card => {
    const peek = card.querySelector('.sessionpeek');
    const more = card.querySelector('.morebtn');
    const body = peek.querySelector('.peekbody');
    const peekRect = peek.getBoundingClientRect();
    return {gap: more.getBoundingClientRect().top - peekRect.bottom,
      peekHeight: peekRect.height,
      bodyBottomGap: peekRect.bottom - body.getBoundingClientRect().bottom};
  });
  expect(peekFrame.gap).toBeLessThan(1);
  expect(peekFrame.peekHeight).toBeGreaterThan(40);
  expect(peekFrame.bodyBottomGap).toBeLessThan(8);

  await reset(page, 'subagent');
  surfaces = await themeSurfaces();
  const running = page.locator('[data-sid="codex:thread-one"]');
  await expect.poll(async () => (await cardStyle(running)).background).toBe(surfaces.card2);
  expect(await running.locator('.shead').evaluate(el => getComputedStyle(el).backgroundColor))
    .toBe(surfaces.card2);
  const activePeekFrame = await running.evaluate(card => {
    const peek = card.querySelector('.sessionpeek');
    const lines = Number(getComputedStyle(card).getPropertyValue('--session-card-lines'));
    const peekRect = peek.getBoundingClientRect();
    const agentsRect = card.querySelector('.agents').getBoundingClientRect();
    return {height: peekRect.height, expected: 13 + lines * 17.4,
      gapToAgents: agentsRect.top - peekRect.bottom};
  });
  expect(activePeekFrame.height).toBeGreaterThanOrEqual(activePeekFrame.expected - 1);
  expect(activePeekFrame.gapToAgents).toBeLessThan(1);
  const more = running.getByRole('button', { name: /more/ });
  expect(await more.evaluate(el => getComputedStyle(el).paddingTop)).toBe('2px');
  expect(await more.evaluate(el => el.getBoundingClientRect().height)).toBeLessThan(22);
  await more.click();
  await expect(running.locator('.detail')).toBeVisible();
  expect(await running.locator('.detail').evaluate(el => getComputedStyle(el).backgroundColor))
    .toBe(surfaces.card);
  await page.screenshot({ path: testInfo.outputPath('active-card-contrast.png'), fullPage: true });

  await page.request.post('/test/reset', { data: { scenario: 'organization' } });
  await page.goto('/?token=abcdef123456');
  await goTo(page, 'history');
  const history = page.locator('#history');
  await expect(history.locator('[data-history-sid="codex:thread-one"]')).toContainText('External');
  await expect(history.locator('[data-history-sid="claude-dormant"]')).toContainText('Inactive');
  await expect(page.locator('#headless')).toHaveCount(0);
  await expect(page.locator('#dormant')).toHaveCount(0);
});

test('quiet in-flight subagents use an uncertain amber signal, not stopped red', async ({ page }) => {
  await reset(page, 'stalled-agent');
  const dot = page.locator('[data-sid="codex:thread-one"] .dot.stalled');
  await expect(dot).toHaveAttribute('aria-label', 'quiet — may still be working');
  const colors = await dot.evaluate(el => {
    const probe = document.createElement('span');
    probe.style.background = 'var(--amber)';
    document.body.appendChild(probe);
    const expected = getComputedStyle(probe).backgroundColor;
    probe.remove();
    return { actual: getComputedStyle(el).backgroundColor, expected };
  });
  expect(colors.actual).toBe(colors.expected);
});

test('full chat keeps main and subagent work visible in a sticky activity footer', async ({ page }) => {
  await reset(page, 'subagent');
  await page.evaluate(() => openSession('codex:thread-one'));
  const activity = page.locator('#sactivity');
  await expect(activity).toBeVisible();
  await expect(activity).toContainText('Main session working');
  await expect(activity).toContainText('1 active subagent');
  await activity.locator('summary').click();
  await expect(activity).toContainText('Review protocol mapping');
  await expect(activity.locator('details')).toHaveAttribute('open', '');

  await reset(page, 'cross-client-active');
  await page.evaluate(() => openSession('codex:thread-one'));
  await expect(activity).toContainText('Main session working');
  await expect(activity).not.toContainText('active subagent');

  await reset(page, 'base');
  await page.evaluate(() => openSession('codex:thread-one'));
  await expect(activity).toBeHidden();
});

test('Codex mode, send, UI stop, and completed lifecycle', async ({ page }) => {
  await reset(page);
  const card = page.locator('[data-sid="codex:thread-one"]');
  await card.locator('.shead').click();
  const attach = page.locator('#sctrl > .termbtn');
  await expect(attach).toHaveText('Attach');
  await expect(attach).toBeEnabled();
  expect(await attach.evaluate(el => el.nextElementSibling.classList.contains('ovwrap'))).toBe(true);
  await attach.click();
  await expect.poll(async () => (await fixtureState(page)).actions.at(-1).type).toBe('focus');
  await page.getByRole('button', { name: 'session actions' }).click();
  await page.getByRole('button', { name: 'Default', exact: true }).click();
  await expect.poll(async () => (await fixtureState(page)).sessions[1].collaboration_mode)
    .toBe('default');

  const input = page.locator('#sft-codex\\:thread-one');
  await input.fill('Run the deterministic check');
  await page.locator('#sact').getByRole('button', { name: 'send' }).click();
  await refresh(page);
  await expect(page.locator('#sctrl > .termbtn')).toHaveText('turn active');
  await expect(page.locator('#sctrl > .termbtn')).toBeDisabled();
  await page.getByRole('button', { name: 'session actions' }).click();
  await page.getByRole('menuitem', { name: /Stop turn/ }).click();
  await expect(page.locator('#confirm')).toContainText('Stop this turn?');
  await page.locator('#confirm').getByRole('button', { name: 'stop the turn' }).click();
  await refresh(page);
  await expect(card.locator('.chip')).toContainText('Available');
  await expect(page.locator('#sctrl > .termbtn')).toHaveText('Attach');
  await expect(page.locator('#sctrl > .termbtn')).toBeEnabled();
});

test('Claude permission modes are capability-gated and bypass always warns', async ({ page }) => {
  await reset(page);
  const card = page.locator('[data-sid="claude-one"]');
  await card.locator('.shead').click();
  await page.getByRole('button', { name: 'session actions' }).click();
  await expect(page.getByRole('button', { name: 'Auto', exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: /Don't ask/ })).toBeDisabled();
  await page.getByRole('button', { name: 'Accept edits', exact: true }).click();
  await expect.poll(async () => (await fixtureState(page)).sessions[0].permission_mode)
    .toBe('acceptEdits');

  await page.getByRole('button', { name: 'session actions' }).click();
  await page.getByRole('button', { name: 'Bypass permissions', exact: true }).click();
  await expect(page.locator('#confirm')).toContainText('Use Bypass permissions?');
  await expect(page.locator('#confirm')).toContainText('isolated, disposable environment');
  await page.locator('#confirm').getByRole('button', { name: 'cancel' }).click();
  expect((await fixtureState(page)).sessions[0].permission_mode).toBe('acceptEdits');

  await page.getByRole('button', { name: 'session actions' }).click();
  await page.getByRole('button', { name: 'Bypass permissions', exact: true }).click();
  await page.locator('#confirm').getByRole('button', { name: 'use bypass permissions' }).click();
  await expect.poll(async () => (await fixtureState(page)).sessions[0].permission_mode)
    .toBe('bypassPermissions');

  await page.locator('#sclose').click();
  await card.getByRole('button', { name: /more/ }).click();
  await card.getByText('session info', { exact: true }).click();
  const selector = card.locator('select.permissionselect');
  await expect(selector).toHaveValue('bypassPermissions');
  await selector.selectOption('plan');
  await expect.poll(async () => (await fixtureState(page)).sessions[0].permission_mode)
    .toBe('plan');
});

test('brand-new Claude sessions are interactive before the first transcript exists', async ({ page }) => {
  await reset(page, 'claude-starting');
  const card = page.locator('[data-sid="claude-one"]');
  await expect(card).toBeVisible();
  await expect(card).toContainText('New Claude session');
  await expect(card).toContainText('Available');
  await expect(card).not.toContainText('$0.00');
  await card.locator('.shead').click();
  await expect(page.locator('#sbody')).toContainText('no conversation yet');
  await expect(page.locator('#sact textarea[placeholder^="send a message"]')).toBeVisible();
  const strip=page.locator('#sact .statusstrip');
  await expect(strip).toContainText('claude · high');
  await expect(strip).not.toContainText('Ctx:');
  await expect(strip).not.toContainText('→0');
  await expect(strip.locator('.status-cache,.status-graph,.status-cost')).toHaveCount(0);
});

test('desktop-owned Codex work is active without unsafe controls', async ({ page }) => {
  await page.request.post('/test/reset', { data: { scenario: 'cross-client-active' } });
  await page.goto('/?token=abcdef123456');
  const card = page.locator('[data-sid="codex:thread-one"]');

  await expect(page.locator('#working')).toContainText('Working · 1');
  await expect(card.locator('.chip')).toContainText('Working elsewhere');
  await expect(card).toContainText('Working in ChatGPT desktop.');
  if ((await page.viewportSize()).width > 700)
    await expect(card.getByRole('button', { name: 'view only' })).toBeDisabled();
  await card.locator('.shead').click();
  const viewOnly = page.locator('#sctrl > .termbtn');
  await expect(viewOnly).toHaveText('view only');
  await expect(viewOnly).toBeDisabled();
  expect(await viewOnly.evaluate(el => el.nextElementSibling.classList.contains('ovwrap'))).toBe(true);
  await page.getByRole('button', { name: 'session actions' }).click();
  await expect(page.getByRole('menuitem', { name: /Stop turn/ })).toBeDisabled();
  await expect(page.getByRole('menuitem', { name: /Close session/ })).toBeDisabled();
  await expect(page.locator('#sft-codex\\:thread-one')).toHaveCount(0);
});

test('subagent transcripts stay isolated when different parents reuse an agent id', async ({ page }) => {
  await reset(page, 'agent-collision');
  const openAgentFor = async sid => {
    const card = page.locator(`[data-sid="${sid}"]`);
    await card.getByRole('button', { name: /more/ }).click();
    await card.getByText(/completed agents/).click();
    await card.getByText('reviewer', { exact: true }).click();
  };
  await openAgentFor('codex:thread-one');
  await expect(page.locator('#abody')).toContainText('First parent report');
  await page.locator('#aclose').click();
  await openAgentFor('codex:thread-two');
  await expect(page.locator('#abody')).toContainText('Second parent report');
  await expect(page.locator('#abody')).not.toContainText('First parent report');
});

test('overflow menus cover chat, Markdown, subagents, theme, and close history', async ({ page }, testInfo) => {
  await reset(page);
  let card = page.locator('[data-sid="codex:thread-one"]');
  await card.locator('.shead').click();
  await page.getByRole('button', { name: 'session actions' }).click();
  await expect(page.getByRole('button', { name: 'Plan', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByRole('menuitem', { name: /Appearance.*light \/ dark/ })).toBeVisible();
  await expect(page.getByRole('menuitem', { name: /Stop turn/ })).toBeDisabled();
  await expect(page.getByRole('menuitem', { name: /Close session.*move to history/ })).toBeEnabled();
  await page.getByRole('menuitem', { name: /Appearance.*light \/ dark/ }).click();
  await expect(page.locator('#sbody')).toHaveClass(/light/);
  await page.locator('#sclose').click();

  card = page.locator('[data-sid="codex:thread-one"]');
  await card.getByRole('button', { name: /more/ }).click();
  await card.getByText(/changed \/ generated files/).click();
  await card.getByRole('button', { name: /artifact.md/ }).click();
  await page.getByRole('button', { name: 'viewer actions' }).click();
  await expect(page.getByRole('button', { name: 'Plan', exact: true })).toBeVisible();
  await expect(page.getByRole('menuitem', { name: /Appearance.*light \/ dark/ })).toBeVisible();
  await expect(page.getByRole('menuitem', { name: /Stop turn/ })).toBeDisabled();
  await expect(page.getByRole('menuitem', { name: /Close session/ })).toBeEnabled();
  await page.screenshot({ path: testInfo.outputPath('viewer-overflow-menu.png'), fullPage: true });
  await page.locator('#vclose').click();

  await page.request.post('/test/reset', { data: { scenario: 'subagent' } });
  await page.reload();
  card = page.locator('[data-sid="codex:thread-one"]');
  await card.getByText('reviewer', { exact: true }).click();
  await expect(page.locator('#abody')).toHaveClass(/light/);
  await page.getByRole('button', { name: 'subagent actions' }).click();
  await expect(page.getByRole('menuitem', { name: /Appearance.*light \/ dark/ })).toBeVisible();
  await expect(page.getByRole('menuitem', { name: /Stop parent turn/ })).toBeEnabled();
  await expect(page.getByRole('menuitem', { name: /Close session/ })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Plan', exact: true })).toHaveCount(0);
  await page.getByRole('menuitem', { name: /Stop parent turn/ }).click();
  await expect(page.locator('#confirm')).toContainText('parent session');
  await page.locator('#confirm').getByRole('button', { name: 'cancel' }).click();
  await page.locator('#aclose').click();

  await reset(page);
  const claude = page.locator('[data-sid="claude-one"]');
  await claude.locator('.shead').click();
  const openTerminal = page.locator('#sctrl > .termbtn');
  await expect(openTerminal).toHaveText('Terminal');
  await expect(openTerminal).toBeEnabled();
  expect(await openTerminal.evaluate(el => el.nextElementSibling.classList.contains('ovwrap'))).toBe(true);
  await openTerminal.click();
  await expect.poll(async () => (await fixtureState(page)).actions.at(-1).type).toBe('focus');
  await page.getByRole('button', { name: 'session actions' }).click();
  await expect(page.getByRole('button', { name: 'Plan', exact: true })).toBeVisible();
  await page.getByRole('menuitem', { name: /Close session/ }).click();
  await expect(page.locator('#confirm')).toContainText('iTerm tab stays open');
  await page.locator('#confirm').getByRole('button', { name: 'cancel' }).click();
  await page.locator('#sclose').click();

  card = page.locator('[data-sid="codex:thread-one"]');
  await card.locator('.shead').click();
  const input = page.locator('#sft-codex\\:thread-one');
  await input.fill('Work that close must stop');
  await page.locator('#sact').getByRole('button', { name: 'send' }).click();
  await refresh(page);
  await page.getByRole('button', { name: 'session actions' }).click();
  await page.getByRole('menuitem', { name: /Close session/ }).click();
  await expect(page.locator('#confirm')).toContainText('archived');
  await expect(page.locator('#confirm')).toContainText('Session history');
  await page.locator('#confirm').getByRole('button', { name: 'stop and close' }).click();
  await expect(page.locator('#sview')).toBeHidden();
  await expect(page.locator('[data-sid="codex:thread-one"]')).toHaveCount(0);
  await goTo(page, 'history');
  await expect(page.locator('#history')).toContainText('2 sessions');
  await expect(page.locator('#history')).toContainText('Codex parity work');
});

test('secondary-worktree close preserves by default and force removal lists every risk', async ({ page }) => {
  await reset(page, 'close-worktree-dirty');
  await page.locator('[data-sid="claude-one"] .shead').click();
  await page.getByRole('button', { name: 'session actions' }).click();
  await page.getByRole('menuitem', { name: /Close session/ }).click();
  const modal = page.locator('#confirm');
  await expect(modal).toContainText('Secondary worktree');
  await expect(modal).toContainText('engine.py');
  await expect(modal).toContainText('static/app.js');
  await expect(modal).toContainText('notes.txt');
  await expect(modal).toContainText('build/cache.bin');
  await expect(modal.getByRole('button', { name: 'close · remove clean worktree' })).toBeDisabled();
  await modal.getByRole('button', { name: 'force remove dirty worktree' }).click();
  await expect(modal).toContainText('permanently deletes every listed worktree file');
  await modal.getByRole('button', { name: 'close and force remove' }).click();
  await expect(page.locator('#sview')).toBeHidden();
  const state = await fixtureState(page);
  expect(state.worktree_removed).toBe(true);
  expect(state.actions.slice(-3).map(item => item.type)).toEqual([
    'close_preview', 'close', 'worktree_cleanup']);
  expect(state.actions.at(-1).force).toBe(true);

  await reset(page, 'close-worktree-shared');
  await page.locator('[data-sid="claude-one"] .shead').click();
  await page.getByRole('button', { name: 'session actions' }).click();
  await page.getByRole('menuitem', { name: /Close session/ }).click();
  await expect(modal).toContainText('Codex parity work');
  await expect(modal.getByRole('button', { name: 'close · remove clean worktree' })).toBeDisabled();
  await expect(modal.getByRole('button', { name: 'force remove dirty worktree' })).toHaveCount(0);
  await modal.getByRole('button', { name: 'cancel' }).click();
});

test('cleanup failure closes the session but reports the preserved worktree', async ({ page }) => {
  await reset(page, 'close-worktree-cleanup-failure');
  await page.locator('[data-sid="claude-one"] .shead').click();
  await page.getByRole('button', { name: 'session actions' }).click();
  await page.getByRole('menuitem', { name: /Close session/ }).click();
  await page.locator('#confirm').getByRole('button', { name: 'force remove dirty worktree' }).click();
  await page.locator('#confirm').getByRole('button', { name: 'close and force remove' }).click();
  await expect(page.locator('#confirm')).toContainText('Session closed · worktree preserved');
  await expect(page.locator('#confirm')).toContainText('fixture Git removal failed');
  await expect(page.locator('#sview')).toBeHidden();
  await page.locator('#confirm').getByRole('button', { name: 'close' }).click();
  expect((await fixtureState(page)).worktree_removed).not.toBe(true);
});

test('single, multi, free-text, dismiss, invalid, and stale questions', async ({ page }) => {
  await reset(page, 'single-question');
  await openAction(page, 'codex:thread-one');
  await expect(page.locator('#sact')).toContainText('How broad should the change be?');
  await page.locator('#sact').getByRole('button', { name: /Focused/ }).click();
  await expect.poll(async () => (await fixtureState(page)).actions.at(-1).type).toBe('option');

  await page.request.post('/test/reset', { data: { scenario: 'single-question' } });
  await page.reload();
  await openAction(page, 'codex:thread-one');
  await page.locator('#oth-smsg-codex\\:thread-one').fill('Only the adapter');
  await page.locator('#sact').getByRole('button', { name: 'answer' }).click();
  await expect.poll(async () => (await fixtureState(page)).actions.at(-1).other)
    .toBe('Only the adapter');

  const stale = await page.request.post('/api/act', { headers: { 'X-Act-Token': 'abcdef123456' },
    data: { type: 'option', session_id: 'codex:thread-one', nonce: 'old', digits: [1] } });
  expect((await stale.json()).ok).toBe(false);
  const invalid = await page.request.post('/api/act', { headers: { 'X-Act-Token': 'abcdef123456' },
    data: { type: 'option', session_id: 'codex:thread-one', nonce: 'q1', digits: [] } });
  expect((await invalid.json()).ok).toBe(false);

  await page.request.post('/test/reset', { data: { scenario: 'multi-question' } });
  await page.reload();
  await openAction(page, 'codex:thread-one');
  await page.locator('#sact').getByRole('button', { name: 'Desktop' }).click();
  await page.locator('#sact .mqarr').last().click();
  await page.locator('#sact').getByRole('button', { name: 'Full' }).click();
  await page.locator('#sact').getByRole('button', { name: 'submit all answers' }).click();
  await expect.poll(async () => (await fixtureState(page)).actions.at(-1).type).toBe('multiq');

  await page.request.post('/test/reset', { data: { scenario: 'single-question' } });
  await page.reload();
  await openAction(page, 'codex:thread-one');
  await page.locator('#sact .xbtn').click();
  await expect.poll(async () => (await fixtureState(page)).actions.at(-1).type).toBe('dismiss');
});

test('messages and question answers render optimistically and recover from failure', async ({ page }) => {
  await reset(page);
  await page.locator('[data-sid="codex:thread-one"] .shead').click();
  const input = page.locator('#sft-codex\\:thread-one');
  await input.fill('Ship the optimistic message');
  await sendModifiedReturn(page, input);
  const sending = page.locator('#sbody .optimistic').filter({ hasText: 'Ship the optimistic message' });
  await expect(sending).toBeVisible();
  await expect(sending.getByLabel('sending')).toBeVisible();
  await expect.poll(async () => page.evaluate(() =>
    window.__fleetPerf.summary().input_feedback_ms.p95)).toBeLessThan(100);
  await page.request.post('/test/confirm', { data: { session_id: 'codex:thread-one',
    text: 'Ship the optimistic message' } });
  await refresh(page);
  await expect(page.locator('#sbody .optimistic')).toHaveCount(0);
  await expect(page.locator('#sbody .cmsg.user').filter({
    hasText: 'Ship the optimistic message' })).toBeVisible();

  await page.request.post('/test/reset', { data: { scenario: 'single-question' } });
  await page.reload();
  await openAction(page, 'codex:thread-one');
  await page.locator('#sact').getByRole('button', { name: /Focused/ }).click();
  const answer = page.locator('#sbody .optimistic').filter({ hasText: 'Scope: Focused' });
  await expect(answer).toBeVisible();
  await expect(answer.locator('.delivery')).toHaveCount(0);
  await page.request.post('/test/confirm', { data: { session_id: 'codex:thread-one',
    kind: 'answer', answers: [{ header: 'Scope', q: 'How broad?', a: 'Focused' }] } });
  await refresh(page);
  await expect(page.locator('#sbody .optimistic')).toHaveCount(0);
  await expect(page.locator('#sbody')).toContainText('Focused');

  await page.request.post('/test/reset', { data: { scenario: 'answer-failure' } });
  await page.reload();
  await openAction(page, 'codex:thread-one');
  await page.locator('#sact').getByRole('button', { name: /Focused/ }).click();
  const failedAnswer = page.locator('#sbody .optimistic').filter({ hasText: 'Scope: Focused' });
  const restoreAnswer = failedAnswer.getByRole('button', {
    name: 'send failed; restore message' });
  await expect(restoreAnswer).toBeVisible();
  await restoreAnswer.click();
  await expect(page.locator('#sbody .optimistic')).toHaveCount(0);
  await expect(page.locator('#sact')).toContainText('How broad should the change be?');

  await page.request.post('/test/reset', { data: { scenario: 'base' } });
  await page.reload();
  await page.locator('[data-sid="codex:thread-one"] .shead').click();
  const timeoutInput = page.locator('#sft-codex\\:thread-one');
  await timeoutInput.fill('Wait for transcript confirmation');
  await sendModifiedReturn(page, timeoutInput);
  const timedOut = page.locator('#sbody .optimistic').filter({
    hasText: 'Wait for transcript confirmation' });
  const restoreTimedOut = timedOut.getByRole('button', {
    name: 'send failed; restore message' });
  await expect(restoreTimedOut).toBeVisible({ timeout: 16_000 });
  await restoreTimedOut.click();
  await expect(timeoutInput).toHaveValue('Wait for transcript confirmation');

  await page.request.post('/test/reset', { data: { scenario: 'send-failure' } });
  await page.reload();
  await page.locator('[data-sid="codex:thread-one"] .shead').click();
  const failedInput = page.locator('#sft-codex\\:thread-one');
  await failedInput.fill('Restore this message');
  await sendModifiedReturn(page, failedInput);
  const failed = page.locator('#sbody .optimistic').filter({ hasText: 'Restore this message' });
  const restore = failed.getByRole('button', { name: 'send failed; restore message' });
  await expect(restore).toBeVisible();
  await restore.click();
  await expect(page.locator('#sbody .optimistic')).toHaveCount(0);
  await expect(failedInput).toHaveValue('Restore this message');
});

test('message composers use Return for newlines and an explicit modified Return to send', async ({ page }) => {
  await reset(page);
  await page.locator('[data-sid="codex:thread-one"] .shead').click();
  const composer = page.locator('#sft-codex\\:thread-one');
  await composer.fill('First line');
  await composer.press('Enter');
  await expect(composer).toHaveValue('First line\n');
  expect((await fixtureState(page)).actions.filter(item => item.type === 'text')).toHaveLength(0);
  await composer.fill('First line\nSecond line');
  await sendModifiedReturn(page, composer);
  await expect.poll(async () => (await fixtureState(page)).actions.at(-1)).toMatchObject({
    type: 'text', text: 'First line\nSecond line' });

  await page.request.post('/test/reset', { data: { scenario: 'subagent' } });
  await page.reload();
  await page.locator('[data-sid="codex:thread-one"]').getByText('reviewer', { exact: true }).click();
  const relay = page.locator('#aft');
  await relay.fill('Relay line');
  await relay.press('Enter');
  await expect(relay).toHaveValue('Relay line\n');
  expect((await fixtureState(page)).actions.filter(item => item.type === 'relay')).toHaveLength(0);
  await relay.fill('Relay line\nMore detail');
  await sendModifiedReturn(page, relay);
  await expect.poll(async () => (await fixtureState(page)).actions.at(-1)).toMatchObject({
    type: 'relay', text: 'Relay line\nMore detail' });
});

test('phone images persist across reload and send through the owning provider', async ({ page }) => {
  await reset(page);
  await page.locator('[data-sid="codex:thread-one"] .shead').click();
  const image={name:'phone-photo.png',mimeType:'image/png',
    buffer:Buffer.from([137,80,78,71,13,10,26,10,0,0,0,0])};
  await page.locator('#sact input[type="file"]').setInputFiles(image);
  await expect(page.locator('#sact .image-draft')).toContainText('phone-photo.png');
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem('fleet.imageDrafts.v1')||'{}')
    ['codex:thread-one']?.length)).toBe(1);

  await page.reload();
  await page.evaluate(() => openSession('codex:thread-one'));
  await expect(page.locator('#sact .image-draft')).toContainText('phone-photo.png');
  await page.locator('#sft-codex\\:thread-one').fill('Inspect the mobile screenshot');
  await page.locator('#sact').getByRole('button',{name:'send'}).click();
  await expect(page.locator('#sbody .image-receipt')).toContainText('1 image');
  await expect.poll(async () => (await fixtureState(page)).uploads.length).toBe(1);
  await expect.poll(async () => (await fixtureState(page)).actions.find(action=>
    action.type==='image_text')).toMatchObject({session_id:'codex:thread-one',
      text:'Inspect the mobile screenshot'});
  expect((await fixtureState(page)).actions.find(action=>action.type==='image_text').upload_ids)
    .toHaveLength(1);
  await expect.poll(() => page.evaluate(() => localStorage.getItem('fleet.imageDrafts.v1')))
    .toBeNull();
});

test('images selected offline survive reload and flush exactly once after reconnection', async ({ page,context }) => {
  await reset(page);
  await page.evaluate(() => navigator.serviceWorker.ready);
  await expect.poll(() => page.evaluate(async () => Boolean(await caches.match('/api/fleet')))).toBe(true);
  await page.reload();
  await context.setOffline(true);
  try{
    await page.evaluate(() => openSession('claude-one'));
    await page.locator('#sact input[type="file"]').setInputFiles({name:'offline-photo.jpg',
      mimeType:'image/jpeg',buffer:Buffer.from([255,216,255,224,0,16,74,70,73,70])});
    await page.locator('#sft-claude-one').fill('Review this offline photo');
    await page.locator('#sact').getByRole('button',{name:'send'}).click();
    await expect(page.locator('#sbody [aria-label="queued offline"]')).toBeVisible();
    expect(await page.evaluate(() => JSON.parse(localStorage.getItem('fleet.offlineMessages.v1')||'[]')[0]
      .imageIds.length)).toBe(1);
    await page.reload({waitUntil:'domcontentloaded'});
    await page.evaluate(() => openSession('claude-one'));
    await expect(page.locator('#sbody [aria-label="queued offline"]')).toBeVisible();

    await context.setOffline(false);
    await expect.poll(async () => (await fixtureState(page)).uploads.length).toBe(1);
    await expect.poll(async () => (await fixtureState(page)).actions.filter(action=>
      action.type==='image_text'&&action.session_id==='claude-one').length).toBe(1);
    await page.waitForTimeout(2200);
    expect((await fixtureState(page)).uploads).toHaveLength(1);
    expect((await fixtureState(page)).actions.filter(action=>
      action.type==='image_text'&&action.session_id==='claude-one')).toHaveLength(1);
    page.__failures=page.__failures.filter(message=>
      !/ERR_INTERNET_DISCONNECTED|net::ERR_FAILED|Failed to fetch/i.test(message));
  }finally{await context.setOffline(false);}
});

test('unsent text drafts survive rerenders and reloads until sent or manually deleted', async ({ page }) => {
  await reset(page, 'base');
  await page.locator('#nowfilter').fill('codex');
  await page.locator('[data-sid="codex:thread-one"] .shead').click();
  await page.locator('#sft-codex\\:thread-one').fill('unsent composer draft');
  await page.reload();
  await expect(page.locator('#nowfilter')).toHaveValue('codex');
  await page.locator('[data-sid="codex:thread-one"] .shead').click();
  const composer=page.locator('#sft-codex\\:thread-one');
  await expect(composer).toHaveValue('unsent composer draft');
  await composer.fill('');
  await page.reload();
  await page.locator('[data-sid="codex:thread-one"] .shead').click();
  await expect(page.locator('#sft-codex\\:thread-one')).toHaveValue('');

  await page.locator('#sclose').click();
  await page.getByRole('button', { name: '+ new coding session' }).click();
  await page.locator('.newform input[data-draft-key="new:directory"]').fill('/Users/test/custom');
  await page.locator('.newform textarea[data-draft-key="new:message"]').fill('persistent new-session draft');
  await page.reload();
  await page.getByRole('button', { name: '+ new coding session' }).click();
  await expect(page.locator('.newform input[data-draft-key="new:directory"]')).toHaveValue('/Users/test/custom');
  await expect(page.locator('.newform textarea[data-draft-key="new:message"]')).toHaveValue('persistent new-session draft');
});

test('new sessions open a provisional card and chat before native startup returns', async ({ page }) => {
  await reset(page, 'spawn-slow');
  await page.getByRole('button', { name: '+ new coding session' }).click();
  const form = page.locator('.newform');
  const permission = form.locator('select').filter({ has: page.locator('option[value="dontAsk"]') });
  await expect(permission).toHaveValue('default');
  await expect(permission.locator('option[value="auto"]')).toHaveCount(1);
  await expect(permission.locator('option[value="dontAsk"]')).toHaveText("Don't ask");
  await form.locator('select').first().selectOption('codex');
  await form.locator('select').nth(1).selectOption('/Users/test/fleet-dash');
  await form.locator('textarea.nfmessage').fill('Start with immediate feedback');
  await form.getByRole('button', { name: /start session/ }).click();

  await expect(page.locator('#sview')).toBeVisible();
  await expect(page.locator('#sbody')).toContainText('Start with immediate feedback');
  await expect(page.locator('#sbody')).toContainText('Starting session');
  await expect(page.locator('[data-sid^="spawn-"]')).toBeVisible();
  await expect(page.locator('[data-now-filter="working"]')).toHaveText('Working · 1');
  await expect.poll(async () => page.evaluate(() =>
    window.__fleetPerf.summary().input_feedback_ms.p95)).toBeLessThan(100);

  await expect(page.locator('[data-sid="codex:new"]')).toBeVisible({ timeout: 5_000 });
  await expect(page.locator('#sview')).toBeVisible();
  await expect(page.locator('#sbody')).toContainText('Start with immediate feedback');
  expect((await fixtureState(page)).actions.filter(item => item.type === 'spawn')).toHaveLength(1);
});

test('new-session forecast ignores stale model results and rejected startup restores exact input', async ({ page }) => {
  await reset(page, 'forecast-race');
  await page.getByRole('button', { name: '+ new coding session' }).click();
  let form = page.locator('.newform');
  await form.locator('select').first().selectOption('codex');
  const model = form.locator('select').filter({ has: page.locator('option[value="gpt-5.4"]') });
  await model.selectOption('gpt-5.4');
  await expect(form.locator('.spawnforecast')).toContainText('Updating forecast');
  await model.selectOption('gpt-5.3-codex');
  await expect(form.locator('.spawnforecast')).toContainText('53k tokens');
  await expect(form.locator('.spawnforecast')).not.toContainText('54k tokens');

  await page.request.post('/test/reset', { data: { scenario: 'spawn-failure' } });
  await page.reload();
  await page.getByRole('button', { name: '+ new coding session' }).click();
  form = page.locator('.newform');
  await form.locator('select').nth(1).selectOption('/Users/test/fleet-dash');
  await form.locator('textarea.nfmessage').fill('Keep this exact startup message');
  await form.getByRole('button', { name: /start session/ }).click();
  await expect(page.locator('#sbody')).toContainText('fixture startup rejected');
  await page.locator('#sact').getByRole('button', { name: 'restore setup' }).click();
  await expect(page.locator('.newform textarea.nfmessage')).toHaveValue('Keep this exact startup message');
});

test('fleet cards show submitting, submitted, and failed quick-response feedback', async ({ page }) => {
  const fleetFeedback = sid => page.locator(
    `[data-action-sid="${sid}"] .quickfeedback, [data-sid="${sid}"] .quickfeedback`);
  await reset(page, 'claude-question-slow');
  let card = await openAction(page, 'claude-one');
  await page.locator('#sact').getByRole('button', { name: /Focused/ }).click();
  await expect(page.locator('#sbody .optimistic').getByLabel('sending')).toBeVisible();
  await page.locator('#sclose').click();
  await expect(page.locator('#sview')).toBeHidden();
  let feedback = fleetFeedback('claude-one');
  await expect(feedback).toContainText('Submitting');
  await expect(feedback).toContainText('Scope: Focused');
  await expect(feedback.getByLabel('sending quick response')).toBeVisible();
  await expect.poll(async () => page.evaluate(() =>
    window.__fleetPerf.summary().input_feedback_ms.p95)).toBeLessThan(100);
  await expect(feedback).toContainText('Submitted', { timeout: 5_000 });
  await expect(feedback.getByLabel('response submitted')).toBeVisible();

  await page.request.post('/test/confirm', { data: { session_id: 'claude-one',
    kind: 'answer', answers: [{ header: 'Scope', q: 'How broad?', a: 'Focused' }] } });
  await refresh(page);
  await expect(fleetFeedback('claude-one')).toHaveCount(0);

  await reset(page, 'claude-question-failure');
  card = await openAction(page, 'claude-one');
  await page.locator('#sact').getByRole('button', { name: /Focused/ }).click();
  await page.locator('#sclose').click();
  feedback = fleetFeedback('claude-one');
  await expect(feedback).toContainText('Failed');
  const restore = feedback.getByRole('button', { name: 'submission failed; restore response' });
  await expect(restore).toBeVisible();
  await restore.click();
  await expect(fleetFeedback('claude-one')).toHaveCount(0);
  await expect(card.locator('.primarybtn')).toBeVisible();

});

test('permission quick-response feedback reaches submitted on a fresh page', async ({ page }) => {
  const feedback = page.locator(
    '[data-action-sid="codex:thread-one"] .quickfeedback, [data-sid="codex:thread-one"] .quickfeedback');
  await reset(page, 'approval-slow');
  await openAction(page, 'codex:thread-one');
  await page.locator('#sact').getByRole('button', { name: 'allow', exact: true }).click();
  await page.locator('#sclose').click();
  await expect(feedback).toContainText('Submitting');
  await expect(feedback).toContainText('Allow permission');
  await expect(feedback.getByLabel('sending quick response')).toBeVisible();
  await expect(feedback).toContainText('Submitted', { timeout: 5_000 });
});

test('every approval decision and MCP single/multi-select elicitation', async ({ page }) => {
  for (const [label, choice] of [['allow', 'allow'], ['always allow', 'always'],
                                 ['deny', 'deny'], ['cancel', 'cancel']]) {
    await reset(page, 'approval');
    await openAction(page, 'codex:thread-one');
    await page.locator('#sact').getByRole('button', { name: label, exact: true }).click();
    await expect.poll(async () => (await fixtureState(page)).actions.at(-1).choice)
      .toBe(choice);
  }

  await reset(page, 'elicitation');
  await openAction(page, 'codex:thread-one');
  await page.locator('#sact select').selectOption({ label: 'Prod' });
  await page.locator('#sact').getByRole('button', { name: 'US', exact: true }).click();
  await page.locator('#sact').getByRole('button', { name: 'EU', exact: true }).click();
  await page.locator('#sact').getByRole('button', { name: 'accept', exact: true }).click();
  await expect.poll(async () => (await fixtureState(page)).actions.at(-1))
    .toMatchObject({ type: 'elicitation', choice: 'accept',
      content: { env: 'prod', regions: ['us', 'eu'] } });
});

test('mute persistence, native commands, skills, and parent-routed subagents', async ({ page }) => {
  await reset(page);
  const card = page.locator('[data-sid="codex:thread-one"]');
  await card.getByRole('button', { name: /more/ }).click();
  await card.locator('button.bell').click();
  await refresh(page);
  await expect(card.locator('button.bell')).toHaveClass(/muted/);

  await card.locator('.shead').click();
  const input = page.locator('#sft-codex\\:thread-one');
  await input.fill('$rev');
  await expect(page.locator('.slashmenu')).toContainText('$reviewer');
  await page.getByRole('button', { name: /\$reviewer/ }).click();
  await sendModifiedReturn(page, input);
  await expect.poll(async () => (await fixtureState(page)).actions.at(-1).type).toBe('skill');

  await page.request.post('/test/reset', { data: { scenario: 'subagent' } });
  await page.reload();
  await card.getByText('reviewer', { exact: true }).click();
  await expect(page.locator('#aview')).toBeVisible();
  await page.locator('#aft').fill('Report the risky mappings');
  await page.locator('#aact').getByRole('button', { name: 'relay' }).click();
  await expect.poll(async () => (await fixtureState(page)).actions.at(-1))
    .toMatchObject({ type: 'relay', agent_id: 'child-one' });
  await expect(page.locator('#aact')).toContainText('parent thread');
});

test('closed, external view-only, stale, unavailable, and read-only states', async ({ page }, testInfo) => {
  await reset(page);
  await goTo(page, 'history');
  await page.locator('[data-history-sid="codex:closed"]').getByRole('button', { name: 'View' }).click();
  await expect(page.locator('#sbody')).toContainText('Durable closed conversation');
  await page.locator('#sclose').click();

  await page.request.post('/test/reset', { data: { scenario: 'reopenable' } });
  await page.reload();
  await expect(page.locator('#sessions [data-sid="codex:thread-one"]')).toHaveCount(0);
  await goTo(page, 'history');
  const history = page.locator('#history');
  const external = page.locator('[data-history-sid="codex:thread-one"]');
  await expect(external).toContainText('External');
  await expect(external).toContainText('View');
  await external.getByRole('button', { name: 'View' }).click();
  await expect(page.locator('#sact')).toContainText('view only');
  await expect(page.locator('#sact input')).toHaveCount(0);
  await expect(page.locator('#sact')).not.toContainText('take over');
  await page.locator('#sclose').click();

  await page.request.post('/test/reset', { data: { scenario: 'stale' } });
  await page.reload();
  await goTo(page, 'now');
  await expect(page.locator('[data-sid="codex:thread-one"]')).toContainText('app-server exited');
  await page.screenshot({ path: testInfo.outputPath('stale-state.png'), fullPage: true });

  await page.request.post('/test/reset', { data: { scenario: 'provider-unavailable' } });
  await page.reload();
  await goTo(page, 'now');
  await expect(page.locator('[data-sid="claude-one"]')).toBeVisible();
  await expect(page.locator('[data-sid="codex:thread-one"]')).toHaveCount(0);
  await expect(page.locator('#providerstate')).toContainText('codex unavailable');

  await page.context().clearCookies();
  await page.goto('/');
  await expect(page.locator('#notoken')).toBeVisible();
  const denied = await page.request.post('/api/act', { data: { type: 'ping' } });
  expect(denied.status()).toBe(403);
  page.__failures = [];
});

test('confirmed ledger recovery is visible without taking either provider down', async ({ page }) => {
  await reset(page, 'ledger-recovery');
  const warning = page.locator('#providerstate');
  await expect(warning).toContainText('Local data recovered');
  await expect(warning).toContainText('ledger.db.corrupt-test');
  await expect(page.locator('[data-sid="claude-one"]')).toBeVisible();
  await expect(page.locator('[data-sid="codex:thread-one"]')).toBeVisible();
});

test('backfilled Claude history supports both view and reopen', async ({ page }) => {
  await reset(page, 'claude-archive');
  await goTo(page, 'history');
  const row = page.locator('[data-history-sid="11111111-2222-3333-4444-555555555555"]');
  await expect(row).toContainText('Historical Claude review');
  await expect(row.getByRole('button', { name: 'View' })).toBeVisible();
  await expect(row.getByRole('button', { name: 'Reopen' })).toBeVisible();

  await row.getByRole('button', { name: 'View' }).click();
  await expect(page.locator('#sbody')).toContainText('Durable closed conversation');
  await expect(page.getByRole('button', { name: 'reopen in terminal' })).toBeVisible();
  await page.locator('#sclose').click();

  await row.getByRole('button', { name: 'Reopen' }).click();
  await expect.poll(async () => (await fixtureState(page)).actions.at(-1))
    .toMatchObject({ type: 'reopen',
      session_id: '11111111-2222-3333-4444-555555555555' });
  await expect(row.getByRole('button', { name: 'opened ✓' })).toBeVisible();
});

test('large transcript archives page history without hiding older rows', async ({ page }) => {
  await reset(page, 'large-history');
  const fleetPayload = await (await page.request.get('/api/fleet')).json();
  expect(fleetPayload.closed_total).toBe(206);
  expect(fleetPayload.closed).toHaveLength(0);
  expect(fleetPayload.closed_ids).toHaveLength(206);
  await goTo(page, 'history');
  const providerFilters = page.locator('.filterline').filter({ hasText: 'Provider' });
  await providerFilters.getByRole('button', { name: 'Claude' }).click();
  await expect(page.locator('[data-history-sid]')).toHaveCount(100);
  const firstMore = page.getByRole('button', { name: 'show 100 more' });
  await expect(firstMore).toBeVisible();
  await firstMore.click();
  await expect(page.locator('[data-history-sid]')).toHaveCount(200);
  await page.getByRole('button', { name: 'show 5 more' }).click();
  await expect(page.locator('[data-history-sid]')).toHaveCount(205);
  await expect(page.getByText('Archived Claude session 204')).toBeVisible();
});

test('available stays visible while inactive lifecycles live in the History destination', async ({ page }, testInfo) => {
  await page.request.post('/test/reset', { data: { scenario: 'organization' } });
  await page.goto('/?token=abcdef123456');

  await expect(page.locator('#sessions [data-sid="claude-one"]')).toContainText('Available');
  await expect(page.locator('#sessions [data-sid="codex:thread-one"]')).toHaveCount(0);
  await expect(page.locator('#sessions [data-sid="claude-dormant"]')).toHaveCount(0);

  await expect(page.locator('[data-history-sid="codex:thread-one"]')).toBeHidden();
  await goTo(page, 'history');
  const history = page.locator('#history');
  await expect(history).toContainText('3 sessions');
  await expect(page.locator('[data-history-sid="codex:thread-one"]')).toContainText('External');
  await expect(page.locator('[data-history-sid="claude-dormant"]')).toContainText('Inactive');
  await expect(page.locator('[data-history-sid="codex:closed"]')).toContainText('Closed');
  await page.screenshot({ path: testInfo.outputPath('organized-session-inventory.png'), fullPage: true });
});

test('action inbox separates requests, work, availability, and unread responses', async ({ page }, testInfo) => {
  await reset(page, 'single-question');
  const question = page.locator('[data-action-sid="codex:thread-one"]');
  await expect(page.locator('#actioninbox')).toContainText('Action inbox');
  await expect(question).toContainText('How broad should the change be?');
  await expect(question).toContainText('Question waiting');
  await expect(question.getByRole('button', { name: 'Respond' })).toBeVisible();
  await expect(page.locator('#usagebody .uprovider')).toHaveCount(2);
  await page.screenshot({ path: testInfo.outputPath('action-inbox.png'), fullPage: true });

  await reset(page, 'subagent');
  const working = page.locator('[data-sid="codex:thread-one"]');
  await expect(page.locator('#working')).toContainText('Working · 1');
  await expect(working.locator('.chip')).toHaveText('Working');
  await expect(working.getByRole('button', { name: 'Open', exact: true })).toBeVisible();
  await expect(page.locator('#usagebody .uprovider')).toHaveCount(2);
  await page.screenshot({ path: testInfo.outputPath('working-queue.png'), fullPage: true });

  await reset(page, 'reply-requested');
  const reply = page.locator('[data-action-sid="codex:thread-one"]');
  await expect(page.locator('#actioninbox')).toContainText('1 item needs review');
  await expect(reply).toContainText('Reply requested');
  await expect(reply).toContainText('Which organization should we use?');
  await reply.getByRole('checkbox').check();
  await page.getByRole('button', { name: 'Mark available 1' }).click();
  await expect.poll(async () => (await fixtureState(page)).reply_available['codex:thread-one'])
    .toBe('reply:1');
  await refresh(page);
  await expect(page.locator('#sessions [data-sid="codex:thread-one"] .chip')).toHaveText('Available');

  await reset(page, 'new-response');
  const fresh = page.locator('[data-action-sid="codex:thread-one"]');
  await expect(fresh).toContainText('Completed work is ready to review');
  await fresh.getByRole('button', { name: 'Continue' }).click();
  await expect.poll(async () => (await fixtureState(page)).read_sessions['codex:thread-one'])
    .toBe('response:1');
  await page.locator('#sclose').click();
  await page.reload();
  await expect(page.locator('[data-action-sid="codex:thread-one"]')).toHaveCount(0);
  await expect(page.locator('#sessions [data-sid="codex:thread-one"]')).toBeVisible();
});

test('mobile Needs You keeps a Claude question identifiable when its inbox action disappears', async ({ page }, testInfo) => {
  await reset(page);
  const mobile = testInfo.project.name.startsWith('mobile');
  if (mobile) await page.dispatchEvent('body', 'touchstart');
  await page.request.post('/test/reset', { data: { scenario: 'mobile-needs-you' } });
  await refresh(page);
  if (mobile) {
    await expect(page.locator('[data-action-sid="claude-one"]')).toHaveCount(0);
    await page.waitForTimeout(850);
    await refresh(page);
  }

  const action = page.locator('[data-action-sid="claude-one"]');
  await expect(page.locator('#actioninbox .actionhead')).toContainText('Needs you');
  await expect(page.locator('#actioninbox .actionhead')).toContainText('Action inbox');
  await expect(action).toContainText('Get 429 into a mergable state');
  await expect(action).toContainText('hazy-hatching-curry');
  await expect(action.getByText('Question waiting', { exact: true })).toBeVisible();
  await expect(action.getByRole('button', { name: 'Respond' })).toBeVisible();
  const actionPin = action.getByRole('button', { name: 'pin session' });
  await expect(actionPin).toBeVisible();
  await actionPin.click();
  await expect.poll(async () => (await fixtureState(page)).settings.pinned_sessions)
    .toEqual(['claude-one']);
  const pinnedPin = page.locator('#pinned [data-sid="claude-one"]')
    .getByRole('button', { name: 'unpin session' });
  await expect(pinnedPin).toBeVisible();
  await pinnedPin.click();
  await expect.poll(async () => (await fixtureState(page)).settings.pinned_sessions)
    .toEqual([]);
  await expect(page.locator('#needsyou [data-sid="claude-one"]')).toHaveCount(0);
  await expect(page.locator('[data-action-sid="claude-one"], #needsyou [data-sid="claude-one"]')).toHaveCount(1);

  await action.getByRole('button', { name: 'Respond' }).click();
  await expect(page.locator('#sview')).toContainText('How should I bring PR #429 up to date with main');
  await page.locator('#sclose').click();

  await page.locator('[data-now-filter="needs_you"]').click();
  await expect(action).toBeVisible();
  await expect(page.locator('#working .card, #sessions .card')).toHaveCount(0);
  await page.locator('#nowfilter').fill('hazy-hatching-curry');
  await expect(action).toBeVisible();
  await page.locator('.actionfilters').getByRole('button', { name: 'Requests' }).click();
  await expect(action).toBeVisible();
  await page.locator('.actionfilters').getByRole('button', { name: 'Approvals' }).click();

  let fallback = page.locator('#needsyou [data-sid="claude-one"]');
  await expect(action).toHaveCount(0);
  await expect(fallback).toBeVisible();
  await expect(fallback).toContainText('Get 429 into a mergable state');
  await expect(fallback).toContainText('hazy-hatching-curry');
  await expect(fallback.locator('.chip')).toHaveText('Question waiting');
  await fallback.getByRole('button', { name: 'Respond' }).click();
  await expect(page.locator('#sview')).toContainText('How should I bring PR #429 up to date with main');
  await page.locator('#sclose').click();

  await page.locator('.actionfilters').getByRole('button', { name: 'All' }).click();
  await expect(action).toBeVisible();
  await expect(fallback).toHaveCount(0);
  await page.request.post('/test/reset', { data: { scenario: 'mobile-needs-you-missing-action' } });
  await refresh(page);
  fallback = page.locator('#needsyou [data-sid="claude-one"]');
  await expect(page.locator('[data-action-sid="claude-one"]')).toHaveCount(0);
  await expect(fallback).toBeVisible();

  await page.evaluate(() => toggleSessionPin('claude-one'));
  await expect.poll(async () => (await fixtureState(page)).settings.pinned_sessions).toContain('claude-one');
  await expect(page.locator('#pinned [data-sid="claude-one"]')).toBeVisible();
  await expect(fallback).toHaveCount(0);
  expect(await page.evaluate(() => Boolean(document.querySelector('#pinned')
    .compareDocumentPosition(document.querySelector('#needsyou')) & Node.DOCUMENT_POSITION_FOLLOWING)))
    .toBe(true);
  await page.evaluate(() => toggleSessionPin('claude-one'));
  await expect.poll(async () => (await fixtureState(page)).settings.pinned_sessions).not.toContain('claude-one');

  await page.context().clearCookies();
  await page.goto('/');
  await expect(page.locator('#notoken')).toBeVisible();
  await expect(page.locator('#needsyou [data-sid="claude-one"]')).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath(`mobile-needs-you-${mobile ? 'mobile' : 'desktop'}.png`), fullPage: true });
  expect(page.__failures.filter(message=>!message.includes('403 (Forbidden)')),
    'read-only probe browser errors').toEqual([]);
  page.__failures.length = 0;
});

test('action inbox bulk triage is safe and never offers bulk approval', async ({ page }) => {
  await reset(page, 'approval');
  let action = page.locator('[data-action-sid="codex:thread-one"]');
  const actionId = await action.getAttribute('data-action-id');
  const unsafe = await page.request.post('/api/settings', { data: { bulk_triage: {
    operation: 'dismiss', items: [{ action_id: actionId, session_id: 'codex:thread-one' }] } } });
  expect((await unsafe.json()).ok).toBe(false);
  await action.getByRole('checkbox').check();
  const bulk = page.locator('.bulkbar');
  await expect(bulk).toContainText('Mute 1');
  await expect(bulk).not.toContainText(/allow|approve|dismiss/i);
  await bulk.getByRole('button', { name: 'Mute 1' }).click();
  await expect.poll(async () => (await fixtureState(page)).sessions
    .find(item => item.session_id === 'codex:thread-one').muted).toBe(true);
  await expect(page.locator('.bulkbar')).toHaveCount(0);

  await reset(page, 'new-response');
  action = page.locator('[data-action-sid="codex:thread-one"]');
  await action.getByRole('checkbox').check();
  await page.locator('.bulkbar').getByRole('button', { name: 'Dismiss 1' }).click();
  await expect(action).toHaveCount(0);
  await expect(page.locator('#sessions [data-sid="codex:thread-one"] .newbadge')).toBeVisible();
  await page.reload();
  await expect(page.locator('[data-action-sid="codex:thread-one"]')).toHaveCount(0);
  await expect(page.locator('#sessions [data-sid="codex:thread-one"] .newbadge')).toBeVisible();
});

test('workstreams roll repositories, worktrees, providers, honest evidence, and saved views', async ({ page }, testInfo) => {
  await reset(page, 'workstreams');
  await goTo(page, 'workstreams');
  const workstream = page.locator('[data-workstream-id="ws-fleet"]');
  await expect(workstream).toBeVisible();
  await expect(workstream).toContainText('fleet-dash');
  await expect(workstream).toContainText('claude · codex');
  await expect(workstream).toContainText('feature/action-inbox');
  await expect(workstream).toContainText('Changes 2 files');
  await expect(workstream).toContainText('Tests passed');
  await expect(workstream).toContainText('PR none');
  await expect(workstream).toContainText('Budget not configured');
  await workstream.locator('.workhead').click();
  await expect(workstream.locator('.worksession')).toHaveCount(3);
  await expect(workstream.locator('.worktrees')).toContainText('/Users/test/fleet-dash-worktrees/ui');

  await page.locator('#workfilter').fill('feature/action-inbox');
  await page.locator('[data-work-filter="mixed"]').click();
  page.once('dialog', dialog => dialog.accept('Cross-provider UI'));
  await page.getByRole('button', { name: /Save view/ }).click();
  await expect(page.locator('#worksaved')).toContainText('Cross-provider UI');
  await page.locator('#workfilter').fill('does-not-match');
  await expect(page.locator('[data-workstream-id]')).toHaveCount(0);
  await page.locator('#worksaved').getByRole('button', { name: 'Cross-provider UI', exact: true }).click();
  await expect(page.locator('#workfilter')).toHaveValue('feature/action-inbox');
  await expect(workstream).toBeVisible();
  await page.reload();
  await goTo(page, 'workstreams');
  await expect(page.locator('#worksaved')).toContainText('Cross-provider UI');
  await page.screenshot({ path: testInfo.outputPath('workstreams.png'), fullPage: true });
});

test('workstreams deep-link to GitHub instead of duplicating repository details', async ({ page }) => {
  await reset(page, 'workstreams');
  await goTo(page, 'workstreams');
  const github=page.locator('[data-workstream-id="ws-fleet"] a.repoopen');
  await expect(github).toHaveAttribute('href','https://github.com/federbenjamin/fleet-dash');
  await expect(github).toHaveAttribute('rel','noopener');
  await expect(page.locator('#repoview')).toHaveCount(0);
});

test('session action menu does not duplicate GitHub repository content', async ({ page }) => {
  await reset(page, 'base');
  await page.locator('[data-sid="claude-one"] .shead').click();
  await page.locator('#sctrl .ovbtn').click();
  await expect(page.locator('#sctrl').getByRole('menuitem', { name: /Repository outcome/ })).toHaveCount(0);
  await expect(page.locator('#sview')).toBeVisible();
});

test('repository action failures do not recreate an in-app GitHub page', async ({ page }) => {
  await reset(page, 'repo-action-failure');
  await goTo(page, 'workstreams');
  await expect(page.locator('[data-workstream-id="ws-fleet"] a.repoopen')).toBeVisible();
  await expect(page.locator('#repoview')).toHaveCount(0);
});

test('message Outbox schedules exact session delivery and exposes durable central controls', async ({ page }, testInfo) => {
  await reset(page, 'base');
  await page.locator('[data-sid="codex:thread-one"] .primarybtn').click();
  await expect(page.locator('#sview')).toBeVisible();
  await page.locator('#sft-codex\\:thread-one').fill('Send this when the Codex session is available');
  await page.locator('#sact').getByRole('button', { name: 'delivery options' }).click();
  await expect(page.locator('#scheduleview')).toBeVisible();
  await page.getByRole('button', { name: 'When available', exact: true }).click();
  await page.locator('#scheduleview').getByRole('button', { name: 'Schedule', exact: true }).click();
  await expect(page.locator('#scheduleview')).toBeHidden();
  await expect(page.locator('#sview')).toBeVisible();
  await expect.poll(async () => (await fixtureState(page)).outbox.length).toBe(1);
  expect((await fixtureState(page)).outbox[0]).toMatchObject({
    state: 'waiting_availability', target_session_id: 'codex:thread-one',
    message: 'Send this when the Codex session is available'
  });

  await page.locator('#sclose').click();
  await expect(page.locator('#sview')).toBeHidden();
  await expect(page.locator('#outboxsummary')).toContainText('1 waiting to send');
  await page.locator('#outboxchip').click();
  await expect(page.locator('#outboxview')).toBeVisible();
  const row = page.locator('.outboxrow').first();
  await expect(row).toContainText('Waiting for availability');
  await row.getByRole('button', { name: 'Edit' }).click();
  await page.locator('#scheduleview textarea').fill('Edited queued message');
  await page.locator('#scheduleview').getByRole('button', { name: 'Save changes' }).click();
  await expect(page.locator('#scheduleview')).toBeHidden();
  await page.locator('#outboxchip').click();
  await page.locator('.outboxrow').first().getByRole('button', { name: 'Send now' }).click();
  await expect.poll(async () => (await fixtureState(page)).outbox[0].state).toBe('sent');
  await page.locator('.outboxtools').getByRole('button', { name: 'Sent', exact: true }).click();
  await expect(page.locator('.outboxrow').first()).toContainText('Sent');
  await expect.poll(async () => (await fixtureState(page)).outbox[0].message).toBe('Edited queued message');
  await page.screenshot({ path: testInfo.outputPath('message-outbox.png'), fullPage: true });
});

test('usage-reset and scheduled-new-session forms keep full target configuration', async ({ page }) => {
  await reset(page, 'base');
  await page.locator('[data-sid="claude-one"] .shead').click();
  await page.locator('#sft-claude-one').fill('Continue after my Claude usage resets');
  await page.locator('#sact').getByRole('button', { name: 'delivery options' }).click();
  await page.getByRole('button', { name: 'When usage resets', exact: true }).click();
  await expect(page.locator('#scheduleview select').last()).toContainText('Claude Code');
  await page.locator('#scheduleview').getByRole('button', { name: 'Schedule', exact: true }).click();
  await expect.poll(async () => (await fixtureState(page)).outbox[0].state)
    .toBe('waiting_usage_reset');
  await page.locator('#sclose').click();

  await page.getByRole('button', { name: '+ new coding session' }).click();
  await page.locator('#newsess select').nth(1).selectOption('/Users/test/fleet-dash');
  await page.locator('#newsess textarea').fill('Audit the scheduled release workflow');
  await page.getByRole('button', { name: 'schedule session' }).click();
  await expect(page.locator('#scheduleview')).toBeVisible();
  await expect(page.locator('#scheduleview')).toContainText('Schedule new coding session');
  await page.locator('#scheduleview').getByRole('button', { name: 'Schedule', exact: true }).click();
  await expect.poll(async () => (await fixtureState(page)).outbox.length).toBe(2);
  const item = (await fixtureState(page)).outbox[1];
  expect(item.kind).toBe('new_session');
  expect(item.spawn_spec).toMatchObject({provider: 'claude', cwd: '/Users/test/fleet-dash'});
  expect(item.message).toBe('Audit the scheduled release workflow');
});

test('fleet briefing separates current attention from completed outcomes and persists review', async ({ page }, testInfo) => {
  await reset(page, 'base');
  await goTo(page, 'notifications');
  await page.getByRole('button', { name: /^Briefing/ }).click();
  await expect(page.locator('#notifications')).toContainText('Fleet briefing');
  await expect(page.locator('#notifications')).toContainText('2 since review');
  await expect(page.locator('.briefbody')).toContainText('Completed since last review');
  await expect(page.locator('.briefbody')).toContainText('Parser tests passed');
  await expect(page.locator('.briefbody')).toContainText('Artifacts delivered');
  await expect.poll(async () => Object.values((await fixtureState(page)).briefing_reviewed)[0]).toBe(3);
  await page.screenshot({ path: testInfo.outputPath('fleet-briefing.png'), fullPage: true });

  await page.reload();
  await page.getByRole('button', { name: /^Briefing/ }).click();
  await expect(page.locator('#notifications')).toContainText('2 recently reviewed');
  await expect(page.locator('.briefbody')).toContainText('Recently reviewed');
  await expect(page.locator('.briefbody')).toContainText('Parser tests passed');
});

test('Notification Center keeps durable state, exact detail routes, and delivery recovery together', async ({ page }, testInfo) => {
  await reset(page, 'base');
  await goTo(page, 'notifications');
  await expect(page.locator('#notificationstatus')).toContainText('3 active');
  await expect(page.locator('#notificationstatus')).toContainText('3 unread');
  await expect(page.getByRole('button', { name: /^Needs action/ })).toContainText('· 1');
  await expect(page.getByRole('button', { name: /^Updates/ })).toContainText('· 1');
  await expect(page.getByRole('button', { name: /^Snoozed/ })).toContainText('· 1');
  await expect(page.getByRole('button', { name: /^Problems/ })).toContainText('· 3');

  await page.getByRole('button', { name: /Choose a release target/ }).click();
  await expect(page).toHaveURL(/#notifications\/evt-6-question$/);
  await expect(page.locator('#notificationdetail')).toContainText('Claude needs one answer');
  await expect(page.locator('#notificationdetail')).toHaveClass(/open/);
  if (testInfo.project.name.startsWith('mobile')) {
    await expect(page.locator('#notificationdetail')).toBeVisible();
    expect(await page.locator('#notificationdetail').evaluate(element => getComputedStyle(element).position)).toBe('fixed');
  }
  await expect.poll(async () => (await fixtureState(page)).notification_read_cursors).not.toEqual({});
  await expect(page.locator('#notificationstatus')).toContainText('0 unread');

  await page.getByRole('button', { name: 'Snooze 15m' }).click();
  await expect(page.locator('#notificationdetail')).toContainText('Snoozed until');
  await expect(page.getByRole('button', { name: /^Snoozed/ })).toContainText('· 2');
  await page.getByRole('button', { name: 'Wake now' }).click();
  await expect(page.locator('#notificationdetail')).toContainText('Moved back to Needs action');
  await page.getByRole('button', { name: 'Mute session' }).click();
  await expect(page.getByRole('button', { name: 'Unmute session' })).toBeVisible();
  await expect.poll(async () => (await fixtureState(page)).muted_notification_sessions).toContain('claude-one');

  await page.goBack();
  await expect(page).toHaveURL(/#notifications$/);
  await expect(page.locator('#notificationdetail')).not.toHaveClass(/open/);
  await page.locator('.notificationbar').getByRole('button', { name: /^Problems/ }).click();
  await expect(page.locator('.deliveryproblem')).toHaveCount(2);
  await page.getByRole('button', { name: 'Retry' }).click();
  await expect(page.locator('.deliveryproblem')).toHaveCount(1);
  await expect(page.getByRole('button', { name: 'Reconnect' })).toBeVisible();

  await page.locator('.notificationbar').getByRole('button', { name: /^History/ }).click();
  await page.locator('.notificationsearch input').fill('artifact');
  await page.getByLabel('notification workstream').selectOption('ws-fleet');
  await page.getByLabel('notification session').selectOption('codex:thread-one');
  await page.getByLabel('notification age').selectOption('604800');
  await expect(page.locator('#notifications .notificationrow')).toHaveCount(1);
  await expect(page.locator('#notifications')).toContainText('Artifact delivered');

  await page.goto('/#notifications/evt-5-failure');
  await expect(page.locator('#notificationdetail')).toContainText('Codex connection interrupted');
  await expect(page.locator('#notificationdetail')).toHaveClass(/open/);
  await page.screenshot({ path: testInfo.outputPath('notification-center.png'), fullPage: true });

  await reset(page, 'notification-request');
  await goTo(page, 'notifications');
  await page.getByRole('button', { name: /Choose a release target/ }).click();
  await expect(page.locator('#notificationdetail')).toContainText('Respond here');
  await expect(page.locator('#notificationdetail')).toContainText('Which release target should Fleet use?');
  await page.locator('#notificationdetail').getByRole('button', { name: /Staging/ }).click();
  await expect.poll(async () => (await fixtureState(page)).actions.some(action =>
    action.type === 'option' && action.session_id === 'claude-one' && action.nonce === 'rev-6')).toBe(true);
});

test('push fallback opens exact current state and direct close stays inside Notifications', async ({ page }) => {
  await reset(page, 'base');
  await page.goto('/?push_action=snooze#notifications/evt-6-question');
  await expect(page).toHaveURL(/\/#notifications\/evt-6-question$/);
  await expect(page.locator('#notificationdetail')).toContainText('Claude needs one answer');
  await expect(page.locator('#notificationdetail')).toContainText(
    'Snooze from the notification did not complete');
  await expect(page.getByRole('button', {name: 'Snooze 15m'})).toBeVisible();
  await page.getByRole('button', {name: 'close notification detail'}).click();
  await expect(page).toHaveURL(/#notifications$/);
  await expect(page.locator('#notificationdetail')).not.toHaveClass(/open/);
});

test('PWA caches the local shell and fleet snapshot without credentials', async ({ page }) => {
  await reset(page, 'base');
  const manifest = await (await page.request.get('/static/manifest.webmanifest')).json();
  expect(manifest).toMatchObject({id: '/', start_url: '/#now', scope: '/', display: 'standalone'});
  expect(manifest.icons.map(icon => icon.sizes)).toEqual(['192x192', '512x512', '512x512']);

  await page.evaluate(() => navigator.serviceWorker.ready);
  await expect.poll(() => page.evaluate(async () => {
    const entries = [];
    for (const key of await caches.keys()) {
      for (const request of await (await caches.open(key)).keys()) entries.push(request.url);
    }
    return entries.some(url => url.endsWith('/api/fleet'));
  })).toBe(true);
  const cached = await page.evaluate(async () => {
    const entries=[];for(const key of await caches.keys())for(const request of await (await caches.open(key)).keys())entries.push(request.url);
    return entries;
  });
  expect(cached.some(url => url.endsWith('/api/fleet'))).toBe(true);
  expect(cached.some(url => url.includes('token='))).toBe(false);
  expect(cached.some(url => new URL(url).pathname === '/')).toBe(true);
  expect(cached.some(url => url.endsWith('/static/offline.html'))).toBe(true);

  await goTo(page, 'settings');
  const setup = page.locator('.pushsetup');
  await expect(setup).toContainText('Fleet app & Web Push');
  await expect(setup).toContainText(/Not requested|Blocked/);
  await expect(setup).toContainText('Not connected');

  const deviceId = await page.evaluate(() => localStorage.getItem('fleet.briefingDevice.v1'));
  await page.request.post('/api/push/subscription', {data: {device_id: deviceId,
    display_name: 'Fixture Mac', platform: 'macOS', permission_state: 'granted',
    subscription: {endpoint: 'https://fcm.googleapis.com/fcm/send/private',
      keys: {p256dh: 'private-p256dh', auth: 'private-auth'}}}});
  await page.evaluate(() => window.__fleetPush.loadPushState(true));
  const name = setup.locator('.pushdevice input').first();
  await expect(name).toHaveValue('Fixture Mac');
  await name.fill('Studio Mac');
  await name.press('Tab');
  await expect.poll(async () => (await fixtureState(page)).push_devices[deviceId].display_name)
    .toBe('Studio Mac');
  await setup.getByRole('button', { name: 'Send test' }).click();
  await expect(setup.locator('.pushnotice')).toContainText('Test queued');
  await expect.poll(async () => (await fixtureState(page)).push_devices[deviceId].health)
    .toBe('healthy');
  await setup.locator('.pushswitch input').uncheck();
  await expect.poll(async () => (await fixtureState(page)).push_devices[deviceId].enabled)
    .toBe(false);
  const state = await fixtureState(page);
  expect(JSON.stringify(state.push_devices)).not.toContain('private-p256dh');
  expect(JSON.stringify(state.push_devices)).not.toContain('private-auth');
  expect(JSON.stringify(state.push_devices)).not.toContain('fcm.googleapis.com');
  const privacy = await page.evaluate(async () => {
    const cacheEntries = [];
    for (const key of await caches.keys()) {
      const cache = await caches.open(key);
      for (const request of await cache.keys()) {
        const response = await cache.match(request);
        cacheEntries.push({url: request.url, body: await response.text()});
      }
    }
    return {url: location.href, dom: document.documentElement.innerHTML,
      local: Object.fromEntries(Object.entries(localStorage)),
      session: Object.fromEntries(Object.entries(sessionStorage)),
      indexedDb: indexedDB.databases ? (await indexedDB.databases()).map(item => item.name) : [],
      cacheEntries};
  });
  const retained = JSON.stringify(privacy);
  for (const secret of ['abcdef123456', 'private-p256dh', 'private-auth',
    'fcm.googleapis.com/fcm/send/private']) expect(retained).not.toContain(secret);
  expect(privacy.session).toEqual({});
  expect(privacy.indexedDb).toEqual([]);
});

test('connection loss reloads the cached dashboard, keeps drafts, and flushes queued messages once', async ({ page, context }) => {
  await reset(page, 'base');
  await page.evaluate(() => navigator.serviceWorker.ready);
  await expect.poll(() => page.evaluate(async () => Boolean(await caches.match('/api/fleet')))).toBe(true);
  await page.reload();
  await context.setOffline(true);
  try {
    await page.reload({waitUntil:'domcontentloaded'});
    await expect(page.locator('#route-now')).toBeVisible();
    await expect(page.locator('#stale')).toContainText('offline — showing the last local snapshot');
    await expect(page.locator('[data-sid="codex:thread-one"]')).toBeVisible();
    await page.locator('#nowfilter').fill('offline draft');
    await page.reload({waitUntil:'domcontentloaded'});
    await expect(page.locator('#nowfilter')).toHaveValue('offline draft');
    await page.locator('#nowfilter').fill('');
    await page.evaluate(() => openSession('codex:thread-one'));
    const composer = page.locator('textarea[data-draft-key="composer:codex:thread-one"]');
    await expect(composer).toBeVisible();
    await composer.fill('Send this when Fleet reconnects');
    await page.locator('#sact').getByRole('button', {name: 'send'}).click();
    await expect(composer).toHaveValue('');
    await expect(page.locator('#sbody [aria-label="queued offline"]')).toBeVisible();
    expect(await page.evaluate(() => JSON.parse(localStorage.getItem('fleet.offlineMessages.v1') || '[]')
      .map(item => ({sid:item.sid,text:item.text})))).toEqual([
        {sid:'codex:thread-one',text:'Send this when Fleet reconnects'}]);
    await page.reload({waitUntil:'domcontentloaded'});
    await expect(page.locator('[data-sid="codex:thread-one"] .quickfeedback')).toContainText('Queued offline');

    await context.setOffline(false);
    await expect.poll(async () => (await fixtureState(page)).actions.filter(action =>
      action.type === 'text' && action.session_id === 'codex:thread-one' &&
      action.text === 'Send this when Fleet reconnects').length).toBe(1);
    await page.evaluate(() => tick());
    await page.waitForTimeout(2200);
    expect((await fixtureState(page)).actions.filter(action =>
      action.type === 'text' && action.session_id === 'codex:thread-one' &&
      action.text === 'Send this when Fleet reconnects')).toHaveLength(1);
    expect(await page.evaluate(() => localStorage.getItem('fleet.offlineMessages.v1'))).toBeNull();
    page.__failures = page.__failures.filter(message =>
      !/ERR_INTERNET_DISCONNECTED|net::ERR_FAILED|Failed to fetch/i.test(message));
  } finally {
    await context.setOffline(false);
  }
});

test('budget editor, manual legacy ntfy, honest token scope, and spawn forecast work together', async ({ page }, testInfo) => {
  await reset(page, 'base');
  await goTo(page, 'settings');
  await page.locator('.budgetsettingsfold summary').click();
  await page.getByRole('button', { name: '＋ Add budget' }).click();
  const budget = page.locator('.budgetedit').first();
  await budget.locator('select').nth(0).selectOption('fleet');
  await budget.locator('select').nth(1).selectOption('tokens');
  await budget.locator('input[type="number"]').fill('10000');
  await budget.locator('input[type="checkbox"]').check();
  await page.getByRole('button', { name: '＋ Add budget' }).click();
  const providerBudget = page.locator('.budgetedit').nth(1);
  await providerBudget.locator('select').nth(0).selectOption('provider');
  await providerBudget.locator('select').nth(1).selectOption('codex');
  await providerBudget.locator('select').nth(2).selectOption('tokens');
  await providerBudget.locator('input[type="number"]').fill('50000');
  await expect(page.locator('#settings')).not.toContainText('waiting on you');
  await expect(page.locator('#settings')).not.toContainText('daily briefing push');
  await page.getByLabel('Enable manual legacy tests').check();
  await expect.poll(async () => (await fixtureState(page)).settings.legacy_ntfy_enabled)
    .toBe(true);
  await page.getByRole('button', { name: 'Send legacy test' }).click();
  await expect(page.locator('#settings')).toContainText('Legacy test queued');
  await page.getByRole('button', { name: 'Save budgets' }).click();
  await expect.poll(async () => (await fixtureState(page)).budgets.length).toBe(2);
  const state = await fixtureState(page);
  expect(state.budgets[0]).toMatchObject({scope_type: 'fleet', metric: 'tokens',
    limit_value: 10000, block_spawns: true});
  expect(state.budgets[1]).toMatchObject({scope_type: 'provider', scope_id: 'codex',
    metric: 'tokens', limit_value: 50000, block_spawns: false});
  expect(state.actions.some(item => item.type === 'legacy_ntfy_test')).toBe(true);

  await page.locator('#setclose').click();
  await goTo(page, 'now');
  await page.locator('.actionfilters').getByRole('button', { name: 'Budgets' }).click();
  const budgetAction = page.locator('.actionrow.budget');
  await expect(budgetAction).toContainText('Fleet tokens budget exceeded');
  await expect(budgetAction).toContainText('Future spawns blocked');
  await expect(budgetAction.getByRole('checkbox')).toHaveCount(0);
  await budgetAction.getByRole('button', { name: 'Review budget' }).click();
  await expect(page.locator('#budgets')).toContainText('Fleet tokens budget');
  await expect(page.locator('#budgets')).toContainText('Provider tokens budget');
  await expect(page.locator('#budgets')).toContainText('token only');
  await expect(page.locator('#budgets')).toContainText('blocks future spawns');
  await expect(page.locator('#budgets')).toContainText('medium confidence');

  await goTo(page, 'now');
  await page.getByRole('button', { name: '+ new coding session' }).click();
  await page.locator('#newsess select').first().selectOption('codex');
  await page.locator('#newsess select').nth(1).selectOption('/Users/test/fleet-dash');
  await expect(page.locator('.spawnforecast')).toContainText('currency unavailable');
  await expect(page.locator('.spawnforecast')).toContainText('medium confidence');
  await expect(page.locator('.spawnforecast')).toContainText('Budget:');
  await page.screenshot({ path: testInfo.outputPath('budgets-and-forecast.png'), fullPage: true });
});

test('pins persist and relocate sessions above the needs-you queue', async ({ page }) => {
  await reset(page, 'single-question');
  const cardPin=page.locator('[data-sid="claude-one"] .shead')
    .getByRole('button', { name: 'pin session' });
  await expect(cardPin).toBeVisible();
  await cardPin.click();
  await expect.poll(async () => (await fixtureState(page)).settings.pinned_sessions)
    .toEqual(['claude-one']);
  const pinnedPin=page.locator('#pinned [data-sid="claude-one"]')
    .getByRole('button', { name: 'unpin session' });
  await expect(pinnedPin).toBeVisible();
  await pinnedPin.click();
  await expect.poll(async () => (await fixtureState(page)).settings.pinned_sessions)
    .toEqual([]);
  const heldHeader=page.locator('[data-sid="claude-one"] .shead');
  await heldHeader.dispatchEvent('touchstart');
  await expect(heldHeader).toHaveClass(/pinpress/);
  await heldHeader.dispatchEvent('touchend');
  await expect(heldHeader).not.toHaveClass(/pinpress/);
  await page.evaluate(async () => {
    await toggleSessionPin('codex:thread-one');
    await toggleSessionPin('claude-one');
  });
  await expect.poll(async () => (await fixtureState(page)).settings.pinned_sessions)
    .toEqual(['codex:thread-one','claude-one']);
  await expect(page.locator('#pinned .pinslot')).toHaveCount(2);
  await expect(page.locator('#pinned .pinslot').nth(0))
    .toHaveAttribute('data-pin-sid','codex:thread-one');
  await expect(page.locator('#pinned .pinslot').nth(1))
    .toHaveAttribute('data-pin-sid','claude-one');
  await expect(page.locator('#pinned [data-sid="codex:thread-one"]')).toBeVisible();
  await expect(page.locator('#actioninbox [data-action-sid="codex:thread-one"]')).toHaveCount(0);
  expect(await page.evaluate(() => Boolean(document.querySelector('#pinned')
    .compareDocumentPosition(document.querySelector('#actioninbox')) & Node.DOCUMENT_POSITION_FOLLOWING)))
    .toBe(true);

  await page.reload();
  await expect(page.locator('#pinned .pinslot').nth(0))
    .toHaveAttribute('data-pin-sid','codex:thread-one');
  await expect(page.locator('#pinned .pinslot').nth(1))
    .toHaveAttribute('data-pin-sid','claude-one');
  await page.evaluate(async () => {
    await toggleSessionPin('codex:thread-one');
    await toggleSessionPin('codex:thread-one');
  });
  await expect.poll(async () => (await fixtureState(page)).settings.pinned_sessions)
    .toEqual(['claude-one','codex:thread-one']);
  await expect(page.locator('#pinned .pinslot').nth(0))
    .toHaveAttribute('data-pin-sid','claude-one');
  await expect(page.locator('#pinned .pinslot').nth(1))
    .toHaveAttribute('data-pin-sid','codex:thread-one');

  let pinRequests=0;
  await page.route('**/api/settings', async route => {
    const payload=route.request().postDataJSON();
    if(payload.pin_session==='codex:thread-one'){
      pinRequests+=1;
      await new Promise(resolve=>setTimeout(resolve,100));
      await route.fulfill({json:{ok:false,error:'pin storage unavailable'}});
    }else await route.continue();
  });
  await page.evaluate(() => {
    toggleSessionPin('codex:thread-one');
    toggleSessionPin('codex:thread-one');
  });
  await expect(page.locator('#pinned [data-sid="codex:thread-one"] .quickfeedback.failed'))
    .toContainText('pin storage unavailable');
  expect(pinRequests).toBe(1);
  await expect(page.locator('#pinned .pinslot').nth(0))
    .toHaveAttribute('data-pin-sid','claude-one');
  await expect(page.locator('#pinned .pinslot').nth(1))
    .toHaveAttribute('data-pin-sid','codex:thread-one');
  await page.unroute('**/api/settings');
});

test('session history text and access/provider chips filter one flat list', async ({ page }) => {
  await reset(page, 'organization');
  await goTo(page, 'history');
  const history = page.locator('#history');
  await expect(page.locator('#historyrows .historyrow')).toHaveCount(3);

  const input = history.getByPlaceholder(/Filter by title/);
  await input.fill('migration');
  await expect(page.locator('#historyrows .historyrow')).toHaveCount(1);
  await expect(page.locator('#historyrows')).toContainText('Dormant migration');
  await input.fill('');

  const access = history.locator('.filterline').nth(0);
  await access.getByRole('button', { name: 'Continue' }).click();
  await expect(page.locator('#historyrows .historyrow')).toHaveCount(1);
  await expect(page.locator('#historyrows')).toContainText('Inactive');
  await access.getByRole('button', { name: 'All' }).click();

  const provider = history.locator('.filterline').nth(1);
  await provider.getByRole('button', { name: 'Codex' }).click();
  await expect(page.locator('#historyrows .historyrow')).toHaveCount(2);
  await expect(page.locator('#historyrows')).toContainText('External');
  await expect(page.locator('#historyrows')).toContainText('Closed');
});
