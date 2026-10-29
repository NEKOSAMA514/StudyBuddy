import { test, expect, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { taskResponse } from "./support/taskContract";

function chapters(count: number, prefix = "章节") {
  return Array.from({ length: count }, (_, index) => ({
    chapter_id: `C${index + 1}`, title: `${prefix}${index + 1}`,
    markdown: Array.from({ length: 6 }, () => "这是一段用于验证长章节阅读位置的正文，解释概念之间的因果关系。").join("\n\n"),
    start_seconds: index * 10, end_seconds: (index + 1) * 10,
    timestamp_basis: "speech" as const, evidence_ids: ["speech-1"],
  }));
}

async function workspace(page: Page, count = 5, running = false) {
  const result = (size: number, prefix?: string) => ({ format_version: 4, title: "章节导航测试", tags: [],
    overview_paragraphs: ["本笔记用于测试章节导航。"], chapters: chapters(size, prefix),
    source_issues: [], evidence_ids: ["speech-1"], model: "fake", prompt_version: "fake", basis: [], coverage_note: "" });
  const task = (id: string, date: string, video = "first") => ({ id, video_id: video,
    status: "succeeded", stage: "completed", notes_state: "ready", notes_revision: 1,
    created_at: date, updated_at: date, config: {}, warnings: [], timings: {}, error: null });
  const state = {
    current: task("latest", "2026-10-20T00:00:00Z"),
    old: task("previous", "2026-10-19T00:00:00Z"),
    other: task("other", "2026-10-20T00:00:00Z", "second"),
    results: { latest: result(count), previous: result(3, "旧章"), other: result(2, "另一个视频") },
  };
  if (!count) Object.assign(state.current, { notes_state: "none", config: { outline_preview: "只有材料导览，暂无章节正文。" } });
  if (running) Object.assign(state.current, { status: "running", stage: "segments", notes_state: "partial" });
  await page.route("**/api/v1/**", async route => {
    const path = new URL(route.request().url()).pathname;
    let body: unknown;
    if (path === "/api/v1/health") body = { model_configured: true, max_upload_bytes: 2147483648, max_duration_seconds: 7200 };
    else if (path === "/api/v1/documents") body = [];
    else if (route.request().method() === "GET" && /^\/api\/v1\/note-marks\/video\/[^/]+$/.test(path)) body = { marks: [] };
    else if (path === "/api/v1/videos") body = [state.current, state.other].map(item => ({
      id: item.video_id, filename: `${item.video_id}.mp4`, duration: 120, size_bytes: 1000,
      created_at: item.created_at, media_info: { container: "mp4" }, latest_task: taskResponse(item),
    }));
    else if (path.endsWith("/analyses")) body = path.includes("/first/")
      ? (running ? [] : [state.current, state.old]).map(taskResponse) : [taskResponse(state.other)];
    else if (path.includes("/tasks/")) body = taskResponse(state.current);
    else if (path.includes("/analyses/")) body = state.results[path.split("/").at(-1)! as keyof typeof state.results];
    else if (path.endsWith("/evidence")) body = [{ id: "speech-1", kind: "speech", start_seconds: 0,
      end_seconds: 1, text: "模拟原始依据。", image_name: null, confidence: null }];
    else if (path.endsWith("/playback")) body = { direct: true, status: "unprepared", mime_type: "video/mp4",
      original_url: path.replace("/playback", "/content"), url: null, error: null };
    else if (path.endsWith("/content")) {
      const media = readFileSync(process.env.E2E_VIDEO_PATH!);
      const range = /bytes=(\d+)-(\d*)/.exec(route.request().headers().range || "");
      const start = Number(range?.[1] || 0), end = range?.[2] ? Number(range[2]) : media.length - 1;
      await route.fulfill({ status: range ? 206 : 200, contentType: "video/mp4", body: media.subarray(start, end + 1),
        headers: { "Accept-Ranges": "bytes", ...(range ? { "Content-Range": `bytes ${start}-${end}/${media.length}` } : {}) } });
      return;
    } else { await route.fulfill({ status: 404, json: {} }); return; }
    await route.fulfill({ json: body });
  });
  await page.goto("/#summary/first");
  await expect(page.locator("#video-notes h2")).toHaveText("视频笔记");
  if (count) await expect(page.locator("#note-block-C1 .note-markdown")).toContainText("因果关系");
  return state;
}

async function scrollTo(page: Page, id: string, offset = 80) {
  await page.locator(id).evaluate((element, top) => window.scrollBy({ top: element.getBoundingClientRect().top - top, behavior: "instant" }), offset);
}

test.beforeEach(async ({ request }) => {
  expect((await (await request.get("/__test__/identity")).json()).fake_services_used).toBe(true);
});

test("点击章节跳转且不改变视频位置，手动滚动长章节更新高亮", async ({ page }) => {
  await workspace(page);
  const nav = page.getByRole("navigation", { name: "笔记章节导航" });
  await expect(nav).toHaveCount(0);
  await scrollTo(page, "#video-notes");
  await expect(nav.getByRole("button")).toHaveCount(5);
  await expect(nav.locator("[aria-current]")).toHaveCount(0);
  await expect.poll(() => page.locator("video").evaluate((video: HTMLVideoElement) => video.readyState)).toBeGreaterThan(0);
  await page.locator("video").evaluate((video: HTMLVideoElement) => { video.pause(); video.currentTime = 7; });
  const mark = nav.getByRole("button", { name: "跳转到第 3 章：章节3", exact: true });
  await mark.hover();
  await expect(page.getByRole("tooltip")).toContainText("00:20–00:30");
  await mark.click();
  await expect(mark).toHaveAttribute("aria-current", "location");
  await expect.poll(() => page.locator("#note-block-C3").evaluate(el => Math.abs(el.getBoundingClientRect().top - 80))).toBeLessThan(2);
  await expect.poll(() => page.locator("video").evaluate((video: HTMLVideoElement) => video.currentTime)).toBe(7);
  await scrollTo(page, "#note-block-C2", -120);
  await expect(nav.getByRole("button", { name: "跳转到第 2 章：章节2", exact: true })).toHaveAttribute("aria-current", "location");
  await page.evaluate(() => { const spacer = document.createElement("div"); spacer.style.height = "1500px"; document.body.append(spacer); });
  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
  await expect(nav).toHaveCount(0);
});

test("键盘跳转尊重减少动态效果设置，预览与焦点关联", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await workspace(page);
  await scrollTo(page, "#video-notes");
  const marks = page.getByRole("navigation", { name: "笔记章节导航" }).getByRole("button");
  await marks.nth(0).focus();
  await page.keyboard.press("Tab");
  await expect(marks.nth(1)).toBeFocused();
  await expect(page.getByRole("tooltip")).toContainText("第 2 章 · 章节2");
  await expect(marks.nth(1)).toHaveAttribute("aria-describedby", await page.getByRole("tooltip").getAttribute("id") as string);
  await page.evaluate(() => {
    const scroll = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = function(options) {
      (window as any).chapterScrollBehavior = (options as ScrollIntoViewOptions)?.behavior;
      scroll.call(this, options);
    };
  });
  await page.keyboard.press("Enter");
  await expect.poll(() => page.evaluate(() => (window as any).chapterScrollBehavior)).toBe("auto");
  await expect(marks.nth(1)).toHaveAttribute("aria-current", "location");
  await marks.nth(2).focus();
  await page.keyboard.press("Space");
  await expect(marks.nth(2)).toHaveAttribute("aria-current", "location");
});

test("大量章节只滚动导航内部，操作导航时不自动挪动横条", async ({ page }) => {
  await workspace(page, 60);
  await scrollTo(page, "#note-block-C45");
  const rail = page.locator(".chapter-navigator-scroll");
  await expect(page.locator('.chapter-mark[data-chapter="C45"]')).toHaveAttribute("aria-current", "location");
  await expect.poll(() => rail.evaluate(el => el.scrollTop)).toBeGreaterThan(0);
  expect(await rail.evaluate(el => el.clientHeight <= innerHeight / 2)).toBe(true);
  await rail.hover();
  await rail.evaluate(el => { el.scrollTop = 0; });
  await scrollTo(page, "#note-block-C46");
  await expect(page.locator('.chapter-mark[data-chapter="C46"]')).toHaveAttribute("aria-current", "location");
  expect(await rail.evaluate(el => el.scrollTop)).toBe(0);
  await page.mouse.move(100, 100);
  await scrollTo(page, "#note-block-C47");
  await expect.poll(() => rail.evaluate(el => el.scrollTop)).toBeGreaterThan(0);
});

test("分段追加和展开依据保留阅读位置与已有节点", async ({ page }) => {
  const state = await workspace(page, 3, true);
  await scrollTo(page, "#note-block-C2", -60);
  await page.locator("#note-block-C2 .cite-button").click();
  await page.locator("#note-block-C1").evaluate(el => { el.dataset.retained = "yes"; });
  const position = await page.evaluate(() => window.scrollY);
  state.results.latest.chapters.push(...chapters(5).slice(3));
  state.current.notes_revision++;
  await expect(page.locator(".chapter-mark")).toHaveCount(5);
  await expect(page.locator("#note-block-C1")).toHaveAttribute("data-retained", "yes");
  await expect(page.locator("#note-block-C2 .cite-button")).toHaveAttribute("aria-expanded", "true");
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBeCloseTo(position, 0);
  await expect(page.locator('.chapter-mark[data-chapter="C2"]')).toHaveAttribute("aria-current", "location");
  // A size change above the reading line must update the active chapter even without scrolling.
  await page.evaluate(() => { document.documentElement.style.overflowAnchor = "none"; });
  await page.locator("#note-block-C1").evaluate(el => { el.style.minHeight = "2000px"; });
  await expect(page.locator('.chapter-mark[data-chapter="C1"]')).toHaveAttribute("aria-current", "location");
});

test("切换笔记版本和视频更新导航，不保留旧章节预览", async ({ page }) => {
  await workspace(page);
  await scrollTo(page, "#video-notes");
  await page.locator(".chapter-mark").nth(1).hover();
  await expect(page.getByRole("tooltip")).toBeVisible();
  await page.getByRole("button", { name: "上一份笔记", exact: true }).click();
  await expect(page.locator(".chapter-mark")).toHaveCount(3);
  await expect(page.locator(".chapter-mark").first()).toHaveAttribute("aria-label", /旧章1/);
  await expect(page.getByRole("tooltip")).toHaveCount(0);
  await page.getByRole("button", { name: "最新笔记", exact: true }).click();
  await expect(page.locator(".chapter-mark")).toHaveCount(5);
  await page.evaluate(() => { location.hash = "summary/second"; });
  await expect(page.locator("#note-block-C1 h3")).toHaveText("另一个视频1");
  await scrollTo(page, "#video-notes");
  await expect(page.locator(".chapter-mark")).toHaveCount(2);
});

for (const count of [0, 1]) test(`${count}章时不显示导航`, async ({ page }) => {
  await workspace(page, count);
  await scrollTo(page, "#video-notes");
  await expect(page.locator(".chapter-navigator")).toHaveCount(0);
});

for (const theme of ["light", "dark"]) test(`${theme}主题窄屏预览不越界且不遮挡正文或回顶按钮`, async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 740 });
  await page.addInitScript(value => localStorage.setItem("studybuddy-theme", value), theme);
  await workspace(page, 30);
  await page.evaluate(value => { document.documentElement.dataset.theme = value; }, theme);
  await scrollTo(page, "#note-block-C1");
  const rail = page.locator(".chapter-navigator-scroll");
  const first = page.locator(".chapter-mark").first();
  await first.hover();
  await expect(page.getByRole("tooltip")).toBeVisible();
  const preview = (await page.getByRole("tooltip").boundingBox())!;
  expect(preview.x).toBeGreaterThanOrEqual(0);
  expect(preview.y).toBeGreaterThanOrEqual(0);
  expect(preview.x + preview.width).toBeLessThanOrEqual(390);
  expect(preview.y + preview.height).toBeLessThanOrEqual(740);
  const body = (await page.locator("#note-block-C1 .note-markdown").boundingBox())!;
  const band = (await rail.boundingBox())!;
  expect(body.x + body.width).toBeLessThan(band.x);
  const back = (await page.getByRole("button", { name: "回到顶端", exact: true }).boundingBox())!;
  expect(band.y + band.height).toBeLessThan(back.y);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: test.info().outputPath(`chapter-rail-${theme}.png`) });
  await first.click();
  await expect(first).toHaveAttribute("aria-current", "location");
});
