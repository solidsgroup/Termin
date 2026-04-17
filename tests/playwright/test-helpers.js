const path = require('path');

function slugify(value) {
  return String(value || '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 64) || 'snapshot';
}

async function captureCheckpoint(page, testInfo, label, options = {}) {
  const filename = `${String(Date.now())}-${slugify(label)}.png`;
  const screenshotPath = testInfo.outputPath(filename);
  await page.screenshot({
    path: screenshotPath,
    fullPage: options.fullPage !== false,
    animations: 'disabled',
  });
  await testInfo.attach(`snapshot:${label}`, {
    path: screenshotPath,
    contentType: 'image/png',
  });
}

async function captureFailureSnapshot(page, testInfo, label, options = {}) {
  const filename = `${String(Date.now())}-${slugify(`failure-${label}`)}.png`;
  const screenshotPath = testInfo.outputPath(filename);
  const selector = options.selector ? String(options.selector) : '';
  const expected = options.expected == null ? '' : String(options.expected);
  const actual = options.actual == null ? '' : String(options.actual);
  const note = options.note ? String(options.note) : '';

  await page.evaluate(({ selector: innerSelector, expected: innerExpected, actual: innerActual, note: innerNote, label: innerLabel }) => {
    document.querySelectorAll('[data-playwright-failure-overlay]').forEach((node) => node.remove());
    document.querySelectorAll('[data-playwright-failure-highlight]').forEach((node) => {
      node.removeAttribute('data-playwright-failure-highlight');
      node.style.outline = '';
      node.style.outlineOffset = '';
      node.style.background = '';
    });
    var target = innerSelector ? document.querySelector(innerSelector) : null;
    if (target) {
      target.setAttribute('data-playwright-failure-highlight', '1');
      target.style.outline = '4px solid #e53935';
      target.style.outlineOffset = '3px';
      target.style.background = 'rgba(229, 57, 53, 0.08)';
      if (typeof target.scrollIntoView === 'function') {
        target.scrollIntoView({ block: 'center', inline: 'nearest' });
      }
    }
    var overlay = document.createElement('div');
    overlay.setAttribute('data-playwright-failure-overlay', '1');
    overlay.style.position = 'fixed';
    overlay.style.left = '20px';
    overlay.style.bottom = '20px';
    overlay.style.zIndex = '2147483647';
    overlay.style.maxWidth = 'min(560px, calc(100vw - 40px))';
    overlay.style.padding = '14px 16px';
    overlay.style.borderRadius = '16px';
    overlay.style.background = 'rgba(127, 29, 29, 0.96)';
    overlay.style.color = '#fff';
    overlay.style.boxShadow = '0 22px 50px rgba(15, 23, 42, 0.34)';
    overlay.style.font = '600 14px/1.45 system-ui, sans-serif';
    overlay.innerHTML =
      '<div style="font-size:12px;opacity:.78;text-transform:uppercase;letter-spacing:.08em;">Assertion Failure</div>' +
      '<div style="margin-top:4px;font-size:18px;line-height:1.2;">' + innerLabel + '</div>' +
      (innerNote ? '<div style="margin-top:8px;opacity:.92;">' + innerNote + '</div>' : '') +
      (innerExpected ? '<div style="margin-top:10px;"><strong>Expected:</strong> ' + innerExpected + '</div>' : '') +
      (innerActual ? '<div><strong>Actual:</strong> ' + innerActual + '</div>' : '') +
      (innerSelector ? '<div style="margin-top:6px;font-size:12px;opacity:.78;"><code>' + innerSelector.replace(/</g, '&lt;').replace(/>/g, '&gt;') + '</code></div>' : '');
    document.body.appendChild(overlay);
  }, { selector, expected, actual, note, label });

  await page.screenshot({
    path: screenshotPath,
    fullPage: options.fullPage !== false,
    animations: 'disabled',
  });
  await testInfo.attach(`failure-snapshot:${label}`, {
    path: screenshotPath,
    contentType: 'image/png',
  });
}

async function captureMultiCheckpoint(testInfo, label, pages) {
  for (const entry of pages) {
    if (!entry || !entry.page) continue;
    const name = entry.name ? `${label} - ${entry.name}` : label;
    await captureCheckpoint(entry.page, testInfo, name, entry.options || {});
  }
}

async function attachReproSteps(testInfo, steps) {
  const lines = Array.isArray(steps) ? steps : [];
  const body = lines.map((step, index) => `${index + 1}. ${String(step)}`).join('\n');
  await testInfo.attach('repro-steps', {
    body: Buffer.from(body, 'utf8'),
    contentType: 'text/plain',
  });
}

function createStepRecorder(testInfo) {
  const steps = [];
  const tags = new Set();

  async function sync() {
    await testInfo.attach('step-map', {
      body: Buffer.from(JSON.stringify(steps), 'utf8'),
      contentType: 'application/json',
    });
    await testInfo.attach('test-tags', {
      body: Buffer.from(JSON.stringify(Array.from(tags).sort()), 'utf8'),
      contentType: 'application/json',
    });
  }

  function addTags(values) {
    (Array.isArray(values) ? values : []).forEach((value) => {
      const normalized = String(value || '').trim().toLowerCase();
      if (normalized) tags.add(normalized);
    });
  }

  async function step(description, page, options = {}) {
    const stepIndex = steps.length + 1;
    const screenshotName = options.name || 'snapshot';
    const label = `step-${stepIndex}:${screenshotName}`;
    const filename = `${String(Date.now())}-${slugify(label)}.png`;
    const screenshotPath = testInfo.outputPath(filename);
    await page.screenshot({
      path: screenshotPath,
      fullPage: options.fullPage !== false,
      animations: 'disabled',
    });
    await testInfo.attach(`step-snapshot:${stepIndex}:${screenshotName}`, {
      path: screenshotPath,
      contentType: 'image/png',
    });
    steps.push({
      index: stepIndex,
      description: String(description),
      snapshots: [{ label: screenshotName }],
    });
    await sync();
  }

  async function multiStep(description, pages) {
    const stepIndex = steps.length + 1;
    const snapshotEntries = [];
    for (const entry of pages) {
      if (!entry || !entry.page) continue;
      const screenshotName = entry.name || `view-${snapshotEntries.length + 1}`;
      const label = `step-${stepIndex}:${screenshotName}`;
      const filename = `${String(Date.now())}-${slugify(label)}.png`;
      const screenshotPath = testInfo.outputPath(filename);
      await entry.page.screenshot({
        path: screenshotPath,
        fullPage: !(entry.options && entry.options.fullPage === false),
        animations: 'disabled',
      });
      await testInfo.attach(`step-snapshot:${stepIndex}:${screenshotName}`, {
        path: screenshotPath,
        contentType: 'image/png',
      });
      snapshotEntries.push({ label: screenshotName });
    }
    steps.push({
      index: stepIndex,
      description: String(description),
      snapshots: snapshotEntries,
    });
    await sync();
  }

  return {
    tags(values) {
      addTags(values);
      return sync();
    },
    step,
    multiStep,
  };
}

function normalizeConsoleLocation(location) {
  if (!location) return '';
  const url = location.url ? String(location.url) : '';
  const line = Number.isFinite(location.lineNumber) ? `:${location.lineNumber}` : '';
  const column = Number.isFinite(location.columnNumber) ? `:${location.columnNumber}` : '';
  return `${url}${line}${column}`.replace(/^:+$/, '');
}

function shouldIgnoreConsoleMessage(type, text) {
  const normalizedType = String(type || '').toLowerCase();
  const normalizedText = String(text || '');
  if (normalizedType !== 'error') return true;
  return [
    'WebSocket transport not available. Install simple-websocket for improved performance.',
    'The WebSocket transport is not available, you must install a WebSocket server that is compatible with your async mode to enable it.',
    "WebSocket connection to 'ws://127.0.0.1:5010/socket.io/?EIO=4&transport=websocket' failed: Error during WebSocket handshake: Unexpected response code: 400",
  ].some((fragment) => normalizedText.includes(fragment));
}

function installBrowserErrorCollector(page, testInfo) {
  const failures = [];
  const seen = new Set();

  function record(kind, message, details = '') {
    const normalizedMessage = String(message || '').trim();
    const normalizedDetails = String(details || '').trim();
    const key = `${kind}::${normalizedMessage}::${normalizedDetails}`;
    if (!normalizedMessage || seen.has(key)) return;
    seen.add(key);
    failures.push({
      kind,
      message: normalizedMessage,
      details: normalizedDetails,
    });
  }

  page.on('pageerror', (error) => {
    const message = error && error.stack ? error.stack : (error && error.message ? error.message : String(error || 'Unknown page error'));
    record('pageerror', message);
  });

  page.on('console', (msg) => {
    const type = msg.type();
    const text = msg.text();
    if (shouldIgnoreConsoleMessage(type, text)) return;
    record(`console.${type}`, text, normalizeConsoleLocation(msg.location && msg.location()));
  });

  return async function assertNoBrowserErrors() {
    if (!failures.length) return;
    const lines = failures.map((entry, index) => {
      const suffix = entry.details ? `\n   at ${entry.details}` : '';
      return `${index + 1}. [${entry.kind}] ${entry.message}${suffix}`;
    });
    await testInfo.attach('browser-errors', {
      body: Buffer.from(lines.join('\n\n'), 'utf8'),
      contentType: 'text/plain',
    });
    throw new Error(`Unexpected browser errors:\n\n${lines.join('\n\n')}`);
  };
}

function installBrowserErrorCollectorOnContext(context, testInfo) {
  const assertions = [];
  const installedPages = new WeakSet();

  function installOnPage(page) {
    if (!page || installedPages.has(page)) return;
    installedPages.add(page);
    assertions.push(installBrowserErrorCollector(page, testInfo));
  }

  if (typeof context.pages === 'function') {
    context.pages().forEach(installOnPage);
  }
  context.on('page', installOnPage);

  return async function assertNoBrowserErrorsOnContext() {
    for (const assertNoErrors of assertions) {
      await assertNoErrors();
    }
  };
}

module.exports = {
  captureCheckpoint,
  captureFailureSnapshot,
  captureMultiCheckpoint,
  attachReproSteps,
  createStepRecorder,
  installBrowserErrorCollector,
  installBrowserErrorCollectorOnContext,
};
