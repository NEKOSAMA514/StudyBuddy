import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";

function isolatedVideo() {
  // 合法 MP4 free box 区分样本，防止去重上传后改写上一用例的视频时长和笔记依据。
  const tag = Buffer.from(`${test.info().title}/${test.info().project.name}`);
  const box = Buffer.alloc(8);
  box.writeUInt32BE(8 + tag.length);
  box.write("free", 4);
  return Buffer.concat([readFileSync(process.env.E2E_VIDEO_PATH!), box, tag]);
}

test("真实分段接口在第二段等待期间即可阅读、下载首段并追加后续正文", async ({ page, request }) => {
  expect((await (await request.get("/__test__/identity")).json()).fake_services_used).toBe(true);
  const uploaded = await request.post("/api/v1/videos", { multipart: {
    file: { name: "分段展示回归.mp4", mimeType: "video/mp4", buffer: isolatedVideo() },
  } });
  expect(uploaded.ok()).toBe(true);
  const video = await uploaded.json();
  const started = await request.post(`/__test__/segmented/${video.id}`);
  expect(started.ok()).toBe(true);
  const { task_id } = await started.json();
  try {
    await page.goto(`/#summary/${video.id}`);
    await expect(page.locator(".notes-partial")).toContainText("1/2");
    const task = await (await request.get(`/api/v1/tasks/${task_id}`)).json();
    expect(task.status).toBe("running");
    expect(task.notes_state).toBe("partial");
    expect(task).not.toHaveProperty("result");
    await expect(page.locator("#note-block-C1")).toHaveCount(1);
    await expect(page.locator("#note-block-C2")).toHaveCount(0);
    await expect(page.locator(".task-panel .notice")).toContainText("已完成 1/2 段");
    await page.getByRole("button", { name: "查看已生成笔记 ↓", exact: true }).click();
    await expect(page.locator(".segment-overview")).toContainText("视频介绍 TCP 慢启动");
    await page.locator("#note-block-C1 .cite-button").click();
    await page.locator("#note-block-C1").evaluate((element) => { element.dataset.retained = "yes"; });
    for (const label of ["下载 TXT", "下载 Markdown"]) {
      const pending = page.waitForEvent("download");
      await page.locator("#video-notes").getByRole("link", { name: label, exact: true }).click();
      const download = await pending;
      expect(readFileSync((await download.path())!, "utf-8")).toContain("未完成笔记");
    }
    await page.screenshot({ path: `${process.env.E2E_REPORT_DIR}/segmented-live-${test.info().project.name}.png` });
    await request.post(`/__test__/segmented/${video.id}/release`);
    await expect(page.locator(".task-panel .status-pill")).toHaveText("已完成");
    await expect(page.locator("#note-block-C2")).toHaveCount(1);
    await expect(page.locator("#note-block-C1")).toHaveAttribute("data-retained", "yes");
    await expect(page.locator("#note-block-C1 .cite-button")).toHaveAttribute("aria-expanded", "true");
    await expect(page.locator(".notes-partial")).toHaveCount(0);
  } finally {
    await request.post(`/__test__/segmented/${video.id}/release`);
  }
});

test("普通视频大纲完成即显示导览，停止刷新后保留并在完成时更新", async ({ page, request }) => {
  const uploaded = await request.post("/api/v1/videos", { multipart: {
    file: { name: "普通视频导览回归.mp4", mimeType: "video/mp4", buffer: isolatedVideo() },
  } });
  expect(uploaded.ok()).toBe(true);
  const video = await uploaded.json();
  const endpoint = `/__test__/segmented/${video.id}`;
  const started = await request.post(`${endpoint}?preview=true`);
  expect(started.ok()).toBe(true);
  const { task_id } = await started.json();
  const preview = "视频介绍 TCP 慢启动与拥塞窗口增长。";
  try {
    await page.goto(`/#summary/${video.id}`);
    await expect(page.locator(".video-overview")).toHaveText(`材料导览${preview}`);
    const task = await (await request.get(`/api/v1/tasks/${task_id}`)).json();
    expect(task.status).toBe("running");
    expect(task.notes_state).toBe("none");
    expect(task).not.toHaveProperty("result");
    expect((await request.get(`/api/v1/analyses/${task_id}`)).status()).toBe(409);
    await expect(page.locator("#note-block-C1")).toHaveCount(0);
    await expect(page.locator(".notes-downloads")).toHaveCount(0);
    await page.getByRole("button", { name: "查看材料导览 ↓", exact: true }).click();
    await expect(page.locator(".empty-summary")).toHaveCount(0);
    await page.screenshot({ path: `${process.env.E2E_REPORT_DIR}/outline-preview-${test.info().project.name}.png` });
    await page.getByRole("button", { name: "停止处理", exact: true }).click();
    await expect(page.locator(".task-panel .status-pill")).toHaveText("已停止");
    await page.reload();
    await expect(page.locator(".video-overview")).toContainText(preview);
    await page.getByRole("button", { name: "继续处理", exact: true }).click();
    await expect(page.locator(".task-panel .status-pill")).toHaveText("处理中");
    await expect(page.locator(".video-overview")).toContainText(preview);
    await request.post(`${endpoint}/release?preview=true`);
    await expect(page.locator(".task-panel .status-pill")).toHaveText("已完成");
    await expect(page.locator(".video-overview")).toHaveCount(1);
    await expect(page.locator(".video-overview")).toContainText("这是自动化测试返回的总结");
    await expect(page.locator("#note-block-C1")).toHaveCount(1);
    await expect(page.locator(".notes-downloads")).toHaveCount(1);
    await expect(page.getByRole("button", { name: "查看材料导览 ↓", exact: true })).toHaveCount(0);
  } finally {
    await request.post(`${endpoint}/release?preview=true`);
  }
});
