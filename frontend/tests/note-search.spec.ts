import { settingsRead } from "./support/settings";
import { test, expect, type Page, type Route } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { taskResponse } from "./support/taskContract";

function gate() {
  let release!: () => void;
  const promise = new Promise<void>(resolve => { release = resolve; });
  return { promise, release };
}

async function mockSearch(page: Page, style = "detailed") {
  const task = (kind: string, id: string, status = "succeeded") => taskResponse({ id, [`${kind}_id`]: kind,
    status, stage: status === "succeeded" ? "completed" : "summarizing", note_style: style,
    notes_state: status === "succeeded" ? "ready" : "partial", notes_revision: 1,
    config: {}, warnings: [], timings: {}, error: null, completed_at: "2026-10-24T00:00:00Z",
    request_count: 0, request_limit: 80 });
  const saved = { video: task("video", "video-saved"), document: task("document", "document-saved") };
  const draft = { video: task("video", "video-draft", "paused"), document: task("document", "document-draft", "paused") };
  const notes = (kind: string, id: string) => ({ id, [`${kind}_id`]: kind, format_version: kind === "video" ? 4 : 1,
    kind, complete: true, total_segments: 1, completed_segments: 1, title: "生物化学笔记", tags: [], note_style: style,
    overview_paragraphs: ["酶的课程概览"], notes_revision: 1, evidence_ids: ["E1"], source_issues: [],
    chapters: [1, 2, 3].map(index => ({ chapter_id: `C${index}`, title: `第${index}章催化作用`, summary: "章节摘要",
      markdown: `## 第${index}节\n\n` + "酶的催化与抑制原理。".repeat(45), start_seconds: index * 5, end_seconds: index * 5 + 5,
      timestamp_basis: "speech", evidence_ids: ["E1"], source_ids: ["D1"], source_labels: ["第1页"], source_spans: [] })) });
  const hit = (kind: string, chapter: string | null = "C2") => ({ kind, file_id: kind, filename: `${kind}.txt`,
    filename_highlights: [] as number[][],
    task_id: saved[kind as keyof typeof saved].id, note_title: "生物化学笔记", area: chapter ? "chapter" : "overview",
    chapter_id: chapter, title: chapter ? "第2章催化作用" : "生物化学笔记", title_highlights: [],
    snippet: "酶的催化与抑制原理", snippet_highlights: [[0, 1], [5, 7]] });
  const state = {
    saved, draft, hit, notes, searchReads: [] as string[], resultReads: [] as string[], unexpected: [] as string[], writes: [] as string[],
    failSearch: false, failVersions: false, missing: false, failBody: false,
    holdSearch: null as ReturnType<typeof gate> | null, holdVersions: null as ReturnType<typeof gate> | null,
    holdBody: null as ReturnType<typeof gate> | null,
  };
  await page.route("**/api/v1/**", async (route: Route) => {
    const url = new URL(route.request().url());
    const path = url.pathname.replace("/api/v1", "");
    if (route.request().method() !== "GET") state.writes.push(path);
    let body: unknown;
    if (path === "/health") body = { model_configured: true, max_upload_bytes: 2048 ** 3, supported_video_extensions: ["mp4"] };
    else if (path === "/videos") body = [{ id: "video", kind: "video", filename: "video.mp4", duration: 120, size_bytes: 100, pinned: false,
      created_at: "2026-10-24T00:00:00Z", latest_task: draft.video }];
    else if (path === "/documents") body = [{ id: "document", kind: "document", filename: "document.txt", format: "txt", size_bytes: 100,
      unit_count: 1, character_count: 10, pinned: false, created_at: "2026-10-24T00:00:00Z", latest_task: draft.document }];
    else if (route.request().method() === "GET" && /^\/note-marks\/(video|document)\/[^/]+$/.test(path)) body = { marks: [] };
    else if (path === "/notes/search") {
      const q = url.searchParams.get("q") || "";
      state.searchReads.push(q);
      const waiting = state.holdSearch;
      if (waiting) { state.holdSearch = null; await waiting.promise; }
      if (state.failSearch) { state.failSearch = false; await route.fulfill({ status: 503, json: { error: { message: "搜索读取失败", error_id: "search-failure" } } }); return; }
      body = { items: q === "空" ? [] : url.searchParams.get("page") === "2" ? [state.hit("document")] : [state.hit("video"), state.hit("document")],
        total: q === "空" ? 0 : 21, page: Number(url.searchParams.get("page")), page_size: 20 };
    } else if (/\/(videos|documents)\/[^/]+\/analyses$/.test(path)) {
      if (state.holdVersions) await state.holdVersions.promise;
      if (state.failVersions) { state.failVersions = false; await route.fulfill({ status: 503, json: { error: { message: "版本读取失败", error_id: "versions-failure" } } }); return; }
      body = state.missing ? [] : [path.startsWith("/videos") ? saved.video : saved.document];
    } else if (/^\/(analyses|document-analyses)\//.test(path)) {
      state.resultReads.push(path);
      if (state.holdBody) await state.holdBody.promise;
      if (state.failBody) { state.failBody = false; await route.fulfill({ status: 503, json: { error: { message: "正文读取失败", error_id: "body-failure" } } }); return; }
      const kind = path.startsWith("/analyses/") ? "video" : "document";
      body = notes(kind, path.split("/").at(-1)!);
    } else if (path.endsWith("/evidence")) body = [{ id: "E1", kind: "speech", text: "原始依据", start_seconds: 5, end_seconds: 10 }];
    else if (path === "/tasks/video-draft") body = draft.video;
    else if (path === "/document-tasks/document-draft") body = draft.document;
    else if (path.endsWith("/playback")) body = { status: "failed", direct: false, error: null };
    else if (path.endsWith("/content")) body = { units: [{ id: "D1", label: "第1页", text: "原文内容", format: "text" }], total: 1, offset: 0 };
    else if (await settingsRead(route)) return;
    else { state.unexpected.push(path); await route.fulfill({ status: 404, json: { error: { message: "意外请求", error_id: "unexpected" } } }); return; }
    await route.fulfill({ json: body }).catch(() => {});
  });
  return state;
}

test.beforeAll(async ({ request }) => {
  expect((await (await request.get("/__test__/identity")).json()).fake_services_used).toBe(true);
});

test("搜索提交、高亮、分页、URL恢复、空结果和失败重试", async ({ page }) => {
  const state = await mockSearch(page);
  await page.goto("/#notes");
  const input = page.getByRole("searchbox", { name: "搜索笔记" });
  await input.fill("酶 抑制");
  expect(state.searchReads).toEqual([]);
  await input.press("Enter");
  await expect(page.getByRole("status")).toContainText("找到 21 处");
  await expect(page.locator(".note-search-group")).toHaveCount(2);
  await expect(page.locator(".note-search-hit mark")).toHaveText(["酶", "抑制", "酶", "抑制"]);
  await page.getByRole("button", { name: "下一页" }).click();
  await expect(page).toHaveURL(/page=2/);
  await expect(page.locator(".note-search-group")).toHaveCount(1);
  await page.reload();
  await expect(input).toHaveValue("酶 抑制");
  await expect(page.locator(".note-search-group")).toHaveCount(1);
  state.failSearch = true;
  await page.getByRole("button", { name: "搜索", exact: true }).click();
  await expect(page.locator(".note-search-state").getByText("搜索读取失败", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "重新搜索" }).click();
  await expect(page.locator(".note-search-group")).toHaveCount(2);
  await input.fill("空"); await input.press("Enter");
  await expect(page.getByRole("status")).toContainText("没有找到相关内容");
  expect(state.unexpected).toEqual([]); expect(state.writes).toEqual([]);
});

test("新搜索取消旧请求，迟到结果不能覆盖，离开后不更新", async ({ page }) => {
  const state = await mockSearch(page);
  const waiting = gate(); state.holdSearch = waiting;
  await page.goto("/#notes?q=旧查询&page=1");
  await expect.poll(() => state.searchReads.length).toBe(1);
  const input = page.getByRole("searchbox", { name: "搜索笔记" });
  await input.fill("空"); await input.press("Enter");
  await expect(page.getByRole("status")).toContainText("没有找到相关内容");
  waiting.release();
  await expect(page.locator(".note-search-group")).toHaveCount(0);
  await page.goBack();
  await expect(input).toHaveValue("旧查询");
  await expect(page.locator(".note-search-group")).toHaveCount(2);
  expect(state.unexpected).toEqual([]);
});

for (const kind of ["video", "document"] as const) for (const style of ["concise", "detailed"]) {
  test(`${kind}/${style} 固定成功版本，等待列表及正文后定位，只定位一次`, async ({ page }) => {
    const state = await mockSearch(page, style);
    const versions = gate(); state.holdVersions = versions;
    const body = gate(); state.holdBody = body;
    await page.goto("/#notes?q=酶&page=1");
    await page.locator(".note-search-group").filter({ hasText: `${kind}.txt` }).getByRole("link").click();
    await expect(page.getByText("正在读取版本列表…")).toBeVisible();
    expect(state.resultReads).toEqual([]);
    versions.release();
    await expect(page.getByText("正在读取笔记…")).toBeVisible();
    body.release();
    const anchor = kind === "video" ? "note-block-C2" : "document-note-C2";
    const element = page.locator(`#${anchor}`);
    await expect(element).toBeFocused();
    await expect(page.locator('.note-version-actions button[aria-pressed="true"]')).toHaveText("最新笔记");
    expect(state.resultReads).toEqual([kind === "video" ? "/analyses/video-saved" : "/document-analyses/document-saved"]);
    await element.evaluate(node => { (node as any).keptForSearch = true; });
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.evaluate(() => { location.hash = "settings"; });
    await page.getByRole("button", { name: "关闭设置", exact: true }).click();
    await expect(element).toBeAttached();
    expect(await element.evaluate(node => (node as any).keptForSearch)).toBe(true);
    expect(await page.evaluate(() => scrollY)).toBe(0);
    await page.reload(); await expect(page.locator(`#${anchor}`)).toBeFocused();
    await page.getByRole("link", { name: "返回笔记中心", exact: false }).click();
    await expect(page.getByRole("searchbox")).toHaveValue("酶");
    expect(state.unexpected).toEqual([]); expect(state.writes).toEqual([]);
  });
}

for (const kind of ["video", "document"]) {
  test(`${kind} 搜索指定版本在列表刷新和失败时保留正文节点与位置`, async ({ page }) => {
    const state = await mockSearch(page);
    const task = state.draft[kind as keyof typeof state.draft];
    task.status = "running";
    await page.clock.install();
    await page.goto(`/#summary/${kind}?task=${kind}-saved&chapter=C2&q=酶&page=1`);
    const anchor = kind === "video" ? "note-block-C2" : "document-note-C2";
    const element = page.locator(`#${anchor}`);
    await page.clock.runFor(30);
    await expect(element).toBeFocused();
    await element.evaluate(node => { (node as any).keptForSearch = true; });
    await page.evaluate(() => scrollBy(0, 150));
    const top = await element.evaluate(node => node.getBoundingClientRect().top);
    const waiting = gate(); state.holdVersions = waiting; state.failVersions = true;
    task.status = "succeeded";
    await page.clock.runFor(2200);
    await expect(page.getByText("正在读取版本列表…")).toBeVisible();
    expect(await element.evaluate(node => (node as any).keptForSearch)).toBe(true);
    waiting.release();
    await expect(page.getByRole("button", { name: "重新读取版本列表" })).toBeVisible();
    expect(await element.evaluate(node => (node as any).keptForSearch)).toBe(true);
    // Scroll offsets are whole pixels while layout coordinates include subpixels.
    expect(Math.abs(await element.evaluate(node => node.getBoundingClientRect().top) - top)).toBeLessThan(1);
    expect(state.resultReads.filter(path => path.endsWith(`${kind}-saved`))).toHaveLength(1);
    expect(state.unexpected).toEqual([]);
  });

  test(`${kind} 列表和正文独立重试，失效版本不退到草稿`, async ({ page }) => {
    const state = await mockSearch(page);
    state.failVersions = true;
    await page.goto(`/#summary/${kind}?task=${kind}-saved&chapter=C2&q=酶&page=1`);
    state.failBody = true;
    await page.getByRole("button", { name: "重新读取版本列表" }).click();
    await page.getByRole("button", { name: "重新读取笔记", exact: true }).click();
    const anchor = kind === "video" ? "note-block-C2" : "document-note-C2";
    await expect(page.locator(`#${anchor}`)).toBeFocused();
    state.missing = true;
    await page.reload();
    await expect(page.getByText(/搜索命中的笔记版本或章节已不可用/)).toBeVisible();
    await expect(page.locator(".summary-section")).toHaveCount(0);
    expect(state.resultReads.some(path => path.includes("draft"))).toBe(false);
    expect(state.unexpected).toEqual([]);
  });
}

test("概览、缺失章节、同页新目标与手动版本切换不会错误定位", async ({ page }) => {
  const state = await mockSearch(page);
  await page.goto("/#summary/document?task=document-saved&q=酶&page=1");
  await expect(page.locator("#document-notes")).toBeFocused();
  await page.evaluate(() => { location.hash = "summary/document?task=document-saved&chapter=C3&q=酶&page=1"; });
  await expect(page.locator("#document-note-C3")).toBeFocused();
  await page.getByRole("button", { name: "回到生成", exact: true }).click();
  await expect.poll(() => state.resultReads.some(path => path.includes("document-draft"))).toBe(true);
  await page.evaluate(() => { location.hash = "summary/document?task=document-saved&chapter=missing&q=酶&page=1"; });
  await expect(page.getByText(/搜索命中的笔记版本或章节已不可用/)).toBeVisible();
  expect(state.unexpected).toEqual([]);
});

test("资料已删除可返回搜索，结果中的HTML始终按文字显示", async ({ page }) => {
  const state = await mockSearch(page);
  const hit = state.hit;
  state.hit = (kind, chapter) => ({ ...hit(kind, chapter), title: "<b>章节标题</b>",
    snippet: '<img src=x onerror="alert(1)">原文', snippet_highlights: [] });
  await page.goto("/#notes?q=酶&page=1");
  await expect(page.locator(".note-search-hit h3").first()).toHaveText("<b>章节标题</b>");
  await expect(page.locator(".note-search-hit img, .note-search-hit b")).toHaveCount(0);
  await page.route("**/api/v1/videos", route => route.fulfill({ json: [] }));
  await page.route("**/api/v1/documents", route => route.fulfill({ json: [] }));
  await page.goto("/#summary/video?task=video-saved&chapter=C2&q=酶&page=1");
  await page.reload();
  await expect(page.getByText("文件不存在或已被删除。", { exact: true })).toBeVisible();
  await page.getByRole("link", { name: "返回笔记中心", exact: false }).click();
  await expect(page.getByRole("searchbox")).toHaveValue("酶");
});

test("搜索页按需资源、窄屏和主题，进入正文确实加载渲染资源", async ({ page }) => {
  await mockSearch(page);
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
  const resources: string[] = [];
  page.on("request", request => resources.push(request.url()));
  const isHeavy = (url: string) => [...heavy].some(file => url.endsWith("/" + file));
  await page.goto("/#notes?q=酶&page=1");
  await expect(page.locator(".note-search-hit")).toHaveCount(2);
  for (const width of [1280, 375]) for (const theme of ["light", "dark"]) {
    await page.setViewportSize({ width, height: 871 });
    await page.evaluate(theme => { document.documentElement.dataset.theme = theme; }, theme);
    for (const expanded of [false, true]) {
      if (width > 700) {
        const collapsed = await page.locator(".shell").evaluate(node => node.classList.contains("sidebar-collapsed"));
        if (expanded === collapsed) await page.getByRole("button", { name: expanded ? "展开侧边栏" : "折叠侧边栏", exact: true }).click();
      } else {
        const open = await page.locator(".shell").evaluate(node => node.classList.contains("sidebar-open"));
        if (expanded !== open) {
          if (expanded) await page.locator(".sidebar-toggle").click();
          else await page.locator(".sidebar-backdrop").click({ position: { x: width - 12, y: 100 } });
        }
        await expect.poll(() => page.locator(".sidebar").evaluate(node => Math.round(node.getBoundingClientRect().x)))
          .toBe(expanded ? 0 : -260);
      }
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await expect(page.getByRole("searchbox")).toBeAttached();
      const ratio = await page.locator(".note-search mark").first().evaluate(node => {
        const style = getComputedStyle(node);
        const luminance = (color: string) => color.match(/[\d.]+/g)!.slice(0, 3).map(Number)
          .map(value => value / 255).map(value => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4)
          .reduce((sum, value, index) => sum + value * [0.2126, 0.7152, 0.0722][index], 0);
        const a = luminance(style.color), b = luminance(style.backgroundColor);
        return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
      });
      expect(ratio).toBeGreaterThanOrEqual(4.5);
      await page.screenshot({ path: test.info().outputPath(`search-${width}-${theme}-${expanded}.png`) });
    }
  }
  expect(resources.filter(isHeavy)).toEqual([]);
  if (await page.locator(".shell").evaluate(node => node.classList.contains("sidebar-open"))) {
    await page.locator(".sidebar-backdrop").click({ position: { x: 363, y: 100 } });
  }
  await page.locator(".note-search-hit a").first().click();
  await expect(page.locator(".note-markdown").first()).toBeVisible();
  expect(resources.some(isHeavy)).toBe(true);
});

test("隔离笔记真实搜索接口与来源定位闭环", async ({ page, request }) => {
  expect((await request.put("/api/v1/settings/model", { data: { token: "fake-search-integration", stream: false } })).ok()).toBe(true);
  const name = `搜索闭环-${test.info().project.name}.txt`;
  const uploaded = await request.post("/api/v1/documents", { multipart: {
    file: { name, mimeType: "text/plain", buffer: Buffer.from("概念的适用条件和边界。" + test.info().testId) },
  } });
  expect(uploaded.ok()).toBe(true);
  const doc = await uploaded.json();
  try {
    const created = await request.post(`/api/v1/documents/${doc.id}/analyses`, { data: { note_style: "detailed" } });
    expect(created.ok()).toBe(true);
    const { task_id: taskId } = await created.json();
    await expect.poll(async () => (await (await request.get(`/api/v1/document-tasks/${taskId}`)).json()).status).toBe("succeeded");
    const writes: string[] = [];
    page.on("request", request => { if (request.method() !== "GET") writes.push(request.url()); });
    await page.goto(`/#notes?q=${encodeURIComponent(name)}&page=1`);
    const filenameGroup = page.locator(".note-search-group").filter({ hasText: name });
    await expect(filenameGroup.locator("h2 mark")).toHaveText(name);
    await expect(filenameGroup.locator(".note-search-hit")).toHaveCount(1);
    await filenameGroup.getByRole("link").click();
    await expect(page.locator("#document-notes")).toBeFocused();
    await page.goto("/#notes?q=解释+概念&page=1");
    const group = page.locator(".note-search-group").filter({ hasText: name });
    await expect(group).toBeVisible();
    await group.getByRole("link").first().click();
    await expect(page.locator("#document-note-C1")).toBeFocused();
    await expect(page.getByRole("link", { name: "下载 Markdown", exact: true })).toHaveAttribute("href", new RegExp(taskId));
    await expect(page.locator("#document-notes .cite-button, #document-notes .document-sources")).toHaveCount(0);
    await expect(page.locator("#document-note-C1 .document-source-label")).toContainText("来源：");
    await page.locator(".document-preview summary").click();
    await expect(page.locator("#document-unit-D1")).toBeVisible();
    expect(writes).toEqual([]);
  } finally {
    expect((await request.delete(`/api/v1/documents/${doc.id}`)).ok()).toBe(true);
  }
});
