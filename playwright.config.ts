import { defineConfig, devices } from '@playwright/test';

// The site deploys under base /lede — every URL in the e2e suite includes it.
export const BASE_URL = 'http://localhost:4321/lede';

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
    url: 'http://localhost:4321/lede/',
    reuseExistingServer: true,
    timeout: 30_000,
  },
});
