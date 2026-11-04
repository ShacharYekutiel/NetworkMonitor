import { defineConfig, devices } from '@playwright/test';
import { existsSync } from 'node:fs';

// The preinstalled Chromium may not match @playwright/test's expected revision; prefer it when present.
const CHROMIUM = process.env.PW_CHROMIUM ?? '/opt/pw-browsers/chromium';

export default defineConfig({
  testDir: './specs',
  // run.sh writes service logs to test-results/logs; keep Playwright's own output (which it wipes
  // at start) in a subfolder.
  outputDir: './test-results/artifacts',
  // The specs share one backend and run in file order (01-, 02-, ...): the first one runs the setup wizard.
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 120_000,
  expect: { timeout: 15_000 },
  reporter: [['list'], ['html', { open: 'never', outputFolder: 'test-results/report' }]],
  use: {
    baseURL: process.env.BASE_URL ?? 'http://localhost:8080',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    viewport: { width: 1440, height: 900 },
    locale: 'en-US',
    timezoneId: 'UTC',
  },
  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        viewport: { width: 1440, height: 900 },
        launchOptions: existsSync(CHROMIUM) ? { executablePath: CHROMIUM } : {},
      },
    },
  ],
});
