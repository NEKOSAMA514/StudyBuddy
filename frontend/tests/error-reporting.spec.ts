import { taskResponse } from "./support/taskContract";
import { modelBudgets } from "./support/modelContract";
import { settingsRead } from "./support/settings";
import { test, expect, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";

// 请求工具的测试直接加载源码，仅在独立 Vite 服务下运行；不向正式后端发送请求。
test.skip(!process.env.E2E_SOURCE_TESTS, "需设置 E2E_SOURCE_TESTS 并使用隔离 Vite 服务。");

async function mockAPI(page: Page) {
  const reports: Record<string, unknown>[] = [];
  const oldError = { message: "历史下载失败", error_id: "old-error", log_file: "log/old-error.log" };
  const state = {
    active_download: null as string | null,
    fastFailure: false,
    probe: "json",
    modelMessage: "",
    files: [] as Record<string, unknown>[],
    models: ["paraformer", "whisper-base"].map((id) => ({
      id, kind: "asr", name: id, description: "测试模型", directory: "models/" + id,
      size: "1 MB", docs: "https://test.invalid", ready: id === "paraformer", runtime_ready: true,
      download: id === "paraformer" ? null : { status: "failed", message: oldError.message,
        downloaded_bytes: 0, total_bytes: null, error: oldError },
    })),
  };
  let failureCount = 0;
  const failDownload = () => {
    state.active_download = null;
    state.models[1].download = { status: "failed", message: "新的下载失败", downloaded_bytes: 0, total_bytes: null,
      error: { message: "新的下载失败", error_id: `new-error-${++failureCount}`, log_file: "log/new-error.log" } };
  };
  await page.addInitScript(() => {
    (window as any).errorNotifications = [];
    window.addEventListener("studybuddy-error", (event) => (window as any).errorNotifications.push((event as CustomEvent).detail.message));
  });
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    let body: unknown;
    if (path === "/api/v1/health") body = { supported_video_extensions: ["mp4", "webm"] };
    else if (path === "/api/v1/videos") body = state.files;
    else if (path === "/api/v1/documents") body = [];
    else if (path === "/api/v1/settings/mineru") body = { has_token: false };
    else if (route.request().method() === "GET" && /\/videos\/[^/]+\/analyses$/.test(path)) body = [];
    else if (/\/videos\/[^/]+\/playback$/.test(path)) body = { status: "unprepared", direct: true, mime_type: "video/mp4", original_url: path.replace(/\/playback$/, "/content"), url: null, error: null };
    else if (path === "/api/v1/settings/model/profiles") body = { active_id: "default", profiles: [{ ...modelBudgets, id: "default", name: "魔搭", model: "fake", has_token: false,
      base_url: "https://test.invalid", stream: true, json_mode: false, free_tier_confirmed: false, section_concurrency: 3, request_interval_seconds: 3 }] };
    else if (path === "/api/v1/settings/model") body = { model: "fake", has_token: false,
      base_url: "https://test.invalid", stream: true, json_mode: false, free_tier_confirmed: false, section_concurrency: 3, request_interval_seconds: 3, summary_prompt: "" };
    else if (path === "/api/v1/settings/recognition") body = {
      settings: { asr_option: "paraformer", ocr_option: "rapidocr", language: "zh" },
      models: state.models, active_download: state.active_download,
    };
    else if (path === "/api/v1/settings/models/whisper-base/download") {
      state.active_download = "whisper-base";
      state.models[1].download = null;
      if (state.fastFailure) failDownload();
      body = { accepted: true };
    } else if (path === "/api/v1/diagnostics/client-errors") {
      const report = route.request().postDataJSON();
      reports.push(report);
      body = { message: report.message, error_id: "client-error", log_file: "log/client-error.log", log_saved: true };
    } else if (path === "/api/v1/probe") {
      if (state.probe === "model-raw") {
        await route.fulfill({ status: 503, json: { error: {
          code: "model_unavailable", message: state.modelMessage, error_id: "model-error",
          log_file: "log/model-error.log", log_saved: true,
        } } }); return;
      }
      if (state.probe === "network") { await route.abort(); return; }
      if (state.probe === "format" || state.probe === "http") {
        await route.fulfill({ status: state.probe === "http" ? 502 : 200, contentType: "text/html", body: "bad response" }); return;
      }
      if (state.probe === "reported") {
        await route.fulfill({ status: 500, json: { error: {
          message: "已报告的错误", error_id: "server-error", log_saved: false,
        } } }); return;
      }
      if (state.probe === "http-json") {
        await route.fulfill({ status: 502, json: { error: { message: "unreported failure" } } }); return;
      }
      body = {};
    } else if (path.endsWith("/analysis-preview")) body = { character_count: null, estimated_segments: null, reason: "识别材料尚未就绪。" };
    else if (await settingsRead(route)) return;
    else throw new Error("Unmocked API: " + path);
    await route.fulfill({ status: 200, json: body });
  });
  await page.goto("/#settings");
  await expect(page.getByRole("heading", { name: "设置", exact: true })).toBeVisible();
  return { state, reports, failDownload };
}

async function probe(page: Page) {
  await page.evaluate(async () => {
    const diagnostics = await import("/src/api.ts");
    try { await diagnostics.requestJSON("/api/v1/probe"); }
    catch (error) { await diagnostics.reportClientError(error); }
  });
}

for (const errorPolicy of ["automatic", "caller", "silent"] as const) {
  test(`请求错误策略分别控制报告和通知：${errorPolicy}`, async ({ page }) => {
    const mock = await mockAPI(page);
    mock.state.probe = "http-json";
    await page.evaluate(async policy => {
      const { requestJSON } = await import("/src/api.ts");
      try { await requestJSON("/api/v1/probe", { errorPolicy: policy }); } catch { /* The caller owns presentation. */ }
    }, errorPolicy);
    expect(mock.reports).toHaveLength(errorPolicy === "silent" ? 0 : 1);
    expect(await page.evaluate(() => (window as any).errorNotifications)).toHaveLength(errorPolicy === "automatic" ? 1 : 0);
  });
}

test("信息通知仅展示业务明确提供的链接", async ({ page }) => {
  const mock = await mockAPI(page);
  await page.evaluate(async () => (await import("/src/notifications.ts")).notifyInfo("普通信息"));
  await expect(page.locator(".error-notification")).toContainText("普通信息");
  await expect(page.locator(".error-notification a")).toHaveCount(0);
  await page.evaluate(async () => (await import("/src/notifications.ts")).notifyInfo("业务信息", { href: "https://test.invalid/help", label: "查看说明" }));
  await expect(page.locator(".error-notification").getByRole("link", { name: "查看说明" })).toHaveAttribute("href", "https://test.invalid/help");
  expect(mock.reports).toHaveLength(0);
});

for (const failure of ["network", "format"] as const) {
  test(`上传与普通请求共用错误策略：${failure}`, async ({ page }) => {
    const mock = await mockAPI(page);
    await page.route("**/api/v1/videos", route => route.request().method() !== "POST" ? route.fallback()
      : failure === "network" ? route.abort() : route.fulfill({ contentType: "text/html", body: "broken upload response" }));
    await page.getByRole("button", { name: "关闭设置" }).click();
    await page.getByRole("button", { name: /开始总结？/ }).click();
    await page.getByLabel("选择本地文件", { exact: true }).setInputFiles({ name: "sample.mp4", mimeType: "video/mp4", buffer: Buffer.from("fake") });
    await expect(page.locator(".error-notification")).toBeVisible();
    expect(mock.reports).toHaveLength(1);
    expect(mock.reports[0]).toMatchObject({ request_url: "/api/v1/videos", request_method: "POST",
      code: failure === "network" ? "request_network_error" : "response_format_error" });
    expect(await page.evaluate(() => (window as any).errorNotifications)).toHaveLength(1);
  });
}

for (const message of [" SSL DNS timeout: provider rejected this request ",
  '<img src=x onerror="window.rawModelErrorExecuted=true"> insufficient balance']) {
  test(`模型接口原始报错作为安全文本展示：${message}`, async ({ page }) => {
    const { state, reports } = await mockAPI(page);
    state.probe = "model-raw";
    state.modelMessage = message;
    await probe(page);
    const notification = page.locator(".error-notification");
    await expect(notification).toBeVisible();
    expect(await notification.locator("p").textContent()).toBe(
      message + "\n详细日志已保存到项目文件夹中的 log/model-error.log（错误编号：model-error）。");
    await expect(notification.locator("img")).toHaveCount(0);
    expect(await page.evaluate(() => (window as any).rawModelErrorExecuted)).toBeUndefined();
    expect(reports).toEqual([]);
    expect(await page.evaluate(() => (window as any).errorNotifications.length)).toBe(1);
  });
}

test("选择历史失败模型、切换标签和重新打开设置均不弹出旧错误", async ({ page }) => {
  const { reports } = await mockAPI(page);
  await page.getByRole("tab", { name: "语音识别模型", exact: true }).click();
  await page.getByRole("combobox", { name: "语音识别模型", exact: true }).selectOption("whisper-base");
  await expect(page.locator(".model-download-state")).toContainText("历史下载失败");
  await page.getByRole("combobox", { name: "语音识别模型", exact: true }).selectOption("paraformer");
  await page.getByRole("combobox", { name: "语音识别模型", exact: true }).selectOption("whisper-base");
  await page.getByRole("button", { name: "关闭设置" }).click();
  await page.getByRole("link", { name: /设置/ }).first().click();
  await page.getByRole("tab", { name: "语音识别模型", exact: true }).click();
  await page.getByRole("combobox", { name: "语音识别模型", exact: true }).selectOption("whisper-base");
  await expect(page.locator(".model-download-state")).toContainText("历史下载失败");
  expect(await page.evaluate(() => (window as any).errorNotifications)).toEqual([]);
  expect(reports).toHaveLength(0);
});

for (const fastFailure of [false, true]) {
  test("新下载失败只提示一次，立即失败=" + fastFailure, async ({ page }) => {
    const mock = await mockAPI(page);
    mock.state.fastFailure = fastFailure;
    await page.getByRole("tab", { name: "语音识别模型", exact: true }).click();
    await page.getByRole("combobox", { name: "语音识别模型", exact: true }).selectOption("whisper-base");
    await page.getByRole("button", { name: "下载模型到 models", exact: true }).click();
    if (!fastFailure) mock.failDownload();
    await expect(page.locator(".error-notification")).toContainText("新的下载失败");
    await expect(page.locator(".model-download-state.task-error")).toHaveText(/新的下载失败/);
    await expect(page.locator(".model-download-state.task-error")).not.toContainText("历史下载失败");
    await page.getByRole("combobox", { name: "语音识别模型", exact: true }).selectOption("paraformer");
    await page.getByRole("combobox", { name: "语音识别模型", exact: true }).selectOption("whisper-base");
    expect(await page.evaluate(() => (window as any).errorNotifications)).toHaveLength(1);
    expect(mock.reports).toHaveLength(0);
  });
}

for (const [kind, code] of [["network", "request_network_error"], ["format", "response_format_error"], ["http", "http_error"], ["http-json", "http_error"]]) {
  test("请求错误正确分类且上层捕获不重复报告：" + kind, async ({ page }) => {
    const mock = await mockAPI(page);
    mock.state.probe = kind;
    await probe(page);
    expect(mock.reports).toHaveLength(1);
    expect(mock.reports[0]).toMatchObject({ code, request_url: "/api/v1/probe", request_method: "GET" });
    if (kind.startsWith("http")) expect(mock.reports[0].http_status).toBe(502);
    if (kind === "format" || kind === "http") expect(mock.reports[0].response_body).toBe("bad response");
    if (kind === "http-json") expect(mock.reports[0].response_body).toContain("unreported failure");
    expect(await page.evaluate(() => (window as any).errorNotifications)).toHaveLength(1);
  });
}

test("后端已报告但日志写入失败时仍不重复生成报告", async ({ page }) => {
  const mock = await mockAPI(page);
  mock.state.probe = "reported";
  await probe(page);
  await expect(page.locator(".error-notification")).toContainText("日志写入失败");
  expect(mock.reports).toHaveLength(0);
});

test("响应读取中断属于连接错误，保留原始异常", async ({ page }) => {
  const mock = await mockAPI(page);
  await page.evaluate(async () => {
    const diagnostics = await import("/src/api.ts");
    const originalFetch = window.fetch;
    window.fetch = (input, init) => input === "/api/v1/probe"
      ? Promise.resolve({ text: () => Promise.reject(new TypeError("response connection interrupted")) } as Response)
      : originalFetch(input, init);
    try { await diagnostics.requestJSON("/api/v1/probe"); }
    catch (error) { await diagnostics.reportClientError(error); }
    finally { window.fetch = originalFetch; }
  });
  expect(mock.reports).toHaveLength(1);
  expect(mock.reports[0].code).toBe("request_network_error");
  expect(mock.reports[0].stack).toContain("response connection interrupted");
});

test("主动取消请求不生成错误报告或弹窗", async ({ page }) => {
  const mock = await mockAPI(page);
  await page.evaluate(async () => {
    const diagnostics = await import("/src/api.ts");
    const controller = new AbortController();
    controller.abort();
    try { await diagnostics.requestJSON("/api/v1/probe", { signal: controller.signal }); }
    catch (error) { await diagnostics.reportClientError(error); }
  });
  expect(mock.reports).toHaveLength(0);
  expect(await page.evaluate(() => (window as any).errorNotifications)).toEqual([]);
});

test("错误报告等待期间取消请求，不再弹出迟到通知", async ({ page }) => {
  const mock = await mockAPI(page);
  mock.state.probe = "http-json";
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  let reporting = false;
  await page.route("**/diagnostics/client-errors", async route => {
    reporting = true;
    await held;
    await route.fulfill({ json: { message: "请求失败", error_id: "late-report", log_saved: true } });
  });
  await page.evaluate(() => {
    const controller = new AbortController();
    (window as any).cancelProbe = () => controller.abort();
    (window as any).pendingProbe = import("/src/api.ts").then(async diagnostics => {
      try { await diagnostics.requestJSON("/api/v1/probe", { signal: controller.signal }); }
      catch (error) { await diagnostics.reportClientError(error); }
    });
  });
  await expect.poll(() => reporting).toBe(true);
  await page.evaluate(() => (window as any).cancelProbe());
  release();
  await page.evaluate(() => (window as any).pendingProbe);
  expect(await page.evaluate(() => (window as any).errorNotifications)).toEqual([]);
  await expect(page.locator(".error-notification")).toHaveCount(0);
});

async function opacity(page: Page) {
  return page.locator(".error-notification").evaluate(element => Number(getComputedStyle(element).opacity));
}

test("错误五秒内渐隐，显示期间相同消息和错误编号再次发生会重新计时", async ({ page }) => {
  const mock = await mockAPI(page);
  mock.state.probe = "model-raw";
  mock.state.modelMessage = "重复接口错误";
  await probe(page);
  await expect.poll(() => opacity(page)).toBeLessThan(0.75);
  await probe(page);
  expect(await opacity(page)).toBeGreaterThan(0.9);
  await expect(page.locator(".error-notification")).toHaveCount(0, { timeout: 5500 });
  expect(await page.evaluate(() => (window as any).errorNotifications.length)).toBe(2);
  expect(mock.reports).toHaveLength(0);
});

test("自动消失及手动关闭后，相同错误仍可再次弹出", async ({ page }) => {
  const mock = await mockAPI(page);
  mock.state.probe = "model-raw";
  mock.state.modelMessage = "完全相同的报错";
  await probe(page);
  await expect(page.locator(".error-notification")).toHaveCount(0, { timeout: 5500 });
  await probe(page);
  await expect(page.locator(".error-notification")).toBeVisible();
  await page.getByRole("button", { name: "关闭错误通知" }).click();
  await expect(page.locator(".error-notification")).toHaveCount(0);
  await page.mouse.move(0, 800);
  await probe(page);
  await expect(page.locator(".error-notification")).toBeVisible();
  await expect.poll(() => opacity(page)).toBeLessThan(0.9);
  expect(await page.evaluate(() => (window as any).errorNotifications.length)).toBe(3);
});

test("悬停恢复完全不透明并停止消失，重复错误保持悬停，移开后重计五秒", async ({ page }) => {
  const mock = await mockAPI(page);
  mock.state.probe = "model-raw";
  mock.state.modelMessage = "可悬停阅读的长错误信息";
  await probe(page);
  await expect.poll(() => opacity(page)).toBeLessThan(0.8);
  await page.locator(".error-notification").hover();
  await expect(page.locator(".error-notification")).toHaveCSS("opacity", "1");
  await page.waitForTimeout(5200);
  await expect(page.locator(".error-notification")).toHaveCSS("opacity", "1");
  await probe(page);
  await page.waitForTimeout(5200);
  await expect(page.locator(".error-notification")).toHaveCSS("opacity", "1");
  await page.mouse.move(0, 800);
  await expect.poll(() => opacity(page)).toBeLessThan(0.95);
  await expect(page.locator(".error-notification")).toHaveCount(0, { timeout: 5500 });
});

test("同一上传校验错误只显示弹窗，消失后再次选择仍弹出", async ({ page }) => {
  await mockAPI(page);
  await page.getByRole("button", { name: "关闭设置" }).click();
  await page.getByRole("button", { name: /开始总结？/ }).click();
  const input = page.getByLabel("选择本地文件", { exact: true });
  const invalid = { name: "不支持.exe", mimeType: "application/octet-stream", buffer: Buffer.from("invalid") };
  await input.setInputFiles(invalid);
  await expect(page.locator(".file-upload-message")).toHaveCount(0);
  await expect(page.locator(".error-notification")).toContainText("请选择支持的视频或文档文件。");
  await expect(page.locator(".error-notification")).toHaveCount(0, { timeout: 5500 });
  await input.setInputFiles(invalid);
  await expect(page.locator(".file-upload-message")).toHaveCount(0);
  await expect(page.locator(".error-notification")).toBeVisible();
});

test("错误弹窗先完整显示三秒，再渐隐移除", async ({ page }) => {
  const mock = await mockAPI(page);
  mock.state.probe = "model-raw";
  mock.state.modelMessage = "先保留三秒的错误";
  await probe(page);
  await page.waitForTimeout(2600);
  await expect(page.locator(".error-notification")).toHaveCSS("opacity", "1");
  await expect.poll(() => opacity(page)).toBeLessThan(0.9);
  await expect(page.locator(".error-notification")).toHaveCount(0, { timeout: 2500 });
});

async function mockFileList(page: Page) {
  const mock = await mockAPI(page);
  mock.state.files = [{ id: "busy-video", filename: "正在处理.mp4", duration: 2, size_bytes: 100,
    created_at: "2026-10-12T00:00:00Z", latest_task: { id: "busy-task", status: "running" } }];
  await page.goto("/#summary");
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { name: "总结分析", exact: true })).toBeVisible();
  return mock;
}

for (const action of ["删除", "重命名", "置顶"]) {
  test(`文件${action}失败只显示一处错误，相同失败仍可重试通知`, async ({ page }) => {
    const mock = await mockFileList(page);
    const message = "请先停止该视频的处理任务，等待停止完成后再删除。";
    let requests = 0;
    await page.route("**/api/v1/videos/busy-video", route => {
      requests++;
      return route.fulfill({ status: 409, json: { error: { message, error_id: "same-error", log_file: "log/same.log", log_saved: true } } });
    });
    await page.locator(".file-list").getByRole("button", { name: "管理文件：正在处理.mp4" }).click();
    await page.getByRole("button", { name: action, exact: true }).click();
    const dialog = page.getByRole("dialog");
    const confirm = dialog.getByRole("button", { name: action === "删除" ? "确认删除" : "保存名称", exact: true });
    if (action !== "置顶") await confirm.click();
    await expect(page.locator(".error-notification")).toContainText(message);
    await expect(page.getByRole("alert")).toHaveCount(1);
    if (action !== "置顶") {
      await expect(dialog).toBeVisible();
      await confirm.click();
    } else {
      await page.locator(".file-list").getByRole("button", { name: "管理文件：正在处理.mp4" }).click();
      await page.getByRole("button", { name: action, exact: true }).click();
    }
    await expect.poll(() => requests).toBe(2);
    await expect(page.getByRole("alert")).toHaveCount(1);
    expect(mock.reports).toHaveLength(0);
  });
}

test("删除和重命名确认框的同组按钮高度与字体间距一致", async ({ page }) => {
  await mockFileList(page);
  for (const action of ["删除", "重命名"]) {
    await page.locator(".file-list").getByRole("button", { name: "管理文件：正在处理.mp4" }).click();
    await page.getByRole("button", { name: action, exact: true }).click();
    const dialog = page.getByRole("dialog");
    const metrics = await dialog.locator(".dialog-actions button").evaluateAll(elements => elements.map(element => {
      const style = getComputedStyle(element), box = element.getBoundingClientRect();
      return { height: box.height, top: box.top, fontSize: style.fontSize, padding: style.padding, margin: style.margin, borderRadius: style.borderRadius };
    }));
    expect(metrics[0]).toEqual(metrics[1]);
    await dialog.getByRole("button", { name: "取消", exact: true }).click();
  }
});

for (const kind of ["llm", "asr"]) {
  test(`设置读取失败仅保留错误弹窗：${kind}`, async ({ page }) => {
    await mockAPI(page);
    await page.getByRole("button", { name: "关闭设置" }).click();
    const path = kind === "llm" ? "model/profiles" : "recognition";
    await page.route(`**/api/v1/settings/${path}`, route => route.fulfill({ status: 500, json: {
      error: { message: "设置读取失败", error_id: "settings-error", log_file: "log/settings.log", log_saved: true },
    } }));
    await page.getByRole("link", { name: /设置/ }).first().click();
    if (kind === "asr") await page.getByRole("tab", { name: "语音识别模型", exact: true }).click();
    await expect(page.locator(".error-notification")).toContainText("设置读取失败");
    await expect(page.getByRole("alert")).toHaveCount(1);
  });
}

for (const operation of ["download", "save"]) {
  test(`识别模型操作失败，下载错误在卡片保留：${operation}`, async ({ page }) => {
    const mock = await mockAPI(page);
    const path = operation === "download" ? "models/paraformer/download" : "recognition";
    await page.route(`**/api/v1/settings/${path}`, async route => {
      if (route.request().method() === "GET") return route.fallback();
      return route.fulfill({ status: 500, json: { error: {
        message: "识别模型操作失败", error_id: "recognition-error", log_file: "log/recognition.log", log_saved: true,
      } } });
    });
    await page.getByRole("tab", { name: "语音识别模型", exact: true }).click();
    await page.getByRole("button", { name: operation === "download" ? "重新下载 / 检查文件" : "保存识别设置", exact: true }).click();
    await expect(page.locator(".error-notification")).toContainText("识别模型操作失败");
    await expect(page.getByRole("alert")).toHaveCount(operation === "download" ? 2 : 1);
    if (operation === "download") await expect(page.locator(".model-download-state.task-error")).toContainText("识别模型操作失败");
    expect(mock.reports).toHaveLength(0);
  });
}

for (const operation of ["profiles", "start", "invalid"]) {
  test(`本次模型选择请求失败仅保留错误弹窗：${operation}`, async ({ page }) => {
    const mock = await mockFileList(page);
    mock.state.models.push({ id: "rapidocr", kind: "ocr", name: "rapidocr", description: "测试 OCR", directory: "models/rapidocr",
      size: "1 MB", docs: "https://test.invalid", ready: true, runtime_ready: true, download: null });
    await page.route("**/api/v1/settings/model/profiles", route => route.fulfill({ json: {
      active_id: "default", profiles: [{ id: "default", name: "测试 API", model: "fake", has_token: true }],
    } }));
    mock.state.files[0].latest_task = null;
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.route("**/api/v1/videos/busy-video/evidence**", route => route.fulfill({ json: [] }));
    await page.route("**/api/v1/videos/busy-video/content", route => route.fulfill({ contentType: "video/mp4",
      body: readFileSync(process.env.E2E_VIDEO_PATH!), }));
    await page.locator(".file-list").getByRole("button", { name: "正在处理.mp4", exact: true }).click();
    const path = operation === "start" ? "videos/busy-video/analyses" : "settings/model/profiles";
    await page.route(`**/api/v1/${path}`, route => operation === "start" && route.request().method() === "GET" ? route.fallback() : route.fulfill(operation === "invalid" ? { contentType: "application/json", body: "{invalid" } : { status: 503, json: { error: {
      message: "本次模型请求失败", error_id: "analysis-error", log_file: "log/analysis.log", log_saved: true,
    } } }));
    await page.getByRole("button", { name: "开始处理", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "选择处理模型" });
    if (operation === "start") await dialog.getByRole("button", { name: "开始处理", exact: true }).click();
    if (operation === "invalid") await expect(page.locator(".error-notification")).toBeVisible();
    else await expect(page.locator(".error-notification")).toContainText("本次模型请求失败");
    await expect(page.getByRole("alert")).toHaveCount(1);
    expect(await page.evaluate(() => (window as any).errorNotifications.length)).toBe(1);
    expect(mock.reports).toHaveLength(operation === "invalid" ? 1 : 0);
    if (operation === "invalid") expect(mock.reports[0].code).toBe("response_format_error");
  });
}

test("系统减少动态效果时仍在五秒后移除错误提示", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const mock = await mockAPI(page);
  mock.state.probe = "model-raw";
  mock.state.modelMessage = "减少动态效果的错误提示";
  await probe(page);
  await expect(page.locator(".error-notification")).toHaveCSS("animation-name", "none");
  await expect(page.locator(".error-notification")).toHaveCount(0, { timeout: 5500 });
});

test("两次下载失败消息相同也分别弹出，轮询和选择历史模型不重复通知", async ({ page }) => {
  const mock = await mockAPI(page);
  mock.state.fastFailure = true;
  await page.getByRole("tab", { name: "语音识别模型", exact: true }).click();
  await page.getByRole("combobox", { name: "语音识别模型", exact: true }).selectOption("whisper-base");
  await page.getByRole("button", { name: "下载模型到 models", exact: true }).click();
  await expect(page.locator(".error-notification")).toContainText("新的下载失败");
  await expect.poll(() => opacity(page)).toBeLessThan(0.8);
  await page.getByRole("button", { name: "下载模型到 models", exact: true }).click();
  await expect.poll(() => page.evaluate(() => (window as any).errorNotifications.length)).toBe(2);
  expect(await opacity(page)).toBeGreaterThan(0.9);
  expect(await page.evaluate(() => (window as any).errorNotifications.length)).toBe(2);
  await page.mouse.move(0, 800);
  await expect(page.locator(".error-notification")).toHaveCount(0, { timeout: 5500 });
  await expect(page.locator(".model-download-state.task-error")).toHaveCount(1);
  await expect(page.locator(".model-download-state.task-error")).toContainText("new-error-2");
  await expect(page.locator(".model-download-state.task-error")).not.toContainText("new-error-1");
});

for (const status of ["running", "failed"]) {
  test(`任务错误和画面识别警告持续保留，轮询不重复通知：${status}`, async ({ page }) => {
    test.skip(!process.env.E2E_VIDEO_PATH, "需要隔离视频样本");
    const mock = await mockAPI(page);
    const task = taskResponse({ id: "warning-task", video_id: "warning-video", status, stage: "ocr", config: {},
      error: status === "failed" ? { message: "任务接口错误", error_id: "task-error", log_file: "log/task.log" } : null,
      warnings: ["有 1 张画面未能完成识别，其余结果已保留。详细日志：log/ocr.log"],
      timings: { ocr: 1.3 }, notes_state: "none", notes_revision: 0 });
    mock.state.files = [{ id: "warning-video", filename: "测试视频.mp4", duration: 2, size_bytes: 100,
      created_at: "2026-10-12T00:00:00Z", latest_task: task }];
    await page.route("**/api/v1/tasks/warning-task", route => route.fulfill({ json: task }));
    await page.route("**/api/v1/videos/warning-video/evidence**", route => route.fulfill({ json: [] }));
    await page.route("**/api/v1/videos/warning-video/content", route => route.fulfill({
      contentType: "video/mp4", body: readFileSync(process.env.E2E_VIDEO_PATH!),
    }));
    await page.clock.install();
    // Navigate a fresh document after changing the fixture, rather than entering the old empty library first.
    await page.goto(`/?warning-task=${status}#summary/warning-video`);
    const activity = page.getByRole("region", { name: "任务动态", exact: true });
    await expect(activity).toContainText("有 1 张画面");
    if (status === "failed") await expect(activity).toContainText("任务接口错误");
    await page.clock.runFor(6000);
    await expect(activity).toContainText("有 1 张画面");
    if (status === "failed") await expect(activity).toContainText("任务接口错误");
    if (status === "failed") await expect(page.locator(".timings")).toContainText("1.3s");
    else await expect(page.getByRole("button", { name: "停止处理", exact: true })).toBeVisible();
    expect(await page.evaluate(() => (window as any).errorNotifications)).toEqual([]);
    if (status === "running") {
      task.status = "failed";
      task.error = { message: "任务接口错误", error_id: "new-task-error", log_file: "log/new-task.log" };
      await expect(page.locator(".error-notification")).toContainText("任务接口错误");
      await expect(activity).toContainText("任务接口错误");
      await expect(activity).toContainText("new-task-error");
      await expect(page.getByRole("alert")).toHaveCount(1);
    }
  });
}
