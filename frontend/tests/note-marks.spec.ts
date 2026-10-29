import { test, expect, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { taskResponse } from "./support/taskContract";

const cases: { name: string; markdown: string; count: number }[] = JSON.parse(readFileSync(resolve("../backend/tests/fixtures/note-mark-headings.json"), "utf8"));
function gate() {
  let release!: () => void;
  const promise = new Promise<void>(resolve => { release = resolve; });
  return { promise, release };
}

async function reader(page: Page, kind: "video" | "document", style: "concise" | "detailed", draft = false) {
  const task = (id: string, date: string) => taskResponse({ id, video_id: "file", document_id: "file", status: draft ? "paused" : "succeeded",
    stage: draft ? "summarizing" : "completed", notes_state: draft ? "partial" : "ready", notes_revision: 1,
    created_at: date, updated_at: date, completed_at: date, config: {}, timings: {}, warnings: [], error: null });
  const latest = task("latest", "2026-10-24T00:00:00Z"), previous = task("previous", "2026-10-23T00:00:00Z");
  const result = { format_version: kind === "video" ? 4 : 1, complete: !draft, note_style: style,
    title: "重点标记测试", tags: [], overview_paragraphs: ["概览不提供标记。"], source_issues: [], evidence_ids: ["E1"],
    total_segments: 1, completed_segments: 1, chapters: [1, 2].map(index => ({ chapter_id: `C${index}`, title: `第${index}章`,
      markdown: index === 1 ? cases.map(item => item.markdown).join("\n\n") + "\n\n脚注[^1]\n\n[^1]: 原文说明。" : "没有二级标题的正文。",
      source_ids: ["D1"], source_labels: ["原文1"], evidence_ids: ["E1"], start_seconds: 0, end_seconds: 30, timestamp_basis: "speech" })) };
  const file = { id: "file", kind, filename: "重点测试", duration: 120, size_bytes: 100, format: "md", unit_count: 1,
    character_count: 100, pinned: false, created_at: latest.created_at, latest_task: latest };
  const state = {
    marks: {} as Record<string, { chapter_id: string; heading_index: number }[]>,
    reads: [] as string[], writes: [] as { id: string; chapter_id: string; heading_index: number; marked: boolean }[],
    unexpected: [] as string[], failRead: false, failSave: false, failList: false, listReads: [] as number[],
    listGate: null as ReturnType<typeof gate> | null,
    readGate: null as ReturnType<typeof gate> | null, saveGate: null as ReturnType<typeof gate> | null,
  };
  await page.route("**/api/v1/**", async route => {
    const path = new URL(route.request().url()).pathname.replace("/api/v1", ""), method = route.request().method();
    let json: unknown;
    if (path === "/note-marks" && method === "GET") {
      const pageNumber = Number(new URL(route.request().url()).searchParams.get("page"));
      state.listReads.push(pageNumber);
      const titles = ["相同标题 00:05", "相同标题 00:10", "Setext小标题", "引用小标题", "动力学 $K_m$"];
      const items = [latest, previous].flatMap((task, version) => (state.marks[task.id] || []).map(mark => ({
        ...mark, kind, file_id: "file", filename: "重点测试", task_id: task.id, note_title: result.title, version: version + 1,
        chapter_title: "第1章", title: mark.heading_index ? titles[mark.heading_index - 1] : "第1章", snippet: "这部分的重点内容。",
      })));
      const held = state.listGate; state.listGate = null;
      if (held) await held.promise;
      if (state.failList) { state.failList = false; return route.fulfill({ status: 503, json: { error: { message: "重点列表读取失败", error_id: "mark-list" } } }); }
      json = { items: items.slice((pageNumber - 1) * 20, pageNumber * 20), total: items.length, page: pageNumber, page_size: 20 };
    } else if (path.startsWith("/note-marks/")) {
      const id = path.split("/").at(-1)!;
      if (method === "GET") {
        state.reads.push(id);
        const snapshot = [...(state.marks[id] || [])];
        const held = state.readGate; state.readGate = null;
        if (held) await held.promise;
        if (state.failRead) { state.failRead = false; return route.fulfill({ status: 503, json: { error: { message: "标记读取失败", error_id: "mark-read" } } }); }
        json = { marks: snapshot };
      } else if (method === "PUT") {
        const value = route.request().postDataJSON();
        state.writes.push({ id, ...value });
        const held = state.saveGate; state.saveGate = null;
        if (held) await held.promise;
        if (state.failSave) { state.failSave = false; return route.fulfill({ status: 503, json: { error: { message: "标记保存失败", error_id: "mark-save" } } }); }
        state.marks[id] = [...(state.marks[id] || []).filter(item => item.chapter_id !== value.chapter_id || item.heading_index !== value.heading_index),
          ...(value.marked ? [{ chapter_id: value.chapter_id, heading_index: value.heading_index }] : [])];
        json = value;
      } else throw new Error(`Unexpected mark method ${method}`);
    } else if (path === "/health") json = { model_configured: true, supported_video_extensions: ["mp4"], supported_document_extensions: ["md"] };
    else if (path === "/videos") json = kind === "video" ? [file] : [];
    else if (path === "/documents") json = kind === "document" ? [file] : [];
    else if (path.endsWith("/analyses")) json = draft ? [] : [latest, previous];
    else if (/^\/(analyses|document-analyses)\//.test(path)) json = result;
    else if (path.endsWith("/evidence")) json = [{ id: "E1", kind: "speech", text: "原始材料", start_seconds: 0, end_seconds: 30 }];
    else if (path.endsWith("/playback")) json = { status: "unprepared", direct: true, original_url: "/api/v1/videos/file/content", mime_type: "video/mp4", error: null };
    else if (path === "/videos/file/content") return route.fulfill({ contentType: "video/mp4", body: readFileSync(process.env.E2E_VIDEO_PATH!) });
    else if (path === "/documents/file/content") json = { units: [{ id: "D1", label: "原文1", format: "markdown", text: "## 原文二级标题\n\n原文。" }], total: 1, offset: 0 };
    else { state.unexpected.push(`${method} ${path}`); return route.fulfill({ status: 404, json: {} }); }
    await route.fulfill({ json }).catch(() => {});
  });
  return { state, result, open: () => page.goto("/#summary/file"), root: page.locator(`#${kind}-notes`) };
}

test.beforeEach(async ({ request }) => {
  expect((await (await request.get("/__test__/identity")).json()).fake_services_used).toBe(true);
});

for (const kind of ["video", "document"] as const) for (const style of ["concise", "detailed"] as const) {
  test(`${kind}/${style} 标记编号、保存、刷新、版本独立与阅读保持`, async ({ page }) => {
    const { state, open, root } = await reader(page, kind, style);
    await open();
    const controls = root.locator(".note-mark-button");
    await expect(controls).toHaveCount(style === "concise" ? cases.reduce((sum, item) => sum + item.count, 0) : 2);
    await expect(controls.first()).toBeEnabled();
    if (style === "concise") {
      await expect(root.locator(".note-heading h2")).toHaveCount(5);
      await expect(root.locator(".note-heading h2")).toHaveText(["相同标题 00:05", "相同标题 00:10", "Setext小标题", "引用小标题", /动力学/]);
      await expect(root.locator("[data-footnotes] .note-mark-button")).toHaveCount(0);
      await expect(root.locator(".note-title .note-mark-button, h1 .note-mark-button, h3 .note-mark-button")).toHaveCount(0);
      for (let index = 0; index < 5; index++) {
        await controls.nth(index).click();
        await expect(controls.nth(index)).toHaveAttribute("aria-pressed", "true");
      }
      expect(state.writes.map(item => item.heading_index)).toEqual([1, 2, 3, 4, 5]);
      expect(state.writes.every(item => item.chapter_id === "C1")).toBe(true);
    } else {
      if (kind === "document") {
        await expect(root.locator(".cite-button, .document-sources")).toHaveCount(0);
        await page.locator(".document-preview summary").click();
        await expect(page.locator(".document-unit")).toContainText("原文二级标题");
        await expect(page.locator(".document-unit .note-mark-button")).toHaveCount(0);
      } else await root.locator(".cite-button").first().click();
      await controls.first().click();
      await expect(controls.first()).toHaveAttribute("aria-pressed", "true");
      expect(state.writes[0]).toEqual({ id: "latest", chapter_id: "C1", heading_index: 0, marked: true });
      if (kind === "video") await expect(root.locator(".cite-button").first()).toHaveAttribute("aria-expanded", "true");
    }
    await controls.first().scrollIntoViewIfNeeded();
    const ids = await root.locator("h2[id]").evaluateAll(nodes => nodes.map(node => node.id));
    await root.locator(".summary-section").first().evaluate(node => { node.dataset.retained = "yes"; });
    const top = await page.evaluate(() => scrollY);
    await controls.first().focus(); await page.keyboard.press("Space");
    await expect(controls.first()).toHaveAttribute("aria-pressed", "false");
    await expect(controls.first()).toBeFocused();
    await expect(root.locator(".summary-section").first()).toHaveAttribute("data-retained", "yes");
    expect(await root.locator("h2[id]").evaluateAll(nodes => nodes.map(node => node.id))).toEqual(ids);
    expect(await page.evaluate(() => scrollY)).toBeCloseTo(top, 0);
    await controls.first().click(); await expect(controls.first()).toHaveAttribute("aria-pressed", "true");
    await page.reload(); await expect(controls.first()).toHaveAttribute("aria-pressed", "true");
    await root.getByRole("button", { name: "上一份笔记", exact: true }).click();
    await expect(controls.first()).toHaveAttribute("aria-pressed", "false");
    await expect(controls.first()).toBeEnabled();
    await controls.first().click(); await expect(controls.first()).toHaveAttribute("aria-pressed", "true");
    expect(state.writes.at(-1)?.id).toBe("previous");
    await root.getByRole("button", { name: "最新笔记", exact: true }).click();
    await expect(controls.first()).toHaveAttribute("aria-pressed", "true");
    if (kind === "document") {
      await page.getByText("提取内容", { exact: true }).click();
      await expect(page.locator(".document-preview .note-markdown h2")).toHaveText("原文二级标题");
      await expect(page.locator(".document-preview .note-mark-button")).toHaveCount(0);
    }
    expect(state.unexpected).toEqual([]);
  });
}

for (const kind of ["video", "document"] as const) test(`${kind} 读取与保存失败独立恢复，并行目标及迟到响应隔离`, async ({ page }) => {
  const { state, open, root } = await reader(page, kind, "concise");
  const initial = gate(); state.readGate = initial; state.failRead = true;
  await open();
  const controls = root.locator(".note-mark-button");
  await expect(controls).toHaveCount(5); await expect(controls.first()).toBeDisabled();
  initial.release();
  await expect(root.getByRole("button", { name: "重新读取重点标记" })).toBeVisible();
  await expect(root.locator(".note-markdown").first()).toContainText("内容一");
  await root.getByRole("button", { name: "重新读取重点标记" }).click();
  await expect(controls.first()).toBeEnabled();
  state.failSave = true;
  await controls.first().click();
  await expect(root.locator(".note-mark-error")).toContainText("标记保存失败");
  await expect(controls.first()).toHaveAttribute("aria-pressed", "false");
  expect(state.writes).toHaveLength(1);
  const saving = gate(); state.saveGate = saving;
  await controls.first().evaluate(node => { (node as HTMLButtonElement).click(); (node as HTMLButtonElement).click(); });
  await expect(controls.first()).toBeDisabled();
  expect(state.writes).toHaveLength(2);
  await expect(controls.nth(1)).toBeEnabled();
  await controls.nth(1).click(); await expect(controls.nth(1)).toHaveAttribute("aria-pressed", "true");
  saving.release(); await expect(controls.first()).toHaveAttribute("aria-pressed", "true");
  await expect(controls.nth(1)).toHaveAttribute("aria-pressed", "true");
  const lateSave = gate(); state.saveGate = lateSave;
  await controls.first().click(); await expect(controls.first()).toBeDisabled();
  await root.getByRole("button", { name: "上一份笔记", exact: true }).click();
  await expect(controls.first()).toBeEnabled();
  lateSave.release();
  await expect.poll(() => state.marks.latest.some(item => item.heading_index === 1)).toBe(false);
  await expect(controls.first()).toHaveAttribute("aria-pressed", "false");
  const lateRead = gate(); state.readGate = lateRead;
  await root.getByRole("button", { name: "最新笔记", exact: true }).click();
  await expect(controls.first()).toBeDisabled();
  await root.getByRole("button", { name: "上一份笔记", exact: true }).click();
  await expect(controls.first()).toBeEnabled();
  lateRead.release();
  await controls.first().click(); await expect(controls.first()).toHaveAttribute("aria-pressed", "true");
  expect(state.writes.at(-1)?.id).toBe("previous");
  expect(state.unexpected).toEqual([]);
});

for (const kind of ["video", "document"] as const) test(`${kind} 草稿没有重点入口或标记请求`, async ({ page }) => {
  const { state, open, root } = await reader(page, kind, "concise", true);
  await open();
  await expect(root.locator(".note-markdown").first()).toContainText("内容一");
  await expect(root.locator(".note-mark-button")).toHaveCount(0);
  expect(state.reads).toEqual([]); expect(state.writes).toEqual([]); expect(state.unexpected).toEqual([]);
});

test("重点按钮亮暗主题与窄屏布局、文字和焦点对比度", async ({ page }) => {
  const { open, root } = await reader(page, "document", "concise");
  await open();
  const button = root.locator(".note-mark-button").first();
  await expect(button).toBeEnabled(); await button.click();
  await expect(button).toHaveAttribute("aria-pressed", "true");
  for (const width of [1280, 375]) for (const theme of ["light", "dark"]) {
    await page.setViewportSize({ width, height: 871 });
    if (width === 375) await expect.poll(() => page.locator(".sidebar").evaluate(node => Math.round(node.getBoundingClientRect().x))).toBe(-260);
    await page.locator("html").evaluate((node, value) => { node.dataset.theme = value; }, theme);
    await button.focus();
    await page.keyboard.press("Tab"); await page.keyboard.press("Shift+Tab");
    await expect(button).toBeFocused();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    const ratios = await button.evaluate(node => {
      const luminance = (value: string) => value.match(/[\d.]+/g)!.slice(0, 3).map(Number).map(x => x / 255)
        .map(x => x <= .04045 ? x / 12.92 : ((x + .055) / 1.055) ** 2.4).reduce((s, x, i) => s + x * [.2126, .7152, .0722][i], 0);
      const ratio = (a: string, b: string) => (Math.max(luminance(a), luminance(b)) + .05) / (Math.min(luminance(a), luminance(b)) + .05);
      const style = getComputedStyle(node), panel = getComputedStyle(node.closest(".summary-panel")!);
      return [ratio(style.color, style.backgroundColor), ratio(style.outlineColor, panel.backgroundColor)];
    });
    expect(ratios[0]).toBeGreaterThanOrEqual(4.5); expect(ratios[1]).toBeGreaterThanOrEqual(3);
    await root.screenshot({ animations: "disabled", path: test.info().outputPath(`marks-${width}-${theme}.png`) });
  }
});

test("隔离文档真实保存与导出、搜索及任务快照不变", async ({ page, request }) => {
  expect((await request.put("/api/v1/settings/model", { data: { token: "fake-marks", stream: false } })).ok()).toBe(true);
  const upload = await request.post("/api/v1/documents", { multipart: { file: {
    name: "重点闭环.txt", mimeType: "text/plain", buffer: Buffer.from(`概念条件与原理。${test.info().testId}`),
  } } });
  expect(upload.ok()).toBe(true);
  const doc = await upload.json();
  try {
    const created = await request.post(`/api/v1/documents/${doc.id}/analyses`, { data: { note_style: "detailed" } });
    expect(created.ok()).toBe(true);
    const { task_id: id } = await created.json();
    await expect.poll(async () => (await (await request.get(`/api/v1/document-tasks/${id}`)).json()).status).toBe("succeeded");
    const before = await (await request.get(`/api/v1/document-tasks/${id}`)).json();
    const notes = await (await request.get(`/api/v1/document-analyses/${id}`)).json();
    const exported = await (await request.get(`/api/v1/document-analyses/${id}/notes?format=md`)).text();
    const search = await (await request.get("/api/v1/notes/search?q=概念")).json();
    await page.goto(`/#summary/${doc.id}`);
    const button = page.locator("#document-notes .note-mark-button").first();
    await expect(button).toBeEnabled(); await button.click();
    await expect(button).toHaveAttribute("aria-pressed", "true");
    await page.reload(); await expect(button).toHaveAttribute("aria-pressed", "true");
    expect((await (await request.get(`/api/v1/note-marks/document/${id}`)).json()).marks).toEqual([{ chapter_id: notes.chapters[0].chapter_id, heading_index: 0 }]);
    await page.goto("/#notes?view=marks&page=1");
    const group = page.locator(".note-search-group").filter({ hasText: "重点闭环.txt" });
    await expect(group.getByRole("link")).toHaveText(notes.chapters[0].title);
    await group.getByRole("link").click();
    await expect(page.locator(`#document-note-${notes.chapters[0].chapter_id}`)).toBeFocused();
    expect(await (await request.get(`/api/v1/document-tasks/${id}`)).json()).toEqual(before);
    expect(await (await request.get(`/api/v1/document-analyses/${id}`)).json()).toEqual(notes);
    expect(await (await request.get(`/api/v1/document-analyses/${id}/notes?format=md`)).text()).toBe(exported);
    expect(await (await request.get("/api/v1/notes/search?q=概念")).json()).toEqual(search);
    await button.click(); await expect(button).toHaveAttribute("aria-pressed", "false");
  } finally { expect((await request.delete(`/api/v1/documents/${doc.id}`)).ok()).toBe(true); }
});

for (const kind of ["video", "document"] as const) for (const style of ["concise", "detailed"] as const) {
  test(`${kind}/${style} 笔记中心显示两版重点并定位，取消后返回更新列表`, async ({ page }) => {
    const { state, open, root } = await reader(page, kind, style);
    state.marks.previous = [{ chapter_id: "C1", heading_index: style === "concise" ? 2 : 0 }];
    await open();
    const controls = root.locator(".note-mark-button");
    await expect(controls.first()).toBeEnabled(); await controls.first().click();
    await expect(controls.first()).toHaveAttribute("aria-pressed", "true");
    await page.goto("/#notes");
    await page.getByRole("link", { name: "我的重点", exact: true }).click();
    await expect(page.locator(".note-center-status")).toContainText("已标记 2 处重点");
    await expect(page.locator(".note-search-hit > .muted").filter({ hasText: "最新笔记" })).toHaveCount(0);
    const old = page.locator(".note-search-hit").filter({ hasText: "上一份笔记" });
    await expect(old.getByRole("link")).toHaveText(style === "concise" ? "相同标题 00:10" : "第1章");
    const markRead = gate(); state.readGate = markRead;
    await old.getByRole("link").click();
    const prefix = kind === "video" ? "note-block" : "document-note";
    const target = page.locator(`#${prefix}-C1${style === "concise" ? ' h2[data-note-heading="2"]' : ""}`);
    await expect(root.getByText("正在读取重点标记…", { exact: true })).toBeVisible();
    await expect(target).not.toBeFocused();
    markRead.release();
    await expect(target).toBeFocused();
    await expect(root.getByRole("button", { name: "上一份笔记", exact: true })).toHaveAttribute("aria-pressed", "true");
    await expect.poll(() => target.evaluate(node => Math.abs(node.getBoundingClientRect().top - 80))).toBeLessThan(3);
    await root.locator(".summary-section").first().evaluate(node => { node.dataset.retained = "yes"; });
    const chosen = controls.nth(style === "concise" ? 1 : 0);
    await chosen.click(); await expect(chosen).toHaveAttribute("aria-pressed", "false");
    await expect(root.locator(".summary-section").first()).toHaveAttribute("data-retained", "yes");
    await page.getByRole("link", { name: "返回笔记中心", exact: false }).click();
    await expect(page.getByRole("link", { name: "我的重点", exact: true })).toHaveAttribute("aria-current", "page");
    await expect(page.locator(".note-center-status")).toContainText("已标记 1 处重点");
    await expect(page.locator(".note-search-hit")).not.toContainText("上一份笔记");
    expect(state.unexpected).toEqual([]);
  });
}

test("重点列表取消迟到响应、空态及独立失败重试", async ({ page }) => {
  const { state } = await reader(page, "document", "concise");
  state.marks.latest = [{ chapter_id: "C1", heading_index: 1 }];
  const held = gate(); state.listGate = held;
  await page.goto("/#notes?view=marks&page=1");
  await expect(page.getByRole("status")).toHaveText("正在读取重点…");
  await page.getByRole("link", { name: "搜索笔记", exact: true }).click();
  state.marks.latest = [];
  await page.getByRole("link", { name: "我的重点", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("还没有重点");
  held.release();
  await expect(page.locator(".note-search-hit")).toHaveCount(0);
  state.failList = true;
  await page.getByRole("button", { name: "刷新重点" }).click();
  await expect(page.locator(".note-search-state")).toContainText("重点列表读取失败");
  state.marks.latest = [{ chapter_id: "C1", heading_index: 1 }];
  await page.getByRole("button", { name: "重新读取重点", exact: true }).click();
  await expect(page.locator(".note-search-hit")).toHaveCount(1);
  expect(state.unexpected).toEqual([]);
});

test("重点分页及URL恢复，失效小节提示而不定位其他内容", async ({ page }) => {
  const { state } = await reader(page, "document", "concise");
  await page.route("**/api/v1/note-marks?*", route => {
    const currentPage = Number(new URL(route.request().url()).searchParams.get("page"));
    return route.fulfill({ json: { total: 21, page: currentPage, page_size: 20, items: [{ kind: "document", file_id: "file",
      filename: "重点测试", task_id: "previous", note_title: "重点标记测试", version: 2, chapter_id: "C1", chapter_title: "第1章",
      heading_index: currentPage === 1 ? 1 : 99, title: currentPage === 1 ? "第一页重点" : "已失效小节", snippet: "重点片段。" }] } });
  });
  await page.goto("/#notes?view=marks&page=1");
  await page.getByRole("button", { name: "下一页" }).click();
  await expect(page).toHaveURL(/view=marks.*page=2/);
  await page.reload();
  await expect(page.getByRole("navigation", { name: "重点列表分页" })).toContainText("第 2 / 2 页");
  await page.getByRole("link", { name: "已失效小节" }).click();
  await expect(page.locator(".notice")).toContainText("小节已不可用");
  await expect(page.locator('#document-note-C1 h2[data-note-heading="1"]')).not.toBeFocused();
  await page.getByRole("link", { name: "返回笔记中心", exact: false }).click();
  await expect(page).toHaveURL(/view=marks/); await expect(page).toHaveURL(/page=2/);
  await expect(page.getByRole("link", { name: "已失效小节" })).toBeVisible();
  expect(state.unexpected).toEqual([]);
});

test("重点列表亮暗主题与375宽度，纯文本和键盘链接可用", async ({ page }) => {
  const { state } = await reader(page, "document", "concise");
  state.marks.latest = [{ chapter_id: "C1", heading_index: 1 }];
  state.marks.previous = [{ chapter_id: "C1", heading_index: 2 }];
  await page.goto("/#notes?view=marks&page=1");
  await expect(page.locator(".note-search-hit")).toHaveCount(2);
  for (const width of [1280, 375]) for (const theme of ["light", "dark"]) {
    await page.setViewportSize({ width, height: 871 });
    if (width === 375) await expect.poll(() => page.locator(".sidebar").evaluate(node => Math.round(node.getBoundingClientRect().x))).toBe(-260);
    await page.locator("html").evaluate((node, value) => { node.dataset.theme = value; }, theme);
    await page.locator(".note-search-hit a").first().focus();
    await expect(page.locator(".note-search-hit a").first()).toBeFocused();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ animations: "disabled", path: test.info().outputPath(`center-marks-${width}-${theme}.png`) });
  }
});
