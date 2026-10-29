import { test, expect, type APIRequestContext } from "@playwright/test";
import { readFileSync } from "node:fs";

async function upload(request: APIRequestContext, name: string, text: string) {
  const response = await request.post("/api/v1/documents", { multipart: { file: { name, mimeType: "text/plain", buffer: Buffer.from(text) } } });
  expect(response.ok()).toBe(true);
  return response.json();
}

async function clearDocuments(request: APIRequestContext) {
  for (const doc of await (await request.get("/api/v1/documents")).json()) {
    if (doc.latest_task && ["queued", "running", "stopping"].includes(doc.latest_task.status)) {
      await request.post(`/api/v1/document-tasks/${doc.latest_task.id}/stop`);
      await expect.poll(async () => (await (await request.get(`/api/v1/document-tasks/${doc.latest_task.id}`)).json()).status).toBe("paused");
    }
    expect((await request.delete(`/api/v1/documents/${doc.id}`)).ok()).toBe(true);
  }
}

test.beforeEach(async ({ request }) => {
  expect((await (await request.get("/__test__/identity")).json()).fake_services_used).toBe(true);
  await request.put("/api/v1/settings/model", { data: { token: "fake-document-browser", stream: false } });
  await clearDocuments(request);
});
test.afterEach(async ({ request }) => { await clearDocuments(request); });

test("文档切换API预算重新预检，迟到片段数不覆盖当前选择", async ({ page, request }) => {
  const doc = await upload(request, "预算预检.txt", "原文".repeat(3000));
  const created = await request.post("/api/v1/settings/model/profiles", { data: {
    name: "小输入预算", token: "fake-preview", input_text_limit: 6000,
  } });
  expect(created.ok()).toBe(true);
  const profile = (await created.json()).profiles.at(-1).id;
  const pattern = `**/documents/${doc.id}/analysis-preview?llm_profile_id=${profile}&note_style=*`;
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  await page.route(pattern, async route => {
    const response = await route.fetch();
    await held;
    await route.fulfill({ response }).catch(() => {});
  });
  await page.goto(`/#summary/${doc.id}`);
  await expect(page.locator("#document-generation .panel-header h2")).toHaveText("处理进度");
  await expect(page.locator("#document-generation .panel-header .status-pill")).toHaveText("待开始");
  await expect(page.locator("#document-generation").getByRole("progressbar")).toHaveCount(0);
  await page.getByRole("button", { name: "生成笔记", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "生成文档笔记" });
  const start = dialog.getByRole("button", { name: "开始生成", exact: true });
  const picker = dialog.getByRole("combobox", { name: "API 配置" });
  await expect(dialog.getByText(/预计 1 段/)).toBeVisible();
  await picker.selectOption(profile);
  await expect(start).toBeDisabled();
  await expect(dialog.getByRole("status")).toContainText("正在检查所选配置");
  await picker.selectOption("default");
  release();
  await expect(start).toBeEnabled();
  await expect(dialog.getByText(/预计 1 段/)).toBeVisible();
  await page.unroute(pattern);
  await picker.selectOption(profile);
  await expect(dialog.getByText(/预计 [2-8] 段/)).toBeVisible();
  await expect(start).toBeEnabled();
  await dialog.getByRole("button", { name: "取消", exact: true }).click();
});

test("文档笔记复用清单与脚注修复，窄屏保留章节层级", async ({ page, request }) => {
  const doc = await upload(request, "正文结构.txt", "概念讲解与相关依据。");
  const created = await request.post(`/api/v1/documents/${doc.id}/analyses`, { data: {} });
  expect(created.ok()).toBe(true);
  const { task_id: taskId } = await created.json();
  await expect.poll(async () => (await (await request.get(`/api/v1/document-tasks/${taskId}`)).json()).status).toBe("succeeded");
  await page.route(`**/api/v1/document-analyses/${taskId}`, async route => {
    const response = await route.fetch();
    const result = await response.json();
    result.chapters[0].markdown = "## 条件说明\n\n- [x] 已验证\n- [ ] 待复习\n\n结论[^1]。\n\n[^1]: 原文依据。";
    await route.fulfill({ response, json: result });
  });
  await page.setViewportSize({ width: 375, height: 871 });
  await page.goto(`/#summary/${doc.id}`);
  await expect(page.locator("#document-notes .overview .eyebrow")).toHaveText("材料导览");
  const body = page.locator("#document-notes .note-markdown").first();
  await expect(body.getByRole("checkbox", { name: "已完成", exact: true })).toBeChecked();
  await expect(body.getByRole("checkbox", { name: "未完成", exact: true })).not.toBeChecked();
  await body.locator("a[data-footnote-ref]").click();
  await expect(body.locator("section[data-footnotes] li")).toBeFocused();
  const chapter = page.locator("#document-notes .summary-section > .note-heading > h3").first();
  expect(await chapter.evaluate(element => Number.parseFloat(getComputedStyle(element).fontSize)))
    .toBeGreaterThan(await body.locator("h2").first().evaluate(element => Number.parseFloat(getComputedStyle(element).fontSize)));
  expect(page.url()).toContain(`/#summary/${doc.id}`);
});

for (const extension of ["pdf", "docx", "pptx", "xlsx", "csv", "txt", "md"]) {
  test(`${extension}文档生成和双格式下载无需识别模型`, async ({ page, request }) => {
    const sample = await request.get(`/__test__/document-samples/${extension}`);
    const uploaded = await request.post("/api/v1/documents", { multipart: {
      file: { name: `笔记样本.${extension}`, mimeType: "application/octet-stream", buffer: await sample.body() },
    } });
    const doc = await uploaded.json();
    const recognizers: string[] = [];
    const sourceRequests: string[] = [];
    page.on("request", request => { if (request.url().includes("/settings/recognition")) recognizers.push(request.url()); });
    page.on("request", request => { if (new URL(request.url()).pathname === `/api/v1/documents/${doc.id}/sources`) sourceRequests.push(request.url()); });
    await page.goto(`/#summary/${doc.id}`);
    await page.getByRole("button", { name: "生成笔记", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "生成文档笔记" });
    await expect(dialog.getByText(/预计 \d+ 段/)).toBeVisible();
    await expect(dialog.getByRole("combobox", { name: "API 配置" })).toBeVisible();
    await expect(dialog.getByText("语音识别模型", { exact: true })).toHaveCount(0);
    await dialog.getByRole("button", { name: "开始生成", exact: true }).click();
    const notes = page.locator("#document-notes");
    await expect(notes.getByRole("heading", { name: "文档主题总结", exact: true })).toBeVisible({ timeout: 15000 });
    await expect(notes.locator(".summary-section")).not.toHaveCount(0);
    await expect(notes.locator(".note-markdown strong").first()).toHaveText("主要结论");
    await expect(page.locator("#document-generation .status-pill")).toHaveText("已完成");
    expect(recognizers).toHaveLength(0);
    for (const label of ["下载 TXT", "下载 Markdown"]) {
      const pending = page.waitForEvent("download");
      await notes.getByRole("link", { name: label, exact: true }).click();
      const downloaded = await pending;
      expect(readFileSync((await downloaded.path())!, "utf-8")).toContain("文档主题总结");
    }
    await expect(notes.locator(".cite-button, .document-sources")).toHaveCount(0);
    await expect(notes.locator(".document-source-label").first()).toContainText("来源：");
    await page.locator(".document-preview summary").click();
    await expect(page.locator(".document-unit").first()).toBeVisible();
    await page.reload();
    await expect(notes.getByRole("heading", { name: "文档主题总结", exact: true })).toBeVisible();
    expect(sourceRequests).toEqual([]);
    await page.getByRole("button", { name: "← 返回", exact: true }).click();
    await expect(page.locator(".file-list tr").filter({ hasText: `笔记样本.${extension}` }).locator(".status-pill")).toHaveText("已完成");
  });
}

test("两版本各自下载和跨分页提取预览，旧版本阅读不触发生成", async ({ page, request }, testInfo) => {
  const doc = await upload(request, "多页来源.txt", Array.from({ length: 2300 }, (_, i) => `第${i + 1}行：章节依据`).join("\n"));
  const ids: string[] = [];
  for (let i = 0; i < 3; i++) {
    const result = await (await request.post(`/api/v1/documents/${doc.id}/analyses`)).json();
    ids.push(result.task_id);
    await expect.poll(async () => (await (await request.get(`/api/v1/document-tasks/${result.task_id}`)).json()).status).toBe("succeeded");
  }
  expect((await request.get(`/api/v1/document-analyses/${ids[0]}`)).status()).toBe(409);
  await page.setViewportSize({ width: 598, height: 871 });
  await page.goto(`/#summary/${doc.id}`);
  const notes = page.locator("#document-notes");
  const preview = page.locator(".document-preview details");
  await expect(preview).not.toHaveAttribute("open");
  await expect(page.getByText("解析说明", { exact: true })).toHaveCount(0);
  const actions = page.locator(".document-task-actions");
  await expect(actions.getByRole("button")).toHaveText(["重新生成笔记", "查看已生成笔记 ↓"]);
  for (const width of [1280, 375]) {
    await page.setViewportSize({ width, height: 871 });
    for (const theme of ["light", "dark"]) {
      if (await page.locator("html").getAttribute("data-theme") !== theme) await page.locator(".theme-toggle").click();
      const generate = await actions.getByRole("button").first().boundingBox();
      const view = await actions.getByRole("button").last().boundingBox();
      expect(generate && view && Math.abs(generate.y - view.y) < 1 && view.x > generate.x).toBeTruthy();
      const back = await page.getByRole("button", { name: "← 返回", exact: true }).boundingBox();
      const heading = await page.locator(".document-heading").boundingBox();
      expect(back && heading && Math.abs(back.x - heading.x) < 1 && back.width < heading.width / 2).toBeTruthy();
      await page.evaluate(() => scrollTo(0, 0));
      await page.screenshot({ path: testInfo.outputPath(`document-controls-${width}-${theme}.png`), animations: "disabled" });
    }
  }
  await page.setViewportSize({ width: 598, height: 871 });
  await notes.getByRole("button", { name: "上一份笔记", exact: true }).click();
  await expect(notes.getByRole("link", { name: "下载 TXT", exact: true })).toHaveAttribute("href", new RegExp(ids[1]));
  await expect(notes.locator(".cite-button, .document-sources")).toHaveCount(0);
  await preview.locator("summary").click();
  await preview.getByRole("spinbutton", { name: "内容块" }).fill("21");
  await preview.getByRole("button", { name: "定位", exact: true }).click();
  await expect(preview).toHaveAttribute("open");
  await expect(page.locator("#document-unit-D23")).toBeVisible();
  await expect(page.locator(".document-preview .document-unit").first()).toHaveAttribute("id", "document-unit-D21");
  await preview.locator("summary").click();
  await expect(page.locator("#document-unit-D23")).toBeHidden();
  await page.locator(".document-preview").scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("document-preview-collapsed.png"), animations: "disabled" });
  await preview.locator("summary").click();
  await expect(preview).toHaveAttribute("open");
  await expect(page.locator("#document-unit-D23")).toBeVisible();
  await notes.getByRole("button", { name: "最新笔记", exact: true }).click();
  await expect(notes.getByRole("link", { name: "下载 Markdown", exact: true })).toHaveAttribute("href", new RegExp(ids[2]));
  await notes.scrollIntoViewIfNeeded();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("document-notes-mobile.png") });
});

test("停止、继续和 API 配置快照保持同一任务，关闭设置取消迟到预检", async ({ page, request }) => {
  const doc = await upload(request, `e2e-document-held-resume-${Date.now()}.txt`, "需要生成的文档原文。");
  await page.goto(`/#summary/${doc.id}`);
  await page.getByRole("button", { name: "生成笔记", exact: true }).click();
  await page.getByRole("button", { name: "开始生成", exact: true }).click();
  await expect(page.locator("#document-generation .status-pill")).toHaveText("处理中");
  const current = (await (await request.get("/api/v1/documents")).json())[0].latest_task.id;
  await page.getByRole("button", { name: "停止处理", exact: true }).click();
  await expect(page.locator("#document-generation .status-pill")).toHaveText("已停止");
  await page.getByRole("button", { name: "继续处理", exact: true }).click();
  await request.post(`/__test__/documents/${doc.id}/release`);
  await expect(page.locator("#document-generation .status-pill")).toHaveText("已完成");
  const versions = await (await request.get(`/api/v1/documents/${doc.id}/analyses`)).json();
  expect(versions[0].id).toBe(current);
  let release!: () => void;
  const pending = new Promise<void>(resolve => { release = resolve; });
  await page.route(`**/documents/${doc.id}/analysis-preview`, async route => {
    await pending;
    await route.fulfill({ status: 500, json: { error: { message: "迟到预检失败" } } }).catch(() => {});
  });
  await page.getByRole("button", { name: "重新生成笔记", exact: true }).click();
  await page.getByRole("button", { name: "关闭文档生成设置", exact: true }).click();
  release();
  await expect(page.getByRole("alert")).toHaveCount(0);
});

test("超限材料保留预览但禁止生成", async ({ page, request }) => {
  const doc = await upload(request, "超限文档.txt", "字".repeat(100001));
  await page.goto(`/#summary/${doc.id}`);
  await page.locator(".document-preview summary").click();
  await expect(page.locator(".document-unit").first()).toBeVisible();
  await page.getByRole("button", { name: "生成笔记", exact: true }).click();
  await expect(page.getByRole("dialog").getByText(/请拆分文件/)).toBeVisible();
  await expect(page.getByRole("button", { name: "开始生成", exact: true })).toBeDisabled();
});

test("阅读已保存版本时停止继续只作用于新任务，完成后不打断旧笔记", async ({ page, request }) => {
  const doc = await upload(request, "原笔记.txt", "原文内容及其条件。\n".repeat(200));
  const first = (await (await request.post(`/api/v1/documents/${doc.id}/analyses`)).json()).task_id;
  await expect.poll(async () => (await (await request.get(`/api/v1/document-tasks/${first}`)).json()).status).toBe("succeeded");
  await request.patch(`/api/v1/documents/${doc.id}`, { data: { filename: `e2e-document-held-old-reading-${Date.now()}.txt` } });
  await page.goto(`/#summary/${doc.id}`);
  const notes = page.locator("#document-notes");
  await expect(notes.getByRole("link", { name: "下载 TXT", exact: true })).toHaveAttribute("href", new RegExp(first));
  await notes.getByRole("button", { name: "最新笔记", exact: true }).click();
  await notes.locator(".summary-section").first().evaluate(el => { el.dataset.retained = "yes"; });
  await page.getByRole("button", { name: "重新生成笔记", exact: true }).click();
  await page.getByRole("button", { name: "开始生成", exact: true }).click();
  await expect(page.locator("#document-generation .status-pill")).toHaveText("处理中");
  const second = (await (await request.get("/api/v1/documents")).json())[0].latest_task.id;
  expect(second).not.toBe(first);
  await page.getByRole("button", { name: "停止处理", exact: true }).click();
  await expect(page.locator("#document-generation .status-pill")).toHaveText("已停止");
  expect((await (await request.get(`/api/v1/document-tasks/${first}`)).json()).status).toBe("succeeded");
  await page.getByRole("button", { name: "继续处理", exact: true }).click();
  await request.post(`/__test__/documents/${doc.id}/release`);
  await expect(page.locator("#document-generation .status-pill")).toHaveText("已完成");
  await expect(notes.getByRole("link", { name: "下载 TXT", exact: true })).toHaveAttribute("href", new RegExp(first));
  await expect(notes.locator(".summary-section").first()).toHaveAttribute("data-retained", "yes");
});

test("快速切换版本取消迟到正文，不覆盖当前阅读或弹旧错误", async ({ page, request }) => {
  const doc = await upload(request, "版本隔离.txt", "不同版本的原文内容。");
  const ids: string[] = [];
  for (let i = 0; i < 2; i++) {
    const result = await (await request.post(`/api/v1/documents/${doc.id}/analyses`)).json();
    ids.push(result.task_id);
    await expect.poll(async () => (await (await request.get(`/api/v1/document-tasks/${result.task_id}`)).json()).status).toBe("succeeded");
  }
  let release!: () => void, reading = false;
  const held = new Promise<void>(resolve => { release = resolve; });
  await page.route(`**/document-analyses/${ids[0]}`, async route => {
    reading = true;
    await held;
    await route.fulfill({ status: 500, json: { error: { message: "迟到的正文错误" } } }).catch(() => {});
  });
  await page.goto(`/#summary/${doc.id}`);
  const notes = page.locator("#document-notes");
  await expect(notes.getByRole("link", { name: "下载 TXT", exact: true })).toHaveAttribute("href", new RegExp(ids[1]));
  await notes.getByRole("button", { name: "上一份笔记", exact: true }).click();
  await expect.poll(() => reading).toBe(true);
  await notes.getByRole("button", { name: "最新笔记", exact: true }).click();
  release();
  await expect(notes.getByRole("link", { name: "下载 TXT", exact: true })).toHaveAttribute("href", new RegExp(ids[1]));
  await page.unroute(`**/document-analyses/${ids[0]}`);
  await expect(page.getByRole("alert")).toHaveCount(0);
  await expect(notes.locator(".cite-button, .document-sources")).toHaveCount(0);
});

for (const theme of ["light", "dark"]) test(`文档章节导航使用原文位置，窄屏键盘跳转与${theme}主题`, async ({ page, request }, testInfo) => {
  const doc = await upload(request, `导航-${theme}.txt`, "原文中的核心条件与解释。\n".repeat(1200));
  const { task_id } = await (await request.post(`/api/v1/documents/${doc.id}/analyses`)).json();
  await expect.poll(async () => (await (await request.get(`/api/v1/document-tasks/${task_id}`)).json()).status).toBe("succeeded");
  await page.addInitScript(value => localStorage.setItem("studybuddy-theme", value), theme);
  await page.setViewportSize({ width: 598, height: 871 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto(`/#summary/${doc.id}`);
  const notes = page.locator("#document-notes");
  await expect(notes.locator(".summary-section")).toHaveCount(2);
  await expect(notes.locator(".note-markdown strong").last()).toHaveText("主要结论");
  await expect(page.getByText("按文档文字规划章节并生成笔记，可按来源回看原文。", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "下载原文件" })).toHaveCount(0);
  const actions = page.locator(".document-task-actions button");
  const boxes = await actions.evaluateAll(buttons => buttons.map(button => {
    const rect = button.getBoundingClientRect();
    return { left: rect.left, top: rect.top, bottom: rect.bottom, radius: getComputedStyle(button).borderRadius };
  }));
  expect(boxes).toHaveLength(2);
  expect(Math.abs(boxes[0].top - boxes[1].top)).toBeLessThan(1);
  expect(boxes[1].left).toBeGreaterThan(boxes[0].left);
  expect(boxes[1].radius).toBe(boxes[0].radius);
  await notes.scrollIntoViewIfNeeded();
  const navigator = notes.getByRole("navigation", { name: "笔记章节导航" });
  const marks = navigator.getByRole("button");
  await expect(marks).toHaveCount(2);
  await marks.last().focus();
  await expect(navigator.getByRole("tooltip")).toContainText("行");
  await expect(navigator.getByRole("tooltip")).not.toContainText("00:");
  await marks.last().press("Enter");
  await expect(notes.locator("#document-note-C2")).toBeFocused();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath(`document-notes-${theme}.png`) });
  await page.getByRole("button", { name: "重新生成笔记", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "生成文档笔记" });
  const start = dialog.getByRole("button", { name: "开始生成", exact: true });
  await expect(start).toBeEnabled();
  const cancelBox = await dialog.getByRole("button", { name: "取消", exact: true }).boundingBox();
  const startBox = await start.boundingBox();
  const rowBox = await dialog.locator(".settings-actions").boundingBox();
  expect(Math.abs(startBox!.y - cancelBox!.y)).toBeLessThan(1);
  expect(Math.abs(startBox!.height - cancelBox!.height)).toBeLessThan(2);
  expect(Math.abs(startBox!.x + startBox!.width - rowBox!.x - rowBox!.width)).toBeLessThan(1);
  await page.screenshot({ path: testInfo.outputPath(`document-setup-${theme}.png`) });
  await dialog.getByRole("button", { name: "取消", exact: true }).click();
});

test("简洁PPT小标题来源显示幻灯片范围并可回看", async ({ page, request }, testInfo) => {
  const sample = await request.get("/__test__/document-samples/pptx");
  const response = await request.post("/api/v1/documents", { multipart: { file: {
    name: "简洁页码来源.pptx", mimeType: "application/octet-stream", buffer: await sample.body(),
  } } });
  const doc = await response.json();
  const { task_id } = await (await request.post(`/api/v1/documents/${doc.id}/analyses`, { data: { note_style: "concise" } })).json();
  await expect.poll(async () => (await (await request.get(`/api/v1/document-tasks/${task_id}`)).json()).status).toBe("succeeded");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto(`/#summary/${doc.id}`);
  const notes = page.locator("#document-notes");
  const source = notes.locator(".note-markdown h2").getByRole("link", { name: "来源：幻灯片 1–幻灯片 2", exact: true });
  await expect(source).toBeVisible();
  await expect(source).toHaveAttribute("href", "#document-unit-D1");
  await source.click();
  await expect(page.locator("#document-unit-D1")).toBeFocused();
  for (const width of [1280, 375]) {
    await page.setViewportSize({ width, height: 871 });
    for (const theme of ["light", "dark"]) {
      await page.evaluate(value => document.documentElement.setAttribute("data-theme", value), theme);
      await notes.screenshot({ path: testInfo.outputPath(`slide-range-${width}-${theme}.png`), animations: "disabled" });
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    }
  }
  for (const format of ["txt", "md"]) {
    const exported = await request.get(`/api/v1/document-analyses/${task_id}/notes?format=${format}`);
    expect(await exported.text()).toContain("来源：幻灯片 1–幻灯片 2");
    expect(await exported.text()).not.toContain("#document-unit-");
  }
});

test("简洁文档小标题来源跨页定位、键盘跳转且不影响重点编号", async ({ page, request }, testInfo) => {
  const doc = await upload(request, "简洁来源定位.txt", "循环条件。\n".repeat(2300));
  const response = await request.post(`/api/v1/documents/${doc.id}/analyses`, { data: { note_style: "concise" } });
  const { task_id } = await response.json();
  await expect.poll(async () => (await (await request.get(`/api/v1/document-tasks/${task_id}`)).json()).status).toBe("succeeded");
  const result = await (await request.get(`/api/v1/document-analyses/${task_id}`)).json();
  expect(result.chapters[0].source_ids).toContain("D23");
  const late = (await (await request.get(`/api/v1/documents/${doc.id}/content?offset=20&limit=10`)).json()).units.find((unit: { id: string }) => unit.id === "D23");
  const first = (await (await request.get(`/api/v1/documents/${doc.id}/content?offset=0&limit=10`)).json()).units[0];
  await page.route(`**/api/v1/document-analyses/${task_id}`, route => route.fulfill({ json: { ...result,
    chapters: [{ ...result.chapters[0], markdown: `## 循环条件 $n$ [来源：${late.label}](#document-unit-D23)\n\n介绍循环的条件。\n\n## 循环条件 $n$ [来源：${first.label}](#document-unit-D1)\n\n介绍循环的起点。` }] } }));
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.setViewportSize({ width: 375, height: 871 });
  await page.goto(`/#summary/${doc.id}`);
  const notes = page.locator("#document-notes");
  const preview = page.locator(".document-preview details");
  await expect(preview).not.toHaveAttribute("open");
  await expect(notes.locator(".cite-button")).toHaveCount(0);
  const links = notes.locator(".note-markdown h2 a");
  await expect(links).toHaveCount(2);
  await expect(links.first()).toHaveText(`来源：${late.label}`);
  await links.first().click();
  await expect(preview).toHaveAttribute("open");
  await expect(page.locator("#document-unit-D23")).toBeFocused();
  await expect(page.locator(".document-unit").first()).toHaveAttribute("id", "document-unit-D21");
  expect(page.url()).toContain(`/#summary/${doc.id}`);
  await links.last().focus();
  await links.last().press("Enter");
  await expect(page.locator("#document-unit-D1")).toBeFocused();
  expect(page.url()).toContain(`/#summary/${doc.id}`);
  expect((await (await request.get(`/api/v1/note-marks/document/${task_id}`)).json()).marks).toEqual([]);
  const marks = notes.getByRole("button", { name: "标为重点", exact: true });
  await expect(marks).toHaveCount(2);
  await marks.first().click();
  await expect(notes.getByRole("button", { name: "取消重点", exact: true })).toHaveCount(1);
  expect((await (await request.get(`/api/v1/note-marks/document/${task_id}`)).json()).marks)
    .toEqual([{ chapter_id: "S1", heading_index: 1 }]);
  for (const theme of ["light", "dark"]) {
    await page.evaluate(value => document.documentElement.setAttribute("data-theme", value), theme);
    await notes.screenshot({ path: testInfo.outputPath(`concise-heading-sources-${theme}.png`), animations: "disabled" });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  }
});

test("文档草稿追加保留已有章节、来源标签与阅读位置", async ({ page, request }) => {
  const doc = await upload(request, "草稿追加.txt", "前后章节均有原文依据。\n".repeat(400));
  const { task_id } = await (await request.post(`/api/v1/documents/${doc.id}/analyses`)).json();
  await expect.poll(async () => (await (await request.get(`/api/v1/document-tasks/${task_id}`)).json()).status).toBe("succeeded");
  const saved = await (await request.get(`/api/v1/document-tasks/${task_id}`)).json();
  const notes = await (await request.get(`/api/v1/document-analyses/${task_id}`)).json();
  let revision = 1;
  const current = () => ({ ...saved, status: "running", notes_state: "partial", notes_revision: revision });
  await page.route("**/api/v1/documents", route => route.fulfill({ json: [{ ...doc, latest_task: current() }] }));
  await page.route(`**/document-tasks/${task_id}`, route => route.fulfill({ json: current() }));
  await page.route(`**/document-analyses/${task_id}`, route => route.fulfill({ json: {
    ...notes, complete: false, notes_revision: revision, chapters: notes.chapters.slice(0, revision),
    completed_segments: revision, total_segments: 2,
  } }));
  await page.goto(`/#summary/${doc.id}`);
  const body = page.locator("#document-notes");
  await expect(body.locator(".summary-section")).toHaveCount(1);
  await expect(body.locator(".note-markdown strong")).toHaveText("主要结论");
  await expect(body.locator(".cite-button, .document-sources")).toHaveCount(0);
  const sourceLabel = body.locator(".document-source-label").first();
  const sourceText = await sourceLabel.textContent();
  const chapter = body.locator(".summary-section").first();
  await chapter.evaluate(el => { el.dataset.retained = "yes"; });
  await chapter.scrollIntoViewIfNeeded();
  const top = await page.evaluate(() => scrollY);
  revision = 2;
  await expect(body.locator(".summary-section")).toHaveCount(2);
  await expect(chapter).toHaveAttribute("data-retained", "yes");
  await expect(sourceLabel).toHaveText(sourceText!);
  expect(Math.abs(await page.evaluate(() => scrollY) - top)).toBeLessThan(8);
});
