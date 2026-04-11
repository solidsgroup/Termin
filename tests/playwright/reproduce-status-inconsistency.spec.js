const { test, expect } = require('@playwright/test');
const { createStepRecorder } = require('./test-helpers');

async function fetchSeedState(request) {
  const reset = await request.post('/_e2e/reset');
  expect(reset.ok()).toBeTruthy();
  const response = await request.get('/_e2e/state');
  expect(response.ok()).toBeTruthy();
  return response.json();
}

async function login(page, email, password) {
  await page.goto('/login');
  await page.locator('#login-email').fill(email);
  await page.locator('#login-password').fill(password);
  await page.locator('.auth-submit').click();
  await page.waitForURL('**/dashboard');
}

async function patchTask(page, taskId, payload) {
  const result = await page.evaluate(async ({ taskId, payload }) => {
    const response = await fetch(`/api/tasks/${taskId}`, {
      method: 'PATCH',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    let data = null;
    try { data = await response.json(); } catch (_) { data = null; }
    return { ok: response.ok, status: response.status, data };
  }, { taskId, payload });
  expect(result.ok).toBeTruthy();
  return result.data;
}

async function collectTaskStatusMap(page) {
  await page.waitForSelector('[data-tree-project-board]');
  await page.waitForTimeout(1000);
  const rows = await page.$$('[data-task-row-id]');
  const map = {};
  for (const row of rows) {
    const id = await row.getAttribute('data-task-row-id');
    const status = await row.getAttribute('data-status-state');
    const statusCell = await row.$('[data-task-status-host] [data-status-state], [data-status-cell][data-status-state]');
    const statusText = statusCell ? (await statusCell.textContent()).trim() : '';
    map[id] = { status: status || null, text: statusText };
  }
  return map;
}

test('e2e: tree status consistency between two users (repro)', async ({ browser, request }, testInfo) => {
  const steps = createStepRecorder(testInfo);
  await steps.tags(['socket', 'tree', 'status', 'repro']);
  testInfo.slow();

  const state = await fetchSeedState(request);

  const ownerContext = await browser.newContext();
  const memberContext = await browser.newContext();
  const ownerPage = await ownerContext.newPage();
  const memberPage = await memberContext.newPage();

  // Login both users using seeded credentials
  await login(ownerPage, state.owner.email, state.owner.password);
  await login(memberPage, state.member.email, state.member.password);

  // Select a project and task seeded for this scenario
  const projectId = (state.direct_project && state.direct_project.id) || state.project.id;
  const taskId = (state.direct_task && state.direct_task.id) || state.task.id;

  // Owner primes a cached snapshot by opening the tree once
  await ownerPage.goto(`/tree/project/${projectId}`);
  await steps.step('Owner opens tree to prime cached snapshot', ownerPage);
  await expect(ownerPage.locator(`[data-task-row-id="${taskId}"]`)).toHaveAttribute('data-status-state', 'open');

  // Owner navigates away to leave the cached snapshot in place
  await ownerPage.goto('/dashboard');
  await steps.step('Owner returns to dashboard leaving cached snapshot', ownerPage);

  // Member updates the task while owner is off-tree
  await memberPage.goto(`/tree/project/${projectId}`);
  await patchTask(memberPage, taskId, { status: 'critical' });
  await steps.step('Member marks task Critical while owner is off-tree', memberPage);

  // Simulate slow snapshot response so owner may apply stale cache
  await ownerPage.route(`**/api/projects/${projectId}/tree_snapshot?**`, async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 4000));
    await route.continue();
  });

  // Both reopen the tree view
  await ownerPage.goto(`/tree/project/${projectId}`);
  await memberPage.goto(`/tree/project/${projectId}`);
  await steps.multiStep('Both sessions open the tree view to compare statuses', [
    { name: 'owner', page: ownerPage },
    { name: 'member', page: memberPage },
  ]);

  // Collect and compare status maps
  const ownerMap = await collectTaskStatusMap(ownerPage);
  const memberMap = await collectTaskStatusMap(memberPage);

  if (JSON.stringify(ownerMap) !== JSON.stringify(memberMap)) {
    await testInfo.attach('owner-tree-screenshot', { body: await ownerPage.screenshot({ fullPage: true }), contentType: 'image/png' });
    await testInfo.attach('member-tree-screenshot', { body: await memberPage.screenshot({ fullPage: true }), contentType: 'image/png' });
    await testInfo.attach('owner-map.json', { body: Buffer.from(JSON.stringify(ownerMap, null, 2), 'utf8'), contentType: 'application/json' });
    await testInfo.attach('member-map.json', { body: Buffer.from(JSON.stringify(memberMap, null, 2), 'utf8'), contentType: 'application/json' });
  }

  expect(ownerMap).toEqual(memberMap);

  await ownerContext.close();
  await memberContext.close();
});
