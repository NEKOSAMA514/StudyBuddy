import { test, expect, type APIRequestContext, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";

const samplePath = process.env.E2E_VIDEO_PATH!;
if (!samplePath) throw new Error("请使用隔离浏览器服务并设置 E2E_VIDEO_PATH。");
const terminal = new Set(["succeeded", "failed", "paused", "interrupted"]);
function payload(name: string) {
  return { name, mimeType: "video/mp4", buffer: Buffer.concat([readFileSync(samplePath), Buffer.from(name)]) };
}
async function save(request: APIRequestContext, name: string) {
  const file = payload(name);
  const response = await request.post("/api/v1/videos", { multipart: { file: { name, mimeType: file.mimeType, buffer: file.buffer } } });
  expect(response.status()).toBe(201);
  return response.json();
}
async function finish(request: APIRequestContext, id: string) {
  await expect.poll(async () => (await (await request.get(`/api/v1/tasks/${id}`)).json()).status, { timeout: 45000 }).toBe("succeeded");
}
async function drop(page: Page, files: { name: string; bytes: number[] }[]) {
  const transfer = await page.evaluateHandle(items => {
    const data = new DataTransfer();
    for (const item of items) data.items.add(new File([new Uint8Array(item.bytes)], item.name, { type: "video/mp4" }));
    return data;
  }, files);
  const zone = page.getByRole("region", { name: "文件上传", exact: true });
  await zone.dispatchEvent("dragenter", { dataTransfer: transfer });
  await expect(zone).toHaveClass(/dragging/);
  await zone.dispatchEvent("drop", { dataTransfer: transfer });
  await transfer.dispose();
}

test.beforeEach(async ({ request }) => {
  const identity = await request.get("/__test__/identity");
  expect(identity.ok(), "仅运行于隔离测试工厂").toBe(true);
  expect((await identity.json()).fake_services_used).toBe(true);
  for (const video of await (await request.get("/api/v1/videos")).json()) {
    if (video.latest_task && !terminal.has(video.latest_task.status)) {
      await request.post(`/api/v1/tasks/${video.latest_task.id}/stop`);
      await expect.poll(async () => terminal.has((await (await request.get(`/api/v1/tasks/${video.latest_task.id}`)).json()).status)).toBe(true);
    }
    expect((await request.delete(`/api/v1/videos/${video.id}`)).ok()).toBe(true);
  }
  expect((await request.put("/api/v1/settings/model", { data: { token: "fake-isolated-only", free_tier_confirmed: true } })).ok()).toBe(true);
});

test("主页及侧栏入口显示上传和文件列表，不恢复上次文件", async ({ page, request }) => {
  const video = await save(request, "历史文件.mp4");
  await page.addInitScript(id => localStorage.setItem("studybuddy-video", id), video.id);
  await page.goto("/#");
  await page.getByRole("button", { name: /开始总结？/ }).click();
  await expect(page).toHaveURL(/#summary$/);
  const list = page.getByRole("region", { name: "文件列表", exact: true });
  await expect(list.getByText(video.filename, { exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "文件上传", exact: true })).toBeVisible();
  await expect(page.locator(".player-panel")).toHaveCount(0);
  await list.getByRole("button", { name: video.filename, exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`#summary/${video.id}$`));
  await page.getByRole("button", { name: "开始总结", exact: true }).click();
  await expect(list).toBeVisible();
  await expect(page.locator(".player-panel")).toHaveCount(0);
  await page.screenshot({ path: test.info().outputPath("file-library.png"), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test("键盘选择上传只保存文件，手动确认模型才创建任务", async ({ page, request }) => {
  const analyses: string[] = [];
  page.on("request", req => { if (req.method() === "POST" && /\/analyses$/.test(req.url())) analyses.push(req.url()); });
  await page.goto("/#summary");
  expect((await (await request.get("/api/v1/health")).json()).max_upload_bytes).toBe(2 * 1024 ** 3);
  await expect(page.getByRole("region", { name: "文件上传", exact: true })).toContainText("最大 2 GB");
  const chooserPromise = page.waitForEvent("filechooser");
  await page.getByRole("button", { name: /点击或拖拽文件到这里/ }).focus();
  await page.keyboard.press("Enter");
  await (await chooserPromise).setFiles(payload("手动处理.mp4"));
  await expect(page).toHaveURL(/#summary\/[^/]+$/);
  await expect(page.getByLabel("任务动态记录", { exact: true }).getByText("视频已保存，等待开始处理", { exact: true })).toBeVisible();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(analyses).toHaveLength(0);
  const video = (await (await request.get("/api/v1/videos")).json())[0];
  expect(video.latest_task).toBeNull();
  await page.getByRole("button", { name: "开始处理", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "选择处理模型" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "取消", exact: true }).click();
  await page.reload();
  await expect(page.getByLabel("任务动态记录", { exact: true }).getByText("视频已保存，等待开始处理", { exact: true })).toBeVisible();
  expect(analyses).toHaveLength(0);
  await page.locator(".detail-back").click();
  await expect(page.getByRole("region", { name: "文件列表", exact: true }).getByText("未处理", { exact: true })).toBeVisible();
  await page.getByRole("region", { name: "文件列表", exact: true }).getByRole("button", { name: video.filename, exact: true }).click();
  await page.getByRole("button", { name: "开始处理", exact: true }).click();
  await dialog.getByRole("button", { name: "开始处理", exact: true }).click();
  await expect(page.getByText("这是自动化测试返回的总结：视频介绍 TCP 慢启动。", { exact: true })).toBeVisible({ timeout: 45000 });
  expect(analyses).toHaveLength(1);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: test.info().outputPath("file-detail.png"), fullPage: true });
  await page.locator(".detail-back").click();
  await expect(page.getByRole("region", { name: "文件列表", exact: true }).getByText("已完成", { exact: true })).toBeVisible();
});

test("拖拽上传进入待开始详情，刷新和前进后退不串文件", async ({ page, request }) => {
  const first = await save(request, "已有文件.mp4");
  await page.goto("/#summary");
  const file = payload("拖入文件.mp4");
  await drop(page, [{ name: file.name, bytes: [...file.buffer] }]);
  await expect(page).toHaveURL(/#summary\/[^/]+$/);
  const detailUrl = page.url();
  await expect(page.locator(".player-panel h2")).toHaveText(file.name);
  await page.reload();
  await expect(page.locator(".player-panel h2")).toHaveText(file.name);
  await page.locator(".detail-back").click();
  await page.getByRole("region", { name: "文件列表", exact: true }).getByRole("button", { name: first.filename, exact: true }).click();
  await expect(page.locator(".player-panel h2")).toHaveText(first.filename);
  await page.goBack();
  await expect(page).toHaveURL(/#summary$/);
  await page.goBack();
  await expect(page).toHaveURL(detailUrl);
  await expect(page.locator(".player-panel h2")).toHaveText(file.name);
  await page.goForward();
  await expect(page.getByRole("region", { name: "文件列表", exact: true })).toBeVisible();
});

test("多个文件和不支持格式普通提示，不上传或生成错误报告", async ({ page }) => {
  const writes: string[] = [];
  page.on("request", req => { if (req.method() === "POST") writes.push(req.url()); });
  await page.goto("/#summary");
  await drop(page, [{ name: "one.mp4", bytes: [1] }, { name: "two.mp4", bytes: [2] }]);
  await expect(page.getByRole("alert").filter({ hasText: "请一次选择一个文件。" })).toBeVisible();
  await drop(page, [{ name: "文档.zip", bytes: [1] }]);
  await expect(page.getByRole("alert").filter({ hasText: "请选择支持的视频或文档文件。" })).toBeVisible();
  expect(writes).toHaveLength(0);
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

test("上传等待响应时显示实际进度并阻止重复提交", async ({ page }) => {
  let release!: () => void;
  const waiting = new Promise<void>(resolve => { release = resolve; });
  const uploads: string[] = [];
  page.on("request", req => { if (req.method() === "POST" && /\/videos$/.test(req.url())) uploads.push(req.url()); });
  await page.route("**/api/v1/videos", async route => {
    if (route.request().method() === "POST") await waiting;
    await route.continue();
  });
  await page.goto("/#summary");
  await page.getByLabel("选择本地文件", { exact: true }).setInputFiles(payload("上传进度.mp4"));
  const progress = page.getByRole("progressbar", { name: "文件上传进度" });
  await expect(progress).toBeVisible();
  await expect.poll(() => uploads.length).toBe(1);
  await expect(page.locator(".file-upload-button")).toBeDisabled();
  await expect(page.getByLabel("选择本地文件", { exact: true })).toBeDisabled();
  const data = await page.evaluateHandle(() => {
    const transfer = new DataTransfer();
    transfer.items.add(new File(["ignored"], "second.mp4", { type: "video/mp4" }));
    return transfer;
  });
  await page.getByRole("region", { name: "文件上传", exact: true }).dispatchEvent("drop", { dataTransfer: data });
  expect(uploads).toHaveLength(1);
  release();
  await expect(page).toHaveURL(/#summary\/[^/]+$/);
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await data.dispose();
});

test("表格重命名和置顶保存后同步侧栏及详情", async ({ page, request }) => {
  const first = await save(request, "较早文件.mp4");
  await save(request, "最新文件.mp4");
  await page.goto("/#summary");
  const list = page.getByRole("region", { name: "文件列表", exact: true });
  await list.getByRole("button", { name: `管理文件：${first.filename}`, exact: true }).click();
  await page.getByRole("button", { name: "重命名", exact: true }).click();
  const rename = page.getByRole("dialog", { name: "重命名文件" });
  await rename.getByLabel("文件名称").fill("改名后的文件");
  await rename.getByRole("button", { name: "保存名称", exact: true }).click();
  await expect(rename).toHaveCount(0);
  await expect(page.locator(".sidebar").getByRole("button", { name: /改名后的文件/ }).first()).toBeVisible();
  await list.getByRole("button", { name: "管理文件：改名后的文件", exact: true }).click();
  await page.getByRole("button", { name: "置顶", exact: true }).click();
  await expect(list.locator("tbody tr").first()).toContainText("改名后的文件");
  await expect(list.locator("tbody tr").first()).toContainText("置顶");
  await page.reload();
  await expect(list.locator("tbody tr").first()).toContainText("改名后的文件");
  await list.getByRole("button", { name: "置顶改名后的文件", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`#summary/${first.id}$`));
  await expect(page.locator(".player-panel h2")).toHaveText("改名后的文件");
});

test("上传失败留在入口，可重选同一文件重试", async ({ page }) => {
  let failed = false;
  await page.route("**/api/v1/videos", route => {
    if (route.request().method() === "POST" && !failed) {
      failed = true;
      return route.fulfill({ status: 400, json: { error: { code: "invalid_media", message: "测试上传失败", error_id: "upload-fixture-error" } } });
    }
    return route.continue();
  });
  await page.goto("/#summary");
  const input = page.getByLabel("选择本地文件", { exact: true });
  await expect(input).toHaveAttribute("accept", /\.mp4(?:,|$)/);
  await input.setInputFiles(payload("重试文件.mp4"));
  await expect(page.getByRole("alert").filter({ hasText: "测试上传失败" })).toBeVisible();
  await expect(page).toHaveURL(/#summary$/);
  await expect(page.getByRole("button", { name: /点击或拖拽文件到这里/ })).toBeEnabled();
  await page.getByLabel("选择本地文件", { exact: true }).setInputFiles(payload("重试文件.mp4"));
  await expect(page).toHaveURL(/#summary\/[^/]+$/);
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

test("重复上传打开已有笔记，不自动再分析", async ({ page, request }) => {
  const video = await save(request, "重复文件.mp4");
  const task = await (await request.post(`/api/v1/videos/${video.id}/analyses`)).json();
  await finish(request, task.task_id);
  const analyses: string[] = [];
  page.on("request", req => { if (req.method() === "POST" && /\/analyses$/.test(req.url())) analyses.push(req.url()); });
  await page.goto("/#summary");
  await page.getByLabel("选择本地文件", { exact: true }).setInputFiles(payload(video.filename));
  await expect(page).toHaveURL(new RegExp(`#summary/${video.id}$`));
  await expect(page.getByText("这是自动化测试返回的总结：视频介绍 TCP 慢启动。", { exact: true })).toBeVisible();
  expect(analyses).toHaveLength(0);
  expect(await (await request.get("/api/v1/videos")).json()).toHaveLength(1);
});

test("关闭设置回到原详情或列表，删除当前文件不跳到另一份", async ({ page, request }) => {
  const first = await save(request, "保留文件.mp4");
  const second = await save(request, "删除文件.mp4");
  await page.goto(`/#summary/${second.id}`);
  await page.getByRole("link", { name: "⚙ 设置", exact: true }).click();
  await page.getByRole("button", { name: "关闭设置", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`#summary/${second.id}$`));
  await expect(page.locator(".player-panel h2")).toHaveText(second.filename);
  await page.getByRole("button", { name: "开始处理", exact: true }).click();
  await page.getByRole("dialog", { name: "选择处理模型" }).getByRole("button", { name: "模型设置", exact: true }).click();
  await page.getByRole("button", { name: "关闭设置", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`#summary/${second.id}$`));
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await page.locator(".sidebar").getByRole("button", { name: `管理文件：${second.filename}`, exact: true }).click();
  await page.getByRole("button", { name: "删除", exact: true }).click();
  await page.getByRole("dialog").getByRole("button", { name: /确认删除/ }).click();
  await expect(page).toHaveURL(/#summary$/);
  const list = page.getByRole("region", { name: "文件列表", exact: true });
  await expect(list.getByRole("button", { name: first.filename, exact: true })).toBeVisible();
  await expect(list.getByText(second.filename, { exact: true })).toHaveCount(0);
  await page.getByRole("link", { name: "⚙ 设置", exact: true }).click();
  await page.getByRole("button", { name: "关闭设置", exact: true }).click();
  await expect(page).toHaveURL(/#summary$/);
});

test("返回列表保持后台处理，并自动刷新完成状态", async ({ page, request }) => {
  const video = await save(request, "后台处理.mp4");
  const stops: string[] = [];
  page.on("request", req => { if (req.method() === "POST" && /\/stop$/.test(req.url())) stops.push(req.url()); });
  await page.goto(`/#summary/${video.id}`);
  await page.getByRole("button", { name: "开始处理", exact: true }).click();
  await page.getByRole("dialog", { name: "选择处理模型" }).getByRole("button", { name: "开始处理", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await page.locator(".detail-back").click();
  const row = page.getByRole("region", { name: "文件列表", exact: true }).getByRole("row").filter({ hasText: video.filename });
  await expect(row.getByText("已完成", { exact: true })).toBeVisible({ timeout: 45000 });
  expect(stops).toHaveLength(0);
});

test("文件列表展示超过 100 条及各种状态，行菜单不进入详情", async ({ page }) => {
  const states = [null, "queued", "running", "succeeded", "failed", "paused", "interrupted", "stopping"];
  const files = Array.from({ length: 105 }, (_, index) => ({ id: `item-${index}`, filename: `资料-${index}.mp4`, size_bytes: 2000,
    created_at: "2026-10-11T00:00:00+00:00", latest_task: states[index % states.length] ? { status: states[index % states.length] } : null }));
  await page.route("**/api/v1/videos", route => route.request().method() === "GET" ? route.fulfill({ json: files }) : route.continue());
  await page.goto("/#summary");
  const list = page.getByRole("region", { name: "文件列表", exact: true });
  await expect(list.getByRole("row")).toHaveCount(106);
  for (const status of ["未处理", "排队中", "处理中", "已完成", "处理失败", "已停止", "已中断", "正在停止"])
    await expect(list.getByText(status, { exact: true }).first()).toBeVisible();
  await list.getByRole("button", { name: "管理文件：资料-0.mp4", exact: true }).click();
  await expect(page.getByRole("button", { name: "重命名", exact: true })).toBeVisible();
  await expect(page).toHaveURL(/#summary$/);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test("不存在的详情地址提供返回入口，不自动选择其他文件", async ({ page, request }) => {
  await save(request, "真实文件.mp4");
  await page.goto("/#summary/missing-file");
  await expect(page.getByText("文件不存在或已被删除。", { exact: true })).toBeVisible();
  await expect(page.locator(".player-panel")).toHaveCount(0);
  await page.locator(".notice").getByRole("button", { name: "返回", exact: true }).click();
  await expect(page.getByRole("region", { name: "文件列表", exact: true })).toBeVisible();
});
