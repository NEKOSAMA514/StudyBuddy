import { test, expect, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { taskResponse } from "./support/taskContract";
import { modelBudgets } from "./support/modelContract";
import { settingsRead } from "./support/settings";

test.beforeAll(async ({ request }) => {
  expect((await (await request.get("/__test__/identity")).json()).fake_services_used).toBe(true);
});

async function reader(page: Page, kind: "video" | "document", failures = { notes: false, versions: false }, style: "concise" | "detailed" = "concise") {
  let reads = 0;
  const id = `reader-${kind}`, taskId = `${id}-task`;
  const base = { id: taskId, video_id: id, document_id: id, status: "succeeded", stage: "completed",
    created_at: "2026-10-20T00:00:00Z", updated_at: "2026-10-20T00:00:00Z", completed_at: "2026-10-20T00:00:00Z",
    notes_state: "ready", notes_revision: 1, config: {}, timings: {}, warnings: [], error: null };
  const task = kind === "video" ? taskResponse(base) : { ...base, progress: { stage_labels: {}, segment: 1, total_segments: 1 }, request_count: 1, request_limit: 80 };
  const file = { id, kind, filename: "阅读检查", duration: 120, format: "md", unit_count: 1,
    character_count: 50, size_bytes: 100, pinned: false, created_at: base.created_at, latest_task: task };
  const result = { note_style: style, format_version: kind === "video" ? 4 : 1, complete: true,
    completed_segments: 1, total_segments: 1, title: "阅读检查", tags: [], overview_paragraphs: [], source_issues: [], evidence_ids: [],
    chapters: [{ chapter_id: "S1", title: "阅读检查", start_seconds: 0, end_seconds: 120, timestamp_basis: "speech", evidence_ids: [], source_ids: [], source_labels: ["原文 1"],
      markdown: "## 小标题 [00:00](#video-time-0)\n\n正文包含 **重点** 与 `inline`。\n\n> 引用需要保持清晰。\n\n```python\n# 注释\nwhile True:\n    print(123, 'text')\n" + "very_long_identifier_".repeat(40) + "\n```\n\n| 标题 | 标题 | 标题 | 标题 | 标题 | 标题 | 标题 | 标题 | 标题 | 标题 | 标题 | 标题 |\n| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n| 数据 | 数据 | 数据 | 数据 | 数据 | 数据 | 数据 | 数据 | 数据 | 数据 | 数据 | 数据 |\n\n$$\n" + "x+".repeat(180) + "1\n$$" }], model: "fake", prompt_version: "fake", coverage_note: "", basis: [] };
  if (style === "detailed") result.chapters.push({ ...result.chapters[0], chapter_id: "S2", title: "第二章",
    start_seconds: 60, end_seconds: 120, markdown: "另一章正文。" });
  const writes: string[] = [];
  await page.route("**/api/v1/**", async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === `/api/v1/videos/${id}/content`) return route.fulfill({ contentType: "video/mp4", body: readFileSync(process.env.E2E_VIDEO_PATH!) });
    if (route.request().method() !== "GET") writes.push(path);
    let json: unknown;
    if (path.endsWith("/health")) json = { model_configured: true, supported_video_extensions: ["mp4"], supported_document_extensions: ["md"] };
    else if (path === "/api/v1/videos") json = kind === "video" ? [file] : [];
    else if (path === "/api/v1/documents") json = kind === "document" ? [file] : [];
    else if (route.request().method() === "GET" && /^\/api\/v1\/note-marks\/(video|document)\/[^/]+$/.test(path)) json = { marks: [] };
    else if (path.endsWith(`/${id}/analyses`)) {
      if (failures.versions) return route.fulfill({ status: 503, json: { error: { message: "版本不可用", error_id: "list", log_saved: true } } });
      json = [task];
    } else if (path.endsWith(`/${taskId}`)) {
      reads++;
      if (failures.notes) return route.fulfill({ status: 503, json: { error: { message: "正文不可用", error_id: "notes", log_saved: true } } });
      json = result;
    }
    else if (path.endsWith("/evidence")) json = [];
    else if (path.endsWith("/playback")) json = { status: "unprepared", direct: true, original_url: `/api/v1/videos/${id}/content`, mime_type: "video/mp4", error: null };
    else if (path.endsWith("/content")) json = { units: [{ id: "D1", label: "原文 1", locator: {}, format: "markdown", text: result.chapters[0].markdown }], total: 1, offset: 0 };
    else if (path.endsWith("/settings/model/profiles")) json = { active_id: "default", profiles: [{ ...modelBudgets, id: "default", name: "测试", model: "fake", base_url: "https://test.invalid", has_token: true, stream: true, json_mode: false, section_concurrency: 3, request_interval_seconds: 3 }] };
    else if (await settingsRead(route)) return;
    else throw new Error(`Unexpected request: ${path}`);
    await route.fulfill({ json });
  });
  await page.goto(`/#summary/${id}`);
  if (failures.notes) await expect(page.getByRole("button", { name: "重新读取笔记" })).toBeVisible();
  else await expect(page.locator(".summary-panel .note-markdown").first()).toBeVisible();
  return { writes, failures, reads: () => reads };
}

async function contrast(page: Page, selector: string) {
  return page.locator(selector).evaluate(root => {
    const rgba = (value: string) => {
      const numbers = value.match(/[\d.]+/g)!.map(Number);
      return [numbers[0], numbers[1], numbers[2], numbers[3] ?? 1];
    };
    const over = (fg: number[], bg: number[]) => [...fg.slice(0, 3).map((value, i) => value * fg[3] + bg[i] * (1 - fg[3])), 1];
    function background(el: Element | null): number[] {
      if (!el) return [255, 255, 255, 1];
      return over(rgba(getComputedStyle(el).backgroundColor), background(el.parentElement));
    }
    const luminance = (color: number[]) => color.slice(0, 3).map(v => v / 255)
      .map(v => v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4)
      .reduce((sum, v, i) => sum + v * [0.2126, 0.7152, 0.0722][i], 0);
    const ratio = (a: number[], b: number[]) => (Math.max(luminance(a), luminance(b)) + 0.05) / (Math.min(luminance(a), luminance(b)) + 0.05);
    const failures: string[] = [];
    for (const el of [root, ...root.querySelectorAll("*")]) {
      if (!(el instanceof HTMLElement) || !el.getClientRects().length || getComputedStyle(el).visibility !== "visible" ||
        el.closest(":disabled, [inert], [aria-hidden=true]") || !Array.from(el.childNodes).some(n => n.nodeType === Node.TEXT_NODE && n.textContent?.trim())) continue;
      const bg = background(el), fg = over(rgba(getComputedStyle(el).color), bg);
      if (ratio(fg, bg) < 4.5) failures.push(`${el.tagName}.${el.className}: ${el.textContent?.slice(0, 35)} ${ratio(fg, bg).toFixed(2)}`);
    }
    for (const el of root.querySelectorAll("input:not([type=checkbox]), textarea, select, .api-profile[aria-pressed=true], .note-version-actions button[aria-pressed=true]")) {
      if (!(el instanceof HTMLElement) || !el.getClientRects().length || el.closest(":disabled")) continue;
      const style = getComputedStyle(el), bg = background(el);
      if (ratio(over(rgba(style.borderTopColor), bg), bg) < 3) failures.push(`控件边界 ${el.tagName}`);
      if (el.matches("input, textarea") && el.getAttribute("placeholder") && !(el as HTMLInputElement).value) {
        const color = over(rgba(getComputedStyle(el, "::placeholder").color), bg);
        if (ratio(color, bg) < 4.5) failures.push(`占位文字 ${el.tagName}`);
      }
    }
    const focused = root.querySelector(":focus-visible");
    if (focused) {
      const bg = background(focused.parentElement), style = getComputedStyle(focused);
      if (ratio(over(rgba(style.outlineColor), bg), bg) < 3) failures.push("焦点指示");
    }
    return failures;
  });
}

for (const kind of ["video", "document"] as const) for (const style of ["concise", "detailed"] as const)
test(`${kind}-${style}阅读断点与实际正文宽度、控件和代码对比度`, async ({ page }) => {
  test.skip(test.info().project.name !== "desktop-1280", "用例内部已遍历尺寸、主题和侧栏矩阵，只执行一次。");
  const { writes } = await reader(page, kind, undefined, style);
  await page.locator(".summary-panel").scrollIntoViewIfNeeded();
  await expect(page.locator(".chapter-navigator")).toHaveCount(style === "detailed" ? 1 : 0);
  for (const width of [1280, 701, 700, 375]) {
    await page.setViewportSize({ width, height: 871 });
    for (const collapsed of [false, true]) {
      if (width > 700) {
        const currentlyCollapsed = await page.locator(".shell").evaluate(el => el.classList.contains("sidebar-collapsed"));
        if (currentlyCollapsed !== collapsed) await page.locator(currentlyCollapsed ? "main .desktop-sidebar-toggle" : ".sidebar .desktop-sidebar-toggle").click();
      } else {
        const open = await page.locator(".shell").evaluate(el => el.classList.contains("sidebar-open"));
        if (open && collapsed) await page.locator(".sidebar .desktop-sidebar-toggle").click();
        else if (!open && !collapsed) await page.locator("main .sidebar-toggle").click();
      }
      for (const theme of ["light", "dark"]) {
        await page.locator("html").evaluate((el, theme) => el.setAttribute("data-theme", theme), theme);
        const body = page.locator(".summary-panel .note-markdown").first();
        if (width === 375) expect(await body.evaluate(el => el.getBoundingClientRect().width)).toBeGreaterThanOrEqual(290);
        expect(await body.evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
        expect(await page.locator("body").evaluate(el => el.scrollWidth <= innerWidth)).toBe(true);
        for (const selector of [".note-code-block pre", ".markdown-table", ".katex-display"]) {
          const area = body.locator(selector);
          await expect(area).toHaveCSS("overflow-x", "auto");
          expect(await area.evaluate(el => el.scrollWidth > el.clientWidth)).toBe(true);
        }
        expect(await contrast(page, ".summary-panel")).toEqual([]);
        await page.locator(".summary-panel").screenshot({ path: test.info().outputPath(`${kind}-${width}-${collapsed}-${theme}.png`) });
      }
    }
  }
  await page.goto("/#settings");
  const dialog = page.getByRole("dialog", { name: "设置", exact: true });
  await expect(dialog.locator(".api-profile")).toBeVisible();
  await dialog.getByText("高级设置", { exact: true }).click();
  for (const theme of ["light", "dark"]) {
    await page.locator("html").evaluate((el, theme) => el.setAttribute("data-theme", theme), theme);
    await page.keyboard.press("Tab");
    expect(await contrast(page, ".settings-dialog")).toEqual([]);
  }
  expect(writes).toEqual([]);
});


test("文档正文与版本列表独立重读，来源密度不继承正文代码字号", async ({ page }) => {
  const state = await reader(page, "document", { notes: true, versions: true });
  await expect(page.getByRole("button", { name: "重新读取版本列表" })).toBeVisible();
  state.failures.notes = false;
  await page.getByRole("button", { name: "重新读取笔记" }).click();
  await expect(page.locator(".summary-panel .note-markdown")).toBeVisible();
  const reads = state.reads();
  state.failures.versions = false;
  await page.getByRole("button", { name: "重新读取版本列表" }).click();
  await expect(page.getByRole("button", { name: "重新读取版本列表" })).toHaveCount(0);
  expect(state.reads()).toBe(reads);
  await page.getByText("提取内容", { exact: true }).click();
  const source = page.locator('.document-preview .note-markdown[data-density="source"]');
  await expect(source).toHaveCSS("font-size", "14px");
  await expect(source.locator("h2")).toHaveCSS("font-size", "20px");
  await expect(source.locator("pre")).toHaveCSS("font-size", "14px");
  await expect(source.locator(".note-code-toolbar")).toHaveCSS("font-size", "12px");
  expect(state.writes).toEqual([]);
});
