const { test, expect } = require('@playwright/test');
const { createStepRecorder, captureFailureSnapshot } = require('./test-helpers');

function isoDateWithOffset(days) {
  const date = new Date();
  date.setHours(0, 0, 0, 0);
  date.setDate(date.getDate() + days);
  return date.toISOString().slice(0, 10);
}

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
    try {
      data = await response.json();
    } catch (error) {
      data = null;
    }
    return { ok: response.ok, status: response.status, data };
  }, { taskId, payload });
  expect(result.ok).toBeTruthy();
  return result.data;
}

async function deleteAssignment(page, assignmentId) {
  const result = await page.evaluate(async (assignmentIdArg) => {
    const response = await fetch(`/api/assignments/${assignmentIdArg}`, {
      method: 'DELETE',
      credentials: 'same-origin',
    });
    return { ok: response.ok, status: response.status };
  }, assignmentId);
  expect(result.ok).toBeTruthy();
}

async function annotateTaskStatusView(page, taskId, label) {
  await page.locator(`[data-task-row-id="${taskId}"]`).scrollIntoViewIfNeeded();
  await page.evaluate(({ taskId, label }) => {
    document.querySelectorAll('[data-playwright-task-status-debug]').forEach((node) => node.remove());
    var row = document.querySelector(`[data-task-row-id="${taskId}"]`);
    if (!row) return;
    row.style.outline = '3px solid #ff8c42';
    row.style.outlineOffset = '2px';
    row.style.background = 'rgba(255, 140, 66, 0.06)';
    var statusNode = row.querySelector('[data-task-status-host] [data-status-state], [data-status-cell][data-status-state]');
    var debug = document.createElement('div');
    debug.setAttribute('data-playwright-task-status-debug', '1');
    debug.style.position = 'fixed';
    debug.style.right = '20px';
    debug.style.bottom = '20px';
    debug.style.zIndex = '99999';
    debug.style.padding = '12px 14px';
    debug.style.borderRadius = '14px';
    debug.style.background = 'rgba(15, 23, 42, 0.94)';
    debug.style.color = '#fff';
    debug.style.font = '600 14px/1.4 system-ui, sans-serif';
    debug.style.boxShadow = '0 18px 40px rgba(15, 23, 42, 0.28)';
    debug.innerHTML =
      '<div style="font-size:12px;opacity:.72;text-transform:uppercase;letter-spacing:.08em;">' + label + '</div>' +
      '<div style="margin-top:4px;">Task row status: ' + (row.getAttribute('data-status-state') || '(none)') + '</div>' +
      '<div>Status cell: ' + (statusNode ? statusNode.textContent.trim() : '(missing)') + '</div>' +
      '<div>Status cell state: ' + (statusNode ? (statusNode.getAttribute('data-status-state') || '(none)') : '(missing)') + '</div>';
    document.body.appendChild(debug);
  }, { taskId, label });
}

async function annotateLocator(page, selector, label, lines = []) {
  const target = page.locator(selector).first();
  await expect(target).toHaveCount(1);
  try {
    await target.scrollIntoViewIfNeeded();
  } catch (_error) {
    await page.waitForTimeout(150);
    const retry = page.locator(selector).first();
    await expect(retry).toHaveCount(1);
    await retry.scrollIntoViewIfNeeded();
  }
  await page.evaluate(({ selector, label, lines }) => {
    document.querySelectorAll('[data-playwright-annotation-overlay]').forEach((node) => node.remove());
    document.querySelectorAll('[data-playwright-annotation-highlight]').forEach((node) => {
      node.removeAttribute('data-playwright-annotation-highlight');
      node.style.outline = '';
      node.style.outlineOffset = '';
      node.style.background = '';
    });
    const target = document.querySelector(selector);
    if (!target) return;
    target.setAttribute('data-playwright-annotation-highlight', '1');
    target.style.outline = '3px solid #2f6fed';
    target.style.outlineOffset = '3px';
    target.style.background = 'rgba(47, 111, 237, 0.06)';
    const overlay = document.createElement('div');
    overlay.setAttribute('data-playwright-annotation-overlay', '1');
    overlay.style.position = 'fixed';
    overlay.style.right = '20px';
    overlay.style.bottom = '20px';
    overlay.style.zIndex = '99999';
    overlay.style.maxWidth = 'min(560px, calc(100vw - 40px))';
    overlay.style.padding = '12px 14px';
    overlay.style.borderRadius = '14px';
    overlay.style.background = 'rgba(15, 23, 42, 0.94)';
    overlay.style.color = '#fff';
    overlay.style.font = '600 14px/1.4 system-ui, sans-serif';
    overlay.style.boxShadow = '0 18px 40px rgba(15, 23, 42, 0.28)';
    overlay.innerHTML = [
      '<div style="font-size:12px;opacity:.72;text-transform:uppercase;letter-spacing:.08em;">Snapshot Focus</div>',
      '<div style="margin-top:4px;">' + label + '</div>',
      ...lines.map((line) => '<div style="margin-top:4px;opacity:.92;">' + line + '</div>'),
    ].join('');
    document.body.appendChild(overlay);
  }, { selector, label, lines });
}

async function focusTreeTask(page, taskId, label, extraLines = []) {
  const selector = `[data-task-row-id="${taskId}"]`;
  const row = page.locator(selector);
  const status = await row.getAttribute('data-status-state');
  const text = ((await row.textContent()) || '').trim().replace(/\s+/g, ' ').slice(0, 180);
  const assignmentLabels = await row.locator('.assignments .badge-label').allTextContents();
  await annotateLocator(page, selector, label, [
    `Row status: ${status || '(none)'}`,
    `Assignees: ${assignmentLabels.length ? assignmentLabels.join(', ') : '(none)'}`,
    `Text: ${text}`,
    ...extraLines,
  ]);
}

async function expectTreeAssignmentsWithFailure(page, testInfo, taskId, expectedLabels, label, note = '') {
  const selector = `[data-task-row-id="${taskId}"] .assignments`;
  const locator = page.locator(selector).first();
  for (const expected of expectedLabels) {
    try {
      await expect(locator).toContainText(expected);
    } catch (error) {
      await captureFailureSnapshot(page, testInfo, label, {
        selector,
        expected: `contains assignee "${expected}"`,
        actual: (((await locator.textContent()) || '').trim().replace(/\s+/g, ' ').slice(0, 260)),
        note,
      });
      throw error;
    }
  }
}

async function setTreeTaskSingleStatus(page, taskId, nextStatus) {
  const statusCell = page.locator(`[data-task-row-id="${taskId}"] [data-task-status-host="${taskId}"] [data-field="status"]`).first();
  await expect(statusCell).toHaveCount(1);
  await statusCell.scrollIntoViewIfNeeded();
  await statusCell.click();
  await expect(page.locator('#status-menu')).toBeVisible();
  await page.locator(`#status-menu [data-status-option="${nextStatus}"]`).click();
}

async function waitForTreeTaskUiToSettle(page, taskId, ms = 1400) {
  await expect(page.locator(`[data-task-row-id="${taskId}"]`)).toHaveCount(1);
  await page.waitForTimeout(ms);
}

async function waitForTreeProjectReady(page, projectId, taskId) {
  const board = page.locator(`[data-tree-project-board="${projectId}"]`).first();
  await expect(board).toHaveCount(1);
  if (taskId != null) {
    await expect(page.locator(`[data-task-row-id="${taskId}"]`)).toHaveCount(1);
  }
}

async function focusTodoTask(page, taskId, label, extraLines = []) {
  const selector = `.todo-item[data-task-id="${taskId}"]`;
  const item = page.locator(selector);
  const status = await item.getAttribute('data-status-state');
  const text = ((await item.textContent()) || '').trim().replace(/\s+/g, ' ').slice(0, 180);
  await annotateLocator(page, selector, label, [
    `Item status: ${status || '(none)'}`,
    `Text: ${text}`,
    ...extraLines,
  ]);
}

async function focusActivity(page, selector, label) {
  const text = ((await page.locator(selector).first().textContent()) || '').trim().replace(/\s+/g, ' ').slice(0, 180);
  await annotateLocator(page, selector, label, [`Text: ${text}`]);
}

async function expectAttributeWithFailure(page, testInfo, selector, name, expected, label, note = '') {
  const locator = page.locator(selector).first();
  try {
    await expect(locator).toHaveAttribute(name, expected);
  } catch (error) {
    await captureFailureSnapshot(page, testInfo, label, {
      selector,
      expected: `${name} = ${expected}`,
      actual: await locator.getAttribute(name),
      note,
    });
    throw error;
  }
}

async function expectContainsTextWithFailure(page, testInfo, selector, expected, label, note = '') {
  const locator = page.locator(selector).first();
  try {
    await expect(locator).toContainText(expected);
  } catch (error) {
    await captureFailureSnapshot(page, testInfo, label, {
      selector,
      expected: `contains text "${expected}"`,
      actual: (((await locator.textContent()) || '').trim().replace(/\s+/g, ' ').slice(0, 260)),
      note,
    });
    throw error;
  }
}

test.describe('dashboard and realtime flows', () => {
  test('dashboard stays on dashboard after refresh', async ({ page, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['dashboard', 'routing', 'refresh']);
    const state = await fetchSeedState(request);
    await page.goto('/login');
    await steps.step('Open the app login page.', page);
    await login(page, state.owner.email, state.owner.password);
    await page.waitForURL('**/dashboard');
    await steps.step('Sign in as owner@example.com with password123 and wait for the dashboard to load.', page);
    await page.reload();
    await page.waitForURL('**/dashboard');
    await expect(page.locator('.dashboard-home-title')).toContainText('Owner');
    await steps.step('Refresh the page and verify the URL stays on /dashboard with the Owner greeting visible.', page);
  });

  test('tree updates live when another user changes task title', async ({ browser, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['socket', 'tree', 'title']);
    const state = await fetchSeedState(request);
    const ownerContext = await browser.newContext();
    const memberContext = await browser.newContext();
    const ownerPage = await ownerContext.newPage();
    const memberPage = await memberContext.newPage();

    await login(ownerPage, state.owner.email, state.owner.password);
    await login(memberPage, state.member.email, state.member.password);
    await steps.multiStep('Open one browser as owner@example.com and another as member@example.com.', [
      { name: 'owner', page: ownerPage },
      { name: 'member', page: memberPage },
    ]);
    await ownerPage.goto(`/tree/project/${state.project.id}`);
    await waitForTreeProjectReady(ownerPage, state.project.id, state.task.id);
    await expect(ownerPage.locator('[data-tree-project-board]')).toContainText('Realtime Task');
    await steps.multiStep('In the owner browser, open /tree/project/1 and confirm Realtime Task is visible.', [
      { name: 'owner', page: ownerPage },
    ]);
    await memberPage.goto(`/tree/project/${state.project.id}`);
    await waitForTreeProjectReady(memberPage, state.project.id, state.task.id);
    await patchTask(memberPage, state.task.id, { title: 'Realtime Task Updated' });
    await focusTreeTask(memberPage, state.task.id, 'Member changed task title', ['Title should now read Realtime Task Updated.']);
    await steps.multiStep('In the member browser, update task 1 title to "Realtime Task Updated".', [
      { name: 'member', page: memberPage },
    ]);

    await expectContainsTextWithFailure(ownerPage, test.info(), '[data-tree-project-board]', 'Realtime Task Updated', 'tree-title-live-update', 'Owner tree board should show the renamed task immediately.');
    await focusTreeTask(ownerPage, state.task.id, 'Owner received live title update');
    await steps.multiStep('Return to the owner browser and verify the tree board updates in place to show the new title.', [
      { name: 'owner', page: ownerPage },
    ]);

    await ownerContext.close();
    await memberContext.close();
  });

  test('tree single-status updates live and stays stable after a follow-up edit', async ({ browser, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['socket', 'tree', 'status', 'single-status']);
    const state = await fetchSeedState(request);
    const ownerContext = await browser.newContext();
    const memberContext = await browser.newContext();
    const ownerPage = await ownerContext.newPage();
    const memberPage = await memberContext.newPage();

    await login(ownerPage, state.owner.email, state.owner.password);
    await login(memberPage, state.member.email, state.member.password);
    await ownerPage.goto(`/tree/project/${state.project.id}`);
    await memberPage.goto(`/tree/project/${state.project.id}`);
    await waitForTreeProjectReady(ownerPage, state.project.id, state.task.id);
    await waitForTreeProjectReady(memberPage, state.project.id, state.task.id);
    await steps.multiStep('Open the same tree project in both browsers with the shared task visible.', [
      { name: 'owner', page: ownerPage },
      { name: 'member', page: memberPage },
    ]);

    await patchTask(memberPage, state.task.id, { status: 'critical' });
    await focusTreeTask(memberPage, state.task.id, 'Member changed task status to Critical', ['The highlighted task should now show Critical.']);
    await steps.multiStep('In the member browser, change the task status to Critical.', [
      { name: 'member', page: memberPage },
    ]);

    await expectAttributeWithFailure(ownerPage, test.info(), `[data-task-row-id="${state.task.id}"]`, 'data-status-state', 'critical', 'tree-single-status-live-owner', 'Owner tree row should update to Critical immediately.');
    await focusTreeTask(ownerPage, state.task.id, 'Owner received live Critical status');
    await steps.multiStep('Verify the owner browser updates to the same Critical status without a refresh.', [
      { name: 'owner', page: ownerPage },
    ]);

    await patchTask(memberPage, state.task.id, { title: 'Realtime Task After Status' });
    await focusTreeTask(memberPage, state.task.id, 'Member changed title after status update', ['Status should remain Critical after this non-status edit.']);
    await steps.multiStep('In the member browser, change the task title after the status update.', [
      { name: 'member', page: memberPage },
    ]);

    await expectAttributeWithFailure(ownerPage, test.info(), `[data-task-row-id="${state.task.id}"]`, 'data-status-state', 'critical', 'tree-single-status-stable-owner', 'Owner tree row should stay Critical after the follow-up title edit.');
    await expectContainsTextWithFailure(ownerPage, test.info(), `[data-task-row-id="${state.task.id}"]`, 'Realtime Task After Status', 'tree-single-status-title-owner', 'Owner should also receive the follow-up title change.');
    await focusTreeTask(ownerPage, state.task.id, 'Owner sees critical status after follow-up edit');
    await steps.multiStep('Verify the owner browser still shows Critical after the title edit, with the new title visible too.', [
      { name: 'owner', page: ownerPage },
    ]);

    await ownerContext.close();
    await memberContext.close();
  });

  test('tree single-status control updates its button state after each inline change', async ({ page, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['tree', 'status', 'single-status', 'ui']);
    const state = await fetchSeedState(request);

    await login(page, state.owner.email, state.owner.password);
    await page.goto(`/tree/project/${state.project.id}`);
    await waitForTreeProjectReady(page, state.project.id, state.task.id);
    await expectTreeAssignmentsWithFailure(page, test.info(), state.task.id, ['Owner', 'Member'], 'tree-inline-status-initial-assignments', 'The seed task should start with both assignee badges visible.');
    await focusTreeTask(page, state.task.id, 'Initial tree task status', ['This task starts as open before any inline status changes.']);
    await steps.step('Open /tree/project/1 as owner@example.com and locate the shared task row in the Tree board.', page);

    await setTreeTaskSingleStatus(page, state.task.id, 'critical');
    await waitForTreeTaskUiToSettle(page, state.task.id);
    await expectAttributeWithFailure(page, test.info(), `[data-task-row-id="${state.task.id}"]`, 'data-status-state', 'critical', 'tree-inline-status-critical-row', 'The Tree row should switch to Critical immediately after selecting Critical.');
    await expectContainsTextWithFailure(page, test.info(), `[data-task-row-id="${state.task.id}"] [data-task-status-host="${state.task.id}"]`, 'critical', 'tree-inline-status-critical-cell', 'The inline status control should read Critical after selecting it.');
    await expectTreeAssignmentsWithFailure(page, test.info(), state.task.id, ['Owner', 'Member'], 'tree-inline-status-critical-assignments', 'Changing only the status should not drop any assignee badges.');
    await focusTreeTask(page, state.task.id, 'Tree status after selecting Critical', ['The highlighted status control should now read Critical.', 'Both assignee badges should still be present.']);
    await steps.step('Use the inline status control to set the task to Critical, then verify the row and status button both update to Critical.', page);

    await setTreeTaskSingleStatus(page, state.task.id, 'complete');
    await waitForTreeTaskUiToSettle(page, state.task.id);
    await expectAttributeWithFailure(page, test.info(), `[data-task-row-id="${state.task.id}"]`, 'data-status-state', 'complete', 'tree-inline-status-complete-row', 'The Tree row should switch to Complete immediately after selecting Complete.');
    await expectContainsTextWithFailure(page, test.info(), `[data-task-row-id="${state.task.id}"] [data-task-status-host="${state.task.id}"]`, 'complete', 'tree-inline-status-complete-cell', 'The inline status control should read Complete after selecting it.');
    await expectTreeAssignmentsWithFailure(page, test.info(), state.task.id, ['Owner', 'Member'], 'tree-inline-status-complete-assignments', 'Changing only the status should not drop any assignee badges.');
    await focusTreeTask(page, state.task.id, 'Tree status after selecting Complete', ['The highlighted status control should now read Complete.', 'Both assignee badges should still be present.']);
    await steps.step('Use the same inline status control to set the task to Complete, then verify the row and status button both update to Complete.', page);

    await setTreeTaskSingleStatus(page, state.task.id, 'open');
    await waitForTreeTaskUiToSettle(page, state.task.id);
    await expectAttributeWithFailure(page, test.info(), `[data-task-row-id="${state.task.id}"]`, 'data-status-state', 'open', 'tree-inline-status-open-row', 'The Tree row should switch back to Open immediately after selecting Open.');
    await expectContainsTextWithFailure(page, test.info(), `[data-task-row-id="${state.task.id}"] [data-task-status-host="${state.task.id}"]`, 'open', 'tree-inline-status-open-cell', 'The inline status control should read Open after selecting it.');
    await expectTreeAssignmentsWithFailure(page, test.info(), state.task.id, ['Owner', 'Member'], 'tree-inline-status-open-assignments', 'Changing only the status should not drop any assignee badges.');
    await focusTreeTask(page, state.task.id, 'Tree status after selecting Open again', ['The highlighted status control should now be back to Open.', 'Both assignee badges should still be present.']);
    await steps.step('Set the same task back to Open and verify the row and status button return to the Open state.', page);
  });

  test('off-tree updates are visible when navigating into tree later', async ({ browser, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['socket', 'tree', 'navigation', 'cache']);
    const state = await fetchSeedState(request);
    const ownerContext = await browser.newContext();
    const memberContext = await browser.newContext();
    const ownerPage = await ownerContext.newPage();
    const memberPage = await memberContext.newPage();

    await login(ownerPage, state.owner.email, state.owner.password);
    await expect(ownerPage).toHaveURL(/\/dashboard$/);
    await login(memberPage, state.member.email, state.member.password);
    await steps.multiStep('Open one browser as owner@example.com on the dashboard and another as member@example.com.', [
      { name: 'owner', page: ownerPage },
      { name: 'member', page: memberPage },
    ]);
    await memberPage.goto(`/tree/project/${state.project.id}`);
    await waitForTreeProjectReady(memberPage, state.project.id, state.task.id);
    await patchTask(memberPage, state.task.id, { title: 'Off Tree Update' });
    await focusTreeTask(memberPage, state.task.id, 'Member changed task while owner stayed off Tree', ['Owner remains on the dashboard for this step.']);
    await steps.multiStep('In the member browser, update task 1 title to "Off Tree Update" while the owner stays off Tree.', [
      { name: 'member', page: memberPage },
      { name: 'owner', page: ownerPage },
    ]);

    await ownerPage.goto(`/tree/project/${state.project.id}`);
    await waitForTreeProjectReady(ownerPage, state.project.id, state.task.id);
    await expectContainsTextWithFailure(ownerPage, test.info(), '[data-tree-project-board]', 'Off Tree Update', 'off-tree-navigation-title', 'Owner tree view should already contain the updated title on navigation.');
    await focusTreeTask(ownerPage, state.task.id, 'Owner navigated into updated tree view');
    await steps.multiStep('Go back to the owner browser, navigate to /tree/project/1, and verify the tree view already shows "Off Tree Update".', [
      { name: 'owner', page: ownerPage },
    ]);

    await ownerContext.close();
    await memberContext.close();
  });

  test('dashboard recent activity updates immediately when another user comments', async ({ browser, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['socket', 'dashboard', 'activity', 'comments']);
    const state = await fetchSeedState(request);
    const ownerContext = await browser.newContext();
    const memberContext = await browser.newContext();
    const ownerPage = await ownerContext.newPage();
    const memberPage = await memberContext.newPage();

    await login(ownerPage, state.owner.email, state.owner.password);
    await ownerPage.waitForURL('**/dashboard');
    await login(memberPage, state.member.email, state.member.password);
    await steps.multiStep('Open one browser as owner@example.com on the dashboard and another as member@example.com.', [
      { name: 'owner', page: ownerPage },
      { name: 'member', page: memberPage },
    ]);
    await memberPage.goto(`/tree/project/${state.project.id}`);
    await waitForTreeProjectReady(memberPage, state.project.id, state.task.id);
    const commentResult = await memberPage.evaluate(async (taskId) => {
      const response = await fetch(`/api/tasks/${taskId}/comments`, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ body: 'Playwright comment from member' }),
      });
      return { ok: response.ok, status: response.status };
    }, state.task.id);
    expect(commentResult.ok).toBeTruthy();
    await focusTreeTask(memberPage, state.task.id, 'Member posted a comment on the task');
    await steps.multiStep('In the member browser, post a comment on task 1 with the text "Playwright comment from member".', [
      { name: 'member', page: memberPage },
    ]);

    await expectContainsTextWithFailure(ownerPage, test.info(), '[data-dashboard-activity-list]', 'Playwright comment from member', 'dashboard-recent-activity-comment', 'Recent Activity should include the new comment preview.');
    await focusActivity(ownerPage, '[data-dashboard-activity-list]', 'Dashboard recent activity updated');
    await steps.multiStep('Return to the owner dashboard and verify Recent Activity shows the new comment immediately.', [
      { name: 'owner', page: ownerPage },
    ]);

    await ownerContext.close();
    await memberContext.close();
  });

  test('direct-project assignees stay consistent after off-tree assignment changes', async ({ browser, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['socket', 'tree', 'assignments', 'direct']);
    const state = await fetchSeedState(request);
    const ownerContext = await browser.newContext();
    const memberContext = await browser.newContext();
    const ownerPage = await ownerContext.newPage();
    const memberPage = await memberContext.newPage();

    await login(ownerPage, state.owner.email, state.owner.password);
    await ownerPage.waitForURL('**/dashboard');
    await login(memberPage, state.member.email, state.member.password);
    await steps.multiStep('Open one browser as owner@example.com and another as member@example.com.', [
      { name: 'owner', page: ownerPage },
      { name: 'member', page: memberPage },
    ]);
    await memberPage.goto(`/tree/project/${state.direct_project.id}`);
    await waitForTreeProjectReady(memberPage, state.direct_project.id, state.direct_task.id);
    await deleteAssignment(memberPage, state.assignments.direct_owner.id);
    await focusTreeTask(memberPage, state.direct_task.id, 'Member removed owner assignment', ['Direct task should now keep only Member assigned.']);
    await steps.multiStep('In the member browser, remove the owner assignment from the direct-project task.', [
      { name: 'member', page: memberPage },
    ]);

    await ownerPage.goto(`/tree/project/${state.direct_project.id}`);
    await memberPage.goto(`/tree/project/${state.direct_project.id}`);
    await waitForTreeProjectReady(ownerPage, state.direct_project.id, state.direct_task.id);
    await waitForTreeProjectReady(memberPage, state.direct_project.id, state.direct_task.id);
    await steps.multiStep('Open /tree/project/2 in both browsers.', [
      { name: 'owner', page: ownerPage },
      { name: 'member', page: memberPage },
    ]);

    const ownerBadges = ownerPage
      .locator(`[data-task-row-id="${state.direct_task.id}"] .assignments .badge-label`)
      .filter({ hasText: /Owner|Member/ });
    const memberBadges = memberPage
      .locator(`[data-task-row-id="${state.direct_task.id}"] .assignments .badge-label`)
      .filter({ hasText: /Owner|Member/ });

    await expect(ownerBadges).toHaveCount(1);
    await expect(memberBadges).toHaveCount(1);
    await expect(ownerBadges.first()).toHaveText('Member');
    await expect(memberBadges.first()).toHaveText('Member');
    await focusTreeTask(ownerPage, state.direct_task.id, 'Owner direct task assignments after sync');
    await focusTreeTask(memberPage, state.direct_task.id, 'Member direct task assignments after sync');
    await steps.multiStep('Inspect the assignee badges on Direct Task in both sessions and verify both browsers show only Member.', [
      { name: 'owner', page: ownerPage },
      { name: 'member', page: memberPage },
    ]);

    await ownerContext.close();
    await memberContext.close();
  });

  test('direct-project single-status task view stays consistent across users after remote update and reopen', async ({ browser, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['socket', 'tree', 'status', 'single-status', 'direct', 'cache']);
    const state = await fetchSeedState(request);
    const ownerContext = await browser.newContext();
    const memberContext = await browser.newContext();
    const ownerPage = await ownerContext.newPage();
    const memberPage = await memberContext.newPage();

    await login(ownerPage, state.owner.email, state.owner.password);
    await ownerPage.waitForURL('**/dashboard');
    await login(memberPage, state.member.email, state.member.password);
    await memberPage.waitForURL('**/dashboard');
    await steps.multiStep('Open one browser as owner@example.com and another as member@example.com, both starting on the dashboard.', [
      { name: 'owner', page: ownerPage },
      { name: 'member', page: memberPage },
    ]);

    await ownerPage.goto(`/tree/project/${state.direct_project.id}`);
    await waitForTreeProjectReady(ownerPage, state.direct_project.id, state.direct_task.id);
    await expect(ownerPage.locator(`[data-task-row-id="${state.direct_task.id}"]`)).toHaveAttribute('data-status-state', 'open');
    await steps.multiStep('In the owner browser, open the direct project tree view once so the initial Open snapshot is cached locally.', [
      { name: 'owner', page: ownerPage },
    ]);

    await ownerPage.goto('/dashboard');
    await expect(ownerPage).toHaveURL(/\/dashboard$/);
    await steps.multiStep('Return the owner browser to the dashboard, leaving the cached direct-project snapshot behind.', [
      { name: 'owner', page: ownerPage },
    ]);

    await memberPage.goto(`/tree/project/${state.direct_project.id}`);
    await waitForTreeProjectReady(memberPage, state.direct_project.id, state.direct_task.id);
    await patchTask(memberPage, state.direct_task.id, { status: 'critical' });
    await focusTreeTask(memberPage, state.direct_task.id, 'Member changed direct task status', ['Single-status task should now read Critical.']);
    await steps.multiStep('In the member browser, update the same direct-project task to Critical while the owner is off Tree.', [
      { name: 'member', page: memberPage },
    ]);

    await ownerPage.route(`**/api/projects/${state.direct_project.id}/tree_snapshot?**`, async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 4000));
      await route.continue();
    });

    await ownerPage.goto(`/tree/project/${state.direct_project.id}`);
    await memberPage.goto(`/tree/project/${state.direct_project.id}`);
    await waitForTreeProjectReady(ownerPage, state.direct_project.id, state.direct_task.id);
    await waitForTreeProjectReady(memberPage, state.direct_project.id, state.direct_task.id);
    const ownerRow = ownerPage.locator(`[data-task-row-id="${state.direct_task.id}"]`);
    const memberRow = memberPage.locator(`[data-task-row-id="${state.direct_task.id}"]`);
    await annotateTaskStatusView(ownerPage, state.direct_task.id, 'Owner task view');
    await annotateTaskStatusView(memberPage, state.direct_task.id, 'Member task view');
    await expectAttributeWithFailure(ownerPage, test.info(), `[data-task-row-id="${state.direct_task.id}"]`, 'data-status-state', 'critical', 'owner-direct-single-status', 'Owner should see the same single-status state after reopening Tree.');
    await expectAttributeWithFailure(memberPage, test.info(), `[data-task-row-id="${state.direct_task.id}"]`, 'data-status-state', 'critical', 'member-direct-single-status', 'Member should still see Critical on the same direct task.');
    await steps.multiStep('Re-open the direct project in both browsers and inspect the single-status task row itself. The highlighted task view should now match in both sessions, with both users seeing Critical.', [
      { name: 'owner', page: ownerPage },
      { name: 'member', page: memberPage },
    ]);

    await ownerContext.close();
    await memberContext.close();
  });

  test('tree project status update does not reset unrelated assignees or status pills', async ({ browser, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['socket', 'tree', 'status', 'single-status', 'assignments', 'jannaf']);
    const state = await fetchSeedState(request);
    const ownerContext = await browser.newContext();
    const memberContext = await browser.newContext();
    const ownerPage = await ownerContext.newPage();
    const memberPage = await memberContext.newPage();

    await login(ownerPage, state.owner.email, state.owner.password);
    await login(memberPage, state.member.email, state.member.password);
    await ownerPage.goto(`/tree/project/${state.jannaf_project.id}`);
    await memberPage.goto(`/tree/project/${state.jannaf_project.id}`);
    await waitForTreeProjectReady(ownerPage, state.jannaf_project.id, state.jannaf_status_task.id);
    await waitForTreeProjectReady(memberPage, state.jannaf_project.id, state.jannaf_status_task.id);
    await expect(ownerPage.locator(`[data-task-row-id="${state.jannaf_assignee_task.id}"]`)).toHaveCount(1);
    await focusTreeTask(ownerPage, state.jannaf_status_task.id, 'Owner initial JANNAF status task', ['Register for JANNAF starts as Open.']);
    await focusTreeTask(ownerPage, state.jannaf_assignee_task.id, 'Owner initial JANNAF assignee task', ['Get JANNAF accounts starts as Critical with both Owner and Member assigned.']);
    await steps.multiStep('Open the JANNAF project tree view in both browsers and confirm both target rows are visible.', [
      { name: 'owner', page: ownerPage },
      { name: 'member', page: memberPage },
    ]);

    await expectAttributeWithFailure(ownerPage, test.info(), `[data-task-row-id="${state.jannaf_assignee_task.id}"]`, 'data-status-state', 'critical', 'jannaf-initial-related-row-state', 'The unaffected JANNAF row should begin in Critical state.');
    await expectContainsTextWithFailure(ownerPage, test.info(), `[data-task-row-id="${state.jannaf_assignee_task.id}"] [data-task-status-host="${state.jannaf_assignee_task.id}"]`, 'critical', 'jannaf-initial-related-row-pill', 'The unaffected JANNAF row should begin with a Critical status pill.');
    await expectTreeAssignmentsWithFailure(ownerPage, test.info(), state.jannaf_assignee_task.id, ['Owner', 'Member'], 'jannaf-initial-related-row-assignees', 'The unaffected JANNAF row should begin with both assignee badges.');

    await patchTask(memberPage, state.jannaf_status_task.id, { status: 'complete' });
    await focusTreeTask(memberPage, state.jannaf_status_task.id, 'Member changed Register for JANNAF to Complete', ['The changed row should now show Complete in the member browser.']);
    await steps.multiStep('In the member browser, change Register for JANNAF to Complete.', [
      { name: 'member', page: memberPage },
    ]);

    await expectAttributeWithFailure(ownerPage, test.info(), `[data-task-row-id="${state.jannaf_status_task.id}"]`, 'data-status-state', 'complete', 'jannaf-changed-row-state-owner', 'The receiving browser should mark Register for JANNAF as Complete.');
    await expectContainsTextWithFailure(ownerPage, test.info(), `[data-task-row-id="${state.jannaf_status_task.id}"] [data-task-status-host="${state.jannaf_status_task.id}"]`, 'complete', 'jannaf-changed-row-pill-owner', 'The receiving browser should show a Complete status pill for Register for JANNAF.');
    await focusTreeTask(ownerPage, state.jannaf_status_task.id, 'Owner received Register for JANNAF status update', ['The status pill should now read Complete.']);
    await steps.multiStep('Return to the owner browser and verify Register for JANNAF updates to Complete immediately.', [
      { name: 'owner', page: ownerPage },
    ]);

    await expectAttributeWithFailure(ownerPage, test.info(), `[data-task-row-id="${state.jannaf_assignee_task.id}"]`, 'data-status-state', 'critical', 'jannaf-unrelated-row-state-owner', 'Get JANNAF accounts should keep its existing Critical row state.');
    await expectContainsTextWithFailure(ownerPage, test.info(), `[data-task-row-id="${state.jannaf_assignee_task.id}"] [data-task-status-host="${state.jannaf_assignee_task.id}"]`, 'critical', 'jannaf-unrelated-row-pill-owner', 'Get JANNAF accounts should keep its existing Critical status pill.');
    await expectTreeAssignmentsWithFailure(ownerPage, test.info(), state.jannaf_assignee_task.id, ['Owner', 'Member'], 'jannaf-unrelated-row-assignees-owner', 'Get JANNAF accounts should keep both assignee badges after the other row changes status.');
    await focusTreeTask(ownerPage, state.jannaf_assignee_task.id, 'Owner unrelated JANNAF row after remote status update', ['Get JANNAF accounts should still show Critical and both assignees.']);
    await steps.multiStep('Inspect Get JANNAF accounts in the owner browser and verify it still shows Critical with both Owner and Member assigned.', [
      { name: 'owner', page: ownerPage },
    ]);

    await ownerContext.close();
    await memberContext.close();
  });

  test('mobile dashboard to todo keeps the sidebar trigger usable', async ({ page, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['mobile', 'navigation', 'sidebar']);
    const state = await fetchSeedState(request);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/login');
    await steps.step('Open the app in a mobile-sized viewport.', page);
    await login(page, state.owner.email, state.owner.password);
    await page.waitForURL('**/dashboard');
    await steps.step('Sign in as owner@example.com with password123.', page);

    const trigger = page.locator('[data-mobile-sidebar-trigger]');
    await expect(trigger).toHaveCount(1);
    await expect(trigger).toBeHidden();
    await steps.step('Confirm the dashboard loads and the mobile sidebar trigger is hidden there.', page);

    await page.locator('.view-switcher [data-dashboard-view-target="todo"]').click();
    await page.waitForURL(/\/todo/);
    await steps.step('Tap the Todo tab in the header switcher.', page);
    await expect(trigger).toBeVisible();
    await trigger.click();
    await expect(page.locator('.layout > .sidebar.is-mobile-open')).toBeVisible();
    await steps.step('Verify the sidebar trigger becomes visible and opens the mobile sidebar drawer.', page);
  });

  test('todo updates live when another user changes task title', async ({ browser, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['socket', 'todo', 'title']);
    const state = await fetchSeedState(request);
    const ownerContext = await browser.newContext();
    const memberContext = await browser.newContext();
    const ownerPage = await ownerContext.newPage();
    const memberPage = await memberContext.newPage();

    await login(ownerPage, state.owner.email, state.owner.password);
    await login(memberPage, state.member.email, state.member.password);
    await ownerPage.goto('/todo');
    await expect(ownerPage.locator(`.todo-item[data-task-id="${state.task.id}"]`)).toContainText('Realtime Task');
    await steps.multiStep('Open /todo as owner@example.com and keep member@example.com in a second browser.', [
      { name: 'owner', page: ownerPage },
      { name: 'member', page: memberPage },
    ]);

    await memberPage.goto('/todo');
    await patchTask(memberPage, state.task.id, { title: 'Todo Socket Rename' });
    await focusTodoTask(memberPage, state.task.id, 'Member renamed Todo task');
    await steps.multiStep('In the member browser, rename the task to "Todo Socket Rename".', [
      { name: 'member', page: memberPage },
    ]);

    await expectContainsTextWithFailure(ownerPage, test.info(), `.todo-item[data-task-id="${state.task.id}"]`, 'Todo Socket Rename', 'todo-title-live-update', 'Owner Todo item should reflect the renamed title.');
    await focusTodoTask(ownerPage, state.task.id, 'Owner Todo item after live title update');
    await steps.multiStep('Verify the owner Todo board updates the task title in place.', [
      { name: 'owner', page: ownerPage },
    ]);

    await ownerContext.close();
    await memberContext.close();
  });

  test('todo rebuckets live when another user changes due date', async ({ browser, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['socket', 'todo', 'due-date', 'buckets']);
    const state = await fetchSeedState(request);
    const ownerContext = await browser.newContext();
    const memberContext = await browser.newContext();
    const ownerPage = await ownerContext.newPage();
    const memberPage = await memberContext.newPage();
    const tomorrow = isoDateWithOffset(1);

    await login(ownerPage, state.owner.email, state.owner.password);
    await login(memberPage, state.member.email, state.member.password);
    await ownerPage.goto('/todo');
    await expect(ownerPage.locator(`.todo-item[data-task-id="${state.task.id}"]`)).toContainText('Realtime Task');
    await steps.multiStep('Open /todo and confirm the task starts in the No Due Date bucket.', [
      { name: 'owner', page: ownerPage },
    ]);

    await memberPage.goto('/todo');
    await patchTask(memberPage, state.task.id, { due_at: tomorrow, due_mode: 'date' });
    await focusTodoTask(memberPage, state.task.id, 'Member changed Todo due date', [`Due date should now be ${tomorrow}.`]);
    await steps.multiStep(`In the member browser, set the task due date to ${tomorrow}.`, [
      { name: 'member', page: memberPage },
    ]);

    await expectContainsTextWithFailure(ownerPage, test.info(), '.todo-date-group[data-todo-date-key="tomorrow"]', 'Realtime Task', 'todo-rebucket-tomorrow', 'Owner Todo board should move the task into Tomorrow.');
    await focusActivity(ownerPage, '.todo-date-group[data-todo-date-key="tomorrow"]', 'Owner Tomorrow bucket after rebucket');
    await steps.multiStep('Verify the owner Todo board moves the task into the Tomorrow bucket.', [
      { name: 'owner', page: ownerPage },
    ]);

    await ownerContext.close();
    await memberContext.close();
  });

  test('tree updates live when another user changes due date', async ({ browser, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['socket', 'tree', 'due-date']);
    const state = await fetchSeedState(request);
    const ownerContext = await browser.newContext();
    const memberContext = await browser.newContext();
    const ownerPage = await ownerContext.newPage();
    const memberPage = await memberContext.newPage();
    const tomorrow = isoDateWithOffset(1);

    await login(ownerPage, state.owner.email, state.owner.password);
    await login(memberPage, state.member.email, state.member.password);
    await ownerPage.goto(`/tree/project/${state.project.id}`);
    await waitForTreeProjectReady(ownerPage, state.project.id, state.task.id);
    await expect(ownerPage.locator('[data-tree-project-board]')).toContainText('Realtime Task');
    await steps.multiStep('Open the project tree board as owner@example.com.', [
      { name: 'owner', page: ownerPage },
    ]);

    await memberPage.goto(`/tree/project/${state.project.id}`);
    await waitForTreeProjectReady(memberPage, state.project.id, state.task.id);
    await patchTask(memberPage, state.task.id, { due_at: tomorrow, due_mode: 'date' });
    await focusTreeTask(memberPage, state.task.id, 'Member changed tree due date', [`Due date should now be ${tomorrow}.`]);
    await steps.multiStep(`In the member browser, set the task due date to ${tomorrow}.`, [
      { name: 'member', page: memberPage },
    ]);

    await expectContainsTextWithFailure(ownerPage, test.info(), `[data-task-row-id="${state.task.id}"]`, tomorrow, 'tree-due-date-live-update', 'Owner tree row should show the updated due date immediately.');
    await focusTreeTask(ownerPage, state.task.id, 'Owner tree row after live due-date update');
    await steps.multiStep('Verify the owner tree row updates its due-date display immediately.', [
      { name: 'owner', page: ownerPage },
    ]);

    await ownerContext.close();
    await memberContext.close();
  });

  test('tree updates live when another user changes assignments', async ({ browser, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['socket', 'tree', 'assignments']);
    const state = await fetchSeedState(request);
    const ownerContext = await browser.newContext();
    const memberContext = await browser.newContext();
    const ownerPage = await ownerContext.newPage();
    const memberPage = await memberContext.newPage();

    await login(ownerPage, state.owner.email, state.owner.password);
    await login(memberPage, state.member.email, state.member.password);
    await ownerPage.goto(`/tree/project/${state.project.id}`);
    await waitForTreeProjectReady(ownerPage, state.project.id, state.task.id);
    await expect(ownerPage.locator(`[data-task-row-id="${state.task.id}"] .assignments`)).toContainText('Owner');
    await expect(ownerPage.locator(`[data-task-row-id="${state.task.id}"] .assignments`)).toContainText('Member');
    await steps.multiStep('Open the shared project tree board and confirm both Owner and Member are assigned.', [
      { name: 'owner', page: ownerPage },
    ]);

    await memberPage.goto(`/tree/project/${state.project.id}`);
    await waitForTreeProjectReady(memberPage, state.project.id, state.task.id);
    await deleteAssignment(memberPage, state.assignments.task_owner.id);
    await focusTreeTask(memberPage, state.task.id, 'Member removed shared task assignment', ['Owner badge should disappear after sync.']);
    await steps.multiStep('In the member browser, remove the owner assignment from the shared task.', [
      { name: 'member', page: memberPage },
    ]);

    await expect(ownerPage.locator(`[data-task-row-id="${state.task.id}"] .assignments`)).not.toContainText('Owner');
    await expect(ownerPage.locator(`[data-task-row-id="${state.task.id}"] .assignments`)).toContainText('Member');
    await focusTreeTask(ownerPage, state.task.id, 'Owner tree row after assignment update');
    await steps.multiStep('Verify the owner tree row updates its assignment badges in place.', [
      { name: 'owner', page: ownerPage },
    ]);

    await ownerContext.close();
    await memberContext.close();
  });

  test('inbox updates immediately when another user comments while viewing inbox', async ({ browser, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['socket', 'inbox', 'comments', 'notifications']);
    const state = await fetchSeedState(request);
    const ownerContext = await browser.newContext();
    const memberContext = await browser.newContext();
    const ownerPage = await ownerContext.newPage();
    const memberPage = await memberContext.newPage();

    await login(ownerPage, state.owner.email, state.owner.password);
    await login(memberPage, state.member.email, state.member.password);
    await ownerPage.goto('/inbox');
    await expect(ownerPage.locator('[data-inbox-feed]')).toBeVisible();
    await steps.multiStep('Open /inbox as owner@example.com and keep member@example.com in a second browser.', [
      { name: 'owner', page: ownerPage },
      { name: 'member', page: memberPage },
    ]);

    await memberPage.goto(`/tree/project/${state.project.id}`);
    await waitForTreeProjectReady(memberPage, state.project.id, state.task.id);
    const commentResult = await memberPage.evaluate(async (taskId) => {
      const response = await fetch(`/api/tasks/${taskId}/comments`, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ body: 'Inbox comment from member' }),
      });
      return { ok: response.ok, status: response.status };
    }, state.task.id);
    expect(commentResult.ok).toBeTruthy();
    await focusTreeTask(memberPage, state.task.id, 'Member posted inbox-driving comment');
    await steps.multiStep('In the member browser, post a comment with the text "Inbox comment from member".', [
      { name: 'member', page: memberPage },
    ]);

    await expectContainsTextWithFailure(ownerPage, test.info(), '[data-inbox-feed]', 'Inbox comment from member', 'inbox-live-comment-preview', 'Owner inbox should show the new comment preview immediately.');
    await focusActivity(ownerPage, '[data-inbox-feed]', 'Owner inbox feed after live comment');
    await steps.multiStep('Verify the owner inbox feed shows the new comment preview immediately.', [
      { name: 'owner', page: ownerPage },
    ]);

    await ownerContext.close();
    await memberContext.close();
  });
});
