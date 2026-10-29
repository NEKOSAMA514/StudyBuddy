import { settingsRead } from "./support/settings";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { test, expect, type Page, type Route } from "@playwright/test";
import { modelBudgets } from "./support/modelContract";

function gate() {
  let release!: () => void;
  const promise = new Promise<void>(resolve => { release = resolve; });
  return { promise, release };
}

async function settingsAPI(page: Page) {
  const profile = { ...modelBudgets, id: "default", name: "测试连接", base_url: "https://test.invalid/v1", model: "test-model",
    has_token: true, stream: true, json_mode: false, section_concurrency: 3, request_interval_seconds: 3 };
  const catalog = { settings: { asr_option: "paraformer", ocr_option: "rapidocr", language: "zh" },
    active_download: null as string | null,
    models: ["paraformer", "rapidocr"].map(id => ({ id, kind: id === "paraformer" ? "asr" : "ocr",
      name: id, description: "测试模型", directory: id, size: "1 MB", docs: "https://test.invalid",
      ready: true, runtime_ready: true, download: null as null | { status: string; message: string; downloaded_bytes: number; total_bytes: number } })) };
  const state = { healthReads: 0, recognitionReads: 0, writes: [] as string[], profile, catalog };
  await page.route("**/api/v1/**", async route => {
    const path = new URL(route.request().url()).pathname;
    const method = route.request().method();
    if (method !== "GET") state.writes.push(path);
    let body: unknown;
    if (path.endsWith("/health")) { state.healthReads++; body = { model_configured: true, supported_video_extensions: ["mp4"] }; }
    else if (path.endsWith("/videos") || path.endsWith("/documents")) body = [];
    else if (path.endsWith("/settings/model/profiles")) body = { active_id: "default", profiles: [profile] };
    else if (path.endsWith("/settings/recognition")) { state.recognitionReads++; body = catalog; }
    else if (method === "PUT" && ["/api/v1/settings/network", "/api/v1/settings/note-style"].includes(path)) body = route.request().postDataJSON();
    else if (await settingsRead(route)) return;
    else throw new Error(`Unexpected request: ${method} ${path}`);
    await route.fulfill({ json: body });
  });
  return state;
}

async function fulfillLate(route: Route, value: object, status = 200) {
  // 请求应已被页面取消，Playwright 仍允许释放测试端的等待。
  await route.fulfill({ status, json: value }).catch(() => {});
}

for (const [category, path, field, draft] of [
  ["网络代理", "network", "代理地址", "http://127.0.0.1:9999"],
  ["笔记风格", "note-style", "笔记补充要求（选填）", "比较概念，再给例子"],
] as const) {
  test(`${category}首次读取失败可独立重试`, async ({ page }) => {
    await settingsAPI(page);
    let fail = true;
    await page.route(`**/settings/${path}`, route => fail ? route.fulfill({ status: 503,
      json: { error: { message: "暂时无法读取设置", error_id: "settings", log_saved: true } } }) : route.fallback());
    await page.goto("/#settings");
    await page.getByRole("tab", { name: category, exact: true }).click();
    await expect(page.getByRole("button", { name: "重新读取设置", exact: true })).toBeVisible();
    fail = false;
    await page.getByRole("button", { name: "重新读取设置", exact: true }).click();
    await expect(page.getByLabel(field, { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "重新读取设置", exact: true })).toHaveCount(0);
  });

  test(`${category}保存失败保留草稿，连续提交只写一次`, async ({ page }) => {
    await settingsAPI(page);
    const held = gate();
    const bodies: any[] = [];
    await page.route(`**/settings/${path}`, async route => {
      if (route.request().method() !== "PUT") return route.fallback();
      bodies.push(route.request().postDataJSON());
      await held.promise;
      await route.fulfill(bodies.length === 1 ? { status: 500, json: { error: { message: "保存失败请重试", error_id: "save", log_saved: true } } }
        : { json: route.request().postDataJSON() });
    });
    await page.goto("/#settings");
    await page.getByRole("tab", { name: category, exact: true }).click();
    await page.getByLabel(field, { exact: true }).fill(draft);
    await page.locator("form.settings-card").evaluate((form: HTMLFormElement) => { form.requestSubmit(); form.requestSubmit(); });
    try { await expect.poll(() => bodies.length).toBe(1); }
    finally { held.release(); }
    await expect(page.locator(".error-notification")).toContainText("保存失败请重试");
    await expect(page.getByLabel(field, { exact: true })).toHaveValue(draft);
    await page.getByRole("button", { name: category === "网络代理" ? "保存代理设置" : "保存笔记风格", exact: true }).click();
    await expect(page.locator(".error-notification.success")).toBeVisible();
    expect(bodies).toHaveLength(2);
    expect(bodies[1]).toEqual(bodies[0]);
  });
}

test("调用方接管的设置错误上报等待期间关闭，不显示迟到通知", async ({ page }) => {
  await settingsAPI(page);
  const held = gate();
  let reporting = false, completed = false;
  await page.route("**/settings/network", route => route.request().method() === "PUT"
    ? route.fulfill({ status: 502, contentType: "text/html", body: "invalid response" }) : route.fallback());
  await page.route("**/diagnostics/client-errors", async route => {
    reporting = true;
    await held.promise;
    await fulfillLate(route, { message: "设置保存失败", error_id: "late", log_saved: true });
    completed = true;
  });
  try {
    await page.goto("/#settings");
    await page.getByRole("tab", { name: "网络代理", exact: true }).click();
    await page.getByRole("button", { name: "保存代理设置", exact: true }).click();
    await expect.poll(() => reporting).toBe(true);
    await page.getByRole("button", { name: "关闭设置", exact: true }).click();
  } finally { held.release(); }
  await expect.poll(() => completed).toBe(true);
  await expect(page.locator(".error-notification")).toHaveCount(0);
});

test.beforeAll(async ({ request }) => {
  expect((await (await request.get("/__test__/identity")).json()).fake_services_used).toBe(true);
});

for (const [category, path, button, message] of [
  ["API设置", "model/profiles/default", "保存配置", "API 配置保存成功。"],
  ["语音识别模型", "recognition", "保存识别设置", "语音识别设置保存成功。"],
  ["OCR 模型", "recognition", "保存识别设置", "OCR 设置保存成功。"],
  ["网络代理", "network", "保存代理设置", "代理设置保存成功。"],
  ["笔记风格", "note-style", "保存笔记风格", "笔记风格保存成功。"],
] as const) {
  test(`${category}仅在保存成功后弹出可关闭的顶部通知`, async ({ page }) => {
    const state = await settingsAPI(page);
    const held = gate();
    let saving = false;
    await page.route(`**/settings/${path}`, async route => {
      if (route.request().method() !== "PUT") return route.fallback();
      saving = true;
      await held.promise;
      await route.fulfill({ json: category === "API设置"
        ? { active_id: "default", profiles: [state.profile] }
        : category === "语音识别模型" || category === "OCR 模型" ? state.catalog : route.request().postDataJSON() });
    });
    await page.goto("/#settings");
    await page.getByRole("tab", { name: category, exact: true }).click();
    await page.getByRole("button", { name: button, exact: true }).click();
    await expect.poll(() => saving).toBe(true);
    await expect(page.locator(".error-notification.success")).toHaveCount(0);
    held.release();
    await expect(page.locator(".error-notification.success")).toBeVisible({ timeout: 2000 });
    await expect(page.locator(".error-notification.success")).toContainText(message);
    expect((await page.locator(".error-notification.success").boundingBox())!.y).toBe(16);
    if (category === "API设置") await page.screenshot({ path: test.info().outputPath("settings-save-success.png"), animations: "disabled" });
    await expect(page.getByRole("dialog", { name: "设置", exact: true })).toBeVisible();
    await expect(page.getByRole("tab", { name: category, exact: true })).toHaveAttribute("aria-selected", "true");
    await page.getByRole("button", { name: "关闭成功通知" }).click();
    await expect(page.locator(".error-notification.success")).toHaveCount(0);
    if (category === "API设置") {
      await page.getByRole("button", { name: button, exact: true }).click();
      await expect(page.locator(".error-notification.success")).toBeVisible();
      await page.getByRole("button", { name: "关闭成功通知" }).click();
    }
  });
}

test("保存成功通知逐渐淡出，重复保存重新计时，消失后仍可再次显示", async ({ page }) => {
  await settingsAPI(page);
  await page.goto("/#settings");
  await page.getByRole("tab", { name: "网络代理", exact: true }).click();
  const save = page.getByRole("button", { name: "保存代理设置" });
  const notice = page.locator(".error-notification.success");
  await save.click();
  await expect(notice).toBeVisible();
  await expect(notice).toHaveClass(/fading-error/);
  await expect.poll(() => notice.evaluate(element => Number(getComputedStyle(element).opacity))).toBeLessThan(0.8);
  await save.click();
  await expect(notice).toHaveCSS("opacity", "1");
  await expect(notice).toHaveCount(0, { timeout: 5500 });
  await expect(page.getByRole("tab", { name: "网络代理", exact: true })).toHaveAttribute("aria-selected", "true");
  await save.click();
  await expect(notice).toBeVisible();
  await page.getByRole("button", { name: "关闭成功通知" }).click();
  await expect(notice).toHaveCount(0);
});

test("保存成功通知悬停暂停消失，移开后重新计时", async ({ page }) => {
  await settingsAPI(page);
  await page.goto("/#settings");
  await page.getByRole("tab", { name: "网络代理", exact: true }).click();
  await page.getByRole("button", { name: "保存代理设置" }).click();
  const notice = page.locator(".error-notification.success");
  await notice.hover();
  await page.waitForTimeout(5200);
  await expect(notice).toHaveCSS("opacity", "1");
  await page.mouse.move(0, 800);
  await expect.poll(() => notice.evaluate(element => Number(getComputedStyle(element).opacity))).toBeLessThan(0.9);
  await expect(notice).toHaveCount(0, { timeout: 2500 });
});

test("设置保存失败只显示错误通知", async ({ page }) => {
  await settingsAPI(page);
  await page.route("**/settings/network", async route => {
    if (route.request().method() !== "PUT") return route.fallback();
    await route.fulfill({ status: 500, json: { error: { message: "设置保存失败", error_id: "save-failed", log_saved: true } } });
  });
  await page.goto("/#settings");
  await page.getByRole("tab", { name: "网络代理", exact: true }).click();
  await page.getByRole("button", { name: "保存代理设置" }).click();
  await expect(page.locator(".error-notification")).toContainText("设置保存失败");
  await expect(page.locator(".error-notification.success")).toHaveCount(0);
});

test("笔记风格保存保留设置且成功通知不依赖健康接口返回", async ({ page }) => {
  await settingsAPI(page);
  const held = gate();
  let reads = 0;
  await page.route("**/health", async route => {
    if (++reads === 1) return route.fallback();
    await held.promise;
    await fulfillLate(route, { model_configured: true, supported_video_extensions: ["mp4"] });
  });
  try {
    await page.goto("/#settings");
    await page.getByRole("tab", { name: "笔记风格", exact: true }).click();
    await page.getByRole("button", { name: "保存笔记风格" }).click();
    await expect(page.getByRole("dialog", { name: "设置", exact: true })).toBeVisible();
    await expect(page.locator(".error-notification.success")).toBeVisible({ timeout: 2000 });
    await page.getByRole("button", { name: "关闭成功通知" }).click();
  } finally { held.release(); }
});

test("切换设置后旧加载失败不弹错，再次进入读取新配置", async ({ page }) => {
  const state = await settingsAPI(page);
  const held = gate();
  let reads = 0, completed = false;
  await page.route("**/settings/model/profiles", async route => {
    if (++reads > 1) return route.fallback();
    await held.promise;
    await fulfillLate(route, { error: { message: "旧请求失败" } }, 500);
    completed = true;
  });
  await page.goto("/#settings");
  await expect(page.getByText("正在读取 API 配置…")).toBeVisible();
  await page.getByRole("tab", { name: "网络代理", exact: true }).click();
  await expect(page.getByLabel("代理地址")).toBeVisible();
  state.profile.name = "新配置";
  await page.getByRole("tab", { name: "API设置", exact: true }).click();
  await expect(page.getByLabel("配置名称")).toHaveValue("新配置");
  held.release();
  await expect.poll(() => completed).toBe(true);
  await expect(page.getByLabel("配置名称")).toHaveValue("新配置");
  await expect(page.locator(".error-notification")).toHaveCount(0);
});

for (const action of ["test", "save"] as const) {
  test(`关闭 API 设置后迟到的${action}响应不通知、不触发保存回调`, async ({ page }) => {
    const state = await settingsAPI(page);
    const held = gate();
    let started = false, completed = false;
    const path = action === "test" ? "**/settings/model/profiles/test*" : "**/settings/model/profiles/default";
    await page.route(path, async route => {
      started = true;
      await held.promise;
      await fulfillLate(route, action === "test" ? { elapsed_seconds: 1 }
        : { active_id: "default", profiles: [state.profile] });
      completed = true;
    });
    await page.goto("/#settings");
    await expect(page.getByLabel("配置名称")).toHaveValue("测试连接");
    const reads = state.healthReads;
    await page.getByRole("button", { name: action === "test" ? "测试连接" : "保存配置", exact: true }).click();
    await expect.poll(() => started).toBe(true);
    await page.getByRole("button", { name: "关闭设置", exact: true }).click();
    await page.getByRole("link", { name: "⚙ 设置", exact: true }).click();
    await expect(page.getByLabel("配置名称")).toBeVisible();
    await page.getByLabel("配置名称").fill("新的草稿");
    held.release();
    await expect.poll(() => completed).toBe(true);
    await expect(page.getByLabel("配置名称")).toHaveValue("新的草稿");
    await expect(page.getByText(/连接成功|配置已保存/)).toHaveCount(0);
    expect(state.healthReads).toBe(reads);
  });
}

for (const [category, path, button] of [
  ["笔记风格", "note-style", "保存笔记风格"],
  ["网络代理", "network", "保存代理设置"],
  ["语音识别模型", "recognition", "保存识别设置"],
] as const) {
  test(`${category}关闭后迟到保存不关闭新设置或刷新健康配置`, async ({ page }) => {
    const state = await settingsAPI(page);
    const held = gate();
    let started = false, completed = false;
    await page.route(`**/settings/${path}`, async route => {
      if (route.request().method() !== "PUT") return route.fallback();
      started = true;
      await held.promise;
      await fulfillLate(route, route.request().postDataJSON());
      completed = true;
    });
    await page.goto("/#settings");
    await page.getByRole("tab", { name: category, exact: true }).click();
    await page.getByRole("button", { name: button, exact: true }).click();
    await expect.poll(() => started).toBe(true);
    const reads = state.healthReads;
    await page.getByRole("tab", { name: "API设置", exact: true }).click();
    await expect(page.getByLabel("配置名称")).toBeVisible();
    held.release();
    await expect.poll(() => completed).toBe(true);
    await expect(page.getByRole("dialog", { name: "设置", exact: true })).toBeVisible();
    await expect(page.getByLabel("配置名称")).toHaveValue("测试连接");
    expect(state.healthReads).toBe(reads);
    await expect(page.locator(".error-notification")).toHaveCount(0);
  });
}

test("慢识别轮询串行执行，关闭设置停止轮询但不取消后台下载", async ({ page }) => {
  const state = await settingsAPI(page);
  state.catalog.active_download = "paraformer";
  state.catalog.models[0].download = { status: "running", message: "正在下载测试模型", downloaded_bytes: 1, total_bytes: 10 };
  const held = gate();
  let reads = 0, completed = false;
  await page.route("**/settings/recognition", async route => {
    if (++reads !== 2) return route.fallback();
    await held.promise;
    await fulfillLate(route, state.catalog);
    completed = true;
  });
  await page.clock.install();
  await page.goto("/#settings");
  await page.getByRole("tab", { name: "语音识别模型", exact: true }).click();
  await expect(page.getByText("正在下载测试模型")).toBeVisible();
  await page.clock.runFor(2100);
  await expect.poll(() => reads).toBe(2);
  await page.clock.runFor(6000);
  expect(reads).toBe(2);
  held.release();
  await expect.poll(() => completed).toBe(true);
  await page.clock.runFor(2100);
  await expect.poll(() => reads).toBe(3);
  await page.getByRole("button", { name: "关闭设置", exact: true }).click();
  await page.clock.runFor(10000);
  expect(reads).toBe(3);
  expect(state.writes).toEqual([]);
});

test("无笔记页面不加载 Markdown 和公式渲染资源", async ({ page }) => {
  await settingsAPI(page);
  const resources: string[] = [];
  page.on("request", request => { resources.push(request.url()); });
  await page.goto("/#summary");
  await expect(page.getByRole("heading", { name: "总结分析", exact: true })).toBeVisible();
  const manifest = JSON.parse(readFileSync(resolve(process.env.STATIC_DIR!, ".vite/manifest.json"), "utf8"));
  const entry = Object.keys(manifest).find(key => key.endsWith("/notes/NoteMarkdown.tsx"))!;
  expect(entry).toBeTruthy();
  const heavy = new Set<string>();
  function collect(key: string) {
    const chunk = manifest[key];
    if (!chunk || chunk.isEntry || heavy.has(chunk.file)) return;
    heavy.add(chunk.file);
    for (const file of [...(chunk.css || []), ...(chunk.assets || [])]) heavy.add(file);
    for (const imported of chunk.imports || []) collect(imported);
  }
  collect(entry);
  const isHeavy = (url: string) => [...heavy].some(file => url.endsWith("/" + file)) ||
    /\/src\/notes\/NoteMarkdown\.tsx|katex|rehype-highlight|react-markdown/i.test(url);
  expect(resources.filter(isHeavy)).toEqual([]);
  await page.route("**/api/v1/note-marks?*", route => route.fulfill({ json: { total: 1, page: 1, page_size: 20, items: [{
    kind: "document", file_id: "lazy", filename: "按需.md", task_id: "saved", note_title: "笔记", version: 1,
    chapter_id: "C1", chapter_title: "章节", heading_index: 0, title: "已标记的章节", snippet: "公式和代码仅显示纯文本。",
  }] } }));
  await page.goto("/#notes?view=marks&page=1");
  await expect(page.locator(".note-search-hit h3")).toHaveText("已标记的章节");
  expect(resources.filter(isHeavy)).toEqual([]);
  await page.route("**/api/v1/documents", route => route.fulfill({ json: [{ id: "lazy", kind: "document", filename: "按需.md",
    format: "md", size_bytes: 100, created_at: "2026-10-20T00:00:00Z", pinned: false, unit_count: 1, character_count: 20 }] }));
  await page.route("**/api/v1/documents/lazy/analyses", route => route.fulfill({ json: [] }));
  await page.route("**/api/v1/documents/lazy/content*", route => route.fulfill({ json: { units: [{ id: "D1", label: "原文", format: "markdown",
    text: "## 原文标题\n\n```python\nprint(1)\n```", locator: {} }], total: 1, offset: 0 } }));
  await page.goto("/#summary");
  await page.reload();
  await page.getByRole("button", { name: "按需.md", exact: true }).first().click();
  await page.getByText("提取内容", { exact: true }).click();
  await expect(page.locator('.document-preview .note-markdown[data-density="source"] h2')).toHaveText("原文标题");
  expect(resources.some(isHeavy)).toBe(true);
  await expect(page.locator(".document-preview .note-code-block pre")).toHaveCSS("font-size", "14px");
  await expect(page.locator(".document-preview .note-code-toolbar")).toHaveCSS("font-size", "12px");
});
