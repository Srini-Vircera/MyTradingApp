import { defineConfig } from "@playwright/test";

// End-to-end tests run the built static export against a mocked API
// (every API request is intercepted in the test; nothing reaches a real server).
const executablePath = process.env.AQ_CHROMIUM_PATH;

export default defineConfig({
  testDir: "tests/e2e",
  timeout: 30_000,
  retries: 0,
  reporter: "list",
  use: {
    baseURL: "http://127.0.0.1:3100",
    launchOptions: executablePath ? { executablePath } : {},
  },
  webServer: {
    command: "node scripts/serve-static.mjs out 3100",
    url: "http://127.0.0.1:3100/",
    reuseExistingServer: false,
    timeout: 30_000,
  },
});
