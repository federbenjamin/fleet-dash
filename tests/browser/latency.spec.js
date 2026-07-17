const { test, expect } = require('@playwright/test');

const SAMPLE_COUNT = 20;

async function reset(page, scenario = 'base') {
  await page.request.post('/test/reset', { data: { scenario } });
  await page.goto('/?token=abcdef123456');
  await expect(page.locator('#usagechip')).toBeVisible();
}

async function paintedSamples(page, flow, count = SAMPLE_COUNT) {
  return page.evaluate(async ({ flow, count }) => {
    const frame = () => new Promise(resolve => requestAnimationFrame(resolve));
    const values = [];
    for (let index = 0; index < count; index += 1) {
      await frame();
      const started = performance.now();
      if (flow === 'navigation') document.querySelector('[data-route="search"]:not([hidden])')?.click();
      else if (flow === 'notifications') document.querySelector('[data-route="notifications"]:not([hidden])')?.click();
      else if (flow === 'notification_detail') openNotification('evt-6-question');
      else if (flow === 'notification_action') snoozeNotification('evt-6-question', 'rev-6', 'quarter');
      else if (flow === 'now') document.querySelector('[data-now-filter="needs_you"]')?.click();
      else if (flow === 'session_chat') document.querySelector('[data-sid="claude-one"] .shead')?.click();
      else if (flow === 'native_requests') document.querySelector('[data-sid="claude-one"] .termbtn')?.click();
      else if (flow === 'session_lifecycle') document.querySelector('#newsess .newbtn')?.click();
      else if (flow === 'subagents') document.querySelector('[data-now-filter="subagents"]')?.click();
      else if (flow === 'search_history') document.querySelector('[data-route="history"]:not([hidden])')?.click();
      else if (flow === 'workstreams_repository') document.querySelector('[data-route="workstreams"]:not([hidden])')?.click();
      else if (flow === 'insights_usage') document.querySelector('#usagechip')?.click();
      else if (flow === 'settings') openSettings();
      else if (flow === 'outbox_handoff') document.querySelector('#outboxchip')?.click();
      else if (flow === 'file_markdown') document.querySelector('#sact .fchip')?.click();
      else if (flow === 'pinning') document.querySelector('[data-sid="claude-one"] .spin')?.click();
      else if (flow === 'mobile_pin_hold') {
        const target = document.querySelector('[data-sid="claude-one"] .shead');
        sessionPressStart('claude-one', target);
      }
      values.push(performance.now() - started);
      // The handler has committed the visible pressed/open/loading state. RAF
      // itself is power-throttled to ~100 ms in some headless runs, so product
      // render duration is gated separately from this input-to-DOM measurement.
      await frame();

      if (flow === 'native_requests') {
        while (terminalActions.get('claude-one')?.busy) {
          await new Promise(resolve => setTimeout(resolve, 2));
        }
      }
      if (flow === 'pinning') {
        while (pinActions.get('claude-one')?.busy) {
          await new Promise(resolve => setTimeout(resolve, 2));
        }
      }
      if (flow === 'notification_action') {
        while (notificationActionState.busy) {
          await new Promise(resolve => setTimeout(resolve, 2));
        }
      }

      if (flow === 'navigation' || flow === 'notifications' || flow === 'search_history' || flow === 'workstreams_repository') navigateTo('now', false);
      else if (flow === 'notification_detail') {
        notificationDetailId = null; notificationDetail = null; notificationDetailError = '';
        navigateTo('now', false);
      }
      else if (flow === 'now' || flow === 'subagents') setNowState('all');
      else if (flow === 'session_chat') closeSession();
      else if (flow === 'native_requests') {
        terminalActions.delete('claude-one');
        render(last, true);
      } else if (flow === 'session_lifecycle') { newOpen = false; render(last, true); }
      else if (flow === 'insights_usage') closeUsage();
      else if (flow === 'settings') closeSettings();
      else if (flow === 'outbox_handoff') closeOutbox();
      else if (flow === 'file_markdown') { closeViewer(); openSession('codex:thread-one'); }
      else if (flow === 'mobile_pin_hold') sessionPressEnd();
    }
    return values;
  }, { flow, count });
}

function p95(values) {
  const ordered = [...values].sort((a, b) => a - b);
  return ordered[Math.min(ordered.length - 1, Math.round((ordered.length - 1) * .95))];
}

test('named interaction inventory meets first-feedback and local completion budgets', async ({ page }) => {
  // The inventory includes 20 serialized native/persistence action samples.
  // Mobile headless RAF throttling can make the complete matrix exceed two
  // minutes even while every measured input remains inside its own budget.
  test.setTimeout(240_000);
  await reset(page);

  const flows = [
    'navigation', 'now', 'session_chat', 'native_requests', 'session_lifecycle',
    'notifications', 'notification_detail', 'notification_action',
    'subagents', 'search_history', 'workstreams_repository', 'insights_usage',
    'settings', 'outbox_handoff', 'pinning', 'mobile_pin_hold',
  ];
  const results = {};
  for (const flow of flows) {
    const samples = await paintedSamples(page, flow);
    results[flow] = { samples: samples.length, p95: p95(samples) };
    expect(samples, `${flow} sample count`).toHaveLength(SAMPLE_COUNT);
    expect(results[flow].p95, `${flow} first-feedback DOM commit p95`).toBeLessThan(100);
  }

  await page.evaluate(() => openSession('codex:thread-one'));
  await expect(page.locator('#sact .fchip')).toBeVisible();
  const fileSamples = await paintedSamples(page, 'file_markdown');
  results.file_markdown = { samples: fileSamples.length, p95: p95(fileSamples) };
  expect(fileSamples).toHaveLength(SAMPLE_COUNT);
  expect(results.file_markdown.p95).toBeLessThan(100);

  const pollSamples = await page.evaluate(async count => {
    const values = [];
    for (let index = 0; index < count; index += 1) {
      const started = performance.now();
      await tick();
      values.push(performance.now() - started);
    }
    return values;
  }, SAMPLE_COUNT);
  results.poll_recovery = { samples: pollSamples.length, p95: p95(pollSamples) };
  expect(pollSamples).toHaveLength(SAMPLE_COUNT);
  expect(results.poll_recovery.p95, 'poll/recovery Fleet-local p95').toBeLessThan(250);

  const native = await page.evaluate(() => window.__fleetPerf.summary().native_focus_ms);
  expect(native.count, 'native focus sample count').toBeGreaterThanOrEqual(SAMPLE_COUNT);
  expect(native.p95, 'fixture native completion p95').toBeLessThan(250);
  const notificationCompletion = await page.evaluate(
    () => window.__fleetPerf.summary().notification_action_completion_ms);
  expect(notificationCompletion.count, 'notification completion sample count')
    .toBeGreaterThanOrEqual(SAMPLE_COUNT);
  expect(notificationCompletion.p95, 'notification action completion p95').toBeLessThan(250);
  await test.info().attach('named-latency-results', {
    body: Buffer.from(JSON.stringify({ results, native, notificationCompletion }, null, 2)),
    contentType: 'application/json',
  });
});
