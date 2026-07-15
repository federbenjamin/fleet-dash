const { test, expect } = require('@playwright/test');

async function reset(page, scenario = 'base') {
  await page.request.post('/test/reset', { data: { scenario } });
  await page.goto('/?token=abcdef123456');
  await expect(page.locator('[data-sid="codex:thread-one"]')).toBeVisible();
}

async function refresh(page) {
  await page.evaluate(() => tick());
}

async function fixtureState(page) {
  return (await page.request.get('/test/state')).json();
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

test('shared fleet, spawn controls, usage, files, and capability-aware cost', async ({ page }, testInfo) => {
  await reset(page);
  await expect(page.locator('[data-sid="claude-one"]')).toContainText('Claude parser fix');
  const codex = page.locator('[data-sid="codex:thread-one"]');
  await expect(codex).toContainText('Codex parity work');
  await expect(page.locator('#usage')).toContainText('Claude Code');
  await expect(page.locator('#usage')).toContainText('Codex CLI');
  await expect(page.locator('#usage')).toContainText('12k lifetime tokens');
  await expect(page.locator('#usage')).toContainText('weekly');
  await expect(page.locator('#usage')).not.toContainText('GPT-5.3-Codex-Spark');
  await expect(codex.locator('select.modesel')).toHaveCount(0);
  if (testInfo.project.name === 'desktop') {
    const terminal = codex.getByRole('button', { name: 'no terminal' });
    const pin = codex.getByRole('button', { name: 'pin session to top' });
    await expect(terminal).toBeDisabled();
    await expect(pin).toBeVisible();
    expect(await terminal.evaluate((el) => el.nextElementSibling === document.querySelector(
      '[data-sid="codex:thread-one"] .spin'))).toBe(true);
    expect(await pin.evaluate((el) => getComputedStyle(el).borderStyle)).toBe('solid');
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

  await reset(page, 'subagent');
  surfaces = await themeSurfaces();
  const running = page.locator('[data-sid="codex:thread-one"]');
  await expect.poll(async () => (await cardStyle(running)).background).toBe(surfaces.card2);
  expect(await running.locator('.shead').evaluate(el => getComputedStyle(el).backgroundColor))
    .toBe(surfaces.card2);
  await running.getByRole('button', { name: /more/ }).click();
  await expect(running.locator('.detail')).toBeVisible();
  expect(await running.locator('.detail').evaluate(el => getComputedStyle(el).backgroundColor))
    .toBe(surfaces.card);
  await page.screenshot({ path: testInfo.outputPath('active-card-contrast.png'), fullPage: true });

  await page.request.post('/test/reset', { data: { scenario: 'organization' } });
  await page.goto('/?token=abcdef123456');
  surfaces = await themeSurfaces();
  const headless = page.locator('#headless details');
  const dormant = page.locator('#dormant details');
  await headless.locator('summary').click();
  await dormant.locator('summary').click();
  expect((await cardStyle(headless.locator('[data-sid="codex:thread-one"]'))).background)
    .toBe(surfaces.card);
  expect((await cardStyle(dormant.locator('[data-sid="claude-dormant"]'))).background)
    .toBe(surfaces.card);
});

test('Codex mode, send, UI stop, and completed lifecycle', async ({ page }) => {
  await reset(page);
  const card = page.locator('[data-sid="codex:thread-one"]');
  await card.locator('.shead').click();
  await page.getByRole('button', { name: 'session actions' }).click();
  await page.getByRole('button', { name: 'Default', exact: true }).click();
  await expect.poll(async () => (await fixtureState(page)).sessions[1].collaboration_mode)
    .toBe('default');

  const input = page.locator('#sft-codex\\:thread-one');
  await input.fill('Run the deterministic check');
  await page.locator('#sact').getByRole('button', { name: 'send' }).click();
  await refresh(page);
  await page.getByRole('button', { name: 'session actions' }).click();
  await page.getByRole('menuitem', { name: /Stop turn/ }).click();
  await expect(page.locator('#confirm')).toContainText('Stop this turn?');
  await page.locator('#confirm').getByRole('button', { name: 'stop the turn' }).click();
  await refresh(page);
  await expect(card.locator('.chip')).toContainText('done');
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
  await page.getByRole('button', { name: 'session actions' }).click();
  await expect(page.getByRole('button', { name: 'Plan', exact: true })).toHaveCount(0);
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
  await expect(page.locator('#closed')).toContainText('session history (2)');
  await page.locator('#closed summary').click();
  await expect(page.locator('#closed')).toContainText('Codex parity work');
});

test('single, multi, free-text, dismiss, invalid, and stale questions', async ({ page }) => {
  await reset(page, 'single-question');
  await page.locator('[data-sid="codex:thread-one"] .qanswer').click();
  await expect(page.locator('#sact')).toContainText('How broad should the change be?');
  await page.locator('#sact').getByRole('button', { name: /Focused/ }).click();
  await expect.poll(async () => (await fixtureState(page)).actions.at(-1).type).toBe('option');

  await page.request.post('/test/reset', { data: { scenario: 'single-question' } });
  await page.reload();
  await page.locator('[data-sid="codex:thread-one"] .qanswer').click();
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
  await page.locator('[data-sid="codex:thread-one"] .qanswer').click();
  await page.locator('#sact').getByRole('button', { name: 'Desktop' }).click();
  await page.locator('#sact .mqarr').last().click();
  await page.locator('#sact').getByRole('button', { name: 'Full' }).click();
  await page.locator('#sact').getByRole('button', { name: 'submit all answers' }).click();
  await expect.poll(async () => (await fixtureState(page)).actions.at(-1).type).toBe('multiq');

  await page.request.post('/test/reset', { data: { scenario: 'single-question' } });
  await page.reload();
  await page.locator('[data-sid="codex:thread-one"] .qanswer').click();
  await page.locator('#sact .xbtn').click();
  await expect.poll(async () => (await fixtureState(page)).actions.at(-1).type).toBe('dismiss');
});

test('every approval decision and MCP single/multi-select elicitation', async ({ page }) => {
  for (const [label, choice] of [['allow', 'allow'], ['always allow', 'always'],
                                 ['deny', 'deny'], ['cancel', 'cancel']]) {
    await reset(page, 'approval');
    await page.locator('[data-sid="codex:thread-one"]').getByRole('button', { name: label,
      exact: true }).click();
    await expect.poll(async () => (await fixtureState(page)).actions.at(-1).choice)
      .toBe(choice);
  }

  await reset(page, 'elicitation');
  await page.locator('[data-sid="codex:thread-one"] .qanswer').click();
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
  await input.press('Enter');
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

test('closed, reconnect/takeover, stale, unavailable, and read-only states', async ({ page }, testInfo) => {
  await reset(page);
  await page.getByText(/session history/).click();
  await page.locator('#closedrows .expandbtn').click();
  await expect(page.locator('#sbody')).toContainText('Durable closed conversation');
  await page.locator('#sclose').click();

  await page.request.post('/test/reset', { data: { scenario: 'reopenable' } });
  await page.reload();
  const card = page.locator('[data-sid="codex:thread-one"]');
  await expect(page.locator('#sessions [data-sid="codex:thread-one"]')).toHaveCount(0);
  const headless = page.locator('#headless details');
  await expect(headless).not.toHaveAttribute('open', '');
  await expect(headless.getByText(/external Codex threads/)).toBeVisible();
  await headless.locator('summary').click();
  await expect(card).toBeVisible();
  await expect(card).toContainText('reopenable');
  await card.getByRole('button', { name: 'take over' }).click();
  await refresh(page);
  await expect(page.locator('#headless details')).toHaveCount(0);
  await expect(page.locator('#sessions [data-sid="codex:thread-one"]')).toBeVisible();
  await expect(card).toContainText('idle');

  await page.request.post('/test/reset', { data: { scenario: 'stale' } });
  await page.reload();
  await expect(page.locator('[data-sid="codex:thread-one"]')).toContainText('app-server exited');
  await page.screenshot({ path: testInfo.outputPath('stale-state.png'), fullPage: true });

  await page.request.post('/test/reset', { data: { scenario: 'provider-unavailable' } });
  await page.reload();
  await expect(page.locator('[data-sid="claude-one"]')).toBeVisible();
  await expect(page.locator('[data-sid="codex:thread-one"]')).toHaveCount(0);

  await page.context().clearCookies();
  await page.goto('/');
  await expect(page.locator('#notoken')).toBeVisible();
  const denied = await page.request.post('/api/act', { data: { type: 'ping' } });
  expect(denied.status()).toBe(403);
  page.__failures = [];
});

test('idle stays visible while headless, dormant, and history remain collapsed', async ({ page }, testInfo) => {
  await page.request.post('/test/reset', { data: { scenario: 'organization' } });
  await page.goto('/?token=abcdef123456');

  await expect(page.locator('#sessions [data-sid="claude-one"]')).toContainText('idle');
  await expect(page.locator('#sessions [data-sid="codex:thread-one"]')).toHaveCount(0);
  await expect(page.locator('#sessions [data-sid="claude-dormant"]')).toHaveCount(0);

  const headless = page.locator('#headless details');
  const dormant = page.locator('#dormant details');
  const history = page.locator('#closed details');
  await expect(headless).not.toHaveAttribute('open', '');
  await expect(dormant).not.toHaveAttribute('open', '');
  await expect(history).not.toHaveAttribute('open', '');
  await expect(headless.locator('[data-sid="codex:thread-one"]')).toBeHidden();
  await expect(dormant.locator('[data-sid="claude-dormant"]')).toBeHidden();
  await expect(history).toContainText('session history (1)');

  const ordered = await page.evaluate(() => {
    const headless = document.querySelector('#headless');
    const dormant = document.querySelector('#dormant');
    return Boolean(headless.compareDocumentPosition(dormant) & Node.DOCUMENT_POSITION_FOLLOWING);
  });
  expect(ordered).toBe(true);

  await headless.locator('summary').click();
  await expect(headless.locator('[data-sid="codex:thread-one"]')).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('organized-session-inventory.png'), fullPage: true });
});
