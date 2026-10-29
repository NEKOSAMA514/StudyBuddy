import { test, expect, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

test.beforeEach(async ({ request }) => {
  expect((await (await request.get("/__test__/identity")).json()).fake_services_used).toBe(true);
  await request.put("/api/v1/settings/model", { data: { token: "fake-style-browser", input_text_limit: 25000, stream: false } });
  await request.put("/api/v1/settings/note-style", { data: { note_style: "detailed", summary_prompt: "" } });
});

async function screenshots(page: Page, name: string) {
  for (const width of [1280, 375]) {
    await page.setViewportSize({ width, height: 871 });
    for (const theme of ["light", "dark"]) {
      await page.locator("html").evaluate((el, value) => el.setAttribute("data-theme", value), theme);
      await page.screenshot({ path: test.info().outputPath(`${name}-${width}-${theme}.png`), animations: "disabled" });
      expect(await page.locator("body").evaluate(el => el.scrollWidth <= window.innerWidth)).toBe(true);
    }
  }
}

for (const kind of ["video", "document"] as const) {
  test(`${kind}简洁与详尽共用阅读层级，宽窄明暗下保持正文排版`, async ({ page, request }) => {
    const video = kind === "video";
    const files = video ? "videos" : "documents";
    const tasks = video ? "tasks" : "document-tasks";
    const analyses = video ? "analyses" : "document-analyses";
    const uploaded = await request.post(`/api/v1/${files}`, { multipart: { file: {
      name: video ? "阅读层级.mp4" : "阅读层级.txt", mimeType: video ? "video/mp4" : "text/plain",
      // Layout checks need a saved note, without the 120-second fixture's 60 delayed OCR calls.
      buffer: video ? Buffer.concat([readFileSync(join(process.env.E2E_MEDIA_DIR!, "sample-vp9.mp4")), Buffer.from(`typography-${test.info().project.name}`)])
        : Buffer.from(`原理、例题和适用条件。${test.info().project.name}`),
    } } });
    const file = await uploaded.json();
    for (const style of ["concise", "detailed"]) {
      const created = await (await request.post(`/api/v1/${files}/${file.id}/analyses`, { data: { note_style: style } })).json();
      await expect.poll(async () => (await (await request.get(`/api/v1/${tasks}/${created.task_id}`)).json()).status).toBe("succeeded");
      await page.route(`**/api/v1/${analyses}/${created.task_id}`, async route => {
        const response = await route.fetch();
        const result = await response.json();
        result.title = "循环结构：概念与应用";
        const time = video && style === "concise" ? " [00:00](#video-time-0)" : "";
        result.chapters[0].markdown = `## 基本概念与适用条件${time}\n\n先理解条件判断，再观察循环体中的变量如何变化。\n\n### 关键步骤${time}\n\n1. 初始化变量。\n2. 判断条件并更新变量。\n\n## 示例与边界${time}\n\n保留例题中的关键过程及必要代码。\n\n\`\`\`python\nwhile ready:\n    update()\n\`\`\``;
        result.chapters.push({ ...result.chapters[0], chapter_id: style === "concise" ? "S2" : "C2",
          title: "后续章节", markdown: `## 后续小节${time}\n\n核对边界条件。` });
        result.total_segments = result.completed_segments = 2;
        await route.fulfill({ response, json: result });
      });
    }
    await page.goto(`/#summary/${file.id}`);
    const root = page.locator(video ? "#video-notes" : "#document-notes");
    const typography = (element: Element) => {
      const style = getComputedStyle(element);
      return { size: style.fontSize, weight: style.fontWeight, lineHeight: style.lineHeight,
        marginBottom: style.marginBottom, color: style.color };
    };
    for (const width of [1280, 375]) {
      await page.setViewportSize({ width, height: 871 });
      for (const theme of ["light", "dark"]) {
        await page.locator("html").evaluate((el, value) => el.setAttribute("data-theme", value), theme);
        await root.getByRole("button", { name: "最新笔记", exact: true }).click();
        await expect(root).toHaveAttribute("data-note-style", "detailed");
        await expect(root.locator(".note-markdown h2").first()).toBeVisible();
        const chapter = await root.locator(".summary-section > .note-heading > h3").first().evaluate(typography);
        const subtopic = await root.locator(".note-markdown h2").first().evaluate(typography);
        const lowerHeading = await root.locator(".note-markdown h3").first().evaluate(typography);
        const title = await root.locator(".overview h3").first().evaluate(typography);
        const paragraph = await root.locator(".note-markdown p").first().evaluate(typography);
        for (const [actual, expected] of [[chapter.size, 26], [subtopic.size, 22], [lowerHeading.size, 20], [title.size, 26], [paragraph.size, 16]] as const) {
          expect(Number.parseFloat(actual)).toBe(expected);
        }
        await root.getByRole("button", { name: "上一份笔记", exact: true }).click();
        await expect(root).toHaveAttribute("data-note-style", "concise");
        await expect(root.locator(".note-markdown h2")).toHaveCount(3);
        expect(await root.locator(".summary-section > .note-heading > h3").first().evaluate(typography)).toEqual(chapter);
        expect(await root.locator(".note-markdown h2").first().evaluate(typography)).toEqual(subtopic);
        expect(await root.locator(".note-markdown h3").first().evaluate(typography)).toEqual(lowerHeading);
        expect(await root.locator(".note-title").evaluate(typography)).toEqual(title);
        expect(await root.locator(".note-markdown p").first().evaluate(typography)).toEqual(paragraph);
        await expect(root.locator(".note-markdown .note-heading").nth(1)).toHaveCSS("border-top-width", "3px");
        await expect(root.locator(".cite-button, .overview, .chapter-navigator")).toHaveCount(0);
        await expect(root.locator(".note-markdown code")).toContainText("while ready:");
        await expect(root.locator(".note-code-block pre")).toHaveCSS("font-size", "16px");
        await expect(root.locator(".note-code-toolbar")).toHaveCSS("font-size", "14px");
        await expect(root.locator(".note-version-actions button").first()).toHaveCSS("font-size", "14px");
        const alignment = await root.evaluate(el => {
          const title = el.querySelector(".note-title")!.getBoundingClientRect();
          const body = el.querySelector(".timeline")!.getBoundingClientRect();
          return Math.abs(title.left - body.left);
        });
        expect(alignment).toBeLessThan(1);
        expect(await page.locator("body").evaluate(el => el.scrollWidth <= innerWidth)).toBe(true);
        await root.scrollIntoViewIfNeeded();
        await root.screenshot({ path: test.info().outputPath(`reading-${kind}-${width}-${theme}.png`), animations: "disabled" });
      }
    }
  });
}

test("风格默认值保存、清空补充要求保留选择，统一输入仅一个字段", async ({ page, request }) => {
  await page.goto("/#settings");
  await page.getByRole("tab", { name: "笔记风格", exact: true }).click();
  await expect(page.getByLabel("默认笔记风格")).toHaveValue("detailed");
  await page.getByLabel("默认笔记风格").selectOption("concise");
  await page.getByLabel("笔记补充要求（选填）").fill("突出重点");
  await page.getByRole("button", { name: "清空补充要求", exact: true }).click();
  await expect(page.getByLabel("默认笔记风格")).toHaveValue("concise");
  await page.getByRole("button", { name: "保存笔记风格", exact: true }).click();
  await expect.poll(async () => (await (await request.get("/api/v1/settings/note-style")).json()).note_style).toBe("concise");
  await screenshots(page, "style");
  await page.reload();
  await page.getByRole("tab", { name: "笔记风格", exact: true }).click();
  await expect(page.getByLabel("默认笔记风格")).toHaveValue("concise");
  await page.getByRole("tab", { name: "API设置", exact: true }).click();
  await page.getByText("高级设置", { exact: true }).click();
  await expect(page.getByLabel("生成输入上限（字符）", { exact: true })).toHaveCount(1);
  await expect(page.getByLabel("章节规划输入上限", { exact: true })).toHaveCount(0);
  await page.getByLabel("生成输入上限（字符）", { exact: true }).fill("18000");
  await page.getByRole("button", { name: "保存配置", exact: true }).click();
  await expect.poll(async () => (await (await request.get("/api/v1/settings/model")).json()).input_text_limit).toBe(18000);
});

test("文档本次简洁覆盖、短文下载与详尽版本切换", async ({ page, request }) => {
  const upload = await request.post("/api/v1/documents", { multipart: { file: { name: `风格课程-${test.info().project.name}.txt`,
    mimeType: "text/plain", buffer: Buffer.from(`概念需要满足适用条件。\n\n首先理解原理，再核对边界。\n测试组：${test.info().project.name}`) } } });
  const doc = await upload.json();
  await page.goto(`/#summary/${doc.id}`);
  await page.getByRole("button", { name: "生成笔记", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "生成文档笔记" });
  await expect(dialog.getByLabel("笔记风格")).toHaveValue("detailed");
  await dialog.getByLabel("笔记风格").selectOption("concise");
  await expect(dialog.getByText(/预计 1 段/)).toBeVisible();
  const created = page.waitForResponse(r => r.url().endsWith(`/documents/${doc.id}/analyses`) && r.request().method() === "POST");
  await dialog.getByRole("button", { name: "开始生成", exact: true }).click();
  const { task_id } = await (await created).json();
  await expect(page.locator("#document-notes .note-markdown")).toContainText("主要结论");
  await expect(page.locator("#document-notes").getByRole("heading", { name: "简洁文档重点", exact: true })).toHaveCount(1);
  await expect(page.locator("#document-notes .chapter-navigator")).toHaveCount(0);
  await expect(page.locator("#document-notes .cite-button")).toHaveCount(0);
  await expect(page.locator("#document-notes .document-sources")).toHaveCount(0);
  const md = await request.get(`/api/v1/document-analyses/${task_id}/notes?format=md`);
  expect((await md.text()).split("简洁文档重点").length - 1).toBe(1);
  expect((await (await request.get("/api/v1/settings/note-style")).json()).note_style).toBe("detailed");
  await page.locator("#document-notes").scrollIntoViewIfNeeded();
  await screenshots(page, "concise-document");
  const detailed = await (await request.post(`/api/v1/documents/${doc.id}/analyses`, { data: { note_style: "detailed" } })).json();
  await expect.poll(async () => (await (await request.get(`/api/v1/document-tasks/${detailed.task_id}`)).json()).status).toBe("succeeded");
  await page.reload();
  await expect(page.locator("#document-notes .overview h3")).toHaveText("文档主题总结");
  await expect(page.locator("#document-notes .cite-button, #document-notes .document-sources")).toHaveCount(0);
  await expect(page.locator("#document-notes .document-source-label").first()).toContainText("来源：");
  await page.locator(".document-preview summary").click();
  await expect(page.locator(".document-unit").first()).toContainText("适用条件");
  await page.getByRole("button", { name: "上一份笔记", exact: true }).click();
  await expect(page.locator("#document-notes .note-markdown")).toContainText("主要结论");
  await expect(page.locator("#document-notes .overview")).toHaveCount(0);
  await expect(page.locator("#document-notes .cite-button")).toHaveCount(0);
  await expect(page.locator("#document-notes .document-sources")).toHaveCount(0);
});

test("简洁视频隐藏依据，正文时间支持点击和键盘，详尽版本保留依据", async ({ page, request }) => {
  const raw = Buffer.concat([readFileSync(process.env.E2E_VIDEO_PATH!), Buffer.from(`note-styles-${test.info().project.name}`)]);
  const uploaded = await request.post("/api/v1/videos", { multipart: { file: { name: "风格视频.mp4", mimeType: "video/mp4", buffer: raw } } });
  const video = await uploaded.json();
  await page.goto(`/#summary/${video.id}`);
  await page.getByRole("button", { name: "开始处理", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "选择处理模型" });
  await expect(dialog.getByLabel("笔记风格")).toHaveValue("detailed");
  await dialog.getByLabel("笔记风格").selectOption("concise");
  const created = page.waitForResponse(r => r.url().endsWith(`/videos/${video.id}/analyses`) && r.request().method() === "POST");
  await dialog.getByRole("button", { name: "开始处理", exact: true }).click();
  const { task_id } = await (await created).json();
  await expect(page.locator("#video-notes .note-markdown")).toContainText("核心结论", { timeout: 45000 });
  await expect(page.locator("#video-notes").getByRole("heading", { name: "简洁笔记重点", exact: true })).toHaveCount(1);
  await expect(page.locator("#video-notes .video-overview")).toHaveCount(0);
  await expect(page.locator("#video-notes .cite-button")).toHaveCount(0);
  await expect(page.locator("#video-notes .evidence-table")).toHaveCount(0);
  const time = page.locator('#video-notes .note-markdown h2 a[href="#video-time-0"]');
  await expect(time).toHaveText("00:00");
  await expect(page.locator('#video-notes .note-markdown p a[href^="#video-time-"]')).toHaveCount(0);
  const player = page.locator("video");
  await expect.poll(() => player.evaluate((el: HTMLVideoElement) => el.readyState)).toBeGreaterThanOrEqual(1);
  await player.evaluate((el: HTMLVideoElement) => { el.pause(); el.currentTime = 1; });
  const url = page.url();
  await time.click();
  await expect.poll(() => player.evaluate((el: HTMLVideoElement) => el.currentTime)).toBeLessThan(0.1);
  expect(page.url()).toBe(url);
  await player.evaluate((el: HTMLVideoElement) => { el.currentTime = 1; });
  await time.focus();
  await page.keyboard.press("Enter");
  await expect.poll(() => player.evaluate((el: HTMLVideoElement) => el.currentTime)).toBeLessThan(0.1);
  await page.locator("#video-notes").scrollIntoViewIfNeeded();
  await screenshots(page, "concise-video-time");
  const result = await (await request.get(`/api/v1/analyses/${task_id}`)).json();
  expect(result.note_style).toBe("concise");
  expect(result.chapters[0].evidence_spans.length).toBeGreaterThan(0);
  expect(result.chapters[0].markdown).toContain("[00:00](#video-time-0)");
  for (const format of ["txt", "md"]) {
    const text = await (await request.get(`/api/v1/analyses/${task_id}/notes?format=${format}`)).text();
    expect(text).toContain("00:00");
    expect(text).not.toContain("#source-");
    expect(text).not.toContain("#video-time-");
  }
  await page.route(`**/api/v1/analyses/${task_id}`, async route => {
    const response = await route.fetch();
    const value = await response.json();
    value.chapters[0].markdown = "## 后讲的主题 [00:01](#video-time-1.25)\n\n解释概念。\n\n## 先讲的主题 [00:00](#video-time-0.25)\n\n核对边界。";
    await route.fulfill({ response, json: value });
  });
  await page.reload();
  const bodyTimes = page.locator('#video-notes .note-markdown h2 a');
  await expect(bodyTimes).toHaveText(["00:01", "00:00"]);
  await bodyTimes.first().click();
  await expect.poll(() => player.evaluate((el: HTMLVideoElement) => el.currentTime)).toBeCloseTo(1.25, 2);
  await bodyTimes.nth(1).click();
  await expect.poll(() => player.evaluate((el: HTMLVideoElement) => el.currentTime)).toBeCloseTo(0.25, 2);
  await page.unroute(`**/api/v1/analyses/${task_id}`);
  const detailed = await (await request.post(`/api/v1/videos/${video.id}/analyses`, { data: { note_style: "detailed" } })).json();
  await expect.poll(async () => (await (await request.get(`/api/v1/tasks/${detailed.task_id}`)).json()).status).toBe("succeeded");
  await page.reload();
  await expect(page.locator("#video-notes .cite-button")).toHaveText("查看本章依据");
  await page.locator("#video-notes .cite-button").click();
  await expect(page.locator("#video-notes")).toContainText("收起本章依据");
  await page.getByRole("button", { name: "上一份笔记", exact: true }).click();
  await expect(page.locator("#video-notes .cite-button")).toHaveCount(0);
  await expect(page.locator('#video-notes .note-markdown a[href="#video-time-0"]')).toHaveCount(1);
});

test("文档来源超出一行才可展开，宽窄屏切换不丢完整来源", async ({ page, request }) => {
  const uploaded = await request.post("/api/v1/documents", { multipart: { file: { name: "来源显示.txt",
    mimeType: "text/plain", buffer: Buffer.from(`来源显示 ${test.info().project.name}`) } } });
  const doc = await uploaded.json();
  const created = await (await request.post(`/api/v1/documents/${doc.id}/analyses`, { data: { note_style: "concise" } })).json();
  await expect.poll(async () => (await (await request.get(`/api/v1/document-tasks/${created.task_id}`)).json()).status).toBe("succeeded");
  await page.route(`**/api/v1/document-analyses/${created.task_id}`, async route => {
    const response = await route.fetch();
    const result = await response.json();
    const chapter = result.chapters[0];
    result.chapters = [
      { ...chapter, source_labels: Array.from({ length: 43 }, (_, i) => `幻灯片 ${i + 1}`) },
      { ...chapter, chapter_id: "S2", title: "短来源", source_labels: ["幻灯片 1"] },
      { ...chapter, chapter_id: "S3", title: "自适应来源", source_labels: Array.from({ length: 6 }, (_, i) => `幻灯片 ${i + 1}`) },
    ];
    result.total_segments = result.completed_segments = 3;
    await route.fulfill({ response, json: result });
  });
  await page.goto(`/#summary/${doc.id}`);
  const summaries = page.locator(".document-source-summary");
  const long = summaries.nth(0), short = summaries.nth(1), responsive = summaries.nth(2);
  await expect(long.getByRole("button", { name: "展开来源" })).toBeVisible();
  await expect(short.getByRole("button")).toHaveCount(0);
  await long.getByRole("button", { name: "展开来源" }).focus();
  await page.keyboard.press("Space");
  await expect(long.getByRole("button", { name: "收起来源" })).toHaveAttribute("aria-expanded", "true");
  expect(await long.locator("p").evaluate(el => el.clientHeight > Number.parseFloat(getComputedStyle(el).lineHeight) * 2)).toBe(true);
  await expect(long.locator("p")).toContainText("幻灯片 43");
  await long.getByRole("button", { name: "收起来源" }).click();
  for (const width of [1280, 375]) {
    await page.setViewportSize({ width, height: 871 });
    await expect(responsive.getByRole("button")).toHaveCount(width === 375 ? 1 : 0);
    for (const theme of ["light", "dark"]) {
      await page.locator("html").evaluate((el, value) => el.setAttribute("data-theme", value), theme);
      await long.scrollIntoViewIfNeeded();
      expect(await long.locator("p").evaluate(el => el.clientHeight <= Number.parseFloat(getComputedStyle(el).lineHeight) + 1)).toBe(true);
      expect(await page.locator("body").evaluate(el => el.scrollWidth <= window.innerWidth)).toBe(true);
      await page.screenshot({ path: test.info().outputPath(`source-collapsed-${width}-${theme}.png`), animations: "disabled" });
    }
  }
});

test("文档风格与API选择框的尺寸、边框和主题样式一致", async ({ page, request }) => {
  const uploaded = await request.post("/api/v1/documents", { multipart: { file: { name: "表单显示.txt",
    mimeType: "text/plain", buffer: Buffer.from(`表单显示 ${test.info().project.name}`) } } });
  const doc = await uploaded.json();
  await page.goto(`/#summary/${doc.id}`);
  await page.getByRole("button", { name: "生成笔记", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "生成文档笔记" });
  await expect(dialog.getByLabel("笔记风格")).toBeVisible();
  for (const width of [1280, 375]) {
    await page.setViewportSize({ width, height: 871 });
    for (const theme of ["light", "dark"]) {
      await page.locator("html").evaluate((el, value) => el.setAttribute("data-theme", value), theme);
      const styles = await dialog.locator("select").evaluateAll(elements => elements.map(el => {
        const style = getComputedStyle(el);
        return ["width", "height", "padding", "border", "border-radius", "background-color", "color", "font-size"].map(key => style.getPropertyValue(key));
      }));
      expect(styles).toHaveLength(2);
      expect(styles[1]).toEqual(styles[0]);
      expect(await dialog.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
      await page.screenshot({ path: test.info().outputPath(`document-selects-${width}-${theme}.png`), animations: "disabled" });
    }
  }
  await dialog.getByRole("button", { name: "取消", exact: true }).click();
});
