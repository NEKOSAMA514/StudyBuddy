import { taskResponse } from "./support/taskContract";
import { settingsRead } from "./support/settings";
import { test, expect, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";

test.beforeAll(async ({ request, baseURL }) => {
  if (process.env.E2E_SOURCE_TESTS) expect(new URL(baseURL!).port).not.toBe("18000");
  else expect((await (await request.get("/__test__/identity")).json()).fake_services_used).toBe(true);
});

async function mockDownloads(page: Page) {
  const login = { state: "logged_in", persistent: true, poll: "waiting", statusReads: 0, polls: 0, ids: [] as string[], cancelled: [] as string[] };
  const state = { posts: [] as string[], selections: [] as unknown[], starts: 0, done: false, fail: false, failPoll: false, failImport: false, failAnalysis: false, ext: "webm", finalExt: null as string | null, stage: "downloading", progress: 50 as number | null, heights: [144, 240, 360, 480, 720, 1080], defaultQuality: 360 as number | null, thumbnail: "https://test.invalid/cover.svg" as string | null };
  let task: any = null;
  const video = { id: "imported-video", filename: "链接下载测试视频-360P.webm", size_bytes: 1024, duration: 125, created_at: "2026-10-12T00:00:00Z", latest_task: null as any };
  const videos: typeof video[] = [];
  const downloadName = () => `链接下载测试视频-360P.${state.ext}`;
  const metadata = () => ({ url: "https://www.bilibili.com/video/test", title: "链接下载测试视频", thumbnail: state.thumbnail, duration: 125,
    qualities: state.heights, default_quality: state.defaultQuality,
    quality_options: state.heights.map(height => ({ height, ext: height === 360 ? state.ext : "mp4",
      filesize: height === 1080 ? null : height === 360 ? 25 * 1024 ** 2 : 10 * 1024 ** 2, approximate: height === 360 })) });
  await page.addInitScript(() => {
    (window as any).downloadNotifications = [];
    window.addEventListener("studybuddy-error", event => (window as any).downloadNotifications.push((event as CustomEvent).detail.message));
  });
  await page.route("https://test.invalid/cover.svg", route => route.fulfill({ contentType: "image/svg+xml", body: '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180"><rect width="320" height="180" fill="#e7edef"/></svg>' }));
  await page.route("**/api/v1/**", async route => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() === "POST") state.posts.push(path);
    if (await settingsRead(route)) return;
    let body: unknown;
    if (path === "/api/v1/health") body = { max_upload_bytes: 524288000, max_duration_seconds: 1200, supported_video_extensions: ["mp4", "webm"] };
    else if (path === "/api/v1/bilibili/session") {
      login.statusReads++;
      if (route.request().method() === "DELETE") login.state = "logged_out";
      body = { state: login.state, persistent: login.persistent, can_persist: true, warning: "" };
    } else if (path === "/api/v1/bilibili/login") {
      const id = route.request().postDataJSON().id;
      login.ids.push(id);
      body = { id, state: "waiting", image: "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jK7sAAAAASUVORK5CYII=" };
    } else if (path.startsWith("/api/v1/bilibili/login/")) {
      const id = path.split("/")[5];
      if (route.request().method() === "DELETE") { login.cancelled.push(id); body = { state: "cancelled" }; }
      else {
        login.polls++;
        if (login.poll === "success") login.state = "logged_in";
        body = { id, state: login.poll, session: { state: login.state, persistent: login.persistent, can_persist: true, warning: "" } };
      }
    }
    else if (path === "/api/v1/videos") body = videos;
    else if (path === "/api/v1/documents") body = [];
    else if (path === "/api/v1/video-downloads/parse") body = { ...metadata(), url: route.request().postDataJSON().url };
    else if (path === "/api/v1/video-downloads") {
      const input = route.request().postDataJSON();
      task = { id: String(++state.starts).padStart(32, "0"), url: input.url, quality: input.quality,
        metadata: metadata(), status: "running", stage: state.stage, progress: state.progress, format_id: "video-test", path: null, relative_path: null, error: null };
      body = task;
    } else if (path.endsWith("/import")) {
      if (state.failImport) { await route.fulfill({ status: 400, json: { error: { message: "下载文件无法读取", error_id: "import-test" } } }); return; }
      video.filename = downloadName();
      if (!videos.length) videos.push(video);
      body = video;
    } else if (path === "/api/v1/videos/imported-video/analyses") {
      if (route.request().method() === "GET") { await route.fulfill({ json: [] }); return; }
      state.selections.push(route.request().postDataJSON());
      if (state.failAnalysis) { await route.fulfill({ status: 400, json: { error: { message: "所选模型无法使用", error_id: "analysis-test" } } }); return; }
      video.latest_task = taskResponse({ id: "imported-task", video_id: video.id, status: "queued", stage: "queued", config: {}, error: null, warnings: [], timings: {}, notes_state: "none", notes_revision: 0 });
      body = { task_id: video.latest_task.id, status: video.latest_task.status };
    } else if (path === "/api/v1/tasks/imported-task") body = video.latest_task;
    else if (path === "/api/v1/videos/imported-video/evidence") body = [];
    else if (path === "/api/v1/videos/imported-video/playback") body = { status: "unprepared", direct: true, mime_type: "video/mp4", original_url: "/api/v1/videos/imported-video/content", url: null, error: null };
    else if (path === "/api/v1/videos/imported-video/content") {
      await route.fulfill({ contentType: "video/mp4", body: readFileSync(process.env.E2E_VIDEO_PATH!) }); return;
    }
    else if (path.startsWith("/api/v1/video-downloads/")) {
      if (state.failPoll) { await route.abort(); return; }
      body = state.fail ? { ...task, status: "failed", stage: "failed", progress: null, error: {
        message: "下载器拒绝请求", error_id: "download-error-" + state.starts, log_file: "log/download.log",
      } } : state.done ? { ...task, status: "succeeded", stage: "completed", progress: 100,
        metadata: state.finalExt ? { ...task.metadata, quality_options: task.metadata.quality_options.map((option: any) =>
          option.height === task.quality ? { ...option, ext: state.finalExt, filesize: 20 * 1024 ** 2, approximate: false } : option) } : task.metadata,
        path: "E:\\self-learning\\codex\\studybuddy\\data\\downloads\\链接下载测试视频-360P\\" + downloadName(),
        relative_path: "downloads/链接下载测试视频-360P/" + downloadName() } : { ...task, stage: state.stage, progress: state.progress };
    } else if (path === "/api/v1/diagnostics/client-errors") body = { error_id: "network-test", log_saved: true, message: "读取状态失败" };
    else if (path.endsWith("/analysis-preview")) body = { character_count: null, estimated_segments: null, reason: "识别材料尚未就绪。" };
    else throw new Error("出现范围外接口：" + path);
    await route.fulfill({ json: body });
  });
  return Object.assign(state, { login });
}

async function preview(page: Page) {
  await page.goto("/#summary");
  await page.getByLabel("视频链接", { exact: true }).fill("https://www.bilibili.com/video/test");
  await page.getByRole("button", { name: "解析链接", exact: true }).click();
  await expect(page.getByRole("heading", { name: "链接下载测试视频" })).toBeVisible();
}

for (const [kind, shared] of [
  ["长链接分享", "【【魂游人物志】惊人的老人】 https://www.bilibili.com/video/BV1umnw6rELq/?share_source=copy_web&vd_source=tracking"],
  ["短链接分享", "【在绀碧大厦击杀重锤，提前拿下八星铳！-哔哩哔哩】 https://b23.tv/pVpZMMe"],
  ["BV号", "BV1umnw6rELq"],
  ["大写协议链接", "HTTPS://WWW.BILIBILI.COM/video/BV1umnw6rELq"],
]) {
  for (const loggedIn of [true, false]) {
    test(`${kind}可直接解析并保留登录衔接（${loggedIn ? "已登录" : "未登录"}）`, async ({ page }) => {
      const state = await mockDownloads(page);
      state.login.state = loggedIn ? "logged_in" : "logged_out";
      await page.goto("/#summary");
      await page.getByLabel("视频链接", { exact: true }).fill(shared);
      await page.getByRole("button", { name: "解析链接", exact: true }).click();
      if (!loggedIn) {
        const dialog = page.getByRole("dialog", { name: "B 站登录", exact: true });
        await expect(dialog).toBeVisible();
        expect(state.posts).not.toContain("/api/v1/video-downloads/parse");
        await dialog.getByRole("button", { name: "暂不登录", exact: true }).click();
      }
      await expect(page.getByRole("heading", { name: "链接下载测试视频", exact: true })).toBeVisible();
      expect(state.posts.filter(path => path === "/api/v1/video-downloads/parse")).toHaveLength(1);
      expect(state.starts).toBe(0);
    });
  }
}

test("链接入口默认360P，各档显示格式、实际估算或未知大小且不下载", async ({ page }) => {
  const state = await mockDownloads(page);
  await preview(page);
  await expect(page.getByAltText("视频封面")).toBeVisible();
  await expect(page.getByLabel("下载画质", { exact: true })).toHaveValue("360");
  await expect(page.getByRole("option", { name: "360P · WEBM · 约 25.0 MB", exact: true })).toHaveCount(1);
  await expect(page.getByRole("option", { name: "480P · MP4 · 10.0 MB", exact: true })).toHaveCount(1);
  await expect(page.getByRole("option", { name: "1080P · MP4 · 大小未知", exact: true })).toHaveCount(1);
  await expect(page.getByRole("option", { name: "144P · MP4 · 10.0 MB", exact: true })).toHaveCount(1);
  await expect(page.getByRole("option", { name: "240P · MP4 · 10.0 MB", exact: true })).toHaveCount(1);
  expect(state.starts).toBe(0);
  const download = await page.getByRole("region", { name: "视频链接下载", exact: true }).boundingBox();
  const upload = await page.getByRole("region", { name: "文件上传", exact: true }).boundingBox();
  expect(download!.y).toBeLessThan(upload!.y);
  await page.getByLabel("视频链接", { exact: true }).fill("https://youtube.com/watch?v=new");
  await expect(page.getByLabel("下载画质", { exact: true })).toHaveCount(0);
});

test("确认后下载，刷新恢复进度，完成隐藏路径但可复制下载路径", async ({ page }) => {
  const state = await mockDownloads(page);
  await page.addInitScript(() => Object.defineProperty(navigator, "clipboard", { value: {
    writeText: async (text: string) => { (window as any).copiedDownloadPath = text; },
  } }));
  await preview(page);
  await page.getByRole("button", { name: "下载视频", exact: true }).click();
  await expect(page.getByRole("progressbar", { name: "下载进度", exact: true })).toHaveAttribute("value", "50");
  await page.reload();
  await expect(page.getByRole("button", { name: "下载中…", exact: true })).toBeDisabled();
  state.done = true;
  await expect(page.getByText("下载完成", { exact: true })).toBeVisible();
  await expect(page.locator(".video-download output")).toHaveCount(0);
  await expect(page.getByText(/本地路径|数据目录中的位置|仅保存视频文件/)).toHaveCount(0);
  await page.getByRole("button", { name: "复制下载路径", exact: true }).click();
  expect(await page.evaluate(() => (window as any).copiedDownloadPath)).toBe("E:\\self-learning\\codex\\studybuddy\\data\\downloads\\链接下载测试视频-360P\\链接下载测试视频-360P.webm");
  expect(state.starts).toBe(1);
  expect(state.posts.every(path => path.startsWith("/api/v1/video-downloads"))).toBe(true);
  await expect(page.getByRole("region", { name: "文件列表", exact: true })).toContainText("0 个文件");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: test.info().outputPath("video-download.png"), fullPage: true });
});

test("用户所选清晰度保持不变，刷新恢复同一下载", async ({ page }) => {
  const state = await mockDownloads(page);
  await preview(page);
  await page.getByLabel("下载画质", { exact: true }).selectOption("720");
  const started = page.waitForRequest(request => request.method() === "POST" && new URL(request.url()).pathname === "/api/v1/video-downloads");
  await page.getByRole("button", { name: "下载视频", exact: true }).click();
  expect((await started).postDataJSON().quality).toBe(720);
  await expect(page.getByRole("progressbar", { name: "下载进度", exact: true })).toHaveAttribute("value", "50");
  await page.reload();
  await expect(page.getByLabel("下载画质", { exact: true })).toHaveValue("720");
  expect(state.starts).toBe(1);
});

test("进度只显示百分比，各内部阶段不显示详细操作", async ({ page }) => {
  const state = await mockDownloads(page);
  await preview(page);
  await page.getByRole("button", { name: "下载视频", exact: true }).click();
  await expect(page.locator(".video-download-status")).toHaveText("50.0%");
  await page.screenshot({ path: test.info().outputPath("download-progress.png"), fullPage: true });
  for (const [index, stage] of ["parsing", "merging", "checking"].entries()) {
    state.stage = stage;
    state.progress = 51 + index;
    await expect(page.locator(".video-download-status")).toHaveText(`${state.progress.toFixed(1)}%`);
  }
  state.progress = null;
  await expect(page.getByRole("progressbar", { name: "下载进度", exact: true })).not.toHaveAttribute("value", /.*/);
  await expect(page.locator(".video-download-status")).toHaveText("");
});

test("只有低清晰度时仍展示全部选项，用户可选择144P并下载", async ({ page }) => {
  const state = await mockDownloads(page);
  state.heights = [144, 240];
  state.defaultQuality = 240;
  await preview(page);
  await expect(page.getByLabel("下载画质", { exact: true })).toHaveValue("240");
  await page.getByLabel("下载画质", { exact: true }).selectOption("144");
  const started = page.waitForRequest(request => request.method() === "POST" && new URL(request.url()).pathname === "/api/v1/video-downloads");
  await page.getByRole("button", { name: "下载视频", exact: true }).click();
  expect((await started).postDataJSON().quality).toBe(144);
  state.done = true;
  await expect(page.getByText("下载完成", { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByLabel("下载画质", { exact: true })).toHaveValue("144");
  await expect(page.getByRole("alert")).toHaveCount(0);
});

test("完成后使用成品的实际格式和大小，刷新仍保持一致", async ({ page }) => {
  const state = await mockDownloads(page);
  await preview(page);
  await expect(page.getByRole("option", { name: "360P · WEBM · 约 25.0 MB", exact: true })).toHaveCount(1);
  await page.getByRole("button", { name: "下载视频", exact: true }).click();
  state.finalExt = "mp4";
  state.done = true;
  await expect(page.getByText("下载完成", { exact: true })).toBeVisible();
  await expect(page.getByRole("option", { name: "360P · MP4 · 20.0 MB", exact: true })).toHaveCount(1);
  await page.reload();
  await expect(page.getByRole("option", { name: "360P · MP4 · 20.0 MB", exact: true })).toHaveCount(1);
});

test("同样的下载错误每次新任务弹窗，轮询和刷新不重复弹出历史错误", async ({ page }) => {
  const state = await mockDownloads(page);
  state.fail = true;
  await preview(page);
  await page.getByRole("button", { name: "下载视频", exact: true }).click();
  await expect.poll(() => page.evaluate(() => (window as any).downloadNotifications.length)).toBe(1);
  await page.getByRole("button", { name: "下载视频", exact: true }).click();
  await expect.poll(() => page.evaluate(() => (window as any).downloadNotifications.length)).toBe(2);
  await page.reload();
  await expect(page.getByText("下载失败", { exact: true })).toBeVisible();
  expect(await page.evaluate(() => (window as any).downloadNotifications.length)).toBe(0);
  await expect(page.getByRole("alert")).toHaveCount(0);
});

test("没有360P时默认选择最接近的更高画质", async ({ page }) => {
  const state = await mockDownloads(page);
  state.heights = [480, 720, 1080];
  state.defaultQuality = 480;
  await preview(page);
  await expect(page.getByLabel("下载画质", { exact: true })).toHaveValue("480");
  await expect(page.getByRole("button", { name: "下载视频", exact: true })).toBeEnabled();
  await expect(page.getByRole("option", { name: /240P/ })).toHaveCount(0);
});

for (const firstFailure of [true, false]) test(`打开下载目录${firstFailure ? "首次失败" : "先成功再失败"}，仅发送任务编号且复制入口仍可用`, async ({ page }) => {
  const state = await mockDownloads(page);
  const requests: unknown[] = [];
  let fail = firstFailure;
  await page.route("**/api/v1/video-downloads/*/reveal", async route => {
    requests.push({ path: new URL(route.request().url()).pathname, body: route.request().postData() });
    await route.fulfill({ status: fail ? 400 : 200, json: fail ? { error: { message: "下载文件已移动", error_id: "reveal-moved" } } : { opened: true,
      path: "E:\\self-learning\\codex\\studybuddy\\data\\downloads\\链接下载测试视频-360P\\链接下载测试视频-360P.webm" } });
  });
  await preview(page);
  await expect(page.getByRole("button", { name: "打开文件所在位置" })).toHaveCount(0);
  await page.getByRole("button", { name: "下载视频", exact: true }).click();
  await expect(page.getByRole("button", { name: "打开文件所在位置" })).toHaveCount(0);
  state.done = true;
  await expect(page.locator(".video-download output")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "复制下载路径" })).toBeEnabled();
  await expect(page.getByRole("button", { name: "开始生成笔记" })).toBeEnabled();
  await expect(page.getByRole("alert")).toHaveCount(0);
  if (!firstFailure) {
    const opened = page.waitForResponse(response => response.url().endsWith("/reveal"));
    await page.getByRole("button", { name: "打开文件所在位置" }).click();
    const response = await opened;
    expect(response.ok()).toBe(true);
    expect((await response.json()).opened).toBe(true);
    await expect(page.getByRole("alert")).toHaveCount(0);
  }
  fail = true;
  await page.getByRole("button", { name: "打开文件所在位置" }).click();
  await expect(page.getByRole("alert")).toHaveCount(1);
  await expect(page.getByRole("alert")).toContainText("下载文件已移动");
  expect(requests).toEqual(Array.from({ length: firstFailure ? 1 : 2 }, () => ({
    path: `/api/v1/video-downloads/${"1".padStart(32, "0")}/reveal`, body: null,
  })));
  await expect(page.getByRole("button", { name: "复制下载路径" })).toBeEnabled();
  await expect(page.getByRole("button", { name: "开始生成笔记" })).toBeEnabled();
  expect(state.starts).toBe(1);
});

test("没有封面时预览区仍使用全部可用宽度", async ({ page }) => {
  const state = await mockDownloads(page);
  state.thumbnail = null;
  await preview(page);
  const previewBox = await page.locator(".video-link-preview").boundingBox();
  const contentBox = await page.locator(".video-link-preview > div").boundingBox();
  expect(contentBox!.width).toBeCloseTo(previewBox!.width, 0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test("项目更名后恢复旧下载编号，清除后刷新不会重新接管", async ({ page }) => {
  const state = await mockDownloads(page);
  await preview(page);
  await page.getByRole("button", { name: "下载视频", exact: true }).click();
  const id = "1".padStart(32, "0");
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem("studybuddy.videoDownloadTaskId"))).toBe(id);
  await page.evaluate(identifier => {
    sessionStorage.setItem("studynote.videoDownloadTaskId", identifier);
    sessionStorage.removeItem("studybuddy.videoDownloadTaskId");
  }, id);
  const reads: string[] = [];
  page.on("request", request => {
    if (request.method() === "GET" && new URL(request.url()).pathname === `/api/v1/video-downloads/${id}`) reads.push(request.url());
  });
  await page.reload();
  await expect.poll(() => reads.length).toBeGreaterThan(0);
  expect(await page.evaluate(() => ({
    current: sessionStorage.getItem("studybuddy.videoDownloadTaskId"),
    legacy: sessionStorage.getItem("studynote.videoDownloadTaskId"),
  }))).toEqual({ current: id, legacy: null });
  expect(state.starts).toBe(1);
  await page.evaluate(() => sessionStorage.removeItem("studybuddy.videoDownloadTaskId"));
  await page.reload();
  expect(await page.evaluate(() => sessionStorage.getItem("studybuddy.videoDownloadTaskId"))).toBeNull();
  expect(state.starts).toBe(1);
});

test("状态读取失败保留任务编号，手动重读后恢复原任务", async ({ page }) => {
  const state = await mockDownloads(page);
  await preview(page);
  state.failPoll = true;
  await page.getByRole("button", { name: "下载视频", exact: true }).click();
  await expect(page.getByRole("button", { name: "重新读取下载状态" })).toBeVisible();
  expect(await page.evaluate(() => sessionStorage.getItem("studybuddy.videoDownloadTaskId"))).toBeTruthy();
  state.failPoll = false;
  state.done = true;
  await page.getByRole("button", { name: "重新读取下载状态" }).click();
  await expect(page.getByText("下载完成", { exact: true })).toBeVisible();
  expect(state.starts).toBe(1);
});

test("下载完成后选择设置，取消不导入，确认后进入对应笔记页面", async ({ page }) => {
  const state = await mockDownloads(page);
  state.ext = "mp4";
  await preview(page);
  await expect(page.getByRole("button", { name: "开始生成笔记", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "下载视频", exact: true }).click();
  state.done = true;
  await page.getByRole("button", { name: "开始生成笔记", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "选择处理模型" });
  await expect(dialog).toBeVisible();
  await expect(page).toHaveURL(/#summary$/);
  await dialog.getByRole("button", { name: "取消", exact: true }).click();
  expect(state.posts.filter(path => path.endsWith("/import") || path.endsWith("/analyses"))).toEqual([]);
  await expect(page.getByRole("region", { name: "文件列表", exact: true })).toContainText("0 个文件");
  await page.getByRole("button", { name: "开始生成笔记", exact: true }).click();
  await dialog.getByLabel("OCR 模型", { exact: true }).selectOption("rapidocr");
  await dialog.getByRole("button", { name: "开始处理", exact: true }).click();
  await expect(page).toHaveURL(/#summary\/imported-video$/);
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "链接下载测试视频-360P.mp4", exact: true })).toBeVisible();
  await expect(page.locator("video")).toHaveAttribute("src", "/api/v1/videos/imported-video/content");
  expect(state.posts.slice(-2)).toEqual(["/api/v1/video-downloads/" + "1".padStart(32, "0") + "/import", "/api/v1/videos/imported-video/analyses"]);
  expect(state.selections).toEqual([{ asr_option: "paraformer", ocr_option: "rapidocr", llm_profile_id: "default", note_style: "detailed" }]);
  await page.reload();
  await expect(page.locator("video")).toHaveAttribute("src", "/api/v1/videos/imported-video/content");
  await page.getByRole("button", { name: "← 返回", exact: true }).click();
  await expect(page.getByRole("region", { name: "文件列表", exact: true })).toContainText("1 个文件");
  await expect(page.getByRole("button", { name: "开始生成笔记", exact: true })).toBeVisible();
});

test("导入失败停留设置，重试相同错误也通知，不创建分析任务", async ({ page }) => {
  const state = await mockDownloads(page);
  await preview(page);
  await page.getByRole("button", { name: "下载视频", exact: true }).click();
  state.done = true;
  state.failImport = true;
  await page.getByRole("button", { name: "开始生成笔记", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "选择处理模型" });
  for (const count of [1, 2]) {
    await dialog.getByRole("button", { name: "开始处理", exact: true }).click();
    await expect.poll(() => page.evaluate(() => (window as any).downloadNotifications.length)).toBe(count);
    await expect(page.getByRole("alert")).toHaveCount(1);
    await expect(page.getByRole("alert")).toContainText("下载文件无法读取");
    await expect(page).toHaveURL(/#summary$/);
    await expect(dialog).toBeVisible();
  }
  expect(state.selections).toEqual([]);
  await dialog.getByRole("button", { name: "取消", exact: true }).click();
  await expect(page.getByText("下载完成", { exact: true })).toBeVisible();
});

test("创建分析失败保留已导入文件，再次确认复用文件且可正常开始", async ({ page }) => {
  const state = await mockDownloads(page);
  state.ext = "mp4";
  await preview(page);
  await page.getByRole("button", { name: "下载视频", exact: true }).click();
  state.done = true;
  state.failAnalysis = true;
  await page.getByRole("button", { name: "开始生成笔记", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "选择处理模型" });
  await dialog.getByRole("button", { name: "开始处理", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("所选模型无法使用");
  await expect(page.getByRole("region", { name: "文件列表", exact: true })).toContainText("1 个文件");
  await expect(page.getByRole("region", { name: "文件列表", exact: true })).toContainText("未处理");
  await expect(dialog).toBeVisible();
  state.failAnalysis = false;
  await dialog.getByRole("button", { name: "开始处理", exact: true }).click();
  await expect(page).toHaveURL(/#summary\/imported-video$/);
  expect(state.starts).toBe(1);
  expect(state.selections.length).toBe(2);
});


test("sessionStorage 读写删除失败仍可创建并查看下载任务", async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(window, "sessionStorage", { get() { throw new DOMException("blocked", "SecurityError"); } });
  });
  const state = await mockDownloads(page);
  await preview(page);
  await page.getByRole("button", { name: "下载视频", exact: true }).click();
  await expect(page.getByRole("button", { name: "下载中…", exact: true })).toBeVisible();
  expect(state.starts).toBe(1);
  state.done = true;
  await expect(page.getByText("下载完成", { exact: true })).toBeVisible();
});

function partMetadata(selected: number, count = 3) {
  const parts = Array.from({ length: count }, (_, index) => ({
    page: index + 1, cid: 100 + index, title: `第${index + 1}讲`, duration: (index + 1) * 60,
    url: `https://www.bilibili.com/video/BV1UhpK6RErH?p=${index + 1}`,
  }));
  return { url: parts[selected - 1].url, title: `P${selected} 第${selected}讲`, thumbnail: null,
    duration: parts[selected - 1].duration, qualities: selected === 2 ? [480, 720] : [360, 720],
    default_quality: selected === 2 ? 480 : 360, parts, selected_part: selected };
}

test("多 P 默认首 P，切换更新时长画质并下载明确的 P 链接", async ({ page }) => {
  await mockDownloads(page);
  await page.route("**/api/v1/video-downloads/parse", route => {
    const input = new URL(route.request().postDataJSON().url);
    return route.fulfill({ json: partMetadata(Number(input.searchParams.get("p") || 1)) });
  });
  await page.goto("/#summary");
  await page.getByLabel("视频链接", { exact: true }).fill("https://www.bilibili.com/video/BV1UhpK6RErH");
  await page.getByRole("button", { name: "解析链接", exact: true }).click();
  await expect(page.getByLabel("视频分 P")).toHaveValue("1");
  await expect(page.getByRole("option", { name: "P2 · 第2讲 · 2:00", exact: true })).toHaveCount(1);
  await page.getByLabel("视频分 P").selectOption("2");
  await expect(page.getByRole("heading", { name: "P2 第2讲", exact: true })).toBeVisible();
  await expect(page.getByText("时长：2:00", { exact: true })).toBeVisible();
  await expect(page.getByLabel("下载画质", { exact: true })).toHaveValue("480");
  if (test.info().project.name === "desktop-887") {
    await page.setViewportSize({ width: 598, height: 871 });
    await page.getByRole("heading", { name: "P2 第2讲", exact: true }).click();
    await page.evaluate(() => window.scrollTo(0, 0));
    await expect.poll(() => page.locator("#main-sidebar").evaluate(element => element.getBoundingClientRect().right)).toBeLessThanOrEqual(0);
    const panel = page.getByRole("region", { name: "视频链接下载", exact: true });
    expect(await panel.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
    expect((await panel.boundingBox())!.x).toBeGreaterThanOrEqual(0);
    await page.screenshot({ path: test.info().outputPath("bilibili-parts-598.png"), fullPage: true, animations: "disabled" });
  }
  const request = page.waitForRequest(request => request.method() === "POST" && new URL(request.url()).pathname === "/api/v1/video-downloads");
  await page.getByRole("button", { name: "下载视频", exact: true }).click();
  expect((await request).postDataJSON()).toEqual({ url: partMetadata(2).url, quality: 480 });
});

test("指定 P 默认选中，快速切换时旧响应不会覆盖最新选择", async ({ page }) => {
  await mockDownloads(page);
  let release!: () => void;
  const delayed = new Promise<void>(resolve => { release = resolve; });
  let secondRequested = false;
  await page.route("**/api/v1/video-downloads/parse", async route => {
    const selected = Number(new URL(route.request().postDataJSON().url).searchParams.get("p") || 1);
    if (selected === 2) { secondRequested = true; await delayed; }
    await route.fulfill({ json: partMetadata(selected) });
  });
  await page.goto("/#summary");
  await page.getByLabel("视频链接", { exact: true }).fill(partMetadata(3).url);
  await page.getByRole("button", { name: "解析链接", exact: true }).click();
  await expect(page.getByLabel("视频分 P")).toHaveValue("3");
  await page.getByLabel("视频分 P").selectOption("2");
  await expect.poll(() => secondRequested).toBe(true);
  await expect(page.getByRole("button", { name: "下载视频", exact: true })).toBeDisabled();
  await page.getByLabel("视频分 P").selectOption("1");
  await expect(page.getByRole("heading", { name: "P1 第1讲", exact: true })).toBeVisible();
  release();
  await expect(page.getByLabel("视频分 P")).toHaveValue("1");
  await expect(page.getByLabel("下载画质", { exact: true })).toHaveValue("360");
  const request = page.waitForRequest(request => request.method() === "POST" && new URL(request.url()).pathname === "/api/v1/video-downloads");
  await page.getByRole("button", { name: "下载视频", exact: true }).click();
  expect((await request).postDataJSON().url).toBe(partMetadata(1).url);
  expect(await page.evaluate(() => (window as any).downloadNotifications)).toEqual([]);
});

test("分 P 读取失败禁止沿用旧画质，重新解析后可恢复", async ({ page }) => {
  await mockDownloads(page);
  let failSecond = true;
  await page.route("**/api/v1/video-downloads/parse", route => {
    const selected = Number(new URL(route.request().postDataJSON().url).searchParams.get("p") || 1);
    if (selected === 2 && failSecond) return route.fulfill({ status: 400,
      json: { error: { message: "分 P 清单读取失败", error_id: "part-test" } } });
    return route.fulfill({ json: partMetadata(selected) });
  });
  await page.goto("/#summary");
  await page.getByLabel("视频链接", { exact: true }).fill(partMetadata(1).url);
  await page.getByRole("button", { name: "解析链接", exact: true }).click();
  await page.getByLabel("视频分 P").selectOption("2");
  await expect(page.getByRole("button", { name: "解析链接", exact: true })).toBeEnabled();
  await expect(page.getByRole("button", { name: "下载视频", exact: true })).toBeDisabled();
  await expect(page.getByLabel("下载画质", { exact: true })).toBeDisabled();
  failSecond = false;
  await page.getByRole("button", { name: "解析链接", exact: true }).click();
  await expect(page.getByLabel("视频分 P")).toHaveValue("2");
  await expect(page.getByLabel("下载画质", { exact: true })).toHaveValue("480");
  await expect(page.getByRole("button", { name: "下载视频", exact: true })).toBeEnabled();
});

test("单 P 视频不显示分 P 选择", async ({ page }) => {
  await mockDownloads(page);
  await page.route("**/api/v1/video-downloads/parse", route => route.fulfill({ json: partMetadata(1, 1) }));
  await page.goto("/#summary");
  await page.getByLabel("视频链接", { exact: true }).fill(partMetadata(1).url);
  await page.getByRole("button", { name: "解析链接", exact: true }).click();
  await expect(page.getByRole("heading", { name: "P1 第1讲", exact: true })).toBeVisible();
  await expect(page.getByLabel("视频分 P")).toHaveCount(0);
});

async function requestBilibiliPreview(page: Page, url = "https://b23.tv/test") {
  await page.goto("/#summary");
  await page.getByLabel("视频链接", { exact: true }).fill(url);
  await page.getByRole("button", { name: "解析链接", exact: true }).click();
}

async function openCredentials(page: Page) {
  await page.evaluate(() => { location.hash = "settings"; });
  await page.getByRole("tab", { name: "登录凭证管理", exact: true }).click();
  return page.getByRole("region", { name: "登录凭证管理", exact: true });
}

test("未登录检测 B 站时使用指定提示文案，暂不登录继续解析", async ({ page }) => {
  const state = await mockDownloads(page);
  state.login.state = "logged_out";
  await requestBilibiliPreview(page);
  const dialog = page.getByRole("dialog", { name: "B 站登录", exact: true });
  await expect(dialog.getByRole("heading", { name: "扫码登录B站，使用AI字幕无需本地语音转写", exact: true })).toBeVisible();
  if (test.info().project.name === "desktop-887") {
    await page.setViewportSize({ width: 598, height: 871 });
    expect(await dialog.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
    await dialog.screenshot({ path: test.info().outputPath("bilibili-login-598.png"), animations: "disabled" });
  }
  expect(state.posts).not.toContain("/api/v1/video-downloads/parse");
  await dialog.getByRole("button", { name: "暂不登录", exact: true }).click();
  await expect(page.getByRole("heading", { name: "链接下载测试视频", exact: true })).toBeVisible();
  expect(state.login.ids).toEqual([]);
  await page.getByRole("button", { name: "解析链接", exact: true }).click();
  await expect(dialog).toBeVisible();
});

test("扫码成功自动继续原链接解析，保存状态可退出并重新提示", async ({ page }) => {
  const state = await mockDownloads(page);
  state.login.state = "logged_out";
  state.login.poll = "success";
  await requestBilibiliPreview(page);
  const dialog = page.getByRole("dialog", { name: "B 站登录", exact: true });
  await dialog.getByRole("button", { name: "扫码登录", exact: true }).click();
  await expect(dialog.getByAltText("B 站登录二维码")).toBeVisible();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "链接下载测试视频", exact: true })).toBeVisible();
  await expect(page.locator(".bilibili-session")).toHaveCount(0);
  const credentials = await openCredentials(page);
  await expect(credentials.getByText("已登录", { exact: true })).toBeVisible();
  await credentials.getByRole("button", { name: "退出登录", exact: true }).click();
  await expect(credentials.getByText("未登录", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "关闭设置", exact: true }).click();
  await expect(page.locator(".bilibili-session")).toBeVisible();
  await page.getByRole("button", { name: "解析链接", exact: true }).click();
  await expect(dialog).toBeVisible();
});

test("二维码过期可以刷新，关闭弹窗取消会话且不自动解析", async ({ page }) => {
  const state = await mockDownloads(page);
  state.login.state = "expired";
  state.login.poll = "expired";
  await requestBilibiliPreview(page);
  const dialog = page.getByRole("dialog", { name: "B 站登录", exact: true });
  await dialog.getByRole("button", { name: "扫码登录", exact: true }).click();
  await expect(dialog.getByText("二维码已过期，请刷新", { exact: true })).toBeVisible();
  const first = state.login.ids.at(-1)!;
  await dialog.getByRole("button", { name: "刷新二维码", exact: true }).click();
  await expect.poll(() => state.login.ids.length).toBeGreaterThan(1);
  await expect.poll(() => state.login.cancelled.includes(first)).toBe(true);
  await dialog.getByRole("button", { name: "关闭 B 站登录", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect.poll(() => state.login.cancelled.includes(state.login.ids.at(-1)!)).toBe(true);
  expect(state.posts).not.toContain("/api/v1/video-downloads/parse");
});

test("慢登录轮询不重叠，关闭后迟到成功不更新或继续解析", async ({ page }) => {
  const state = await mockDownloads(page);
  state.login.state = "logged_out";
  await page.clock.install();
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  let polls = 0;
  await page.route("**/api/v1/bilibili/login/*/poll", async route => {
    polls++;
    await held;
    await route.fulfill({ json: { id: state.login.ids.at(-1), state: "success",
      session: { state: "logged_in", persistent: true, can_persist: true, warning: "" } } });
  });
  await requestBilibiliPreview(page);
  const dialog = page.getByRole("dialog", { name: "B 站登录", exact: true });
  await dialog.getByRole("button", { name: "扫码登录", exact: true }).click();
  await expect(dialog.getByAltText("B 站登录二维码")).toBeVisible();
  await page.clock.fastForward(1100);
  await expect.poll(() => polls).toBe(1);
  await page.clock.fastForward(10000);
  expect(polls).toBe(1);
  await dialog.getByRole("button", { name: "关闭 B 站登录", exact: true }).click();
  release();
  await page.clock.fastForward(10000);
  await expect(page.locator(".bilibili-session")).toBeVisible();
  expect(state.posts).not.toContain("/api/v1/video-downloads/parse");
  expect(await page.evaluate(() => (window as any).downloadNotifications)).toEqual([]);
});

test("非 B 站链接不查询或弹出 B 站登录", async ({ page }) => {
  const state = await mockDownloads(page);
  await requestBilibiliPreview(page, "https://www.youtube.com/watch?v=hh73MR7j_WM");
  await expect(page.getByRole("heading", { name: "链接下载测试视频", exact: true })).toBeVisible();
  expect(state.login.statusReads).toBe(0);
  await expect(page.getByRole("dialog", { name: "B 站登录", exact: true })).toHaveCount(0);
});

test("暂不登录后切换分 P 不重复询问登录", async ({ page }) => {
  const state = await mockDownloads(page);
  state.login.state = "logged_out";
  await page.route("**/api/v1/video-downloads/parse", route => {
    const selected = Number(new URL(route.request().postDataJSON().url).searchParams.get("p") || 1);
    return route.fulfill({ json: partMetadata(selected) });
  });
  await requestBilibiliPreview(page);
  await page.getByRole("button", { name: "暂不登录", exact: true }).click();
  await expect(page.getByLabel("视频分 P")).toBeVisible();
  const statusReads = state.login.statusReads;
  await page.getByLabel("视频分 P").selectOption("2");
  await expect(page.getByRole("heading", { name: "P2 第2讲", exact: true })).toBeVisible();
  await expect(page.getByRole("dialog", { name: "B 站登录", exact: true })).toHaveCount(0);
  expect(state.login.statusReads).toBe(statusReads);
});

test("扫码登录后刷新恢复后端登录状态，不重复扫码", async ({ page }) => {
  const state = await mockDownloads(page);
  state.login.state = "logged_out";
  state.login.poll = "success";
  await requestBilibiliPreview(page);
  await page.getByRole("dialog", { name: "B 站登录", exact: true }).getByRole("button", { name: "扫码登录", exact: true }).click();
  await expect(page.locator(".bilibili-session")).toHaveCount(0);
  await page.getByRole("button", { name: "下载视频", exact: true }).click();
  await expect(page.getByRole("button", { name: "下载中…", exact: true })).toBeVisible();
  const logins = state.login.ids.length;
  const reads = state.login.statusReads;
  await page.reload();
  await expect(page.locator(".bilibili-session")).toHaveCount(0);
  await expect.poll(() => state.login.statusReads).toBeGreaterThan(reads);
  expect(state.login.ids.length).toBe(logins);
  await expect(page.getByRole("dialog", { name: "B 站登录", exact: true })).toHaveCount(0);
});

test("粘贴 B 站链接自动读取已保存的登录，不要求先解析", async ({ page }) => {
  const state = await mockDownloads(page);
  await page.goto("/#summary");
  await page.getByLabel("视频链接", { exact: true }).fill("https://b23.tv/test");
  await expect(page.locator(".bilibili-session")).toHaveCount(0);
  await expect.poll(() => state.login.statusReads).toBeGreaterThan(0);
  expect(state.posts).toEqual([]);
  await expect(page.getByRole("dialog", { name: "B 站登录", exact: true })).toHaveCount(0);
});

test("自动查询的迟到未登录响应不覆盖扫码成功", async ({ page }) => {
  const state = await mockDownloads(page);
  state.login.state = "logged_out";
  state.login.poll = "success";
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  let reads = 0;
  let released = false;
  await page.route("**/api/v1/bilibili/session", async route => {
    if (++reads !== 1) { await route.fallback(); return; }
    await held;
    await route.fulfill({ json: { state: "logged_out", persistent: false, can_persist: true, warning: "" } });
    released = true;
  });
  await page.goto("/#summary");
  await page.getByLabel("视频链接", { exact: true }).fill("https://b23.tv/test");
  await expect.poll(() => reads).toBe(1);
  const cancelled = page.waitForEvent("requestfailed", request => new URL(request.url()).pathname === "/api/v1/bilibili/session");
  await page.getByRole("button", { name: "解析链接", exact: true }).click();
  await cancelled;
  await page.getByRole("dialog", { name: "B 站登录", exact: true }).getByRole("button", { name: "扫码登录", exact: true }).click();
  await expect(page.locator(".bilibili-session")).toHaveCount(0);
  release();
  await expect.poll(() => released).toBe(true);
  await expect(page.locator(".bilibili-session")).toHaveCount(0);
  expect(await page.evaluate(() => (window as any).downloadNotifications)).toEqual([]);
});

for (const status of ["logged_out", "expired", "logged_in", "unknown"]) {
  test(`${status} 仅确认未登录的 B 站链接显示字幕提示条`, async ({ page }) => {
    const state = await mockDownloads(page);
    state.login.state = status;
    await page.goto("/#summary");
    await expect(page.locator(".bilibili-session")).toHaveCount(0);
    const response = page.waitForResponse(response => new URL(response.url()).pathname === "/api/v1/bilibili/session");
    await page.getByLabel("视频链接", { exact: true }).fill("https://b23.tv/test");
    await response;
    const banner = page.locator(".bilibili-session");
    if (["logged_out", "expired"].includes(status)) {
      await expect(banner).toContainText("扫码登录B站，使用AI字幕无需本地语音转写");
      await expect(banner.getByRole("button", { name: "扫码登录", exact: true })).toBeVisible();
      await expect(banner.getByRole("button", { name: "退出登录", exact: true })).toHaveCount(0);
      if (test.info().project.name === "desktop-887" && status === "logged_out") {
        await page.setViewportSize({ width: 598, height: 871 });
        for (const theme of ["light", "dark"]) {
          await page.evaluate(theme => { document.documentElement.dataset.theme = theme; }, theme);
          expect(await banner.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
          await banner.screenshot({ path: test.info().outputPath("subtitle-notice-" + theme + ".png"), animations: "disabled" });
        }
      }
    } else await expect(banner).toHaveCount(0);
    await expect(page.getByRole("dialog", { name: "B 站登录", exact: true })).toHaveCount(0);
    await page.getByLabel("视频链接", { exact: true }).fill("https://www.youtube.com/watch?v=hh73MR7j_WM");
    await expect(banner).toHaveCount(0);
  });
}

test("凭证管理显示保存方式，退出失败保留状态，成功后刷新保持未登录", async ({ page }) => {
  await mockDownloads(page);
  let fail = true;
  await page.route("**/api/v1/bilibili/session", async route => {
    if (route.request().method() !== "DELETE" || !fail) return route.fallback();
    await route.fulfill({ status: 500, json: { error: { message: "退出失败，请重试", error_id: "logout-test" } } });
  });
  await page.goto("/#summary");
  const credentials = await openCredentials(page);
  await expect(credentials.getByText("登录凭证已加密保存到本机", { exact: true })).toBeVisible();
  if (test.info().project.name === "desktop-887") {
    await page.setViewportSize({ width: 598, height: 871 });
    const dialog = page.getByRole("dialog", { name: "设置", exact: true });
    expect(await dialog.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
    await dialog.screenshot({ path: test.info().outputPath("credential-settings-598.png"), animations: "disabled" });
  }
  await credentials.getByRole("button", { name: "退出登录", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("退出失败，请重试");
  await expect(credentials.getByText("已登录", { exact: true })).toBeVisible();
  fail = false;
  await credentials.getByRole("button", { name: "退出登录", exact: true }).click();
  await expect(credentials.getByText("未登录", { exact: true })).toBeVisible();
  await expect(credentials.getByRole("button", { name: "退出登录", exact: true })).toHaveCount(0);
  await page.reload();
  await page.getByRole("tab", { name: "登录凭证管理", exact: true }).click();
  await expect(credentials.getByText("未登录", { exact: true })).toBeVisible();
});

test("关闭凭证管理会取消查询，迟到状态不覆盖重新打开的页面", async ({ page }) => {
  const state = await mockDownloads(page);
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  let reads = 0, released = false;
  await page.route("**/api/v1/bilibili/session", async route => {
    if (++reads !== 1) return route.fallback();
    await held;
    await route.fulfill({ json: { state: "logged_in", persistent: true, can_persist: true, warning: "" } });
    released = true;
  });
  await page.goto("/#summary");
  const credentials = await openCredentials(page);
  await expect.poll(() => reads).toBe(1);
  const cancelled = page.waitForEvent("requestfailed", request => new URL(request.url()).pathname === "/api/v1/bilibili/session");
  await page.getByRole("button", { name: "关闭设置", exact: true }).click();
  await cancelled;
  state.login.state = "logged_out";
  await openCredentials(page);
  await expect(credentials.getByText("未登录", { exact: true })).toBeVisible();
  release();
  await expect.poll(() => released).toBe(true);
  await expect(credentials.getByText("未登录", { exact: true })).toBeVisible();
  expect(await page.evaluate(() => (window as any).downloadNotifications)).toEqual([]);
});

test("设置退出会更新链接提示，旧自动登录查询不能恢复已退出状态", async ({ page }) => {
  await mockDownloads(page);
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  let reads = 0, released = false;
  await page.route("**/api/v1/bilibili/session", async route => {
    if (++reads !== 1) return route.fallback();
    await held;
    await route.fulfill({ json: { state: "logged_in", persistent: true, can_persist: true, warning: "" } });
    released = true;
  });
  await page.goto("/#summary");
  await page.getByLabel("视频链接", { exact: true }).fill("https://b23.tv/test");
  await expect.poll(() => reads).toBe(1);
  const credentials = await openCredentials(page);
  await expect(credentials.getByText("已登录", { exact: true })).toBeVisible();
  const cancelled = page.waitForEvent("requestfailed", request => new URL(request.url()).pathname === "/api/v1/bilibili/session");
  await credentials.getByRole("button", { name: "退出登录", exact: true }).click();
  await cancelled;
  await expect(credentials.getByText("未登录", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "关闭设置", exact: true }).click();
  release();
  await expect.poll(() => released).toBe(true);
  await expect(page.locator(".bilibili-session")).toBeVisible();
  await expect(page.getByRole("button", { name: "退出登录", exact: true })).toHaveCount(0);
});

test("下载任务回执交接详情，列表及首次任务读取失败不丢失已接受任务", async ({ page }) => {
  const state = await mockDownloads(page);
  state.ext = "mp4";
  await page.clock.install();
  await preview(page);
  await page.getByRole("button", { name: "下载视频", exact: true }).click();
  state.done = true;
  await page.clock.runFor(2100);
  await page.getByRole("button", { name: "开始生成笔记", exact: true }).click();
  let reads = 0, failList = true;
  await page.route("**/api/v1/videos", route => failList ? route.fulfill({ status: 503, json: { error: { message: "列表暂不可用", error_id: "list" } } }) : route.fallback());
  await page.route("**/api/v1/tasks/imported-task", route => ++reads === 1
    ? route.fulfill({ status: 503, json: { error: { message: "任务暂不可用", error_id: "task" } } }) : route.fallback());
  await page.getByRole("dialog", { name: "选择处理模型", exact: true }).getByRole("button", { name: "开始处理", exact: true }).click();
  await expect(page).toHaveURL(/#summary\/imported-video$/);
  await expect(page.getByText("操作已接受，正在同步状态", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "链接下载测试视频-360P.mp4", exact: true })).toBeVisible();
  failList = false;
  await page.clock.runFor(5100);
  await expect(page.getByRole("button", { name: "停止处理", exact: true })).toBeEnabled();
  expect(state.selections).toHaveLength(1);
  expect(state.posts.filter(path => path.endsWith("/import"))).toHaveLength(1);
});
