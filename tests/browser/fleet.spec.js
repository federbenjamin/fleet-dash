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

async function fixtureFile(page, sid, name) {
  const response = await page.request.get(`/api/context?sid=${encodeURIComponent(sid)}`);
  const context = await response.json();
  const file = (context.files || []).find(item => item.name === name);
  expect(file, `fixture file ${name} for ${sid}`).toBeTruthy();
  expect(file).not.toHaveProperty('path');
  return file;
}

async function openFixtureFile(page, sid, name) {
  const file = await fixtureFile(page, sid, name);
  await page.evaluate(({ sessionId, fileId }) =>
    viewFile(encodeURIComponent(sessionId), encodeURIComponent(fileId)),
  { sessionId: sid, fileId: file.file_id });
  await expect(page.locator('#sview')).toBeVisible();
  await expect(page.locator('#stab-files')).toHaveAttribute('aria-selected', 'true');
  return file;
}

async function openSubagent(page, sid, agentId, { all = false } = {}) {
  await page.evaluate(sessionId => {
    if (globalThis.settingsOpen) closeSettings();
    openSession(sessionId);
    setSessionSection('subagents');
  }, sid);
  await expect(page.locator('#stab-subagents')).toHaveAttribute('aria-selected','true');
  if (all) await page.evaluate(() => setSubagentFilter('all'));
  await page.evaluate(({ sessionId, aid }) =>
    selectWorkspaceAgent(encodeURIComponent(sessionId), encodeURIComponent(aid)),
  { sessionId: sid, aid: agentId });
  await expect(page.locator('#atitle')).not.toHaveText('Subagents');
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

async function openSettingsSection(page, section) {
  await page.evaluate(value => selectSettingsSection(value), section);
  await expect(page).toHaveURL(new RegExp(`#settings/${section}$`));
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
  expect(await page.context().cookies()).toEqual(expect.arrayContaining([
    expect.objectContaining({name:'act_token_staging',value:'abcdef123456'})]));
  await expect(page.locator('#instancebanner')).toBeVisible();
  await expect(page.locator('#instancebanner')).toContainText('STAGING');
  await expect(page).toHaveTitle(/Fleet Staging/);
  await expect(page.locator('[data-sid="claude-one"] .accessbadge')).toHaveText('view only');
  await page.locator('[data-sid="claude-one"] .shead').click();
  await expect(page.locator('#sact .composer')).toHaveCount(0);
  await page.locator('#sact .latestfile').click();
  await expect(page.locator('#stab-files')).toHaveAttribute('aria-selected', 'true');
  await expect(page.locator('#vbody')).toContainText('Safe preview');
  await page.locator('#sclose').click();
  await page.locator('[data-sid="claude-one"] .shead').click();
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
  // This test needs Playwright's page.route to own /api/fleet. A service worker
  // can win registration during the setup idle callback and bypass page.route,
  // which tests the browser cache instead of response ordering.
  await page.addInitScript(() => {
    navigator.serviceWorker.register=async()=>{throw new Error('disabled for route-order test');};
  });
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
  await page.evaluate(async () => {
    const first=tick();
    await new Promise(resolve=>setTimeout(resolve,20));
    const second=tick(true);
    await Promise.allSettled([first,second]);
  });
  expect(fleetCalls).toBeGreaterThanOrEqual(2);
  await expect.poll(() => page.evaluate(() =>
    last.sessions.find(item => item.session_id === 'claude-one')?.title)).toBe('fresh poll result');
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

test('fleet polling is single-flight and a hung request times out without blanking the UI', async ({ page }) => {
  await page.addInitScript(() => {
    navigator.serviceWorker.register=async()=>{throw new Error('disabled for poll reliability test');};
  });
  await reset(page);
  const fleet=await(await page.request.get('/api/fleet')).json();
  let calls=0,active=0,maxActive=0;
  await page.route('**/api/fleet',async route=>{
    calls++;active++;maxActive=Math.max(maxActive,active);
    await new Promise(resolve=>setTimeout(resolve,250));active--;
    try{await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(fleet)});}catch(_){}
  });
  await page.evaluate(()=>Promise.all([tick(true),tick(),tick()]));
  expect(calls).toBe(1);expect(maxActive).toBe(1);
  await page.unroute('**/api/fleet');

  await page.route('**/api/fleet',async route=>{
    await new Promise(resolve=>setTimeout(resolve,800));
    try{await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(fleet)});}catch(_){}
  });
  await page.evaluate(async()=>{globalThis.__fleetPollTimeoutMs=100;await tick(true);});
  await expect(page.locator('#stale')).toContainText('offline — showing the last local snapshot');
  await expect(page.locator('[data-sid="codex:thread-one"]')).toBeVisible();
  await page.unroute('**/api/fleet');
});

test('native decisions lock double taps and relay/Outbox failures keep recovery', async ({ page }) => {
  await reset(page, 'claude-question-slow');
  await page.evaluate(() => openSession('claude-one'));
  await expect(page.locator('#sact .optbtn').first()).toBeVisible();
  await page.locator('#sact .optbtn').first().evaluate(button => { button.click(); button.click(); });
  await expect(page.locator('#sbody .optimistic')).toHaveCount(1);
  await expect(page.locator('#sact .optbtn')).toHaveCount(0);
  await page.waitForTimeout(850);
  expect((await fixtureState(page)).actions.filter(item => item.type === 'option')).toHaveLength(1);

  await reset(page, 'subagent');
  await page.evaluate(() => openAgent('codex:thread-one', 'agent-child-one'));
  await expect(page.locator('#aft')).toBeVisible();
  await page.route('**/api/act', async route => {
    const payload = route.request().postDataJSON();
    if (payload.type !== 'relay') return route.continue();
    await new Promise(resolve => setTimeout(resolve, 250));
    return route.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify({ ok: false, error: 'relay path unavailable' }) });
  });
  await page.locator('#aft').fill('Preserve this exact relay');
  await page.locator('#sact').getByRole('button', { name: 'relay' }).click();
  await expect(page.locator('#sact .quickfeedback')).toContainText('Relaying');
  await expect(page.locator('#sact .quickfeedback')).toContainText('relay path unavailable');
  await page.locator('#sact .quickfeedback').getByRole('button', { name: 'restore' }).click();
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

test('lost native delivery results never offer an unsafe automatic retry', async ({ page }) => {
  await reset(page);
  await page.evaluate(()=>openSession('claude-one'));
  await page.route('**/api/act',async route=>{
    const payload=route.request().postDataJSON();
    if(payload.type!=='send_message')return route.continue();
    return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({
      ok:false,code:'delivery_uncertain',error:'Delivery uncertain — check the Claude terminal'})});
  });
  await page.locator('#sft-claude-one').fill('May already be in the terminal');
  await page.locator('#sact').getByRole('button',{name:'send'}).click();
  const optimistic=page.locator('#sbody .optimistic').last();
  await expect(optimistic).toContainText('Delivery uncertain');
  await expect(optimistic.getByRole('button',{name:'send failed; restore message'})).toHaveCount(0);
  await expect(optimistic.getByRole('button',{name:'dismiss uncertain message receipt'})).toBeVisible();
  await page.unroute('**/api/act');

  await reset(page,'subagent');
  await page.evaluate(()=>openAgent('codex:thread-one','agent-child-one'));
  await page.route('**/api/act',async route=>{
    const payload=route.request().postDataJSON();
    if(payload.type!=='relay')return route.continue();
    return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({
      ok:false,code:'delivery_uncertain',error:'Check the parent terminal'})});
  });
  await page.locator('#aft').fill('Possibly relayed');
  await page.locator('#sact').getByRole('button',{name:'relay'}).click();
  const relay=page.locator('#sact .quickfeedback');
  await expect(relay).toContainText('Relay unconfirmed');
  await expect(relay).toContainText('Check the parent terminal');
  await expect(relay.getByRole('button',{name:'restore'})).toHaveCount(0);
  await page.unroute('**/api/act');

  await reset(page);
  await page.evaluate(()=>openHandoff('claude-one','codex'));
  await expect(page.locator('#handoffpreview')).toBeVisible();
  await page.route('**/api/act',async route=>{
    const payload=route.request().postDataJSON();
    if(payload.type!=='handoff')return route.continue();
    return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({
      ok:false,code:'delivery_uncertain',error:'Check the destination terminal',
      destination_session_id:'codex:handoff-unknown',retryable:false})});
  });
  await page.locator('.handoffsubmit').click();
  await expect(page.locator('.handoffstatus')).toContainText('Delivery unconfirmed');
  await expect(page.getByRole('button',{name:'Retry delivery to the same session'})).toHaveCount(0);
  await expect(page.getByRole('button',{name:'Open exact destination'})).toBeVisible();
});

test('unchanged and focused conversations repaint only when their content revision changes', async ({ page }) => {
  await reset(page, 'large-conversation');
  await page.evaluate(() => openSession('codex:thread-one'));
  await expect(page.locator('#sbody .cmsg')).toHaveCount(50);
  await page.evaluate(() => { window.__stableConversation = document.querySelector('#sbody .aconvo'); });
  await refresh(page);
  expect(await page.evaluate(() => document.querySelector('#sbody .aconvo') === window.__stableConversation)).toBe(true);

  await reset(page, 'subagent');
  await page.evaluate(() => openAgent('codex:thread-one', 'agent-child-one'));
  const relay = page.locator('#aft');
  await relay.fill('draft survives transcript repaint');
  await page.evaluate(() => {
    const key = agentCacheKey('codex:thread-one', 'agent-child-one');
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

  await openSubagent(page,'codex:thread-one','agent-child-one',{all:true});
  const agentStrip=page.locator('#sact .statusstrip');
  await expect(agentStrip).toBeVisible();
  await expect(agentStrip).toHaveClass(/frozen/);
  await expect(agentStrip).toContainText('GPT-5.4 · high');
  await expect(agentStrip).not.toContainText('$0.00');
  await expect(agentStrip.locator('.status-cache')).toHaveCount(0);
  await page.locator('#sclose').click();

  await goTo(page,'history');
  await page.locator('[data-history-sid="codex:closed"]').getByRole('button',{name:'View'}).click();
  const closedStrip=page.locator('#sact .statusstrip');
  await expect(closedStrip).toBeVisible();
  await expect(closedStrip).toHaveClass(/frozen/);
  await expect(page.locator('#sact textarea')).toBeVisible();
  await expect(page.locator('#sact textarea')).toBeDisabled();
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

  await reset(page, 'inactive-usage-warning');
  await expect(page.locator('#usagechip')).toHaveText('Usage · Claude 20/30 · Codex 30');
  await expect(page.locator('#usagechip')).not.toHaveClass(/usagewarn|usagedanger/);
  await page.locator('#usagechip').click();
  await expect(page.locator('#usagepanel')).toBeVisible();
  await expect(page.locator('#usagebody')).toContainText('94%');
  // the panel hangs off the button: top-left pinned to its bottom-left, small gap
  const anchored = await page.evaluate(() => {
    const button = document.querySelector('#usagechip').getBoundingClientRect();
    const panel = document.querySelector('#usagepanel').getBoundingClientRect();
    return { gap: Math.round(panel.top - button.bottom), leftDelta: Math.round(panel.left - button.left),
      rightOverflow: Math.round(Math.max(0, panel.right - innerWidth)),
      bottomOverflow: Math.round(Math.max(0, panel.bottom - innerHeight)) };
  });
  expect(anchored).toEqual({ gap: 8, leftDelta: 0, rightOverflow: 0, bottomOverflow: 0 });
  await page.locator('#usagepanel').getByRole('button', { name: 'close usage' }).click();

  await reset(page, 'usage-amber');
  await expect(page.locator('#usagechip')).toHaveClass(/usagewarn/);
  await expect(page.locator('#usagechip')).not.toHaveClass(/usagedanger/);

  await reset(page, 'usage-warning');
  await expect(page.locator('#usagechip')).toHaveText('Usage · Claude 20/30 · Codex 30');
  await expect(page.locator('#usagechip')).toHaveClass(/usagedanger/);
  await page.locator('#usagechip').click();
  await expect(page.locator('#usagepanel')).toBeVisible();
  await expect(page.locator('#usagebody')).toContainText('Fable weekly');
  await expect(page.locator('#usagebody')).toContainText('96%');
  await expect(page.locator('#usagebody')).toContainText('second@example.com');
  await page.locator('#usagepanel').getByRole('button', { name: 'close usage' }).click();
  await expect(page.locator('#usagepanel')).toBeHidden();

  await reset(page, 'missing-active-warning');
  await expect(page.locator('#usagechip')).toHaveClass(/usagedanger/);

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
  await expect(page.locator('#sview')).toBeVisible();
  await expect(page.locator('#stab-subagents')).toHaveAttribute('aria-selected','true');
  await expect(page.locator('#atitle')).toContainText('Review protocol mapping');
});

test('desktop navigation side and nested Settings preserve the full chat state', async ({ page }, testInfo) => {
  await reset(page);
  await goTo(page, 'settings');
  await openSettingsSection(page, 'appearance');
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
  await page.evaluate(() => { document.querySelector('#sbody').scrollTop = 125; });
  const before = await page.locator('#sbody').evaluate(element => element.scrollTop);
  await page.locator('#stab-details').click();
  await expect(page.locator('#spanel-details')).toBeVisible();
  await expect(page.locator('#detail-placement')).toContainText('Available');
  await page.evaluate(() => navigateTo('settings'));
  await expect(page.locator('#settingsview')).toBeVisible();
  await page.goBack();
  await expect(page.locator('#settingsview')).toBeHidden();
  await expect(page.locator('#sview')).toBeVisible();
  await expect(page.locator('#stab-details')).toHaveAttribute('aria-selected','true');
  await page.locator('#stab-chat').click();
  expect(Math.abs(await page.locator('#sbody').evaluate(element => element.scrollTop)-before)).toBeLessThan(4);

  await openFixtureFile(page,'codex:thread-one','artifact.md');
  await expect(page.locator('#vtitle')).toContainText('artifact.md');
  await expect(page.locator('#vtitle')).toContainText('Codex updated this file');
  await expect(page.locator('#vtitle')).not.toContainText('Codex parity work');
  await expect(page.locator('#vtitle .vfsep')).toHaveCount(0);
});

test('generic file viewer renders isolated inline-script HTML, PDF, and formatted JSON', async ({ page }) => {
  await reset(page);
  await page.evaluate(() => { window.__unsafeHtmlRan=false; });
  const htmlFile=await openFixtureFile(page,'codex:thread-one','preview.html');
  const htmlFrame=page.locator('#vbody .htmlpreview');
  await expect(htmlFrame).toBeVisible();
  await expect(htmlFrame).toHaveAttribute('sandbox','allow-scripts');
  await expect(htmlFrame).toHaveAttribute('referrerpolicy','no-referrer');
  const sandboxedDoc=await htmlFrame.evaluate(frame=>{
    const doc=new DOMParser().parseFromString(frame.srcdoc,'text/html');
    return{heading:doc.querySelector('h1')?.textContent,inlineScripts:doc.querySelectorAll(
      'script:not([src])').length,externalScripts:doc.querySelectorAll('script[src]').length,
      formAction:doc.querySelector('form')?.getAttribute('action'),
      imageSource:doc.querySelector('img')?.getAttribute('src'),
      csp:doc.querySelector('meta[http-equiv="Content-Security-Policy"]')?.content};
  });
  expect(sandboxedDoc).toMatchObject({heading:'Rendered HTML',inlineScripts:1,externalScripts:0,
    formAction:null,imageSource:null});
  expect(sandboxedDoc.csp).toContain("default-src 'none'");
  expect(sandboxedDoc.csp).toContain("script-src 'unsafe-inline'");
  expect(sandboxedDoc.csp).toContain("connect-src 'none'");
  await expect(page.frameLocator('#vbody .htmlpreview').locator('#generated')).toHaveText('Inline chart rendered');
  expect(await page.evaluate(()=>window.__unsafeHtmlRan)).toBe(false);
  const htmlResponse=await page.request.get(
    `/api/file?sid=codex%3Athread-one&fid=${htmlFile.file_id}`);
  expect(htmlResponse.headers()['content-type']).toContain('text/plain');

  await openFixtureFile(page,'codex:thread-one','data.json');
  await expect(page.locator('#vbody .jsondoc')).toContainText('"status": "ready"');
  await expect(page.locator('#vbody .jsondoc')).toContainText('"items": [');
  await expect(page.locator('#vbody')).not.toHaveClass(/frameview/);

  const pdfFile=await fixtureFile(page,'codex:thread-one','report.pdf');
  const pdfResponse=await page.request.get(
    `/api/file?sid=codex%3Athread-one&fid=${pdfFile.file_id}`);
  expect(pdfResponse.headers()['content-type']).toContain('application/pdf');
  expect(pdfResponse.headers()['x-content-type-options']).toBe('nosniff');
  await openFixtureFile(page,'codex:thread-one','report.pdf');
  const pdfFrame=page.locator('#vbody .pdfpreview');
  await expect(pdfFrame).toBeVisible();
  await expect(pdfFrame).not.toHaveAttribute('sandbox',/./);
  await expect(pdfFrame).toHaveAttribute('src',new RegExp(pdfFile.file_id));
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
  await expect(page.locator('#stab-files')).toHaveAttribute('aria-selected','true');
  await expect(page.locator('#vbody')).toContainText('Safe preview');
  await page.locator('#sclose').click();

  await page.locator('#search-rebuild').click();
  await expect(page.locator('#confirm')).toBeVisible();
  await page.locator('#confirm').getByRole('button', { name: 'rebuild index' }).click();
  await expect.poll(async () => (await fixtureState(page)).actions.at(-1)?.type)
    .toBe('search_rebuild');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test('search filter changes ignore a stale form value restored after overlay back', async ({ page }) => {
  await reset(page);
  await goTo(page, 'search');
  const query=page.locator('#searchquery');
  await query.fill('protocol regression');
  await expect(page.locator('#searchresults .searchresult')).toHaveCount(1);
  await page.locator('#searchresults .searchresult').click();
  await expect(page.locator('#searchview')).toBeVisible();
  await page.goBack();
  await expect(page.locator('#searchview')).toBeHidden();

  await query.fill('');
  // Model the load-sensitive same-document history restoration recorded in
  // the mobile trace: the DOM regains its prior value without an input event.
  await page.evaluate(()=>{clearTimeout(searchTimer);document.querySelector('#searchquery').value='protocol regression';});
  const requested=page.waitForRequest(request=>{
    const url=new URL(request.url());
    return url.pathname==='/api/search'&&url.searchParams.get('provider')==='claude';
  });
  await page.locator('#searchprovider').selectOption('claude');
  const url=new URL((await requested).url());
  expect(url.searchParams.get('q')).toBe('');
  await expect(query).toHaveValue('');
  await expect(page.locator('#searchresults .searchresult')).toHaveCount(1);
  await expect(page.locator('#searchresults')).toContainText('Claude review agent');
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

  await openFixtureFile(page,'codex:thread-one','artifact.md');
  await page.locator('#sctrl .ovbtn').click();
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
  await expect(page.locator('#usagechip')).toHaveText('Usage · Claude 20/30 · Codex 30');
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
    await expect.poll(async () => (await fixtureState(page)).actions.at(-1)?.type).toBe('focus');
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

  await codex.locator('.shead').click();
  await page.locator('#stab-details').click();
  await expect(page.locator('#detail-overview')).toContainText('unavailable');
  await page.locator('#stab-files').click();
  await page.getByRole('button', { name: /artifact\.md/ }).click();
  await expect(page.locator('#vbody')).toContainText('Safe preview');
  await page.screenshot({ path: testInfo.outputPath('artifact-preview.png'), fullPage: true });
});

test('session placement evidence lives in the Details section', async ({ page }, testInfo) => {
  await reset(page);
  const card = page.locator('[data-sid="claude-one"]');
  await card.locator('.shead').click();
  await expect(page.locator('#sview')).toBeVisible();
  await page.locator('#stab-details').click();
  const details = page.locator('#spanel-details');
  await expect(details).toBeVisible();
  const panelLayout = await page.evaluate(() => {
    const workspace = document.querySelector('#sworkspace').getBoundingClientRect();
    const chat = document.querySelector('#spanel-chat');
    const selected = document.querySelector('#spanel-details').getBoundingClientRect();
    return {workspaceTop: workspace.top, selectedTop: selected.top,
      chatDisplay: getComputedStyle(chat).display, chatHeight: chat.getBoundingClientRect().height};
  });
  expect(panelLayout.chatDisplay).toBe('none');
  expect(panelLayout.chatHeight).toBe(0);
  expect(Math.abs(panelLayout.selectedTop-panelLayout.workspaceTop)).toBeLessThan(1);
  await expect(details.locator('#detail-placement')).toContainText('Available');
  await expect(details.locator('#detail-placement')).toContainText('placement.default.available');
  await expect(details.locator('#detail-placement')).toContainText('Provider signal');
  await expect(details.locator('.evidenceevent')).toHaveCount(2);
  await expect(details).not.toContainText('changed / generated files');
  await expect(details).not.toContainText('completed agents');
  if (testInfo.project.name.startsWith('mobile')) {
    await expect(page.locator('#sdetailindex')).toHaveCSS('overflow-x','auto');
  }
  await page.screenshot({ path: testInfo.outputPath(`state-evidence-${testInfo.project.name}.png`) });
});

test('workspace uses active agent counts, persistent desktop splits, and one parent composer height', async ({ page }, testInfo) => {
  await reset(page, 'subagent');
  await page.evaluate(() => {
    workspaceSplitWidths={files:300,subagents:300};persistWorkspaceSplits();
    const session=last.sessions.find(item=>item.session_id==='codex:thread-one');
    session.agents.push({...session.agents[0],agent_id:'agent-completed-two',state:'done',
      description:'Completed child'});
    session.agents_total=2;render(last,true);openSession('codex:thread-one');
  });
  await expect(page.locator('#stab-subagents')).toHaveText('Subagents 1');
  const actionHeight=()=>page.locator('#sact').evaluate(element=>element.getBoundingClientRect().height);
  const heights=[await actionHeight()];

  await page.locator('#stab-files').click();
  heights.push(await actionHeight());
  const fileDivider=page.getByRole('separator',{name:'Resize file list'});
  if(testInfo.project.name==='desktop'){
    await expect(fileDivider).toBeVisible();
    const before=await page.locator('#sfilelist').evaluate(element=>element.getBoundingClientRect().width);
    await fileDivider.focus();await fileDivider.press('ArrowRight');
    const after=await page.locator('#sfilelist').evaluate(element=>element.getBoundingClientRect().width);
    expect(after-before).toBeGreaterThan(10);
  }else await expect(fileDivider).toBeHidden();

  await page.locator('#stab-subagents').click();
  heights.push(await actionHeight());
  const agentDivider=page.getByRole('separator',{name:'Resize subagent list'});
  if(testInfo.project.name==='desktop'){
    await agentDivider.focus();await agentDivider.press('ArrowLeft');
    const saved=await page.evaluate(()=>JSON.parse(localStorage.getItem(WORKSPACE_SPLIT_STORE_KEY)));
    expect(saved.files).toBeGreaterThan(saved.subagents);
  }
  await page.locator('#stab-details').click();
  heights.push(await actionHeight());
  expect(Math.max(...heights)-Math.min(...heights)).toBeLessThan(1);
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
  await peekRow.locator('.lmwho').click();
  await expect(peekRow).toHaveClass(/expanded/);
  await expect(peekRow.getByRole('button', { name: 'collapse latest message' })).toHaveText('Less');
  const expandedBox = await peek.evaluate(el => el.getBoundingClientRect().height);
  expect(expandedBox).toBeGreaterThan(peekBox.height);
  const expandedText = await peek.innerText();
  expect(expandedText.length).toBeLessThanOrEqual(800);
  expect(expandedText.endsWith('…')).toBe(true);
  await page.evaluate(() => {
    window.__peekLinkClicks = 0;
    document.querySelector('.sessionpeek a').addEventListener('click', event => {
      event.preventDefault();window.__peekLinkClicks++;
    });
  });
  await peekRow.getByRole('link', { name: 'Fleet docs' }).dispatchEvent('click');
  await expect(peekRow).toHaveClass(/expanded/);
  expect(await page.evaluate(() => window.__peekLinkClicks)).toBe(1);
  await page.screenshot({ path: testInfo.outputPath('markdown-peek-expanded.png'), fullPage: true });
  await peekRow.locator('.lmwho').click();
  await expect(peekRow).toHaveClass(/expanded/);
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
  await openSettingsSection(page, 'appearance');
  const widthGroup = page.getByRole('group', { name: 'Full-screen reading width' });
  await expect(widthGroup.getByRole('button', { name: 'Fit the screen' }))
    .toHaveAttribute('aria-pressed', 'true');
  await widthGroup.getByRole('button', { name: /Centered/ }).click();
  await expect(page.locator('html')).toHaveAttribute('data-reader-width', 'centered');
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('data-reader-width', 'centered');
  await expect(page.locator('#settingsview')).toBeVisible();
  await page.locator('#setclose').click();
  await expect(page.locator('#settingsview')).toBeHidden();

  card = page.locator('[data-sid="codex:thread-one"]');
  await card.locator('.shead').click();
  chat = page.locator('#sbody > .aconvo');
  const centeredChat = await chat.evaluate(el => ({ width: el.getBoundingClientRect().width,
    left: el.getBoundingClientRect().left,
    right: innerWidth - el.getBoundingClientRect().right }));
  expect(centeredChat.width).toBeLessThanOrEqual(760);
  expect(Math.abs(centeredChat.left - centeredChat.right)).toBeLessThan(2);
  await page.locator('#sclose').click();

  await openFixtureFile(page,'codex:thread-one','artifact.md');
  const doc = page.locator('#vbody > .mdoc');
  const centeredDoc = await doc.evaluate(el => {const rect=el.getBoundingClientRect(),parent=el.parentElement.getBoundingClientRect();return({
    width:rect.width,left:rect.left-parent.left,right:parent.right-rect.right});});
  expect(centeredDoc.width).toBeLessThanOrEqual(760);
  expect(Math.abs(centeredDoc.left - centeredDoc.right)).toBeLessThan(2);
  await openSubagent(page,'codex:thread-one','agent-child-one',{all:true});
  const agent = page.locator('#abody > .aconvo');
  const centeredAgent = await agent.evaluate(el => {const rect=el.getBoundingClientRect(),parent=el.parentElement.getBoundingClientRect();return({
    width:rect.width,left:rect.left-parent.left,right:parent.right-rect.right});});
  expect(centeredAgent.width).toBeLessThanOrEqual(760);
  expect(Math.abs(centeredAgent.left - centeredAgent.right)).toBeLessThan(2);
  await page.screenshot({ path: testInfo.outputPath('centered-reading-width.png'), fullPage: true });
});

test('a fully visible collapsed peek has no expansion action', async ({ page }) => {
  await reset(page);
  const peek = page.locator('[data-sid="codex:thread-one"] .sessionpeek');
  await expect(peek).not.toHaveClass(/truncated|expanded/);
  await peek.locator('.lmwho').click();
  await expect(peek).not.toHaveClass(/truncated|expanded/);
  await expect(peek.getByRole('button', { name: 'collapse latest message' })).toHaveCount(0);
});

test('full chat renders a large message without discarding text', async ({ page }) => {
  await reset(page, 'large-message');
  await page.evaluate(() => openSession('codex:thread-one'));
  const body = page.locator('#sbody .cmsg.assistant .cbody');
  await expect(body).toContainText('Large response begins.');
  await expect(body).toContainText('END-OF-LARGE-RESPONSE');
  expect((await body.innerText()).length).toBeGreaterThan(10_000);
});

test('saving a numeric setting does not swallow the next control click', async ({ page }) => {
  await reset(page, 'base');
  await goTo(page, 'settings');
  await openSettingsSection(page, 'appearance');
  const previewHeight = page.locator('.settingsfield').filter({hasText:'Session peek height'})
    .locator('input[type="number"]');
  await previewHeight.fill('5');
  await page.getByRole('button', {name:'Centered'}).click();
  await expect.poll(async () => (await fixtureState(page)).settings.preview_session_lines).toBe(5);
  await expect.poll(async () => (await fixtureState(page)).settings.reader_width).toBe('centered');
  await expect(page.getByRole('button', {name:'Centered'})).toHaveAttribute('aria-pressed','true');
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

  await openSubagent(page,'codex:thread-one','agent-child-one',{all:true});
  await expect(page.locator('#abody')).toContainText('Subagent report 204');
  for (let pageIndex = 0; pageIndex < 4; pageIndex += 1) {
    await page.locator('#abody .oldermsgs').click();
  }
  await expect(page.locator('#abody .cmsg')).toHaveCount(205);
  await expect(page.locator('#abody')).toContainText('Subagent report 000');
  await page.locator('#sclose').click();

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
    '/api/agent_context?sid=codex%3Athread-one&aid=agent-child-one']) {
    const response = await (await page.request.get(route+'&limit=50&cursor=999')).json();
    expect(response.ok).toBe(false);
  }
});

test('loaded conversation pages survive tail refresh and an offline reload', async ({ page, context }) => {
  await reset(page,'large-conversation');
  await page.locator('[data-sid="codex:thread-one"] .shead').click();
  for(let index=0;index<4;index++)await page.locator('#sbody .oldermsgs').click();
  await expect(page.locator('#sbody .cmsg')).toHaveCount(205);
  await page.request.post('/test/confirm',{data:{session_id:'codex:thread-one',text:'Newest canonical tail message'}});
  await page.evaluate(()=>tick(true));
  await expect(page.locator('#sbody')).toContainText('Newest canonical tail message');
  await expect(page.locator('#sbody')).toContainText('Conversation message 000');
  expect(await page.evaluate(()=>JSON.parse(localStorage.getItem('fleet.contextCache.v1')||'{}')
    ['session:codex:thread-one:']?.messages?.length)).toBe(206);

  await page.evaluate(()=>navigator.serviceWorker.ready);
  await expect.poll(()=>page.evaluate(async()=>Boolean(await caches.match('/api/fleet')))).toBe(true);
  await context.setOffline(true);
  try{
    await page.reload({waitUntil:'domcontentloaded'});
    await page.evaluate(()=>openSession('codex:thread-one'));
    await expect(page.locator('#sbody')).toContainText('Conversation message 000');
    await expect(page.locator('#sbody')).toContainText('Newest canonical tail message');
    await expect(page.locator('#sbody .cmsg')).toHaveCount(206);
    page.__failures=page.__failures.filter(message=>
      !/ERR_INTERNET_DISCONNECTED|net::ERR_FAILED|Failed to fetch/i.test(message));
  }finally{await context.setOffline(false);}
});

test('session cards remove More and list every running subagent', async ({ page }, testInfo) => {
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
    const body = peek.querySelector('.peekbody');
    const peekRect = peek.getBoundingClientRect();
    return {peekHeight: peekRect.height,
      bodyBottomGap: peekRect.bottom - body.getBoundingClientRect().bottom};
  });
  expect(peekFrame.peekHeight).toBeGreaterThan(40);
  expect(peekFrame.bodyBottomGap).toBeLessThan(8);
  await expect(idle.locator('.morebtn,.detail,.agents')).toHaveCount(0);

  await reset(page, 'subagent');
  surfaces = await themeSurfaces();
  const running = page.locator('[data-sid="codex:thread-one"]');
  await expect.poll(async () => (await cardStyle(running)).background).toBe(surfaces.card2);
  expect(await running.locator('.shead').evaluate(el => getComputedStyle(el).backgroundColor))
    .toBe(surfaces.card2);
  const preview=running.locator('.agents');
  await expect(preview).toBeVisible();
  await expect(preview.locator('.arow')).toHaveCount(1);
  await expect(preview.locator('.arow.done-row')).toHaveCount(0);
  await expect(preview).toContainText('Review protocol mapping');
  await expect(running.locator('.morebtn,.detail')).toHaveCount(0);
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

test('tapping a card subagent row opens the workspace Subagents section with it selected', async ({ page }) => {
  await reset(page, 'subagent');
  const row = page.locator('[data-sid="codex:thread-one"] .agents .arow').first();
  await expect(row).toBeVisible();
  await row.click();
  await expect(page.locator('#sview')).toBeVisible();
  await expect(page.locator('#spanel-subagents')).toBeVisible();
  await expect(page.locator('.agentworkspaceitem.selected')).toHaveCount(1);
  await expect(page.locator('.agentworkspaceitem.selected')).toContainText('Review protocol mapping');
  expect(page.url()).toContain('#session/codex%3Athread-one/subagents/agent-child-one');
  // Back clears the selected agent before leaving the section (invariant 36)
  await page.goBack();
  await expect(page.locator('#spanel-subagents')).toBeVisible();
  await expect(page.locator('.agentworkspaceitem.selected')).toHaveCount(0);
});

test('full chat renders main work as the newest non-interactive conversation row', async ({ page }) => {
  await reset(page, 'subagent');
  await page.evaluate(() => openSession('codex:thread-one'));
  const activity = page.locator('#sactivity');
  await expect(activity).toBeVisible();
  await expect(activity).toContainText('Main agent working');
  await expect(activity.locator('.mainworkingrow')).toBeVisible();
  await expect(activity.locator('button,summary,details')).toHaveCount(0);
  await expect(activity).not.toContainText('active subagent');

  await reset(page, 'cross-client-active');
  await page.evaluate(() => openSession('codex:thread-one'));
  await expect(activity).toContainText('Main agent working');

  await reset(page, 'base');
  await page.evaluate(() => openSession('codex:thread-one'));
  await expect(activity).toBeHidden();
});

test('quiet age is limited to working session cards', async ({ page }) => {
  await reset(page);
  await expect(page.locator('[data-sid="claude-one"] .squiet')).toHaveCount(0);
  await expect(page.locator('[data-sid="codex:thread-one"] .squiet')).toHaveCount(0);

  await reset(page, 'single-question');
  await expect(page.locator('[data-sid="claude-one"] .squiet')).toHaveCount(0);

  await reset(page, 'send-while-busy');
  await expect(page.locator('[data-sid="claude-one"] .squiet')).toHaveText('quiet 3s');
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
  await expect.poll(async () => (await fixtureState(page)).actions.at(-1)?.type).toBe('focus');
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
  await card.locator('.shead').click();
  await page.getByRole('button', { name: 'session actions' }).click();
  await expect(page.getByRole('button',{name:'Bypass permissions',exact:true})).toHaveAttribute('aria-pressed','true');
  await page.getByRole('button',{name:'Plan',exact:true}).click();
  await expect.poll(async () => (await fixtureState(page)).sessions[0].permission_mode)
    .toBe('plan');
});

test('existing Claude chat repairs model effort immediately and persists accepted settings', async ({ page }) => {
  await reset(page);
  await page.locator('[data-sid="claude-one"] .shead').click();
  await page.getByRole('button', { name: 'session actions' }).click();
  await expect(page.getByLabel('Session model')).toHaveValue('sonnet');
  let release;
  await page.route('**/api/act',async route=>{
    const payload=route.request().postDataJSON();
    if(payload.type!=='session_settings')return route.continue();
    await new Promise(resolve=>{release=resolve;});
    return route.continue();
  });
  await page.getByLabel('Session model').selectOption('opus');
  await expect(page.getByLabel('Session effort')).toHaveValue('medium');
  await expect(page.getByLabel('Session effort')).toBeDisabled();
  await expect(page.getByRole('status')).toHaveText('Saving…');
  release();
  await expect(page.getByRole('status')).toHaveText('Saved ✓');
  await expect.poll(async () => (await fixtureState(page)).sessions[0])
    .toMatchObject({model:'opus',effort:'medium'});
  await page.unroute('**/api/act');

  await page.getByLabel('Session model').selectOption('sonnet');
  await expect(page.getByLabel('Session effort')).toHaveValue('high');
  await expect(page.getByRole('status')).toHaveText('Saved ✓');
  await page.getByLabel('Session effort').selectOption('low');
  await expect(page.getByRole('status')).toHaveText('Saved ✓');
  await expect.poll(async () => (await fixtureState(page)).sessions[0])
    .toMatchObject({model:'sonnet',effort:'low'});

  await page.reload();
  await page.evaluate(() => openSession('claude-one'));
  await page.getByRole('button', { name: 'session actions' }).click();
  await expect(page.getByLabel('Session model')).toHaveValue('sonnet');
  await expect(page.getByLabel('Session effort')).toHaveValue('low');
});

test('native model changes supersede settled Fleet feedback and seed the next CAS', async ({ page }) => {
  await reset(page);
  await page.locator('[data-sid="claude-one"] .shead').click();
  await page.getByRole('button', { name: 'session actions' }).click();
  await page.getByLabel('Session model').selectOption('opus');
  await expect(page.getByRole('status')).toHaveText('Saved ✓');
  await expect.poll(async () => (await fixtureState(page)).sessions[0])
    .toMatchObject({model:'opus',effort:'medium'});

  const changed=await page.request.post('/test/canonical-session-settings',{data:{
    session_id:'claude-one',model:'sonnet',effort:'low'}});
  expect(changed.ok()).toBeTruthy();
  await page.evaluate(() => tick(true));

  await expect(page.locator('[data-sid="claude-one"] .amodel')).toContainText('sonnet · low');
  await expect(page.locator('#sact .status-secondary')).toContainText('sonnet · low');
  await expect(page.getByLabel('Session model')).toHaveValue('sonnet');
  await expect(page.getByLabel('Session effort')).toHaveValue('low');

  await page.getByLabel('Session effort').selectOption('high');
  await expect(page.getByRole('status')).toHaveText('Saved ✓');
  await expect.poll(async () => (await fixtureState(page)).actions)
    .toEqual(expect.arrayContaining([expect.objectContaining({type:'session_settings',
      model:'sonnet',effort:'high',expected_model:'sonnet',expected_effort:'low'})]));
});

test('existing-chat settings roll back on failure and respect active, external, and staging gates', async ({ page }) => {
  await reset(page);
  await page.locator('[data-sid="codex:thread-one"] .shead').click();
  await page.getByRole('button', { name: 'session actions' }).click();
  await page.route('**/api/act',async route=>{
    const payload=route.request().postDataJSON();
    if(payload.type==='session_settings')return route.fulfill({status:200,contentType:'application/json',
      body:JSON.stringify({ok:false,error:'provider rejected settings'})});
    return route.continue();
  });
  await page.getByLabel('Session model').selectOption('gpt-5.3-codex');
  await expect(page.getByRole('status')).toContainText('Could not save · provider rejected settings');
  await expect(page.getByLabel('Session model')).toHaveValue('gpt-5.4');
  expect((await fixtureState(page)).sessions[1]).toMatchObject({model:'gpt-5.4',effort:'high'});
  await page.unroute('**/api/act');
  await page.route('**/api/act',async route=>{
    const payload=route.request().postDataJSON();
    if(payload.type==='session_settings')return route.fulfill({status:200,contentType:'application/json',
      body:JSON.stringify({ok:true,model:payload.model,effort:payload.effort,durable:false,
        warning:'Applied in Codex, but Fleet could not durably save the setting'})});
    return route.continue();
  });
  await page.getByLabel('Session model').selectOption('gpt-5.3-codex');
  await expect(page.getByRole('status')).toContainText('Applied ✓ · Applied in Codex');
  await expect(page.getByLabel('Session model')).toHaveValue('gpt-5.3-codex');
  await page.unroute('**/api/act');

  await reset(page,'send-while-busy');
  await page.locator('[data-sid="claude-one"] .shead').click();
  await page.getByRole('button', { name: 'session actions' }).click();
  await expect(page.getByLabel('Session model')).toBeDisabled();
  await expect(page.locator('.settingsfeedback')).toContainText('Available when Claude is idle');

  await reset(page,'cross-client-active');
  await page.locator('[data-sid="codex:thread-one"] .shead').click();
  await page.getByRole('button', { name: 'session actions' }).click();
  await expect(page.getByLabel('Session model')).toHaveCount(0);

  await reset(page,'staging');
  await page.locator('[data-sid="claude-one"] .shead').click();
  await page.getByRole('button', { name: 'session actions' }).click();
  await expect(page.getByLabel('Session model')).toHaveCount(0);
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
  await expect(page.locator('#sact textarea[placeholder="send message"]')).toBeVisible();
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
  await openSubagent(page,'codex:thread-one','agent-child-one',{all:true});
  await expect(page.locator('#abody')).toContainText('First parent report');
  await page.locator('#sclose').click();
  await openSubagent(page,'codex:thread-two','agent-child-one',{all:true});
  await expect(page.locator('#abody')).toContainText('Second parent report');
  await expect(page.locator('#abody')).not.toContainText('First parent report');
});

test('the shared workspace header keeps session controls across every section', async ({ page }, testInfo) => {
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

  await openFixtureFile(page,'codex:thread-one','artifact.md');
  await page.getByRole('button', { name: 'session actions' }).click();
  await expect(page.getByRole('button', { name: 'Plan', exact: true })).toBeVisible();
  await expect(page.getByRole('menuitem', { name: /Appearance.*light \/ dark/ })).toBeVisible();
  await expect(page.getByRole('menuitem', { name: /Stop turn/ })).toBeDisabled();
  await expect(page.getByRole('menuitem', { name: /Close session/ })).toBeEnabled();
  await page.screenshot({ path: testInfo.outputPath('viewer-overflow-menu.png'), fullPage: true });
  await page.locator('#sclose').click();

  await page.request.post('/test/reset', { data: { scenario: 'subagent' } });
  await page.reload();
  await openSubagent(page,'codex:thread-one','agent-child-one');
  await expect(page.locator('#sview')).toHaveClass(/light/);
  await page.getByRole('button', { name: 'session actions' }).click();
  await expect(page.getByRole('menuitem', { name: /Appearance.*light \/ dark/ })).toBeVisible();
  await expect(page.getByRole('menuitem', { name: /Stop turn/ })).toBeEnabled();
  await expect(page.getByRole('menuitem', { name: /Close session/ })).toBeEnabled();
  await page.locator('#sclose').click();

  await reset(page);
  const claude = page.locator('[data-sid="claude-one"]');
  await claude.locator('.shead').click();
  const openTerminal = page.locator('#sctrl > .termbtn');
  await expect(openTerminal).toHaveText('Terminal');
  await expect(openTerminal).toBeEnabled();
  expect(await openTerminal.evaluate(el => el.nextElementSibling.classList.contains('ovwrap'))).toBe(true);
  await openTerminal.click();
  await expect.poll(async () => (await fixtureState(page)).actions.at(-1)?.type).toBe('focus');
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
  await expect.poll(async () => (await fixtureState(page)).actions.at(-1)?.type).toBe('option');

  await page.request.post('/test/reset', { data: { scenario: 'single-question' } });
  await page.reload();
  await expect(page.locator('#sact')).toContainText('How broad should the change be?');
  await page.locator('#oth-smsg-codex\\:thread-one').fill('Only the adapter');
  await page.locator('#sact').getByRole('button', { name: 'answer' }).click();
  await expect.poll(async () => (await fixtureState(page)).actions.at(-1)?.other)
    .toBe('Only the adapter');

  const stale = await page.request.post('/api/act', { headers: { 'X-Act-Token': 'abcdef123456' },
    data: { type: 'option', session_id: 'codex:thread-one', nonce: 'old', digits: [1] } });
  expect((await stale.json()).ok).toBe(false);
  const invalid = await page.request.post('/api/act', { headers: { 'X-Act-Token': 'abcdef123456' },
    data: { type: 'option', session_id: 'codex:thread-one', nonce: 'q1', digits: [] } });
  expect((await invalid.json()).ok).toBe(false);

  await page.request.post('/test/reset', { data: { scenario: 'multi-question' } });
  await page.reload();
  await expect(page.locator('#sact')).toContainText('Targets');
  await page.locator('#sact').getByRole('button', { name: 'Desktop' }).click();
  await page.locator('#sact .mqarr').last().click();
  await page.locator('#sact').getByRole('button', { name: 'Full' }).click();
  await page.locator('#sact').getByRole('button', { name: 'submit all answers' }).click();
  await expect.poll(async () => (await fixtureState(page)).actions.at(-1)?.type).toBe('multiq');

  await page.request.post('/test/reset', { data: { scenario: 'single-question' } });
  await page.reload();
  await expect(page.locator('#sact')).toContainText('How broad should the change be?');
  await page.locator('#sact .xbtn').click();
  await expect.poll(async () => (await fixtureState(page)).actions.at(-1)?.type).toBe('dismiss');
});

test('composer follow-ups dismiss questions instead of selecting an option', async ({ page }) => {
  for (const scenario of ['single-question', 'claude-question-slow']) {
    await reset(page, scenario);
    const sid=scenario.startsWith('claude-')?'claude-one':'codex:thread-one';
    await openAction(page, sid);
    const composer=page.locator('#sact').getByPlaceholder('send message');
    await composer.fill(`Follow-up for ${scenario}`);
    await page.locator('#sact').getByRole('button',{name:'send',exact:true}).click();
    await expect.poll(async () => (await fixtureState(page)).actions.at(-1)?.type)
      .toBe('dismiss_then_send');
    const state=await fixtureState(page),action=state.actions.at(-1);
    expect(action).toMatchObject({session_id:sid,nonce:'q1',
      text:`Follow-up for ${scenario}`});
    expect(state.actions.some(row=>['option','multiq'].includes(row.type))).toBe(false);
    await expect(page.locator('#sact .question-drawer')).toHaveCount(0);
    await expect(page.locator('#sbody .optimistic[data-delivery-status="queued"]'))
      .toContainText(`Follow-up for ${scenario}`);
  }
});

test('a poll render during a mouse press cannot swallow the composer send', async ({ page }) => {
  await reset(page, 'base');
  await page.evaluate(() => openSession('codex:thread-one'));
  const composer = page.locator('#sact').getByPlaceholder('send message');
  await composer.fill('Press-guard message');
  const send = page.locator('#sact .pbtn.send');
  const box = await send.boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  // mousedown moves focus off the textarea, dropping the `typing` guard; an
  // unforced poll render landing here used to replace the button, so mouseup
  // fired click on the container instead and the send was silently lost.
  await page.mouse.down();
  await page.evaluate(() => render(last));
  await page.mouse.up();
  await expect.poll(async () => (await fixtureState(page)).actions.at(-1)?.type)
    .toBe('send_message');
  expect((await fixtureState(page)).actions.at(-1)).toMatchObject(
    { session_id: 'codex:thread-one', text: 'Press-guard message' });
});

test('composer follow-up keeps its draft when question dismissal is rejected', async ({ page }) => {
  await reset(page, 'single-question');
  await openAction(page, 'codex:thread-one');
  await page.route('**/api/act', async route => {
    const payload=route.request().postDataJSON();
    if(payload.type!=='dismiss_then_send')return route.continue();
    return route.fulfill({status:200,contentType:'application/json',
      body:JSON.stringify({ok:false,error:'provider rejected dismiss'})});
  });
  const composer=page.locator('#sact').getByPlaceholder('send message');
  await composer.fill('Keep this exact draft');
  await page.locator('#sact').getByRole('button',{name:'send',exact:true}).click();
  await expect(composer).toHaveValue('Keep this exact draft');
  await expect(page.locator('#sact .question-drawer')).toHaveCount(1);
  await expect(page.locator('#sbody .optimistic')).toHaveCount(0);
  await expect(page.locator('#sact')).toContainText('provider rejected dismiss');
});

test('offline question follow-up remembers the dismiss through reconnection', async ({ page }) => {
  await reset(page, 'single-question');
  await openAction(page, 'codex:thread-one');
  const composer=page.locator('#sact').getByPlaceholder('send message');
  await composer.fill('Send this after reconnecting');
  await page.evaluate(async () => {clearTimeout(pollTimer);setFleetOffline(true);
    await sendText('codex:thread-one','sft','smsg');});
  await expect(composer).toHaveValue('');
  const queued=await page.evaluate(() => JSON.parse(
    localStorage.getItem('fleet.offlineMessages.v1')||'[]'));
  expect(queued).toHaveLength(1);
  expect(queued[0].dismissNonce).toBe('q1');
  await page.evaluate(async () => {setFleetOffline(false);await flushOfflineMessages();});
  await expect.poll(async () => (await fixtureState(page)).actions.at(-1)?.type)
    .toBe('dismiss_then_send');
  expect((await fixtureState(page)).actions.at(-1)).toMatchObject({
    nonce:'q1',text:'Send this after reconnecting'});
});

test('Claude prompt controls stay disabled until the same native prompt is waiting', async ({ page }) => {
  await reset(page, 'claude-prompt-gate');
  await openAction(page, 'claude-one');
  const panel=page.locator('#sact');
  await expect(panel).toContainText("Waiting for Claude's native prompt state");
  await expect(panel.getByRole('button',{name:'Yes',exact:true})).toBeDisabled();
  await expect(panel.locator('.xbtn')).toBeDisabled();
  expect((await fixtureState(page)).actions.filter(item=>item.session_id==='claude-one')).toHaveLength(0);

  await page.request.post('/test/native-prompt-state',{
    data:{session_id:'claude-one',waiting:true}});
  await refresh(page);
  await expect(panel.getByRole('button',{name:'Yes',exact:true})).toBeEnabled();
  await panel.getByRole('button',{name:'Yes',exact:true}).click();
  await expect.poll(async()=>((await fixtureState(page)).actions.at(-1)||{}).type).toBe('option');
});

test('fullscreen question drawer preserves reading position and resizes from nearly full to collapsed', async ({ page }, testInfo) => {
  await reset(page, 'long-multi-question');
  await openAction(page, 'codex:thread-one');
  const drawer=page.locator('#sact .question-drawer');
  const scroll=page.locator('#sact .question-scroll');
  const grip=page.locator('#sact .question-resizer');
  await expect(drawer).toBeVisible();
  await expect(scroll).toContainText('Target 18');
  await expect(grip).toHaveCSS('touch-action','none');
  const readingTop=await scroll.evaluate(element=>{
    element.scrollTop=Math.max(1,element.scrollHeight-element.clientHeight-90);
    return element.scrollTop;
  });
  expect(readingTop).toBeGreaterThan(0);
  for(let index=0;index<3;index+=1)await refresh(page);
  await expect.poll(()=>scroll.evaluate(element=>element.scrollTop)).toBeGreaterThan(readingTop-3);
  await page.locator('#sact').getByRole('button',{name:/Target 18/}).click();
  await expect.poll(()=>scroll.evaluate(element=>element.scrollTop)).toBeGreaterThan(readingTop-3);

  const initial=await drawer.boundingBox();
  const gripBox=await grip.boundingBox();
  await page.mouse.move(gripBox.x+gripBox.width/2,gripBox.y+gripBox.height/2);
  await page.mouse.down();
  await page.mouse.move(gripBox.x+gripBox.width/2,1,{steps:8});
  await page.mouse.up();
  const expanded=await drawer.boundingBox();
  expect(expanded.height).toBeGreaterThan(initial.height+20);
  const geometry=await page.evaluate(()=>{
    const view=document.querySelector('#sview').getBoundingClientRect();
    const head=document.querySelector('#shead2').getBoundingClientRect();
    const tabs=document.querySelector('#stabs').getBoundingClientRect();
    const drawer=document.querySelector('#sact .question-drawer').getBoundingClientRect();
    const composer=document.querySelector('#sact .composer-dock').getBoundingClientRect();
    return {viewBottom:view.y+view.height,headBottom:head.y+head.height,tabsBottom:tabs.y+tabs.height,drawerTop:drawer.y,
      composerBottom:composer.y+composer.height};
  });
  expect(geometry.drawerTop).toBeLessThanOrEqual(geometry.tabsBottom+20);
  expect(geometry.composerBottom).toBeLessThanOrEqual(geometry.viewBottom+1);

  let expandedGrip=null;
  await expect.poll(async()=>{
    expandedGrip=await grip.boundingBox();
    return Boolean(expandedGrip);
  }).toBe(true);
  await page.mouse.move(expandedGrip.x+expandedGrip.width/2,expandedGrip.y+expandedGrip.height/2);
  await page.mouse.down();
  await page.mouse.move(expandedGrip.x+expandedGrip.width/2,expandedGrip.y+1200,{steps:5});
  await page.mouse.up();
  await expect(drawer).toHaveClass(/collapsed/);
  await expect(scroll).toHaveCount(0);
  await expect(drawer.getByRole('button')).toContainText('expand');

  await page.reload();
  await page.evaluate(() => openSession('codex:thread-one'));
  await expect(page.locator('#sview')).toBeVisible();
  await expect(drawer).toHaveClass(/collapsed/);
  await drawer.getByRole('button').click();
  await expect(page.locator('#sact .question-scroll')).toBeVisible();
  await drawer.locator('.question-drawer-toggle').click();
  await expect(drawer).toHaveClass(/collapsed/);
  await page.evaluate(()=>closeSession());
  await page.evaluate(()=>openSessionQ('codex:thread-one'));
  await expect(drawer).not.toHaveClass(/collapsed/);
  const restored=await drawer.boundingBox();
  expect(restored.height).toBeGreaterThanOrEqual(expanded.height-2);
  await page.screenshot({path:testInfo.outputPath('resizable-question-drawer.png'),fullPage:true});
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
  await expect(page.locator('#sact')).toContainText('How broad should the change be?');
  await page.locator('#sact').getByRole('button', { name: /Focused/ }).click();
  const answer = page.locator('#sbody .optimistic').filter({ hasText: 'Scope: Focused' });
  await expect(answer).toBeVisible();
  await expect(answer.getByLabel('sending')).toBeVisible();
  await expect(page.locator('#sact .pend')).toHaveCount(0);
  await page.request.post('/test/confirm', { data: { session_id: 'codex:thread-one',
    kind: 'answer', answers: [{ header: 'Scope', q: 'How broad?', a: 'Focused' }] } });
  await refresh(page);
  await expect(page.locator('#sbody .optimistic')).toHaveCount(0);
  await expect(page.locator('#sbody')).toContainText('Focused');

  await page.request.post('/test/reset', { data: { scenario: 'answer-failure' } });
  await page.reload();
  await expect(page.locator('#sact')).toContainText('How broad should the change be?');
  await page.locator('#sact').getByRole('button', { name: /Focused/ }).click();
  const failedAnswer = page.locator('#sbody .optimistic').filter({ hasText: 'Scope: Focused' });
  const restoreAnswer = failedAnswer.getByRole('button', {
    name: 'send failed; restore message' });
  await expect(restoreAnswer).toBeVisible();
  await expect(page.locator('#sact .pend')).toHaveCount(0);
  await restoreAnswer.click();
  await expect(page.locator('#sbody .optimistic')).toHaveCount(0);
  await expect(page.locator('#sact')).toContainText('How broad should the change be?');
  await expect(page.locator('#sact').getByRole('button', { name: /Focused/ })).toHaveClass(/sel/);

  await page.request.post('/test/reset', { data: { scenario: 'single-question' } });
  await page.reload();
  await expect(page.locator('#sact')).toContainText('How broad should the change be?');
  await page.route('**/api/act', route => {
    const payload=route.request().postDataJSON();
    return payload.type==='option'?route.abort('connectionfailed'):route.continue();
  });
  await page.locator('#sact').getByRole('button', { name: /Focused/ }).click();
  const uncertainAnswer=page.locator('#sbody .optimistic').filter({ hasText: 'Scope: Focused' });
  await expect(uncertainAnswer).toContainText('Delivery uncertain');
  await expect(uncertainAnswer.getByRole('button', { name: /restore/i })).toHaveCount(0);
  await expect(page.locator('#sact .pend')).toHaveCount(0);
  await page.unroute('**/api/act');
  page.__failures=page.__failures.filter(message=>
    message!=='Failed to load resource: net::ERR_CONNECTION_FAILED');

  await page.request.post('/test/reset', { data: { scenario: 'send-failure' } });
  await page.reload();
  await expect(page.locator('#sview')).toBeVisible();
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

test('near-bottom chat follows canonical replies and late growth but disengages when scrolled up', async ({ page }) => {
  await reset(page,'large-conversation');
  await page.evaluate(()=>openSession('codex:thread-one'));
  const body=page.locator('#sbody');
  await body.evaluate(element=>{element.scrollTop=Math.max(0,element.scrollHeight-element.clientHeight-80);
    element.dispatchEvent(new Event('scroll'));});
  expect(await page.evaluate(()=>sessionFollowTail)).toBe(true);
  await page.request.post('/test/confirm',{data:{session_id:'codex:thread-one',role:'assistant',
    text:'Newest canonical reply must remain fully visible'}});
  await refresh(page);
  await expect(body).toContainText('Newest canonical reply must remain fully visible');
  await expect.poll(()=>body.evaluate(element=>element.scrollHeight-element.scrollTop-element.clientHeight))
    .toBeLessThan(2);

  await body.evaluate(element=>{const target=sessionTailTarget(element);target.querySelector('.cbody').style.paddingBottom='220px';});
  await expect.poll(()=>body.evaluate(element=>element.scrollHeight-element.scrollTop-element.clientHeight))
    .toBeLessThan(10);

  await body.evaluate(element=>{element.dispatchEvent(new WheelEvent('wheel',{deltaY:-620,bubbles:true}));
    element.scrollTop=Math.max(0,element.scrollTop-620);
    element.dispatchEvent(new Event('scroll'));});
  await expect.poll(()=>page.evaluate(()=>sessionFollowTail)).toBe(false);
  const readingTop=await body.evaluate(element=>element.scrollTop);
  await page.request.post('/test/confirm',{data:{session_id:'codex:thread-one',role:'assistant',
    text:'This reply must not steal an older reading position'}});
  await refresh(page);
  await expect(body).toContainText('This reply must not steal an older reading position');
  expect(Math.abs((await body.evaluate(element=>element.scrollTop))-readingTop)).toBeLessThan(3);
});

test('unconfirmed optimistic messages recover without waiting on a full fleet render', async ({ page }) => {
  await page.clock.install();
  await reset(page);
  await page.locator('[data-sid="codex:thread-one"] .shead').click();
  const input=page.locator('#sft-codex\\:thread-one');
  await input.fill('Wait for transcript confirmation');
  await sendModifiedReturn(page,input);
  const receipt=page.locator('#sbody .optimistic').filter({
    hasText:'Wait for transcript confirmation'});
  await expect(receipt.getByLabel('sending')).toBeVisible();

  // Model a costly/throttled fleet repaint. Timeout recovery must update the
  // open receipt directly instead of depending on that unrelated work.
  await page.evaluate(()=>{
    clearTimeout(pollTimer);
    window.__fleetTestUiRefresh=uiRefresh;
    uiRefresh=()=>{};
  });
  await page.clock.fastForward(15_000);
  const restore=receipt.getByRole('button',{name:'send failed; restore message'});
  await expect(restore).toBeVisible();
  await page.evaluate(()=>{uiRefresh=window.__fleetTestUiRefresh;delete window.__fleetTestUiRefresh;});
  await restore.click();
  await expect(input).toHaveValue('Wait for transcript confirmation');
});

test('overdue optimistic deadlines reconcile after a throttled timer', async ({ page }) => {
  await reset(page);
  await page.locator('[data-sid="codex:thread-one"] .shead').click();
  const input=page.locator('#sft-codex\\:thread-one');
  await input.fill('Recover after a throttled timer');
  await sendModifiedReturn(page,input);
  const throttled=page.locator('#sbody .optimistic').filter({
    hasText:'Recover after a throttled timer'});
  await expect(throttled.getByLabel('sending')).toBeVisible();
  await page.evaluate(()=>{
    const item=optimisticList('codex:thread-one').find(entry=>
      entry.text==='Recover after a throttled timer');
    clearTimeout(item.confirmTimer);
    item.confirmDeadline=Date.now()-1;
    uiRefresh();
  });
  await expect(throttled.getByRole('button',{
    name:'send failed; restore message'})).toBeVisible();
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
    type: 'send_message', text: 'First line\nSecond line' });

  await page.request.post('/test/reset', { data: { scenario: 'subagent' } });
  await page.reload();
  await openSubagent(page,'codex:thread-one','agent-child-one');
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

test('mobile chat keeps a docked composer and dismisses it on a vertical history drag', async ({ page }, testInfo) => {
  test.skip(!testInfo.project.name.startsWith('mobile'), 'mobile interaction');
  await reset(page);
  await page.locator('[data-sid="codex:thread-one"] .shead').click();
  const composer=page.locator('#sft-codex\\:thread-one');
  await composer.focus();
  await expect(page.locator('#sact')).toHaveClass(/composer-active/);
  await expect(page.locator('#sact .session-context')).toBeHidden();
  const bounds=await page.locator('#sview').boundingBox();
  expect(Math.round(bounds.y+bounds.height)).toBeLessThanOrEqual(844);
  const headerBounds=await page.locator('#shead2').evaluate(header=>{const close=header.querySelector('#sclose').getBoundingClientRect(),controls=header.querySelector('#sctrl').getBoundingClientRect();return{
    scrollWidth:header.scrollWidth,clientWidth:header.clientWidth,closeLeft:close.left,
    closeTop:close.top,controlsRight:controls.right,viewport:innerWidth};});
  expect(headerBounds.scrollWidth).toBeLessThanOrEqual(headerBounds.clientWidth);
  expect(headerBounds.closeLeft).toBeGreaterThanOrEqual(0);
  expect(headerBounds.closeTop).toBeGreaterThanOrEqual(0);
  expect(headerBounds.controlsRight).toBeLessThanOrEqual(headerBounds.viewport);
  await page.screenshot({path:testInfo.outputPath('mobile-composer-focused.png'),fullPage:true});
  await page.locator('#sbody').evaluate(body=>{
    const touch=(type,x,y)=>{
      const event=new Event(type,{bubbles:true,cancelable:true});
      Object.defineProperty(event,'touches',{value:type==='touchend'?[]:[{clientX:x,clientY:y}]});
      body.dispatchEvent(event);
    };
    touch('touchstart',100,500);touch('touchmove',102,450);touch('touchend',102,450);
  });
  await expect(composer).not.toBeFocused();
  await expect(page.locator('#sact')).not.toHaveClass(/composer-active/);
});

test('mobile session swipes are bounded, ignore horizontal readers, and edge-exit', async ({ page }, testInfo) => {
  test.skip(!testInfo.project.name.startsWith('mobile'), 'mobile workspace gestures');
  await reset(page);
  await page.evaluate(()=>openSession('codex:thread-one'));
  const swipe=async (fromX,toX,target='#sworkspace')=>page.locator(target).evaluate((node,{fromX,toX})=>{
    const fire=(type,x)=>{const event=new Event(type,{bubbles:true,cancelable:true});
      const point={clientX:x,clientY:360};
      Object.defineProperty(event,'touches',{value:type==='touchend'?[]:[point]});
      Object.defineProperty(event,'changedTouches',{value:[point]});node.dispatchEvent(event);};
    fire('touchstart',fromX);fire('touchmove',toX);fire('touchend',toX);
  },{fromX,toX});

  await swipe(330,90);await expect(page.locator('#stab-files')).toHaveAttribute('aria-selected','true');
  await swipe(330,90);await expect(page.locator('#stab-subagents')).toHaveAttribute('aria-selected','true');
  await swipe(330,90);await expect(page.locator('#stab-details')).toHaveAttribute('aria-selected','true');
  await swipe(330,90);await expect(page.locator('#stab-details')).toHaveAttribute('aria-selected','true');
  await swipe(70,320);await expect(page.locator('#stab-subagents')).toHaveAttribute('aria-selected','true');
  await swipe(70,320);await expect(page.locator('#stab-files')).toHaveAttribute('aria-selected','true');
  await swipe(70,320);await expect(page.locator('#stab-chat')).toHaveAttribute('aria-selected','true');

  await page.locator('#sbody').evaluate(body=>{const scroller=document.createElement('div');
    scroller.id='gesture-scroll-probe';scroller.style.cssText='overflow-x:auto;width:120px';
    scroller.innerHTML='<span style="display:block;width:600px">horizontal reader</span>';body.prepend(scroller);});
  await swipe(330,90,'#gesture-scroll-probe');
  await expect(page.locator('#stab-chat')).toHaveAttribute('aria-selected','true');

  await swipe(8,180);
  await expect(page.locator('#sview')).toBeHidden();
  await expect(page.locator('#route-now')).toBeVisible();
  await expect(page).not.toHaveURL(/#session\//);
});

test('mobile keyboard geometry is flush and preserves chat and Markdown reading anchors', async ({ page }, testInfo) => {
  test.skip(!testInfo.project.name.startsWith('mobile'), 'mobile visual viewport contract');
  await reset(page,'large-conversation');
  await page.evaluate(()=>openSession('codex:thread-one'));
  const body=page.locator('#sbody');
  await expect(body).toContainText('Conversation message 204');
  await body.evaluate(element=>{element.scrollTop=Math.max(0,element.scrollHeight-element.clientHeight-520);
    element.dispatchEvent(new Event('scroll'));});
  const visibleAnchor=async locator=>locator.evaluate(element=>{const rect=element.getBoundingClientRect();
    const rows=[...element.querySelectorAll(element.id==='sbody'?'.aconvo > *':'.mdoc > *')];
    const row=rows.find(item=>item.getBoundingClientRect().bottom>rect.top+1);return{
      text:row?.textContent.trim(),offset:row?row.getBoundingClientRect().top-rect.top:0};});
  const before=await visibleAnchor(body);
  const composer=page.locator('#sact').getByPlaceholder('send message');await composer.focus();
  await page.evaluate(()=>{globalThis.__fleetVisualViewportOverride={height:520,offsetTop:0};syncVisualViewport();});
  await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  const focused=await visibleAnchor(body);
  expect(focused.text).toBe(before.text);expect(Math.abs(focused.offset-before.offset)).toBeLessThan(2);
  const seam=await page.evaluate(()=>{const view=document.querySelector('#sview').getBoundingClientRect(),
    dock=document.querySelector('#sact .composer-dock').getBoundingClientRect(),
    row=document.querySelector('#sact .freetext.composer').getBoundingClientRect();return{
      viewBottom:view.bottom,dockBottom:dock.bottom,rowBottom:row.bottom};});
  expect(Math.abs(seam.viewBottom-seam.dockBottom)).toBeLessThan(.6);
  expect(seam.dockBottom-seam.rowBottom).toBeLessThanOrEqual(3);
  await composer.evaluate(element=>element.blur());
  await page.evaluate(()=>{globalThis.__fleetVisualViewportOverride={height:844,offsetTop:0};syncVisualViewport();});
  await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  const restored=await visibleAnchor(body);
  expect(restored.text).toBe(before.text);expect(Math.abs(restored.offset-before.offset)).toBeLessThan(2);

  await page.locator('#sact .latestfile').click();
  const viewerBody=page.locator('#vbody');
  await expect(viewerBody).toContainText('Safe preview');
  await viewerBody.evaluate(element=>{element.innerHTML='<div class="mdoc">'+Array.from({length:90},(_,index)=>
    `<p>Markdown reading block ${String(index).padStart(3,'0')} with enough detail to wrap across the phone.</p>`).join('')+'</div>';
    element.scrollTop=760;});
  const viewerBefore=await visibleAnchor(viewerBody);
  const viewerComposer=page.locator('#sact').getByPlaceholder('send message');
  await viewerComposer.focus();
  await page.evaluate(()=>{globalThis.__fleetVisualViewportOverride={height:520,offsetTop:0};syncVisualViewport();});
  await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  const viewerAfter=await visibleAnchor(viewerBody);
  expect(viewerAfter.text).toBe(viewerBefore.text);
  expect(Math.abs(viewerAfter.offset-viewerBefore.offset)).toBeLessThan(2);
  await page.evaluate(()=>{delete globalThis.__fleetVisualViewportOverride;syncVisualViewport();});
});

test('Files uses the canonical composer and the persistent Chat tab', async ({ page }, testInfo) => {
  await reset(page);
  await openFixtureFile(page,'codex:thread-one','artifact.md');
  await expect(page.locator('#vbody')).toContainText('Safe preview');
  await expect(page.locator('#vbody .aconvo')).toHaveCount(0);
  const viewerComposer = page.locator('#sact .freetext.composer');
  await expect(viewerComposer.getByPlaceholder('send message')).toBeVisible();
  await expect(viewerComposer.getByRole('button', {name:'message options'})).toBeVisible();
  await expect(viewerComposer.getByRole('button', {name:'send', exact:true})).toBeVisible();
  await expect(page.locator('#stab-chat')).toBeVisible();
  expect(await viewerComposer.evaluate(element=>[...element.children].map(child=>
    child.matches('.composertools')?'plus':child.tagName==='TEXTAREA'?'message':child.textContent.trim())))
    .toEqual(['plus','message','send']);
  await page.screenshot({path:testInfo.outputPath('markdown-canonical-composer.png'),fullPage:true});
  await viewerComposer.getByPlaceholder('send message').fill('Draft shared across reading surfaces');
  await viewerComposer.getByPlaceholder('send message').evaluate(element=>element.blur());
  await page.locator('#stab-chat').click();
  await expect(page.locator('#spanel-chat')).toBeVisible();
  const chatComposer = page.locator('#sact .freetext.composer');
  await expect(chatComposer.getByPlaceholder('send message'))
    .toHaveValue('Draft shared across reading surfaces');
  await expect(chatComposer.getByRole('button', {name:'message options'})).toBeVisible();
  await expect(chatComposer.getByRole('button', {name:'send', exact:true})).toBeVisible();
});

test('Chat and Files share exact composer geometry and one persistent header', async ({ page }, testInfo) => {
  await reset(page);
  await page.evaluate(() => openSession('claude-one'));
  const chatComposer=page.locator('#sact .freetext.composer');
  await expect(chatComposer).toBeVisible();
  await expect(page.locator('#sact .latestfile')).toContainText('artifact.md');
  expect(await chatComposer.evaluate(element=>[...element.children].map(child=>
    child.matches('.composertools')?'plus':child.tagName==='TEXTAREA'?'message':child.textContent.trim())))
    .toEqual(['plus','message','send']);
  const resting=await chatComposer.evaluate(element=>[...element.children].map(child=>
    child.matches('.composertools')?child.querySelector('button').getBoundingClientRect().height:
      child.getBoundingClientRect().height));
  expect(Math.max(...resting)-Math.min(...resting)).toBeLessThan(.6);
  expect(resting[0]).toBe(44);
  const input=chatComposer.getByPlaceholder('send message');
  await input.fill('one\ntwo\nthree\nfour');
  const grown=await input.evaluate(element=>element.getBoundingClientRect().height);
  expect(grown).toBeGreaterThan(resting[1]+60);
  await input.fill('one\ntwo\nthree\nfour\nfive');
  expect(await input.evaluate(element=>element.getBoundingClientRect().height)).toBe(grown);
  await input.fill('');
  await input.evaluate(element=>element.blur());
  await expect(page.locator('#sact')).not.toHaveClass(/composer-active/);
  await expect(page.locator('#sact .fstrip')).toHaveCount(0);
  const chatHeader=await page.locator('#shead2').evaluate(header=>{const title=header.querySelector('#stitle2').getBoundingClientRect(),
    close=header.querySelector('#sclose').getBoundingClientRect(),controls=header.querySelector('#sctrl').getBoundingClientRect();return{
    titleLeft:title.left,closeRight:close.right,titleRight:title.right,controlsLeft:controls.left,
    font:parseFloat(getComputedStyle(header.querySelector('#stitle2 b')).fontSize)};});
  expect(chatHeader.titleLeft-chatHeader.closeRight).toBeGreaterThanOrEqual(7);
  expect(chatHeader.titleRight).toBeLessThanOrEqual(chatHeader.controlsLeft);
  expect(chatHeader.font).toBeGreaterThanOrEqual(16);
  if(testInfo.project.name.startsWith('mobile')){
    const compact=await page.locator('#sact .session-surfacebar').evaluate(element=>({
      status:element.querySelector('.statusstrip').getBoundingClientRect().height,
      file:element.querySelector('.latestfile').getBoundingClientRect().height}));
    expect(Math.abs(compact.status-compact.file)).toBeLessThan(.6);
    await page.locator('#sact .status-expand').click();
    const expanded=await page.locator('#sact .session-surfacebar').evaluate(element=>{
      const status=element.querySelector('.statusstrip').getBoundingClientRect(),
        file=element.querySelector('.latestfile').getBoundingClientRect();
      return{status:status.height,file:file.height,statusBottom:status.bottom,fileBottom:file.bottom};});
    expect(expanded.status).toBeGreaterThan(compact.status);
    expect(expanded.file).toBe(compact.file);
    expect(Math.abs(expanded.statusBottom-expanded.fileBottom)).toBeLessThan(.6);
  }
  await page.locator('#sact .latestfile').click();
  const viewerComposer=page.locator('#sact .freetext.composer');
  await expect(viewerComposer).toBeVisible();
  expect(await viewerComposer.evaluate(element=>[...element.children].map(child=>
    child.matches('.composertools')?'plus':child.tagName==='TEXTAREA'?'message':child.textContent.trim())))
    .toEqual(['plus','message','send']);
  const viewerResting=await viewerComposer.evaluate(element=>[...element.children].map(child=>
    child.matches('.composertools')?child.querySelector('button').getBoundingClientRect().height:
      child.getBoundingClientRect().height));
  expect(viewerResting).toEqual(resting);
  await expect(page.locator('#shead2')).toBeVisible();
  await expect(page.locator('#stabs [role="tab"]')).toHaveCount(4);
  const panes=await page.locator('#sfilebrowser').evaluate(element=>({
    width:element.getBoundingClientRect().width,
    list:element.querySelector('#sfilelist').getBoundingClientRect().width,
    divider:element.querySelector('.workspacedivider').getBoundingClientRect().width,
    preview:element.querySelector('#sfilepreview').getBoundingClientRect().width}));
  expect(panes.list+panes.divider+panes.preview).toBeGreaterThanOrEqual(panes.width-2);
});

test('phone image menu keeps the trusted tap, persists, and sends through the owning provider', async ({ page }, testInfo) => {
  await reset(page);
  await page.locator('[data-sid="codex:thread-one"] .shead').click();
  await expect(page.locator('#sact').getByRole('menu')).toBeHidden();
  await page.locator('#sact').getByRole('button',{name:'message options'}).click();
  const pictureAction=page.locator('#sact').getByRole('menuitem',{name:'Send picture'});
  await expect(pictureAction).toBeVisible();
  await expect(page.locator('#sact').getByRole('menuitem',{name:'Schedule message'})).toBeVisible();
  const image={name:'phone-photo.png',mimeType:'image/png',
    buffer:Buffer.from([137,80,78,71,13,10,26,10,0,0,0,0])};
  const chooserPromise=page.waitForEvent('filechooser');
  if(testInfo.project.name.startsWith('mobile'))await pictureAction.tap();
  else await pictureAction.click();
  const chooser=await chooserPromise;await chooser.setFiles(image);
  await expect(page.locator('#sact').getByRole('menu')).toBeHidden();
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
    action.type==='send_message'&&action.upload_ids?.length)).toMatchObject({session_id:'codex:thread-one',
      text:'Inspect the mobile screenshot'});
  expect((await fixtureState(page)).actions.find(action=>
    action.type==='send_message'&&action.upload_ids?.length).upload_ids)
    .toHaveLength(1);
  await expect.poll(() => page.evaluate(() => localStorage.getItem('fleet.imageDrafts.v1')))
    .toBeNull();
});

test('Files Photo uses the trusted input target and shares one draft with Chat', async ({ page }, testInfo) => {
  await reset(page);
  await openFixtureFile(page,'codex:thread-one','artifact.md');
  const options=page.locator('#sact').getByRole('button',{name:'message options'});
  await options.click();
  let chooserPromise=page.waitForEvent('filechooser');
  const photo=page.locator('#sact').getByRole('menuitem',{name:'Send picture'});
  if(testInfo.project.name.startsWith('mobile'))await photo.tap();else await photo.click();
  let chooser=await chooserPromise;await chooser.setFiles([]);
  await expect(page.locator('#sact .image-draft')).toHaveCount(0);

  await page.evaluate(()=>closeComposerMenus());
  await options.click();
  await page.locator('#sact .composer-file-input').setInputFiles({name:'viewer-photo.jpg',mimeType:'image/jpeg',
    buffer:Buffer.from([255,216,255,224,0,16,74,70,73,70])});
  await expect(page.locator('#sact .image-draft')).toContainText('viewer-photo.jpg');
  await page.locator('#stab-chat').click();
  await expect(page.locator('#sact .image-draft')).toContainText('viewer-photo.jpg');
  expect(await page.evaluate(()=>JSON.parse(localStorage.getItem('fleet.imageDrafts.v1')||'{}')
    ['codex:thread-one']?.length)).toBe(1);
});

test('default send visibly queues a busy session and never becomes a false failure', async ({ page }) => {
  await reset(page,'send-while-busy');
  await page.locator('[data-sid="claude-one"] .shead').click();
  const input=page.locator('#sft-claude-one');
  await input.fill('Send after the current turn');
  await page.locator('#sact').getByRole('button',{name:'send'}).click();
  const receipt=page.locator('#sbody .optimistic').filter({hasText:'Send after the current turn'});
  await expect(receipt).toContainText('Queued · waiting for session');
  await expect(receipt.locator('[aria-label="message queued"]')).toBeVisible();
  await expect.poll(async () => (await fixtureState(page)).outbox).toHaveLength(1);
  expect((await fixtureState(page)).outbox[0]).toMatchObject({
    kind:'when_available',state:'waiting_availability',target_session_id:'claude-one',
    origin:'automatic_fallback',message:'Send after the current turn'});
  await page.waitForTimeout(15_500);
  await expect(receipt).toContainText('Queued · waiting for session');
  await expect(receipt.getByRole('button',{name:'send failed; restore message'})).toHaveCount(0);
  await page.locator('#sclose').click();
  await expect(page.locator('[data-sid="claude-one"] .quickfeedback'))
    .toContainText('Queued · waiting for session');
  await page.reload();
  await expect(page.locator('#outboxsummary')).toContainText('1 waiting to send');
  await page.evaluate(()=>openSession('claude-one'));
  const restored=page.locator('#sbody .optimistic').filter({hasText:'Send after the current turn'});
  await expect(restored).toContainText('Queued · waiting for session');
  expect(await page.evaluate(()=>Object.keys(JSON.parse(localStorage.getItem('fleet.outboxReceipts.v1')||'{}')).length)).toBe(1);
});

test('failed automatic-send receipts dismiss or restore once and never resurrect', async ({ page }) => {
  await reset(page,'send-while-busy');
  await page.evaluate(()=>openSession('claude-one'));
  const input=page.locator('#sft-claude-one');
  for(const message of ['Dismiss this failed delivery','Restore this failed delivery']){
    await input.fill(message);await page.locator('#sact').getByRole('button',{name:'send'}).click();
  }
  const queued=(await fixtureState(page)).outbox;
  expect(queued).toHaveLength(2);
  for(const item of queued)await page.request.post('/test/outbox-state',{
    data:{outbox_id:item.id,state:'failed',error:'Provider rejected the queued delivery'}});
  await page.evaluate(()=>loadOutbox(true));
  const dismissRow=page.locator('#sbody .optimistic').filter({hasText:'Dismiss this failed delivery'});
  const restoreRow=page.locator('#sbody .optimistic').filter({hasText:'Restore this failed delivery'});
  await expect(dismissRow.getByRole('button',{name:'dismiss failed message receipt'})).toBeVisible();
  await expect(restoreRow.getByRole('button',{name:'send failed; restore message'})).toBeVisible();
  await dismissRow.getByRole('button',{name:'dismiss failed message receipt'}).click();
  await restoreRow.getByRole('button',{name:'send failed; restore message'}).click();
  await expect(input).toHaveValue('Restore this failed delivery');
  await expect(page.locator('#sbody .optimistic')).toHaveCount(0);
  expect(await page.evaluate(()=>Object.keys(JSON.parse(localStorage.getItem('fleet.outboxResolved.v1')||'{}')).sort()))
    .toEqual(queued.map(item=>item.id).sort());
  await page.evaluate(()=>loadOutbox(true));
  await expect(page.locator('#sbody .optimistic')).toHaveCount(0);
  await page.reload();await page.evaluate(()=>openSession('claude-one'));await page.evaluate(()=>loadOutbox(true));
  await expect(page.locator('#sbody .optimistic')).toHaveCount(0);
  await expect(page.locator('#sft-claude-one')).toHaveValue('Restore this failed delivery');
});

test('scheduled sends lock duplicate submits and carry a stable idempotency key', async ({ page }) => {
  await reset(page,'base');
  await page.locator('[data-sid="codex:thread-one"] .shead').click();
  await page.locator('#sft-codex\\:thread-one').fill('Create this schedule once');
  await page.evaluate(()=>openSchedule('codex:thread-one','sft-codex:thread-one'));
  await expect(page.locator('#scheduleview')).toBeVisible();
  await page.evaluate(()=>Promise.all([submitSchedule(),submitSchedule()]));
  await expect(page.locator('#scheduleview')).toBeHidden();
  const actions=(await fixtureState(page)).actions.filter(item=>item.type==='outbox_create');
  expect(actions).toHaveLength(1);
  expect(actions[0].client_request_id).toMatch(/^schedule-/);
});

test('an ambiguous offline flush stays durable and never retries automatically', async ({ page }) => {
  await reset(page,'base');
  await page.locator('[data-sid="codex:thread-one"] .shead').click();
  await page.evaluate(()=>setFleetOffline(true));
  await page.locator('#sft-codex\\:thread-one').fill('Do not duplicate this uncertain delivery');
  await page.locator('#sact').getByRole('button',{name:'send'}).click();
  let sendAttempts=0;
  await page.route('**/api/act',route=>{
    const payload=route.request().postDataJSON();
    if(payload.type==='send_message'){sendAttempts++;return route.abort('connectionfailed');}
    return route.continue();
  });
  await page.evaluate(async()=>{setFleetOffline(false);await flushOfflineMessages();});
  expect(sendAttempts).toBe(1);
  expect(await page.evaluate(()=>JSON.parse(localStorage.getItem('fleet.offlineMessages.v1')||'[]')[0]?.state))
    .toBe('confirmation_unknown');
  await page.evaluate(()=>flushOfflineMessages());
  expect(sendAttempts).toBe(1);
  await page.reload();await page.evaluate(()=>openSession('codex:thread-one'));
  await expect(page.locator('#sbody .optimistic')).toContainText('Delivery unconfirmed');
  await page.evaluate(()=>flushOfflineMessages());expect(sendAttempts).toBe(1);
  page.__failures=page.__failures.filter(message=>!/ERR_CONNECTION_FAILED|Failed to load resource/.test(message));
  await page.unroute('**/api/act');
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
      action.type==='send_message'&&action.upload_ids?.length&&
      action.session_id==='claude-one').length).toBe(1);
    await page.waitForTimeout(2200);
    expect((await fixtureState(page)).uploads).toHaveLength(1);
    expect((await fixtureState(page)).actions.filter(action=>
      action.type==='send_message'&&action.upload_ids?.length&&
      action.session_id==='claude-one')).toHaveLength(1);
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
  const composer=page.locator('#sft-codex\\:thread-one');
  await expect(composer).toHaveValue('unsent composer draft');
  await composer.fill('');
  await page.reload();
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

test('new-session, scheduled-spawn, and handoff model dependencies stay synchronized', async ({ page }) => {
  await reset(page,'base');
  await page.getByRole('button',{name:'+ new coding session'}).click();
  const form=page.locator('.newform');
  await form.locator('.nfrow .nfcol').nth(0).locator('select').selectOption('sonnet');
  await form.locator('.nfrow .nfcol').nth(1).locator('select').selectOption('low');
  await form.locator('select').first().selectOption('codex');
  await expect(form.locator('.nfrow .nfcol').nth(0).locator('select')).toHaveValue('');
  await expect(form.locator('.nfrow .nfcol').nth(0).locator('select option[value="gpt-5.4"]')).toHaveCount(1);
  await expect(form.locator('.nfrow .nfcol').nth(1).locator('select')).toHaveValue('');
  await expect(form.locator('.nfrow .nfcol').nth(1).locator('select option[value="low"]')).toHaveCount(0);

  await form.locator('select').first().selectOption('claude');
  await form.locator('select').nth(1).selectOption('/Users/test/fleet-dash');
  await form.locator('textarea').fill('Run this scheduled dependency check');
  await form.getByRole('button',{name:'schedule session'}).click();
  const schedule=page.locator('#scheduleview');
  await schedule.locator('select').first().selectOption('codex');
  await expect(schedule.locator('.nfrow .nfcol').nth(0).locator('select option[value="gpt-5.4"]')).toHaveCount(1);
  await expect(schedule.locator('.nfrow .nfcol').nth(1).locator('select option[value="low"]')).toHaveCount(0);
  await page.evaluate(()=>dismissOverlay());

  await page.evaluate(()=>openHandoff('codex:thread-one','claude'));
  await expect(page.locator('#handoffpreview')).toBeVisible();
  await page.locator('#handoffview select').first().selectOption('codex');
  await expect(page.locator('#handoffpreview')).toBeVisible();
  await page.locator('#handoffbody details').evaluate(element=>element.open=true);
  await expect(page.locator('#handoffbody details select').first().locator('option[value="gpt-5.4"]')).toHaveCount(1);
});

test('failed commands keep their exact durable draft', async ({ page }) => {
  await reset(page,'base');
  await page.locator('[data-sid="codex:thread-one"] .shead').click();
  const input=page.locator('#sft-codex\\:thread-one');
  await input.fill('/rev');
  await expect(page.locator('.slashmenu')).toContainText('/review');
  await page.getByRole('button',{name:/\/review/}).click();
  await page.route('**/api/act',async route=>{
    const payload=route.request().postDataJSON();
    if(payload.type==='review')return route.fulfill({status:200,contentType:'application/json',
      body:JSON.stringify({ok:false,error:'review unavailable'})});
    return route.continue();
  });
  await sendModifiedReturn(page,input);
  await expect(input).toHaveValue('/review ');
  await expect(page.locator('#sact')).toContainText('review unavailable');
  await page.reload();await page.evaluate(()=>openSession('codex:thread-one'));
  await expect(page.locator('#sft-codex\\:thread-one')).toHaveValue('/review ');
  await page.unroute('**/api/act');
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
  await expect(page.locator('#sact .pend')).toHaveCount(0);
  await page.locator('#sclose').click();
  await expect(page.locator('#sview')).toBeHidden();
  let feedback = fleetFeedback('claude-one');
  await expect(feedback).toContainText(/Submitting|Submitted/);
  await expect(feedback).toContainText('Scope: Focused');
  if((await feedback.textContent()).includes('Submitting'))
    await expect(feedback.getByLabel('sending quick response')).toBeVisible();
  await expect.poll(async () => page.evaluate(() =>
    window.__fleetPerf.summary().input_feedback_ms.p95)).toBeLessThan(100);
  await expect(feedback).toContainText('Submitted', { timeout: 5_000 });
  await expect(feedback.getByLabel('sending quick response')).toBeVisible();

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
  await expect(feedback).toContainText(/Submitting|Submitted/);
  await expect(feedback).toContainText('Allow permission');
  if((await feedback.textContent()).includes('Submitting'))
    await expect(feedback.getByLabel('sending quick response')).toBeVisible();
  await expect(feedback).toContainText('Submitted', { timeout: 5_000 });
});

test('every approval decision and MCP single/multi-select elicitation', async ({ page }) => {
  for (const [label, choice] of [['allow', 'allow'], ['always allow', 'always'],
                                 ['deny', 'deny'], ['cancel', 'cancel']]) {
    await reset(page, 'approval');
    await openAction(page, 'codex:thread-one');
    await page.locator('#sact').getByRole('button', { name: label, exact: true }).click();
    await expect.poll(async () => (await fixtureState(page)).actions.at(-1)?.choice)
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
  await card.locator('.shead').click();
  await page.locator('#stab-details').click();
  await page.locator('#detail-notifications button.bell').click();
  await refresh(page);
  await expect(page.locator('#detail-notifications button.bell')).toHaveClass(/muted/);

  await page.locator('#stab-chat').click();
  const input = page.locator('#sft-codex\\:thread-one');
  await input.fill('$rev');
  await expect(page.locator('.slashmenu')).toContainText('$reviewer');
  await page.getByRole('button', { name: /\$reviewer/ }).click();
  await sendModifiedReturn(page, input);
  await expect.poll(async () => (await fixtureState(page)).actions.at(-1)?.type).toBe('skill');

  await page.request.post('/test/reset', { data: { scenario: 'subagent' } });
  await page.reload();
  await openSubagent(page,'codex:thread-one','agent-child-one');
  await expect(page.locator('#stab-subagents')).toHaveAttribute('aria-selected','true');
  await page.locator('#aft').fill('Report the risky mappings');
  await page.locator('#sact').getByRole('button', { name: 'relay' }).click();
  await expect.poll(async () => (await fixtureState(page)).actions.at(-1))
    .toMatchObject({ type: 'relay', agent_id: 'agent-child-one' });
  await expect(page.locator('#sact')).toContainText('parent thread');
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
  await page.locator('#stab-details').click();
  await expect(page.locator('#detail-continuation').getByRole('button', { name: 'reopen in terminal' })).toBeVisible();
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
  await page.locator('#sact').getByRole('button', { name: 'message options' }).click();
  await page.locator('#sact').getByRole('menuitem', { name: 'Schedule message' }).click();
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
  await page.locator('.outboxrow').first().getByRole('button', { name: 'Delete', exact: true }).click();
  await expect(page.locator('#confirm')).toContainText('original delivery outcome remains');
  await page.locator('#confirm').getByRole('button', { name: 'delete message' }).click();
  await expect.poll(async () => (await fixtureState(page)).outbox[0].cancelled).toBe(true);
  await expect(page.locator('.outboxrow')).toHaveCount(0);
  await page.locator('.outboxtools').getByRole('button', { name: 'Cancelled', exact: true }).click();
  await expect(page.locator('.outboxrow').first()).toContainText('Cancelled');
  await expect(page.locator('.outboxrow').first()).toContainText('was Sent');
  await expect(page.locator('.outboxrow').first().getByRole('button', { name: 'Delete' })).toHaveCount(0);
});

test('usage-reset and scheduled-new-session forms keep full target configuration', async ({ page }) => {
  await reset(page, 'base');
  await page.locator('[data-sid="claude-one"] .shead').click();
  await page.locator('#sft-claude-one').fill('Continue after my Claude usage resets');
  await page.locator('#sact').getByRole('button', { name: 'message options' }).click();
  await page.locator('#sact').getByRole('menuitem', { name: 'Schedule message' }).click();
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

test('notification actions are event-scoped, reject double taps, and cannot leak across detail races', async ({ page }) => {
  await reset(page,'base');await goTo(page,'notifications');
  await page.getByRole('button',{name:/Choose a release target/}).click();
  await page.evaluate(()=>{
    const realFetch=window.fetch.bind(window);let release;
    globalThis.__notificationSnoozeCalls=0;
    globalThis.__releaseNotificationSnooze=()=>release?.(new Response(JSON.stringify({ok:true}),
      {status:200,headers:{'Content-Type':'application/json'}}));
    window.fetch=(input,init)=>String(input).includes('/api/notifications/snooze')?
      (globalThis.__notificationSnoozeCalls++,new Promise(resolve=>{release=resolve;})):realFetch(input,init);
    snoozeNotification('evt-6-question','rev-6','quarter');
    snoozeNotification('evt-6-question','rev-6','quarter');
  });
  await expect(page.getByRole('button',{name:'Snooze 15m'})).toBeDisabled();
  expect(await page.evaluate(()=>globalThis.__notificationSnoozeCalls)).toBe(1);

  await page.evaluate(()=>openNotification('evt-5-failure'));
  await expect(page.locator('#notificationdetail')).toContainText('Codex connection interrupted');
  await expect(page.locator('#notificationdetail')).not.toContainText('Working…');
  await expect(page.getByRole('button',{name:'Mute session'})).toBeEnabled();
  await page.getByRole('button',{name:'Mute session'}).click();
  await expect(page.locator('#notificationdetail')).toContainText('Session muted until you unmute it');
  await page.evaluate(()=>globalThis.__releaseNotificationSnooze());
  await expect.poll(()=>page.evaluate(()=>globalThis.__notificationSnoozeCalls)).toBe(1);
});

test('an omitted fleet row cannot close or erase an open conversation', async ({ page }) => {
  await reset(page,'base');
  await page.locator('[data-sid="codex:thread-one"] .primarybtn').click();
  await expect(page.locator('#sview')).toBeVisible();
  const fleet=await(await page.request.get('/api/fleet')).json();
  const omitted=structuredClone(fleet);omitted.sessions=omitted.sessions.filter(item=>item.session_id!=='codex:thread-one');
  // Apply the exact transient snapshot deterministically. A background poll is
  // deliberately not involved: this contract starts at render's accepted
  // server snapshot boundary.
  await page.evaluate(snapshot=>{clearTimeout(pollTimer);last=snapshot;render(last,true);},omitted);
  await expect(page.locator('#sview')).toBeVisible();
  await expect(page.locator('#sactivity')).toContainText('Reconnecting to session');
  await expect(page.locator('#sbody')).toContainText('Working through the matrix');
  await page.evaluate(snapshot=>{last=snapshot;render(last,true);},fleet);
  await expect(page.locator('#sactivity')).not.toContainText('Reconnecting to session');
  await expect(page.locator('#sview')).toBeVisible();
});

test('full-screen surfaces are semantic focus modals and session headers open Chat by keyboard', async ({ page }) => {
  await reset(page,'base');
  const card=page.locator('[data-sid="claude-one"]');
  const header=card.locator('.shead');
  await expect(card.locator('.sessionopen')).toHaveCount(0);
  await card.locator('.spin').click();
  await expect(page.locator('#sview')).toBeHidden();
  await card.locator('.smeta').click();
  await expect(page.locator('#sview')).toBeVisible();
  await page.locator('#sclose').click();
  await expect(page.locator('#sview')).toBeHidden();
  await header.focus();await page.keyboard.press('Enter');
  const session=page.locator('#sview');await expect(session).toBeVisible();
  await expect(session).toHaveAttribute('role','dialog');await expect(session).toHaveAttribute('aria-modal','true');
  await expect.poll(()=>page.evaluate(()=>document.activeElement?.closest('#sview')?.id)).toBe('sview');
  expect(await page.locator('#appshell').evaluate(element=>element.inert)).toBe(true);
  await page.locator('#sact').getByRole('button',{name:'message options'}).click();
  const scheduleOpener=page.locator('#sact').getByRole('menuitem',{name:'Schedule message'});
  await scheduleOpener.click();
  await expect(page.locator('#scheduleview')).toHaveAttribute('role','dialog');
  expect(await page.locator('#sview').evaluate(element=>element.inert)).toBe(true);
  await page.locator('#scheduleview').getByRole('button',{name:'back'}).click();
  await expect(page.locator('#scheduleview')).toBeHidden();
  await expect.poll(()=>page.evaluate(()=>document.activeElement?.closest('#sview')?.id)).toBe('sview');
  await page.locator('#sclose').click();await expect(session).toBeHidden();
  await expect(header).toBeFocused();
  // The live poll always rebuilds volatile card headers. That reconciliation
  // must not erase the focus which the closed dialog returned to the header.
  await page.evaluate(()=>render(last,true));await expect(header).toBeFocused();
  expect(await page.locator('#appshell').evaluate(element=>element.inert)).toBe(false);
});

test('all full-screen forms fit the visual viewport while editing', async ({ page },testInfo) => {
  test.skip(!testInfo.project.name.startsWith('mobile'),'mobile visual viewport contract');
  await reset(page,'base');
  await page.evaluate(()=>{globalThis.__fleetVisualViewportOverride={height:500,offsetTop:12};syncVisualViewport();});
  const assertGeometry=async selector=>{
    const box=await page.locator(selector).evaluate(element=>{const rect=element.getBoundingClientRect();return{top:rect.top,height:rect.height,bottom:rect.bottom};});
    expect(box.top).toBe(12);expect(box.height).toBe(500);expect(box.bottom).toBe(512);
  };
  await page.evaluate(()=>openSettings('sessions'));await expect(page.locator('#settingsview')).toBeVisible();
  await page.locator('#settings input').first().focus();await page.evaluate(()=>syncVisualViewport());await assertGeometry('#settingsview');
  await page.evaluate(()=>dismissOverlay());await expect(page.locator('#settingsview')).toBeHidden();
  await page.evaluate(()=>openSearchContext(901));await expect(page.locator('#searchview')).toBeVisible();await assertGeometry('#searchview');
  await page.evaluate(()=>dismissOverlay());await expect(page.locator('#searchview')).toBeHidden();
  await page.evaluate(()=>openHandoff('codex:thread-one','claude'));await expect(page.locator('#handoffpreview')).toBeVisible();
  await page.locator('#handoffpreview').focus();await page.evaluate(()=>syncVisualViewport());await assertGeometry('#handoffview');
  await page.evaluate(()=>dismissOverlay());await expect(page.locator('#handoffview')).toBeHidden();
  await page.evaluate(()=>openOutbox());await expect(page.locator('#outboxview')).toBeVisible();await assertGeometry('#outboxview');
  await page.evaluate(()=>dismissOverlay());await expect(page.locator('#outboxview')).toBeHidden();
  await page.evaluate(()=>openSchedule('codex:thread-one',null));await expect(page.locator('#scheduleview')).toBeVisible();
  await page.locator('#scheduleview textarea').focus();await page.evaluate(()=>syncVisualViewport());await assertGeometry('#scheduleview');
  await page.evaluate(()=>{delete globalThis.__fleetVisualViewportOverride;syncVisualViewport();});
});

test('notification policy controls every kind, warns on aggressive cadence, and nests cleanly in Settings', async ({ page }, testInfo) => {
  await reset(page, 'base');
  await goTo(page, 'settings');
  await expect(page.locator('.policyanswer')).toContainText('Push on');
  await expect(page.locator('.policyanswer')).toContainText('Last delivery: sent · Quick build finished');
  await expect(page.locator('.policyrule')).toHaveCount(12);
  const guide = page.locator('.policyguide');
  await expect(guide).toContainText('What these settings mean');
  await guide.locator('summary').click();
  await expect(guide).toHaveAttribute('open', '');
  await expect(guide).toContainText('Info');
  await expect(guide).toContainText('Warning');
  await expect(guide).toContainText('Critical');
  await expect(guide).toContainText('session mute');
  const question = page.locator('.policyrule[data-policy-kind="question"]');
  await question.locator('summary').click();
  await expect(question).toContainText('Once + reminder');
  await expect(question).toContainText('waiting for one answer from you');
  await expect(question.getByLabel('Minimum severity')).toHaveValue('info');
  await expect(question.getByLabel('Minimum severity').locator('option').nth(0)).toHaveText(
    'All events (Info, Warning, or Critical)');
  await expect(question).toContainText('does not change sound, color, or presentation');
  await expect(question.getByLabel('Show Question in Fleet')).toBeChecked();
  await expect(question.locator('summary')).toContainText('In app on');
  await question.getByLabel('Show Question in Fleet').uncheck();
  await expect.poll(async () => (await fixtureState(page)).actions.filter(action =>
    action.type==='notification_policy'&&action.kind==='question').at(-1)?.patch?.in_app_enabled).toBe(false);
  await expect(question.locator('summary')).toContainText('In app off');
  await expect(question.getByLabel('Web Push cadence')).toHaveValue('remind_once');
  await question.getByLabel('Apply this change to 1 active event').check();
  await question.getByLabel('Web Push cadence').selectOption('repeat');
  await expect.poll(async () => (await fixtureState(page)).actions.filter(action =>
    action.type==='notification_policy'&&action.kind==='question').at(-1)?.patch?.mode).toBe('repeat');
  await expect(page.locator('.policyguide')).toHaveAttribute('open', '');
  const beforeInvalidDuration = (await fixtureState(page)).actions.filter(action =>
    action.type==='notification_policy'&&action.kind==='question').length;
  await page.locator('#policy-question-repeat_interval_seconds-amount').evaluate(input => { input.value = '1'; });
  await page.locator('#policy-question-repeat_interval_seconds-unit').selectOption('1');
  await expect(question.locator('summary em')).toContainText('enter 1 minute to 7 days');
  expect((await fixtureState(page)).actions.filter(action =>
    action.type==='notification_policy'&&action.kind==='question')).toHaveLength(beforeInvalidDuration);
  const maximum = question.getByLabel('Maximum deliveries');
  await maximum.fill('20');
  await maximum.press('Tab');
  await expect(page.locator('#confirm')).toContainText('High notification cadence');
  await expect(page.locator('#confirm')).toContainText('20 pushes per day');
  await page.locator('#confirm').getByRole('button', {name:'use high cadence'}).click();
  await expect.poll(async () => (await fixtureState(page)).actions.filter(action =>
    action.type==='notification_policy'&&action.kind==='question').at(-1)?.patch?.max_deliveries).toBe(20);
  expect((await fixtureState(page)).actions.filter(action =>
    action.type==='notification_policy'&&action.kind==='question').at(-1)?.apply_current).toBe(true);

  await page.getByRole('checkbox', {name:/^Quiet hours/}).check();
  await expect(page.getByRole('textbox', {name:'Starts', exact:true})).toBeVisible();
  await expect(page.getByRole('textbox', {name:'Ends', exact:true})).toBeVisible();
  await page.screenshot({path:testInfo.outputPath('notification-policy-settings.png'),fullPage:true});
  await openSettingsSection(page, 'devices');
  await expect(page.locator('.pushsetup')).toContainText('Fleet app & Web Push');
  await page.goBack();
  await expect(page.locator('#settingsview')).toBeVisible();
  await expect(page).toHaveURL(/#settings\/notifications$/);
  await expect(page.locator('.policyanswer')).toBeVisible();
  await page.goBack();
  await expect(page.locator('#settingsview')).toBeHidden();
  if (testInfo.project.name.startsWith('mobile'))
    await expect(page.locator('#bottomnav')).toBeVisible();
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
  await openSettingsSection(page, 'devices');
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
  await page.request.post('/api/push/subscription', {data: {device_id: 'remote-phone',
    display_name: 'Travel phone', platform: 'iOS', permission_state: 'granted',
    subscription: {endpoint: 'https://fcm.googleapis.com/fcm/send/another-private',
      keys: {p256dh: 'another-p256dh', auth: 'another-auth'}}}});
  await page.evaluate(() => window.__fleetPush.loadPushState(true));
  let remote = page.locator('[data-push-device="remote-phone"]');
  await expect(remote).toBeVisible();
  const remoteName = remote.getByLabel('Rename Travel phone');
  await remoteName.fill('Pocket phone');
  await remoteName.press('Tab');
  await expect.poll(async () => (await fixtureState(page)).push_devices['remote-phone'].display_name)
    .toBe('Pocket phone');
  remote = page.locator('[data-push-device="remote-phone"]');
  await remote.getByRole('button', {name:'Test', exact:true}).click();
  await expect.poll(async () => (await fixtureState(page)).actions.some(action =>
    action.type==='push_test'&&action.device_id==='remote-phone')).toBe(true);
  remote = page.locator('[data-push-device="remote-phone"]');
  await remote.getByRole('button', {name:'Pause', exact:true}).click();
  await expect.poll(async () => (await fixtureState(page)).push_devices['remote-phone'].enabled)
    .toBe(false);
  remote = page.locator('[data-push-device="remote-phone"]');
  await remote.getByRole('button', {name:'Resume', exact:true}).click();
  await expect.poll(async () => (await fixtureState(page)).push_devices['remote-phone'].enabled)
    .toBe(true);
  remote = page.locator('[data-push-device="remote-phone"]');
  await remote.getByRole('button', {name:'Remove', exact:true}).click();
  await expect(page.locator('#confirm')).toContainText('Pocket phone');
  await page.locator('#confirm').getByRole('button', {name:'remove device'}).click();
  await expect.poll(async () => (await fixtureState(page)).push_devices['remote-phone']).toBeUndefined();
  await expect(page.locator('[data-push-device="remote-phone"]')).toHaveCount(0);
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
    'fcm.googleapis.com/fcm/send/private', 'another-p256dh', 'another-auth',
    'fcm.googleapis.com/fcm/send/another-private']) expect(retained).not.toContain(secret);
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
      action.type === 'send_message' && action.session_id === 'codex:thread-one' &&
      action.text === 'Send this when Fleet reconnects').length).toBe(1);
    await page.evaluate(() => tick());
    await page.waitForTimeout(2200);
    expect((await fixtureState(page)).actions.filter(action =>
      action.type === 'send_message' && action.session_id === 'codex:thread-one' &&
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
  await openSettingsSection(page, 'budgets');
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
  await page.getByRole('button', { name: 'Save budgets' }).click();
  await expect(page.locator('#setmsg')).toContainText('saved ✓');
  await expect.poll(async () => (await fixtureState(page)).budgets.length).toBe(2);
  await openSettingsSection(page, 'advanced');
  await page.getByLabel('Enable manual legacy tests').check();
  await expect.poll(async () => (await fixtureState(page)).settings.legacy_ntfy_enabled)
    .toBe(true);
  await page.getByRole('button', { name: 'Send legacy test' }).click();
  await expect(page.locator('#settings')).toContainText('Legacy test queued');
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
