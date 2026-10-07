import { defineConfig, devices } from '@playwright/test';
export default defineConfig({
  testDir: './e2e',
  outputDir: './test-results/live',
  testMatch: '**/*.live.spec.ts',
  fullyParallel: false,
  workers: 1,
  timeout: 600_000,
  expect: { timeout: 180_000 },
  reporter: 'list',
  use: {
    baseURL: 'http://127.0.0.1:5181',
    actionTimeout: 30_000,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    { name: 'live-chromium', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 960 } } },
  ],
  webServer: {
    command: 'npm run dev -- --port 5181',
    url: 'http://127.0.0.1:5181',
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
  },
});
