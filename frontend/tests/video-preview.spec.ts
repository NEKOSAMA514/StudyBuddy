import { test, expect, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";

test.beforeEach(async ({ request }) => {
  expect((await (await request.get("/__test__/identity")).json()).fake_services_used).toBe(true);
  await request.put("/api/v1/settings/model", { data: { token: "fake-preview", input_text_limit: 25000 } });
  await request.put("/api/v1/settings/note-style", { data: { note_style: "detailed", summary_prompt: "" } });
});

async function upload(page: Page, suffix: string) {
  const response = await page.request.post("/api/v1/videos", { multipart: { file: {
    name: "预估课程.mp4", mimeType: "video/mp4",
    buffer: Buffer.concat([readFileSync(process.env.E2E_VIDEO_PATH!), Buffer.from(`${suffix}-${test.info().project.name}`)]),
  } } });
  expect(response.ok()).toBe(true);
  return response.json();
}

async function open(page: Page, id: string) {
  await page.goto(`/#summary/${id}`);
  await page.getByRole("button", { name: /^(开始处理|重新生成笔记)$/ }).click();
  return page.getByRole("dialog", { name: "选择处理模型" });
}

test("未识别提示、已有材料字符与段数，打开预估不创建任务", async ({ page }) => {
  const video = await upload(page, "ready");
  let dialog = await open(page, video.id);
  await expect(dialog.locator(".video-analysis-preview")).toContainText("识别完成后可查看字符数与预计段数");
  expect(await (await page.request.get(`/api/v1/videos/${video.id}/analyses`)).json()).toEqual([]);
  await dialog.getByRole("button", { name: "取消", exact: true }).click();
  const created = await (await page.request.post(`/api/v1/videos/${video.id}/analyses`)).json();
  await expect.poll(async () => (await (await page.request.get(`/api/v1/tasks/${created.task_id}`)).json()).status).toBe("succeeded");
  dialog = await open(page, video.id);
  const preview = await (await page.request.get(`/api/v1/videos/${video.id}/analysis-preview`)).json();
  await expect(dialog.locator(".video-analysis-preview")).toContainText(`${preview.character_count.toLocaleString()} 字符 · 预计 ${preview.estimated_segments} 段`);
  const versions = await (await page.request.get(`/api/v1/videos/${video.id}/analyses`)).json();
  expect(versions).toHaveLength(1);
  for (const width of [1280, 375]) {
    await page.setViewportSize({ width, height: 871 });
    for (const theme of ["light", "dark"]) {
      await page.locator("html").evaluate((element, value) => element.setAttribute("data-theme", value), theme);
      await dialog.evaluate(element => { element.scrollTop = 0; });
      await page.screenshot({ path: test.info().outputPath(`video-preview-${width}-${theme}.png`), animations: "disabled" });
      expect(await dialog.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
    }
  }
});

test("切换风格和API重新估算，迟到响应不覆盖新选择，失败不阻止提交", async ({ page }) => {
  const video = await upload(page, "race");
  const created = await (await page.request.post("/api/v1/settings/model/profiles", { data: {
    name: "预估小预算", token: "fake-preview-small", input_text_limit: 5500,
  } })).json();
  const profile = created.profiles.at(-1);
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  let first = true;
  await page.route(`**/videos/${video.id}/analysis-preview?*`, async route => {
    const params = new URL(route.request().url()).searchParams;
    if (params.get("note_style") === "detailed" && first) {
      first = false;
      await held;
      await route.fulfill({ json: { character_count: 111, estimated_segments: 9, reason: null } }).catch(() => {});
    } else if (params.get("llm_profile_id") === profile.id) {
      await route.fulfill({ json: { character_count: 20000, estimated_segments: 5, reason: null } });
    } else {
      await route.fulfill({ json: { character_count: 20000, estimated_segments: 2, reason: null } });
    }
  });
  const dialog = await open(page, video.id);
  await expect(dialog.locator(".video-analysis-preview")).toContainText("正在检查已有识别材料");
  await dialog.getByLabel("笔记风格").selectOption("concise");
  await expect(dialog.locator(".video-analysis-preview")).toContainText("20,000 字符 · 预计 2 段");
  release();
  await dialog.getByLabel("API 配置").selectOption(profile.id);
  await expect(dialog.locator(".video-analysis-preview")).toContainText("20,000 字符 · 预计 5 段");
  await expect(dialog.locator(".video-analysis-preview")).not.toContainText("111 字符");
  await page.unroute(`**/videos/${video.id}/analysis-preview?*`);
  await page.route(`**/videos/${video.id}/analysis-preview?*`, route => route.fulfill({ status: 503, json: { message: "预估读取失败" } }));
  await dialog.getByLabel("笔记风格").selectOption("detailed");
  await expect(dialog.locator(".video-analysis-preview")).toContainText("暂无法读取材料规模");
  await expect(dialog.getByRole("button", { name: "开始处理", exact: true })).toBeEnabled();
  await dialog.getByRole("button", { name: "取消", exact: true }).click();
  await page.request.delete(`/api/v1/settings/model/profiles/${profile.id}`);
});
