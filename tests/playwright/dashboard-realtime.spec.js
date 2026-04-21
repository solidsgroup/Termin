const { test, expect } = require('@playwright/test');
const {
  createStepRecorder,
  captureFailureSnapshot,
  installBrowserErrorCollectorOnContext,
} = require('./test-helpers');

test.beforeEach(async ({ page, browser }, testInfo) => {
  const assertions = [];
  assertions.push(installBrowserErrorCollectorOnContext(page.context(), testInfo));

  const originalNewContext = browser.newContext.bind(browser);
  browser.newContext = async (...args) => {
    const context = await originalNewContext(...args);
    assertions.push(installBrowserErrorCollectorOnContext(context, testInfo));
    return context;
  };

  testInfo.__assertNoBrowserErrors = async function assertNoBrowserErrors() {
    for (const assertContextErrors of assertions) {
      await assertContextErrors();
    }
  };
});

test.afterEach(async ({}, testInfo) => {
  if (typeof testInfo.__assertNoBrowserErrors === 'function') {
    await testInfo.__assertNoBrowserErrors();
  }
});

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

async function waitForRealtimeSocketIfPresent(page) {
  try {
    await page.waitForFunction(() => {
      if (typeof window === 'undefined') return true;
      if (!('socketConnected' in window)) return true;
      return !!window.socketConnected;
    }, { timeout: 4000 });
  } catch (_error) {
    // Some pages in the suite do not initialize the dashboard socket runtime.
  }
}

async function patchTask(page, taskId, payload) {
  await waitForRealtimeSocketIfPresent(page);
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

async function createTask(page, payload) {
  await waitForRealtimeSocketIfPresent(page);
  const result = await page.evaluate(async (taskPayload) => {
    const response = await fetch('/api/tasks', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(taskPayload),
    });
    let data = null;
    try {
      data = await response.json();
    } catch (error) {
      data = null;
    }
    return { ok: response.ok, status: response.status, data };
  }, payload);
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

async function createAssignment(page, taskId, email) {
  const result = await page.evaluate(async ({ taskId: targetTaskId, email: targetEmail }) => {
    const response = await fetch('/api/assignments', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        target_type: 'task',
        target_id: targetTaskId,
        email: targetEmail,
      }),
    });
    let data = null;
    try {
      data = await response.json();
    } catch (_error) {
      data = null;
    }
    return { ok: response.ok, status: response.status, data };
  }, { taskId, email });
  expect(result.ok).toBeTruthy();
  return result.data;
}

async function patchGroup(page, groupId, payload) {
  const result = await page.evaluate(async ({ groupId, payload }) => {
    const response = await fetch(`/api/groups/${groupId}`, {
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
  }, { groupId, payload });
  expect(result.ok).toBeTruthy();
  return result.data;
}

async function createTaskPrerequisite(page, taskId, prerequisiteTaskId) {
  const result = await page.evaluate(async ({ taskId: targetTaskId, prerequisiteTaskId: requiredTaskId }) => {
    const response = await fetch(`/api/tasks/${targetTaskId}/prerequisites`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prerequisite_task_id: requiredTaskId }),
    });
    let data = null;
    try {
      data = await response.json();
    } catch (error) {
      data = null;
    }
    return { ok: response.ok, status: response.status, data };
  }, { taskId, prerequisiteTaskId });
  expect(result.ok).toBeTruthy();
  return result.data;
}

async function deleteTaskPrerequisite(page, prerequisiteId) {
  const result = await page.evaluate(async (rowId) => {
    const response = await fetch(`/api/task-prerequisites/${rowId}`, {
      method: 'DELETE',
      credentials: 'same-origin',
    });
    let data = null;
    try {
      data = await response.json();
    } catch (error) {
      data = null;
    }
    return { ok: response.ok, status: response.status, data };
  }, prerequisiteId);
  expect(result.ok).toBeTruthy();
  return result.data;
}

async function deleteTask(page, taskId) {
  const result = await page.evaluate(async (rowTaskId) => {
    const response = await fetch(`/api/tasks/${rowTaskId}?confirm=1`, {
      method: 'DELETE',
      credentials: 'same-origin',
    });
    let data = null;
    try {
      data = await response.json();
    } catch (error) {
      data = null;
    }
    return { ok: response.ok, status: response.status, data };
  }, taskId);
  expect(result.ok).toBeTruthy();
  return result.data;
}

async function convertCollaborator(request) {
  const response = await request.post('/_e2e/convert-collaborator');
  expect(response.ok()).toBeTruthy();
  return response.json();
}

async function fetchDirectProjects(page) {
  const result = await page.evaluate(async () => {
    const response = await fetch('/api/direct-projects', {
      method: 'GET',
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
    });
    let data = {};
    try {
      data = await response.json();
    } catch (_error) {
      data = {};
    }
    return {
      ok: response.ok,
      status: response.status,
      data,
    };
  });
  expect(result.ok).toBeTruthy();
  return result.data;
}

async function fetchNotificationState(page) {
  return page.evaluate(async () => {
    const response = await fetch('/api/notifications', {
      method: 'GET',
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
    });
    let data = {};
    try {
      data = await response.json();
    } catch (_error) {
      data = {};
    }
    return {
      ok: response.ok,
      status: response.status,
      data,
    };
  });
}

async function waitForRegularNotificationCount(page, expectedMinimum) {
  await page.waitForFunction(async (minimum) => {
    const response = await fetch('/api/notifications', {
      method: 'GET',
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
    });
    const payload = await response.json().catch(() => ({}));
    return Number((payload && payload.unread_regular_total) || 0) >= minimum;
  }, expectedMinimum);
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

async function focusTreeDirectProjectRow(page, projectId, label, extraLines = []) {
  const selector = `[data-tree-direct-project="${projectId}"] .todo-tree-row`;
  const row = page.locator(selector).first();
  const text = ((await row.textContent()) || '').trim().replace(/\s+/g, ' ').slice(0, 180);
  const hasAvatar = await row.locator('.avatar-stack.is-tree-direct .avatar-chip').count();
  const shareIcons = await row.locator('.shared-with-me-icon').count();
  const toggles = await row.locator('[data-tree-toggle-project], .todo-tree-toggle.is-hidden, .todo-tree-toggle.is-project-toggle').count();
  await annotateLocator(page, selector, label, [
    `Row text: ${text}`,
    `Direct avatar chips: ${hasAvatar}`,
    `Share icons: ${shareIcons}`,
    `Project toggles: ${toggles}`,
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
    await expect(page.locator(`[data-task-row-id="${taskId}"]`)).toHaveCount(1, { timeout: 15000 });
    await page.waitForFunction(({ projectId: targetProjectId, taskId: targetTaskId }) => {
      const boardEl = document.querySelector(`[data-tree-project-board="${targetProjectId}"]`);
      if (!boardEl) return false;
      const taskRow = document.querySelector(`[data-task-row-id="${targetTaskId}"]`);
      if (!taskRow) return false;
      const loadingPanel = boardEl.querySelector('[data-tree-project-loading-panel]');
      return !loadingPanel || loadingPanel.hidden || loadingPanel.style.display === 'none' || loadingPanel.getAttribute('aria-hidden') === 'true';
    }, { projectId, taskId }, { timeout: 15000 }).catch(() => {});
  } else {
    await page.waitForFunction((targetProjectId) => {
      const boardEl = document.querySelector(`[data-tree-project-board="${targetProjectId}"]`);
      if (!boardEl) return false;
      const loadingPanel = boardEl.querySelector('[data-tree-project-loading-panel]');
      return !loadingPanel || loadingPanel.hidden || loadingPanel.style.display === 'none' || loadingPanel.getAttribute('aria-hidden') === 'true';
    }, projectId, { timeout: 15000 }).catch(() => {});
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

async function expectTodoTaskLinkBadge(page, testInfo, taskId, linkUrl, label, note = '') {
  const selector = `.todo-item[data-task-id="${taskId}"] a[href="${linkUrl}"]`;
  const locator = page.locator(selector).first();
  try {
    await expect(locator).toHaveCount(1);
    await expect(locator.locator('img')).toHaveCount(1);
  } catch (error) {
    await captureFailureSnapshot(page, testInfo, label, {
      selector: `.todo-item[data-task-id="${taskId}"]`,
      expected: `link badge for ${linkUrl}`,
      actual: (((await page.locator(`.todo-item[data-task-id="${taskId}"]`).first().innerHTML()) || '').replace(/\s+/g, ' ').slice(0, 500)),
      note,
    });
    throw error;
  }
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

async function expectPrereqHoverCard(page, triggerSelector, expectedTitle) {
  await page.locator(triggerSelector).first().hover();
  const hoverCard = page.locator('#prereq-hover-card');
  await expect(hoverCard).toBeVisible();
  await expect(hoverCard).toContainText('Prerequisites');
  if (expectedTitle) {
    await expect(hoverCard).toContainText(expectedTitle);
  }
}

async function openPollDialogFromTree(page, taskId) {
  const trigger = page.locator(`[data-task-row-id="${taskId}"] [data-task-poll-trigger="${taskId}"]`).first();
  await expect(trigger).toHaveCount(1);
  await trigger.scrollIntoViewIfNeeded();
  await trigger.click();
  await expect(page.locator('#poll-response-dialog')).toBeVisible();
}

async function submitPollResponse(page, optionId) {
  const option = page.locator(`#poll-response-options [data-poll-response-option="${optionId}"]`).first();
  await expect(option).toHaveCount(1);
  await option.click();
  await page.locator('#poll-response-save').click();
  await expect(page.locator('#poll-response-dialog')).toBeHidden();
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

  test('dashboard action items remove a completed task without refresh', async ({ page, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['dashboard', 'action-items', 'status', 'regression']);
    const state = await fetchSeedState(request);
    const today = isoDateWithOffset(0);

    await login(page, state.owner.email, state.owner.password);
    await patchTask(page, state.task.id, { due_at: today, due_mode: 'date', status: 'open' });
    await page.goto('/dashboard');

    const actionList = page.locator('.dashboard-action-list');
    const taskTitle = page.locator('.dashboard-action-title', { hasText: 'Realtime Task' });

    await expect(actionList).toContainText('Realtime Task');
    await steps.step(`Open /dashboard with Realtime Task due ${today} so it appears in Action Items.`, page);

    await patchTask(page, state.task.id, { status: 'complete' });
    await steps.step('Mark Realtime Task complete while staying on the dashboard.', page);

    await expect(taskTitle).toHaveCount(0);
    await steps.step('Verify the completed task disappears from Action Items without a refresh.', page);
  });

  test('dashboard action items remove a per-user completed task without refresh', async ({ page, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['dashboard', 'action-items', 'multi-status', 'regression']);
    const state = await fetchSeedState(request);
    const today = isoDateWithOffset(0);

    await login(page, state.owner.email, state.owner.password);
    await patchTask(page, state.task.id, { due_at: today, due_mode: 'date', status_mode: 'multi' });
    await page.goto('/dashboard');

    const actionList = page.locator('.dashboard-action-list');
    const taskTitle = page.locator('.dashboard-action-title', { hasText: 'Realtime Task' });

    await expect(actionList).toContainText('Realtime Task');
    await steps.step(`Open /dashboard with Realtime Task in multi-status mode due ${today} so it appears in Action Items.`, page);

    await patchTask(page, state.task.id, { user_status: 'complete', status_user_id: state.owner.id });
    await steps.step('Mark only the owner status complete while staying on the dashboard.', page);

    await expect(taskTitle).toHaveCount(0);
    await steps.step('Verify the task disappears from Action Items when the viewer-specific status becomes complete.', page);
  });

  test('dashboard action items remove a 100 percent progress task without refresh', async ({ page, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['dashboard', 'action-items', 'percent-status', 'regression']);
    const state = await fetchSeedState(request);
    const today = isoDateWithOffset(0);

    await login(page, state.owner.email, state.owner.password);
    await patchTask(page, state.task.id, { due_at: today, due_mode: 'date', status_mode: 'percent', status_percentage: 25 });
    await page.goto('/dashboard');

    const actionList = page.locator('.dashboard-action-list');
    const taskTitle = page.locator('.dashboard-action-title', { hasText: 'Realtime Task' });

    await expect(actionList).toContainText('Realtime Task');
    await steps.step(`Open /dashboard with Realtime Task at 25% progress due ${today} so it appears in Action Items.`, page);

    await patchTask(page, state.task.id, { status_mode: 'percent', status_percentage: 100 });
    await steps.step('Update the same task to 100% progress while staying on the dashboard.', page);

    await expect(taskTitle).toHaveCount(0);
    await steps.step('Verify the task disappears from Action Items when progress reaches 100% without a refresh.', page);
  });

  test('dashboard action items remove a converted collaborator task without refresh', async ({ browser, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['dashboard', 'action-items', 'converted-collaborator', 'regression']);
    const state = await fetchSeedState(request);
    const today = isoDateWithOffset(0);
    const conversion = await convertCollaborator(request);
    const convertedUser = conversion.user;

    const convertedContext = await browser.newContext();
    const convertedPage = await convertedContext.newPage();

    await login(convertedPage, convertedUser.email, convertedUser.password);
    await patchTask(convertedPage, state.collaborator_dated_task.id, { due_at: today, due_mode: 'date', status: 'open' });
    await convertedPage.goto('/dashboard');

    const actionList = convertedPage.locator('.dashboard-action-list');
    const taskTitle = convertedPage.locator('.dashboard-action-title', { hasText: 'Email Collaborator Due Soon' });

    await expect(actionList).toContainText('Email Collaborator Due Soon');
    await steps.step(`Open /dashboard as the converted collaborator with Email Collaborator Due Soon due ${today} so it appears in Action Items.`, convertedPage);

    await patchTask(convertedPage, state.collaborator_dated_task.id, { status: 'complete' });
    await steps.step('Mark the converted collaborator task complete while staying on the dashboard.', convertedPage);

    await expect(taskTitle).toHaveCount(0);
    await steps.step('Verify the converted collaborator dashboard removes the completed task from Action Items without a refresh.', convertedPage);

    await convertedContext.close();
  });

  test('quick task add creates and selects the self direct project for a user without one', async ({ page, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['dashboard', 'quick-add', 'direct-project', 'regression']);
    const state = await fetchSeedState(request);

    await login(page, state.owner.email, state.owner.password);
    await page.goto('/dashboard');

    const before = await fetchDirectProjects(page);
    const beforeResults = Array.isArray(before.results) ? before.results : [];
    expect(beforeResults.some((project) => (project.display_name || project.name) === 'Owner')).toBeFalsy();
    await steps.step('Verify the owner starts without a self direct project in the direct-project API results.', page);

    await page.locator('#dashboard-quick-task-fab').click();
    await expect(page.locator('#quick-task-modal')).toBeVisible();
    await expect(page.locator('#quick-task-project-title')).toHaveText('Owner');
    await expect(page.locator('#quick-task-assignee')).toHaveValue(state.owner.email);
    await steps.step('Open quick task add and confirm it selects the self direct project with the owner as default assignee.', page);

    const after = await fetchDirectProjects(page);
    const afterResults = Array.isArray(after.results) ? after.results : [];
    expect(afterResults.some((project) => (project.display_name || project.name) === 'Owner')).toBeTruthy();
    expect(afterResults).toHaveLength(beforeResults.length + 1);
    await steps.step('Verify opening quick task add created the self direct project for the owner.', page);
  });

  test('dashboard action item drawer can delete a task and close without refresh', async ({ page, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['dashboard', 'action-items', 'delete', 'regression']);
    const state = await fetchSeedState(request);
    const today = isoDateWithOffset(0);

    await login(page, state.owner.email, state.owner.password);
    await patchTask(page, state.task.id, { due_at: today, due_mode: 'date', status: 'open' });
    await page.goto('/dashboard');

    const taskTitle = page.locator('.dashboard-action-title', { hasText: 'Realtime Task' });
    await expect(taskTitle).toHaveCount(1);
    await steps.step(`Open /dashboard with Realtime Task due ${today} so it appears in Action Items.`, page);

    await taskTitle.click();
    await expect(page.locator('#discussion-drawer')).toHaveClass(/open/);
    await steps.step('Open the task drawer from the dashboard Action Items title.', page);

    await page.locator('#task-settings-delete').click();
    await expect(page.locator('#discussion-drawer')).not.toHaveClass(/open/);
    await expect(page.locator('.dashboard-action-title', { hasText: 'Realtime Task' })).toHaveCount(0);
    await steps.step('Delete the task from the drawer and verify the drawer closes and the task disappears without a refresh.', page);
  });

  test('dashboard delete toast can undo a task delete', async ({ page, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['dashboard', 'action-items', 'delete', 'toast', 'undo']);
    const state = await fetchSeedState(request);
    const today = isoDateWithOffset(0);

    await login(page, state.owner.email, state.owner.password);
    await patchTask(page, state.task.id, { due_at: today, due_mode: 'date', status: 'open' });
    await page.goto('/dashboard');

    await page.locator('.dashboard-action-title', { hasText: 'Realtime Task' }).click();
    await expect(page.locator('#discussion-drawer')).toHaveClass(/open/);
    await page.locator('#task-settings-delete').click();
    await expect(page.locator('.action-toast-title', { hasText: 'Task deleted' })).toHaveCount(1);
    await expect(page.locator('.dashboard-action-title', { hasText: 'Realtime Task' })).toHaveCount(0);
    await steps.step('Delete Realtime Task from the dashboard drawer and verify the undo toast appears while the task disappears locally.', page);

    await page.locator('.action-toast').filter({ hasText: 'Realtime Task' }).locator('button', { hasText: 'Undo' }).click();
    await expect(page.locator('.dashboard-action-title', { hasText: 'Realtime Task' })).toHaveCount(1);
    await steps.step('Click Undo on the delete toast and verify the task returns to the dashboard Action Items list.', page);
  });

  test('dashboard delete toast dismiss commits the delete', async ({ page, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['dashboard', 'action-items', 'delete', 'toast', 'dismiss']);
    const state = await fetchSeedState(request);
    const today = isoDateWithOffset(0);

    await login(page, state.owner.email, state.owner.password);
    await patchTask(page, state.task.id, { due_at: today, due_mode: 'date', status: 'open' });
    await page.goto('/dashboard');

    await page.locator('.dashboard-action-title', { hasText: 'Realtime Task' }).click();
    await page.locator('#task-settings-delete').click();
    const toast = page.locator('.action-toast').filter({ hasText: 'Realtime Task' }).first();
    await expect(toast).toBeVisible();
    await toast.locator('button', { hasText: 'Dismiss' }).click();
    await expect(page.locator('.dashboard-action-title', { hasText: 'Realtime Task' })).toHaveCount(0);
    await page.reload();
    await expect(page.locator('.dashboard-action-title', { hasText: 'Realtime Task' })).toHaveCount(0);
    await steps.step('Dismiss the delete toast and verify the task remains deleted after a reload.', page);
  });

  test('dashboard completion toast can undo marking a task complete', async ({ page, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['dashboard', 'action-items', 'complete', 'toast', 'undo']);
    const state = await fetchSeedState(request);
    const today = isoDateWithOffset(0);

    await login(page, state.owner.email, state.owner.password);
    await patchTask(page, state.task.id, { due_at: today, due_mode: 'date', status: 'open', status_mode: 'single' });
    await page.goto('/dashboard');

    await page.locator('.dashboard-action-title', { hasText: 'Realtime Task' }).click();
    await expect(page.locator('#discussion-drawer')).toHaveClass(/open/);
    await page.locator('#task-settings-status').click();
    await expect(page.locator('#status-menu')).toBeVisible();
    await page.locator('#status-menu [data-status-option="complete"]').click();
    await expect(page.locator('.action-toast-title', { hasText: 'Task completed' })).toHaveCount(1);
    await expect(page.locator('.dashboard-action-title', { hasText: 'Realtime Task' })).toHaveCount(0);
    await steps.step('Mark Realtime Task complete from the dashboard drawer and verify the completion toast appears while the action item disappears.', page);

    await page.locator('.action-toast').filter({ hasText: 'Realtime Task' }).locator('button', { hasText: 'Undo' }).click();
    await expect(page.locator('.dashboard-action-title', { hasText: 'Realtime Task' })).toHaveCount(1);
    await steps.step('Click Undo on the completion toast and verify the task returns to the dashboard Action Items list.', page);
  });

  test('dashboard delete undo restores action item order', async ({ page, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['dashboard', 'action-items', 'delete', 'toast', 'undo', 'order']);
    const state = await fetchSeedState(request);
    const today = isoDateWithOffset(0);

    await login(page, state.owner.email, state.owner.password);
    const firstTask = await createTask(page, {
      project_id: state.project.id,
      group_id: state.group.id,
      title: 'Undo Order First',
      assignee_email: state.owner.email,
    });
    const secondTask = await createTask(page, {
      project_id: state.project.id,
      group_id: state.group.id,
      title: 'Undo Order Second',
      assignee_email: state.owner.email,
    });
    await patchTask(page, firstTask.id, { due_at: today, due_mode: 'date', status: 'open' });
    await patchTask(page, secondTask.id, { due_at: today, due_mode: 'date', status: 'open' });
    await page.goto('/dashboard');

    const actionTitles = page.locator('.dashboard-action-title');
    await expect(actionTitles.filter({ hasText: 'Undo Order First' })).toHaveCount(1);
    await expect(actionTitles.filter({ hasText: 'Undo Order Second' })).toHaveCount(1);

    const beforeOrder = await actionTitles.evaluateAll((nodes) =>
      nodes.map((node) => (node.textContent || '').trim()).filter((value) => value === 'Undo Order First' || value === 'Undo Order Second')
    );
    expect(beforeOrder).toHaveLength(2);
    expect(new Set(beforeOrder)).toEqual(new Set(['Undo Order First', 'Undo Order Second']));

    await page.locator('.dashboard-action-title', { hasText: 'Undo Order First' }).click();
    await page.locator('#task-settings-delete').click();
    await page.locator('.action-toast').filter({ hasText: 'Undo Order First' }).locator('button', { hasText: 'Undo' }).click();

    const afterOrder = await actionTitles.evaluateAll((nodes) =>
      nodes.map((node) => (node.textContent || '').trim()).filter((value) => value === 'Undo Order First' || value === 'Undo Order Second')
    );
    expect(afterOrder).toEqual(beforeOrder);
    await steps.step('Delete the first of two adjacent action items, undo it, and verify the original ordering is preserved.', page);
  });

  test('dashboard delete propagates to another dashboard window without refresh', async ({ browser, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['dashboard', 'socket', 'delete', 'toast']);
    const state = await fetchSeedState(request);
    const today = isoDateWithOffset(0);
    const ownerContextA = await browser.newContext();
    const ownerPageA = await ownerContextA.newPage();
    const ownerContextB = await browser.newContext();
    const ownerPageB = await ownerContextB.newPage();

    await login(ownerPageA, state.owner.email, state.owner.password);
    await login(ownerPageB, state.owner.email, state.owner.password);
    await patchTask(ownerPageA, state.task.id, { due_at: today, due_mode: 'date', status: 'open' });
    await ownerPageA.goto('/dashboard');
    await ownerPageB.goto('/dashboard');

    await expect(ownerPageA.locator('.dashboard-action-title', { hasText: 'Realtime Task' })).toHaveCount(1);
    await expect(ownerPageB.locator('.dashboard-action-title', { hasText: 'Realtime Task' })).toHaveCount(1);

    await ownerPageA.locator('.dashboard-action-title', { hasText: 'Realtime Task' }).click();
    await ownerPageA.locator('#task-settings-delete').click();
    const toast = ownerPageA.locator('.action-toast').filter({ hasText: 'Realtime Task' }).first();
    await expect(toast).toBeVisible();
    await toast.locator('button', { hasText: 'Dismiss' }).click();
    await expect(ownerPageA.locator('.dashboard-action-title', { hasText: 'Realtime Task' })).toHaveCount(0);
    await expect(ownerPageB.locator('.dashboard-action-title', { hasText: 'Realtime Task' })).toHaveCount(0);
    await steps.step('Dismiss a delete toast in one dashboard window and verify the task disappears in another dashboard window without a refresh.', ownerPageA);

    await ownerContextA.close();
    await ownerContextB.close();
  });

  test('prereq-blocked tasks render as readonly prereq status in tree todo and drawer', async ({ page, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['prereq', 'status', 'tree', 'todo', 'drawer']);
    const state = await fetchSeedState(request);
    const today = isoDateWithOffset(0);

    await login(page, state.owner.email, state.owner.password);
    await patchTask(page, state.task.id, { due_at: today, due_mode: 'date', status: 'open' });
    await patchTask(page, state.linked_todo_task.id, { status: 'open' });
    await createTaskPrerequisite(page, state.task.id, state.linked_todo_task.id);

    await page.goto(`/tree/project/${state.project.id}`);
    await waitForTreeProjectReady(page, state.project.id, state.task.id);
    const treeStatusCell = page.locator(`[data-task-row-id="${state.task.id}"] [data-status-cell="1"]`).first();
    await expect(treeStatusCell).toHaveAttribute('data-status-state', 'prereq');
    await expect(treeStatusCell).not.toHaveAttribute('role', 'button');
    await expect(treeStatusCell).not.toHaveAttribute('data-field', 'status');
    await expect(treeStatusCell).toHaveClass(/status-editable/);
    await steps.step('Verify the Tree row renders a readonly prereq-blocked status cell.', page);

    await page.goto('/todo');
    const todoStatusCell = page.locator(`.todo-item[data-task-id="${state.task.id}"] [data-status-cell="1"]`).first();
    await expect(todoStatusCell).toHaveAttribute('data-status-state', 'prereq');
    await expect(todoStatusCell).not.toHaveAttribute('role', 'button');
    await expect(todoStatusCell).not.toHaveAttribute('data-field', 'status');
    await expect(todoStatusCell).toHaveClass(/status-editable/);
    await steps.step('Verify the Todo row renders the same readonly prereq-blocked status treatment.', page);

    await page.goto(`/tree/project/${state.project.id}`);
    await page.locator(`[data-task-row-id="${state.task.id}"] [data-open-settings="${state.task.id}"]`).click();
    await expect(page.locator('#discussion-drawer')).toHaveClass(/open/);
    await expect(page.locator('#task-settings-status')).toBeHidden();
    await expect(page.locator('#task-settings-status-mode-picker')).toBeVisible();
    const drawerReadonly = page.locator('#task-settings-status-readonly');
    await expect(drawerReadonly).toBeVisible();
    await expect(drawerReadonly).toHaveAttribute('data-status-state', 'prereq');
    await steps.step('Verify the drawer hides the interactive status control, keeps the mode picker available, and shows a readonly prereq status indicator.', page);
  });

  test('poll task status badge opens the dialog and updates response counts live', async ({ browser, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['poll', 'status', 'dialog', 'socket']);
    const state = await fetchSeedState(request);
    const ownerContext = await browser.newContext();
    const memberContext = await browser.newContext();
    const ownerPage = await ownerContext.newPage();
    const memberPage = await memberContext.newPage();

    await login(ownerPage, state.owner.email, state.owner.password);
    await login(memberPage, state.member.email, state.member.password);

    const pollTask = await createTask(ownerPage, {
      project_id: state.project.id,
      group_id: state.group.id,
      title: 'Live Poll Task',
      assignee_email: state.owner.email,
    });
    await createAssignment(ownerPage, pollTask.id, state.member.email);
    await patchTask(ownerPage, pollTask.id, {
      task_type: 'poll',
      poll: {
        question: 'Choose a session',
        allows_multiple: false,
        results_visibility: 'everyone',
        options: [
          { id: 'session-a', label: 'Session A' },
          { id: 'session-b', label: 'Session B' },
        ],
      },
    });

    await ownerPage.goto(`/tree/project/${state.project.id}`);
    await memberPage.goto(`/tree/project/${state.project.id}`);
    await waitForTreeProjectReady(ownerPage, state.project.id, pollTask.id);
    await waitForTreeProjectReady(memberPage, state.project.id, pollTask.id);

    const ownerBadge = ownerPage.locator(`[data-task-row-id="${pollTask.id}"] [data-task-poll-trigger="${pollTask.id}"]`).first();
    await expect(ownerBadge).toContainText('0/2');
    await steps.step('Open the same poll task in two Tree views and verify the initial poll badge count is 0/2.', ownerPage);

    await openPollDialogFromTree(memberPage, pollTask.id);
    await expect(memberPage.locator('#poll-response-question')).toContainText('Choose a session');
    await submitPollResponse(memberPage, 'session-a');
    await steps.step('Respond to the poll from the member browser by selecting Session A.', memberPage);

    await expect(ownerBadge).toContainText('1/2');
    await steps.step('Verify the owner Tree view updates the poll badge live to 1/2 without a refresh.', ownerPage);

    await ownerContext.close();
    await memberContext.close();
  });

  test('poll results visible to everyone show responder avatars in the poll dialog', async ({ browser, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['poll', 'results', 'avatars', 'visibility']);
    const state = await fetchSeedState(request);
    const ownerContext = await browser.newContext();
    const memberContext = await browser.newContext();
    const ownerPage = await ownerContext.newPage();
    const memberPage = await memberContext.newPage();

    await login(ownerPage, state.owner.email, state.owner.password);
    await login(memberPage, state.member.email, state.member.password);

    const pollTask = await createTask(ownerPage, {
      project_id: state.project.id,
      group_id: state.group.id,
      title: 'Visible Poll Results Task',
      assignee_email: state.owner.email,
    });
    await createAssignment(ownerPage, pollTask.id, state.member.email);
    await patchTask(ownerPage, pollTask.id, {
      task_type: 'poll',
      poll: {
        question: 'Which venue works?',
        allows_multiple: false,
        results_visibility: 'everyone',
        options: [
          { id: 'venue-a', label: 'Venue A' },
          { id: 'venue-b', label: 'Venue B' },
        ],
      },
    });

    await memberPage.goto(`/tree/project/${state.project.id}`);
    await waitForTreeProjectReady(memberPage, state.project.id, pollTask.id);
    await openPollDialogFromTree(memberPage, pollTask.id);
    await submitPollResponse(memberPage, 'venue-a');

    await ownerPage.goto(`/tree/project/${state.project.id}`);
    await waitForTreeProjectReady(ownerPage, state.project.id, pollTask.id);
    await openPollDialogFromTree(ownerPage, pollTask.id);
    await expect(ownerPage.locator('#poll-response-options [data-poll-response-option="venue-a"] .poll-response-responder')).toHaveCount(1);
    await expect(ownerPage.locator('#poll-response-options [data-poll-response-option="venue-b"] .poll-response-responder')).toHaveCount(0);
    await steps.step('Open the poll as the owner and verify the selected option shows the responder avatar when results are visible to everyone.', ownerPage);

    await ownerContext.close();
    await memberContext.close();
  });

  test('poll results visible only to the creator hide responder avatars from non creators', async ({ browser, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['poll', 'results', 'visibility', 'creator-only']);
    const state = await fetchSeedState(request);
    const ownerContext = await browser.newContext();
    const memberContext = await browser.newContext();
    const ownerPage = await ownerContext.newPage();
    const memberPage = await memberContext.newPage();

    await login(ownerPage, state.owner.email, state.owner.password);
    await login(memberPage, state.member.email, state.member.password);

    const pollTask = await createTask(ownerPage, {
      project_id: state.project.id,
      group_id: state.group.id,
      title: 'Creator Only Poll Results Task',
      assignee_email: state.owner.email,
    });
    await createAssignment(ownerPage, pollTask.id, state.member.email);
    await patchTask(ownerPage, pollTask.id, {
      task_type: 'poll',
      poll: {
        question: 'Who can see this?',
        allows_multiple: false,
        results_visibility: 'creator',
        options: [
          { id: 'creator-a', label: 'Private Option A' },
          { id: 'creator-b', label: 'Private Option B' },
        ],
      },
    });

    await memberPage.goto(`/tree/project/${state.project.id}`);
    await waitForTreeProjectReady(memberPage, state.project.id, pollTask.id);
    await openPollDialogFromTree(memberPage, pollTask.id);
    await submitPollResponse(memberPage, 'creator-a');

    await ownerPage.goto(`/tree/project/${state.project.id}`);
    await waitForTreeProjectReady(ownerPage, state.project.id, pollTask.id);
    await openPollDialogFromTree(ownerPage, pollTask.id);
    await expect(ownerPage.locator('#poll-response-options [data-poll-response-option="creator-a"] .poll-response-responder')).toHaveCount(1);
    await steps.step('Open the creator-only poll as the owner and verify the responder avatar is visible to the creator.', ownerPage);

    await openPollDialogFromTree(memberPage, pollTask.id);
    await expect(memberPage.locator('#poll-response-options .poll-response-responder')).toHaveCount(0);
    await steps.step('Open the same poll as the non-creator and verify responder avatars are hidden.', memberPage);

    await ownerContext.close();
    await memberContext.close();
  });

  test('non-owner poll tasks keep the poll badge and dialog instead of the normal status menu', async ({ browser, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['poll', 'non-owner', 'status', 'tree']);
    const state = await fetchSeedState(request);
    const ownerContext = await browser.newContext();
    const memberContext = await browser.newContext();
    const ownerPage = await ownerContext.newPage();
    const memberPage = await memberContext.newPage();

    await login(ownerPage, state.owner.email, state.owner.password);
    await login(memberPage, state.member.email, state.member.password);

    const pollTask = await createTask(ownerPage, {
      project_id: state.project.id,
      group_id: state.group.id,
      title: 'Non Owner Poll Task',
      assignee_email: state.owner.email,
    });
    await createAssignment(ownerPage, pollTask.id, state.member.email);
    await patchTask(ownerPage, pollTask.id, {
      task_type: 'poll',
      poll: {
        question: 'Which time works?',
        allows_multiple: false,
        results_visibility: 'everyone',
        options: [
          { id: 'time-a', label: 'Time A' },
          { id: 'time-b', label: 'Time B' },
        ],
      },
    });

    await ownerPage.goto(`/tree/project/${state.project.id}`);
    await memberPage.goto(`/tree/project/${state.project.id}`);
    await waitForTreeProjectReady(ownerPage, state.project.id, pollTask.id);
    await waitForTreeProjectReady(memberPage, state.project.id, pollTask.id);

    await openPollDialogFromTree(ownerPage, pollTask.id);
    await submitPollResponse(ownerPage, 'time-a');
    await expect(ownerPage.locator(`[data-task-row-id="${pollTask.id}"] [data-task-poll-trigger="${pollTask.id}"]`).first()).toContainText('1/2');
    await steps.step('Respond to the poll as the owner so the shared poll count becomes 1/2.', ownerPage);

    const memberPollBadge = memberPage.locator(`[data-task-row-id="${pollTask.id}"] [data-task-poll-trigger="${pollTask.id}"]`).first();
    await expect(memberPollBadge).toHaveCount(1);
    await expect(memberPollBadge).toContainText('1/2');
    await expect(memberPage.locator(`[data-task-row-id="${pollTask.id}"] [data-field="status"]`)).toHaveCount(0);
    await steps.step('Verify the non-owner sees the same shared 1/2 poll count and no single-status control.', memberPage);

    await memberPollBadge.click();
    await expect(memberPage.locator('#poll-response-dialog')).toBeVisible();
    await expect(memberPage.locator('#status-menu')).toBeHidden();
    await expect(memberPage.locator('#poll-response-question')).toContainText('Which time works?');
    await steps.step('Click the non-owner poll badge and verify it opens the poll dialog instead of the normal status menu.', memberPage);

    await ownerContext.close();
    await memberContext.close();
  });

  test('blocked prereq status hover shows the prerequisite list in tree and todo', async ({ page, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['prereq', 'hover', 'tree', 'todo']);
    const state = await fetchSeedState(request);

    await login(page, state.owner.email, state.owner.password);
    const prerequisiteTask = await createTask(page, {
      project_id: state.project.id,
      group_id: state.group.id,
      title: 'Hover Source Task',
      assignee_email: state.owner.email,
    });
    const dependentTask = await createTask(page, {
      project_id: state.project.id,
      group_id: state.group.id,
      title: 'Hover Dependent Task',
      assignee_email: state.owner.email,
    });
    await createTaskPrerequisite(page, dependentTask.id, prerequisiteTask.id);

    await page.goto(`/tree/project/${state.project.id}`);
    await waitForTreeProjectReady(page, state.project.id, dependentTask.id);
    await expect(page.locator(`[data-task-row-id="${dependentTask.id}"] [data-status-cell="1"]`)).toHaveAttribute('data-status-state', 'prereq');
    await expectPrereqHoverCard(page, `[data-task-row-id="${dependentTask.id}"] [data-status-cell="1"]`, 'Hover Source Task');
    await steps.step('Hover the blocked Tree status badge and verify the prereq hover card shows the prerequisite task.', page);

    await page.goto('/todo');
    await expect(page.locator(`.todo-item[data-task-id="${dependentTask.id}"] [data-status-cell="1"]`)).toHaveAttribute('data-status-state', 'prereq');
    await expectPrereqHoverCard(page, `.todo-item[data-task-id="${dependentTask.id}"] [data-status-cell="1"]`, 'Hover Source Task');
    await steps.step('Hover the blocked Todo status badge and verify the same prereq hover card appears there as well.', page);
  });

  test('adding and removing a prerequisite updates the dependent live in tree', async ({ browser, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['prereq', 'socket', 'tree', 'add-remove']);
    const state = await fetchSeedState(request);
    const ownerContext = await browser.newContext();
    const memberContext = await browser.newContext();
    const ownerPage = await ownerContext.newPage();
    const memberPage = await memberContext.newPage();

    await login(ownerPage, state.owner.email, state.owner.password);
    await login(memberPage, state.member.email, state.member.password);

    const prerequisiteTask = await createTask(memberPage, {
      project_id: state.project.id,
      group_id: state.group.id,
      title: 'Live Prerequisite Source',
      assignee_email: state.owner.email,
    });
    const dependentTask = await createTask(memberPage, {
      project_id: state.project.id,
      group_id: state.group.id,
      title: 'Live Dependent Task',
      assignee_email: state.owner.email,
    });

    await ownerPage.goto(`/tree/project/${state.project.id}`);
    await memberPage.goto(`/tree/project/${state.project.id}`);
    await waitForTreeProjectReady(ownerPage, state.project.id, dependentTask.id);
    await waitForTreeProjectReady(memberPage, state.project.id, dependentTask.id);
    await expect(ownerPage.locator(`[data-task-row-id="${dependentTask.id}"]`)).toHaveAttribute('data-status-state', 'open');
    await steps.step('Open the same Tree project in both browsers with a fresh prerequisite source task and dependent task starting as open.', ownerPage);

    const prerequisiteRow = await createTaskPrerequisite(memberPage, dependentTask.id, prerequisiteTask.id);
    await expect(ownerPage.locator(`[data-task-row-id="${dependentTask.id}"]`)).toHaveAttribute('data-status-state', 'prereq');
    await expect(ownerPage.locator(`[data-task-row-id="${dependentTask.id}"] [data-status-cell="1"]`)).toHaveAttribute('data-status-state', 'prereq');
    await focusTreeTask(ownerPage, dependentTask.id, 'Dependent task after prerequisite add', ['The dependent should switch into prereq-blocked state without a refresh.']);
    await steps.step('Add the prerequisite in one browser and verify the dependent task turns prereq-blocked live in the other browser.', ownerPage);

    await deleteTaskPrerequisite(memberPage, prerequisiteRow.id);
    await expect(ownerPage.locator(`[data-task-row-id="${dependentTask.id}"]`)).toHaveAttribute('data-status-state', 'open');
    await expect(ownerPage.locator(`[data-task-row-id="${dependentTask.id}"] [data-status-cell="1"]`)).toHaveAttribute('data-status-state', 'open');
    await focusTreeTask(ownerPage, dependentTask.id, 'Dependent task after prerequisite removal', ['Removing the prerequisite should restore the normal open status live.']);
    await steps.step('Remove the prerequisite in one browser and verify the dependent task returns to normal open state live in the other browser.', ownerPage);

    await ownerContext.close();
    await memberContext.close();
  });

  test('prerequisite completion updates dependent status live in tree', async ({ browser, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['prereq', 'socket', 'tree', 'status-propagation']);
    const state = await fetchSeedState(request);
    const ownerContext = await browser.newContext();
    const memberContext = await browser.newContext();
    const ownerPage = await ownerContext.newPage();
    const memberPage = await memberContext.newPage();

    await login(ownerPage, state.owner.email, state.owner.password);
    await login(memberPage, state.member.email, state.member.password);

    const prerequisiteTask = await createTask(memberPage, {
      project_id: state.project.id,
      group_id: state.group.id,
      title: 'Status Source Task',
      assignee_email: state.owner.email,
    });
    const dependentTask = await createTask(memberPage, {
      project_id: state.project.id,
      group_id: state.group.id,
      title: 'Status Dependent Task',
      assignee_email: state.owner.email,
    });
    await createTaskPrerequisite(memberPage, dependentTask.id, prerequisiteTask.id);

    await ownerPage.goto(`/tree/project/${state.project.id}`);
    await memberPage.goto(`/tree/project/${state.project.id}`);
    await waitForTreeProjectReady(ownerPage, state.project.id, dependentTask.id);
    await waitForTreeProjectReady(memberPage, state.project.id, dependentTask.id);
    await expect(ownerPage.locator(`[data-task-row-id="${dependentTask.id}"]`)).toHaveAttribute('data-status-state', 'prereq');
    await steps.step('Start both browsers on the same Tree project with a dependent task blocked by an incomplete prerequisite.', ownerPage);

    await patchTask(memberPage, prerequisiteTask.id, { status: 'complete' });
    await expect(ownerPage.locator(`[data-task-row-id="${dependentTask.id}"]`)).toHaveAttribute('data-status-state', 'open');
    await expect(ownerPage.locator(`[data-task-row-id="${dependentTask.id}"] [data-status-cell="1"]`)).toHaveAttribute('data-status-state', 'open');
    await focusTreeTask(ownerPage, dependentTask.id, 'Dependent task after prerequisite completion', ['Completing the prerequisite should unblock the dependent immediately.']);
    await steps.step('Complete the prerequisite in one browser and verify the dependent task unblocks live in the other browser.', ownerPage);

    await patchTask(memberPage, prerequisiteTask.id, { status: 'open' });
    await expect(ownerPage.locator(`[data-task-row-id="${dependentTask.id}"]`)).toHaveAttribute('data-status-state', 'prereq');
    await expect(ownerPage.locator(`[data-task-row-id="${dependentTask.id}"] [data-status-cell="1"]`)).toHaveAttribute('data-status-state', 'prereq');
    await focusTreeTask(ownerPage, dependentTask.id, 'Dependent task after prerequisite reopen', ['Reopening the prerequisite should block the dependent again live.']);
    await steps.step('Reopen the prerequisite and verify the dependent task returns to prereq-blocked live in the other browser.', ownerPage);

    await ownerContext.close();
    await memberContext.close();
  });

  test('recursive prerequisite changes propagate to downstream dependents live', async ({ browser, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['prereq', 'socket', 'tree', 'recursive']);
    const state = await fetchSeedState(request);
    const ownerContext = await browser.newContext();
    const memberContext = await browser.newContext();
    const ownerPage = await ownerContext.newPage();
    const memberPage = await memberContext.newPage();

    await login(ownerPage, state.owner.email, state.owner.password);
    await login(memberPage, state.member.email, state.member.password);

    const taskA = await createTask(memberPage, {
      project_id: state.project.id,
      group_id: state.group.id,
      title: 'Chain Task A',
      assignee_email: state.owner.email,
    });
    const taskB = await createTask(memberPage, {
      project_id: state.project.id,
      group_id: state.group.id,
      title: 'Chain Task B',
      assignee_email: state.owner.email,
    });
    const taskC = await createTask(memberPage, {
      project_id: state.project.id,
      group_id: state.group.id,
      title: 'Chain Task C',
      assignee_email: state.owner.email,
    });
    await createTaskPrerequisite(memberPage, taskB.id, taskA.id);
    await createTaskPrerequisite(memberPage, taskC.id, taskB.id);

    await ownerPage.goto(`/tree/project/${state.project.id}`);
    await memberPage.goto(`/tree/project/${state.project.id}`);
    await waitForTreeProjectReady(ownerPage, state.project.id, taskC.id);
    await waitForTreeProjectReady(memberPage, state.project.id, taskC.id);

    await expect(ownerPage.locator(`[data-task-row-id="${taskB.id}"]`)).toHaveAttribute('data-status-state', 'prereq');
    await expect(ownerPage.locator(`[data-task-row-id="${taskC.id}"]`)).toHaveAttribute('data-status-state', 'prereq');
    await steps.step('Start with a three-task dependency chain where both downstream tasks begin blocked.', ownerPage);

    await patchTask(memberPage, taskA.id, { status: 'complete' });
    await expect(ownerPage.locator(`[data-task-row-id="${taskB.id}"]`)).toHaveAttribute('data-status-state', 'open');
    await expect(ownerPage.locator(`[data-task-row-id="${taskC.id}"]`)).toHaveAttribute('data-status-state', 'prereq');
    await focusTreeTask(ownerPage, taskB.id, 'Middle task after upstream completion', ['Task B should unblock once Task A completes.']);
    await focusTreeTask(ownerPage, taskC.id, 'Downstream task remains blocked', ['Task C should stay blocked because Task B is still incomplete.']);
    await steps.step('Complete the root prerequisite and verify the middle task unblocks while the downstream task stays blocked.', ownerPage);

    await patchTask(memberPage, taskB.id, { status: 'complete' });
    await expect(ownerPage.locator(`[data-task-row-id="${taskC.id}"]`)).toHaveAttribute('data-status-state', 'open');
    await expect(ownerPage.locator(`[data-task-row-id="${taskC.id}"] [data-status-cell="1"]`)).toHaveAttribute('data-status-state', 'open');
    await focusTreeTask(ownerPage, taskC.id, 'Downstream task after middle completion', ['Task C should unblock once Task B completes.']);
    await steps.step('Complete the middle task and verify the downstream dependent unblocks live as well.', ownerPage);

    await ownerContext.close();
    await memberContext.close();
  });

  test('deleting a prerequisite task clears dependent blocked status live', async ({ browser, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['prereq', 'socket', 'tree', 'delete-cleanup']);
    const state = await fetchSeedState(request);
    const ownerContext = await browser.newContext();
    const memberContext = await browser.newContext();
    const ownerPage = await ownerContext.newPage();
    const memberPage = await memberContext.newPage();

    await login(ownerPage, state.owner.email, state.owner.password);
    await login(memberPage, state.member.email, state.member.password);

    const prerequisiteTask = await createTask(memberPage, {
      project_id: state.project.id,
      group_id: state.group.id,
      title: 'Delete Source Task',
      assignee_email: state.owner.email,
    });
    const dependentTask = await createTask(memberPage, {
      project_id: state.project.id,
      group_id: state.group.id,
      title: 'Delete Dependent Task',
      assignee_email: state.owner.email,
    });
    await createTaskPrerequisite(memberPage, dependentTask.id, prerequisiteTask.id);

    await ownerPage.goto(`/tree/project/${state.project.id}`);
    await memberPage.goto(`/tree/project/${state.project.id}`);
    await waitForTreeProjectReady(ownerPage, state.project.id, dependentTask.id);
    await waitForTreeProjectReady(memberPage, state.project.id, dependentTask.id);
    await expect(ownerPage.locator(`[data-task-row-id="${dependentTask.id}"]`)).toHaveAttribute('data-status-state', 'prereq');
    await steps.step('Start with a dependent task blocked by a prerequisite task that still exists in the Tree board.', ownerPage);

    await deleteTask(memberPage, prerequisiteTask.id);
    await expect(ownerPage.locator(`[data-task-row-id="${prerequisiteTask.id}"]`)).toHaveCount(0);
    await expect(ownerPage.locator(`[data-task-row-id="${dependentTask.id}"]`)).toHaveAttribute('data-status-state', 'open');
    await expect(ownerPage.locator(`[data-task-row-id="${dependentTask.id}"] [data-status-cell="1"]`)).toHaveAttribute('data-status-state', 'open');
    await focusTreeTask(ownerPage, dependentTask.id, 'Dependent task after prerequisite deletion', ['Deleting the prerequisite task should clear the blocked state live.']);
    await steps.step('Delete the prerequisite task in one browser and verify the blocked dependent task immediately returns to normal open state in the other browser.', ownerPage);

    await ownerContext.close();
    await memberContext.close();
  });

  test('adding and removing a prerequisite updates the dependent live in todo', async ({ browser, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['prereq', 'socket', 'todo', 'add-remove']);
    const state = await fetchSeedState(request);
    const ownerContext = await browser.newContext();
    const memberContext = await browser.newContext();
    const ownerPage = await ownerContext.newPage();
    const memberPage = await memberContext.newPage();

    await login(ownerPage, state.owner.email, state.owner.password);
    await login(memberPage, state.member.email, state.member.password);

    const prerequisiteTask = await createTask(memberPage, {
      project_id: state.project.id,
      group_id: state.group.id,
      title: 'Todo Prerequisite Source',
      assignee_email: state.owner.email,
    });
    const dependentTask = await createTask(memberPage, {
      project_id: state.project.id,
      group_id: state.group.id,
      title: 'Todo Dependent Task',
      assignee_email: state.owner.email,
    });

    await ownerPage.goto('/todo');
    await memberPage.goto('/todo');
    await expect(ownerPage.locator(`.todo-item[data-task-id="${dependentTask.id}"]`)).toHaveCount(1);
    await expect(memberPage.locator(`.todo-item[data-task-id="${dependentTask.id}"]`)).toHaveCount(1);
    await expect(ownerPage.locator(`.todo-item[data-task-id="${dependentTask.id}"]`)).toHaveAttribute('data-status-state', 'open');
    await steps.step('Open Todo in both browsers with a fresh dependent task starting in the normal open state.', ownerPage);

    const prerequisiteRow = await createTaskPrerequisite(memberPage, dependentTask.id, prerequisiteTask.id);
    await expect(ownerPage.locator(`.todo-item[data-task-id="${dependentTask.id}"]`)).toHaveAttribute('data-status-state', 'prereq');
    await expect(ownerPage.locator(`.todo-item[data-task-id="${dependentTask.id}"] [data-status-cell="1"]`)).toHaveAttribute('data-status-state', 'prereq');
    await focusTodoTask(ownerPage, dependentTask.id, 'Todo dependent after prerequisite add', ['The Todo row should turn prereq-blocked without a refresh.']);
    await steps.step('Add the prerequisite in one browser and verify the dependent Todo row turns prereq-blocked live in the other browser.', ownerPage);

    await deleteTaskPrerequisite(memberPage, prerequisiteRow.id);
    await expect(ownerPage.locator(`.todo-item[data-task-id="${dependentTask.id}"]`)).toHaveAttribute('data-status-state', 'open');
    await expect(ownerPage.locator(`.todo-item[data-task-id="${dependentTask.id}"] [data-status-cell="1"]`)).toHaveAttribute('data-status-state', 'open');
    await focusTodoTask(ownerPage, dependentTask.id, 'Todo dependent after prerequisite removal', ['Removing the prerequisite should restore the Todo row to open live.']);
    await steps.step('Remove the prerequisite in one browser and verify the dependent Todo row returns to open live in the other browser.', ownerPage);

    await ownerContext.close();
    await memberContext.close();
  });

  test('prerequisite completion updates dependent status live in todo', async ({ browser, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['prereq', 'socket', 'todo', 'status-propagation']);
    const state = await fetchSeedState(request);
    const ownerContext = await browser.newContext();
    const memberContext = await browser.newContext();
    const ownerPage = await ownerContext.newPage();
    const memberPage = await memberContext.newPage();

    await login(ownerPage, state.owner.email, state.owner.password);
    await login(memberPage, state.member.email, state.member.password);

    const prerequisiteTask = await createTask(memberPage, {
      project_id: state.project.id,
      group_id: state.group.id,
      title: 'Todo Status Source Task',
      assignee_email: state.owner.email,
    });
    const dependentTask = await createTask(memberPage, {
      project_id: state.project.id,
      group_id: state.group.id,
      title: 'Todo Status Dependent Task',
      assignee_email: state.owner.email,
    });
    await createTaskPrerequisite(memberPage, dependentTask.id, prerequisiteTask.id);

    await ownerPage.goto('/todo');
    await memberPage.goto('/todo');
    await expect(ownerPage.locator(`.todo-item[data-task-id="${dependentTask.id}"]`)).toHaveAttribute('data-status-state', 'prereq');
    await expect(memberPage.locator(`.todo-item[data-task-id="${dependentTask.id}"]`)).toHaveAttribute('data-status-state', 'prereq');
    await steps.step('Open Todo in both browsers with a dependent task initially blocked by an incomplete prerequisite.', ownerPage);

    await patchTask(memberPage, prerequisiteTask.id, { status: 'complete' });
    await expect(ownerPage.locator(`.todo-item[data-task-id="${dependentTask.id}"]`)).toHaveAttribute('data-status-state', 'open');
    await expect(ownerPage.locator(`.todo-item[data-task-id="${dependentTask.id}"] [data-status-cell="1"]`)).toHaveAttribute('data-status-state', 'open');
    await focusTodoTask(ownerPage, dependentTask.id, 'Todo dependent after prerequisite completion', ['Completing the prerequisite should unblock the Todo row immediately.']);
    await steps.step('Complete the prerequisite in one browser and verify the dependent Todo row unblocks live in the other browser.', ownerPage);

    await patchTask(memberPage, prerequisiteTask.id, { status: 'open' });
    await expect(ownerPage.locator(`.todo-item[data-task-id="${dependentTask.id}"]`)).toHaveAttribute('data-status-state', 'prereq');
    await expect(ownerPage.locator(`.todo-item[data-task-id="${dependentTask.id}"] [data-status-cell="1"]`)).toHaveAttribute('data-status-state', 'prereq');
    await focusTodoTask(ownerPage, dependentTask.id, 'Todo dependent after prerequisite reopen', ['Reopening the prerequisite should block the Todo row again live.']);
    await steps.step('Reopen the prerequisite and verify the dependent Todo row returns to prereq-blocked live in the other browser.', ownerPage);

    await ownerContext.close();
    await memberContext.close();
  });

  test('open drawer updates live when prerequisites are added and cleared remotely', async ({ browser, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['prereq', 'socket', 'drawer', 'live-update']);
    const state = await fetchSeedState(request);
    const ownerContext = await browser.newContext();
    const memberContext = await browser.newContext();
    const ownerPage = await ownerContext.newPage();
    const memberPage = await memberContext.newPage();

    await login(ownerPage, state.owner.email, state.owner.password);
    await login(memberPage, state.member.email, state.member.password);

    const prerequisiteTask = await createTask(memberPage, {
      project_id: state.project.id,
      group_id: state.group.id,
      title: 'Drawer Prerequisite Source',
      assignee_email: state.owner.email,
    });
    const dependentTask = await createTask(memberPage, {
      project_id: state.project.id,
      group_id: state.group.id,
      title: 'Drawer Dependent Task',
      assignee_email: state.owner.email,
    });

    await ownerPage.goto(`/tree/project/${state.project.id}`);
    await memberPage.goto(`/tree/project/${state.project.id}`);
    await waitForTreeProjectReady(ownerPage, state.project.id, dependentTask.id);
    await waitForTreeProjectReady(memberPage, state.project.id, dependentTask.id);

    await ownerPage.locator(`[data-task-row-id="${dependentTask.id}"] [data-open-settings="${dependentTask.id}"]`).click();
    await expect(ownerPage.locator('#discussion-drawer')).toHaveClass(/open/);
    await expect(ownerPage.locator('#task-settings-status')).toBeVisible();
    await expect(ownerPage.locator('#task-settings-status-readonly')).toBeHidden();
    await expect(ownerPage.locator('#task-settings-prerequisites')).not.toContainText('Drawer Prerequisite Source');
    await steps.step('Open the dependent task drawer before any prerequisites exist, with the normal status control still visible.', ownerPage);

    const prerequisiteRow = await createTaskPrerequisite(memberPage, dependentTask.id, prerequisiteTask.id);
    const readonlyStatus = ownerPage.locator('#task-settings-status-readonly');
    await expect(readonlyStatus).toBeVisible();
    await expect(readonlyStatus).toHaveAttribute('data-status-state', 'prereq');
    await expect(ownerPage.locator('#task-settings-status')).toBeHidden();
    await expect(ownerPage.locator('#task-settings-prerequisites')).toContainText('Drawer Prerequisite Source');
    await steps.step('Add the prerequisite remotely and verify the open drawer switches into readonly prereq-blocked state and lists the prerequisite live.', ownerPage);

    await patchTask(memberPage, prerequisiteTask.id, { status: 'complete' });
    await expect(ownerPage.locator('#task-settings-status-readonly')).toBeHidden();
    await expect(ownerPage.locator('#task-settings-status')).toBeVisible();
    await expect(ownerPage.locator('#task-settings-prerequisites')).toContainText('Drawer Prerequisite Source');
    await steps.step('Complete the prerequisite remotely and verify the open drawer unlocks the normal status control while keeping the prerequisite list visible.', ownerPage);

    await deleteTaskPrerequisite(memberPage, prerequisiteRow.id);
    await expect(ownerPage.locator('#task-settings-prerequisites')).not.toContainText('Drawer Prerequisite Source');
    await expect(ownerPage.locator('#task-settings-status')).toBeVisible();
    await expect(ownerPage.locator('#task-settings-status-readonly')).toBeHidden();
    await steps.step('Remove the prerequisite remotely and verify the open drawer removes the prerequisite row and stays on the normal status control.', ownerPage);

    await ownerContext.close();
    await memberContext.close();
  });

  test('blocked prereq hover updates live after remote prerequisite add in tree', async ({ browser, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['prereq', 'hover', 'socket', 'tree']);
    const state = await fetchSeedState(request);
    const ownerContext = await browser.newContext();
    const memberContext = await browser.newContext();
    const ownerPage = await ownerContext.newPage();
    const memberPage = await memberContext.newPage();

    await login(ownerPage, state.owner.email, state.owner.password);
    await login(memberPage, state.member.email, state.member.password);

    const prerequisiteTask = await createTask(memberPage, {
      project_id: state.project.id,
      group_id: state.group.id,
      title: 'Hover Live Source',
      assignee_email: state.owner.email,
    });
    const dependentTask = await createTask(memberPage, {
      project_id: state.project.id,
      group_id: state.group.id,
      title: 'Hover Live Dependent',
      assignee_email: state.owner.email,
    });

    await ownerPage.goto(`/tree/project/${state.project.id}`);
    await memberPage.goto(`/tree/project/${state.project.id}`);
    await waitForTreeProjectReady(ownerPage, state.project.id, dependentTask.id);
    await waitForTreeProjectReady(memberPage, state.project.id, dependentTask.id);
    await expect(ownerPage.locator(`[data-task-row-id="${dependentTask.id}"] [data-status-cell="1"]`)).toHaveAttribute('data-status-state', 'open');
    await steps.step('Open the same Tree board in both browsers with a dependent task that starts unblocked.', ownerPage);

    const prerequisiteRow = await createTaskPrerequisite(memberPage, dependentTask.id, prerequisiteTask.id);
    const ownerStatusCell = ownerPage.locator(`[data-task-row-id="${dependentTask.id}"] [data-status-cell="1"]`).first();
    await expect(ownerStatusCell).toHaveAttribute('data-status-state', 'prereq');
    await expectPrereqHoverCard(ownerPage, `[data-task-row-id="${dependentTask.id}"] [data-status-cell="1"]`, 'Hover Live Source');
    await steps.step('Add the prerequisite remotely and verify the blocked Tree status hover card appears with the new prerequisite.', ownerPage);

    await deleteTaskPrerequisite(memberPage, prerequisiteRow.id);
    await expect(ownerStatusCell).toHaveAttribute('data-status-state', 'open');
    await steps.step('Remove the prerequisite remotely and verify the Tree row itself returns to open after the hover-based blocked state had been exercised.', ownerPage);

    await ownerContext.close();
    await memberContext.close();
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

  test('multi-status host does not pick up single-status classes after my-status changes', async ({ page, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['tree', 'todo', 'status', 'multi-status', 'regression']);
    const state = await fetchSeedState(request);

    await login(page, state.owner.email, state.owner.password);
    await patchTask(page, state.task.id, { status_mode: 'multi' });

    await page.goto(`/tree/project/${state.project.id}`);
    await waitForTreeProjectReady(page, state.project.id, state.task.id);
    const treeMultiButton = page.locator(`[data-task-row-id="${state.task.id}"] [data-task-status-host="${state.task.id}"] .task-status-multi-button`).first();
    await expect(treeMultiButton).toHaveCount(1);
    await expect(treeMultiButton).not.toHaveClass(/status-editable/);
    await expect(treeMultiButton).not.toHaveClass(/editable/);

    await patchTask(page, state.task.id, { user_status: 'complete', status_user_id: state.owner.id });
    await expect(treeMultiButton).toHaveCount(1);
    await expect(treeMultiButton).not.toHaveClass(/status-editable/);
    await expect(treeMultiButton).not.toHaveClass(/editable/);
    await steps.step('Switch the seeded task into multi-status mode, change only the viewer status, and verify the Tree multi-status button does not pick up single-status classes.', page);

    await page.goto('/todo');
    const todoMultiButton = page.locator(`.todo-item[data-task-id="${state.task.id}"] [data-task-status-host="${state.task.id}"] .task-status-multi-button`).first();
    await expect(todoMultiButton).toHaveCount(1);
    await expect(todoMultiButton).not.toHaveClass(/status-editable/);
    await expect(todoMultiButton).not.toHaveClass(/editable/);
    await steps.step('Open Todo and verify the same multi-status host stays a proper multi-status button there as well.', page);
  });

  test('tree shows project and group descriptions in the board layout', async ({ page, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['tree', 'descriptions', 'project', 'group']);
    const state = await fetchSeedState(request);

    await login(page, state.owner.email, state.owner.password);
    await page.goto(`/tree/project/${state.project.id}`);
    await waitForTreeProjectReady(page, state.project.id, state.task.id);

    await expectContainsTextWithFailure(
      page,
      test.info(),
      `[data-tree-project-board="${state.project.id}"] [data-project-description="${state.project.id}"]`,
      'Realtime project description for the tree board.',
      'tree-project-description-visible',
      'The project description should render below the project header and above the mode tabs.'
    );
    await expectContainsTextWithFailure(
      page,
      test.info(),
      `.group-block[data-group-id="${state.group.id}"] [data-group-description="${state.group.id}"]`,
      'Realtime group description for the tree board.',
      'tree-group-description-visible',
      'The group description should render below the group title and above the task table.'
    );
    await steps.step('Open Tree on the seeded realtime project and verify both the project and group descriptions are visible in the board layout.', page);
  });

  test('tree group description updates live for another user', async ({ browser, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['socket', 'tree', 'group', 'description']);
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

    await expectContainsTextWithFailure(
      ownerPage,
      test.info(),
      `.group-block[data-group-id="${state.group.id}"] [data-group-description="${state.group.id}"]`,
      'Realtime group description for the tree board.',
      'tree-group-description-initial-owner',
      'The receiving browser should start with the seeded group description.'
    );

    await patchGroup(memberPage, state.group.id, {
      description: 'Live group description updated from another browser.',
      description_format: 'markdown',
    });
    await annotateLocator(
      memberPage,
      `.group-block[data-group-id="${state.group.id}"] [data-group-description="${state.group.id}"]`,
      'Member updated group description',
      ['The new group description should show immediately in the editing browser.']
    );
    await steps.multiStep('In the member browser, update the group description for the realtime group.', [
      { name: 'member', page: memberPage },
    ]);

    await expectContainsTextWithFailure(
      ownerPage,
      test.info(),
      `.group-block[data-group-id="${state.group.id}"] [data-group-description="${state.group.id}"]`,
      'Live group description updated from another browser.',
      'tree-group-description-live-owner',
      'The receiving browser should show the updated group description without a refresh.'
    );
    await annotateLocator(
      ownerPage,
      `.group-block[data-group-id="${state.group.id}"] [data-group-description="${state.group.id}"]`,
      'Owner received live group description update',
      ['The updated group description should appear without refreshing the Tree page.']
    );
    await steps.multiStep('Verify the owner browser receives the new group description immediately over realtime updates.', [
      { name: 'owner', page: ownerPage },
    ]);

    await ownerContext.close();
    await memberContext.close();
  });

  test('project context menu applies a group template', async ({ page, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['templates', 'groups', 'tree', 'settings']);
    const state = await fetchSeedState(request);

    await login(page, state.owner.email, state.owner.password);
    await page.goto('/account?section=templates');
    await page.locator('[data-group-template-create-form] #group-template-title').fill('Semester Template');
    const createForm = page.locator('[data-group-template-create-form]');
    await createForm.locator('input[name="task_title"]').first().fill('Draft outline');
    await createForm.locator('textarea[name="task_description"]').first().fill('Write the **initial** outline.');
    await createForm.locator('[data-group-template-add-task]').click();
    await expect(createForm.locator('input[name="task_title"]')).toHaveCount(2);
    await createForm.locator('input[name="task_title"]').nth(1).fill('Review budget');
    await createForm.locator('textarea[name="task_description"]').nth(1).fill('Confirm the budget assumptions.');
    await createForm.locator('button[type="submit"]').click();
    const createdTemplateCard = page.locator('[data-group-template-card]').first();
    await expect(createdTemplateCard.locator('input[name="title"]')).toHaveValue('Semester Template');
    await expect(createdTemplateCard.locator('input[name="task_title"]').first()).toHaveValue('Draft outline');
    await expect(createdTemplateCard.locator('textarea[name="task_description"]').first()).toHaveValue('Write the **initial** outline.');
    await steps.step('Create a private group template in Settings with two task rows.', page);

    await page.goto(`/tree/project/${state.project.id}`);
    await waitForTreeProjectReady(page, state.project.id, state.task.id);
    const projectRow = page.locator(`[data-tree-project-row="${state.project.id}"]`).first();
    await projectRow.click({ button: 'right' });
    await expect(page.locator('#context-menu [data-action="add-group-template"]')).toBeVisible();
    await page.locator('#context-menu [data-action="add-group-template"]').click();
    await expect(page.locator('#group-template-modal')).toBeVisible();
    const applyResponsePromise = page.waitForResponse((response) => response.url().includes(`/api/projects/${state.project.id}/group-templates/`) && response.request().method() === 'POST');
    await page.locator('#group-template-list [data-apply-group-template]').filter({ hasText: 'Semester Template' }).click();
    const applyResponse = await applyResponsePromise;
    const applyPayload = await applyResponse.json();

    const templateGroup = page.locator('.group-block', {
      has: page.locator('.group-title-text', { hasText: 'Semester Template' }),
    }).first();
    await expect(templateGroup).toContainText('Draft outline');
    await expect(templateGroup).toContainText('Review budget');
    expect(applyPayload.tasks).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ title: 'Draft outline', description: 'Write the **initial** outline.' }),
        expect.objectContaining({ title: 'Review budget', description: 'Confirm the budget assumptions.' }),
      ])
    );
    await steps.step('Open the project context menu in Tree, apply the template, and verify the new group and its tasks render immediately.', page);
  });

  test('email collaborator invite accepts from the magic link page', async ({ page, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['collaborator', 'email', 'invite']);
    const state = await fetchSeedState(request);

    await page.goto(`/invites/${state.collaborator_invite.token}`);
    await expectContainsTextWithFailure(
      page,
      test.info(),
      '.panel',
      'Email Collaborator Invite',
      'collaborator-invite-page-task',
      'The invite landing page should show the task title for the emailed collaborator.'
    );
    await page.locator('button[name="action"][value="accept"]').click();
    await expectContainsTextWithFailure(
      page,
      test.info(),
      '.panel',
      'already accepted',
      'collaborator-invite-accepted',
      'Accepting the invite should transition the page into the accepted state.'
    );
    await expect(page.locator('a[href*="/collaborators/"]')).toHaveCount(1);
    await steps.step('Open the emailed invite link, accept it, and verify the invite page reflects the accepted state with a collaborator portal link.', page);
  });

  test('email collaborator portal can mark an email assignment complete and undo it', async ({ page, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['collaborator', 'email', 'portal']);
    const state = await fetchSeedState(request);

    await page.goto(`/collaborators/${state.collaborator.access_token}`);
    const collaboratorRow = page.locator(`[data-collab-task-row="${state.collaborator_assignment_task.id}"]`).first();
    await expectContainsTextWithFailure(
      page,
      test.info(),
      `[data-collab-task-row="${state.collaborator_assignment_task.id}"]`,
      'Email Collaborator Assignment',
      'collaborator-portal-task-visible',
      'The collaborator portal should show the email-only assignment row.'
    );
    await expect(collaboratorRow.locator('button[name="action"][value="complete"]')).toBeDisabled();

    await collaboratorRow.locator('button[name="action"][value="accept"]').click();
    await expect(collaboratorRow.locator('button[name="action"][value="complete"]')).toBeEnabled();
    await collaboratorRow.locator('button[name="action"][value="complete"]').click();
    await expect(collaboratorRow).toHaveClass(/complete/);
    await expect(collaboratorRow.locator('button[name="action"][value="accept"]')).toHaveCount(0);
    await expect(collaboratorRow.locator('button[name="action"][value="decline"]')).toHaveCount(0);
    await expect(collaboratorRow.locator('button[name="action"][value="uncomplete"]')).toHaveCount(1);

    await collaboratorRow.locator('button[name="action"][value="uncomplete"]').click();
    await expect(collaboratorRow).not.toHaveClass(/complete/);
    await expect(collaboratorRow.locator('button[name="action"][value="accept"]')).toHaveCount(1);
    await expect(collaboratorRow.locator('button[name="action"][value="decline"]')).toHaveCount(1);
    await expect(collaboratorRow.locator('button[name="action"][value="complete"]')).toHaveCount(1);
    await expect(collaboratorRow.locator('button[name="action"][value="complete"]')).toBeEnabled();
    await steps.step('Open the collaborator portal, confirm Mark complete is disabled until Accept is selected, then mark complete and verify Accept/Decline disappear until the task is uncompleted.', page);
  });

  test('email collaborator portal still loads after task description changes', async ({ browser, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['collaborator', 'email', 'portal', 'description']);
    const state = await fetchSeedState(request);
    const ownerContext = await browser.newContext();
    const ownerPage = await ownerContext.newPage();
    const collaboratorContext = await browser.newContext();
    const collaboratorPage = await collaboratorContext.newPage();

    await login(ownerPage, state.owner.email, state.owner.password);
    await patchTask(ownerPage, state.collaborator_assignment_task.id, {
      description: 'Updated plain text description for collaborator portal.\n\nSecond paragraph after edit.',
      description_format: 'plain',
    });
    await steps.step('As the owner, update the shared collaborator task description to plain text with multiple paragraphs.', ownerPage);

    await collaboratorPage.goto(`/collaborators/${state.collaborator.access_token}`);
    const collaboratorRowSelector = `[data-collab-task-row="${state.collaborator_assignment_task.id}"]`;
    await expectContainsTextWithFailure(
      collaboratorPage,
      test.info(),
      collaboratorRowSelector,
      'Email Collaborator Assignment',
      'collaborator-portal-description-task-visible',
      'The collaborator portal should still load the shared task row after the owner edits its description.'
    );
    await expectContainsTextWithFailure(
      collaboratorPage,
      test.info(),
      `${collaboratorRowSelector} .collab-description`,
      'Updated plain text description for collaborator portal.',
      'collaborator-portal-description-visible',
      'The collaborator portal should render the updated plain text description instead of failing to load.'
    );
    await steps.step('Open the collaborator portal and verify the task row and updated description both render after the owner edit.', collaboratorPage);

    await ownerContext.close();
    await collaboratorContext.close();
  });

  test('email collaborator portal shows ASAP for asap due mode', async ({ browser, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['collaborator', 'email', 'portal', 'due-mode']);
    const state = await fetchSeedState(request);
    const ownerContext = await browser.newContext();
    const ownerPage = await ownerContext.newPage();
    const collaboratorContext = await browser.newContext();
    const collaboratorPage = await collaboratorContext.newPage();

    await login(ownerPage, state.owner.email, state.owner.password);
    await patchTask(ownerPage, state.collaborator_assignment_task.id, {
      due_mode: 'asap',
      due_at: '',
    });
    await steps.step('As the owner, change the shared collaborator task due mode to ASAP.', ownerPage);

    await collaboratorPage.goto(`/collaborators/${state.collaborator.access_token}`);
    await expectContainsTextWithFailure(
      collaboratorPage,
      test.info(),
      `[data-collab-task-row="${state.collaborator_assignment_task.id}"] .collab-due`,
      'ASAP',
      'collaborator-portal-asap-visible',
      'The collaborator portal should show ASAP when the shared task due mode is ASAP.'
    );
    await steps.step('Open the collaborator portal and verify the task row shows ASAP instead of falling back to Created.', collaboratorPage);

    await ownerContext.close();
    await collaboratorContext.close();
  });

  test('email collaborator portal orders asap first, then dated, then undated by table order', async ({ page, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['collaborator', 'email', 'portal', 'ordering']);
    const state = await fetchSeedState(request);

    await page.goto(`/collaborators/${state.collaborator.access_token}`);
    const titles = await page.locator('[data-collab-task-row] .collab-title > span:first-child').allTextContents();
    expect(titles.indexOf('Email Collaborator ASAP')).toBeGreaterThanOrEqual(0);
    expect(titles.indexOf('Email Collaborator Due Soon')).toBeGreaterThanOrEqual(0);
    expect(titles.indexOf('Email Collaborator Assignment')).toBeGreaterThanOrEqual(0);
    expect(titles.indexOf('Email Collaborator Followup')).toBeGreaterThanOrEqual(0);
    expect(titles.indexOf('Email Collaborator ASAP')).toBeLessThan(titles.indexOf('Email Collaborator Due Soon'));
    expect(titles.indexOf('Email Collaborator Due Soon')).toBeLessThan(titles.indexOf('Email Collaborator Assignment'));
    expect(titles.indexOf('Email Collaborator Assignment')).toBeLessThan(titles.indexOf('Email Collaborator Followup'));
    await steps.step('Open the collaborator portal and verify tasks are ordered with ASAP first, then dated tasks, then undated tasks in project-table order.', page);
  });

  test('email collaborator portal reorders live after socket updates', async ({ browser, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['collaborator', 'email', 'portal', 'socket', 'ordering']);
    const state = await fetchSeedState(request);
    const ownerContext = await browser.newContext();
    const ownerPage = await ownerContext.newPage();
    const collaboratorContext = await browser.newContext();
    const collaboratorPage = await collaboratorContext.newPage();

    await login(ownerPage, state.owner.email, state.owner.password);
    await collaboratorPage.goto(`/collaborators/${state.collaborator.access_token}`);
    const initialTitles = await collaboratorPage.locator('[data-collab-task-row] .collab-title > span:first-child').allTextContents();
    expect(initialTitles.indexOf('Email Collaborator Due Soon')).toBeLessThan(initialTitles.indexOf('Email Collaborator Assignment'));
    await steps.step('Open the collaborator portal and confirm the dated task starts below the existing ASAP task but above undated tasks.', collaboratorPage);

    await patchTask(ownerPage, state.collaborator_dated_task.id, {
      due_mode: 'asap',
      due_at: '',
    });
    await collaboratorPage.waitForFunction(() => {
      const titles = Array.from(document.querySelectorAll('[data-collab-task-row] .collab-title > span:first-child')).map((node) => (node.textContent || '').trim());
      return titles[0] === 'Email Collaborator ASAP' && titles[1] === 'Email Collaborator Due Soon';
    });
    const updatedTitles = await collaboratorPage.locator('[data-collab-task-row] .collab-title > span:first-child').allTextContents();
    expect(updatedTitles.indexOf('Email Collaborator Due Soon')).toBeLessThan(updatedTitles.indexOf('Email Collaborator Assignment'));
    await steps.step('Change the dated task to ASAP in the owner browser and verify the collaborator portal reorders it immediately after the existing ASAP row.', collaboratorPage);

    await ownerContext.close();
    await collaboratorContext.close();
  });

  test('email collaborator actions notify the owner dashboard', async ({ browser, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['collaborator', 'email', 'notifications', 'dashboard']);
    const state = await fetchSeedState(request);
    const ownerContext = await browser.newContext();
    const ownerPage = await ownerContext.newPage();
    const collaboratorContext = await browser.newContext();
    const collaboratorPage = await collaboratorContext.newPage();

    await login(ownerPage, state.owner.email, state.owner.password);
    await ownerPage.waitForURL('**/dashboard');
    const initialNotificationState = await fetchNotificationState(ownerPage);
    expect(initialNotificationState.ok).toBeTruthy();
    const initialRegularCount = Number((initialNotificationState.data && initialNotificationState.data.unread_regular_total) || 0);
    await steps.step('Open the owner dashboard and capture the starting unread regular notification count.', ownerPage);

    await collaboratorPage.goto(`/collaborators/${state.collaborator.access_token}`);
    const collaboratorRow = collaboratorPage.locator(`[data-collab-task-row="${state.collaborator_asap_task.id}"]`).first();
    await collaboratorRow.locator('button[name="action"][value="accept"]').click();
    await collaboratorRow.locator('button[name="action"][value="complete"]').click();
    await steps.step('In the email collaborator portal, accept and complete the ASAP collaborator task.', collaboratorPage);

    await waitForRegularNotificationCount(ownerPage, initialRegularCount + 1);
    await ownerPage.locator('[data-notification-kind="regular"] [data-notifications-trigger]').click();
    await expectContainsTextWithFailure(
      ownerPage,
      test.info(),
      '[data-notification-kind="regular"] .notifications-menu',
      'Email Collaborator ASAP',
      'email-collaborator-owner-notification-task',
      'The owner should receive a regular notification mentioning the email collaborator task title.'
    );
    await expectContainsTextWithFailure(
      ownerPage,
      test.info(),
      '[data-notification-kind="regular"] .notifications-menu',
      'Email Collaborator',
      'email-collaborator-owner-notification-actor',
      'The owner notification menu should identify the email collaborator actor.'
    );
    await steps.step('Verify the owner dashboard receives a new regular notification for the collaborator completion, including the task title and actor name.', ownerPage);

    await ownerContext.close();
    await collaboratorContext.close();
  });

  test('converted collaborator account actions notify the owner and render as account badges', async ({ browser, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['collaborator', 'conversion', 'notifications', 'tree', 'assignments']);
    const state = await fetchSeedState(request);

    const ownerContext = await browser.newContext();
    const ownerPage = await ownerContext.newPage();
    const collaboratorContext = await browser.newContext();
    const collaboratorPage = await collaboratorContext.newPage();

    await login(ownerPage, state.owner.email, state.owner.password);
    await ownerPage.waitForURL('**/dashboard');
    await collaboratorPage.goto(`/collaborators/${state.collaborator.access_token}`);
    const portalRow = collaboratorPage.locator(`[data-collab-task-row="${state.collaborator_assignment_task.id}"]`).first();
    await portalRow.locator('button[name="action"][value="accept"]').click();
    await portalRow.locator('button[name="action"][value="complete"]').click();
    await steps.step('Start from the email-only collaborator state and complete one shared task in the collaborator portal.', collaboratorPage);

    const conversion = await convertCollaborator(request);
    const convertedUser = conversion.user;
    expect(convertedUser).toBeTruthy();
    await steps.step('Use the e2e conversion helper to convert the collaborator email into a full account and migrate assignment state.', ownerPage);

    const ownerInitialNotificationState = await fetchNotificationState(ownerPage);
    expect(ownerInitialNotificationState.ok).toBeTruthy();
    const ownerInitialRegularCount = Number((ownerInitialNotificationState.data && ownerInitialNotificationState.data.unread_regular_total) || 0);

    const convertedContext = await browser.newContext();
    const convertedPage = await convertedContext.newPage();
    await login(convertedPage, convertedUser.email, convertedUser.password);
    await convertedPage.goto(`/tree/project/${state.project.id}`);
    await waitForTreeProjectReady(convertedPage, state.project.id, state.task.id);
    await patchTask(convertedPage, state.collaborator_dated_task.id, { status: 'critical' });
    await steps.step('Sign in as the converted collaborator account and update another shared task from the Tree view.', convertedPage);

    await waitForRegularNotificationCount(ownerPage, ownerInitialRegularCount + 1);

    await ownerPage.locator('[data-notification-kind="regular"] [data-notifications-trigger]').click();
    await expectContainsTextWithFailure(
      ownerPage,
      test.info(),
      '[data-notification-kind="regular"] .notifications-menu',
      'Email Collaborator Due Soon',
      'converted-collaborator-owner-notification-task',
      'The owner should receive a notification for the converted collaborator account action.'
    );
    await expectContainsTextWithFailure(
      ownerPage,
      test.info(),
      '[data-notification-kind="regular"] .notifications-menu',
      'Email Collaborator',
      'converted-collaborator-owner-notification-actor',
      'The owner notification menu should identify the converted collaborator account actor.'
    );

    await ownerPage.goto(`/tree/project/${state.project.id}`);
    await waitForTreeProjectReady(ownerPage, state.project.id, state.task.id);
    await expectTreeAssignmentsWithFailure(
      ownerPage,
      test.info(),
      state.collaborator_dated_task.id,
      ['Email Collaborator'],
      'converted-collaborator-tree-assignment-badge',
      'After conversion, the collaborator should render as an account assignee badge in the tree row.'
    );
    await focusTreeTask(ownerPage, state.collaborator_dated_task.id, 'Owner sees converted collaborator assignee badge', [
      'The assignee badges should now include the converted collaborator as an account user.',
    ]);
    await steps.step('Verify the owner sees both a new regular notification and a proper tree-row assignee badge for the converted collaborator account.', ownerPage);

    await ownerContext.close();
    await collaboratorContext.close();
    await convertedContext.close();
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

  test('tree direct-project click keeps avatar and avoids regular project chrome', async ({ page, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['tree', 'direct', 'sidebar', 'avatar']);
    const state = await fetchSeedState(request);

    await login(page, state.owner.email, state.owner.password);
    await page.goto(`/tree/project/${state.project.id}`);
    await waitForTreeProjectReady(page, state.project.id, state.task.id);
    await expect(page.locator(`[data-tree-direct-project="${state.direct_project.id}"]`)).toHaveCount(1);

    const directRowSelector = `[data-tree-direct-project="${state.direct_project.id}"] .todo-tree-row`;
    const directButtonSelector = `[data-tree-direct-project="${state.direct_project.id}"] [data-tree-select-type="project"][data-tree-select-id="${state.direct_project.id}"]`;
    const directNodeSelector = `[data-tree-direct-project="${state.direct_project.id}"]`;

    await expect(page.locator(`${directRowSelector} .avatar-stack.is-tree-direct .avatar-chip`)).toHaveCount(1);
    await expect(page.locator(`${directRowSelector} .shared-with-me-icon`)).toHaveCount(0);
    await expect(page.locator(`${directRowSelector} [data-tree-toggle-project="${state.direct_project.id}"]`)).toHaveCount(0);
    await focusTreeDirectProjectRow(page, state.direct_project.id, 'Direct project row before click', [
      'The sidebar row should show the peer avatar.',
      'No shared/share-out icon should be present.',
      'No project toggle should be present.',
    ]);
    await steps.step('Open Tree on a regular project first and inspect the direct-project row in the sidebar before clicking it.', page);

    await page.locator(directButtonSelector).click();
    await waitForTreeProjectReady(page, state.direct_project.id, state.direct_task.id);
    await expect(page.locator(directButtonSelector)).toHaveClass(/is-active/);
    await expect(page.locator(`${directRowSelector} .avatar-stack.is-tree-direct .avatar-chip`)).toHaveCount(1);
    await expect(page.locator(`${directRowSelector} .shared-with-me-icon`)).toHaveCount(0);
    await expect(page.locator(`${directRowSelector} [data-tree-toggle-project="${state.direct_project.id}"]`)).toHaveCount(0);
    await expect(page.locator(`${directNodeSelector} .todo-tree-groups [data-tree-group-row]`)).toHaveCount(0);
    await focusTreeDirectProjectRow(page, state.direct_project.id, 'Direct project row after click', [
      'The avatar should still be present after selection.',
      'No shared/share-out icon should appear after click.',
      'The direct row should not grow project-group children.',
    ]);
    await steps.step('Click the direct-project row and verify it stays avatar-based, with no share icon and no expandable group chrome injected.', page);
  });

  test('tree shared structure project shows header avatars', async ({ page, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['tree', 'shared', 'project-header', 'avatars']);
    const state = await fetchSeedState(request);

    await login(page, state.owner.email, state.owner.password);
    await page.goto(`/tree/project/${state.project.id}`);
    await waitForTreeProjectReady(page, state.project.id, state.task.id);

    const headerAvatarSelector = `[data-tree-project-board="${state.project.id}"] .project-header-avatars .avatar-chip`;
    await expect(page.locator(headerAvatarSelector)).toHaveCount(2);
    await expect(page.locator(`[data-tree-project-board="${state.project.id}"] .project-header-avatars [data-user-id="${state.owner.id}"]`)).toHaveCount(1);
    await expect(page.locator(`[data-tree-project-board="${state.project.id}"] .project-header-avatars [data-user-id="${state.member.id}"]`)).toHaveCount(1);
    await expect(page.locator(`[data-tree-project-board="${state.project.id}"] .project-header-share-tools`)).toHaveCount(1);
    await annotateLocator(page, `[data-tree-project-board="${state.project.id}"] .project-board-header`, 'Shared structure project header avatars', [
      'The shared structure project should show header avatars for Owner and Member.',
      'The share tools container should remain present on the board header.',
    ]);
    await steps.step('Open the shared structure project and verify its board header renders Owner and Member avatar chips immediately.', page);
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

  test('todo refresh preserves link favicons', async ({ page, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['todo', 'links', 'refresh', 'favicons']);
    const state = await fetchSeedState(request);
    const taskId = state.linked_todo_task.id;
    const linkUrl = state.linked_todo_task.link;

    await login(page, state.owner.email, state.owner.password);
    await page.goto('/todo');
    await expect(page.locator(`.todo-item[data-task-id="${taskId}"]`)).toContainText('Linked Todo Task');
    await expectTodoTaskLinkBadge(page, test.info(), taskId, linkUrl, 'todo-link-badge-before-refresh', 'The linked Todo task should show its favicon badge on the initial Todo load.');
    await focusTodoTask(page, taskId, 'Linked Todo task before refresh', [`Expected link badge: ${linkUrl}`]);
    await steps.step('Open /todo as owner@example.com and verify Linked Todo Task shows its link favicon badge.', page);

    await page.reload();
    await expect(page.locator(`.todo-item[data-task-id="${taskId}"]`)).toContainText('Linked Todo Task');
    await expectTodoTaskLinkBadge(page, test.info(), taskId, linkUrl, 'todo-link-badge-after-refresh', 'The same Todo task should still show its favicon badge immediately after a full page refresh.');
    await focusTodoTask(page, taskId, 'Linked Todo task after refresh', [`Expected link badge: ${linkUrl}`]);
    await steps.step('Refresh /todo and verify Linked Todo Task still shows the same link favicon badge immediately after reload.', page);
  });

  test('todo shows a compact active tasks section for started future work', async ({ page, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['todo', 'active-tasks', 'start-date']);
    const state = await fetchSeedState(request);
    const today = isoDateWithOffset(0);
    const futureDue = isoDateWithOffset(14);

    await login(page, state.owner.email, state.owner.password);
    await patchTask(page, state.task.id, {
      due_at: futureDue,
      due_mode: 'date',
      start_date: today,
      status: 'open',
    });
    await page.goto('/todo');

    const activeTasks = page.locator('[data-todo-active-tasks]');
    const activeRows = activeTasks.locator('.todo-active-task-row');
    await expect(activeTasks).toBeVisible();
    await expect(activeRows).toHaveCount(1);
    await expect(activeRows.first()).toContainText('Realtime Task');
    await expect(activeRows.first()).toContainText((state.project && (state.project.display_name || state.project.name)) || 'Realtime Project');
    await steps.step(`Open /todo with a task started on ${today} and due ${futureDue}, then verify the compact Active Tasks section shows a single-line row for Realtime Task.`, page);
  });

  test('todo active tasks are ordered by nearest due date first', async ({ page, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['todo', 'active-tasks', 'ordering']);
    const state = await fetchSeedState(request);
    const today = isoDateWithOffset(0);
    const soonerDue = isoDateWithOffset(7);
    const laterDue = isoDateWithOffset(21);

    await login(page, state.owner.email, state.owner.password);
    const laterTask = await createTask(page, {
      project_id: state.project.id,
      group_id: state.group.id,
      title: 'Later Active Task',
      due_at: laterDue,
      due_mode: 'date',
      start_date: today,
      assignee_email: state.owner.email,
    });
    const soonerTask = await createTask(page, {
      project_id: state.project.id,
      group_id: state.group.id,
      title: 'Sooner Active Task',
      due_at: soonerDue,
      due_mode: 'date',
      start_date: today,
      assignee_email: state.owner.email,
    });

    await page.goto('/todo');
    const activeTaskTitles = page.locator('[data-todo-active-tasks-list] .todo-active-task-title');
    await expect.poll(async () => {
      return activeTaskTitles.evaluateAll((nodes) =>
        nodes.map((node) => (node.textContent || '').trim()).filter(Boolean)
      );
    }).toEqual(expect.arrayContaining(['Sooner Active Task', 'Later Active Task']));
    const labels = await activeTaskTitles.evaluateAll((nodes) =>
      nodes.map((node) => (node.textContent || '').trim()).filter(Boolean)
    );
    expect(labels.indexOf('Sooner Active Task')).toBeGreaterThanOrEqual(0);
    expect(labels.indexOf('Later Active Task')).toBeGreaterThanOrEqual(0);
    expect(labels.indexOf('Sooner Active Task')).toBeLessThan(labels.indexOf('Later Active Task'));
    await steps.step(`Open /todo with two started tasks due on ${soonerDue} and ${laterDue}, then verify the compact Active Tasks section orders the nearer due task first.`, page);
  });

  test('todo active tasks respect assignee filters', async ({ page, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['todo', 'active-tasks', 'filters']);
    const state = await fetchSeedState(request);
    const today = isoDateWithOffset(0);
    const futureDue = isoDateWithOffset(10);

    await login(page, state.owner.email, state.owner.password);
    await patchTask(page, state.task.id, {
      due_at: futureDue,
      due_mode: 'date',
      start_date: today,
      status: 'open',
    });
    await page.goto('/todo');

    const activeTasks = page.locator('[data-todo-active-tasks]');
    const activeRow = activeTasks.locator('.todo-active-task-row', { hasText: 'Realtime Task' });
    await expect(activeRow).toHaveCount(1);

    await page.locator('[data-todo-assignee-dropdown]').click();
    await expect(page.locator('[data-todo-assignees-none]')).toBeVisible();
    await page.locator('[data-todo-assignees-none]').click();
    await expect(activeRow).toHaveCSS('display', 'none');
    await steps.step('Switch the Todo assignee filter to no assignees and verify the compact Active Tasks strip hides the assigned active task.', page);
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

  test('tree shows project assignment badge after refresh instead of expanded member badges', async ({ page, request }) => {
    const steps = createStepRecorder(test.info());
    await steps.tags(['tree', 'assignments', 'project-mode', 'refresh']);
    const state = await fetchSeedState(request);

    await login(page, state.owner.email, state.owner.password);
    const task = await createTask(page, {
      project_id: state.project.id,
      group_id: state.group.id,
      title: 'Project Assignment Badge Task',
      assignee_email: state.owner.email,
    });

    const assignAllResult = await page.evaluate(async (taskId) => {
      const response = await fetch(`/api/tasks/${taskId}`, {
        method: 'PATCH',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ assign_group_members: true }),
      });
      let data = {};
      try {
        data = await response.json();
      } catch (_error) {
        data = {};
      }
      return {
        ok: response.ok,
        status: response.status,
        data,
      };
    }, task.id);
    expect(assignAllResult.ok).toBeTruthy();

    await page.goto(`/tree/project/${state.project.id}`);
    await waitForTreeProjectReady(page, state.project.id, task.id);
    await page.reload();
    await waitForTreeProjectReady(page, state.project.id, task.id);

    const assignmentCell = page.locator(`[data-task-row-id="${task.id}"] .assignments`).first();
    await expect(assignmentCell.locator('[data-group-assignment-badge]')).toHaveCount(1);
    await expect(assignmentCell).toContainText('Project');
    await expect(assignmentCell).not.toContainText('Owner');
    await expect(assignmentCell).not.toContainText('Member');
    await focusTreeTask(page, task.id, 'Tree project assignment badge after refresh', [
      'The assignee column should keep the Project badge after reload.',
      'Expanded individual member badges should not replace project assignment mode.',
    ]);
    await steps.step('Enable project assignment mode, hard-reload the Tree view, and verify the assignee column still shows the Project badge rather than expanded member badges.', page);
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
