const { spawn } = require('child_process');
const path = require('path');

const configPath = path.resolve(__dirname, '..', 'playwright.config.js');
const reportPath = path.resolve(__dirname, '..', 'playwright-report-gallery', 'index.html');

console.log(`Playwright snapshot report: ${reportPath}`);

const child = spawn('npx', ['playwright', 'test', '--config', configPath, ...process.argv.slice(2)], {
  stdio: 'inherit',
  cwd: path.resolve(__dirname, '..', '..'),
});

child.on('exit', (code, signal) => {
  console.log(`\nPlaywright snapshot report: ${reportPath}`);
  if (signal) {
    process.kill(process.pid, signal);
    return;
  }
  process.exit(code ?? 1);
});
