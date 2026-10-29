import { expect, test, type Page } from "@playwright/test";

const selectedFile = { name: "本次导入.pdf", mimeType: "application/pdf", buffer: Buffer.from("mock PDF") };
const dialog = (page: Page) => page.getByRole("dialog", { name: "生成文档笔记" });
async function importPending(page: Page, file = selectedFile) {
  await page.goto("/#summary");
  const input = page.getByLabel("选择本地文件", { exact: true });
  await expect(input).toHaveAttribute("accept", /\.pdf/);
  await input.setInputFiles(file);
  await expect(page.getByRole("heading", { name: file.name, exact: true })).toBeVisible();
  await expect(dialog(page)).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "总结会出现在这里" })).toBeVisible();
  return new URL(page.url()).hash.split("/")[1];
}
async function openConfiguration(page: Page, file = selectedFile) {
  await importPending(page, file);
  await page.getByRole("button", { name: "开始处理", exact: true }).click();
}
const catalog = {
  settings: { asr_option: "paraformer", ocr_option: "rapidocr", language: "auto" }, active_download: null,
  models: ["rapidocr"].map(id => ({ id, kind: "ocr", name: "RapidOCR",
    ready: true, runtime_ready: true, directory_exists: true, description: "模拟识别", directory: "models/test", size: "test", docs: "", download: null })),
};

test.beforeEach(async ({ page, request }) => {
  expect((await (await request.get("/__test__/identity")).json()).fake_services_used).toBe(true);
  await page.route("**/settings/recognition", route => route.fulfill({ json: catalog }));
  await page.route("**/settings/model/profiles", route => route.fulfill({ json: {
    active_id: "default", profiles: [{ id: "default", name: "当前 API", model: "mock", has_token: true }],
  } }));
  await page.route("**/settings/note-style", route => route.fulfill({ json: { note_style: "concise" } }));
});

test("导入直接显示笔记页，刷新保留原件，点击开始处理才读取配置", async ({ page, request }) => {
  const writes: string[] = [];
  const configs: string[] = [];
  page.on("request", request => { if (request.method() === "POST" && /\/documents(?:\/|$)/.test(request.url())) writes.push(request.url()); });
  page.on("request", request => { if (/\/settings\/(recognition|model\/profiles|note-style)$/.test(request.url())) configs.push(request.url()); });
  const id = await importPending(page);
  expect(writes).toHaveLength(1);
  expect(configs).toEqual([]);
  const saved = (await (await request.get("/api/v1/documents")).json()).find((doc: { id: string }) => doc.id === id);
  expect(saved.extracted).toBe(false);
  expect(saved.latest_task).toBeNull();
  expect((await request.get(`/api/v1/documents/${id}/original`)).ok()).toBe(true);
  await page.reload();
  await expect(page.getByRole("heading", { name: selectedFile.name, exact: true })).toBeVisible();
  await expect(dialog(page)).toHaveCount(0);
  await page.getByRole("button", { name: "开始处理", exact: true }).click();
  await expect(dialog(page).getByLabel("API 配置")).toHaveValue("default");
  await expect(dialog(page).getByRole("combobox", { name: /^笔记风格/ })).toHaveValue("concise");
  await expect(dialog(page).getByLabel("OCR 模型")).toHaveValue("rapidocr");
  await expect(dialog(page).getByRole("button", { name: "开始生成", exact: true })).toBeEnabled();
  expect(writes).toHaveLength(1);
  await dialog(page).getByRole("button", { name: "取消", exact: true }).click();
  await expect(dialog(page)).toHaveCount(0);
  expect(writes).toHaveLength(1);
  await page.getByRole("button", { name: "开始处理", exact: true }).click();
  await expect(dialog(page)).toBeVisible();
});

test("不可用 OCR 禁止开始，纯文本不需要 OCR", async ({ page }) => {
  await page.route("**/settings/recognition", route => route.fulfill({ json: {
    ...catalog, models: catalog.models.map(model => ({ ...model, runtime_ready: false })),
  } }));
  await openConfiguration(page);
  await expect(dialog(page).getByText("所选 OCR 模型不可用", { exact: false })).toBeVisible();
  await expect(dialog(page).getByRole("button", { name: "开始生成", exact: true })).toBeDisabled();
  await dialog(page).getByRole("button", { name: "取消", exact: true }).click();
  await openConfiguration(page, { name: "无须OCR.txt", mimeType: "text/plain", buffer: Buffer.from("原文") });
  await expect(dialog(page).getByLabel("OCR 模型")).toBeDisabled();
  await expect(dialog(page).getByRole("button", { name: "开始生成", exact: true })).toBeEnabled();
});

test("配置读取失败可重读，取消后迟到配置不重开弹窗", async ({ page }) => {
  let attempts = 0;
  await page.route("**/settings/recognition", route => {
    attempts++;
    return route.fulfill(attempts === 1 ? { status: 500, json: { error: { message: "模拟配置失败" } } } : { json: catalog });
  });
  await openConfiguration(page);
  await dialog(page).getByRole("button", { name: "重新读取配置" }).click();
  await expect(dialog(page).getByRole("button", { name: "开始生成", exact: true })).toBeEnabled();
  await dialog(page).getByRole("button", { name: "取消", exact: true }).click();
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  await page.route("**/settings/recognition", async route => { await held; await route.fulfill({ json: catalog }).catch(() => {}); });
  await page.getByRole("button", { name: "开始处理", exact: true }).click();
  await expect(dialog(page).getByText("正在读取生成配置…")).toBeVisible();
  await dialog(page).getByRole("button", { name: "取消", exact: true }).click();
  release();
  await expect(dialog(page)).toHaveCount(0);
});

test("确认后才发送 OCR 提取，失败保留原件且可重试，处理中不能重复或关闭", async ({ page }) => {
  let sends = 0, body = "", release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  await page.route("**/api/v1/documents/*/extract", async route => {
    if (route.request().method() !== "POST") return route.continue();
    sends++;
    body = route.request().postDataBuffer()!.toString();
    await held;
    await route.fulfill({ status: 400, json: { error: { message: "模拟提取失败：请重试", error_id: "import-error" } } });
  });
  await openConfiguration(page);
  await dialog(page).getByLabel("OCR 模型").selectOption("rapidocr");
  await dialog(page).getByRole("button", { name: "开始生成", exact: true }).click();
  await expect(dialog(page).getByRole("button", { name: "正在处理…" })).toBeDisabled();
  await expect(dialog(page).getByRole("button", { name: "取消", exact: true })).toBeDisabled();
  await page.keyboard.press("Escape");
  await expect(dialog(page)).toBeVisible();
  await expect.poll(() => sends).toBe(1);
  expect(JSON.parse(body)).toEqual({ ocr_option: "rapidocr", parser: "mineru_agent", llm_profile_id: "default", note_style: "concise" });
  release();
  await expect(dialog(page).locator("p.notice[role=alert]")).toContainText("模拟提取失败");
  await expect(dialog(page).getByLabel("OCR 模型")).toHaveValue("rapidocr");
  await dialog(page).getByRole("button", { name: "开始生成", exact: true }).click();
  await expect.poll(() => sends).toBe(2);
  await expect(dialog(page).locator("p.notice[role=alert]")).toContainText("模拟提取失败");
});

test("提取后生成失败仍保留文档，重新生成不再次上传或 OCR", async ({ page, request }) => {
  await request.put("/api/v1/settings/model", { data: { token: "fake-import", stream: false } });
  expect((await request.post("/__test__/document-import-failure", { data: { filename: "保留原文.txt" } })).ok()).toBe(true);
  let uploads = 0, creations = 0;
  page.on("request", request => { if (request.method() === "POST" && request.url().endsWith("/api/v1/documents")) uploads++; });
  await page.route("**/documents/*/analyses", route => {
    if (route.request().method() !== "POST") return route.continue();
    creations++;
    expect(route.request().postDataJSON()).toEqual({ llm_profile_id: "default", note_style: "concise" });
    return route.fulfill({ status: 400, json: { error: { message: "模拟生成限制，提取内容仍可查看", error_id: "generation-limit" } } });
  });
  await openConfiguration(page, { name: "保留原文.txt", mimeType: "text/plain", buffer: Buffer.from("导入成功的原文") });
  await dialog(page).getByRole("button", { name: "开始生成", exact: true }).click();
  await expect(dialog(page)).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "保留原文.txt", exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "文档提取进度" }).getByRole("alert")).toContainText("模拟生成限制");
  const id = new URL(page.url()).hash.split("/")[1];
  const content = await (await request.get(`/api/v1/documents/${id}/content`)).json();
  expect(content.units[0].text).toBe("导入成功的原文");
  await page.getByRole("button", { name: "生成笔记", exact: true }).click();
  await expect(dialog(page).getByText(/沿用已有提取内容/)).toBeVisible();
  await expect(dialog(page).getByLabel("OCR 模型")).toHaveCount(0);
  await dialog(page).getByRole("button", { name: "开始生成", exact: true }).click();
  await expect.poll(() => creations).toBe(1);
  expect(uploads).toBe(1);
  await request.delete(`/api/v1/documents/${id}`);
});

test("导入配置在桌面窄屏与亮暗主题下可操作", async ({ page }, testInfo) => {
  await page.goto("/#summary");
  for (const width of [1280, 375]) {
    await page.setViewportSize({ width, height: 871 });
    for (const theme of ["light", "dark"]) {
      await importPending(page);
      await page.evaluate(theme => { document.documentElement.dataset.theme = theme; }, theme);
      await page.screenshot({ path: testInfo.outputPath(`pending-${width}-${theme}.png`), animations: "disabled" });
      await page.getByRole("button", { name: "开始处理", exact: true }).click();
      const setup = dialog(page);
      await expect(setup.getByRole("button", { name: "开始生成", exact: true })).toBeEnabled();
      expect(await setup.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await setup.getByLabel("OCR 模型").focus();
      await expect(setup.getByLabel("OCR 模型")).toBeFocused();
      await page.screenshot({ path: testInfo.outputPath(`import-${width}-${theme}.png`), animations: "disabled" });
      await page.keyboard.press("Escape");
      await expect(setup).toHaveCount(0);
    }
  }
});

test("提取完成的迟到响应不抢占用户已经切换的页面", async ({ page, request }) => {
  await request.put("/api/v1/settings/model", { data: { token: "fake-pending-late", stream: false } });
  let release!: () => void, entered!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  const sent = new Promise<void>(resolve => { entered = resolve; });
  await page.route("**/documents/*/extract", async route => {
    entered();
    await held;
    await route.continue();
  });
  const filename = `迟到提取-${test.info().project.name}.txt`;
  await openConfiguration(page, { name: filename, mimeType: "text/plain", buffer: Buffer.from(filename) });
  await dialog(page).getByRole("button", { name: "开始生成", exact: true }).click();
  await sent;
  await page.evaluate(() => { location.hash = "#summary"; });
  await expect(page.getByRole("heading", { name: "总结分析", exact: true })).toBeVisible();
  release();
  await expect.poll(async () => (await (await request.get("/api/v1/documents")).json())
    .find((doc: { filename: string; extracted: boolean }) => doc.filename === filename && doc.extracted)?.latest_task?.status).toBe("succeeded");
  await expect(page).toHaveURL(/#summary$/);
});
