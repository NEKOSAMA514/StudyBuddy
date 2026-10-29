import { expect, test, type Page, type APIRequestContext } from "@playwright/test";

async function importPDF(page: Page, request: APIRequestContext) {
  const raw = await (await request.get("/__test__/document-samples/pdf")).body();
  await page.goto("/#summary");
  const input = page.getByLabel("选择本地文件", { exact: true });
  await expect(input).toHaveAttribute("accept", /\.pdf/);
  await input.setInputFiles({ name: "MinerU测试.pdf", mimeType: "application/pdf", buffer: raw });
  await expect(page.getByRole("heading", { name: "MinerU测试.pdf", exact: true })).toBeVisible();
  return new URL(page.url()).hash.split("/")[1];
}

test.beforeEach(async ({ request }) => {
  expect((await (await request.get("/__test__/identity")).json()).fake_services_used).toBe(true);
  await request.put("/api/v1/settings/model", { data: { token: "fake-mineru-notes", stream: false } });
  await request.put("/api/v1/settings/mineru", { data: { clear_token: true } });
});

test("轻量默认、Markdown按批保存、后台生成及刷新恢复", async ({ page, request }) => {
  const source = await importPDF(page, request);
  await expect(page.locator(".mineru-hint")).toContainText("免费申请 API Token");
  await page.getByRole("button", { name: "开始处理", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "生成文档笔记" });
  await expect(dialog.getByLabel("PDF 解析方式")).toHaveValue("mineru_agent");
  await expect(dialog.getByText("确认开始后会将 PDF", { exact: false })).toBeVisible();
  await dialog.getByRole("button", { name: "开始生成", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect.poll(async () => (await (await request.get(`/api/v1/documents/${source}/extraction`)).json()).status).toBe("done");
  const state = await (await request.get(`/api/v1/documents/${source}/extraction`)).json();
  await expect(page).toHaveURL(new RegExp(`#summary/${state.document_id}$`));
  await expect(page.locator(".mineru-hint")).toHaveCount(0);
  const content = await (await request.get(`/api/v1/documents/${state.document_id}/content`)).json();
  expect(content.units).toHaveLength(1);
  expect(content.units[0]).toMatchObject({ id: "D1", format: "markdown", locator: { page_start: 1, page_end: 2 } });
  let disconnected = true;
  await page.route(`**/api/v1/documents/${source}/extraction`, route => disconnected
    ? route.fulfill({ status: 503, json: { error: { message: "模拟断线", error_id: "disconnected" } } }) : route.continue());
  await page.goto(`/#summary/${source}`);
  await page.reload();
  await expect(page.getByText("正在重新连接，恢复文档提取记录…", { exact: true })).toBeVisible();
  await expect(page.getByText("文件不存在或已被删除。", { exact: true })).toHaveCount(0);
  disconnected = false;
  await expect(page).toHaveURL(new RegExp(`#summary/${state.document_id}$`));
  await expect(page.getByRole("heading", { name: "文档笔记", exact: true })).toBeVisible();
  await request.delete(`/api/v1/documents/${state.document_id}`);
});

test("Token独立保存清除，配置后PDF默认精准，仍可选择本地", async ({ page, request }) => {
  await importPDF(page, request);
  await page.goto("/#settings");
  const settings = page.getByRole("dialog", { name: "设置", exact: true });
  await expect(settings.getByRole("heading", { name: "LLM API 连接", exact: true })).toBeVisible();
  const llmBox = await settings.locator(".api-profile-heading").boundingBox();
  const mineruBox = await settings.locator(".mineru-settings").boundingBox();
  expect(llmBox!.y + llmBox!.height).toBeLessThan(mineruBox!.y);
  expect(await settings.locator(".mineru-settings > label").evaluate(node => {
    const other = document.querySelector(".api-profile-form > label")!;
    return ["font-size", "font-weight", "margin-top"].every(property =>
      getComputedStyle(node).getPropertyValue(property) === getComputedStyle(other).getPropertyValue(property));
  })).toBe(true);
  await settings.getByLabel("MinerU API Token").fill("fake-mineru-secret");
  await settings.getByRole("button", { name: "保存 MinerU 设置", exact: true }).click();
  await expect(settings.getByText("MinerU 设置已保存。", { exact: true })).toBeVisible();
  expect(await (await request.get("/api/v1/settings/mineru")).json()).toEqual({ has_token: true });
  await settings.getByRole("button", { name: "关闭设置", exact: true }).click();
  await expect(page.locator(".mineru-hint")).toHaveCount(0);
  await page.getByRole("button", { name: "开始处理", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "生成文档笔记" });
  await expect(dialog.getByLabel("PDF 解析方式")).toHaveValue("mineru_vlm");
  await dialog.getByLabel("PDF 解析方式").selectOption("local");
  await expect(dialog.getByText("仅在本机提取文字并识别内嵌图片。", { exact: true })).toBeVisible();
  await dialog.getByRole("button", { name: "取消", exact: true }).click();
  await page.goto("/#settings");
  await settings.getByRole("button", { name: "清除 MinerU Token", exact: true }).click();
  await expect(settings.getByText("MinerU Token 已清除。", { exact: true })).toBeVisible();
});

test("刷新保留并行进度、当前错误常驻及手动重试和本地选择", async ({ page, request }, testInfo) => {
  const id = await importPDF(page, request);
  let status = "running", parser = "mineru_agent", starts = 0, disconnected = false, startFailed = false;
  const state = () => ({ source_id: id, parser, status, document_id: null, task_id: "fake-task",
    progress: { cloud: { stage: "云端排队", message: "第1–20页" }, ocr: { stage: "本地图片 OCR", message: "第3/20页" } },
    error: status === "failed" ? { message: "模拟云端失败", error_id: "fake-reference", log_file: "log/fake-reference.log" } : null });
  await page.route("**/api/v1/documents", async route => {
    const response = await route.fetch();
    const rows = await response.json();
    await route.fulfill({ response, json: rows.map((row: { id: string }) => row.id === id && starts ? { ...row, extraction: state() } : row) });
  });
  await page.route(`**/api/v1/documents/${id}/extract`, route => {
    starts++;
    if (startFailed) return route.fulfill({ status: 500,
      json: { error: { message: "模拟重试启动失败", error_id: "new-start-error", log_file: "log/new-start-error.log" } } });
    parser = route.request().postDataJSON().parser; status = "running";
    return route.fulfill({ status: 202, json: state() });
  });
  await page.route(`**/api/v1/documents/${id}/extraction`, route => route.fulfill(disconnected
    ? { status: 503, json: { error: { message: "模拟断线", error_id: "connection" } } } : { json: state() }));
  await page.getByRole("button", { name: "开始处理", exact: true }).click();
  await page.getByRole("dialog", { name: "生成文档笔记" }).getByRole("button", { name: "开始生成", exact: true }).click();
  const progress = page.getByRole("region", { name: "文档提取进度" });
  await expect(progress).toContainText("本地图片 OCR");
  await page.reload();
  await expect(progress).toContainText("第1–20页");
  disconnected = true;
  await expect(progress.getByRole("status")).toContainText("正在重新连接");
  disconnected = false;
  await expect(progress.getByRole("status")).toHaveCount(0);
  for (const width of [1280, 375]) {
    await page.setViewportSize({ width, height: 871 });
    for (const theme of ["light", "dark"]) {
      await page.evaluate(value => { document.documentElement.dataset.theme = value; }, theme);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.screenshot({ path: testInfo.outputPath(`mineru-${width}-${theme}.png`), animations: "disabled" });
    }
  }
  status = "failed";
  await expect(progress.getByRole("alert")).toContainText("fake-reference");
  await page.waitForTimeout(5200);
  await expect(progress.getByRole("alert")).toContainText("模拟云端失败");
  startFailed = true;
  await progress.getByRole("button", { name: "重试未完成部分" }).click();
  const card = page.locator("#document-generation");
  await expect(card.getByRole("alert")).toContainText("log/new-start-error.log");
  await expect(card.getByRole("alert")).not.toContainText("fake-reference");
  startFailed = false;
  await progress.getByRole("button", { name: "重试未完成部分" }).click();
  await expect.poll(() => starts).toBe(3);
  await expect(card.getByRole("alert")).toHaveCount(0);
  status = "failed";
  await page.reload();
  await expect(progress.getByRole("alert")).toContainText("模拟云端失败");
  await progress.getByRole("button", { name: "整份改用本地提取" }).click();
  await expect.poll(() => parser).toBe("local");
  await expect(page.locator(".mineru-hint")).toHaveCount(0);
  await page.goto("/#");
  await page.unrouteAll({ behavior: "wait" });
});

test("解析方式复用表单外观，窄屏和亮暗主题保持对齐", async ({ page, request }, testInfo) => {
  await importPDF(page, request);
  await page.getByRole("button", { name: "开始处理", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "生成文档笔记" });
  const parser = dialog.getByLabel("PDF 解析方式");
  const ocr = dialog.getByLabel("OCR 模型");
  for (const width of [1280, 375]) {
    await page.setViewportSize({ width, height: 871 });
    for (const theme of ["light", "dark"]) {
      await page.evaluate(value => { document.documentElement.dataset.theme = value; }, theme);
      const parserBox = (await parser.boundingBox())!, ocrBox = (await ocr.boundingBox())!;
      expect(Math.abs(parserBox.width - ocrBox.width)).toBeLessThanOrEqual(1);
      expect(Math.abs(parserBox.x - ocrBox.x)).toBeLessThanOrEqual(1);
      expect(await parser.evaluate(node => {
        const other = document.querySelector('label.note-style-selection select')!;
        return ["font-size", "border-radius", "border-color", "background-color", "padding"].every(property =>
          getComputedStyle(node).getPropertyValue(property) === getComputedStyle(other).getPropertyValue(property));
      })).toBe(true);
      expect(await dialog.evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
      await dialog.screenshot({ path: testInfo.outputPath(`parser-${width}-${theme}.png`) });
    }
  }
  await parser.focus();
  await expect(parser).toBeFocused();
});

test("MinerU配置读取失败不阻止本地提取，重读不重置手动选择", async ({ page, request }) => {
  let failed = true;
  await page.route("**/api/v1/settings/mineru", route => route.fulfill(failed
    ? { status: 503, json: { error: { message: "模拟解析配置读取失败", error_id: "mineru-settings-read" } } }
    : { json: { has_token: true } }));
  await importPDF(page, request);
  await page.getByRole("button", { name: "开始处理", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "生成文档笔记" });
  await expect(dialog.getByRole("alert")).toContainText("模拟解析配置读取失败");
  await expect(dialog.getByLabel("PDF 解析方式")).toHaveValue("local");
  await expect(dialog.getByRole("button", { name: "开始生成", exact: true })).toBeEnabled();
  await expect(dialog.locator('option[value="mineru_vlm"]')).toHaveAttribute("disabled", "");
  await dialog.getByLabel("笔记风格", { exact: false }).selectOption("concise");
  failed = false;
  await dialog.getByRole("button", { name: "重新读取 MinerU 设置", exact: true }).click();
  await expect(dialog.locator('option[value="mineru_vlm"]')).not.toHaveAttribute("disabled");
  await expect(dialog.getByRole("alert")).toHaveCount(0);
  await expect(dialog.getByLabel("PDF 解析方式")).toHaveValue("local");
  await expect(dialog.getByLabel("笔记风格", { exact: false })).toHaveValue("concise");
  const submitted = page.waitForResponse(response => response.url().endsWith("/extract") && response.request().method() === "POST");
  await dialog.getByRole("button", { name: "开始生成", exact: true }).click();
  const response = await submitted;
  expect(response.status()).toBe(202);
  expect(response.request().postDataJSON()).toMatchObject({ parser: "local", note_style: "concise" });
});

for (const action of ["save", "clear"]) {
  test(`MinerU ${action}关闭后隔离迟到响应与错误`, async ({ page }) => {
    let release!: () => void, sent!: () => void;
    const gate = new Promise<void>(resolve => { release = resolve; });
    const requested = new Promise<void>(resolve => { sent = resolve; });
    let puts = 0, closed = false, lateHealthReads = 0;
    page.on("request", request => { if (closed && request.url().endsWith("/health")) lateHealthReads++; });
    await page.route("**/api/v1/settings/mineru", async route => {
      if (route.request().method() === "PUT") {
        puts++;
        expect(route.request().postDataJSON().clear_token).toBe(action === "clear");
        sent(); await gate;
        await route.fulfill(action === "save" ? { json: { has_token: true } }
          : { status: 500, json: { error: { message: "不应显示的旧保存错误", error_id: "late-mineru-save" } } });
      } else await route.fulfill({ json: { has_token: true } });
    });
    await page.goto("/#settings");
    const settings = page.getByRole("dialog", { name: "设置", exact: true });
    if (action === "save") await settings.getByLabel("MinerU API Token").fill("fake-isolated-token");
    await settings.getByRole("button", { name: action === "save" ? "保存 MinerU 设置" : "清除 MinerU Token", exact: true }).click();
    await requested;
    await expect(settings.getByLabel("MinerU API Token")).toBeDisabled();
    expect(puts).toBe(1);
    await settings.getByRole("button", { name: "关闭设置", exact: true }).click();
    closed = true;
    release();
    await page.goto("/#settings");
    await expect(settings.getByLabel("MinerU API Token")).toBeEnabled();
    await page.waitForLoadState("networkidle");
    await expect(settings).not.toContainText("不应显示的旧保存错误");
    await expect(page.locator(".error-notification").filter({ hasText: "不应显示的旧保存错误" })).toHaveCount(0);
    expect(lateHealthReads).toBe(0);
  });
}

test("MinerU读取与保存错误常驻，独立重读和保存成功后清除", async ({ page }, testInfo) => {
  let readFailed = true, saveFailed = true;
  await page.route("**/api/v1/settings/mineru", route => {
    const failed = route.request().method() === "PUT" ? saveFailed : readFailed;
    return route.fulfill(failed ? { status: 503, json: { error: { message: "模拟 MinerU 设置错误",
      error_id: "settings-reference", log_file: "log/settings-reference.log" } } } : { json: { has_token: true } });
  });
  await page.clock.install();
  await page.goto("/#settings");
  const settings = page.getByRole("dialog", { name: "设置", exact: true });
  const card = settings.locator(".mineru-settings");
  await expect(card.getByRole("alert")).toContainText("log/settings-reference.log");
  await expect(settings.getByRole("heading", { name: "LLM API 连接", exact: true })).toBeVisible();
  readFailed = false;
  await card.getByRole("button", { name: "重新读取 MinerU 设置", exact: true }).click();
  await expect(card.getByLabel("MinerU API Token")).toBeEnabled();
  await card.getByLabel("MinerU API Token").fill("fake-isolated-token");
  await card.getByRole("button", { name: "保存 MinerU 设置", exact: true }).click();
  await expect(card.getByRole("alert")).toContainText("settings-reference");
  await page.clock.runFor(6000);
  await expect(card.getByRole("alert")).toBeVisible();
  for (const width of [1280, 375]) {
    await page.setViewportSize({ width, height: 871 });
    for (const theme of ["light", "dark"]) {
      await page.evaluate(value => { document.documentElement.dataset.theme = value; }, theme);
      expect(await card.evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
      await card.screenshot({ path: testInfo.outputPath(`mineru-settings-${width}-${theme}.png`) });
    }
  }
  saveFailed = false;
  await card.getByRole("button", { name: "保存 MinerU 设置", exact: true }).click();
  await expect(card.getByRole("alert")).toHaveCount(0);
  await expect(card.getByRole("status")).toHaveText("MinerU 设置已保存。");
  await expect(card.getByLabel("MinerU API Token")).toHaveValue("");
});

test("提取重连不重复通知或上报客户端日志，恢复后清除连接提示", async ({ page, request }) => {
  const id = await importPDF(page, request);
  let failure = "", failedPolls = 0, reports = 0;
  const state = { source_id: id, parser: "mineru_agent", status: "running", document_id: null, task_id: "fake-task",
    progress: { cloud: { stage: "云端排队", message: "第1–20页" } }, error: null };
  page.on("request", request => { if (request.url().includes("/diagnostics/client-errors")) reports++; });
  await page.route(`**/api/v1/documents/${id}/extract`, route => route.fulfill({ status: 202, json: state }));
  await page.route("**/api/v1/documents", async route => {
    const response = await route.fetch();
    await route.fulfill({ response, json: (await response.json()).map((row: { id: string }) => row.id === id ? { ...row, extraction: state } : row) });
  });
  await page.route(`**/api/v1/documents/${id}/extraction`, route => {
    if (!failure) return route.fulfill({ json: state });
    failedPolls++;
    return failure === "network" ? route.abort("connectionrefused") : route.fulfill(failure === "json"
      ? { status: 502, body: "<html>gateway unavailable</html>", contentType: "text/html" }
      : { status: 503, json: { error: { message: "模拟断线", error_id: "repeat-connection" } } });
  });
  await page.reload();
  const progress = page.getByRole("region", { name: "文档提取进度" });
  for (const mode of ["http", "network", "json"]) {
    failure = mode; failedPolls = 0;
    await expect.poll(() => failedPolls).toBeGreaterThanOrEqual(2);
    await expect(progress.getByRole("status")).toContainText("正在重新连接");
    await expect(page.locator(".error-notification:not(.info):not(.success)")).toHaveCount(0);
    expect(reports).toBe(0);
    failure = "";
    await expect(progress.getByRole("status")).toHaveCount(0);
  }
});

test("PPTX配置不增加MinerU选项或提示", async ({ page, request }) => {
  const raw = await (await request.get("/__test__/document-samples/pptx")).body();
  const calls: string[] = [];
  page.on("request", request => { if (request.url().endsWith("/settings/mineru")) calls.push(request.url()); });
  await page.goto("/#summary");
  await page.getByLabel("选择本地文件", { exact: true }).setInputFiles({ name: "本地PPT.pptx", mimeType: "application/octet-stream", buffer: raw });
  await expect(page.getByRole("heading", { name: "本地PPT.pptx", exact: true })).toBeVisible();
  await expect(page.locator(".mineru-hint")).toHaveCount(0);
  await page.getByRole("button", { name: "开始处理", exact: true }).click();
  await expect(page.getByLabel("OCR 模型")).toBeVisible();
  await expect(page.getByLabel("PDF 解析方式")).toHaveCount(0);
  expect(calls).toEqual([]);
});
