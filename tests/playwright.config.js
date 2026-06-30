const fs = require('fs');
const path = require('path');
const { defineConfig } = require('@playwright/test');

const reportFolder = path.resolve(__dirname, 'playwright-report-gallery');
const e2ePort = Number(process.env.TERMIN_E2E_PORT || process.env.PORT || 5010);
const e2eBaseURL = process.env.TERMIN_E2E_BASE_URL || `http://127.0.0.1:${e2ePort}`;
const venvPython = path.resolve(__dirname, '..', '.venv', 'bin', 'python3');
const e2ePython = process.env.TERMIN_E2E_PYTHON || (fs.existsSync(venvPython) ? venvPython : 'python3');

module.exports = defineConfig({
  testDir: path.resolve(__dirname, 'playwright'),
  workers: 1,
  timeout: 30_000,
  outputDir: path.resolve(__dirname, 'test-results'),
  expect: {
    timeout: 5_000,
  },
  reporter: [
    ['line'],
    [path.resolve(__dirname, 'playwright/simple-snapshot-reporter.js'), { outputFolder: reportFolder }],
  ],
  use: {
    baseURL: e2eBaseURL,
    headless: true,
    trace: 'retain-on-failure',
  },
  webServer: {
    command: `${e2ePython} tests/e2e_server.py`,
    cwd: path.resolve(__dirname, '..'),
    env: {
      ...process.env,
      PYTHONPATH: path.resolve(__dirname, '..'),
      TERMIN_E2E_PORT: String(e2ePort),
      TERMIN_E2E_BASE_URL: e2eBaseURL,
      PUBLIC_BASE_URL: e2eBaseURL,
    },
    url: `${e2eBaseURL}/health`,
    reuseExistingServer: false,
    timeout: 30_000,
  },
});
