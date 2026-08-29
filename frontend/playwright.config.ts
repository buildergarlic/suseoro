import { defineConfig, devices } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";

const frontendDir = path.dirname(fileURLToPath(import.meta.url));
const repositoryDir = path.resolve(frontendDir, "..");

export default defineConfig({
  testDir: "./e2e",
  outputDir: path.join(repositoryDir, "output", "playwright", "test-results"),
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["line"]],
  use: {
    baseURL: "http://127.0.0.1:4173",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
    ...devices["Desktop Chrome"],
  },
  webServer: [
    {
      command: "uv run python tests/e2e_server.py",
      cwd: path.join(repositoryDir, "backend"),
      url: "http://127.0.0.1:8000/__e2e__/health",
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      command: "npm run dev -- --host 127.0.0.1 --port 4173 --strictPort",
      cwd: frontendDir,
      url: "http://127.0.0.1:4173/login",
      reuseExistingServer: false,
      timeout: 120_000,
    },
  ],
});
