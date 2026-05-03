const { spawn } = require('child_process');
const net = require('net');
const path = require('path');

const configPath = path.resolve(__dirname, '..', 'playwright.config.js');
const reportPath = path.resolve(__dirname, '..', 'playwright-report-gallery', 'index.html');

function portIsAvailable(port) {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.once('error', () => resolve(false));
    server.once('listening', () => {
      server.close(() => resolve(true));
    });
    server.listen(port, '127.0.0.1');
  });
}

async function choosePort() {
  const requested = Number(process.env.TERMIN_E2E_PORT || process.env.PORT || 5010);
  if (Number.isInteger(requested) && requested > 0 && await portIsAvailable(requested)) {
    return requested;
  }
  for (let port = 5011; port < 5100; port += 1) {
    if (await portIsAvailable(port)) return port;
  }
  throw new Error('No available e2e test port found between 5010 and 5099.');
}

choosePort()
  .then((port) => {
    const baseURL = `http://127.0.0.1:${port}`;
    const env = {
      ...process.env,
      TERMIN_E2E_PORT: String(port),
      TERMIN_E2E_BASE_URL: baseURL,
      PUBLIC_BASE_URL: baseURL,
    };

    console.log(`Playwright snapshot report: ${reportPath}`);
    console.log(`Playwright e2e server: ${baseURL}`);

    const child = spawn('npx', ['playwright', 'test', '--config', configPath, ...process.argv.slice(2)], {
      stdio: 'inherit',
      cwd: path.resolve(__dirname, '..', '..'),
      env,
    });

    child.on('exit', (code, signal) => {
      console.log(`\nPlaywright snapshot report: ${reportPath}`);
      if (signal) {
        process.kill(process.pid, signal);
        return;
      }
      process.exit(code ?? 1);
    });
  })
  .catch((error) => {
    console.error(error && error.message ? error.message : error);
    process.exit(1);
  });
