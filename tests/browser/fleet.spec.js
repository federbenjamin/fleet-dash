const { test, expect } = require('@playwright/test');

async function reset(page, scenario = 'base') {
  await page.request.post('/test/reset', { data: { scenario } });
  await page.goto('/?token=abcdef123456');
  await expect(page.locator('#totals')).toBeVisible();
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
  await expect(page.locator('#totals > span')).toHaveText([
    '0 need you', '0 working', '2 available · 0 subagents']);
  await expect(page.locator('#totals .sep')).toHaveCount(1);
  await expect(page.locator('#totals > span').first().locator('b')).not.toHaveAttribute('style');
  await expect(page.locator('#totals')).not.toContainText('history');
  await expect(page.locator('#totals')).not.toContainText('new');
  await expect(page.locator('#totals')).not.toContainText('running');
  await expect(page.locator('[data-sid="claude-one"]')).toContainText('Claude parser fix');
  const codex = page.locator('[data-sid="codex:thread-one"]');
  await expect(codex).toContainText('Codex parity work');
  await expect(page.locator('#usage')).toContainText('Claude Code');
  await expect(page.locator('#usage')).toContainText('Codex CLI');
  await expect(page.locator('#usage')).toContainText('12k lifetime tokens');
  const usageHeads = page.locator('#usage .uhead');
  await expect(usageHeads.nth(0).locator('.useg')).toHaveText([
    '·claude@example.com', '·active', '·59.59B local lifetime tokens']);
  await expect(usageHeads.nth(1).locator('.useg')).toHaveText([
    '·second@example.com']);
  await expect(usageHeads.nth(2).locator('.useg')).toHaveText([
    '·codex@example.com', '·pro', '·12k lifetime tokens']);
  await expect(page.locator('#usage .uaccount')).toHaveCount(2);
  await expect(page.locator('#usage')).toContainText('weekly');
  await expect(page.locator('#usage')).not.toContainText('GPT-5.3-Codex-Spark');
  await expect(codex.locator('select.modesel')).toHaveCount(0);
  if (testInfo.project.name === 'desktop') {
    const terminal = codex.getByRole('button', { name: 'attach' });
    const pin = codex.getByRole('button', { name: 'pin session to top' });
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

  await page.locator('#gear').click();
  const settingsPage = page.locator('#settingsview');
  await expect(settingsPage).toBeVisible();
  await expect(settingsPage.locator('#settitle')).toHaveText('Settings');
  await expect(settingsPage.getByRole('button', { name: 'back to fleet' })).toBeVisible();
  expect(await settingsPage.evaluate(el => getComputedStyle(el).position)).toBe('fixed');
  await page.screenshot({ path: testInfo.outputPath('settings-page.png'), fullPage: true });
  await settingsPage.getByRole('button', { name: 'back to fleet' }).click();
  await expect(settingsPage).toBeHidden();

  await page.locator('#gear').click();
  await page.evaluate(() => history.back());
  await expect(settingsPage).toBeHidden();
  await expect(page.locator('[data-sid="codex:thread-one"]')).toBeVisible();

  await page.locator('#gear').click();
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
  const history = page.locator('#history details');
  await history.locator('summary').click();
  await expect(history.locator('[data-history-sid="codex:thread-one"]')).toContainText('External');
  await expect(history.locator('[data-history-sid="claude-dormant"]')).toContainText('Inactive');
  await expect(page.locator('#headless')).toHaveCount(0);
  await expect(page.locator('#dormant')).toHaveCount(0);
});

test('Codex mode, send, UI stop, and completed lifecycle', async ({ page }) => {
  await reset(page);
  const card = page.locator('[data-sid="codex:thread-one"]');
  await card.locator('.shead').click();
  const attach = page.locator('#sctrl > .termbtn');
  await expect(attach).toHaveText('attach');
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
  await expect(page.locator('#sctrl > .termbtn')).toHaveText('attach');
  await expect(page.locator('#sctrl > .termbtn')).toBeEnabled();
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
  await expect(openTerminal).toHaveText('open');
  await expect(openTerminal).toBeEnabled();
  expect(await openTerminal.evaluate(el => el.nextElementSibling.classList.contains('ovwrap'))).toBe(true);
  await openTerminal.click();
  await expect.poll(async () => (await fixtureState(page)).actions.at(-1).type).toBe('focus');
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
  await expect(page.locator('#history')).toContainText('Session history (2)');
  await page.locator('#history summary').click();
  await expect(page.locator('#history')).toContainText('Codex parity work');
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

test('messages and question answers render optimistically and recover from failure', async ({ page }) => {
  await reset(page);
  await page.locator('[data-sid="codex:thread-one"] .shead').click();
  const input = page.locator('#sft-codex\\:thread-one');
  await input.fill('Ship the optimistic message');
  await input.press('Enter');
  const sending = page.locator('#sbody .optimistic').filter({ hasText: 'Ship the optimistic message' });
  await expect(sending).toBeVisible();
  await expect(sending.getByLabel('sending')).toBeVisible();
  await page.request.post('/test/confirm', { data: { session_id: 'codex:thread-one',
    text: 'Ship the optimistic message' } });
  await refresh(page);
  await expect(page.locator('#sbody .optimistic')).toHaveCount(0);
  await expect(page.locator('#sbody .cmsg.user').filter({
    hasText: 'Ship the optimistic message' })).toBeVisible();

  await page.request.post('/test/reset', { data: { scenario: 'single-question' } });
  await page.reload();
  await page.locator('[data-sid="codex:thread-one"] .qanswer').click();
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
  await page.locator('[data-sid="codex:thread-one"] .qanswer').click();
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
  await timeoutInput.press('Enter');
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
  await failedInput.press('Enter');
  const failed = page.locator('#sbody .optimistic').filter({ hasText: 'Restore this message' });
  const restore = failed.getByRole('button', { name: 'send failed; restore message' });
  await expect(restore).toBeVisible();
  await restore.click();
  await expect(page.locator('#sbody .optimistic')).toHaveCount(0);
  await expect(failedInput).toHaveValue('Restore this message');
});

test('fleet cards show submitting, submitted, and failed quick-response feedback', async ({ page }) => {
  await reset(page, 'claude-question-slow');
  let card = page.locator('[data-sid="claude-one"]');
  await card.locator('.qanswer').click();
  await page.locator('#sact').getByRole('button', { name: /Focused/ }).click();
  await expect(page.locator('#sbody .optimistic').getByLabel('sending')).toBeVisible();
  await page.locator('#sclose').click();
  await expect(page.locator('#sview')).toBeHidden();
  let feedback = card.locator('.quickfeedback');
  await expect(feedback).toContainText('Submitting');
  await expect(feedback).toContainText('Scope: Focused');
  await expect(feedback.getByLabel('sending quick response')).toBeVisible();
  await expect(feedback).toContainText('Submitted', { timeout: 2_000 });
  await expect(feedback.getByLabel('response submitted')).toBeVisible();

  await page.request.post('/test/confirm', { data: { session_id: 'claude-one',
    kind: 'answer', answers: [{ header: 'Scope', q: 'How broad?', a: 'Focused' }] } });
  await refresh(page);
  await expect(card.locator('.quickfeedback')).toHaveCount(0);

  await reset(page, 'claude-question-failure');
  card = page.locator('[data-sid="claude-one"]');
  await card.locator('.qanswer').click();
  await page.locator('#sact').getByRole('button', { name: /Focused/ }).click();
  await page.locator('#sclose').click();
  feedback = card.locator('.quickfeedback');
  await expect(feedback).toContainText('Failed');
  const restore = feedback.getByRole('button', { name: 'submission failed; restore response' });
  await expect(restore).toBeVisible();
  await restore.click();
  await expect(card.locator('.quickfeedback')).toHaveCount(0);
  await expect(card.locator('.qanswer')).toBeVisible();

  await reset(page, 'approval-slow');
  card = page.locator('[data-sid="codex:thread-one"]');
  await card.getByRole('button', { name: 'allow', exact: true }).click();
  feedback = card.locator('.quickfeedback');
  await expect(feedback).toContainText('Submitting');
  await expect(feedback).toContainText('Allow permission');
  await expect(feedback.getByLabel('sending quick response')).toBeVisible();
  await expect(feedback).toContainText('Submitted', { timeout: 2_000 });
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

test('closed, external view-only, stale, unavailable, and read-only states', async ({ page }, testInfo) => {
  await reset(page);
  await page.getByText(/Session history/).click();
  await page.locator('[data-history-sid="codex:closed"]').getByRole('button', { name: 'View' }).click();
  await expect(page.locator('#sbody')).toContainText('Durable closed conversation');
  await page.locator('#sclose').click();

  await page.request.post('/test/reset', { data: { scenario: 'reopenable' } });
  await page.reload();
  await expect(page.locator('#sessions [data-sid="codex:thread-one"]')).toHaveCount(0);
  const history = page.locator('#history details');
  await expect(history).not.toHaveAttribute('open', '');
  await history.locator('summary').click();
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
  await expect(page.locator('[data-sid="codex:thread-one"]')).toContainText('app-server exited');
  await page.screenshot({ path: testInfo.outputPath('stale-state.png'), fullPage: true });

  await page.request.post('/test/reset', { data: { scenario: 'provider-unavailable' } });
  await page.reload();
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

test('backfilled Claude history supports both view and reopen', async ({ page }) => {
  await reset(page, 'claude-archive');
  await page.locator('#history summary').click();
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
  await page.locator('#history summary').click();
  const providerFilters = page.locator('.filterline').filter({ hasText: 'Provider' });
  await providerFilters.getByRole('button', { name: 'Claude' }).click();
  await expect(page.locator('[data-history-sid]')).toHaveCount(100);
  const firstMore = page.getByRole('button', { name: 'show 100 more of 205' });
  await expect(firstMore).toBeVisible();
  await firstMore.click();
  await expect(page.locator('[data-history-sid]')).toHaveCount(200);
  await page.getByRole('button', { name: 'show 5 more of 205' }).click();
  await expect(page.locator('[data-history-sid]')).toHaveCount(205);
  await expect(page.getByText('Archived Claude session 204')).toBeVisible();
});

test('available stays visible while every inactive lifecycle shares one collapsed history', async ({ page }, testInfo) => {
  await page.request.post('/test/reset', { data: { scenario: 'organization' } });
  await page.goto('/?token=abcdef123456');

  await expect(page.locator('#sessions [data-sid="claude-one"]')).toContainText('Available');
  await expect(page.locator('#sessions [data-sid="codex:thread-one"]')).toHaveCount(0);
  await expect(page.locator('#sessions [data-sid="claude-dormant"]')).toHaveCount(0);

  const history = page.locator('#history details');
  await expect(history).not.toHaveAttribute('open', '');
  await expect(history).toContainText('Session history (3)');
  await expect(page.locator('[data-history-sid="codex:thread-one"]')).toBeHidden();
  await history.locator('summary').click();
  await expect(page.locator('[data-history-sid="codex:thread-one"]')).toContainText('External');
  await expect(page.locator('[data-history-sid="claude-dormant"]')).toContainText('Inactive');
  await expect(page.locator('[data-history-sid="codex:closed"]')).toContainText('Closed');
  await page.screenshot({ path: testInfo.outputPath('organized-session-inventory.png'), fullPage: true });
});

test('action queue separates requests, work, availability, and unread responses', async ({ page }, testInfo) => {
  await reset(page, 'single-question');
  const question = page.locator('[data-sid="codex:thread-one"]');
  await expect(page.locator('#needs')).toContainText('Needs you · 1');
  await expect(question.locator('.chip')).toHaveText('Question waiting');
  await expect(question.getByRole('button', { name: 'Respond' })).toBeVisible();
  await expect(page.locator('#usage .uprovider')).toHaveCount(2);
  await page.screenshot({ path: testInfo.outputPath('needs-you-queue.png'), fullPage: true });

  await reset(page, 'subagent');
  const working = page.locator('[data-sid="codex:thread-one"]');
  await expect(page.locator('#working')).toContainText('Working · 1');
  await expect(working.locator('.chip')).toHaveText('Working');
  await expect(working.getByRole('button', { name: 'Open', exact: true })).toBeVisible();
  await expect(page.locator('#usage .uprovider')).toHaveCount(2);
  await page.screenshot({ path: testInfo.outputPath('working-queue.png'), fullPage: true });

  await reset(page, 'reply-requested');
  const reply = page.locator('[data-sid="codex:thread-one"]');
  await expect(page.locator('#needs')).toContainText('Needs you · 1');
  await expect(reply.locator('.chip')).toHaveText('Reply requested');
  await expect(reply).toContainText('Which organization should we use?');
  await reply.getByRole('button', { name: 'mark available' }).click();
  await expect.poll(async () => (await fixtureState(page)).reply_available['codex:thread-one'])
    .toBe('reply:1');
  await refresh(page);
  await expect(page.locator('#sessions [data-sid="codex:thread-one"] .chip')).toHaveText('Available');

  await reset(page, 'new-response');
  const fresh = page.locator('[data-sid="codex:thread-one"]');
  await expect(fresh.locator('.newbadge')).toHaveText('new');
  await fresh.getByRole('button', { name: 'Continue' }).click();
  await expect.poll(async () => (await fixtureState(page)).read_sessions['codex:thread-one'])
    .toBe('response:1');
  await page.locator('#sclose').click();
  await page.reload();
  await expect(page.locator('[data-sid="codex:thread-one"] .newbadge')).toHaveCount(0);
});

test('pins persist and relocate sessions above the needs-you queue', async ({ page }) => {
  await reset(page, 'single-question');
  await page.evaluate(() => toggleSessionPin('codex:thread-one'));
  await expect.poll(async () => (await fixtureState(page)).settings.pinned_sessions)
    .toContain('codex:thread-one');
  await expect(page.locator('#pinned [data-sid="codex:thread-one"]')).toBeVisible();
  await expect(page.locator('#needs [data-sid="codex:thread-one"]')).toHaveCount(0);
  expect(await page.evaluate(() => Boolean(document.querySelector('#pinned')
    .compareDocumentPosition(document.querySelector('#needs')) & Node.DOCUMENT_POSITION_FOLLOWING)))
    .toBe(true);

  await page.reload();
  await expect(page.locator('#pinned [data-sid="codex:thread-one"]')).toBeVisible();
  await page.evaluate(() => toggleSessionPin('codex:thread-one'));
  await expect.poll(async () => (await fixtureState(page)).settings.pinned_sessions)
    .not.toContain('codex:thread-one');
});

test('session history text and access/provider chips filter one flat list', async ({ page }) => {
  await reset(page, 'organization');
  const history = page.locator('#history details');
  await history.locator('summary').click();
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
