const { defineConfig, devices } = require('@playwright/test');

module.exports = defineConfig({
  testDir: './tests/browser',
  fullyParallel: false,
  workers: 1,
  timeout: 30_000,
  expect: { timeout: 5_000 },
  use: {
    baseURL: 'http://127.0.0.1:8399',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    { name: 'desktop', use: { viewport: { width: 1440, height: 1000 } } },
    { name: 'mobile-390x844', use: { ...devices['iPhone 13'],
      browserName: 'chromium',
      viewport: { width: 390, height: 844 } } },
  ],
  webServer: {
    command: 'python3 tests/browser_fixture_server.py',
    url: 'http://127.0.0.1:8399/api/fleet',
    reuseExistingServer: false,
    timeout: 10_000,
  },
});
