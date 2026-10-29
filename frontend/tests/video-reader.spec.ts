import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";
import path from "node:path";

test.skip(!process.env.E2E_MEDIA_DIR, "需要隔离的多格式视频样本。");

test.beforeEach(async ({ request }) => {
  expect((await (await request.get("/__test__/identity")).json()).fake_services_used).toBe(true);
  for (const video of await (await request.get("/api/v1/videos")).json()) {
    if (video.latest_task && ["queued", "running", "stopping"].includes(video.latest_task.status)) {
      await request.post(`/api/v1/tasks/${video.latest_task.id}/stop`);
      await expect.poll(async () => (await (await request.get(`/api/v1/tasks/${video.latest_task.id}`)).json()).status).toBe("paused");
    }
    expect((await request.delete(`/api/v1/videos/${video.id}`)).ok()).toBe(true);
  }
});

for (const extension of ["mp4", "webm", "mkv"]) {
  test(`${extension} 共用接收入口，显示实际格式并准备可播放内容`, async ({ page }) => {
    const buffer = Buffer.concat([readFileSync(path.join(process.env.E2E_MEDIA_DIR!, `sample.${extension}`)), Buffer.from(test.info().testId)]);
    const posts: string[] = [];
    page.on("request", req => { if (req.method() === "POST") posts.push(req.url()); });
    await page.goto("/#summary");
    const input = page.getByLabel("选择本地文件", { exact: true });
    await expect(input).toHaveAttribute("accept", /\.webm.*\.mkv.*\.avi/);
    await input.setInputFiles({ name: `多格式.${extension}`, mimeType: "application/octet-stream", buffer });
    await expect(page).toHaveURL(/#summary\/[^/]+$/);
    await expect(page.locator("video")).toHaveAttribute("src", extension === "mkv" ? /\/playback\/content$/ : /\/content$/, { timeout: 20000 });
    await expect.poll(() => page.locator("video").evaluate((video: HTMLVideoElement) => video.readyState)).toBeGreaterThanOrEqual(1);
    expect(posts.filter(url => url.endsWith("/analyses"))).toHaveLength(0);
    if (extension === "mp4") expect(posts.filter(url => url.endsWith("/playback"))).toHaveLength(0);
    await page.screenshot({ path: test.info().outputPath(`${extension}-detail.png`) });
    await page.reload();
    await expect.poll(() => page.locator("video").evaluate((video: HTMLVideoElement) => video.readyState)).toBeGreaterThanOrEqual(1);
    await page.locator(".detail-back").click();
    const row = page.locator(".file-list tr").filter({ hasText: `多格式.${extension}` });
    await expect(row.getByText(`视频 · ${extension.toUpperCase()}`, { exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}

test("实际解码错误触发一次后台副本，跳转位置保留", async ({ page, request }) => {
  const buffer = Buffer.concat([readFileSync(path.join(process.env.E2E_MEDIA_DIR!, "sample.mp4")), Buffer.from(test.info().testId)]);
  const video = await (await request.post("/api/v1/videos", { multipart: { file: { name: "解码失败.mp4", mimeType: "video/mp4", buffer } } })).json();
  let posts = 0;
  page.on("request", req => { if (req.method() === "POST" && req.url().endsWith("/playback")) posts++; });
  await page.goto(`/#summary/${video.id}`);
  await expect.poll(() => page.locator("video").evaluate((video: HTMLVideoElement) => video.readyState)).toBeGreaterThanOrEqual(1);
  await page.locator("video").evaluate((video: HTMLVideoElement) => {
    video.currentTime = 35;
    Object.defineProperty(video, "error", { configurable: true, value: { code: 3 } });
    video.dispatchEvent(new Event("error"));
    delete (video as any).error;
  });
  await expect(page.locator("video")).toHaveAttribute("src", /\/playback\/content$/, { timeout: 20000 });
  await expect.poll(() => page.locator("video").evaluate((video: HTMLVideoElement) => video.currentTime)).toBeGreaterThan(34);
  expect(posts).toBe(1);
});

for (const codec of ["av1", "vp9"]) {
  test(`完整 ${codec} MP4 直接播放原件且不请求转码`, async ({ page, request }) => {
    const buffer = readFileSync(path.join(process.env.E2E_MEDIA_DIR!, `sample-${codec}.mp4`));
    const video = await (await request.post("/api/v1/videos", { multipart: { file: { name: `${codec}.mp4`, mimeType: "video/mp4", buffer } } })).json();
    expect(video.media_info.video.codec_name).toBe(codec);
    let posts = 0;
    page.on("request", req => { if (req.method() === "POST" && req.url().endsWith("/playback")) posts++; });
    await page.goto(`/#summary/${video.id}`);
    await expect(page.locator("video")).toHaveAttribute("src", `/api/v1/videos/${video.id}/content`);
    await page.locator("video").evaluate(async (video: HTMLVideoElement) => { video.muted = true; await video.play(); });
    await expect.poll(() => page.locator("video").evaluate((video: HTMLVideoElement) => video.currentTime)).toBeGreaterThan(0);
    expect(posts).toBe(0);
    await page.reload();
    await expect(page.locator("video")).toHaveAttribute("src", `/api/v1/videos/${video.id}/content`);
    expect(posts).toBe(0);
  });
}

test("已有副本仍先播放原件，解码失败才复用副本且不循环转换", async ({ page, request }) => {
  const buffer = readFileSync(path.join(process.env.E2E_MEDIA_DIR!, "sample.mp4"));
  const video = await (await request.post("/api/v1/videos", { multipart: { file: { name: "已有副本.mp4", mimeType: "video/mp4", buffer } } })).json();
  const endpoint = `/api/v1/videos/${video.id}/playback`;
  await request.post(endpoint);
  await expect.poll(async () => (await (await request.get(endpoint)).json()).status, { timeout: 20000 }).toBe("ready");
  let posts = 0;
  page.on("request", req => { if (req.method() === "POST" && req.url().endsWith("/playback")) posts++; });
  await page.goto(`/#summary/${video.id}`);
  await expect(page.locator("video")).toHaveAttribute("src", `/api/v1/videos/${video.id}/content`);
  await expect.poll(() => page.locator("video").evaluate((video: HTMLVideoElement) => video.readyState)).toBeGreaterThanOrEqual(1);
  expect(posts).toBe(0);
  await page.locator("video").evaluate((video: HTMLVideoElement) => {
    video.currentTime = 35;
    Object.defineProperty(video, "error", { configurable: true, value: { code: 3 } });
    video.dispatchEvent(new Event("error"));
    delete (video as any).error;
  });
  await expect(page.locator("video")).toHaveAttribute("src", endpoint + "/content");
  await expect.poll(() => page.locator("video").evaluate((video: HTMLVideoElement) => video.currentTime)).toBeGreaterThan(34);
  expect(posts).toBe(1);
  await page.locator("video").evaluate((video: HTMLVideoElement) => {
    Object.defineProperty(video, "error", { configurable: true, value: { code: 3 } });
    video.dispatchEvent(new Event("error"));
    delete (video as any).error;
  });
  await expect(page.getByRole("alert").filter({ hasText: "浏览器无法播放这个文件" })).toBeVisible();
  expect(posts).toBe(1);
});

for (const status of ["preparing", "failed"]) {
  test(`副本${status}时可播放的原件不显示准备或失败提示`, async ({ page, request }) => {
    const buffer = readFileSync(path.join(process.env.E2E_MEDIA_DIR!, "sample.mp4"));
    const video = await (await request.post("/api/v1/videos", { multipart: { file: { name: "原件可播.mp4", mimeType: "video/mp4", buffer } } })).json();
    let posts = 0;
    await page.route(`**/api/v1/videos/${video.id}/playback`, async route => {
      if (route.request().method() === "POST") posts++;
      await route.fulfill({ json: { status, direct: true, mime_type: "video/mp4", original_url: `/api/v1/videos/${video.id}/content`,
        url: null, error: status === "failed" ? { message: "旧副本失败", error_id: "old-copy" } : null } });
    });
    await page.goto(`/#summary/${video.id}`);
    await expect.poll(() => page.locator("video").evaluate((video: HTMLVideoElement) => video.readyState)).toBeGreaterThanOrEqual(1);
    await expect(page.locator("video")).toHaveAttribute("src", `/api/v1/videos/${video.id}/content`);
    await expect(page.getByText("正在准备播放视频", { exact: true })).toHaveCount(0);
    await expect(page.getByText("播放视频暂不可用，可继续开始处理。", { exact: true })).toHaveCount(0);
    await expect(page.getByRole("alert")).toHaveCount(0);
    expect(posts).toBe(0);
  });
}

test("准备失败不阻止开始分析，轮询不重复弹窗，手动重试与刷新恢复", async ({ page, request }) => {
  const buffer = Buffer.concat([readFileSync(path.join(process.env.E2E_MEDIA_DIR!, "sample.mkv")), Buffer.from(test.info().testId)]);
  const video = await (await request.post("/api/v1/videos", { multipart: { file: { name: "播放重试.mkv", mimeType: "video/x-matroska", buffer } } })).json();
  let posts = 0, reads = 0, retry = false;
  await page.route(`**/api/v1/videos/${video.id}/playback`, async route => {
    if (retry) { await route.continue(); return; }
    const method = route.request().method();
    if (method === "POST") posts++;
    const failed = method === "GET" && posts > 0 && ++reads > 1;
    await route.fulfill({ json: { status: failed ? "failed" : posts ? "preparing" : "unprepared",
      direct: false, mime_type: "video/x-matroska", original_url: `/api/v1/videos/${video.id}/content`, url: null,
      error: failed ? { message: "播放副本测试失败", error_id: "playback-test-error", log_file: "log/test.log" } : null } });
  });
  await page.goto(`/#summary/${video.id}`);
  await expect(page.getByText("正在准备播放视频", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "开始处理", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "选择处理模型" })).toBeVisible();
  await page.getByRole("dialog").getByRole("button", { name: "取消", exact: true }).click();
  const toast = page.getByRole("alert").filter({ hasText: "播放副本测试失败" });
  await expect(toast).toBeVisible();
  await expect(toast).toHaveCount(1);
  await expect(page.getByRole("button", { name: "重试播放" })).toBeVisible();
  expect(posts).toBe(1);
  retry = true;
  await page.getByRole("button", { name: "重试播放" }).click();
  await expect(page.locator("video")).toHaveAttribute("src", /\/playback\/content$/, { timeout: 20000 });
  await page.reload();
  await expect(page.locator("video")).toHaveAttribute("src", /\/playback\/content$/);
  await expect(page.getByRole("alert").filter({ hasText: "播放副本测试失败" })).toHaveCount(0);
});
