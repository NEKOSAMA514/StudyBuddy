import { defineConfig } from "@playwright/test";
import path from "node:path";

if (!process.env.E2E_BASE_URL || !process.env.E2E_REPORT_DIR) {
  throw new Error("请通过 scripts/run-tests.ps1 -Suite Browser 启动隔离测试。");
}
const reportDir = process.env.E2E_REPORT_DIR;

export default defineConfig({
  testDir: "./tests",
  timeout: 60000,
  workers: 1,
  outputDir: path.join(reportDir, "artifacts"),
  use: {
    baseURL: process.env.E2E_BASE_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    { name: "desktop-887", use: { viewport: { width: 887, height: 871 } } },
    { name: "desktop-1280", use: { viewport: { width: 1280, height: 871 } } },
  ],
  reporter: [
    ["list"],
    ["json", { outputFile: path.join(reportDir, "results.json") }],
    ["html", { outputFolder: path.join(reportDir, "html"), open: "never" }],
  ],
});
