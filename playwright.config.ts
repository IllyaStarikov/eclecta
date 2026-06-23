import { defineConfig, devices } from '@playwright/test';

// The site deploys at the root (base /) on eclecta.co; the e2e suite hits the
// local preview root.
export const BASE_URL = 'http://localhost:4321';

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
    command: 'npx astro preview',
    url: 'http://localhost:4321/',
    reuseExistingServer: true,
    timeout: 30_000,
  },
});
