const path = require('path');
const { defineConfig } = require('@playwright/test');

const reportFolder = path.resolve(__dirname, 'playwright-report-gallery');

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
    baseURL: 'http://127.0.0.1:5010',
    headless: true,
    trace: 'retain-on-failure',
  },
  webServer: {
    command: 'python3 tests/e2e_server.py',
    cwd: path.resolve(__dirname, '..'),
    env: {
      ...process.env,
      PYTHONPATH: path.resolve(__dirname, '..'),
    },
    url: 'http://127.0.0.1:5010/health',
    reuseExistingServer: false,
    timeout: 30_000,
  },
});
