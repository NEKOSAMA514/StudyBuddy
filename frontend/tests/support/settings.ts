import type { Route } from "@playwright/test";
import { modelBudgets } from "./modelContract";

// Only shared, read-only settings. Scenario-specific routes remain in each test.
const profiles = { active_id: "default", profiles: [{ ...modelBudgets, id: "default", name: "测试 API", model: "fake", has_token: true,
  base_url: "https://test.invalid", stream: false, json_mode: false, free_tier_confirmed: true, section_concurrency: 3, request_interval_seconds: 3 }] };
export const settingsValues: Record<string, unknown> = {
  "/settings/note-style": { note_style: "detailed", summary_prompt: "" },
  "/settings/mineru": { has_token: false },
  "/settings/model/profiles": profiles,
  "/settings/model": profiles.profiles[0],
  "/settings/network": { enabled: false, url: "" },
  "/settings/recognition": { settings: { asr_option: "paraformer", ocr_option: "rapidocr", language: "auto" }, active_download: null,
    models: ["paraformer", "rapidocr"].map(id => ({ id, kind: id === "paraformer" ? "asr" : "ocr", name: id,
      ready: true, runtime_ready: true, directory_exists: true, directory: id, description: "测试", size: "test", docs: "", download: null })) },
};
export async function settingsRead(route: Route): Promise<boolean> {
  if (route.request().method() !== "GET") return false;
  const path = new URL(route.request().url()).pathname.replace("/api/v1", "");
  if (!(path in settingsValues)) return false;
  await route.fulfill({ json: settingsValues[path] });
  return true;
}
