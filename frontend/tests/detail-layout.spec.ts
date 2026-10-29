import { test, expect, type APIRequestContext } from "@playwright/test";
import { readFileSync } from "node:fs";

const samplePath = process.env.E2E_VIDEO_PATH!;
const payload = (name: string) => ({ name, mimeType: "video/mp4", buffer: Buffer.concat([readFileSync(samplePath), Buffer.from(name)]) });
async function save(request: APIRequestContext, name: string) {
  const response = await request.post("/api/v1/videos", { multipart: { file: payload(name) } });
  expect(response.status()).toBe(201);
  return response.json();
}

test.beforeAll(async ({ request }) => {
  expect((await (await request.get("/__test__/identity")).json()).fake_services_used).toBe(true);
  await request.put("/api/v1/settings/model", { data: { token: "fake-layout-only" } });
});

test("详情保留重新选择文件，取消不影响笔记，选新文件仅保存", async ({ page, request }) => {
  const video = await save(request, `原文件-${Date.now()}.mp4`);
  const { task_id } = await (await request.post(`/api/v1/videos/${video.id}/analyses`)).json();
  await expect.poll(async () => (await (await request.get(`/api/v1/tasks/${task_id}`)).json()).status, { timeout: 45000 }).toBe("succeeded");
  const analyses: string[] = [];
  page.on("request", request => { if (request.method() === "POST" && /\/analyses$/.test(request.url())) analyses.push(request.url()); });
  await page.goto(`/#summary/${video.id}`);
  const reselect = page.getByRole("button", { name: "重新选择文件", exact: true });
  await expect(reselect).toBeVisible();
  await expect(page.getByRole("button", { name: "回到笔记 ↓", exact: true })).toBeVisible();
  let chooser = page.waitForEvent("filechooser");
  await reselect.click();
  await (await chooser).setFiles([]);
  await expect(page.locator(".overview p").first()).toContainText("这是自动化测试返回的总结");
  chooser = page.waitForEvent("filechooser");
  await reselect.click();
  await (await chooser).setFiles({ name: "程序.exe", mimeType: "application/octet-stream", buffer: Buffer.from("executable") });
  await expect(page.getByRole("alert").filter({ hasText: "请选择支持的视频或文档文件。" })).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`#summary/${video.id}$`));
  await expect(page.locator(".overview p").first()).toContainText("这是自动化测试返回的总结");
  chooser = page.waitForEvent("filechooser");
  await reselect.click();
  const next = payload(`新文件-${Date.now()}.mp4`);
  await (await chooser).setFiles(next);
  await expect(page.locator(".player-panel h2")).toHaveText(next.name);
  await expect(page.getByLabel("任务动态记录", { exact: true }).getByText("视频已保存，等待开始处理", { exact: true })).toBeVisible();
  await expect(page.locator(".overview")).toHaveCount(0);
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(reselect).toBeVisible();
  expect(analyses).toHaveLength(0);
});

test("文件页和播放器随工作区伸展，宽窄窗口无固定宽度限制", async ({ page, request }) => {
  const video = await save(request, `布局-${Date.now()}.mp4`);
  for (const width of [1920, 1280, 887, 537, 531]) {
    await page.setViewportSize({ width, height: 871 });
    await page.goto(`/#summary/${video.id}`);
    await expect(page.locator(".player-panel h2")).toHaveText(video.filename);
    const bounds = await page.locator("main").evaluate(main => {
      const box = main.getBoundingClientRect();
      const style = getComputedStyle(main);
      const player = main.querySelector(".player-panel")!.getBoundingClientRect();
      const task = main.querySelector(".task-panel")!.getBoundingClientRect();
      return { available: box.width - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight), playerWidth: player.width, playerBottom: player.bottom, taskTop: task.top };
    });
    expect(Math.abs(bounds.playerWidth - bounds.available)).toBeLessThan(1);
    expect(bounds.taskTop).toBeGreaterThan(bounds.playerBottom);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: test.info().outputPath(`detail-${width}.png`), fullPage: false });
    await page.locator(".detail-back").click();
    await expect(page.locator(".file-upload")).toBeVisible();
    const gap = await page.locator("main").evaluate(main => {
      const style = getComputedStyle(main);
      return main.getBoundingClientRect().width - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight) - main.querySelector(".file-library")!.getBoundingClientRect().width;
    });
    expect(Math.abs(gap)).toBeLessThan(1);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: test.info().outputPath(`files-${width}.png`), fullPage: false });
  }
});
