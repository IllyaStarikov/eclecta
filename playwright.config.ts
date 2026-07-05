import { defineConfig, devices } from '@playwright/test';

// The site deploys at the root (base /) on eclecta.co; the e2e suite hits its
// OWN preview on a dedicated port. Never reuse :4321 — a dev server from
// another checkout on the default port once fed a test run the wrong site.
export const BASE_URL = 'http://localhost:4332';

export default defineConfig({
  testDir: 'tests/e2e',
  fullyParallel: true,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? 'github' : 'list',
  use: {
    baseURL: BASE_URL,
    contextOptions: { reducedMotion: 'reduce' },
    trace: 'retain-on-failure',
    ...devices['Desktop Chrome'],
  },
  webServer: {
    command: 'npx astro preview --port 4332',
    url: 'http://localhost:4332/',
    reuseExistingServer: false,
    timeout: 30_000,
  },
});
