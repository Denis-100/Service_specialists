const { defineConfig } = require('@playwright/test');
module.exports = defineConfig({
  testDir: './tests/browser',
  workers: 1,
  retries: 0,
  use: {
    baseURL: 'http://127.0.0.1:5051',
    viewport: { width: 1440, height: 1000 },
    launchOptions: process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {},
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure'
  },
  webServer: {
    command: '.venv/bin/python scripts/e2e_server.py',
    url: 'http://127.0.0.1:5051/api/session',
    reuseExistingServer: false,
    timeout: 30000
  }
});
