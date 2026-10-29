import { settingsRead } from "./support/settings";
import { taskResponse } from "./support/taskContract";
import { modelBudgets } from "./support/modelContract";
import { test, expect, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";

const course = "主题验证课程.mp4";
const evidence = [
  { id: "speech-1", kind: "speech", start_seconds: 5, end_seconds: 25, text: "慢启动通过逐步增大拥塞窗口探测可用带宽。", image_name: null, confidence: null },
  { id: "frame-1", kind: "frame", start_seconds: 30, end_seconds: 30, text: "拥塞窗口示意", image_name: "frame.jpg", confidence: 0.95 },
];
const result = {
  format_version: 4, title: "TCP 拥塞控制", tags: ["慢启动"],
  overview_paragraphs: ["课程介绍 TCP 拥塞控制及慢启动。"],
  chapters: [{ chapter_id: "C1", title: "慢启动", markdown: "拥塞窗口逐步增长。", start_seconds: 5, end_seconds: 25,
    timestamp_basis: "speech", evidence_ids: ["speech-1", "frame-1"] }],
  source_issues: [],
  evidence_ids: evidence.map(item => item.id), model: "fake", prompt_version: "fake", basis: ["speech", "frame"], coverage_note: "测试预设笔记。",
};
const task = { id: "theme-task", video_id: "theme-video", status: "succeeded", stage: "completed",
  config: { extract_version: "extract-v3-chinese-region-ocr" }, error: null, warnings: [], timings: { total: 12 }, notes_state: "ready", notes_revision: 1,
  progress: { percent: 100, completed_steps: 6, current_step: 6, processed: 1, total: 1 } };
const video = { id: "theme-video", filename: course, duration: 120, size_bytes: 1000000,
  created_at: "2026-10-13T00:00:00", media_info: { container: "mp4" }, latest_task: taskResponse(task) };
const models = ["paraformer", "rapidocr"].map(id => ({ id, kind: id === "paraformer" ? "asr" : "ocr",
  name: id, description: "隔离测试模型", directory: "models/" + id, size: "1 MB", docs: "https://test.invalid",
  ready: true, runtime_ready: true, download: null }));

async function mockAPI(page: Page) {
  const unexpected: string[] = [];
  await page.route("**/api/v1/**", async route => {
    const path = new URL(route.request().url()).pathname;
    let body: unknown;
    if (path === "/api/v1/health") body = { model_configured: true, free_tier_confirmed: true,
      max_upload_bytes: 524288000, max_duration_seconds: 1200, supported_video_extensions: ["mp4", "webm"],
      recognition: { extract_version: "extract-v3-chinese-region-ocr" } };
    else if (path === "/api/v1/videos") body = [video];
    else if (path === "/api/v1/documents") body = [];
    else if (route.request().method() === "GET" && path === "/api/v1/note-marks/video/theme-task") body = { marks: [] };
    else if (path === "/api/v1/tasks/theme-task") body = taskResponse(task);
    else if (path === "/api/v1/videos/theme-video/analyses") body = [taskResponse(task)];
    else if (path === "/api/v1/analyses/theme-task") body = result;
    else if (path === "/api/v1/videos/theme-video/evidence") body = evidence;
    else if (path === "/api/v1/videos/theme-video/playback") body = { status: "ready", url: "/api/v1/videos/theme-video/content", method: "remux", error: null };
    else if (path.endsWith("/content")) {
      const media = readFileSync(process.env.E2E_VIDEO_PATH!);
      const range = /bytes=(\d+)-(\d*)/.exec(route.request().headers().range || "");
      const start = range ? Number(range[1]) : 0;
      const end = range?.[2] ? Math.min(Number(range[2]), media.length - 1) : media.length - 1;
      await route.fulfill({ status: range ? 206 : 200, contentType: "video/mp4", body: media.subarray(start, end + 1),
        headers: { "Accept-Ranges": "bytes", ...(range ? { "Content-Range": `bytes ${start}-${end}/${media.length}` } : {}) } });
      return;
    } else if (path.endsWith("/image")) {
      await route.fulfill({ contentType: "image/png", body: Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jZ1cAAAAASUVORK5CYII=", "base64") });
      return;
    } else if (path === "/api/v1/settings/model/profiles") body = { active_id: "default", profiles: [{ ...modelBudgets, id: "default", name: "魔搭", base_url: "https://test.invalid", model: "fake", has_token: false,
      stream: true, json_mode: false, free_tier_confirmed: true, section_concurrency: 3, request_interval_seconds: 3 }] };
    else if (path === "/api/v1/settings/model") body = { base_url: "https://test.invalid", model: "fake", has_token: false,
      stream: true, json_mode: false, free_tier_confirmed: true, section_concurrency: 3, request_interval_seconds: 3, summary_prompt: "" };
    else if (path.endsWith("/analysis-preview")) body = { character_count: null, estimated_segments: null, reason: "识别材料尚未就绪。" };
    else if (path === "/api/v1/settings/note-style") body = { summary_prompt: "", note_style: "detailed" };
    else if (path === "/api/v1/settings/recognition") body = { settings: { asr_option: "paraformer", ocr_option: "rapidocr", language: "zh" }, models, active_download: null };
    else if (await settingsRead(route)) return;
    else {
      unexpected.push(`${route.request().method()} ${path}`);
      await route.abort();
      return;
    }
    await route.fulfill({ json: body });
  });
  return unexpected;
}

async function openSettings(page: Page) {
  const sidebar = page.locator(".sidebar");
  if (!(await sidebar.isVisible()) || (await page.viewportSize())!.width <= 700) {
    await page.getByRole("button", { name: "展开侧边栏", exact: true }).click();
  }
  await sidebar.getByRole("link", { name: "⚙ 设置", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "设置", exact: true })).toBeVisible();
}

test.beforeAll(async ({ request, baseURL }) => {
  if (process.env.E2E_SOURCE_TESTS) {
    expect(new URL(baseURL!).port).not.toBe("18000");
  } else {
    expect((await (await request.get("/__test__/identity")).json()).fake_services_used).toBe(true);
  }
  expect(process.env.E2E_VIDEO_PATH).toBeTruthy();
});

test.beforeEach(async ({ page }) => {
  const unexpected = await mockAPI(page);
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  (page as any).themeErrors = { unexpected, errors };
});
test.afterEach(async ({ page }) => {
  expect((page as any).themeErrors).toEqual({ unexpected: [], errors: [] });
});

for (const theme of ["light", "dark"] as const) {
  test(`侧栏任务图标在${theme}主题保持亮色和状态区分`, async ({ page }) => {
    test.skip(test.info().project.name !== "desktop-1280", "用例内部已遍历完整尺寸矩阵，只执行一次。");
    await page.addInitScript(value => localStorage.setItem("studybuddy-theme", value), theme);
    const examples = [
      { id: "running", filename: "正在生成.mp4", status: "running", label: "处理中" },
      { id: "completed", filename: "完成笔记.mp4", status: "succeeded", label: "已总结" },
      { id: "stopped", filename: "停止任务.mp4", status: "paused", label: "已停止" },
    ];
    await page.route("**/api/v1/videos", route => route.fulfill({ json: examples.map(file => ({
      ...file, duration: 120, size_bytes: 100, created_at: "2026-10-23T00:00:00Z",
      latest_task: taskResponse({ ...task, id: file.id, video_id: file.id, status: file.status, notes_state: "none", notes_revision: 0 }),
    })) }));
    await page.route("**/api/v1/documents", route => route.fulfill({ json: [{ id: "failed-document", kind: "document", filename: "失败文档.pdf",
      format: "pdf", size_bytes: 100, unit_count: 1, character_count: 100, warnings: [],
      latest_task: { ...task, id: "document-task", document_id: "failed-document", status: "failed" } }] }));
    for (const width of [1280, 375]) {
      await page.setViewportSize({ width, height: 871 });
      await page.goto("/");
      if (width === 375) await page.getByRole("button", { name: "展开侧边栏", exact: true }).click();
      const expected = theme === "light"
        ? ["rgb(242, 196, 13)", "rgb(40, 189, 100)", "rgb(226, 54, 54)"]
        : ["rgb(232, 201, 138)", "rgb(169, 216, 191)", "rgb(255, 180, 180)"];
      for (const [index, file] of examples.entries()) {
        const item = page.locator(".sidebar .library-item").filter({ hasText: file.filename });
        await expect(item).toContainText(file.label);
        await expect(item.locator(".file-glyph")).toHaveCSS("color", expected[index]);
      }
      await expect(page.locator(".sidebar .file-glyph.failed")).toHaveCSS("color", expected[2]);
      await expect(page.locator(".sidebar .file-glyph.failed")).toHaveText("▤");
      const ratios = await page.locator(".sidebar .file-glyph").evaluateAll(elements => {
        const root = getComputedStyle(document.documentElement);
        const rgb = (color: string) => color.match(/[\d.]+/g)!.slice(0, 3).map(Number);
        const luminance = (color: string) => rgb(color).map(n => n / 255)
          .map(n => n <= 0.04045 ? n / 12.92 : ((n + 0.055) / 1.055) ** 2.4)
          .reduce((sum, n, i) => sum + n * [0.2126, 0.7152, 0.0722][i], 0);
        const backgrounds = [getComputedStyle(document.querySelector(".sidebar")!).backgroundColor,
          ...["--surface", "--surface-hover"].map(name => {
            const raw = root.getPropertyValue(name).trim().slice(1);
            const hex = raw.length === 3 ? [...raw].map(c => c + c).join("") : raw;
            return `rgb(${[0, 2, 4].map(i => parseInt(hex.slice(i, i + 2), 16)).join(",")})`;
          })];
        return elements.flatMap(element => {
          const style = getComputedStyle(element);
          const color = parseFloat(style.webkitTextStrokeWidth) > 0 ? style.webkitTextStrokeColor : style.color;
          return backgrounds.map(background => {
            const a = luminance(color), b = luminance(background);
            return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
          });
        });
      });
      expect(Math.min(...ratios)).toBeGreaterThanOrEqual(3);
      if (test.info().project.name === "desktop-1280") await page.screenshot({
        path: test.info().outputPath(`sidebar-status-${theme}-${width}.png`), animations: "disabled",
      });
    }
  });
}

for (const current of [false, true]) {
  test(`项目更名迁移旧偏好${current ? "且优先保留新设置" : "并保留主题与侧栏状态"}`, async ({ page }) => {
    await page.addInitScript(hasCurrent => {
      if (sessionStorage.getItem("rename-seeded")) return;
      localStorage.setItem("studynote-theme", "dark");
      localStorage.setItem("studynote-sidebar-collapsed", "true");
      localStorage.setItem("studynotebook-unrelated", "keep");
      if (hasCurrent) {
        localStorage.setItem("studybuddy-theme", "light");
        localStorage.setItem("studybuddy-sidebar-collapsed", "false");
      }
      sessionStorage.setItem("rename-seeded", "true");
    }, current);
    await page.goto("/#summary");
    await expect(page).toHaveTitle(/^StudyBuddy/);
    await expect(page.locator("html")).toHaveAttribute("data-theme", current ? "light" : "dark");
    if (current) await expect(page.locator(".sidebar")).toBeVisible();
    else await expect(page.locator(".sidebar")).toBeHidden();
    expect(await page.evaluate(() => ({
      theme: localStorage.getItem("studybuddy-theme"),
      collapsed: localStorage.getItem("studybuddy-sidebar-collapsed"),
      oldTheme: localStorage.getItem("studynote-theme"),
      oldCollapsed: localStorage.getItem("studynote-sidebar-collapsed"),
      unrelated: localStorage.getItem("studynotebook-unrelated"),
    }))).toEqual({ theme: current ? "light" : "dark", collapsed: current ? "false" : "true",
      oldTheme: null, oldCollapsed: null, unrelated: "keep" });
    await page.reload();
    await expect(page.locator("html")).toHaveAttribute("data-theme", current ? "light" : "dark");
    if (!current) await page.getByRole("button", { name: "展开侧边栏", exact: true }).click();
    await expect(page.locator(".sidebar .brand")).toHaveText("StudyBuddy");
  });
}

test("默认亮色不跟随系统，键盘切换不导航或发写请求", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "dark" });
  await page.goto("/#summary");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await expect(page.locator("html")).toHaveCSS("color-scheme", "light");
  const writes: string[] = [];
  page.on("request", request => { if (request.method() !== "GET") writes.push(request.url()); });
  const toggle = page.locator(".theme-toggle");
  await expect(toggle).toHaveAttribute("title", "切换到暗色模式");
  await toggle.focus();
  await expect(toggle).toHaveCSS("outline-style", "solid");
  await page.keyboard.press("Enter");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(page.locator("html")).toHaveCSS("color-scheme", "dark");
  await expect(toggle).toHaveAttribute("aria-label", "切换到亮色模式");
  await page.keyboard.press("Space");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await expect(page).toHaveURL(/#summary$/);
  expect(writes).toEqual([]);
  await expect(page.locator(".topbar a[href='#settings']")).toHaveCount(0);
});

test("切换后刷新、导航与重新打开保留选择", async ({ page, context }) => {
  await page.goto("/#summary");
  await page.getByRole("button", { name: "切换到暗色模式", exact: true }).click();
  expect(await page.evaluate(() => localStorage.getItem("studybuddy-theme"))).toBe("dark");
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.locator(".sidebar").getByRole("button", { name: "笔记中心", exact: true }).click();
  await expect(page).toHaveURL(/#notes/);
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  const reopened = await context.newPage();
  await mockAPI(reopened);
  await reopened.goto("/#summary");
  await expect(reopened.locator("html")).toHaveAttribute("data-theme", "dark");
  await reopened.getByRole("button", { name: "切换到亮色模式", exact: true }).click();
  await reopened.reload();
  await expect(reopened.locator("html")).toHaveAttribute("data-theme", "light");
  await reopened.close();
});

test("已保存暗色在应用脚本执行前生效", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("studybuddy-theme", "dark"));
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  await page.route(process.env.E2E_SOURCE_TESTS ? "**/src/main.tsx" : "**/assets/*.js", async route => { await gate; await route.continue(); });
  try {
    await page.goto("/#summary", { waitUntil: "commit" });
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
    if (!process.env.E2E_SOURCE_TESTS) await expect(page.locator("html")).toHaveCSS("background-color", "rgb(24, 24, 27)");
    await expect(page.locator("#root")).toBeEmpty();
  } finally { release(); }
  // Vite imports CSS through main.tsx; built pages load the stylesheet independently of JavaScript.
  if (process.env.E2E_SOURCE_TESTS) await expect(page.locator("html")).toHaveCSS("background-color", "rgb(24, 24, 27)");
  await expect(page.getByRole("button", { name: "切换到亮色模式", exact: true })).toBeVisible();
});

for (const storage of ["invalid", "read-blocked", "write-blocked"] as const) {
  test(`存储 ${storage} 时默认亮色且仍可切换`, async ({ page }) => {
    await page.addInitScript(mode => {
      if (mode === "invalid") localStorage.setItem("studybuddy-theme", "unknown");
      else if (mode === "read-blocked") Object.defineProperty(window, "localStorage", { get() { throw new DOMException("Blocked", "SecurityError"); } });
      else Storage.prototype.setItem = () => { throw new DOMException("Full", "QuotaExceededError"); };
    }, storage);
    await page.goto("/#summary");
    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
    await page.getByRole("button", { name: "切换到暗色模式", exact: true }).click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
    await page.getByRole("button", { name: "切换到亮色模式", exact: true }).click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  });
}

test("两种主题在三个桌面宽度保持顶部布局，设置可由侧栏访问", async ({ page }) => {
  test.skip(test.info().project.name !== "desktop-1280", "用例内部已遍历尺寸和主题矩阵，只执行一次。");
  await page.goto("/#summary");
  for (const width of [554, 887, 1280]) {
    await page.setViewportSize({ width, height: 871 });
    for (const theme of ["light", "dark"]) {
      if (await page.locator("html").getAttribute("data-theme") !== theme) await page.locator(".theme-toggle").click();
      const geometry = await page.locator(".theme-toggle").evaluate(button => {
        const box = button.getBoundingClientRect();
        const header = button.closest("header")!.getBoundingClientRect();
        const main = button.closest("main")!.getBoundingClientRect();
        const gutter = parseFloat(getComputedStyle(button.closest("main")!).paddingRight);
        return { width: box.width, height: box.height, rightGap: header.right - box.right, gutter,
          headerLeft: header.left, mainLeft: main.left, headerRight: header.right, viewport: innerWidth,
          overflow: document.documentElement.scrollWidth > innerWidth };
      });
      expect(geometry.width).toBe(32);
      expect(geometry.height).toBe(32);
      expect(Math.abs(geometry.rightGap - geometry.gutter)).toBeLessThan(1);
      expect(Math.abs(geometry.headerLeft - geometry.mainLeft)).toBeLessThan(1);
      expect(Math.abs(geometry.headerRight - geometry.viewport)).toBeLessThan(1);
      expect(geometry.overflow).toBe(false);
      await page.screenshot({ animations: "disabled", path: test.info().outputPath(`files-${theme}-${width}.png`), fullPage: true });
    }
    await openSettings(page);
    await page.getByRole("button", { name: "关闭设置", exact: true }).click();
    await expect(page).toHaveURL(/#summary$/);
    // Collapse before the next width to cover both regular and overlay sidebars.
    if (width > 700) await page.getByRole("button", { name: "折叠侧边栏", exact: true }).click();
  }
});

for (const theme of ["light", "dark"]) {
  test(`${theme} 覆盖菜单、表单、笔记、依据、弹窗和通知`, async ({ page }) => {
    await page.addInitScript(value => localStorage.setItem("studybuddy-theme", value), theme);
    await page.goto("/#summary");
    await page.locator(".file-list").getByRole("button", { name: `管理文件：${course}`, exact: true }).click();
    await expect(page.locator(".file-action-menu")).toBeVisible();
    await page.screenshot({ animations: "disabled", path: test.info().outputPath(`menu-${theme}.png`) });
    await page.getByRole("button", { name: "重命名", exact: true }).click();
    await expect(page.getByRole("dialog", { name: "重命名文件", exact: true })).toBeVisible();
    await page.screenshot({ animations: "disabled", path: test.info().outputPath(`rename-${theme}.png`) });
    await page.getByRole("button", { name: "取消", exact: true }).click();
    await openSettings(page);
    await page.getByRole("tab", { name: "笔记风格", exact: true }).click();
    await page.getByLabel("笔记补充要求（选填）", { exact: true }).fill("保留这个未保存的输入");
    if (theme === "dark") {
      await expect(page.locator(".settings-dialog")).toHaveCSS("background-color", "rgb(36, 36, 40)");
      await expect(page.getByLabel("笔记补充要求（选填）", { exact: true })).toHaveCSS("color", "rgb(237, 237, 240)");
    }
    await page.screenshot({ animations: "disabled", path: test.info().outputPath(`settings-${theme}.png`) });
    await page.evaluate(() => window.dispatchEvent(new CustomEvent("studybuddy-error", { detail: { kind: "error", message: "测试预设错误通知" } })));
    await expect(page.locator(".error-notification")).toBeVisible();
    await page.screenshot({ animations: "disabled", path: test.info().outputPath(`error-${theme}.png`) });
    await page.evaluate(() => window.dispatchEvent(new CustomEvent("studybuddy-success", { detail: { kind: "success", message: "测试预设成功通知" } })));
    await expect(page.locator(".error-notification.success")).toBeVisible();
    await page.screenshot({ animations: "disabled", path: test.info().outputPath(`success-${theme}.png`) });
    await page.getByRole("button", { name: "关闭设置", exact: true }).click();
    await page.locator(".file-name").click();
    await expect(page.locator(".overview h3")).toHaveText(result.title);
    await page.locator(".note-subsection > .cite-button").click();
    await expect(page.locator(".citation-table img")).toBeVisible();
    await expect(page.locator(".citation-table img")).toHaveCSS("filter", "none");
    await page.screenshot({ animations: "disabled", path: test.info().outputPath(`notes-${theme}.png`), fullPage: true });
    await page.getByRole("button", { name: "重新生成笔记", exact: true }).click();
    await expect(page.getByRole("dialog", { name: "选择处理模型", exact: true })).toBeVisible();
    await page.screenshot({ animations: "disabled", path: test.info().outputPath(`setup-${theme}.png`) });
    await page.getByRole("button", { name: "取消", exact: true }).click();
  });
}

test("切换不重新加载播放器或改变播放位置与下载表单", async ({ page }) => {
  await page.goto("/#summary");
  const input = page.getByLabel("视频链接", { exact: true });
  await input.fill("https://example.com/my-course");
  await page.locator(".theme-toggle").click();
  await expect(input).toHaveValue("https://example.com/my-course");
  await page.locator(".file-name").click();
  const player = page.locator("video");
  await expect.poll(() => player.evaluate(element => (element as HTMLVideoElement).readyState)).toBeGreaterThanOrEqual(2);
  const position = await player.evaluate(element => {
    const position = Math.min(15, (element as HTMLVideoElement).duration / 2);
    (element as HTMLVideoElement).currentTime = position;
    (window as any).originalThemePlayer = element;
    return position;
  });
  await expect.poll(() => player.evaluate(element => (element as HTMLVideoElement).currentTime)).toBeCloseTo(position, 1);
  await page.locator(".theme-toggle").click();
  expect(await player.evaluate(element => element === (window as any).originalThemePlayer)).toBe(true);
  expect(await player.evaluate(element => (element as HTMLVideoElement).currentTime)).toBeCloseTo(position, 1);
  await expect(page).toHaveURL(/#summary\/theme-video$/);
});
