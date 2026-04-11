const fs = require('fs');
const path = require('path');

function ensureDir(dir) {
  fs.mkdirSync(dir, { recursive: true });
}

function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function safeName(value) {
  return String(value || '')
    .replace(/[^a-zA-Z0-9._-]+/g, '-')
    .replace(/^-+|-+$/g, '') || 'artifact';
}

class SimpleSnapshotReporter {
  constructor(options = {}) {
    this.outputFolder = options.outputFolder || path.resolve(process.cwd(), 'playwright-report-gallery');
    this.tests = [];
    this.runStart = null;
    this.totalTests = 0;
  }

  onBegin(config, suite) {
    this.runStart = new Date();
    fs.rmSync(this.outputFolder, { recursive: true, force: true });
    ensureDir(this.outputFolder);
    this.totalTests = this.countTests(suite);
    this.writeReport({ overallStatus: 'running' });
  }

  onTestEnd(test, result) {
    const testId = safeName(`${test.parent.title}-${test.title}-${Date.now()}`);
    const testDir = path.join(this.outputFolder, testId);
    ensureDir(testDir);

    const snapshots = [];
    const failureSnapshots = [];
    const otherArtifacts = [];
    let reproSteps = [];
    let stepMap = [];
    let tags = [];

    for (const attachment of result.attachments || []) {
      if (attachment.name === 'test-tags') {
        const rawBody = attachment.body ? attachment.body.toString('utf8') : '[]';
        try {
          tags = JSON.parse(rawBody);
        } catch (_error) {
          tags = [];
        }
        continue;
      }
      if (attachment.name === 'step-map') {
        const rawBody = attachment.body ? attachment.body.toString('utf8') : '[]';
        try {
          stepMap = JSON.parse(rawBody);
        } catch (_error) {
          stepMap = [];
        }
        continue;
      }
      if (attachment.name === 'repro-steps') {
        const rawBody = attachment.body ? attachment.body.toString('utf8') : '';
        reproSteps = rawBody
          .split('\n')
          .map((line) => line.trim())
          .filter(Boolean)
          .map((line) => line.replace(/^\d+\.\s*/, ''));
        continue;
      }
      if (!attachment.path || !fs.existsSync(attachment.path)) continue;
      const ext = path.extname(attachment.path) || '';
      const artifactName = `${safeName(attachment.name)}${ext}`;
      const destination = path.join(testDir, artifactName);
      fs.copyFileSync(attachment.path, destination);
      const relativePath = path.relative(this.outputFolder, destination);
      if ((attachment.contentType || '').startsWith('image/')) {
        if (attachment.name.startsWith('step-snapshot:')) {
          const [, stepIndexRaw, snapshotLabelRaw] = attachment.name.split(':');
          snapshots.push({
            label: snapshotLabelRaw || attachment.name,
            path: relativePath,
            stepIndex: Number(stepIndexRaw || 0),
          });
        } else if (attachment.name.startsWith('failure-snapshot:')) {
          const [, failureLabelRaw] = attachment.name.split(':');
          failureSnapshots.push({
            label: failureLabelRaw || attachment.name,
            path: relativePath,
          });
        } else {
          snapshots.push({
            label: attachment.name,
            path: relativePath,
            stepIndex: 0,
          });
        }
      } else {
        otherArtifacts.push({
          label: attachment.name,
          path: relativePath,
        });
      }
    }

    this.tests.push({
      title: test.title,
      suite: test.parent.title,
      status: result.status,
      duration: result.duration,
      error: result.error ? (result.error.stack || result.error.message || String(result.error)) : '',
      tags,
      reproSteps,
      stepMap,
      snapshots,
      failureSnapshots,
      otherArtifacts,
    });

    this.writeReport({ overallStatus: 'running' });
  }

  async onEnd(result) {
    this.writeReport({ overallStatus: result.status || 'finished' });
  }

  countTests(suite) {
    let count = 0;
    const stack = [suite];
    while (stack.length) {
      const node = stack.pop();
      if (!node) continue;
      if (Array.isArray(node.tests)) count += node.tests.length;
      if (Array.isArray(node.suites)) stack.push(...node.suites);
    }
    return count;
  }

  buildCards() {
    return this.tests.map((test) => {
      const isOpen = test.status !== 'passed';
      const groupedSnapshots = new Map();
      for (const shot of test.snapshots) {
        const key = Number(shot.stepIndex || 0);
        if (!groupedSnapshots.has(key)) groupedSnapshots.set(key, []);
        groupedSnapshots.get(key).push(shot);
      }
      const artifactHtml = test.otherArtifacts.length
        ? `<div class="artifacts">${test.otherArtifacts.map((artifact) => `<a href="${encodeURI(artifact.path)}">${escapeHtml(artifact.label)}</a>`).join('')}</div>`
        : '';
      const stepSequence = (test.stepMap && test.stepMap.length)
        ? test.stepMap
        : test.reproSteps.map((description, index) => ({ index: index + 1, description, snapshots: [] }));
      const stepsHtml = stepSequence.length
        ? `<ol class="repro-steps">${stepSequence.map((step) => {
            const shots = groupedSnapshots.get(Number(step.index || 0)) || [];
            const shotHtml = shots.length
              ? `<div class="step-snapshots">${shots.map((shot) => `
                  <figure class="snapshot-card">
                    <figcaption>${escapeHtml(shot.label)}</figcaption>
                    <a href="${encodeURI(shot.path)}" target="_blank" rel="noreferrer">
                      <img src="${encodeURI(shot.path)}" alt="${escapeHtml(shot.label)}" loading="lazy" />
                    </a>
                  </figure>
                `).join('')}</div>`
              : '<p class="empty">No snapshot captured for this step.</p>';
            return `<li class="repro-step"><div class="repro-step-text"><span class="repro-step-index">${escapeHtml(step.index)}</span><div class="repro-step-copy">${escapeHtml(step.description)}</div></div>${shotHtml}</li>`;
          }).join('')}</ol>`
        : '<p class="empty">No reproduction steps recorded.</p>';
      const errorHtml = test.error
        ? `<pre class="error-block">${escapeHtml(test.error)}</pre>`
        : '';
      const failureHtml = (test.failureSnapshots || []).length
        ? `<div class="detail-section">
            <h3>Failure Focus</h3>
            <div class="failure-gallery">${test.failureSnapshots.map((shot) => `
              <figure class="failure-card">
                <figcaption>${escapeHtml(shot.label)}</figcaption>
                <a href="${encodeURI(shot.path)}" target="_blank" rel="noreferrer">
                  <img src="${encodeURI(shot.path)}" alt="${escapeHtml(shot.label)}" loading="lazy" />
                </a>
              </figure>
            `).join('')}</div>
          </div>`
        : '';
      const tagsHtml = (test.tags || []).length
        ? `<div class="tag-list">${test.tags.map((tag) => `<span class="tag-pill">${escapeHtml(tag)}</span>`).join('')}</div>`
        : '';
      return `
        <details class="test-card status-${escapeHtml(test.status)}" data-status="${escapeHtml(test.status)}" data-tags="${escapeHtml((test.tags || []).join(' '))}"${isOpen ? ' open' : ''}>
          <summary class="test-summary">
            <div class="test-summary-main">
              <span class="summary-caret" aria-hidden="true"></span>
              <div>
                <div class="suite-name">${escapeHtml(test.suite)}</div>
                <h2>${escapeHtml(test.title)}</h2>
                ${tagsHtml}
              </div>
            </div>
            <div class="test-meta">
              <span class="status-pill">${escapeHtml(test.status)}</span>
              <span>${Math.round(test.duration)} ms</span>
              <span>${test.snapshots.length} shot${test.snapshots.length === 1 ? '' : 's'}</span>
            </div>
          </summary>
          <div class="test-details">
            ${artifactHtml}
            <div class="detail-section">
              <h3>Reproduce</h3>
              ${stepsHtml}
            </div>
            ${failureHtml}
            ${errorHtml}
          </div>
        </details>
      `;
    }).join('');
  }

  writeReport({ overallStatus }) {
    const generatedAt = new Date();
    const counts = this.tests.reduce((acc, test) => {
      acc[test.status] = (acc[test.status] || 0) + 1;
      return acc;
    }, {});
    const allTags = Array.from(new Set(this.tests.flatMap((test) => test.tags || []))).sort();
    const completedCount = this.tests.length;
    const totalCount = this.totalTests || completedCount;
    const progressPercent = totalCount ? Math.max(0, Math.min(100, (completedCount / totalCount) * 100)) : 0;
    const cards = this.buildCards();
    const html = `<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Playwright Snapshot Report</title>
    ${overallStatus === 'running' ? '<meta http-equiv="refresh" content="2" />' : ''}
    <style>
      :root {
        --bg: #f3efe7;
        --bg-accent: #e7dece;
        --panel: rgba(255, 252, 246, 0.92);
        --panel-2: rgba(255, 249, 240, 0.96);
        --border: rgba(77, 58, 39, 0.13);
        --border-strong: rgba(77, 58, 39, 0.24);
        --text: #1f2933;
        --muted: #6e7781;
        --ok: #1f8f55;
        --fail: #c93f3f;
        --warn: #a36d00;
        --running: #2f6fed;
        --link: #215fbe;
        --shadow: 0 24px 48px rgba(45, 31, 15, 0.12);
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        padding: 28px;
        color: var(--text);
        font: 14px/1.5 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        background:
          radial-gradient(circle at top left, rgba(255,255,255,0.8), transparent 28%),
          linear-gradient(180deg, var(--bg) 0%, var(--bg-accent) 100%);
      }
      a { color: var(--link); text-decoration: none; }
      a:hover { text-decoration: underline; }
      .shell {
        max-width: 1520px;
        margin: 0 auto;
      }
      .hero {
        position: sticky;
        top: 0;
        z-index: 20;
        margin-bottom: 22px;
        padding: 22px 24px 18px;
        border-radius: 26px;
        border: 1px solid var(--border);
        background:
          linear-gradient(135deg, rgba(255,245,220,0.96) 0%, rgba(245,235,255,0.96) 50%, rgba(221,244,255,0.96) 100%);
        box-shadow: var(--shadow);
        backdrop-filter: blur(14px);
      }
      .page-header {
        display: flex;
        justify-content: space-between;
        align-items: flex-end;
        gap: 16px;
        margin-bottom: 16px;
      }
      .page-header h1 {
        margin: 4px 0;
        font-size: 32px;
        line-height: 1.05;
        letter-spacing: -0.03em;
      }
      .hero-status {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        padding: 6px 10px;
        border-radius: 999px;
        border: 1px solid var(--border-strong);
        background: rgba(255,255,255,0.55);
        color: var(--muted);
        font-size: 12px;
        font-weight: 700;
        letter-spacing: 0.08em;
        text-transform: uppercase;
      }
      .hero-status::before {
        content: "";
        width: 10px;
        height: 10px;
        border-radius: 999px;
        background: ${overallStatus === 'running' ? 'var(--running)' : overallStatus === 'passed' ? 'var(--ok)' : 'var(--fail)'};
        box-shadow: 0 0 0 4px rgba(255,255,255,0.4);
      }
      .meta {
        color: var(--muted);
      }
      .progress-wrap {
        display: grid;
        gap: 10px;
      }
      .progress-row {
        display: flex;
        justify-content: space-between;
        align-items: baseline;
        gap: 12px;
      }
      .progress-row strong {
        font-size: 18px;
      }
      .progress-bar {
        position: relative;
        overflow: hidden;
        height: 18px;
        border-radius: 999px;
        background: rgba(31, 41, 51, 0.08);
        border: 1px solid rgba(31, 41, 51, 0.08);
      }
      .progress-fill {
        position: absolute;
        inset: 0 auto 0 0;
        width: ${progressPercent}%;
        border-radius: inherit;
        background: linear-gradient(90deg, #ff9f43 0%, #7a64ff 42%, #2f6fed 100%);
      }
      .progress-gloss {
        position: absolute;
        inset: 0;
        background: linear-gradient(180deg, rgba(255,255,255,0.32), transparent 52%);
      }
      .progress-breakdown {
        display: grid;
        grid-template-columns: repeat(4, minmax(0, 1fr));
        gap: 10px;
      }
      .progress-card {
        padding: 12px 14px;
        border-radius: 16px;
        border: 1px solid var(--border);
        background: rgba(255,255,255,0.58);
      }
      .progress-card strong {
        display: block;
        font-size: 18px;
      }
      .progress-card span {
        color: var(--muted);
        font-size: 12px;
        text-transform: uppercase;
        letter-spacing: 0.07em;
      }
      .summary {
        display: flex;
        gap: 10px;
        flex-wrap: wrap;
        margin-bottom: 18px;
      }
      .summary-controls,
      .tag-controls {
        display: flex;
        gap: 8px;
        flex-wrap: wrap;
        margin-bottom: 16px;
      }
      .summary-pill,
      .filter-button {
        border: 1px solid var(--border);
        background: rgba(255,255,255,0.58);
        border-radius: 999px;
        padding: 8px 12px;
      }
      .filter-button {
        color: var(--text);
        cursor: pointer;
      }
      .filter-button.is-active {
        background: rgba(47,111,237,0.14);
        border-color: rgba(47,111,237,0.35);
      }
      .test-grid {
        display: grid;
        gap: 16px;
      }
      .test-card {
        border: 1px solid var(--border);
        background: var(--panel);
        border-radius: 18px;
        overflow: hidden;
        box-shadow: 0 10px 22px rgba(45, 31, 15, 0.06);
      }
      .status-passed { border-color: rgba(31,143,85,0.25); }
      .status-failed { border-color: rgba(201,63,63,0.38); }
      .status-timedOut,
      .status-timedout { border-color: rgba(163,109,0,0.36); }
      .test-card[hidden] { display: none; }
      .test-summary {
        display: flex;
        justify-content: space-between;
        gap: 16px;
        align-items: center;
        padding: 14px 16px;
        cursor: pointer;
        list-style: none;
      }
      .test-summary::-webkit-details-marker { display: none; }
      .test-summary h2 {
        margin: 0;
        font-size: 17px;
        line-height: 1.3;
      }
      .test-summary-main {
        min-width: 0;
        display: flex;
        align-items: flex-start;
        gap: 10px;
      }
      .summary-caret {
        width: 10px;
        height: 10px;
        border-right: 2px solid var(--muted);
        border-bottom: 2px solid var(--muted);
        transform: rotate(-45deg);
        margin-top: 8px;
        transition: transform 120ms ease;
      }
      .test-card[open] .summary-caret {
        transform: rotate(45deg);
      }
      .suite-name {
        color: var(--muted);
        margin-bottom: 2px;
        font-size: 12px;
        text-transform: uppercase;
        letter-spacing: 0.08em;
      }
      .tag-list {
        display: flex;
        gap: 6px;
        flex-wrap: wrap;
        margin-top: 8px;
      }
      .tag-pill {
        border: 1px solid rgba(47,111,237,0.18);
        background: rgba(47,111,237,0.08);
        color: #1c4c99;
        border-radius: 999px;
        padding: 2px 8px;
        font-size: 11px;
      }
      .test-meta {
        display: flex;
        gap: 10px;
        align-items: center;
        color: var(--muted);
        white-space: nowrap;
        font-size: 12px;
      }
      .status-pill {
        border-radius: 999px;
        padding: 4px 10px;
        background: rgba(255,255,255,0.62);
        text-transform: capitalize;
      }
      .status-passed .status-pill { color: var(--ok); }
      .status-failed .status-pill { color: var(--fail); }
      .status-timedOut .status-pill,
      .status-timedout .status-pill { color: var(--warn); }
      .test-details {
        padding: 0 16px 16px;
        border-top: 1px solid rgba(77,58,39,0.08);
      }
      .artifacts {
        display: flex;
        gap: 12px;
        flex-wrap: wrap;
        margin-top: 12px;
      }
      .detail-section {
        margin-top: 14px;
      }
      .detail-section h3 {
        margin: 0 0 8px;
        color: var(--muted);
        font-size: 13px;
        text-transform: uppercase;
        letter-spacing: 0.08em;
      }
      .repro-steps {
        margin: 0;
        padding: 0;
        list-style: none;
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
        gap: 14px;
      }
      .repro-step {
        border: 1px solid var(--border);
        background: var(--panel-2);
        border-radius: 16px;
        padding: 12px;
        display: flex;
        flex-direction: column;
        min-width: 0;
      }
      .repro-step-text {
        display: flex;
        gap: 10px;
        align-items: flex-start;
        margin-bottom: 10px;
      }
      .repro-step-index {
        width: 24px;
        height: 24px;
        border-radius: 999px;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        flex: 0 0 auto;
        background: rgba(47,111,237,0.12);
        color: #1c4c99;
        font-size: 12px;
        font-weight: 700;
      }
      .repro-step-copy {
        min-width: 0;
        flex: 1 1 auto;
      }
      .step-snapshots {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
        gap: 10px;
      }
      .snapshot-card {
        margin: 0;
        border: 1px solid var(--border);
        border-radius: 12px;
        overflow: hidden;
        background: rgba(255,255,255,0.8);
      }
      .snapshot-card figcaption {
        padding: 8px 10px;
        color: var(--muted);
        border-bottom: 1px solid var(--border);
        font-size: 12px;
      }
      .snapshot-card img {
        display: block;
        width: 100%;
        height: 360px;
        object-fit: contain;
        object-position: top center;
        background: #fff;
      }
      .failure-gallery {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
        gap: 12px;
      }
      .failure-card {
        margin: 0;
        border: 1px solid rgba(201,63,63,0.22);
        border-radius: 14px;
        overflow: hidden;
        background: rgba(255, 248, 248, 0.96);
      }
      .failure-card figcaption {
        padding: 9px 11px;
        color: var(--fail);
        border-bottom: 1px solid rgba(201,63,63,0.18);
        font-size: 12px;
        font-weight: 700;
      }
      .failure-card img {
        display: block;
        width: 100%;
        height: 440px;
        object-fit: contain;
        object-position: top center;
        background: #fff;
      }
      .error-block {
        white-space: pre-wrap;
        margin: 0;
        padding: 12px;
        border-radius: 12px;
        border: 1px solid rgba(201,63,63,0.28);
        background: rgba(201,63,63,0.08);
        color: #7d2626;
      }
      .empty {
        margin: 0;
        color: var(--muted);
      }
      @media (max-width: 900px) {
        body { padding: 16px; }
        .hero {
          position: static;
          padding: 18px;
        }
        .page-header,
        .progress-row,
        .test-summary {
          display: block;
        }
        .progress-breakdown {
          grid-template-columns: repeat(2, minmax(0, 1fr));
        }
        .test-meta {
          margin-top: 8px;
          white-space: normal;
          flex-wrap: wrap;
        }
        .repro-steps,
        .step-snapshots,
        .failure-gallery {
          grid-template-columns: 1fr;
        }
        .snapshot-card img,
        .failure-card img {
          height: auto;
        }
      }
    </style>
  </head>
  <body>
    <div class="shell">
      <section class="hero">
        <header class="page-header">
          <div>
            <div class="hero-status">${escapeHtml(overallStatus)}</div>
            <h1>Playwright Snapshot Report</h1>
            <div class="meta">Generated ${escapeHtml(generatedAt.toLocaleString())} • Started ${escapeHtml(this.runStart ? this.runStart.toLocaleString() : 'unknown')}</div>
          </div>
          <div class="meta">${completedCount} / ${totalCount} results written</div>
        </header>
        <div class="progress-wrap">
          <div class="progress-row">
            <strong>${completedCount} of ${totalCount} tests processed</strong>
            <span class="meta">${Math.round(progressPercent)}% complete</span>
          </div>
          <div class="progress-bar">
            <div class="progress-fill"></div>
            <div class="progress-gloss"></div>
          </div>
          <div class="progress-breakdown">
            <div class="progress-card"><strong>${counts.passed || 0}</strong><span>Passed</span></div>
            <div class="progress-card"><strong>${counts.failed || 0}</strong><span>Failed</span></div>
            <div class="progress-card"><strong>${counts.timedOut || 0}</strong><span>Timed Out</span></div>
            <div class="progress-card"><strong>${Math.max(0, totalCount - completedCount)}</strong><span>Remaining</span></div>
          </div>
        </div>
      </section>

      <div class="summary">
        <span class="summary-pill">Passed: ${counts.passed || 0}</span>
        <span class="summary-pill">Failed: ${counts.failed || 0}</span>
        <span class="summary-pill">Timed out: ${counts.timedOut || 0}</span>
        <span class="summary-pill">Total: ${completedCount}</span>
      </div>

      <div class="summary-controls">
        <button class="filter-button is-active" type="button" data-filter-status="all">All</button>
        <button class="filter-button" type="button" data-filter-status="passed">Passed</button>
        <button class="filter-button" type="button" data-filter-status="failed">Failed</button>
        <button class="filter-button" type="button" data-filter-status="timedOut">Timed out</button>
        <button class="filter-button" type="button" data-expand-action="expand-failed">Expand failures</button>
        <button class="filter-button" type="button" data-expand-action="collapse-all">Collapse all</button>
        <button class="filter-button" type="button" data-expand-action="expand-all">Expand all</button>
      </div>

      <div class="tag-controls">
        <button class="filter-button is-active" type="button" data-filter-tag="all">All tags</button>
        ${allTags.map((tag) => `<button class="filter-button" type="button" data-filter-tag="${escapeHtml(tag)}">${escapeHtml(tag)}</button>`).join('')}
      </div>

      <main class="test-grid">
        ${cards || '<p class="empty">No tests completed yet.</p>'}
      </main>
    </div>

    <script>
      (function () {
        var cards = Array.from(document.querySelectorAll('.test-card'));
        var filterButtons = Array.from(document.querySelectorAll('[data-filter-status]'));
        var expandButtons = Array.from(document.querySelectorAll('[data-expand-action]'));
        var tagButtons = Array.from(document.querySelectorAll('[data-filter-tag]'));
        var activeStatus = 'all';
        var activeTag = 'all';

        function applyFilter() {
          cards.forEach(function (card) {
            var tags = String(card.getAttribute('data-tags') || '').split(/\\s+/).filter(Boolean);
            var matchesStatus = activeStatus === 'all' || card.getAttribute('data-status') === activeStatus;
            var matchesTag = activeTag === 'all' || tags.indexOf(activeTag) !== -1;
            card.hidden = !(matchesStatus && matchesTag);
          });
          filterButtons.forEach(function (button) {
            button.classList.toggle('is-active', button.getAttribute('data-filter-status') === activeStatus);
          });
          tagButtons.forEach(function (button) {
            button.classList.toggle('is-active', button.getAttribute('data-filter-tag') === activeTag);
          });
        }

        filterButtons.forEach(function (button) {
          button.addEventListener('click', function () {
            activeStatus = button.getAttribute('data-filter-status') || 'all';
            applyFilter();
          });
        });

        tagButtons.forEach(function (button) {
          button.addEventListener('click', function () {
            activeTag = button.getAttribute('data-filter-tag') || 'all';
            applyFilter();
          });
        });

        expandButtons.forEach(function (button) {
          button.addEventListener('click', function () {
            var action = button.getAttribute('data-expand-action');
            cards.forEach(function (card) {
              if (action === 'expand-all') {
                card.open = true;
              } else if (action === 'collapse-all') {
                card.open = false;
              } else if (action === 'expand-failed') {
                card.open = card.getAttribute('data-status') !== 'passed';
              }
            });
          });
        });

        applyFilter();
      })();
    </script>
  </body>
</html>`;

    fs.writeFileSync(path.join(this.outputFolder, 'index.html'), html, 'utf8');
  }
}

module.exports = SimpleSnapshotReporter;
